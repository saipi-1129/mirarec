#!/bin/bash

# Multi-user recording manager for Mirrativ
# This script manages recording processes for multiple users

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORDER_SCRIPT="${RECORDER_SCRIPT:-$SCRIPT_DIR/recorder_single.sh}"
TARGETS_FILE="${TARGETS_FILE:-$SCRIPT_DIR/config/targets.json}"
PID_DIR="${PID_DIR:-$SCRIPT_DIR/pids}"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/manager.log}"

mkdir -p "$PID_DIR"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [MANAGER] $1" | tee -a "$LOG_FILE"
}

# Load targets from JSON file
load_targets() {
    if [ -f "$TARGETS_FILE" ]; then
        python3 -c "
import json, sys
try:
    with open('$TARGETS_FILE', 'r') as f:
        data = json.load(f)
        targets = data.get('targets', [])
        for target in targets:
            if target.get('enabled', True):
                ci = target.get('checkInterval', 30)
                rd = target.get('retentionDays', 7)
                print(f\"{target.get('userId', '')} {ci} {rd}\")
except Exception as e:
    sys.stderr.write(f'Error loading targets: {e}\n')
"
    fi
}

# Get check interval for a specific user
get_check_interval() {
    local user_id="$1"
    if [ -f "$TARGETS_FILE" ]; then
        python3 -c "
import json, sys
try:
    with open('$TARGETS_FILE', 'r') as f:
        data = json.load(f)
        for t in data.get('targets', []):
            if t.get('userId') == '$user_id':
                print(t.get('checkInterval', 30))
                sys.exit(0)
    print(30)
except:
    print(30)
"
    else
        echo 30
    fi
}

get_retention_days() {
    local user_id="$1"
    if [ -f "$TARGETS_FILE" ]; then
        python3 -c "
import json, sys
try:
    with open('$TARGETS_FILE', 'r') as f:
        data = json.load(f)
        for t in data.get('targets', []):
            if t.get('userId') == '$user_id':
                print(t.get('retentionDays', 7))
                sys.exit(0)
    print(7)
except:
    print(7)
"
    else
        echo 7
    fi
}

# Start recording for a user
start_user_recording() {
    local user_id="$1"
    local pid_file="$PID_DIR/$user_id.pid"
    
    if [ -f "$pid_file" ]; then
        local existing_pid=$(cat "$pid_file")
        if kill -0 "$existing_pid" 2>/dev/null; then
            log "Recording already running for user $user_id (PID: $existing_pid)"
            return 0
        else
            rm -f "$pid_file"
        fi
    fi
    
    local check_interval retention_days
    check_interval=$(get_check_interval "$user_id")
    retention_days=$(get_retention_days "$user_id")
    log "Starting recording for user: $user_id (interval=${check_interval}s retention=${retention_days}d)"

    # Start recording process
    nohup bash "$RECORDER_SCRIPT" "$user_id" "$check_interval" "$retention_days" > "/dev/null" 2>&1 &
    local pid=$!
    
    echo "$pid" > "$pid_file"
    log "Started recording for user $user_id (PID: $pid)"
}

# Stop recording for a user
stop_user_recording() {
    local user_id="$1"
    local pid_file="$PID_DIR/$user_id.pid"
    
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            log "Stopping recording for user $user_id (PID: $pid)"
            kill -TERM "$pid"
            sleep 2
            if kill -0 "$pid" 2>/dev/null; then
                log "Force killing process for user $user_id"
                kill -KILL "$pid"
            fi
            rm -f "$pid_file"
            log "Stopped recording for user $user_id"
        else
            log "Process for user $user_id not found, removing stale PID file"
            rm -f "$pid_file"
        fi
    else
        log "No recording found for user $user_id"
    fi
}

# Start all recordings
start_all() {
    log "Starting all user recordings..."
    
    local targets=$(load_targets)
    
    if [ -z "$targets" ]; then
        log "No targets found"
        return
    fi
    
    echo "$targets" | while read -r user_id _ci; do
        if [ -n "$user_id" ]; then
            start_user_recording "$user_id"
        fi
    done
}

# Stop all recordings
stop_all() {
    log "Stopping all user recordings..."
    
    for pid_file in "$PID_DIR"/*.pid; do
        if [ -f "$pid_file" ]; then
            local user_id=$(basename "$pid_file" .pid)
            stop_user_recording "$user_id"
        fi
    done
}

# Check status of all recordings
status() {
    echo "Recording Status:"
    echo "=================="
    
    if [ ! -d "$PID_DIR" ] || [ -z "$(ls -A "$PID_DIR" 2>/dev/null)" ]; then
        echo "No active recordings"
        return
    fi
    
    for pid_file in "$PID_DIR"/*.pid; do
        if [ -f "$pid_file" ]; then
            local user_id=$(basename "$pid_file" .pid)
            local pid=$(cat "$pid_file")
            
            if kill -0 "$pid" 2>/dev/null; then
                echo "✓ User $user_id: Recording (PID: $pid)"
            else
                echo "✗ User $user_id: Process not found (stale PID file)"
                rm -f "$pid_file"
            fi
        fi
    done
}

# Cleanup stale PID files
cleanup() {
    for pid_file in "$PID_DIR"/*.pid; do
        if [ -f "$pid_file" ]; then
            local pid=$(cat "$pid_file")
            if ! kill -0 "$pid" 2>/dev/null; then
                local user_id=$(basename "$pid_file" .pid)
                log "Removing stale PID file for user $user_id"
                rm -f "$pid_file"
            fi
        fi
    done
}

# Handle script termination
cleanup_on_exit() {
    log "Manager script terminating, stopping all recordings..."
    stop_all
    exit 0
}

trap cleanup_on_exit INT TERM

case "$1" in
    start-loop)
        # Docker用: 起動後に全ターゲットを開始し、30秒ごとに新ターゲットを検出して追加起動するループ
        log "Starting in loop mode (Docker)..."
        start_all
        while true; do
            sleep 30
            targets=$(load_targets)
            echo "$targets" | while read -r user_id _ci; do
                [ -z "$user_id" ] && continue
                pid_file="$PID_DIR/${user_id}.pid"
                if [ ! -f "$pid_file" ]; then
                    start_user_recording "$user_id"
                elif ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
                    rm -f "$pid_file"
                    start_user_recording "$user_id"
                fi
            done
        done
        ;;
    start)
        if [ -n "$2" ]; then
            start_user_recording "$2"
        else
            start_all
        fi
        ;;
    stop)
        if [ -n "$2" ]; then
            stop_user_recording "$2"
        else
            stop_all
        fi
        ;;
    restart)
        if [ -n "$2" ]; then
            stop_user_recording "$2"
            sleep 1
            start_user_recording "$2"
        else
            stop_all
            sleep 2
            start_all
        fi
        ;;
    status)
        status
        ;;
    cleanup)
        cleanup
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|cleanup} [user_id]"
        echo ""
        echo "Commands:"
        echo "  start [user_id]  - Start recording for specific user or all users"
        echo "  stop [user_id]   - Stop recording for specific user or all users"
        echo "  restart [user_id]- Restart recording for specific user or all users"
        echo "  status           - Show status of all recordings"
        echo "  cleanup          - Remove stale PID files"
        echo ""
        echo "Examples:"
        echo "  $0 start                    # Start all user recordings"
        echo "  $0 start 12345              # Start recording for user 12345"
        echo "  $0 stop                     # Stop all recordings"
        echo "  $0 status                   # Show status"
        exit 1
        ;;
esac