import http.server
import socketserver
import json
import os
import subprocess
import urllib.parse
import re
import mimetypes
import shutil
import time
import sys
import threading
import signal
import secrets
import hashlib
import http.cookies
import asyncio
import collections
import pymysql

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

PORT = int(os.environ.get('PORT', 3001))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# --- Saved config (written by setup wizard, lower priority than env vars) ---
CONFIG_DIR = os.environ.get('CONFIG_DIR', os.path.join(BASE_DIR, 'config'))
SETUP_CONFIG_FILE = os.path.join(CONFIG_DIR, 'server_config.json')
os.makedirs(CONFIG_DIR, exist_ok=True)

_saved_config = {}
if os.path.exists(SETUP_CONFIG_FILE):
    try:
        with open(SETUP_CONFIG_FILE) as f:
            _saved_config = json.load(f)
    except Exception:
        pass

def _cfg(env_key, config_key, default=''):
    """env var > saved config > default"""
    v = os.environ.get(env_key)
    if v is not None:
        return v
    return _saved_config.get(config_key, default)

def is_setup_complete():
    return bool(_saved_config.get('setup_complete')) or bool(os.environ.get('ADMIN_PASS'))

# MySQL: env varがあれば有効、なければ saved_config の use_mysql に従う
if os.environ.get('MYSQL_HOST'):
    MYSQL_DISABLED = False
elif _saved_config:
    MYSQL_DISABLED = not bool(_saved_config.get('use_mysql'))
else:
    MYSQL_DISABLED = True  # セットアップ未完了時は無効

MYSQL_CONFIG = {
    'host':     _cfg('MYSQL_HOST',     'mysql_host',     'localhost'),
    'user':     _cfg('MYSQL_USER',     'mysql_user',     'clip'),
    'password': _cfg('MYSQL_PASSWORD', 'mysql_password', ''),
    'database': _cfg('MYSQL_DATABASE', 'mysql_database', 'Mirrativ'),
    'connect_timeout': 5,
    'read_timeout': 5,
    'charset': 'utf8mb4',
}

def get_db():
    if MYSQL_DISABLED:
        raise RuntimeError('MySQL is disabled')
    return pymysql.connect(**MYSQL_CONFIG)

NAS_DIR = os.environ.get('NAS_DIR', os.path.join(BASE_DIR, '../data'))
TARGETS_FILE = os.environ.get('TARGETS_FILE', os.path.join(CONFIG_DIR, 'targets.json'))
PID_DIR = os.environ.get('PID_DIR', os.path.join(BASE_DIR, '../pids'))
MANAGER_SCRIPT = os.environ.get('MANAGER_SCRIPT', os.path.join(BASE_DIR, '../manage_recordings.sh'))
THUMBNAILS_DIR = os.path.join(NAS_DIR, 'images')
CLIPS_DIR = os.path.join(NAS_DIR, 'clips')
API_LOG_FILE = os.path.join(NAS_DIR, '.api_log')

# Ensure directories exist
os.makedirs(NAS_DIR, exist_ok=True)
os.makedirs(PID_DIR, exist_ok=True)
os.makedirs(THUMBNAILS_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)

# --- API call monitoring ---
_api_log = collections.deque(maxlen=300)
_api_log_lock = threading.Lock()

def _add_api_log(entry):
    with _api_log_lock:
        _api_log.append(entry)
    ws_broadcast('api_log', entry)

def _log_api(source, url, status, elapsed_ms, user_id=''):
    endpoint = url.replace('https://www.mirrativ.com/api/', '').split('?')[0]
    entry = {
        'ts': int(time.time() * 1000),
        'source': source,
        'endpoint': endpoint,
        'status': status,
        'ms': round(elapsed_ms),
        'user': str(user_id),
    }
    _add_api_log(entry)

def api_log_file_watcher():
    """Watch .api_log file written by recorder_single.sh and import entries."""
    last_pos = 0
    while True:
        try:
            if os.path.exists(API_LOG_FILE):
                with open(API_LOG_FILE) as f:
                    f.seek(last_pos)
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                _add_api_log(json.loads(line))
                            except Exception:
                                pass
                    last_pos = f.tell()
        except Exception:
            pass
        time.sleep(3)

# Authentication
ADMIN_USERNAME = _cfg('ADMIN_USER', 'admin_user', 'admin')
_admin_pass = _cfg('ADMIN_PASS', 'admin_pass', '')
ADMIN_PASSWORD_HASH = (
    _saved_config.get('admin_pass_hash') if not os.environ.get('ADMIN_PASS') and _saved_config.get('admin_pass_hash')
    else hashlib.sha256(_admin_pass.encode()).hexdigest()
)
_guest_ids_raw = _cfg('GUEST_USER_IDS', 'guest_user_ids', '')
GUEST_VISIBLE_USER_IDS = set(x for x in _guest_ids_raw.split(',') if x)
sessions = {}  # token -> {"role": "admin" | "guest"}
sessions_lock = threading.Lock()

# Transcription jobs: {filename: {'status': 'pending'|'processing'|'done'|'error', 'segments': [...], 'error': str}}
transcription_jobs = {}
transcription_lock = threading.Lock()
TRANSCRIPTION_MAX_CONCURRENCY = max(1, int(os.environ.get('TRANSCRIPTION_MAX_CONCURRENCY', '1')))
transcription_slots = threading.Semaphore(TRANSCRIPTION_MAX_CONCURRENCY)
TRANSCRIPTS_DIR = os.path.join(NAS_DIR, 'transcripts')
os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

DISCORD_WEBHOOK = _cfg('DISCORD_WEBHOOK_RECORDER', 'discord_webhook_recorder', '')
PUBLIC_URL = _cfg('PUBLIC_URL', 'public_url', '')

def send_discord(message):
    try:
        import urllib.request
        payload = json.dumps({'content': message}).encode()
        req = urllib.request.Request(DISCORD_WEBHOOK, data=payload,
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'MiraRec/1.0'})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

def _fmt_secs(secs):
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def auto_detect_and_notify(filename, interval=2):
    """録画終了後に自動でdetectを実行し、Discord通知する"""
    video_path = os.path.join(NAS_DIR, filename)
    if not os.path.exists(video_path):
        print(f'[AUTO_DETECT] file not found: {filename}')
        return

    job_key = f'{filename}::i{interval}'
    cache_dir = os.path.join(NAS_DIR, '.detect_cache')
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, filename + f'.i{interval}.json')

    # すでに有効なキャッシュがあればスキップ
    if os.path.exists(cache_file):
        mtime_vid = os.path.getmtime(video_path)
        if os.path.getmtime(cache_file) > mtime_vid:
            print(f'[AUTO_DETECT] cache hit, skipping: {filename}')
            return

    # すでに実行中ならスキップ
    with detect_jobs_lock:
        existing = detect_jobs.get(job_key)
        if existing and existing.get('status') == 'running':
            print(f'[AUTO_DETECT] job already running: {filename}')
            return

    print(f'[AUTO_DETECT] starting: {filename} interval={interval}')
    job = {'status': 'running', 'progress_done': 0, 'progress_total': 1, 'result': None, 'error': None}
    with detect_jobs_lock:
        detect_jobs[job_key] = job

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'detect_mirrativ.py')
    try:
        proc = subprocess.Popen(
            ['python3', script, video_path, str(interval)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        def _read_stderr():
            for line in proc.stderr:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                    with detect_jobs_lock:
                        detect_jobs[job_key]['progress_done'] = p.get('done', 0)
                        detect_jobs[job_key]['progress_total'] = p.get('total', 1)
                except Exception:
                    pass
        t = threading.Thread(target=_read_stderr, daemon=True)
        t.start()
        stdout = proc.stdout.read()
        proc.wait(timeout=3600)
        t.join()
        if proc.returncode != 0:
            with detect_jobs_lock:
                detect_jobs[job_key]['status'] = 'error'
                detect_jobs[job_key]['error'] = '解析スクリプトがエラーで終了しました'
            send_discord(f'❌ **自動検出失敗** `{filename}`\n解析スクリプトエラー')
            return
        data = json.loads(stdout)
        with open(cache_file, 'w') as f:
            json.dump(data, f)
        with detect_jobs_lock:
            detect_jobs[job_key]['status'] = 'done'
            detect_jobs[job_key]['result'] = data

        segs = data.get('segments', [])
        dur = data.get('duration', 0)
        if segs:
            lines = [f'🔍 **画面検出完了** `{filename}`',
                     f'動画: {_fmt_secs(dur)} / **{len(segs)}件** の非ミラティブ画面を検出']
            for seg in segs[:10]:
                length = round(seg['end'] - seg['start'])
                lines.append(f'  • {_fmt_secs(seg["start"])} 〜 {_fmt_secs(seg["end"])} ({length}秒)')
            if len(segs) > 10:
                lines.append(f'  …他{len(segs) - 10}件')
            send_discord('\n'.join(lines))
        else:
            send_discord(f'✅ **画面検出完了** `{filename}`\nミラティブ以外の画面は検出されませんでした')
    except Exception as e:
        with detect_jobs_lock:
            detect_jobs[job_key]['status'] = 'error'
            detect_jobs[job_key]['error'] = str(e)
        send_discord(f'❌ **自動検出エラー** `{filename}`\n{e}')

def run_transcription(filepath, filename):
    transcript_path = os.path.join(TRANSCRIPTS_DIR, filename + '.json')
    with transcription_lock:
        transcription_jobs[filename] = {'status': 'queued', 'segments': []}
    transcription_slots.acquire()
    try:
        with transcription_lock:
            transcription_jobs[filename] = {'status': 'processing', 'segments': []}
        from faster_whisper import WhisperModel
        model = WhisperModel('medium', device='cpu', compute_type='int8')
        segments_iter, info = model.transcribe(
            filepath, language='ja', beam_size=5,
            condition_on_previous_text=False,
            vad_filter=True,
            no_speech_threshold=0.6,
        )
        segs = []
        for seg in segments_iter:
            segs.append({'start': round(seg.start, 2), 'end': round(seg.end, 2), 'text': seg.text.strip()})
        with open(transcript_path, 'w', encoding='utf-8') as f:
            json.dump({'segments': segs, 'language': info.language}, f, ensure_ascii=False, indent=2)
        # Save to MySQL
        try:
            conn = get_db()
            cur = conn.cursor()
            segs_json = json.dumps(segs, ensure_ascii=False)
            cur.execute('INSERT INTO transcripts (filename, segments, language) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE segments=%s, language=%s',
                        (filename, segs_json, info.language, segs_json, info.language))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as db_err:
            print(f"Transcript MySQL save error: {db_err}")
        with transcription_lock:
            transcription_jobs[filename] = {'status': 'done', 'segments': segs}
        print(f"Transcription done: {filename} ({len(segs)} segments)")
        send_discord(f"📝 **文字起こし完了**\n🎬 `{filename}`\n📊 {len(segs)}セグメント")
    except Exception as e:
        print(f"Transcription error for {filename}: {e}")
        with transcription_lock:
            transcription_jobs[filename] = {'status': 'error', 'segments': [], 'error': str(e)}
    finally:
        transcription_slots.release()

# Live comments cache: {filename: {'comments': [...], 'highlights': [...]}}
livecomments_cache = {}
livecomments_lock = threading.Lock()

MENTAKO_USER_ID = os.environ.get('MENTAKO_USER_ID', '')

def fetch_live_comments_db(start_time_ms, duration_s):
    """Fetch comments from MySQL (めんたこ専用)."""
    import datetime
    start_utc = datetime.datetime.fromtimestamp(start_time_ms / 1000, tz=datetime.timezone.utc)
    start_jst = start_utc + datetime.timedelta(hours=9)
    end_jst = start_jst + datetime.timedelta(seconds=max(duration_s + 300, 3600))
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            'SELECT time, name, comment FROM comments WHERE time >= %s AND time <= %s ORDER BY time',
            (start_jst.replace(tzinfo=None), end_jst.replace(tzinfo=None))
        )
        rows = cur.fetchall()
        db.close()
    except Exception as e:
        print(f"DB error fetching comments: {e}")
        return [], []
    comments = []
    for row in rows:
        rel = (row[0] - start_jst.replace(tzinfo=None)).total_seconds()
        if rel < 0:
            continue
        comments.append({'time': round(rel, 1), 'user': row[1], 'text': row[2]})
    return comments, detect_comment_highlights(comments)

def fetch_live_comments_collected(live_id, user_id, start_time_ms):
    """Fetch comments from collected live_comments_{user_id} table."""
    table = _comment_table(user_id)
    start_s = start_time_ms / 1000
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(f'SHOW TABLES LIKE %s', (table,))
        if not cur.fetchone():
            db.close()
            return [], []
        cur.execute(
            f'SELECT user_name, comment, comment_time FROM `{table}` WHERE live_id=%s ORDER BY comment_time',
            (live_id,)
        )
        rows = cur.fetchall()
        db.close()
        comments = [
            {'time': round(r[2] - start_s, 1), 'user': r[0], 'text': r[1]}
            for r in rows if r[2] - start_s >= 0
        ]
        return comments, detect_comment_highlights(comments)
    except Exception as e:
        print(f"fetch_live_comments_collected error: {e}")
        return [], []

def fetch_live_comments_api(live_id, start_time_ms, duration_s):
    """Fetch comments from Mirrativ API for non-めんたこ users."""
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    start_s = start_time_ms / 1000
    url = f'https://www.mirrativ.com/api/live/live_comments?live_id={live_id}'
    comments = []
    t0 = time.time()
    status = 0
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            status = r.status
            data = json.loads(r.read())
        for c in data.get('comments', []):
            rel = float(c.get('created_at', 0)) - start_s
            if rel < 0:
                continue
            comments.append({'time': round(rel, 1), 'user': c.get('user_name', ''), 'text': c.get('comment', '')})
        comments.sort(key=lambda x: x['time'])
    except Exception as e:
        status = -1
        print(f"API error fetching comments: {e}")
    finally:
        _log_api('server', url, status, (time.time() - t0) * 1000)
    return comments, detect_comment_highlights(comments)

def detect_comment_highlights(comments, window=30, top_n=5):
    if not comments:
        return []
    buckets = {}
    for c in comments:
        b = int(c['time'] // window) * window
        buckets[b] = buckets.get(b, 0) + 1
    top = sorted(buckets.items(), key=lambda x: -x[1])[:top_n]
    top.sort(key=lambda x: x[0])
    return [{'time': t, 'count': cnt} for t, cnt in top]

# --- Live comment collector ---
_collector_state = {}  # live_id -> {'user_id', 'last_fetched'}
_collector_lock = threading.Lock()
USER_AGENT_COLLECT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

def _comment_table(user_id):
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', str(user_id))
    return f'live_comments_{safe}'

def _ensure_comment_table(db, user_id):
    table = _comment_table(user_id)
    cur = db.cursor()
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS `{table}` (
            id INT AUTO_INCREMENT PRIMARY KEY,
            live_id VARCHAR(255) NOT NULL,
            user_name VARCHAR(255),
            comment TEXT,
            comment_time INT,
            INDEX idx_live (live_id),
            UNIQUE KEY uniq (live_id, user_name(100), comment_time)
        ) CHARACTER SET utf8mb4
    ''')
    db.commit()

def _fetch_and_store_comments(live_id, user_id):
    url = f'https://www.mirrativ.com/api/live/live_comments?live_id={live_id}'
    t0 = time.time()
    status = 0
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT_COLLECT})
        with urllib.request.urlopen(req, timeout=10) as r:
            status = r.status
            data = json.loads(r.read())
        comments = data.get('comments', [])
        _log_api('collector', url, status, (time.time() - t0) * 1000, user_id)
        if not comments:
            return 0
        db = get_db()
        _ensure_comment_table(db, user_id)
        table = _comment_table(user_id)
        cur = db.cursor()
        inserted = 0
        for c in comments:
            try:
                cur.execute(
                    f'INSERT IGNORE INTO `{table}` (live_id, user_name, comment, comment_time) VALUES (%s, %s, %s, %s)',
                    (live_id, c.get('user_name', ''), c.get('comment', ''), int(c.get('created_at', 0)))
                )
                inserted += cur.rowcount
            except Exception:
                pass
        db.commit()
        db.close()
        print(f"[COMMENT_COLLECTOR] {live_id} ({user_id}): +{inserted}/{len(comments)}")
        return inserted
    except Exception as e:
        status = -1
        _log_api('collector', url, status, (time.time() - t0) * 1000, user_id)
        print(f"[COMMENT_COLLECTOR] error {live_id}: {e}")
        return 0

def comment_collector_loop():
    """Collect comments every 10min for active recordings; final fetch on stream end."""
    INTERVAL = 120  # 2 minutes
    CHECK_EVERY = 60  # check for new/ended streams every 60s
    while True:
        try:
            current = set()
            for fname in os.listdir(NAS_DIR):
                if not fname.endswith('.ts'):
                    continue
                ts_path = os.path.join(NAS_DIR, fname)
                if time.time() - os.path.getmtime(ts_path) > 120:
                    continue
                json_path = os.path.join(NAS_DIR, os.path.splitext(fname)[0] + '.json')
                if not os.path.exists(json_path):
                    continue
                try:
                    with open(json_path) as jf:
                        meta = json.load(jf)
                except Exception:
                    continue
                live_id = meta.get('live_id', '')
                user_id = meta.get('user_id', '')
                if not live_id or not user_id:
                    continue
                if user_id == MENTAKO_USER_ID:
                    continue  # めんたこはMySQLに別途保存済み
                current.add(live_id)
                now = time.time()
                with _collector_lock:
                    state = _collector_state.get(live_id)
                    if state is None:
                        _collector_state[live_id] = {'user_id': user_id, 'last_fetched': 0, 'filename': fname}
                        state = _collector_state[live_id]
                    if now - state['last_fetched'] >= INTERVAL:
                        state['last_fetched'] = now
                        threading.Thread(target=_fetch_and_store_comments, args=(live_id, user_id), daemon=True).start()

            # Final fetch for ended streams
            with _collector_lock:
                ended = [lid for lid in _collector_state if lid not in current]
                for live_id in ended:
                    info = _collector_state.pop(live_id)
                    threading.Thread(target=_fetch_and_store_comments, args=(live_id, info['user_id']), daemon=True).start()
                    print(f"[COMMENT_COLLECTOR] stream ended, final fetch: {live_id}")
                    if info.get('filename'):
                        fname_ended = info['filename']
                        threading.Thread(target=auto_detect_and_notify, args=(fname_ended,), daemon=True).start()
                        print(f"[AUTO_DETECT] queued: {fname_ended}")
        except Exception as e:
            print(f"[COMMENT_COLLECTOR] loop error: {e}")
        time.sleep(CHECK_EVERY)

# Clip share tokens: {token: {'filename': str, 'created_at': float}}
SHARE_TOKENS_FILE = os.path.join(BASE_DIR, 'share_tokens.json')
share_tokens_lock = threading.Lock()
SHARE_TOKEN_TTL = 86400  # 24 hours

def _load_share_tokens():
    if os.path.exists(SHARE_TOKENS_FILE):
        try:
            with open(SHARE_TOKENS_FILE, 'r') as f:
                data = json.load(f)
            now = time.time()
            return {t: v for t, v in data.items() if now - v['created_at'] <= SHARE_TOKEN_TTL}
        except Exception:
            pass
    return {}

def _save_share_tokens(tokens):
    try:
        with open(SHARE_TOKENS_FILE, 'w') as f:
            json.dump(tokens, f)
    except Exception:
        pass

share_tokens = _load_share_tokens()

# Detect jobs: {filename: {status, progress_done, progress_total, result, error}}
detect_jobs = {}
detect_jobs_lock = threading.Lock()

# Metadata index cache (filename/live_id -> recording meta)
_meta_index_cache = {'data': None, 'mtime': 0}
_meta_index_lock = threading.Lock()

def _get_metadata_index():
    with _meta_index_lock:
        try:
            dir_mtime = os.path.getmtime(NAS_DIR)
        except Exception:
            dir_mtime = 0
        if _meta_index_cache['data'] is not None and dir_mtime <= _meta_index_cache['mtime']:
            return _meta_index_cache['data']
        by_filename, by_live_id, sorted_by_start = {}, {}, []
        try:
            for fname in os.listdir(NAS_DIR):
                if not fname.endswith('.json') or fname.startswith('.'):
                    continue
                try:
                    with open(os.path.join(NAS_DIR, fname), encoding='utf-8') as f:
                        d = json.load(f)
                    meta = {
                        'filename': d.get('filename', ''),
                        'user_id': str(d.get('user_id', '')),
                        'user_name': d.get('user_name', ''),
                        'title': d.get('title', ''),
                        'start_time': int(d.get('start_time', 0)),
                        'live_id': d.get('live_id', ''),
                    }
                    if meta['filename']:
                        by_filename[meta['filename']] = meta
                    if meta['live_id']:
                        by_live_id[meta['live_id']] = meta
                    if meta['start_time']:
                        sorted_by_start.append(meta)
                except Exception:
                    pass
        except Exception:
            pass
        sorted_by_start.sort(key=lambda m: m['start_time'])
        data = {'by_filename': by_filename, 'by_live_id': by_live_id, 'sorted_by_start': sorted_by_start}
        _meta_index_cache['data'] = data
        _meta_index_cache['mtime'] = dir_mtime
        return data

# Global cache for dir size
_dir_size_cache = {'size': 0, 'last_updated': 0}
_dir_size_lock = threading.Lock()

def generate_missing_thumbnails():
    """Generate thumbnails for all MP4 files that don't have one."""
    try:
        files = [f for f in os.listdir(NAS_DIR) if f.endswith('.mp4')]
    except:
        return
    for f in files:
        thumb_path = os.path.join(THUMBNAILS_DIR, f + '.jpg')
        if os.path.exists(thumb_path):
            continue
        video_path = os.path.join(NAS_DIR, f)
        try:
            subprocess.run([
                'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                '-i', video_path, '-ss', '00:00:05', '-vframes', '1',
                '-vf', 'scale=480:-1', '-q:v', '10', thumb_path
            ], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not os.path.exists(thumb_path):
                subprocess.run([
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', video_path, '-ss', '00:00:00', '-vframes', '1',
                    '-vf', 'scale=480:-1', '-q:v', '10', thumb_path
                ], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(thumb_path):
                print(f"Thumbnail generated: {f}")
        except Exception as e:
            print(f"Thumbnail failed for {f}: {e}")

def prefetch_durations():
    """Pre-warm duration cache for all video files at startup."""
    time.sleep(5)  # wait for server to be ready
    exts = ('.ts', '.mp4')
    for fname in os.listdir(NAS_DIR):
        if not any(fname.endswith(e) for e in exts):
            continue
        fp = os.path.join(NAS_DIR, fname)
        try:
            mtime = os.path.getmtime(fp)
            if fp not in _duration_cache or _duration_cache[fp][0] != mtime:
                get_duration(fp)
        except Exception:
            pass

def update_dir_size_loop():
    while True:
        try:
            total_size = 0
            for root, dirs, files in os.walk(NAS_DIR):
                for f in files:
                    fp = os.path.join(root, f)
                    if not os.path.islink(fp):
                        total_size += os.path.getsize(fp)
            
            with _dir_size_lock:
                _dir_size_cache['size'] = total_size
                _dir_size_cache['last_updated'] = time.time()
        except Exception as e:
            print(f"Error calculating dir size: {e}")
        
        time.sleep(60) # Update every minute

def run_command(cmd):
    try:
        subprocess.run(cmd, shell=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {cmd}, {e}")
        return False

_duration_cache = {}  # file_path -> (mtime, duration)

def get_duration(file_path):
    try:
        mtime = os.path.getmtime(file_path)
        cached = _duration_cache.get(file_path)
        if cached and cached[0] == mtime:
            return cached[1]
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        dur = float(result.stdout.strip())
        _duration_cache[file_path] = (mtime, dur)
        return dur
    except:
        return 0

def get_user_info(user_id):
    url = f"https://www.mirrativ.com/api/user/profile?user_id={user_id}"
    t0 = time.time()
    status = 0
    try:
        cmd = ['wget', '-qO-', '--header=User-Agent: Mozilla/5.0', '--timeout=10', '--tries=2', url]
        result = subprocess.run(cmd, capture_output=True, text=True)
        status = 200 if result.returncode == 0 else 0
        data = json.loads(result.stdout)
        onlive = data.get('onlive')
        live_thumbnail = None
        if onlive:
            live_thumbnail = onlive.get('thumbnail_image_url') or onlive.get('image_url')
        return {
            'userId': user_id,
            'name': data.get('name', f"User_{user_id}"),
            'avatar': data.get('profile_image_url'),
            'live_thumbnail': live_thumbnail
        }
    except Exception as e:
        status = -1
        print(f"Error fetching user info: {e}")
        return None
    finally:
        _log_api('server', url, status, (time.time() - t0) * 1000, user_id)

def check_live_status(user_id):
    pid_file = os.path.join(PID_DIR, f"{user_id}.pid")
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file, 'r') as f:
            parent_pid = f.read().strip()
        
        # Check parent
        os.kill(int(parent_pid), 0)
        
        # Check ffmpeg child
        cmd = f"pgrep -P {parent_pid} -x ffmpeg"
        result = subprocess.run(cmd, shell=True, capture_output=True)
        return result.returncode == 0
    except:
        return False

def load_targets():
    if os.path.exists(TARGETS_FILE):
        try:
            with open(TARGETS_FILE, 'r') as f:
                return json.load(f).get('targets', [])
        except:
            pass
    return []

def save_targets(targets):
    with open(TARGETS_FILE, 'w') as f:
        json.dump({'targets': targets, 'lastUpdated': datetime_iso()}, f, indent=2)

def datetime_iso():
    from datetime import datetime
    return datetime.now().isoformat()

_MIRRATIV_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
_HLS_PROXY_HOSTS = ('mirrativ.com', 'cloudfront.net', 'fastly.net', 'akamaihd.net', 'limelight.com', 'lldns.net')

def _is_allowed_proxy_url(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(':')[0]
        return any(host == h or host.endswith('.' + h) for h in _HLS_PROXY_HOSTS)
    except Exception:
        return False

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('CDN-Cache-Control', 'no-store')
        self.send_header('Cloudflare-CDN-Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json_with_cookie(self, data, cookie_header, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.send_header('Set-Cookie', cookie_header)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def get_session_role(self):
        cookie_header = self.headers.get('Cookie', '')
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(cookie_header)
        except http.cookies.CookieError:
            return 'guest'
        token = cookies.get('session')
        if not token:
            return 'guest'
        with sessions_lock:
            session = sessions.get(token.value)
        if session:
            return session['role']
        return 'guest'

    def require_admin(self):
        role = self.get_session_role()
        if role != 'admin':
            self.send_json({'error': 'Forbidden', 'message': 'Admin access required'}, status=403)
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # セットアップ未完了なら /setup にリダイレクト
        if not is_setup_complete():
            if path == '/setup':
                self._serve_setup()
                return
            if path.startswith('/api/setup'):
                self.handle_setup_status()
                return
            if path.startswith('/api/') or path in ('/', '/index.html'):
                self.send_response(302)
                self.send_header('Location', '/setup')
                self.end_headers()
                return
            super().do_GET()
            return

        if path == '/api/auth/status':
            self.handle_auth_status()
        elif path == '/api/search':
            self.handle_search(parsed.query)
        elif path.startswith('/api/user-avatar/'):
            user_id = path[len('/api/user-avatar/'):]
            self.handle_user_avatar(user_id)
        elif path == '/api/recordings':
            self.handle_recordings()
        elif path == '/api/targets':
            self.handle_targets_get()
        elif path == '/api/system/storage':
            self.handle_system_storage()
        elif path == '/api/system/logs':
            if not self.require_admin():
                return
            self.handle_system_logs()
        elif path == '/api/admin/api-log':
            if not self.require_admin():
                return
            self.handle_api_log_get()
        elif path == '/api/clips':
            self.handle_clips_get()
        elif path == '/api/internal/clip-created' and self.client_address[0] in ('127.0.0.1', '::1', 'localhost'):
            ws_broadcast('clip_update')
            self.send_json({'ok': True})
        elif path.startswith('/api/clip-comments/'):
            filename = urllib.parse.unquote(path.split('/api/clip-comments/')[1])
            self.handle_clip_comments_get(filename)
        elif path.startswith('/api/transcriptions/'):
            filename = urllib.parse.unquote(path.split('/api/transcriptions/')[1])
            self.handle_transcription_get(filename)
        elif path.startswith('/api/duration/'):
            filename = urllib.parse.unquote(path.split('/api/duration/')[1])
            self.handle_duration(filename)
        elif path.startswith('/api/recordings/') and path.endswith('/subtitles'):
            filename = urllib.parse.unquote(path[len('/api/recordings/'):-len('/subtitles')])
            self.handle_subtitles(filename, parsed.query)
        elif path.startswith('/api/recordings/') and path.endswith('/livecomments'):
            filename = urllib.parse.unquote(path[len('/api/recordings/'):-len('/livecomments')])
            self.handle_livecomments(filename)
        elif path.startswith('/api/recordings/') and path.endswith('/detect/status'):
            filename = urllib.parse.unquote(path[len('/api/recordings/'):-len('/detect/status')])
            self.handle_detect_status(filename)
        elif path.startswith('/api/recordings/') and path.endswith('/detect'):
            filename = urllib.parse.unquote(path[len('/api/recordings/'):-len('/detect')])
            self.handle_detect_screen(filename, parsed.query)
        elif path.startswith('/api/detect-frame/'):
            filename = urllib.parse.unquote(path[len('/api/detect-frame/'):])
            self.handle_detect_frame(filename, parsed.query)
        elif path.startswith('/api/clip-thumbnails/'):
            filename = urllib.parse.unquote(path.split('/')[-1])
            self.handle_clip_thumbnail(filename)
        elif path.startswith('/share/'):
            parts = path.split('/')
            token = parts[2] if len(parts) >= 3 else ''
            if len(parts) >= 4 and parts[3] == 'video':
                self.handle_share_video(token)
            else:
                self.handle_share_page(token)
            return
        elif path.startswith('/clip/'):
            filename = urllib.parse.unquote(path.split('/')[-1])
            self.handle_clip_video(filename)
        elif path.startswith('/video/'):
            filename = urllib.parse.unquote(path.split('/')[-1])
            self.handle_video(filename)
        elif path == '/api/hls-proxy':
            self.handle_hls_proxy(parsed.query)
        elif path.startswith('/api/live-stream-url/'):
            filename = urllib.parse.unquote(path[len('/api/live-stream-url/'):])
            self.handle_live_stream_url(filename)
        elif path.startswith('/api/thumbnails/'):
            filename = urllib.parse.unquote(path.split('/')[-1])
            self.handle_thumbnail(filename)
        elif path == '/':
            self.path = '/index.html'
            self._serve_no_cache()
        elif path.endswith('.html') or path.endswith('.js') or path.endswith('.css'):
            self._serve_no_cache()
        else:
            super().do_GET()

    def _serve_no_cache(self):
        """HTMLやJSファイルをno-cacheヘッダー付きで配信"""
        import mimetypes
        fs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static',
                               self.path.lstrip('/'))
        if not os.path.exists(fs_path):
            return self.send_error(404)
        ctype = mimetypes.guess_type(fs_path)[0] or 'text/html'
        with open(fs_path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get('content-length', 0))
        body = self.rfile.read(length) if length > 0 else b'{}'
        try:
            data = json.loads(body)
        except:
            data = {}

        if path == '/api/setup/complete':
            self.handle_setup_complete(data)
            return
        elif path == '/api/setup/test-db':
            self.handle_setup_test_db(data)
            return

        if path == '/api/auth/login':
            self.handle_auth_login(data)
        elif path == '/api/auth/logout':
            self.handle_auth_logout()
        elif path == '/api/auth/guest':
            self.handle_auth_guest()
        elif path == '/api/targets':
            if not self.require_admin():
                return
            self.handle_targets_post(data)
        elif path == '/api/clips':
            self.handle_clips_post(data)
        elif path.startswith('/api/transcriptions/'):
            filename = urllib.parse.unquote(path.split('/api/transcriptions/')[1])
            self.handle_transcription_start(filename)
        elif path.startswith('/api/detect/'):
            filename = urllib.parse.unquote(path[len('/api/detect/'):])
            self.handle_detect_trigger(filename)
        elif path.endswith('/share') and path.startswith('/api/clips/'):
            filename = urllib.parse.unquote(path.split('/')[3])
            self.handle_clip_share(filename)
        elif path.startswith('/api/clip-comments/'):
            filename = urllib.parse.unquote(path.split('/api/clip-comments/')[1])
            self.handle_clip_comments_post(filename, data)
        elif path.endswith('/toggle') and path.startswith('/api/targets/'):
            if not self.require_admin():
                return
            user_id = path.split('/')[3]
            self.handle_target_toggle(user_id, data)
        else:
            self.send_error(404)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get('content-length', 0))
        body = self.rfile.read(length) if length > 0 else b'{}'
        try:
            data = json.loads(body)
        except:
            data = {}

        if path.startswith('/api/clips/'):
            filename = urllib.parse.unquote(path.split('/')[3])
            self.handle_clip_rename(filename, data)
        else:
            self.send_error(404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith('/api/targets/'):
            if not self.require_admin():
                return
            user_id = path.split('/')[3]
            self.handle_target_delete(user_id)
        elif path.startswith('/api/recordings/'):
            if not self.require_admin():
                return
            filename = urllib.parse.unquote(path.split('/')[3])
            self.handle_recording_delete(filename)
        elif path.startswith('/api/clip-comments/'):
            comment_id = path.split('/api/clip-comments/')[1]
            self.handle_clip_comment_delete(comment_id)
        elif path.startswith('/api/clips/'):
            if not self.require_admin():
                return
            filename = urllib.parse.unquote(path.split('/')[3])
            self.handle_clip_delete(filename)
        else:
            self.send_error(404)

    def validate_user_id(self, user_id):
        if not user_id or not re.match(r'^[a-zA-Z0-9_]+$', user_id):
            return False
        return True

    # --- Setup wizard ---
    def _serve_setup(self):
        setup_path = os.path.join(STATIC_DIR, 'setup.html')
        if not os.path.exists(setup_path):
            self.send_error(404)
            return
        with open(setup_path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def handle_setup_status(self):
        self.send_json({'setup_complete': is_setup_complete(), 'mysql_disabled': MYSQL_DISABLED})

    def handle_setup_test_db(self, data):
        try:
            conn = pymysql.connect(
                host=data.get('host', 'localhost'),
                user=data.get('user', 'clip'),
                password=data.get('password', ''),
                database=data.get('database', 'Mirrativ'),
                connect_timeout=5,
                charset='utf8mb4',
            )
            conn.close()
            self.send_json({'ok': True})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    def handle_setup_complete(self, data):
        global MYSQL_DISABLED, MYSQL_CONFIG, ADMIN_USERNAME, ADMIN_PASSWORD_HASH
        global GUEST_VISIBLE_USER_IDS, DISCORD_WEBHOOK, PUBLIC_URL, _saved_config

        use_mysql = data.get('use_mysql', False)
        admin_user = data.get('admin_user', 'admin').strip()
        admin_pass = data.get('admin_pass', '').strip()
        if not admin_user or not admin_pass:
            self.send_json({'ok': False, 'error': 'ユーザー名とパスワードは必須です'})
            return

        config = {
            'setup_complete': True,
            'use_mysql': use_mysql,
            'admin_user': admin_user,
            'admin_pass_hash': hashlib.sha256(admin_pass.encode()).hexdigest(),
            'guest_user_ids': data.get('guest_user_ids', ''),
            'discord_webhook_recorder': data.get('discord_webhook_recorder', ''),
            'discord_webhook_clip': data.get('discord_webhook_clip', ''),
            'public_url': data.get('public_url', ''),
        }
        if use_mysql:
            config.update({
                'mysql_host': data.get('mysql_host', 'localhost'),
                'mysql_user': data.get('mysql_user', 'clip'),
                'mysql_password': data.get('mysql_password', ''),
                'mysql_database': data.get('mysql_database', 'Mirrativ'),
            })

        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(SETUP_CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.send_json({'ok': False, 'error': f'設定の保存に失敗しました: {e}'})
            return

        # Apply config to running process
        _saved_config = config
        MYSQL_DISABLED = not use_mysql
        if use_mysql:
            MYSQL_CONFIG.update({
                'host': config.get('mysql_host', 'localhost'),
                'user': config.get('mysql_user', 'clip'),
                'password': config.get('mysql_password', ''),
                'database': config.get('mysql_database', 'Mirrativ'),
            })
        ADMIN_USERNAME = admin_user
        ADMIN_PASSWORD_HASH = config['admin_pass_hash']
        _g = config.get('guest_user_ids', '')
        GUEST_VISIBLE_USER_IDS = set(x for x in _g.split(',') if x)
        DISCORD_WEBHOOK = config.get('discord_webhook_recorder', '')
        PUBLIC_URL = config.get('public_url', '')

        self.send_json({'ok': True})

    def handle_auth_login(self, data):
        username = data.get('username', '')
        password = data.get('password', '')
        password_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()

        if username == ADMIN_USERNAME and password_hash == ADMIN_PASSWORD_HASH:
            token = secrets.token_hex(32)
            with sessions_lock:
                sessions[token] = {'role': 'admin'}
            cookie = f'session={token}; Path=/; HttpOnly; SameSite=Strict'
            self.send_json_with_cookie({'authenticated': True, 'role': 'admin'}, cookie)
        else:
            self.send_json({'error': 'Invalid credentials'}, status=401)

    def handle_auth_logout(self):
        cookie_header = self.headers.get('Cookie', '')
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(cookie_header)
        except http.cookies.CookieError:
            pass
        token = cookies.get('session')
        if token:
            with sessions_lock:
                sessions.pop(token.value, None)
        cookie = 'session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
        self.send_json_with_cookie({'authenticated': False}, cookie)

    def handle_auth_guest(self):
        token = secrets.token_hex(32)
        with sessions_lock:
            sessions[token] = {'role': 'guest'}
        cookie = f'session={token}; Path=/; HttpOnly; SameSite=Strict'
        self.send_json_with_cookie({'authenticated': True, 'role': 'guest'}, cookie)

    def handle_auth_status(self):
        role = self.get_session_role()
        authenticated = role != 'guest' or self._has_session_cookie()
        self.send_json({
            'authenticated': authenticated,
            'role': role
        })

    def _has_session_cookie(self):
        cookie_header = self.headers.get('Cookie', '')
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(cookie_header)
        except http.cookies.CookieError:
            return False
        token = cookies.get('session')
        if not token:
            return False
        with sessions_lock:
            return token.value in sessions

    def handle_system_storage(self):
        try:
            total, used, free = shutil.disk_usage(NAS_DIR)
            
            # Use cached size
            with _dir_size_lock:
                recordings_size = _dir_size_cache['size']

            self.send_json({
                'total': total,
                'used': used,
                'free': free,
                'recordingsSize': recordings_size
            })
        except Exception as e:
            self.send_error(500, str(e))

    def handle_api_log_get(self):
        cutoff = int(time.time() * 1000) - 10 * 60 * 1000  # 直近10分のみ
        with _api_log_lock:
            data = [e for e in _api_log if e['ts'] >= cutoff]
        self.send_json(data)

    def handle_system_logs(self):
        log_file = os.path.join(BASE_DIR, '../recorder.log')
        try:
            # Simple tail implementation
            if not os.path.exists(log_file):
                return self.send_json({'logs': 'Log file not found.'})
            
            with open(log_file, 'r') as f:
                # Read last 200 lines roughly
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 20000))
                lines = f.readlines()
                # If we cut a line in half, drop the first one
                if len(lines) > 1 and size > 20000:
                    lines = lines[1:]
                
                content = "".join(lines)
                self.send_json({'logs': content})
        except Exception as e:
            self.send_error(500, str(e))

    def handle_transcription_get(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        # Check MySQL first
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT segments FROM transcripts WHERE filename=%s', (filename,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                segs = json.loads(row[0])
                return self.send_json({'status': 'done', 'segments': segs})
        except Exception as db_err:
            print(f"Transcript MySQL get error: {db_err}")
        # Fallback: check saved transcript file
        transcript_path = os.path.join(TRANSCRIPTS_DIR, filename + '.json')
        if os.path.exists(transcript_path):
            try:
                with open(transcript_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return self.send_json({'status': 'done', 'segments': data.get('segments', [])})
            except:
                pass
        # Check in-memory job status
        with transcription_lock:
            job = transcription_jobs.get(filename)
        if job:
            return self.send_json(job)
        return self.send_json({'status': 'none', 'segments': []})

    def handle_transcription_start(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        # Check MySQL first
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT segments FROM transcripts WHERE filename=%s', (filename,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                segs = json.loads(row[0])
                return self.send_json({'status': 'done', 'segments': segs})
        except Exception as db_err:
            print(f"Transcript MySQL check error: {db_err}")
        # Check file
        transcript_path = os.path.join(TRANSCRIPTS_DIR, filename + '.json')
        if os.path.exists(transcript_path):
            return self.send_json({'status': 'done'})
        with transcription_lock:
            job = transcription_jobs.get(filename)
            if job and job['status'] in ('processing', 'pending'):
                return self.send_json({'status': job['status']})
            transcription_jobs[filename] = {'status': 'pending', 'segments': []}
        # Determine file path (recording or clip)
        filepath = os.path.join(NAS_DIR, filename)
        if not os.path.exists(filepath):
            filepath = os.path.join(CLIPS_DIR, filename)
        if not os.path.exists(filepath):
            return self.send_error(404)
        threading.Thread(target=run_transcription, args=(filepath, filename), daemon=True).start()
        self.send_json({'status': 'pending'})

    def handle_user_avatar(self, user_id):
        """GET /api/user-avatar/<user_id> — ディスクキャッシュ付きアバター画像プロキシ"""
        if not user_id or not re.match(r'^\d+$', user_id):
            return self.send_error(400)
        cache_dir = os.path.join(NAS_DIR, '.avatar_cache')
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f'{user_id}.jpg')
        # キャッシュが7日以内なら返す
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < 604800:
            with open(cache_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', len(data))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(data)
            return
        # Mirrativ APIからアバターURL取得
        try:
            info = get_user_info(user_id)
            avatar_url = info['avatar'] if info else None
            if not avatar_url:
                return self.send_error(404)
            cmd = ['wget', '-qO-', f'--header=User-Agent: {_MIRRATIV_UA}', '--timeout=10', avatar_url]
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode != 0 or not result.stdout:
                return self.send_error(502)
            img_data = result.stdout
            with open(cache_path, 'wb') as f:
                f.write(img_data)
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', len(img_data))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(img_data)
        except Exception as e:
            print(f'[AVATAR] {user_id}: {e}')
            self.send_error(502)

    def handle_search(self, query_string):
        """GET /api/search?q=<query>&type=all|transcript|comment"""
        params = urllib.parse.parse_qs(query_string)
        q = params.get('q', [''])[0].strip()
        search_type = params.get('type', ['all'])[0]
        if len(q) < 1:
            return self.send_json([])

        is_guest = self.get_session_role() != 'admin'
        idx = _get_metadata_index()
        by_filename = idx['by_filename']
        by_live_id = idx['by_live_id']
        sorted_by_start = idx['sorted_by_start']
        like_q = f'%{q}%'
        results = []

        try:
            conn = get_db()
            cur = conn.cursor()

            # --- 字幕検索 ---
            if search_type in ('all', 'transcript'):
                cur.execute('SELECT filename, segments FROM transcripts WHERE segments LIKE %s LIMIT 500', (like_q,))
                for filename, segs_json in cur.fetchall():
                    meta = by_filename.get(filename)
                    if is_guest and (not meta or meta['user_id'] != MENTAKO_USER_ID):
                        continue
                    try:
                        segs_data = json.loads(segs_json) if isinstance(segs_json, str) else segs_json
                        if isinstance(segs_data, dict):
                            segs_data = segs_data.get('segments', [])
                        for seg in segs_data:
                            text = seg.get('text', '')
                            if q.lower() in text.lower():
                                t = seg.get('start', 0)
                                results.append({
                                    'type': 'transcript',
                                    'filename': filename,
                                    'user_id': meta['user_id'] if meta else '',
                                    'user_name': meta['user_name'] if meta else '',
                                    'title': meta['title'] if meta else '',
                                    'time': t,
                                    'text': text.strip(),
                                    '_abs_time': (meta['start_time'] if meta else 0) + t * 1000,
                                })
                    except Exception:
                        pass

            # --- コメント検索 ---
            if search_type in ('all', 'comment'):
                import datetime as _dt

                # めんたこ専用 comments テーブル (datetime型)
                cur.execute(
                    'SELECT time, name, comment FROM comments WHERE comment LIKE %s ORDER BY time DESC LIMIT 200',
                    (like_q,)
                )
                for comment_dt, uname, comment in cur.fetchall():
                    ts_ms = comment_dt.timestamp() * 1000
                    meta = None
                    for m in reversed(sorted_by_start):
                        if m['user_id'] == MENTAKO_USER_ID and m['start_time'] <= ts_ms:
                            meta = m
                            break
                    rel = round((ts_ms - meta['start_time']) / 1000, 1) if meta else None
                    results.append({
                        'type': 'comment',
                        'filename': meta['filename'] if meta else None,
                        'user_id': MENTAKO_USER_ID,
                        'user_name': uname,
                        'title': meta['title'] if meta else '',
                        'time': rel,
                        'text': comment,
                        '_abs_time': ts_ms,
                    })

                # 各ユーザーの live_comments_* テーブル (adminのみ)
                if not is_guest:
                    cur.execute("SHOW TABLES LIKE 'live_comments_%'")
                    tables = [r[0] for r in cur.fetchall()]
                    for table in tables:
                        streamer_user_id = table[len('live_comments_'):]
                        cur.execute(
                            f'SELECT live_id, user_name, comment, comment_time FROM `{table}` WHERE comment LIKE %s ORDER BY comment_time DESC LIMIT 100',
                            (like_q,)
                        )
                        for live_id, uname, comment, ctime in cur.fetchall():
                            meta = by_live_id.get(live_id)
                            rel = round(ctime - meta['start_time'] / 1000, 1) if meta else None
                            results.append({
                                'type': 'comment',
                                'filename': meta['filename'] if meta else None,
                                'user_id': streamer_user_id,
                                'user_name': uname,
                                'title': meta['title'] if meta else '',
                                'time': rel,
                                'text': comment,
                                '_abs_time': ctime * 1000,
                            })

            cur.close()
            conn.close()
        except Exception as e:
            print(f'[SEARCH] error: {e}')
            import traceback; traceback.print_exc()

        results.sort(key=lambda r: (r.pop('_abs_time', 0)), reverse=True)
        self.send_json(results[:200])

    def handle_detect_trigger(self, filename):
        """POST /api/detect/<filename>: 認証不要の内部detectトリガー（recorder_single.shから呼び出す）"""
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        video_path = os.path.join(NAS_DIR, filename)
        if not os.path.exists(video_path):
            return self.send_error(404)
        threading.Thread(target=auto_detect_and_notify, args=(filename, 2), daemon=True).start()
        self.send_json({'status': 'queued', 'filename': filename})

    def handle_subtitles(self, filename, query_string):
        """GET /api/recordings/<filename>/subtitles?format=srt|vtt"""
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        params = urllib.parse.parse_qs(query_string)
        fmt = params.get('format', ['srt'])[0].lower()
        if fmt not in ('srt', 'vtt'):
            return self.send_error(400)

        transcript_path = os.path.join(TRANSCRIPTS_DIR, filename + '.json')
        if not os.path.exists(transcript_path):
            return self.send_error(404)
        try:
            with open(transcript_path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return self.send_error(500)

        segs = data if isinstance(data, list) else data.get('segments', [])

        def fmt_time_srt(s):
            h = int(s // 3600)
            m = int((s % 3600) // 60)
            sec = int(s % 60)
            ms = int(round((s - int(s)) * 1000))
            return f'{h:02d}:{m:02d}:{sec:02d},{ms:03d}'

        def fmt_time_vtt(s):
            return fmt_time_srt(s).replace(',', '.')

        if fmt == 'srt':
            lines = []
            for i, seg in enumerate(segs, 1):
                lines.append(str(i))
                lines.append(f'{fmt_time_srt(seg["start"])} --> {fmt_time_srt(seg["end"])}')
                lines.append(seg.get('text', '').strip())
                lines.append('')
            body = '\n'.join(lines).encode('utf-8')
            content_type = 'text/plain; charset=utf-8'
            dl_name = filename + '.srt'
        else:
            lines = ['WEBVTT', '']
            for seg in segs:
                lines.append(f'{fmt_time_vtt(seg["start"])} --> {fmt_time_vtt(seg["end"])}')
                lines.append(seg.get('text', '').strip())
                lines.append('')
            body = '\n'.join(lines).encode('utf-8')
            content_type = 'text/vtt; charset=utf-8'
            dl_name = filename + '.vtt'

        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(body))
        self.send_header('Content-Disposition', f'attachment; filename="{dl_name}"')
        self.end_headers()
        self.wfile.write(body)

    def handle_duration(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        file_path = os.path.join(NAS_DIR, filename)
        if not os.path.exists(file_path):
            return self.send_error(404)
        dur = get_duration(file_path)
        self.send_json({'duration': dur})

    def handle_livecomments(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        with livecomments_lock:
            if filename in livecomments_cache:
                return self.send_json(livecomments_cache[filename])
        base = filename.rsplit('.', 1)[0]
        json_path = os.path.join(NAS_DIR, base + '.json')
        if not os.path.exists(json_path):
            return self.send_json({'comments': [], 'highlights': []})
        try:
            with open(json_path) as f:
                meta = json.load(f)
        except Exception:
            return self.send_json({'comments': [], 'highlights': []})
        start_time_ms = meta.get('start_time', 0)
        if not start_time_ms:
            return self.send_json({'comments': [], 'highlights': []})
        duration_s = get_duration(os.path.join(NAS_DIR, filename)) or 7200
        user_id = meta.get('user_id', '')
        live_id = meta.get('live_id', '')
        if user_id == MENTAKO_USER_ID:
            comments, highlights = fetch_live_comments_db(start_time_ms, duration_s)
        else:
            comments, highlights = fetch_live_comments_collected(live_id, user_id, start_time_ms)
            if not comments:
                comments, highlights = fetch_live_comments_api(live_id, start_time_ms, duration_s)
        result = {'comments': comments, 'highlights': highlights}
        with livecomments_lock:
            livecomments_cache[filename] = result
        self.send_json(result)

    def handle_detect_screen(self, filename, query_string):
        """POST/GET: 解析ジョブを開始し、現在の状態を返す"""
        if not self.require_admin():
            return
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        video_path = os.path.join(NAS_DIR, filename)
        if not os.path.exists(video_path):
            return self.send_error(404)

        params = urllib.parse.parse_qs(query_string)
        interval = int(params.get('interval', ['10'])[0])
        interval = max(1, min(60, interval))
        force = params.get('force', ['0'])[0] == '1'
        job_key = f'{filename}::i{interval}'

        # force時は既存ジョブとキャッシュを削除
        if force:
            with detect_jobs_lock:
                detect_jobs.pop(job_key, None)
            cache_dir = os.path.join(NAS_DIR, '.detect_cache')
            cache_file = os.path.join(cache_dir, filename + f'.i{interval}.json')
            try:
                os.remove(cache_file)
            except OSError:
                pass
        else:
            with detect_jobs_lock:
                job = detect_jobs.get(job_key)
                if job:
                    return self.send_json(job)

        # キャッシュ確認
        cache_dir = os.path.join(NAS_DIR, '.detect_cache')
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, filename + f'.i{interval}.json')
        if not force and os.path.exists(cache_file):
            mtime_vid = os.path.getmtime(video_path)
            if os.path.getmtime(cache_file) > mtime_vid:
                try:
                    with open(cache_file) as f:
                        data = json.load(f)
                    job = {'status': 'done', 'progress_done': 1, 'progress_total': 1, 'result': data}
                    with detect_jobs_lock:
                        detect_jobs[job_key] = job
                    return self.send_json(job)
                except Exception:
                    pass

        # 新規ジョブ開始
        job = {'status': 'running', 'progress_done': 0, 'progress_total': 1, 'result': None, 'error': None}
        with detect_jobs_lock:
            detect_jobs[job_key] = job

        def run_job():
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'detect_mirrativ.py')
            try:
                proc = subprocess.Popen(
                    ['python3', script, video_path, str(interval)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
                # stderrを別スレッドで読み進捗更新
                def read_stderr():
                    for line in proc.stderr:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            p = json.loads(line)
                            with detect_jobs_lock:
                                detect_jobs[job_key]['progress_done'] = p.get('done', 0)
                                detect_jobs[job_key]['progress_total'] = p.get('total', 1)
                        except Exception:
                            pass
                t = threading.Thread(target=read_stderr, daemon=True)
                t.start()
                # communicate()はstderrも読むため競合する。stdout.read()で代替。
                stdout = proc.stdout.read()
                proc.wait(timeout=900)
                t.join()
                if proc.returncode != 0:
                    with detect_jobs_lock:
                        detect_jobs[job_key]['status'] = 'error'
                        detect_jobs[job_key]['error'] = '解析スクリプトがエラーで終了しました'
                    return
                data = json.loads(stdout)
                with open(cache_file, 'w') as f:
                    json.dump(data, f)
                with detect_jobs_lock:
                    detect_jobs[job_key]['status'] = 'done'
                    detect_jobs[job_key]['result'] = data
            except Exception as e:
                with detect_jobs_lock:
                    detect_jobs[job_key]['status'] = 'error'
                    detect_jobs[job_key]['error'] = str(e)

        threading.Thread(target=run_job, daemon=True).start()
        self.send_json(job)

    def handle_detect_status(self, filename):
        """GET: ジョブの現在状態を返す"""
        if not self.require_admin():
            return
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        # クエリを再パース (pathから取れないのでURLそのものから取る)
        raw = self.path.split('?', 1)
        qs = raw[1] if len(raw) > 1 else ''
        params = urllib.parse.parse_qs(qs)
        interval = int(params.get('interval', ['10'])[0])
        interval = max(1, min(60, interval))
        job_key = f'{filename}::i{interval}'
        with detect_jobs_lock:
            job = detect_jobs.get(job_key)
        if not job:
            return self.send_json({'status': 'none'})
        self.send_json(job)

    def handle_detect_frame(self, filename, query_string):
        """GET: 指定時刻のフレームをJPEGで返す"""
        if not self.require_admin():
            return
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        video_path = os.path.join(NAS_DIR, filename)
        if not os.path.exists(video_path):
            return self.send_error(404)
        params = urllib.parse.parse_qs(query_string)
        t = params.get('t', ['0'])[0]
        try:
            float(t)
        except ValueError:
            return self.send_error(400)

        # キャッシュ
        cache_dir = os.path.join(NAS_DIR, '.detect_cache', 'frames')
        os.makedirs(cache_dir, exist_ok=True)
        safe_name = filename.replace('/', '_').replace('\\', '_')
        frame_cache = os.path.join(cache_dir, f'{safe_name}.t{t}.jpg')
        if not os.path.exists(frame_cache):
            try:
                result = subprocess.run(
                    ['ffmpeg', '-ss', t, '-i', video_path,
                     '-vframes', '1', '-vf', 'scale=320:-1',
                     '-q:v', '5', '-loglevel', 'quiet', frame_cache],
                    timeout=15
                )
                if result.returncode != 0 or not os.path.exists(frame_cache):
                    return self.send_error(500)
            except Exception:
                return self.send_error(500)

        with open(frame_cache, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'max-age=86400')
        self.end_headers()
        self.wfile.write(data)

    def handle_thumbnail(self, filename):
        video_path = os.path.join(NAS_DIR, filename)
        thumb_name = filename + '.jpg'
        thumb_path = os.path.join(THUMBNAILS_DIR, thumb_name)
        
        if not os.path.exists(thumb_path):
            if not os.path.exists(video_path):
                return self.send_error(404)
            
            # Generate thumbnail
            try:
                # Capture frame at 5 seconds or 10% if short
                cmd = [
                    'ffmpeg', '-y', '-i', video_path, 
                    '-ss', '00:00:05', 
                    '-vframes', '1', 
                    '-vf', 'scale=480:-1', 
                    '-q:v', '10', 
                    thumb_path
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # If failed (maybe video shorter than 5s), try at 0s
                if not os.path.exists(thumb_path):
                    cmd[4] = '00:00:00'
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"Thumbnail generation failed: {e}")
                return self.send_error(500)

        if os.path.exists(thumb_path):
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(os.path.getsize(thumb_path)))
            self.end_headers()
            with open(thumb_path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        else:
            self.send_error(404)

    def handle_recording_delete(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400, "Invalid filename")
            
        file_path = os.path.join(NAS_DIR, filename)
        if not os.path.exists(file_path):
            return self.send_error(404, "File not found")
            
        try:
            os.remove(file_path)
            
            # Also try to remove json metadata if it exists
            json_path = os.path.splitext(file_path)[0] + '.json'
            if os.path.exists(json_path):
                os.remove(json_path)

            # Remove thumbnail
            thumb_path = os.path.join(THUMBNAILS_DIR, filename + '.jpg')
            if os.path.exists(thumb_path):
                os.remove(thumb_path)

            self.send_json({'success': True})
            ws_broadcast('recording_update')
        except Exception as e:
            print(f"Error deleting file: {e}")
            self.send_error(500, str(e))

    def handle_recordings(self):
        is_guest = self.get_session_role() != 'admin'
        files = []
        try:
            files = [f for f in os.listdir(NAS_DIR) if f.endswith('.mp4') or f.endswith('.ts')]
        except:
            pass

        recordings = []
        files_set = set(files)

        for file in files:
            # Skip TS if MP4 exists
            if file.endswith('.ts'):
                mp4_name = file[:-3] + '.mp4'
                if mp4_name in files_set:
                    continue

            full_path = os.path.join(NAS_DIR, file)
            json_path = os.path.splitext(full_path)[0] + '.json'

            meta = None
            user_id = None
            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r') as f:
                        data = json.load(f)
                        meta = {
                            'displayName': data.get('user_name', 'Unknown'),
                            'title': data.get('title', ''),
                            'startTime': data.get('start_time', 0)
                        }
                        user_id = data.get('user_id', '')
                except:
                    pass

            if not meta:
                meta = {'displayName': file, 'title': '', 'startTime': 0}

            # Guest filter: only show recordings from allowed users
            if is_guest and user_id and user_id not in GUEST_VISIBLE_USER_IDS:
                continue

            # Use cached duration if available, else 0 (client fetches async)
            cached_dur = _duration_cache.get(full_path)
            duration = cached_dur[1] if cached_dur else 0
            recordings.append({
                'filename': file,
                'displayName': meta['displayName'],
                'title': meta['title'],
                'startTime': meta['startTime'],
                'userId': user_id or '',
                'duration': duration,
                'size': os.path.getsize(full_path) if os.path.exists(full_path) else 0
            })

        # Sort newest first
        recordings.sort(key=lambda x: x['startTime'], reverse=True)
        self.send_json(recordings)

    def handle_targets_get(self):
        is_guest = self.get_session_role() != 'admin'
        targets = load_targets()
        enriched = []
        for t in targets:
            user_id = t['userId']
            # Guest filter
            if is_guest and user_id not in GUEST_VISIBLE_USER_IDS:
                continue
            info = get_user_info(user_id)
            if info:
                t['name'] = info['name']
                t['avatar'] = info['avatar']
                t['liveThumbnail'] = info['live_thumbnail']

            is_live = check_live_status(user_id)
            t['isRecording'] = t.get('enabled', True)
            t['isLiveRecording'] = is_live
            t['checkInterval'] = t.get('checkInterval', 30)
            t['retentionDays'] = t.get('retentionDays', 7)
            enriched.append(t)
        self.send_json({'targets': enriched})

    def handle_targets_post(self, data):
        user_id = data.get('userId')
        if not self.validate_user_id(user_id):
            return self.send_error(400, "Invalid User ID")
            
        targets = load_targets()
        if any(t['userId'] == user_id for t in targets):
            return self.send_error(400, "Already exists")
            
        info = get_user_info(user_id)
        if not info:
            return self.send_error(404, "User not found")
            
        check_interval = data.get('checkInterval', 30)
        try:
            check_interval = max(10, min(300, int(check_interval)))
        except (ValueError, TypeError):
            check_interval = 30

        retention_days = data.get('retentionDays', 7)
        try:
            retention_days = max(1, min(365, int(retention_days)))
        except (ValueError, TypeError):
            retention_days = 7

        new_target = {
            'userId': user_id,
            'name': info['name'],
            'addedAt': datetime_iso(),
            'enabled': True,
            'checkInterval': check_interval,
            'retentionDays': retention_days
        }
        targets.append(new_target)
        save_targets(targets)
        
        run_command(f'bash "{MANAGER_SCRIPT}" start "{user_id}"')
        
        new_target['avatar'] = info['avatar']
        new_target['isRecording'] = True
        self.send_json({'success': True, 'target': new_target})
        ws_broadcast('target_update')

    def handle_target_toggle(self, user_id, data):
        if not self.validate_user_id(user_id):
            return self.send_error(400, "Invalid User ID")

        targets = load_targets()
        found = False
        need_restart = False
        for t in targets:
            if t['userId'] == user_id:
                t['enabled'] = data.get('enabled', t.get('enabled', True))
                # Update checkInterval if provided
                if 'checkInterval' in data:
                    try:
                        new_ci = max(10, min(300, int(data['checkInterval'])))
                    except (ValueError, TypeError):
                        new_ci = 30
                    if new_ci != t.get('checkInterval', 30):
                        t['checkInterval'] = new_ci
                        need_restart = True
                # Update retentionDays if provided
                if 'retentionDays' in data:
                    try:
                        new_rd = max(1, min(365, int(data['retentionDays'])))
                    except (ValueError, TypeError):
                        new_rd = 7
                    if new_rd != t.get('retentionDays', 7):
                        t['retentionDays'] = new_rd
                        need_restart = True
                found = True
                if need_restart and t['enabled']:
                    # Restart to apply new interval
                    run_command(f'bash "{MANAGER_SCRIPT}" stop "{user_id}"')
                    import time as _time; _time.sleep(1)
                    run_command(f'bash "{MANAGER_SCRIPT}" start "{user_id}"')
                elif t['enabled']:
                    run_command(f'bash "{MANAGER_SCRIPT}" start "{user_id}"')
                else:
                    run_command(f'bash "{MANAGER_SCRIPT}" stop "{user_id}"')
                break
        
        if found:
            save_targets(targets)
            self.send_json({'success': True})
            ws_broadcast('target_update')
        else:
            self.send_error(404)

    def handle_target_delete(self, user_id):
        if not self.validate_user_id(user_id):
            return self.send_error(400, "Invalid User ID")

        targets = load_targets()
        new_targets = [t for t in targets if t['userId'] != user_id]
        if len(new_targets) == len(targets):
            return self.send_error(404)
            
        save_targets(new_targets)
        run_command(f'bash "{MANAGER_SCRIPT}" stop "{user_id}"')
        self.send_json({'success': True})
        ws_broadcast('target_update')

    def handle_clips_get(self):
        is_guest = self.get_session_role() != 'admin'
        clips = []
        try:
            files = [f for f in os.listdir(CLIPS_DIR) if f.endswith('.mp4')]
        except:
            files = []
        for f in files:
            full_path = os.path.join(CLIPS_DIR, f)
            json_path = os.path.splitext(full_path)[0] + '.json'
            meta = {}
            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r') as jf:
                        meta = json.load(jf)
                except:
                    pass

            # Guest filter: check user_id in clip meta, or look up from source
            if is_guest:
                clip_user_id = meta.get('user_id', '')
                if not clip_user_id and meta.get('source'):
                    source_json = os.path.join(NAS_DIR, os.path.splitext(meta['source'])[0] + '.json')
                    if os.path.exists(source_json):
                        try:
                            with open(source_json, 'r') as sf:
                                clip_user_id = json.load(sf).get('user_id', '')
                        except:
                            pass
                if clip_user_id and clip_user_id not in GUEST_VISIBLE_USER_IDS:
                    continue

            source = meta.get('source', '')
            if source and not os.path.exists(os.path.join(NAS_DIR, source)):
                alt = os.path.splitext(source)[0] + ('.mp4' if source.endswith('.ts') else '.ts')
                if os.path.exists(os.path.join(NAS_DIR, alt)):
                    source = alt
            clips.append({
                'filename': f,
                'source': source,
                'startTime': meta.get('clip_start', 0),
                'endTime': meta.get('clip_end', 0),
                'displayName': meta.get('user_name', ''),
                'title': meta.get('title', ''),
                'clipName': meta.get('clip_name', ''),
                'createdAt': meta.get('created_at', 0),
                'size': os.path.getsize(full_path) if os.path.exists(full_path) else 0
            })
        def _sort_key(c):
            v = c['createdAt']
            if isinstance(v, (int, float)):
                return v / 1000  # ms → sec
            try:
                from datetime import datetime
                return datetime.fromisoformat(str(v)).timestamp()
            except Exception:
                return 0
        clips.sort(key=_sort_key, reverse=True)
        self.send_json(clips)

    def handle_clips_post(self, data):
        source = data.get('filename', '')
        start = data.get('startTime')
        end = data.get('endTime')

        if not source or start is None or end is None:
            return self.send_json({'error': 'filename, startTime, endTime required'}, status=400)
        if '/' in source or '\\' in source or '..' in source:
            return self.send_json({'error': 'Invalid filename'}, status=400)

        start = float(start)
        end = float(end)
        if end <= start or (end - start) < 0.5:
            return self.send_json({'error': 'Invalid time range'}, status=400)

        source_path = os.path.join(NAS_DIR, source)
        if not os.path.exists(source_path):
            return self.send_json({'error': 'Source file not found'}, status=404)

        # Build clip filename
        from datetime import datetime
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = os.path.splitext(source)[0]
        clip_name = f"clip_{base}_{ts}.mp4"
        clip_path = os.path.join(CLIPS_DIR, clip_name)

        # ffmpeg cut with -c copy for speed
        duration = end - start
        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-ss', str(start),
            '-i', source_path,
            '-t', str(duration),
            '-c', 'copy',
            '-movflags', '+faststart',
            '-avoid_negative_ts', 'make_zero',
            clip_path
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0 or not os.path.exists(clip_path):
                return self.send_json({'error': f'ffmpeg failed: {result.stderr}'}, status=500)
        except subprocess.TimeoutExpired:
            return self.send_json({'error': 'Clip creation timed out'}, status=500)

        # Read source metadata
        source_meta = {}
        source_json = os.path.splitext(source_path)[0] + '.json'
        if os.path.exists(source_json):
            try:
                with open(source_json, 'r') as f:
                    source_meta = json.load(f)
            except:
                pass

        # Save clip metadata
        clip_label = data.get('clipName', '').strip()
        clip_meta = {
            'source': source,
            'clip_start': start,
            'clip_end': end,
            'user_id': source_meta.get('user_id', ''),
            'user_name': source_meta.get('user_name', ''),
            'title': source_meta.get('title', ''),
            'clip_name': clip_label,
            'created_at': int(time.time() * 1000)
        }
        clip_json_path = os.path.splitext(clip_path)[0] + '.json'
        with open(clip_json_path, 'w') as f:
            json.dump(clip_meta, f, indent=2)

        # Generate thumbnail
        clip_thumb_dir = os.path.join(CLIPS_DIR, 'images')
        os.makedirs(clip_thumb_dir, exist_ok=True)
        clip_thumb_path = os.path.join(clip_thumb_dir, clip_name + '.jpg')
        try:
            subprocess.run([
                'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                '-i', clip_path, '-ss', '00:00:01', '-vframes', '1',
                '-vf', 'scale=480:-1', '-q:v', '10', clip_thumb_path
            ], timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not os.path.exists(clip_thumb_path):
                subprocess.run([
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', clip_path, '-ss', '00:00:00', '-vframes', '1',
                    '-vf', 'scale=480:-1', '-q:v', '10', clip_thumb_path
                ], timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass

        self.send_json({
            'success': True,
            'filename': clip_name,
            'size': os.path.getsize(clip_path)
        })
        ws_broadcast('clip_update')

    def handle_clip_comments_get(self, clip_filename):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, author, comment, created_at FROM clip_comments WHERE clip_filename = %s ORDER BY created_at ASC",
                (clip_filename,)
            )
            rows = cur.fetchall()
            conn.close()
            comments = [{
                'id': r[0], 'author': r[1], 'comment': r[2],
                'createdAt': r[3].strftime('%Y-%m-%d %H:%M:%S') if r[3] else ''
            } for r in rows]
            self.send_json(comments)
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_clip_comments_post(self, clip_filename, data):
        author = data.get('author', '').strip()
        comment = data.get('comment', '').strip()
        if not comment:
            return self.send_json({'error': 'Comment is required'}, status=400)
        if not author:
            author = 'Anonymous'
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO clip_comments (clip_filename, author, comment) VALUES (%s, %s, %s)",
                (clip_filename, author, comment)
            )
            conn.commit()
            comment_id = cur.lastrowid
            conn.close()
            self.send_json({'success': True, 'id': comment_id})
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_clip_comment_delete(self, comment_id):
        if not self.require_admin():
            return
        try:
            cid = int(comment_id)
            conn = get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM clip_comments WHERE id = %s", (cid,))
            conn.commit()
            conn.close()
            self.send_json({'success': True})
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_clip_rename(self, filename, data):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400, "Invalid filename")
        clip_json = os.path.join(CLIPS_DIR, os.path.splitext(filename)[0] + '.json')
        if not os.path.exists(clip_json):
            return self.send_error(404, "Clip not found")
        try:
            with open(clip_json, 'r') as f:
                meta = json.load(f)
            meta['clip_name'] = data.get('clipName', '').strip()
            with open(clip_json, 'w') as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            self.send_json({'success': True})
            ws_broadcast('clip_update')
        except Exception as e:
            self.send_error(500, str(e))

    def handle_clip_delete(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400, "Invalid filename")
        clip_path = os.path.join(CLIPS_DIR, filename)
        if not os.path.exists(clip_path):
            return self.send_error(404, "Clip not found")
        try:
            os.remove(clip_path)
            json_path = os.path.splitext(clip_path)[0] + '.json'
            if os.path.exists(json_path):
                os.remove(json_path)
            self.send_json({'success': True})
            ws_broadcast('clip_update')
        except Exception as e:
            self.send_error(500, str(e))

    def handle_clip_thumbnail(self, filename):
        clip_thumb_dir = os.path.join(CLIPS_DIR, 'images')
        thumb_path = os.path.join(clip_thumb_dir, filename + '.jpg')

        if not os.path.exists(thumb_path):
            # Generate on demand
            clip_path = os.path.join(CLIPS_DIR, filename)
            if not os.path.exists(clip_path):
                return self.send_error(404)
            os.makedirs(clip_thumb_dir, exist_ok=True)
            try:
                subprocess.run([
                    'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', clip_path, '-ss', '00:00:01', '-vframes', '1',
                    '-vf', 'scale=480:-1', '-q:v', '10', thumb_path
                ], timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if not os.path.exists(thumb_path):
                    subprocess.run([
                        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', clip_path, '-ss', '00:00:00', '-vframes', '1',
                        '-vf', 'scale=480:-1', '-q:v', '10', thumb_path
                    ], timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                return self.send_error(500)

        if os.path.exists(thumb_path):
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(os.path.getsize(thumb_path)))
            self.end_headers()
            with open(thumb_path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        else:
            self.send_error(404)

    def handle_clip_video(self, filename):
        file_path = os.path.join(CLIPS_DIR, filename)
        if not os.path.exists(file_path):
            return self.send_error(404)
        # Reuse same video serving logic
        self._serve_file(file_path, 'video/mp4')

    def handle_clip_share(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        clip_path = os.path.join(CLIPS_DIR, filename)
        if not os.path.exists(clip_path):
            return self.send_error(404)
        token = secrets.token_hex(16)
        with share_tokens_lock:
            # Cleanup expired tokens
            now = time.time()
            expired = [t for t, v in share_tokens.items() if now - v['created_at'] > SHARE_TOKEN_TTL]
            for t in expired:
                del share_tokens[t]
            share_tokens[token] = {'filename': filename, 'created_at': now}
            _save_share_tokens(share_tokens)
        url = f"/share/{token}"
        self.send_json({'success': True, 'url': url, 'token': token})

    def _resolve_share_token(self, token):
        with share_tokens_lock:
            info = share_tokens.get(token)
            if not info:
                return None
            if time.time() - info['created_at'] > SHARE_TOKEN_TTL:
                del share_tokens[token]
                _save_share_tokens(share_tokens)
                return None
            return info['filename']

    def handle_share_page(self, token):
        filename = self._resolve_share_token(token)
        if not filename:
            self.send_response(404)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'<html><body style="background:#0a0e1a;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif"><h2>Link expired or invalid</h2></body></html>')
            return
        # Read clip metadata for title
        clip_json = os.path.join(CLIPS_DIR, os.path.splitext(filename)[0] + '.json')
        title = filename
        if os.path.exists(clip_json):
            try:
                with open(clip_json, 'r') as f:
                    meta = json.load(f)
                title = meta.get('clip_name') or meta.get('title') or filename
            except:
                pass
        html = f'''<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - MiraRec</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0e1a;color:#e8eaf0;font-family:sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;padding:16px}}
h1{{font-size:18px;margin-bottom:12px;text-align:center}}video{{max-width:100%;max-height:80vh;border-radius:12px;background:#000}}
.dl{{margin-top:12px;padding:8px 20px;background:#4f6ef7;color:#fff;border-radius:8px;text-decoration:none;font-weight:600}}</style>
</head><body><h1>{title}</h1><video controls playsinline autoplay src="/share/{token}/video"></video>
<a class="dl" href="/share/{token}/video" download="{filename}">Download</a></body></html>'''
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def handle_share_video(self, token):
        filename = self._resolve_share_token(token)
        if not filename:
            return self.send_error(404)
        file_path = os.path.join(CLIPS_DIR, filename)
        if not os.path.exists(file_path):
            return self.send_error(404)
        self._serve_file(file_path, 'video/mp4')

    def _serve_file(self, file_path, mime_type):
        file_size = os.path.getsize(file_path)
        range_header = self.headers.get('Range')
        if range_header:
            byte_range = range_header.replace('bytes=', '').split('-')
            start = int(byte_range[0])
            end = int(byte_range[1]) if byte_range[1] else file_size - 1
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(length))
            self.send_header('Content-Type', mime_type)
            self.end_headers()
            with open(file_path, 'rb') as f:
                f.seek(start)
                remaining = length
                chunk = 1 << 20  # 1MB
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        else:
            self.send_response(200)
            self.send_header('Content-Length', str(file_size))
            self.send_header('Content-Type', mime_type)
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(file_path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)

    def handle_video(self, filename):
        file_path = os.path.join(NAS_DIR, filename)
        if not os.path.exists(file_path):
            return self.send_error(404)
        mime_type = 'video/mp2t' if filename.endswith('.ts') else 'video/mp4'
        self._serve_file(file_path, mime_type)

    def handle_live_stream_url(self, filename):
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return self.send_error(400)
        json_path = os.path.join(NAS_DIR, os.path.splitext(filename)[0] + '.json')
        if not os.path.exists(json_path):
            return self.send_json({'is_live': False})
        try:
            with open(json_path) as f:
                meta = json.load(f)
        except Exception:
            return self.send_json({'is_live': False})
        live_id = meta.get('live_id', '')
        if not live_id:
            return self.send_json({'is_live': False})
        api_url = f'https://www.mirrativ.com/api/live/get_streaming_url?live_id={live_id}'
        try:
            import urllib.request as _ureq
            req = _ureq.Request(api_url, headers={'User-Agent': _MIRRATIV_UA})
            with _ureq.urlopen(req, timeout=10) as r:
                stream_data = json.loads(r.read())
        except Exception as e:
            return self.send_json({'is_live': False, 'error': str(e)})
        if not stream_data.get('is_live'):
            return self.send_json({'is_live': False})
        hls_url = stream_data.get('streaming_url_hls', '')
        if not hls_url:
            return self.send_json({'is_live': False})
        proxy_url = '/api/hls-proxy?url=' + urllib.parse.quote(hls_url, safe='')
        self.send_json({'is_live': True, 'proxy_url': proxy_url})

    def handle_hls_proxy(self, query_string):
        params = urllib.parse.parse_qs(query_string)
        url = params.get('url', [''])[0]
        if not url:
            return self.send_error(400)
        if not _is_allowed_proxy_url(url):
            return self.send_error(403)
        try:
            import urllib.request as _ureq
            req = _ureq.Request(url, headers={'User-Agent': _MIRRATIV_UA})
            with _ureq.urlopen(req, timeout=20) as r:
                content_type = r.headers.get('Content-Type', 'application/octet-stream')
                content = r.read()
        except Exception:
            return self.send_error(502)
        url_path = url.split('?')[0]
        if 'mpegurl' in content_type.lower() or url_path.endswith('.m3u8'):
            base_url = url_path.rsplit('/', 1)[0] + '/'
            lines = content.decode('utf-8', errors='replace').split('\n')
            out = []
            for line in lines:
                stripped = line.rstrip('\r')
                if stripped and not stripped.startswith('#'):
                    abs_url = stripped if stripped.startswith('http') else base_url + stripped
                    out.append('/api/hls-proxy?url=' + urllib.parse.quote(abs_url, safe=''))
                else:
                    out.append(stripped)
            content = '\n'.join(out).encode('utf-8')
            content_type = 'application/vnd.apple.mpegurl'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(content)

# --- WebSocket ---
WS_PORT = 3002
ws_clients = set()
ws_loop = None

def ws_broadcast(event_type, data=None):
    if not HAS_WEBSOCKETS or not ws_loop or not ws_clients:
        return
    msg = json.dumps({'type': event_type, 'data': data or {}})
    async def _send():
        dead = set()
        for ws in ws_clients.copy():
            try:
                await ws.send(msg)
            except:
                dead.add(ws)
        ws_clients.difference_update(dead)
    asyncio.run_coroutine_threadsafe(_send(), ws_loop)

async def ws_handler(websocket):
    ws_clients.add(websocket)
    try:
        async for _ in websocket:
            pass  # We only send, not receive
    except:
        pass
    finally:
        ws_clients.discard(websocket)

def ws_live_monitor():
    """Periodically check live status and broadcast changes."""
    prev_live = {}
    while True:
        try:
            targets = load_targets()
            changed = False
            for t in targets:
                uid = t['userId']
                is_live = check_live_status(uid)
                if prev_live.get(uid) != is_live:
                    changed = True
                prev_live[uid] = is_live
            if changed:
                ws_broadcast('live_status')
        except:
            pass
        time.sleep(15)

def start_ws_server():
    global ws_loop
    ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_loop)
    async def _run():
        async with websockets.serve(ws_handler, '0.0.0.0', WS_PORT):
            await asyncio.Future()  # run forever
    print(f"WebSocket server on port {WS_PORT}")
    ws_loop.run_until_complete(_run())

# Initialize recordings on start
def init_recordings():
    print("Initializing recordings...")
    subprocess.run(f'bash "{MANAGER_SCRIPT}" start', shell=True)

if __name__ == '__main__':
    # init_recordings() # Run in background or before server
    threading.Thread(target=init_recordings, daemon=True).start()
    threading.Thread(target=update_dir_size_loop, daemon=True).start()
    threading.Thread(target=generate_missing_thumbnails, daemon=True).start()
    threading.Thread(target=comment_collector_loop, daemon=True).start()
    threading.Thread(target=prefetch_durations, daemon=True).start()
    threading.Thread(target=api_log_file_watcher, daemon=True).start()
    if HAS_WEBSOCKETS:
        threading.Thread(target=start_ws_server, daemon=True).start()
        threading.Thread(target=ws_live_monitor, daemon=True).start()
    else:
        print("Warning: 'websockets' not installed, WebSocket disabled. Install with: pip3 install websockets")
    
    class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    server = ReusableThreadingTCPServer(('0.0.0.0', PORT), Handler)
    print(f"Serving on port {PORT}")
    
    def signal_handler(signum, frame):
        print(f"Received signal {signum}, shutting down...")
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    server.serve_forever()
    server.server_close()
