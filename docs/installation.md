---
layout: default
title: Installation
nav_order: 4
description: "Installation guide for ADaPT (Adaptive Data Pipeline Toolkit)"
permalink: /installation/
---

# ADaPT Installation Guide

ADaPT is one Python package, **adapt-core** (the `adapt` command: run, validate, connectors), plus optional connectors, each
its own package in `connectors/<name>/`.

## Prerequisites

- Python 3.10 or newer, and pip
- `make` (optional: every target below is a plain `pip install`)
- Git

adapt-core installs its own dependencies: PyYAML, requests, DuckDB and pytz. The connectors install theirs (a vendor
SDK, or DuckDB for `files`, `s3`, `gcs` and `postgres`).

## Install

```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL
python -m venv .venv && source .venv/bin/activate

make install                # adapt-core
make install-connectors     # all seven connectors
```

| Mode | Command | What it does |
|---|---|---|
| prod (default) | `make install` | `pip install .` |
| dev | `make install MODE=dev` | `pip install -e .` (editable) |
| dist | `make install MODE=dist` | builds the distributions into `/tmp/sdist/adapt`, then installs from there |

Without make:

```bash
pip install ./adapt-core
pip install ./connectors/ads/google_ads ./connectors/ads/microsoft_ads ./connectors/ads/facebook_ads \
            ./connectors/readers/files ./connectors/readers/s3 ./connectors/readers/gcs ./connectors/readers/postgres
```

### Connectors

Install only the connectors your sources use:

| Connector | Make target | Package |
|---|---|---|
| `google_ads` | `make install-google-ads` | `adapt-google-ads` (google-ads) |
| `microsoft_ads` | `make install-microsoft-ads` | `adapt-microsoft-ads` (bingads) |
| `facebook_ads` | `make install-facebook-ads` | `adapt-facebook-ads` (facebook_business) |
| `files` | `make install-files` | `adapt-files` (DuckDB) |
| `s3` | `make install-s3` | `adapt-s3` (DuckDB httpfs) |
| `gcs` | `make install-gcs` | `adapt-gcs` (DuckDB httpfs) |
| `postgres` | `make install-postgres` | `adapt-postgres` (DuckDB postgres extension) |

### dlt

`--output dlt:DESTINATION` needs dlt and the destination's extra:

```bash
pip install "./adapt-core[dlt]" "dlt[duckdb]"     # or "dlt[bigquery]", ...
```

## Verify

```bash
adapt --help
adapt connectors                    # one line per installed connector
adapt validate examples/sources     # "... file(s) checked: 0 error(s), 0 warning(s)"
adapt validate --help
make verify
make verify-connectors
```

## Environment variables

| Variable | Used for |
|---|---|
| `ADAPT_SECRET_<NAME>` | a value for the source's `spec.secrets` entry `<name>` (`--secrets FILE` overrides it) |
| `ADAPT_CONFIGS` | the default path of `adapt validate` when no PATH is given |

## Docker

```bash
docker compose build                                    # adapt-core and all connectors, editable, in /app
docker compose run --rm adapt-etl adapt connectors
docker compose run --rm adapt-etl adapt validate examples/sources
```

or `docker build -t adapt-etl .` and `docker run --rm adapt-etl adapt --help`. The image used by the Dagster
orchestration, `adapt-pipeline:local` (adapt-core with the ad and reader connectors), is built by
`bash orchestration/docker/build.sh`; see [orchestration/docker/README.md](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/orchestration/docker/README.md), and
[orchestration/README.md](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/orchestration/README.md) for the orchestration itself.

## Development setup

```bash
make install MODE=dev && make install-connectors MODE=dev
pip install pytest jsonschema "dlt[duckdb]"
make test          # python -m pytest tests connectors -q
make validate      # adapt validate --strict configs
```

Connector tests that need a vendor SDK are skipped when it is not installed.

## Uninstall and clean

```bash
make uninstall     # adapt-core and every connector
make clean         # build artifacts, __pycache__, *.egg-info
make clean-dist    # /tmp/sdist/adapt
```

## Troubleshooting

- **`adapt: command not found`**: the virtual environment that has adapt-core is not active, or its `bin/` is not
  on `PATH`.
- **`ModuleNotFoundError: No module named 'adapt.source'`**: install adapt-core (`make install`) in the Python you
  run.
- **A connector is missing** (`adapt run` exits with status 2 and names it): install it, e.g. `make install-google-ads`;
  `adapt connectors` lists the installed ones.
- **Leftovers of the removed packages** (`adapt-utils`, `adapt-connector`, `adapt-serializer`, `adapt-pipeline`): they
  are no longer part of ADaPT; remove them with `pip uninstall adapt-utils adapt-connector adapt-serializer
  adapt-pipeline`.
