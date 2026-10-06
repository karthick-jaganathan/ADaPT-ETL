#!/usr/bin/env bash
# Builds the pipeline image (adapt + the ad connectors + the reader connectors) from the repository root:
#   bash orchestration/docker/build.sh            # -> adapt-pipeline:local (or $ADAPT_IMAGE)
#   bash orchestration/docker/build.sh --no-cache # extra arguments go to docker build
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${ADAPT_IMAGE:-adapt-pipeline:local}"

cd "$repo_root"
DOCKER_BUILDKIT=1 docker build -t "$image" -f orchestration/docker/Dockerfile "$@" .
docker image ls "$image" --format 'built {{.Repository}}:{{.Tag}} ({{.Size}})'
