# streamwright.orchestration — running the streamwright CLI as Dagster pipelines (prototype)

This folder is the `streamwright-orchestration` package (`streamwright.orchestration`): named declarative DAGs (**pipelines**) that
run the `streamwright` CLI per **user** and **network** as Dagster op graphs. It never imports streamwright — every node shells out to
`$STREAMWRIGHT_BIN`.

- **Pipelines** (`config/pipelines/<name>.yaml`) — a named DAG of canonical node names and their `after` edges.
- **Networks** (`config/networks.yaml`) — each network's StreamWright source, `inputs`, `connectors`, `image` and `streams:`
  map (alias or `false`-skip each canonical node), so one pipeline runs across networks.
- **Accounts & secrets** (`src/streamwright/orchestration/accounts.py`) — the account provider (the `(user, account,
  network)` rows a trigger fans out over, each with its own `token`) and the secrets provider (per-network app
  credentials by region); each an API (`STREAMWRIGHT_ACCOUNTS_URL` / `STREAMWRIGHT_SECRETS_URL`) or a file for development.
- **Wrappers** — turn a node into its `streamwright run` command, derived from the source's declared `spec`.
- **Execution** — a local subprocess (default), a `docker run`, or a Kubernetes Job (`STREAMWRIGHT_EXECUTION`); see
  [docker/](docker/README.md), [k8s/](k8s/) and [localstack/](localstack/README.md).

## Install and test

```sh
python3 -m venv .venv && .venv/bin/pip install -e .     # from this folder
export STREAMWRIGHT_BIN=/path/to/streamwright                          # the streamwright CLI every node runs
.venv/bin/python -m unittest discover -s tests
```

📖 Full documentation: https://karthick-jaganathan.github.io/streamwright/orchestration/ — the
[architecture](https://karthick-jaganathan.github.io/streamwright/orchestration/architecture/) (model, shared vocabulary,
wrappers, execution mechanics, secrets) and [running](https://karthick-jaganathan.github.io/streamwright/orchestration/running/)
(triggering, the Dagster UI, execution modes, environment).
