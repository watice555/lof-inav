#!/bin/bash

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
DATA_DIR="${LOF_INAV_DATA_DIR:-$ROOT/data}"
PID_PATH="$DATA_DIR/lof_inav.pid"
DEFAULT_PORT="${LOF_INAV_PORT:-8001}"

pause_before_close() {
    if [ -t 0 ]; then
        printf '\nPress Enter to close...'
        read -r _
    fi
}

read_json_number() {
    key="$1"
    file="$2"
    sed -nE "s/.*\"${key}\"[[:space:]]*:[[:space:]]*([0-9]+).*/\\1/p" "$file" | head -n 1
}

process_command() {
    ps -p "$1" -o command= 2>/dev/null
}

is_project_server() {
    pid="$1"
    port="$2"

    case "$pid:$port" in
        *[!0-9:]*|:*) return 1 ;;
    esac

    kill -0 "$pid" 2>/dev/null || return 1

    command_line="$(process_command "$pid")"
    case "$command_line" in
        *"$ROOT/serve.py"*) ;;
        *) return 1 ;;
    esac

    command -v lsof >/dev/null 2>&1 || return 1
    [ -n "$(lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)" ]
}

stop_pid() {
    pid="$1"
    port="$2"
    command_line="$(process_command "$pid")"
    printf 'Stopping LOF iNAV server PID %s: %s\n' "$pid" "$command_line"
    kill -TERM "$pid" 2>/dev/null || return 1

    attempts=0
    while kill -0 "$pid" 2>/dev/null && [ "$attempts" -lt 50 ]; do
        sleep 0.1
        attempts=$((attempts + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        printf 'Server did not stop gracefully; forcing it to exit.\n'
        kill -KILL "$pid" 2>/dev/null || return 1
    fi

    rm -f "$PID_PATH"
    printf 'LOF iNAV server stopped.\n'

    if [ -z "$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)" ]; then
        printf 'Port %s is free.\n' "$port"
    else
        printf 'Port %s is still used by another process; it was left untouched.\n' "$port"
    fi
}

pid=""
port="$DEFAULT_PORT"

if [ -f "$PID_PATH" ]; then
    pid="$(read_json_number pid "$PID_PATH")"
    recorded_port="$(read_json_number port "$PID_PATH")"
    if [ -n "$recorded_port" ]; then
        port="$recorded_port"
    fi

    if is_project_server "$pid" "$port"; then
        stop_pid "$pid" "$port" || {
            printf 'Failed to stop LOF iNAV server PID %s.\n' "$pid" >&2
            pause_before_close
            exit 1
        }
        pause_before_close
        exit 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_PATH"
    else
        printf 'PID file was not trusted; leaving process %s untouched.\n' "$pid"
        pause_before_close
        exit 1
    fi
fi

# Backward-compatible fallback for a server started before PID-file support.
listener_pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sort -u)"
for candidate_pid in $listener_pids; do
    if is_project_server "$candidate_pid" "$port"; then
        stop_pid "$candidate_pid" "$port" || {
            printf 'Failed to stop LOF iNAV server PID %s.\n' "$candidate_pid" >&2
            pause_before_close
            exit 1
        }
        pause_before_close
        exit 0
    fi
done

printf 'No verified LOF iNAV server process found.\n'
if [ -z "$listener_pids" ]; then
    printf 'Port %s is free.\n' "$port"
else
    printf 'Port %s is used by another process; it was left untouched.\n' "$port"
fi
pause_before_close
