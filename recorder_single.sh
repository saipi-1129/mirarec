#!/bin/bash

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CHECK_INTERVAL=30
OUTPUT_DIR="${NAS_DIR:-$SCRIPT_DIR/data}"
LOG_FILE="$SCRIPT_DIR/recorder.log"
DISCORD_WEBHOOK="${DISCORD_WEBHOOK_RECORDER:-}"

# --- Functions ---

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$1] $2" | tee -a "$LOG_FILE"
}

log_api() {
    local url="$1" status="$2" elapsed="$3"
    local endpoint="${url#https://www.mirrativ.com/api/}"
    endpoint="${endpoint%%\?*}"
    printf '{"ts":%s,"source":"recorder","endpoint":"%s","status":%s,"ms":%s,"user":"%s"}\n' \
        "$(date +%s%3N)" "$endpoint" "$status" "$elapsed" "${USER_ID:-}" \
        >> "$OUTPUT_DIR/.api_log" 2>/dev/null || true
}

send_discord() {
    local message="$1"
    local payload
    payload=$(python3 -c "import json,sys; print(json.dumps({'content':sys.argv[1]}))" "$message" 2>/dev/null)
    wget -qO- --header="Content-Type: application/json" \
         --header="User-Agent: MiraRec/1.0" \
         --post-data="$payload" \
         "$DISCORD_WEBHOOK" > /dev/null 2>&1 || true
}

queue_local_api_post() {
    local endpoint="$1"
    local label="$2"
    local max_tries=5
    local delay=2
    local i
    for i in $(seq 1 "$max_tries"); do
        if wget -qO- --timeout=8 --tries=1 --method=POST "$endpoint" > /dev/null 2>&1; then
            log "$USER_ID" "$label queued: ${BASE_FILENAME}.mp4 (try $i/$max_tries)"
            return 0
        fi
        sleep "$delay"
        delay=$((delay * 2))
    done
    log "$USER_ID" "WARNING: failed to queue $label after ${max_tries} tries: ${BASE_FILENAME}.mp4"
    return 1
}

cleanup() {
    if [ -n "$TEMP_TS" ] && [ -f "$TEMP_TS" ]; then
        log "$USER_ID" "Cleaning up temp file: $TEMP_TS"
        rm -f "$TEMP_TS"
    fi
}

trap cleanup EXIT

# Handle shutdown signals
handle_term() {
    log "$USER_ID" "Received termination signal. Exiting..."
    if [ -n "$FFMPEG_PID" ] && kill -0 "$FFMPEG_PID" 2>/dev/null; then
        kill -INT "$FFMPEG_PID" 2>/dev/null
        wait "$FFMPEG_PID" 2>/dev/null
    fi
    exit 0
}
trap handle_term INT TERM

# Check dependencies
if ! command -v ffmpeg &> /dev/null; then
    log "SYSTEM" "Error: 'ffmpeg' command not found."
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    log "SYSTEM" "Error: 'python3' command not found."
    exit 1
fi

# Create directories
mkdir -p "$OUTPUT_DIR"

if [ -z "$1" ]; then
    echo "Usage: $0 <user_id> [check_interval]"
    exit 1
fi

USER_ID=$1
if [ -n "$2" ] && [ "$2" -gt 0 ] 2>/dev/null; then
    CHECK_INTERVAL=$2
fi
RETENTION_DAYS=7
if [ -n "$3" ] && [ "$3" -gt 0 ] 2>/dev/null; then
    RETENTION_DAYS=$3
fi
log "$USER_ID" "Starting Mirrativ Recorder for User ID: $USER_ID"
log "$USER_ID" "Saving to: $OUTPUT_DIR"

# Select best quality HLS variant from master playlist
select_best_stream() {
    local master_url="$1"
    local best_url
    best_url=$(wget -qO- --header="User-Agent: $USER_AGENT" --timeout=10 "$master_url" | python3 -c "
import sys
lines = sys.stdin.read().strip().split('\n')
best_bw = -1
best_url = ''
next_is_url = False
for line in lines:
    if line.startswith('#EXT-X-STREAM-INF:'):
        for part in line.split(','):
            if 'BANDWIDTH=' in part:
                try:
                    bw = int(part.split('BANDWIDTH=')[1].split(',')[0])
                    if bw > best_bw:
                        best_bw = bw
                        next_is_url = True
                except:
                    pass
    elif next_is_url and not line.startswith('#'):
        best_url = line.strip()
        next_is_url = False
if best_url:
    print(best_url)
else:
    print('')
" 2>/dev/null)
    echo "$best_url"
}

# Check if stream is live via API
check_stream_live() {
    local api_url="$1" check_json _t0 _rc
    _t0=$(date +%s%3N)
    check_json=$(wget -qO- --header="User-Agent: $USER_AGENT" --timeout=10 --tries=2 "$api_url" 2>/dev/null)
    _rc=$?
    log_api "$api_url" $((_rc==0?200:0)) $(($(date +%s%3N)-_t0))
    echo "$check_json" | python3 -c "import sys, json; print(json.load(sys.stdin).get('is_live', 0))" 2>/dev/null
}

# Remux TS to MP4
do_remux() {
    local ts_file="$1"
    local mp4_file="$2"
    local json_file="$3"
    local base_name="$4"

    if [ ! -f "$ts_file" ] || [ ! -s "$ts_file" ]; then
        log "$USER_ID" "Error: Recording file not found or empty."
        return 1
    fi

    log "$USER_ID" "Remuxing to MP4..."
    ffmpeg -y -hide_banner -loglevel error \
        -err_detect ignore_err \
        -fflags +genpts+discardcorrupt+igndts \
        -i "$ts_file" \
        -c:v copy \
        -c:a aac -b:a 128k -af aresample=async=1000:first_pts=0 \
        -movflags +faststart \
        "$mp4_file"

    if [ -f "$mp4_file" ] && [ -s "$mp4_file" ]; then
        # Move moov atom to front for fast playback start
        if command -v qt-faststart &> /dev/null; then
            local tmp_fast="${mp4_file}.fast"
            qt-faststart "$mp4_file" "$tmp_fast" 2>/dev/null
            if [ -f "$tmp_fast" ] && [ -s "$tmp_fast" ]; then
                mv "$tmp_fast" "$mp4_file"
                log "$USER_ID" "faststart applied."
            else
                rm -f "$tmp_fast" 2>/dev/null
            fi
        fi
        # Verify the output MP4 is playable
        if ! ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$mp4_file" > /dev/null 2>&1; then
            log "$USER_ID" "Warning: MP4 validation failed (corrupt or unreadable)."
            send_discord "⚠️ **変換後MP4が破損している可能性があります**
🎬 \`$(basename "$mp4_file")\`
👤 ${USER_NAME} / ${STREAM_TITLE}
📁 ファイルは保存されましたが再生できない可能性があります"
        fi

        log "$USER_ID" "Remux successful. Removing TS."
        rm -f "$ts_file"
        TEMP_TS=""
        sed -i "s/\"filename\": \"${base_name}.ts\"/\"filename\": \"${base_name}.mp4\"/" "$json_file"

        # Generate Thumbnail
        THUMBNAILS_DIR="$OUTPUT_DIR/images"
        mkdir -p "$THUMBNAILS_DIR"
        THUMB_PATH="$THUMBNAILS_DIR/${base_name}.mp4.jpg"
        log "$USER_ID" "Generating thumbnail..."
        ffmpeg -y -hide_banner -loglevel error -i "$mp4_file" -ss 00:00:05 -vframes 1 -vf "scale=480:-1" -q:v 10 "$THUMB_PATH" || \
        ffmpeg -y -hide_banner -loglevel error -i "$mp4_file" -ss 00:00:00 -vframes 1 -vf "scale=480:-1" -q:v 10 "$THUMB_PATH"

        log "$USER_ID" "Saved: $mp4_file"

        # Discord notification
        local size_mb duration_str
        size_mb=$(python3 -c "import os; print(f'{os.path.getsize(\"$mp4_file\")/1024/1024:.1f}')" 2>/dev/null || echo "?")
        duration_str=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$mp4_file" 2>/dev/null | python3 -c "
import sys
try:
    s=float(sys.stdin.read()); h=int(s//3600); m=int(s%3600//60); ss=int(s%60)
    print(f'{h}:{m:02d}:{ss:02d}' if h else f'{m}:{ss:02d}')
except: print('?')
" 2>/dev/null)
        send_discord "✅ **録画完了**
🎬 \`$(basename "$mp4_file")\`
👤 ${USER_NAME} / ${STREAM_TITLE}
⏱️ ${duration_str} / ${size_mb}MB"

        return 0
    else
        log "$USER_ID" "Remux failed. Keeping TS."
        log "$USER_ID" "Saved: $ts_file"
        send_discord "⚠️ **リマックス失敗**
👤 ${USER_NAME} / ${STREAM_TITLE}
📁 $(basename "$ts_file") のMP4変換に失敗しました（TSファイルは保持）"
        TEMP_TS=""
        return 1
    fi
}

while true; do
    # Cleanup old recordings
    find "$OUTPUT_DIR" -name "*.mp4" -type f -mtime +$RETENTION_DAYS -delete 2>/dev/null
    find "$OUTPUT_DIR" -name "*.json" -type f -mtime +$RETENTION_DAYS -delete 2>/dev/null

    # 1. Get Profile
    PROFILE_URL="https://www.mirrativ.com/api/user/profile?user_id=${USER_ID}"
    _t0=$(date +%s%3N)
    PROFILE_JSON=$(wget -qO- --header="User-Agent: $USER_AGENT" --timeout=10 --tries=3 "$PROFILE_URL")
    _rc=$?; log_api "$PROFILE_URL" $((_rc==0?200:0)) $(($(date +%s%3N)-_t0))

    if [ -z "$PROFILE_JSON" ]; then
        log "$USER_ID" "Warning: Failed to fetch profile. Retrying in ${CHECK_INTERVAL}s..."
        sleep "$CHECK_INTERVAL"
        continue
    fi

    # Parse Live ID
    LIVE_ID=$(echo "$PROFILE_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
live_id = data.get('live_id', '')
if not live_id:
    onlive = data.get('onlive')
    if onlive:
        live_id = onlive.get('live_id', '')
print(live_id)
" 2>/dev/null)

    if [ -n "$LIVE_ID" ]; then
        # 2. Get Stream Info
        STREAM_URL_API="https://www.mirrativ.com/api/live/get_streaming_url?live_id=${LIVE_ID}"
        _t0=$(date +%s%3N)
        STREAM_JSON=$(wget -qO- --header="User-Agent: $USER_AGENT" --timeout=10 --tries=3 "$STREAM_URL_API")
        _rc=$?; log_api "$STREAM_URL_API" $((_rc==0?200:0)) $(($(date +%s%3N)-_t0))
        
        IS_LIVE=$(echo "$STREAM_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('is_live', 0))" 2>/dev/null)
        HLS_URL=$(echo "$STREAM_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('streaming_url_hls', ''))" 2>/dev/null)

        if [ -n "$HLS_URL" ]; then
            
            # Try to select highest quality variant
            BEST_VARIANT=$(select_best_stream "$HLS_URL")
            if [ -n "$BEST_VARIANT" ]; then
                if [[ "$BEST_VARIANT" != http* ]]; then
                    BASE_URL=$(echo "$HLS_URL" | sed 's|/[^/]*$|/|')
                    BEST_VARIANT="${BASE_URL}${BEST_VARIANT}"
                fi
                log "$USER_ID" "Selected best quality variant: $BEST_VARIANT"
                RECORD_URL="$BEST_VARIANT"
            else
                log "$USER_ID" "No variant playlist found, using original URL"
                RECORD_URL="$HLS_URL"
            fi

            # Extract Metadata
            USER_NAME=$(echo "$PROFILE_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('name', 'User_${USER_ID}'))")
            STREAM_TITLE=$(echo "$STREAM_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('title', ''))")

            # Fallback for Title
            if [ -z "$STREAM_TITLE" ]; then
                HISTORY_URL="https://www.mirrativ.com/api/live/live_history?user_id=${USER_ID}&page=1"
                HISTORY_JSON=$(wget -qO- --header="User-Agent: $USER_AGENT" --timeout=10 --tries=2 "$HISTORY_URL")
                if [ -n "$HISTORY_JSON" ]; then
                    STREAM_TITLE=$(echo "$HISTORY_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
lives = data.get('lives', [])
if lives:
    target_live_id = '$LIVE_ID'
    for live in lives:
        if live.get('live_id') == target_live_id:
            print(live.get('title', ''))
            sys.exit(0)
    if lives[0].get('is_live'):
        print(lives[0].get('title', ''))
" 2>/dev/null)
                fi
            fi
            
            # Sanitize for filename (keep only safe ASCII + common CJK)
            SAFE_NAME=$(echo "$USER_NAME" | python3 -c "
import sys, re, unicodedata
s = sys.stdin.read().strip()
# Normalize unicode
s = unicodedata.normalize('NFKC', s)
# Remove non-printable and special chars, keep alnum + CJK + hyphen/underscore
s = re.sub(r'[^\w\u3000-\u9fff\u30a0-\u30ff\u3040-\u309f-]', '_', s)
s = re.sub(r'_+', '_', s).strip('_')
print(s if s else 'Unknown')
")
            SAFE_TITLE=$(echo "$STREAM_TITLE" | python3 -c "
import sys, re, unicodedata
s = sys.stdin.read().strip()
s = unicodedata.normalize('NFKC', s)
s = re.sub(r'[^\w\u3000-\u9fff\u30a0-\u30ff\u3040-\u309f-]', '_', s)
s = re.sub(r'_+', '_', s).strip('_')
print(s)
")
            TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
            
            BASE_FILENAME="${SAFE_NAME}_${TIMESTAMP}"
            if [ -n "$SAFE_TITLE" ]; then
                 BASE_FILENAME="${SAFE_NAME}_${SAFE_TITLE}_${TIMESTAMP}"
            fi
            
            # Paths
            TEMP_TS="${OUTPUT_DIR}/${BASE_FILENAME}.ts"
            FINAL_MP4="${OUTPUT_DIR}/${BASE_FILENAME}.mp4"
            FINAL_JSON="${OUTPUT_DIR}/${BASE_FILENAME}.json"

            log "$USER_ID" "---------------------------------------------------"
            log "$USER_ID" "Stream FOUND: $USER_NAME - $STREAM_TITLE"
            log "$USER_ID" "Live ID: $LIVE_ID"
            log "$USER_ID" "Recording to: $TEMP_TS"
            log "$USER_ID" "---------------------------------------------------"

            # Create Metadata JSON
            cat <<EOF > "$FINAL_JSON"
{
  "live_id": "$LIVE_ID",
  "user_id": "$USER_ID",
  "user_name": "$(echo $USER_NAME | sed 's/"/\\"/g')",
  "title": "$(echo $STREAM_TITLE | sed 's/"/\\"/g')",
  "start_time": $(date +%s%3N),
  "filename": "${BASE_FILENAME}.ts"
}
EOF

            # Discord: recording started
            send_discord "🔴 **録画開始**
👤 ${USER_NAME} / ${STREAM_TITLE}
🆔 Live ID: ${LIVE_ID}"

            # === Recording loop: retry if ffmpeg exits while stream is still live ===
            STREAM_ENDED=false
            RETRY_COUNT=0
            MAX_RETRIES=10
            SEGMENT_INDEX=0
            SEGMENT_FILES=("$TEMP_TS")

            while [ "$STREAM_ENDED" = false ]; do

                # Determine output file for this segment
                if [ "$SEGMENT_INDEX" -eq 0 ]; then
                    CURRENT_SEGMENT="$TEMP_TS"
                else
                    CURRENT_SEGMENT="${OUTPUT_DIR}/${BASE_FILENAME}_seg${SEGMENT_INDEX}.ts"
                    SEGMENT_FILES+=("$CURRENT_SEGMENT")
                fi

                # Start ffmpeg
                ffmpeg -y -hide_banner -loglevel warning \
                    -user_agent "$USER_AGENT" \
                    -reconnect 1 \
                    -reconnect_at_eof 1 \
                    -reconnect_streamed 1 \
                    -reconnect_on_network_error 1 \
                    -reconnect_on_http_error "4xx,5xx" \
                    -reconnect_delay_max 30 \
                    -rw_timeout 30000000 \
                    -i "$RECORD_URL" \
                    -c copy \
                    -f mpegts "$CURRENT_SEGMENT" &
                FFMPEG_PID=$!
                log "$USER_ID" "ffmpeg started (PID: $FFMPEG_PID, segment: $SEGMENT_INDEX, retry: $RETRY_COUNT)"

                # Monitor loop
                OFFLINE_COUNT=0
                STALL_COUNT=0
                LAST_SIZE=0
                while kill -0 "$FFMPEG_PID" 2>/dev/null; do
                    sleep 30

                    # --- Stall detection: check if file is growing ---
                    if [ -f "$CURRENT_SEGMENT" ]; then
                        CURRENT_SIZE=$(stat -c%s "$CURRENT_SEGMENT" 2>/dev/null || stat -f%z "$CURRENT_SEGMENT" 2>/dev/null || echo 0)
                    else
                        CURRENT_SIZE=0
                    fi

                    if [ "$CURRENT_SIZE" -eq "$LAST_SIZE" ] && [ "$CURRENT_SIZE" -gt 0 ]; then
                        STALL_COUNT=$((STALL_COUNT + 1))
                        log "$USER_ID" "File size stalled at ${CURRENT_SIZE} bytes (stall count: $STALL_COUNT/4)"
                        if [ "$STALL_COUNT" -ge 4 ]; then
                            log "$USER_ID" "ffmpeg stalled for 2+ minutes, restarting..."
                            send_discord "⚠️ **録画ストール検知 → 再起動**
👤 ${USER_NAME} / ${STREAM_TITLE}
📁 $(basename "$CURRENT_SEGMENT") が2分間更新なし"
                            kill -INT "$FFMPEG_PID" 2>/dev/null
                            sleep 5
                            if kill -0 "$FFMPEG_PID" 2>/dev/null; then
                                kill -KILL "$FFMPEG_PID" 2>/dev/null
                            fi
                            wait "$FFMPEG_PID" 2>/dev/null
                            break
                        fi
                    else
                        STALL_COUNT=0
                        LAST_SIZE=$CURRENT_SIZE
                    fi

                    # --- API offline detection ---
                    CHECK_LIVE=$(check_stream_live "$STREAM_URL_API")
                    if [ "$CHECK_LIVE" != "1" ]; then
                        OFFLINE_COUNT=$((OFFLINE_COUNT + 1))
                        log "$USER_ID" "API reports offline (count: $OFFLINE_COUNT/6). Waiting for ffmpeg to finish naturally..."
                        if [ "$OFFLINE_COUNT" -ge 6 ]; then
                            log "$USER_ID" "Stream offline for 3+ minutes, stopping ffmpeg."
                            kill -INT "$FFMPEG_PID" 2>/dev/null
                            sleep 5
                            if kill -0 "$FFMPEG_PID" 2>/dev/null; then
                                kill -KILL "$FFMPEG_PID" 2>/dev/null
                            fi
                            wait "$FFMPEG_PID" 2>/dev/null
                            STREAM_ENDED=true
                            break
                        fi
                    else
                        OFFLINE_COUNT=0
                    fi
                done
                wait "$FFMPEG_PID" 2>/dev/null
                FFMPEG_PID=""

                # If we already determined stream ended, break
                if [ "$STREAM_ENDED" = true ]; then
                    break
                fi

                # ffmpeg exited (crash, stall-kill, or natural end). Check if stream is still live.
                sleep 5
                RECHECK_LIVE=$(check_stream_live "$STREAM_URL_API")
                if [ "$RECHECK_LIVE" = "1" ]; then
                    RETRY_COUNT=$((RETRY_COUNT + 1))
                    if [ "$RETRY_COUNT" -ge "$MAX_RETRIES" ]; then
                        log "$USER_ID" "Max retries ($MAX_RETRIES) reached. Giving up."
                        send_discord "❌ **録画断念**
👤 ${USER_NAME} / ${STREAM_TITLE}
⚠️ ffmpegが ${MAX_RETRIES} 回連続で失敗しました。配信は継続中の可能性があります。"
                        STREAM_ENDED=true
                        break
                    fi

                    SEGMENT_INDEX=$((SEGMENT_INDEX + 1))
                    log "$USER_ID" "ffmpeg exited but stream still live. Resuming recording (retry $RETRY_COUNT/$MAX_RETRIES, segment $SEGMENT_INDEX)..."

                    # Re-fetch HLS URL in case it changed
                    NEW_STREAM_JSON=$(wget -qO- --header="User-Agent: $USER_AGENT" --timeout=10 --tries=3 "$STREAM_URL_API")
                    NEW_HLS=$(echo "$NEW_STREAM_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('streaming_url_hls', ''))" 2>/dev/null)
                    if [ -n "$NEW_HLS" ]; then
                        NEW_VARIANT=$(select_best_stream "$NEW_HLS")
                        if [ -n "$NEW_VARIANT" ]; then
                            if [[ "$NEW_VARIANT" != http* ]]; then
                                NEW_BASE=$(echo "$NEW_HLS" | sed 's|/[^/]*$|/|')
                                NEW_VARIANT="${NEW_BASE}${NEW_VARIANT}"
                            fi
                            RECORD_URL="$NEW_VARIANT"
                        else
                            RECORD_URL="$NEW_HLS"
                        fi
                    fi

                    # Backoff: wait longer on repeated failures
                    BACKOFF=$((3 + RETRY_COUNT * 2))
                    log "$USER_ID" "Waiting ${BACKOFF}s before retry..."
                    sleep "$BACKOFF"
                else
                    log "$USER_ID" "Stream ended. ffmpeg exited normally."
                    STREAM_ENDED=true
                fi
            done

            # === Concatenate segments if multiple ===
            if [ "$SEGMENT_INDEX" -gt 0 ]; then
                log "$USER_ID" "Concatenating $((SEGMENT_INDEX + 1)) segments..."
                CONCAT_LIST="${OUTPUT_DIR}/${BASE_FILENAME}_concat.txt"
                > "$CONCAT_LIST"
                for seg in "${SEGMENT_FILES[@]}"; do
                    if [ -f "$seg" ] && [ -s "$seg" ]; then
                        echo "file '$seg'" >> "$CONCAT_LIST"
                    fi
                done

                MERGED_TS="${OUTPUT_DIR}/${BASE_FILENAME}_merged.ts"
                ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i "$CONCAT_LIST" -c copy -f mpegts "$MERGED_TS"

                if [ -f "$MERGED_TS" ] && [ -s "$MERGED_TS" ]; then
                    # Clean up individual segments, replace TEMP_TS with merged file
                    for seg in "${SEGMENT_FILES[@]}"; do
                        rm -f "$seg"
                    done
                    mv "$MERGED_TS" "$TEMP_TS"
                    log "$USER_ID" "Segments merged successfully."
                else
                    log "$USER_ID" "Segment merge failed. Using first segment only."
                fi
                rm -f "$CONCAT_LIST"
            fi

            log "$USER_ID" "Recording stopped."

            # Remux
            do_remux "$TEMP_TS" "$FINAL_MP4" "$FINAL_JSON" "$BASE_FILENAME"

            # Queue transcription and detect via server API
            if [ -f "$FINAL_MP4" ]; then
                ENCODED_MP4=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${BASE_FILENAME}.mp4'))" 2>/dev/null)
                queue_local_api_post "http://localhost:3001/api/transcriptions/${ENCODED_MP4}" "Transcription"
                queue_local_api_post "http://localhost:3001/api/detect/${ENCODED_MP4}" "Detect (interval=2)"
            fi

            log "$USER_ID" "Waiting for next stream..."
            sleep 10
        else
            :
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
