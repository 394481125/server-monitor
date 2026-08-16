#!/usr/bin/env bash
set -euo pipefail

version="${1:-dev}"
force="${2:-}"
if [[ ! "$version" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "Version may only contain letters, numbers, dots, underscores, and hyphens." >&2
    exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_root="${project_root}/dist"
source_name="server-monitor-github-${version}"
deploy_name="server-monitor-deploy-${version}"
source_dir="${output_root}/${source_name}"
deploy_dir="${output_root}/${deploy_name}"
source_archive="${output_root}/${source_name}.tar.gz"
deploy_archive="${output_root}/${deploy_name}.tar.gz"
checksums="${output_root}/SHA256SUMS"

if [[ "$force" == "--force" ]]; then
    echo "Refusing --force: release directories are never overwritten." >&2
    echo "Use a new version name, or update the Git repository with scripts/update_github.sh." >&2
    exit 1
fi
if [[ -e "$source_dir" || -e "$deploy_dir" || -e "$source_archive" || -e "$deploy_archive" ]]; then
    echo "Release output already exists for ${version}; choose a new version number." >&2
    exit 1
fi

mkdir -p "$source_dir" "$deploy_dir"
source_files=(
    .env.example
    .dockerignore
    .gitignore
    .github
    Dockerfile
    README.md
    docker-compose.yml
    gunicorn.conf.py
    requirements.txt
    requirements.lock
    monitor
    tests
    scripts
    deploy
    docs
)
deploy_files=(
    .env.example
    .dockerignore
    Dockerfile
    README.md
    docker-compose.yml
    gunicorn.conf.py
    requirements.lock
    monitor
    deploy
    docs
    scripts/reset_admin_password.py
    scripts/quick_start.sh
    scripts/start_ubuntu.sh
)
if [[ -f "${project_root}/LICENSE" ]]; then
    source_files+=(LICENSE)
    deploy_files+=(LICENSE)
fi

copy_files() {
    local destination="$1"
    shift
    tar \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        -C "$project_root" \
        -cf - "$@" \
        | tar -C "$destination" -xf -
}

copy_files "$source_dir" "${source_files[@]}"
copy_files "$deploy_dir" "${deploy_files[@]}"
tar -C "$output_root" -czf "$source_archive" "$source_name"
tar -C "$output_root" -czf "$deploy_archive" "$deploy_name"
(
    cd "$output_root"
    sha256sum "$(basename "$source_archive")" "$(basename "$deploy_archive")" > "$(basename "$checksums")"
)

echo "Created GitHub source: $source_dir"
echo "Created GitHub archive: $source_archive"
echo "Created deployment package: $deploy_dir"
echo "Created deployment archive: $deploy_archive"
echo "Created checksums: $checksums"
