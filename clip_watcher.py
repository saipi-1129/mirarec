#!/usr/bin/env python3
"""
Watch MySQL comments for "!切り抜き" command from めんたこ's stream.
When found, create a 60-second clip from the current recording and notify Discord.
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

CLIP_USER_ID    = os.environ.get("CLIP_USER_ID",    "126246308")
CLIP_COMMAND_PREFIX = os.environ.get("CLIP_COMMAND_PREFIX", "!切り抜き")
CLIP_DURATION_DEFAULT = 60  # seconds
CLIP_DURATION_MIN = 10
CLIP_DURATION_MAX = 300

DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "")
DISCORD_MAX_FILE_MB = 10

# キーワード警報: カンマ区切りで複数指定可
# 空欄の場合は警報機能を無効化
ALERT_KEYWORDS_RAW = os.environ.get("ALERT_KEYWORDS", "")
ALERT_KEYWORDS = [k.strip() for k in ALERT_KEYWORDS_RAW.split(",") if k.strip()]
ALERT_MESSAGE  = os.environ.get("ALERT_MESSAGE", "🚨")

WEB_SERVER_URL = os.environ.get("WEB_SERVER_URL", "http://localhost:3001")
PUBLIC_URL     = os.environ.get("PUBLIC_URL",     "https://mirrativ-record.saipi1129.com")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
NAS_DIR    = os.environ.get("NAS_DIR", os.path.join(BASE_DIR, "data"))
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
    """Find the active .ts recording file for めんたこ (126246308)."""
    import re
    try:
        files = [f for f in os.listdir(NAS_DIR) if f.endswith(".ts")]
    except:
        return None

    # Sort so _segN files come after the base, then we pick the most recently modified
    files.sort()
    candidates = []
    for f in files:
        base = os.path.splitext(f)[0]
        json_path = os.path.join(NAS_DIR, base + ".json")
        # _segN.ts: also try the base name without the _segN suffix
        if not os.path.exists(json_path):
            base_no_seg = re.sub(r'_seg\d+$', '', base)
            json_path = os.path.join(NAS_DIR, base_no_seg + ".json")
        if not os.path.exists(json_path):
            continue
        try:
            with open(json_path, "r") as jf:
                meta = json.load(jf)
            if meta.get("user_id") == CLIP_USER_ID:
                ts_path = os.path.join(NAS_DIR, f)
                mtime = os.path.getmtime(ts_path)
                # Check file is actively being written (modified in last 60s)
                if time.time() - mtime < 60:
                    candidates.append((mtime, ts_path, meta))
        except:
            continue
    if not candidates:
        return None
    # Return the most recently written segment
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


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

    # Re-encode if clip exceeds Discord limit
    size_mb = os.path.getsize(clip_path) / (1024 * 1024)
    if size_mb > DISCORD_MAX_FILE_MB:
        target_kbps = int(DISCORD_MAX_FILE_MB * 8 * 1024 / clip_dur * 0.95)
        video_kbps = max(300, target_kbps - 128)
        log(f"Clip {size_mb:.1f}MB > {DISCORD_MAX_FILE_MB}MB, re-encoding at {video_kbps}kbps")
        reenc_path = clip_path + ".reenc.mp4"
        reenc_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", clip_path,
            "-c:v", "libx264", "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            reenc_path,
        ]
        try:
            r2 = subprocess.run(reenc_cmd, capture_output=True, text=True, timeout=180)
            if r2.returncode == 0 and os.path.exists(reenc_path):
                os.replace(reenc_path, clip_path)
            else:
                log(f"Re-encode failed: {r2.stderr}")
        except subprocess.TimeoutExpired:
            log("Re-encode timed out")

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


    # Web UI に通知
    try:
        urllib.request.urlopen(
            urllib.request.Request('http://localhost:3001/api/internal/clip-created',
                                   data=b'{}', method='POST'),
            timeout=2
        )
    except Exception:
        pass

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

                # Find current recording for めんたこ
                result = find_current_recording()
                if not result:
                    log("No active recording found for めんたこ")
                    send_discord(f"⚠️ `{commenter}` が `!切り抜き` しましたが、めんたこの録画が見つかりません")
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

            # キーワード警報
            if ALERT_KEYWORDS:
                try:
                    conn2 = pymysql.connect(
                        host=MYSQL_HOST, user=MYSQL_USER, password=MYSQL_PASS,
                        database=MYSQL_DB, connect_timeout=5, read_timeout=5,
                    )
                    cur2 = conn2.cursor()
                    conditions = " OR ".join(["comment LIKE %s"] * len(ALERT_KEYWORDS))
                    params = [last_check_time] + [f"%{k}%" for k in ALERT_KEYWORDS]
                    cur2.execute(
                        f"SELECT time, name, comment FROM {MYSQL_TABLE} "
                        f"WHERE time > %s AND ({conditions}) "
                        f"ORDER BY time ASC",
                        params,
                    )
                    alert_rows = cur2.fetchall()
                    conn2.close()
                    for atime, commenter, comment in alert_rows:
                        log(f"キーワード検知: {commenter} -> {comment}")
                        send_discord(f"{ALERT_MESSAGE}\n`{commenter}`: {comment}")
                    if alert_rows:
                        new_t = max(row[0] for row in alert_rows)
                        if new_t > last_check_time:
                            last_check_time = new_t
                            save_last_check_time(last_check_time)
                except Exception as e:
                    log(f"キーワード警報 check error: {e}")

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
