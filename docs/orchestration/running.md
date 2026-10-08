---
layout: default
title: Running
parent: Orchestration
nav_order: 3
permalink: /orchestration/running/
---

# Running the pipelines

How to install and test `streamwright-orchestration`, trigger pipelines, see the runs in the Dagster UI, and choose where each
node runs. The model behind these commands is in [Architecture]({{ site.baseurl }}/orchestration/architecture/).

## Running

Work from the [`orchestration/`](https://github.com/karthick-jaganathan/streamwright/tree/master/orchestration) folder,
in its own venv:

- The package is installed editable.
- streamwright is NOT needed in it: every node runs the streamwright CLI of `$STREAMWRIGHT_BIN` (see
  [streamwright]({{ site.baseurl }}/core/)).

```sh
python3 -m venv .venv && .venv/bin/pip install -e .                # once: streamwright-orchestration + dagster, duckdb, pyyaml
PY=.venv/bin/python
export STREAMWRIGHT_BIN=/tmp/streamwright-sdk-venv/bin/streamwright

$PY -m unittest discover -s tests                                  # unit tests
.venv/bin/dagster definitions validate -m streamwright.orchestration.definitions
$PY -c "from streamwright.orchestration import spec; print(spec.pipeline_names())"   # ['metadata', 'performance']
$PY -u demo.py                                                     # live demo: both pipelines, user u1
$PY -m streamwright.orchestration.definitions metadata u1 [ACCOUNT_ID]    # trigger a pipeline from the command line (JSON)
.venv/bin/dagster dev                                              # the ads_<pipeline>__<network> jobs in the UI
```

### Seeing CLI runs in the Dagster UI

CLI runs show in the UI only when both share one `$DAGSTER_HOME`.

- `trigger()` runs a job with `execute_in_process`, which by default uses an **ephemeral** in-process Dagster instance.
- So the run is not stored and does **not** appear in the `dagster dev` UI.
- To record CLI runs where the UI can see them, point BOTH at the same `$DAGSTER_HOME`:

```sh
export DAGSTER_HOME=$PWD/.dagster_home && mkdir -p "$DAGSTER_HOME"
.venv/bin/dagster dev -m streamwright.orchestration.definitions            # terminal 1: the UI (same DAGSTER_HOME)
$PY -m streamwright.orchestration.definitions metadata u1                  # terminal 2: now shows under Runs in the UI
```

- Without `$DAGSTER_HOME` the CLI run still executes (and writes the warehouse) — it is just not recorded.
- You can also launch runs from the UI Launchpad: pick `ads_<pipeline>__<network>`, then enter the run config
  `{user_id, account_id, network}`.

## Execution modes

`STREAMWRIGHT_EXECUTION` selects where each node's `streamwright run` executes. The op graph and run config are identical across
modes (mechanics in [Architecture]({{ site.baseurl }}/orchestration/architecture/#execution-modes-mechanics)).

| mode | where a node runs | warehouse |
|---|---|---|
| `subprocess` (default) | the local streamwright CLI (`$STREAMWRIGHT_BIN`) | a local DuckDB file |
| `docker` | a `docker run --rm` of the network's image | a mounted DuckDB file |
| `k8s` | a Kubernetes Job (one pod) of that image | DuckLake: Parquet on S3 + Postgres catalog |

```sh
# docker: build the image once, then run every node in a container
bash docker/build.sh
STREAMWRIGHT_EXECUTION=docker $PY -u demo.py

# k8s: load the image into the kind cluster, deploy the in-cluster S3 + catalog + Secret, then run
bash docker/build.sh && /tmp/bin/kind load docker-image streamwright-pipeline:local --name streamwright
bash k8s/setup.sh
STREAMWRIGHT_EXECUTION=k8s $PY -m streamwright.orchestration.definitions metadata u1
kubectl --context kind-streamwright -n streamwright exec deploy/localstack -- awslocal s3 ls s3://streamwright-warehouse/u1/ --recursive
```

The images and manifests live next to the code, on GitHub:

- [orchestration/docker/README.md](https://github.com/karthick-jaganathan/streamwright/blob/master/orchestration/docker/README.md)
  — the `streamwright-pipeline:local` image for `STREAMWRIGHT_EXECUTION=docker` and `k8s`.
- [orchestration/k8s/](https://github.com/karthick-jaganathan/streamwright/tree/master/orchestration/k8s) — `setup.sh`,
  the in-cluster S3 (LocalStack) and the DuckLake catalog (Postgres).
- [orchestration/localstack/README.md](https://github.com/karthick-jaganathan/streamwright/blob/master/orchestration/localstack/README.md)
  — a LocalStack S3 helper (docker compose) for trying the s3 reader locally.

## Environment

**Pipelines and paths**

| Variable | Notes |
|---|---|
| `STREAMWRIGHT_BIN` | the streamwright CLI every node runs |
| `STREAMWRIGHT_PIPELINE_DIR` | the pipelines folder, default `config/pipelines/` |
| `STREAMWRIGHT_PIPELINE_SPEC` | one pipeline file instead of the folder |
| `STREAMWRIGHT_PIPELINE_NETWORKS` | default `config/networks.yaml` |
| `STREAMWRIGHT_PIPELINE_WAREHOUSE_DIR` | default `warehouse/` |
| `STREAMWRIGHT_PIPELINE_RUNS_DIR` | each node's `--summary` JSON |
| `STREAMWRIGHT_ACCOUNTS_URL` / `STREAMWRIGHT_ACCOUNTS_FILE` | the accounts provider (API, else a file; default `~/.streamwright/accounts.yaml`) |
| `STREAMWRIGHT_SECRETS_URL` / `STREAMWRIGHT_SECRETS_FILE` | the secrets provider (API, else a file; default `~/.streamwright/secrets.yaml`) |
| `STREAMWRIGHT_REGION` | fallback region when an account row has none (selects the secrets region overlay) |
| `DAGSTER_HOME` | where runs are recorded, so the Dagster UI shows them (above) |

**Execution mode:** `STREAMWRIGHT_EXECUTION`, `STREAMWRIGHT_IMAGE`, `STREAMWRIGHT_DOCKER_ARGS`, `STREAMWRIGHT_DOCKER_BIN`, `STREAMWRIGHT_K8S_*`,
`STREAMWRIGHT_KUBECTL_BIN` (see
[Execution modes: mechanics]({{ site.baseurl }}/orchestration/architecture/#execution-modes-mechanics)).

Secrets reach `streamwright` only as `STREAMWRIGHT_SECRET_*` environment variables (via a Kubernetes `Secret` in k8s mode); see
[Security: secrets]({{ site.baseurl }}/orchestration/architecture/#security-secrets).
