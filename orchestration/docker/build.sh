#!/usr/bin/env bash
# Builds the pipeline image (streamwright + the ad connectors + the reader connectors) from the repository root:
#   bash orchestration/docker/build.sh            # -> streamwright-pipeline:local (or $STREAMWRIGHT_IMAGE)
#   bash orchestration/docker/build.sh --no-cache # extra arguments go to docker build
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${STREAMWRIGHT_IMAGE:-streamwright-pipeline:local}"

cd "$repo_root"
DOCKER_BUILDKIT=1 docker build -t "$image" -f orchestration/docker/Dockerfile "$@" .
docker image ls "$image" --format 'built {{.Repository}}:{{.Tag}} ({{.Size}})'
