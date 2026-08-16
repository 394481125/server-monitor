#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: bash scripts/update_github.sh \"commit message\" [--skip-tests]" >&2
    exit 2
fi

skip_tests=0
message="$1"
if [[ "${2:-}" == "--skip-tests" ]]; then
    skip_tests=1
fi
if [[ -z "$message" || "$message" == "--skip-tests" ]]; then
    echo "A non-empty commit message is required." >&2
    exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "Run this script inside the GitHub source repository." >&2
    exit 1
}
branch="$(git branch --show-current)"
[[ -n "$branch" ]] || { echo "Detached HEAD; checkout a branch first." >&2; exit 1; }
git remote get-url origin >/dev/null 2>&1 || { echo "No origin remote is configured." >&2; exit 1; }

if [[ "$skip_tests" -eq 0 ]]; then
    if [[ -x .venv/bin/python ]]; then
        .venv/bin/python -m pytest -q
    elif command -v python3 >/dev/null 2>&1 && python3 -c 'import pytest' >/dev/null 2>&1; then
        python3 -m pytest -q
    else
        echo "pytest is unavailable; continuing without tests (use a development virtualenv for full checks)." >&2
    fi
    if command -v node >/dev/null 2>&1; then
        node --check monitor/static/app.js
    fi
fi

git add -A
if git diff --cached --quiet; then
    echo "No changes to commit."
    exit 0
fi
git diff --cached --check
git commit -m "$message"
git push origin "$branch"
echo "Updated origin/$branch."
