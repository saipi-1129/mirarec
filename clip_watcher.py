#!/usr/bin/env python3
"""
Watch MySQL comments for clip command (e.g. "!切り抜き") from a configured user's stream.
When found, create a clip from the current recording and notify Discord.
"""

import pymysql
import os
import sys
import time
import subprocess
import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

# --- Config (env vars override hardcoded defaults) ---
MYSQL_HOST  = os.environ.get("MYSQL_HOST",     "localhost")
MYSQL_USER  = os.environ.get("MYSQL_USER",     "clip")
MYSQL_PASS  = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB    = os.environ.get("MYSQL_DATABASE", "Mirrativ")
MYSQL_TABLE = "comments"

MENTAKO_USER_ID     = os.environ.get("MENTAKO_USER_ID",     "")
CLIP_COMMAND_PREFIX = os.environ.get("CLIP_COMMAND_PREFIX", "!切り抜き")
CLIP_DURATION_DEFAULT = 60  # seconds
CLIP_DURATION_MIN = 10
CLIP_DURATION_MAX = 300

DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "")
DISCORD_MAX_FILE_MB = 25

WEB_SERVER_URL = os.environ.get("WEB_SERVER_URL", "http://localhost:3001")
PUBLIC_URL     = os.environ.get("PUBLIC_URL",     "")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
NAS_DIR    = os.environ.get("NAS_DIR", os.path.join(BASE_DIR, "nas"))
CLIPS_DIR  = os.path.join(NAS_DIR, "clips")
LOG_FILE = os.path.join(BASE_DIR, "clip_watcher.log")
LAST_CHECK_FILE = os.path.join(BASE_DIR, "clip_watcher_last.txt")

POLL_INTERVAL = 1  # seconds

os.makedirs(CLIPS_DIR, exist_ok=True)


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [CLIP_WATCHER] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass


def generate_share_url(clip_name):
    """Call local server to generate a share token, return full public URL or None."""
    try:
        encoded = urllib.parse.quote(clip_name)
        req = urllib.request.Request(
            f"{WEB_SERVER_URL}/api/clips/{encoded}/share",
            data=b'{}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        token_path = data.get("url", "")  # e.g. /share/abc123
        base = PUBLIC_URL.rstrip("/") if PUBLIC_URL else WEB_SERVER_URL.rstrip("/")
        return base + token_path
    except Exception as e:
        log(f"share token generation failed: {e}")
        return None


def send_discord(message, file_path=None):
    """Send message to Discord. If file_path given and ≤25MB, attach the file."""
    try:
        if file_path and os.path.exists(file_path):
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if size_mb <= DISCORD_MAX_FILE_MB:
                import uuid
                boundary = uuid.uuid4().hex
                fname = os.path.basename(file_path)
                with open(file_path, "rb") as f:
                    file_data = f.read()
                payload_json = json.dumps({"content": message}).encode("utf-8")
                body = (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="payload_json"\r\n'
                    f"Content-Type: application/json\r\n\r\n"
                ).encode() + payload_json + (
                    f"\r\n--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                    f"Content-Type: video/mp4\r\n\r\n"
                ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
                req = urllib.request.Request(
                    DISCORD_WEBHOOK, data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "ClipWatcher/1.0"},
                )
                urllib.request.urlopen(req, timeout=60)
                log("Discord notification sent with file")
                return
            else:
                message += f"\n⚠️ ファイルサイズ超過 ({size_mb:.1f}MB) のためファイル送信スキップ"

        payload = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "ClipWatcher/1.0"},
        )
        urllib.request.urlopen(req, timeout=10)
        log("Discord notification sent")
    except Exception as e:
        log(f"Discord notification failed: {e}")


def find_current_recording():
    """Find the active .ts recording file for the configured MENTAKO_USER_ID."""
    try:
        files = [f for f in os.listdir(NAS_DIR) if f.endswith(".ts")]
    except:
        return None

    for f in files:
        json_path = os.path.join(NAS_DIR, os.path.splitext(f)[0] + ".json")
        if os.path.exists(json_path):
            try:
                with open(json_path, "r") as jf:
                    meta = json.load(jf)
                if meta.get("user_id") == MENTAKO_USER_ID:
                    ts_path = os.path.join(NAS_DIR, f)
                    # Check file is actively being written (modified in last 60s)
                    if time.time() - os.path.getmtime(ts_path) < 60:
                        return ts_path, meta
            except:
                continue
    return None


def get_ts_duration(filepath):
    """Get duration of a TS file using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except:
        return 0


def create_clip(ts_path, meta, clip_seconds=None, clip_title=None):
    """Create a clip from the end of the current recording."""
    if clip_seconds is None:
        clip_seconds = CLIP_DURATION_DEFAULT
    duration = get_ts_duration(ts_path)
    if duration <= 0:
        log(f"Could not get duration of {ts_path}")
        return None

    start = max(0, duration - clip_seconds)
    clip_dur = min(clip_seconds, duration)

    ts_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    source_base = os.path.splitext(os.path.basename(ts_path))[0]
    clip_name = f"clip_{source_base}_{ts_name}.mp4"
    clip_path = os.path.join(CLIPS_DIR, clip_name)

    log(f"Creating clip: {clip_name} (from {start:.1f}s, duration {clip_dur:.1f}s)")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start),
        "-i", ts_path,
        "-t", str(clip_dur),
        "-c", "copy",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        clip_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(clip_path):
            log(f"ffmpeg failed: {result.stderr}")
            return None
    except subprocess.TimeoutExpired:
        log("ffmpeg timed out")
        return None

    # Save clip metadata
    clip_meta = {
        "source": os.path.basename(ts_path),
        "clip_start": start,
        "clip_end": start + clip_dur,
        "user_id": meta.get("user_id", ""),
        "user_name": meta.get("user_name", ""),
        "title": meta.get("title", ""),
        "clip_name": clip_title or "",
        "created_at": int(time.time() * 1000),
        "auto_clip": True,
    }
    clip_json = os.path.splitext(clip_path)[0] + ".json"
    with open(clip_json, "w") as f:
        json.dump(clip_meta, f, indent=2, ensure_ascii=False)

    size_mb = os.path.getsize(clip_path) / (1024 * 1024)
    log(f"Clip created: {clip_name} ({size_mb:.1f}MB)")
    return clip_name, size_mb, clip_dur


def load_last_check_time():
    try:
        with open(LAST_CHECK_FILE) as f:
            return datetime.fromisoformat(f.read().strip())
    except:
        return datetime.now()

def save_last_check_time(t):
    try:
        with open(LAST_CHECK_FILE, "w") as f:
            f.write(t.isoformat())
    except:
        pass

def main():
    log("Starting clip watcher...")
    log(f"Watching for '{CLIP_COMMAND_PREFIX}' in comments")

    last_check_time = load_last_check_time()
    log(f"Resuming from {last_check_time}")

    while True:
        try:
            conn = pymysql.connect(
                host=MYSQL_HOST,
                user=MYSQL_USER,
                password=MYSQL_PASS,
                database=MYSQL_DB,
                connect_timeout=5,
                read_timeout=5,
            )
            cur = conn.cursor()
            cur.execute(
                f"SELECT time, name, comment FROM {MYSQL_TABLE} "
                f"WHERE time > %s AND comment LIKE %s "
                f"ORDER BY time ASC",
                (last_check_time, CLIP_COMMAND_PREFIX + '%'),
            )
            rows = cur.fetchall()
            conn.close()

            if rows:
                # Update last_check_time immediately to avoid reprocessing
                last_check_time = max(row[0] for row in rows)
                save_last_check_time(last_check_time)

            for row in rows:
                comment_time, commenter, comment = row
                # Parse: !切り抜き [秒数] [タイトル]
                clip_seconds = CLIP_DURATION_DEFAULT
                clip_title = None
                rest = comment.strip()[len(CLIP_COMMAND_PREFIX):].strip()
                parts = rest.split(None, 1)  # 最大2トークン
                if parts:
                    try:
                        clip_seconds = max(CLIP_DURATION_MIN, min(CLIP_DURATION_MAX, int(parts[0])))
                        clip_title = parts[1].strip() if len(parts) >= 2 else None
                    except ValueError:
                        # 数字でなければタイトルとして扱う
                        clip_title = rest.strip() or None
                log(f"Clip command detected! by {commenter} at {comment_time} ({clip_seconds}s, title={clip_title!r})")

                # Find current recording for the target user
                result = find_current_recording()
                if not result:
                    log(f"No active recording found for user {MENTAKO_USER_ID}")
                    send_discord(f"⚠️ `{commenter}` が `{CLIP_COMMAND_PREFIX}` しましたが、録画が見つかりません")
                    continue

                ts_path, meta = result
                clip_result = create_clip(ts_path, meta, clip_seconds, clip_title)
                if clip_result:
                    clip_name, size_mb, clip_dur = clip_result
                    clip_path = os.path.join(CLIPS_DIR, clip_name)
                    share_url = generate_share_url(clip_name)
                    parts = []
                    if clip_title:
                        parts.append(f"**{clip_title}**")
                    if share_url:
                        parts.append(share_url)
                    msg = "\n".join(parts) if parts else "clip"
                    send_discord(msg, file_path=clip_path)
                else:
                    send_discord(f"❌ 切り抜き作成に失敗しました (コマンド: {commenter})")

            if not rows and (datetime.now() - last_check_time).total_seconds() > 60:
                last_check_time = datetime.now() - timedelta(seconds=2)

        except pymysql.Error as e:
            log(f"MySQL error: {e}")
            time.sleep(5)
            continue
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(5)
            continue

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
