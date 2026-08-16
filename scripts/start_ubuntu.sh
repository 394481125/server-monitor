#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

[[ -x .venv/bin/python ]] || {
    echo "Missing .venv. Run the Ubuntu installation commands in README.md first." >&2
    exit 1
}

action="${1:-start}"
export SERVER_MONITOR_DATA_DIR="${SERVER_MONITOR_DATA_DIR:-${project_root}/data}"
export SERVER_MONITOR_BIND="${SERVER_MONITOR_BIND:-127.0.0.1:8000}"

pid_file="${SERVER_MONITOR_PID_FILE:-${SERVER_MONITOR_DATA_DIR}/server-monitor.pid}"
log_dir="${SERVER_MONITOR_LOG_DIR:-${SERVER_MONITOR_DATA_DIR}/logs}"
log_file="${log_dir}/server-monitor.log"

mkdir -p "$SERVER_MONITOR_DATA_DIR" "$log_dir"
chmod 700 "$SERVER_MONITOR_DATA_DIR" "$log_dir"

bind_host="${SERVER_MONITOR_BIND%:*}"
bind_port="${SERVER_MONITOR_BIND##*:}"
case "$bind_host" in
    0.0.0.0|::|"[::]") health_host="127.0.0.1" ;;
    *) health_host="$bind_host" ;;
esac
health_url="http://${health_host}:${bind_port}/health"

read_pid() {
    local pid
    [[ -r "$pid_file" ]] || return 1
    pid="$(<"$pid_file")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$pid"
}

pid_matches_service() {
    local pid="$1" cmdline process_cwd
    [[ -r "/proc/${pid}/cmdline" ]] || return 1
    cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline")"
    [[ "$cmdline" == *gunicorn* && "$cmdline" == *monitor.wsgi:app* ]] || return 1
    process_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null)" || return 1
    [[ "$process_cwd" == "$project_root" ]]
}

is_running() {
    local pid
    pid="$(read_pid)" || return 1
    kill -0 "$pid" 2>/dev/null && pid_matches_service "$pid"
}

health_ok() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --max-time 2 "$health_url" >/dev/null 2>&1
    else
        is_running
    fi
}

start_service() {
    if is_running; then
        local pid
        pid="$(read_pid)"
        if health_ok; then
            echo "Server Monitor is already running (PID ${pid})."
            echo "Open: http://${health_host}:${bind_port}"
            return 0
        fi
        echo "Server Monitor process ${pid} exists, but the health check failed." >&2
        echo "Check: ${log_file}" >&2
        return 1
    fi

    if [[ -e "$pid_file" ]]; then
        rm -f "$pid_file"
    fi
    if health_ok; then
        echo "A service is already responding at ${health_url}, but it is not managed by ${pid_file}." >&2
        echo "Stop the existing service before starting another instance." >&2
        return 1
    fi

    .venv/bin/python -m gunicorn \
        --daemon \
        --pid "$pid_file" \
        --error-logfile "$log_file" \
        --access-logfile "$log_file" \
        -c gunicorn.conf.py \
        monitor.wsgi:app

    for _ in {1..50}; do
        if is_running && health_ok; then
            echo "Server Monitor started (PID $(read_pid))."
            echo "Open: http://${health_host}:${bind_port}"
            echo "Log: ${log_file}"
            return 0
        fi
        sleep 0.2
    done

    echo "Server Monitor failed to become healthy. Check: ${log_file}" >&2
    tail -n 30 "$log_file" >&2 || true
    return 1
}

stop_service() {
    local pid
    if ! pid="$(read_pid)" || ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pid_file"
        echo "Server Monitor is not running."
        return 0
    fi
    if ! pid_matches_service "$pid"; then
        rm -f "$pid_file"
        echo "Removed a stale PID file; PID ${pid} does not belong to this Server Monitor instance." >&2
        if health_ok; then
            echo "A service is still responding at ${health_url}, but this script will not stop an unmanaged process." >&2
            return 1
        fi
        return 0
    fi

    kill -TERM "$pid"
    for _ in {1..70}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file"
            echo "Server Monitor stopped."
            return 0
        fi
        sleep 0.5
    done

    echo "Server Monitor did not stop within 35 seconds (PID ${pid})." >&2
    echo "Inspect the process and ${log_file}; the script will not force-kill it." >&2
    return 1
}

status_service() {
    if is_running; then
        local pid state
        pid="$(read_pid)"
        state="unhealthy"
        health_ok && state="healthy"
        echo "Server Monitor is running (PID ${pid}, ${state})."
        echo "Open: http://${health_host}:${bind_port}"
        echo "Log: ${log_file}"
        [[ "$state" == "healthy" ]]
        return
    fi
    if health_ok; then
        echo "A service is responding at ${health_url}, but ${pid_file} does not identify it." >&2
        return 1
    fi
    echo "Server Monitor is stopped."
    return 3
}

case "$action" in
    start) start_service ;;
    stop) stop_service ;;
    restart)
        stop_service
        start_service
        ;;
    status) status_service ;;
    foreground|run)
        if is_running || health_ok; then
            echo "Server Monitor is already running; stop it before foreground mode." >&2
            exit 1
        fi
        exec .venv/bin/python -m gunicorn --pid "$pid_file" -c gunicorn.conf.py monitor.wsgi:app
        ;;
    *)
        echo "Usage: bash scripts/start_ubuntu.sh [start|stop|restart|status|foreground]" >&2
        exit 2
        ;;
esac
