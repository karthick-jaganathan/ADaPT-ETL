# syntax=docker/dockerfile:1
#
# streamwright-pipeline:local - the streamwright CLI with the AD connectors (google_ads + its gaql query builder, microsoft_ads,
# facebook_ads) and the READER connectors (files, s3, gcs, postgres), so one image can run any network of the
# pipeline (orchestration/config/networks.yaml `image:`). With STREAMWRIGHT_EXECUTION=docker each pipeline node runs
#   docker run --rm streamwright-pipeline:local run /app/examples/sources/<source> --stream <node> ...
#
# Build from the repository root (the build context; Dockerfile.dockerignore next to this file keeps it small):
#   bash orchestration/docker/build.sh
# See orchestration/docker/README.md. The ad SDKs (google-ads, bingads, facebook_business) are heavy: the
# build takes a few minutes and the image is large.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# The packages are installed from a read-write bind mount of the build context, so their sources (and pip's build
# leftovers) never become an image layer - only the installed site-packages do.
RUN --mount=type=bind,target=/build,rw \
    cd /build \
 && pip install \
      ./core \
      ./connectors/ads/google_ads ./connectors/ads/microsoft_ads ./connectors/ads/facebook_ads \
      ./connectors/readers/files ./connectors/readers/s3 ./connectors/readers/gcs ./connectors/readers/postgres \
 && streamwright --help > /dev/null

# A non-root runtime user. DuckDB's httpfs (s3, gcs), postgres and ducklake extensions (the DuckLake output: data on
# S3, a Postgres catalog) are installed for it now, so a --rm container or a Kubernetes Job pod never downloads them
# at run time.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin streamwright
USER streamwright
RUN python -c "import duckdb; c = duckdb.connect(); [c.install_extension(e) for e in ('httpfs', 'postgres', 'ducklake')]"

# The source folders at their repository-relative paths: <repo>/examples/sources/<x> is /app/examples/sources/<x>.
WORKDIR /app
COPY examples/sources /app/examples/sources

# Mount points: the user warehouses (DuckDB files) and the --summary JSON files persist on the host.
VOLUME ["/warehouse", "/runs"]

ENTRYPOINT ["streamwright"]
CMD ["--help"]
