#!/bin/bash
# Recorder container entrypoint
# Environment variables are passed in via docker-compose

export OUTPUT_DIR="${OUTPUT_DIR:-/app/data}"
export TARGETS_FILE="${TARGETS_FILE:-/app/config/targets.json}"
export PID_DIR="${PID_DIR:-/app/pids}"
export LOG_FILE="${LOG_FILE:-/dev/null}"  # stdout from tee already handles output
export RECORDER_SCRIPT="/app/recorder_single.sh"

# Wait for targets.json to exist (web container may not have created it yet)
echo "Waiting for targets.json at $TARGETS_FILE..."
for i in $(seq 1 30); do
    if [ -f "$TARGETS_FILE" ]; then
        echo "targets.json found."
        break
    fi
    sleep 2
done

exec bash /app/manage_recordings.sh start-loop
