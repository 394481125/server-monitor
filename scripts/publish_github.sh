#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: bash scripts/publish_github.sh <github-repository-url> [commit-message]" >&2
    exit 2
fi

repository_url="$1"
commit_message="${2:-发布 Server Monitor}"
case "$repository_url" in
    git@github.com:*/*.git|https://github.com/*/*|https://github.com/*/*.git) ;;
    *)
        echo "Repository URL must point to github.com (SSH or HTTPS)." >&2
        exit 2
        ;;
esac

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
export GIT_CEILING_DIRECTORIES="$(dirname "$project_root")"

if [[ -e "$project_root/.git" ]]; then
    echo "This release directory already has its own .git repository." >&2
    echo "Use: bash scripts/update_github.sh \"commit message\"" >&2
    exit 1
fi
if [[ ! -f LICENSE ]]; then
    echo "Warning: LICENSE is missing. Choose a license before making the repository public." >&2
fi

git init -b main
git remote add origin "$repository_url"

# A GitHub repository may already contain an initial README or previous
# release.  Base this snapshot on remote main before committing so push stays
# fast-forward and never needs --force.
remote_main="$(git ls-remote --heads origin refs/heads/main)" || {
    echo "Unable to read origin/main. Check the repository URL and GitHub credentials." >&2
    exit 1
}
if [[ -n "$remote_main" ]]; then
    git fetch origin main
    git reset --mixed origin/main
fi

git add -A
if git diff --cached --quiet; then
    echo "No files to publish."
else
    git diff --cached --check
    git commit -m "$commit_message"
fi
git push -u origin main

echo "Published to: $repository_url"
