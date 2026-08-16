#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

[[ -x .venv/bin/python ]] || {
    echo "Missing .venv. Run the Ubuntu installation commands in README.md first." >&2
    exit 1
}

export SERVER_MONITOR_DATA_DIR="${SERVER_MONITOR_DATA_DIR:-${project_root}/data}"
export SERVER_MONITOR_BIND="${SERVER_MONITOR_BIND:-127.0.0.1:8000}"

exec .venv/bin/python -m gunicorn -c gunicorn.conf.py monitor.wsgi:app
