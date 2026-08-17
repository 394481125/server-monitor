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
export GIT_CEILING_DIRECTORIES="$(dirname "$project_root")"

[[ -e "$project_root/.git" ]] || {
    echo "This release directory has not been published yet." >&2
    echo "Run scripts/publish_github.sh first; update_github.sh will not use a parent directory's repository." >&2
    exit 1
}
actual_root="$(git rev-parse --show-toplevel)"
[[ "$actual_root" == "$project_root" ]] || {
    echo "Git repository root mismatch: $actual_root" >&2
    exit 1
}
branch="$(git branch --show-current)"
[[ -n "$branch" ]] || { echo "Detached HEAD; checkout a branch first." >&2; exit 1; }
git remote get-url origin >/dev/null 2>&1 || { echo "No origin remote is configured." >&2; exit 1; }

sync_remote() {
    git fetch origin "$branch"
    if git show-ref --verify --quiet "refs/remotes/origin/${branch}" && ! git merge-base --is-ancestor "origin/${branch}" HEAD; then
        echo "Remote ${branch} contains commits not in this checkout; rebasing local work..."
        if ! git rebase --autostash "origin/${branch}"; then
            echo "Rebase stopped because of conflicts. Resolve them, run tests, then rerun this script." >&2
            echo "Do not use git push --force." >&2
            exit 1
        fi
    fi
}

sync_remote

if [[ "$skip_tests" -eq 0 ]]; then
    if [[ -x .venv/bin/python ]]; then
        .venv/bin/python -m pytest -q
    elif command -v python3 >/dev/null 2>&1 && python3 -c 'import pytest' >/dev/null 2>&1; then
        python3 -m pytest -q
    else
        echo "pytest is unavailable; continuing without tests (use a development virtualenv for full checks)." >&2
    fi
    if command -v node >/dev/null 2>&1; then
        node --check monitor/static/app_logic.js
        node --check monitor/static/app.js
        node --check scripts/browser_acceptance.js
        node --test tests_js/*.test.js
        if command -v google-chrome >/dev/null 2>&1; then
            if [[ -x .venv/bin/python ]]; then
                .venv/bin/python scripts/e2e_acceptance.py
            else
                python3 scripts/e2e_acceptance.py
            fi
        else
            echo "google-chrome is unavailable; browser E2E was skipped." >&2
        fi
    fi
fi

git add -A
if git diff --cached --quiet; then
    echo "No changes to commit."
    exit 0
fi
git diff --cached --check
git commit -m "$message"
sync_remote
git push origin "$branch"
echo "Updated origin/$branch."
