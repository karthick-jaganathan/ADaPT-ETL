---
layout: default
title: Running
parent: Orchestration
nav_order: 3
permalink: /orchestration/running/
---

# Running the pipelines

How to install and test `adapt-orchestration`, trigger pipelines, see the runs in the Dagster UI, and choose where each
node runs. The model behind these commands is in [Architecture]({{ site.baseurl }}/orchestration/architecture/).

## Running

Work from the [`orchestration/`](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration) folder,
in its own venv:

- The package is installed editable.
- adapt-core is NOT needed in it: every node runs the adapt CLI of `$ADAPT_BIN` (see
  [adapt-core]({{ site.baseurl }}/adapt-core/)).

```sh
python3 -m venv .venv && .venv/bin/pip install -e .                # once: adapt-orchestration + dagster, duckdb, pyyaml
PY=.venv/bin/python
export ADAPT_BIN=/tmp/adapt-sdk-venv/bin/adapt

$PY -m unittest discover -s tests                                  # unit tests
.venv/bin/dagster definitions validate -m adapt.orchestration.definitions
$PY -c "from adapt.orchestration import spec; print(spec.pipeline_names())"   # ['metadata', 'performance']
$PY -u demo.py                                                     # live demo: both pipelines, user u1
$PY -m adapt.orchestration.definitions metadata u1 [ACCOUNT_ID]    # trigger a pipeline from the command line (JSON)
.venv/bin/dagster dev                                              # the ads_<pipeline>__<network> jobs in the UI
```

### Seeing CLI runs in the Dagster UI

CLI runs show in the UI only when both share one `$DAGSTER_HOME`.

- `trigger()` runs a job with `execute_in_process`, which by default uses an **ephemeral** in-process Dagster instance.
- So the run is not stored and does **not** appear in the `dagster dev` UI.
- To record CLI runs where the UI can see them, point BOTH at the same `$DAGSTER_HOME`:

```sh
export DAGSTER_HOME=$PWD/.dagster_home && mkdir -p "$DAGSTER_HOME"
.venv/bin/dagster dev -m adapt.orchestration.definitions            # terminal 1: the UI (same DAGSTER_HOME)
$PY -m adapt.orchestration.definitions metadata u1                  # terminal 2: now shows under Runs in the UI
```

- Without `$DAGSTER_HOME` the CLI run still executes (and writes the warehouse) — it is just not recorded.
- You can also launch runs from the UI Launchpad: pick `ads_<pipeline>__<network>`, then enter the run config
  `{user_id, account_id, network}`.

## Execution modes

`ADAPT_EXECUTION` selects where each node's `adapt run` executes. The op graph and run config are identical across
modes (mechanics in [Architecture]({{ site.baseurl }}/orchestration/architecture/#execution-modes-mechanics)).

| mode | where a node runs | warehouse |
|---|---|---|
| `subprocess` (default) | the local adapt CLI (`$ADAPT_BIN`) | a local DuckDB file |
| `docker` | a `docker run --rm` of the network's image | a mounted DuckDB file |
| `k8s` | a Kubernetes Job (one pod) of that image | DuckLake: Parquet on S3 + Postgres catalog |

```sh
# docker: build the image once, then run every node in a container
bash docker/build.sh
ADAPT_EXECUTION=docker $PY -u demo.py

# k8s: load the image into the kind cluster, deploy the in-cluster S3 + catalog + Secret, then run
bash docker/build.sh && /tmp/bin/kind load docker-image adapt-pipeline:local --name adapt
bash k8s/setup.sh
ADAPT_EXECUTION=k8s $PY -m adapt.orchestration.definitions metadata u1
kubectl --context kind-adapt -n adapt exec deploy/localstack -- awslocal s3 ls s3://adapt-warehouse/u1/ --recursive
```

The images and manifests live next to the code, on GitHub:

- [orchestration/docker/README.md](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/orchestration/docker/README.md)
  — the `adapt-pipeline:local` image for `ADAPT_EXECUTION=docker` and `k8s`.
- [orchestration/k8s/](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration/k8s) — `setup.sh`,
  the in-cluster S3 (LocalStack) and the DuckLake catalog (Postgres).
- [orchestration/localstack/README.md](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/orchestration/localstack/README.md)
  — a LocalStack S3 helper (docker compose) for trying the s3 reader locally.

## Environment

**Pipelines and paths**

| Variable | Notes |
|---|---|
| `ADAPT_BIN` | the adapt CLI every node runs |
| `ADAPT_PIPELINE_DIR` | the pipelines folder, default `config/pipelines/` |
| `ADAPT_PIPELINE_SPEC` | one pipeline file instead of the folder |
| `ADAPT_PIPELINE_NETWORKS` | default `config/networks.yaml` |
| `ADAPT_PIPELINE_WAREHOUSE_DIR` | default `warehouse/` |
| `ADAPT_PIPELINE_RUNS_DIR` | each node's `--summary` JSON |
| `ADAPT_ACCOUNTS_URL` / `ADAPT_ACCOUNTS_FILE` | the accounts provider (API, else a file; default `~/.adapt/accounts.yaml`) |
| `ADAPT_SECRETS_URL` / `ADAPT_SECRETS_FILE` | the secrets provider (API, else a file; default `~/.adapt/secrets.yaml`) |
| `ADAPT_REGION` | fallback region when an account row has none (selects the secrets region overlay) |
| `DAGSTER_HOME` | where runs are recorded, so the Dagster UI shows them (above) |

**Execution mode:** `ADAPT_EXECUTION`, `ADAPT_IMAGE`, `ADAPT_DOCKER_ARGS`, `ADAPT_DOCKER_BIN`, `ADAPT_K8S_*`,
`ADAPT_KUBECTL_BIN` (see
[Execution modes: mechanics]({{ site.baseurl }}/orchestration/architecture/#execution-modes-mechanics)).

Secrets reach `adapt` only as `ADAPT_SECRET_*` environment variables (via a Kubernetes `Secret` in k8s mode); see
[Security: secrets]({{ site.baseurl }}/orchestration/architecture/#security-secrets).
