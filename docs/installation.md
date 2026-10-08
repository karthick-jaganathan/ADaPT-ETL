---
layout: default
title: Installation
nav_order: 4
description: "Installation guide for StreamWright"
permalink: /installation/
---

# StreamWright Installation Guide

StreamWright is one Python package, **streamwright** (the `streamwright` command: run, validate, connectors), plus optional connectors, each
its own package in `connectors/<name>/`.

## Prerequisites

- Python 3.10 or newer, and pip
- `make` (optional: every target below is a plain `pip install`)
- Git

streamwright installs its own dependencies: PyYAML, requests, DuckDB and pytz. The connectors install theirs (a vendor
SDK, or DuckDB for `files`, `s3`, `gcs` and `postgres`).

## Install

```bash
git clone https://github.com/karthick-jaganathan/streamwright.git
cd streamwright
python -m venv .venv && source .venv/bin/activate

make install                # streamwright
make install-connectors     # all seven connectors
```

| Mode | Command | What it does |
|---|---|---|
| prod (default) | `make install` | `pip install .` |
| dev | `make install MODE=dev` | `pip install -e .` (editable) |
| dist | `make install MODE=dist` | builds the distributions into `/tmp/sdist/streamwright`, then installs from there |

Without make:

```bash
pip install ./core
pip install ./connectors/ads/google_ads ./connectors/ads/microsoft_ads ./connectors/ads/meta_ads \
            ./connectors/readers/files ./connectors/readers/s3 ./connectors/readers/gcs ./connectors/readers/postgres
```

### Connectors

Install only the connectors your sources use:

| Connector | Make target | Package |
|---|---|---|
| `google_ads` | `make install-google-ads` | `streamwright-google-ads` (google-ads) |
| `microsoft_ads` | `make install-microsoft-ads` | `streamwright-microsoft-ads` (bingads) |
| `meta_ads` | `make install-meta-ads` | `streamwright-meta-ads` (facebook_business) |
| `files` | `make install-files` | `streamwright-files` (DuckDB) |
| `s3` | `make install-s3` | `streamwright-s3` (DuckDB httpfs) |
| `gcs` | `make install-gcs` | `streamwright-gcs` (DuckDB httpfs) |
| `postgres` | `make install-postgres` | `streamwright-postgres` (DuckDB postgres extension) |

### dlt

`--output dlt:DESTINATION` needs dlt and the destination's extra:

```bash
pip install "./core[dlt]" "dlt[duckdb]"     # or "dlt[bigquery]", ...
```

## Verify

```bash
streamwright --help
streamwright connectors                    # one line per installed connector
streamwright validate examples/sources     # "... file(s) checked: 0 error(s), 0 warning(s)"
streamwright validate --help
make verify
make verify-connectors
```

## Environment variables

| Variable | Used for |
|---|---|
| `STREAMWRIGHT_SECRET_<NAME>` | a value for the source's `spec.secrets` entry `<name>` (`--secrets FILE` overrides it) |
| `STREAMWRIGHT_CONFIGS` | the default path of `streamwright validate` when no PATH is given |

## Docker

```bash
docker compose build                                    # streamwright and all connectors, editable, in /app
docker compose run --rm streamwright streamwright connectors
docker compose run --rm streamwright streamwright validate examples/sources
```

or `docker build -t streamwright .` and `docker run --rm streamwright streamwright --help`. The image used by the Dagster
orchestration, `streamwright-pipeline:local` (streamwright with the ad and reader connectors), is built by
`bash docker/build.sh` in [streamwright-orchestration](https://github.com/karthick-jaganathan/streamwright-orchestration); see [orchestration/docker/README.md](https://github.com/karthick-jaganathan/streamwright-orchestration/blob/main/docker/README.md), and
[orchestration/README.md](https://github.com/karthick-jaganathan/streamwright-orchestration) for the orchestration itself.

## Development setup

```bash
make install MODE=dev && make install-connectors MODE=dev
pip install pytest jsonschema "dlt[duckdb]"
make test          # python -m pytest tests connectors -q
make validate      # streamwright validate --strict configs
```

Connector tests that need a vendor SDK are skipped when it is not installed.

## Uninstall and clean

```bash
make uninstall     # streamwright and every connector
make clean         # build artifacts, __pycache__, *.egg-info
make clean-dist    # /tmp/sdist/streamwright
```

## Troubleshooting

- **`streamwright: command not found`**: the virtual environment that has streamwright is not active, or its `bin/` is not
  on `PATH`.
- **`ModuleNotFoundError: No module named 'streamwright.source'`**: install streamwright (`make install`) in the Python you
  run.
- **A connector is missing** (`streamwright run` exits with status 2 and names it): install it, e.g. `make install-google-ads`;
  `streamwright connectors` lists the installed ones.
- **Leftovers of the removed packages** (`adapt-utils`, `adapt-connector`, `adapt-serializer`, `adapt-pipeline`): they
  are no longer part of StreamWright; remove them with `pip uninstall adapt-utils adapt-connector adapt-serializer
  adapt-pipeline`.
