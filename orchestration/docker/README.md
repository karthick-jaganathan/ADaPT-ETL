# streamwright-pipeline:local — the pipeline's container image

With `STREAMWRIGHT_EXECUTION=docker`, every pipeline node's `streamwright run` executes in a **`docker run --rm` container** of the
network's image instead of the local streamwright CLI. This folder builds that image.

```
orchestration/docker/
├── Dockerfile                 # streamwright-pipeline:local - streamwright + the ad connectors + the reader connectors
├── Dockerfile.dockerignore    # the build context allow-list (Docker reads <Dockerfile>.dockerignore, not ./.dockerignore)
└── build.sh                   # docker build -t streamwright-pipeline:local -f orchestration/docker/Dockerfile .
```

## The image

`python:3.13-slim` + `streamwright` (the `streamwright` CLI) + the **ad connectors** (`connectors/ads/google_ads` with its
`gaql` query builder, `microsoft_ads`, `facebook_ads`) + the **reader connectors** (`connectors/readers/files`, `s3`,
`gcs`, `postgres`), so one image runs any network in `config/networks.yaml`. The ad SDKs (google-ads, bingads,
facebook_business) are heavy: the build takes a few minutes and the image is ~480 MB.

- Installed from a bind mount of the build context (`--mount=type=bind,target=/build,rw`): package sources never become
  a layer. The context is the repository root filtered by `Dockerfile.dockerignore` (an allow-list: `streamwright`, the
  connectors above, `examples/sources`; ~1.5 MB — never `.git`, `.venv`, `warehouse/`, `runs/` or secrets files).
- `examples/sources` is copied to `/app/examples/sources` (`WORKDIR /app`): the repository path
  `<repo>/examples/sources/ads/google_ads` is `/app/examples/sources/ads/google_ads` in the container. Changing a source folder
  means rebuilding the image.
- Runs as the non-root user `streamwright` (uid 1000); DuckDB's `httpfs`/`postgres`/`ducklake` extensions are pre-installed for it.
- `ENTRYPOINT ["streamwright"]`, `VOLUME ["/warehouse", "/runs"]`.
- **No secrets** are in the image: credentials arrive at run time as environment variables.

```bash
bash orchestration/docker/build.sh                        # from anywhere; extra args go to docker build
docker run --rm streamwright-pipeline:local connectors           # google_ads, microsoft_ads, facebook_ads, files, s3, gcs, postgres
```

## Running the pipeline in containers

```bash
cd orchestration
STREAMWRIGHT_EXECUTION=docker .venv/bin/python demo.py 2>&1 | tee demo_docker_run.log      # both pipelines
STREAMWRIGHT_EXECUTION=docker .venv/bin/python -m streamwright.orchestration.definitions metadata u1 1000000001
```

| variable            | meaning                                                                                       |
|---------------------|-----------------------------------------------------------------------------------------------|
| `STREAMWRIGHT_EXECUTION`   | `subprocess` (default: the local `$STREAMWRIGHT_BIN`) or `docker`                                    |
| `STREAMWRIGHT_IMAGE`       | overrides the network's `image:` (config/networks.yaml) for every network                     |
| `STREAMWRIGHT_DOCKER_ARGS` | extra `docker run` options placed before the image (e.g. `--user 1000:1000 --network streamwright`) |
| `STREAMWRIGHT_DOCKER_BIN`  | the docker CLI (default `docker`)                                                             |

The wrapper still builds the same logical, host-oriented `streamwright run` argv; the runner (`streamwright.orchestration.runner`,
`docker_command` in `runner/docker.py`) translates it:

```
docker run --rm --init --pull never --name streamwright-pipe-u1-1000000001-campaigns-<run id> \
  --add-host host.docker.internal:host-gateway \
  -v <orchestration>/warehouse:/warehouse -v <orchestration>/runs:/runs \
  -e STREAMWRIGHT_SECRET_CLIENT_ID -e STREAMWRIGHT_SECRET_CLIENT_SECRET -e STREAMWRIGHT_SECRET_DEVELOPER_TOKEN -e STREAMWRIGHT_SECRET_REFRESH_TOKEN \
  streamwright-pipeline:local run /app/examples/sources/ads/google_ads --stream campaigns \
  --set customer_ids=1000000001 --set login_customer_id=2000000002 --set start_date=-7d \
  --timezone America/Los_Angeles --output duckdb:/warehouse/u1.duckdb:google_ads_1000000001 \
  --allow-connector google_ads --allow-connector gaql --summary /runs/<run id>/campaigns.summary.json
```

- the source folder (and `<repo>` paths in `--set` values) → `/app/...`;
- `--output duckdb:<host warehouse>/<user>.duckdb:<schema>` → `duckdb:/warehouse/<user>.duckdb:<schema>`, and
  `--summary <runs>/<run>/<node>.summary.json` → `/runs/<run>/<node>.summary.json` — both mounted, so the DuckDB file
  and the summary persist on the host (the runner reads the summary from the host path);
- `--stream`, `--timezone`, `--allow-connector` are unchanged.

**Secrets** are passed as `-e STREAMWRIGHT_SECRET_<NAME>` **without a value**: the runner puts the values only in the
environment of the `docker` process it spawns, and docker copies them into the container. No secret value is ever in
the argv, the image, the run config or the log (streamwright's output is redacted for the run's secret values, and
`docker_command` refuses to build an argv that contains one). If the run is interrupted, the runner removes the
container (`docker rm -f <name>`).

On Linux, the container user (uid 1000) must be able to write the mounted `warehouse/` and `runs/` folders: either
make them writable for it or run as yourself with `STREAMWRIGHT_DOCKER_ARGS="--user $(id -u):$(id -g)"`.
