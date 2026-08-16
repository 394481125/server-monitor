#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: bash scripts/publish_github.sh <github-repository-url>" >&2
    exit 2
fi

repository_url="$1"
case "$repository_url" in
    git@github.com:*/*.git|https://github.com/*/*|https://github.com/*/*.git) ;;
    *)
        echo "Repository URL must point to github.com (SSH or HTTPS)." >&2
        exit 2
        ;;
esac

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "This directory is already a Git repository; push it with normal Git commands." >&2
    exit 1
fi
if [[ ! -f LICENSE ]]; then
    echo "Warning: LICENSE is missing. Choose a license before making the repository public." >&2
fi

git init -b main
git add .
git commit -m "Initial release"
git remote add origin "$repository_url"
git push -u origin main

echo "Published to: $repository_url"
