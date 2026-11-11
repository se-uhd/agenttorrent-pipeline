#!/usr/bin/env bash
# Build the Linux development image with the locked distributions of every stage: x86-64 by default,
# tagged agentcontext-development:latest, or with the argument `arm64` for Apple silicon, tagged
# agentcontext-development:arm64, where the x86-64 image runs emulated.
set -euo pipefail
case "${1:-amd64}" in
    amd64) platform=linux/amd64 tag=agentcontext-development:latest ;;
    arm64) platform=linux/arm64 tag=agentcontext-development:arm64 ;;
    *) echo "usage: $0 [amd64|arm64]" >&2; exit 2 ;;
esac
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
for path in locks/build.txt locks/development.txt mise.toml deploy/install_tools.sh; do
    mkdir -p "$context/$(dirname "$path")"
    cp "$ROOT/$path" "$context/$path"
done
cp "$ROOT/deploy/Dockerfile.development" "$context/Dockerfile"
docker buildx build --platform "$platform" --load -t "$tag" "$context"
