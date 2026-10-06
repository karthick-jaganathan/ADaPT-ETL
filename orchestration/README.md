# adapt.orchestration — running the adapt CLI as Dagster pipelines (prototype)

This folder is the `adapt-orchestration` package (`adapt.orchestration`): named declarative DAGs (**pipelines**) that
run the `adapt` CLI per **user** and **network** as Dagster op graphs. It never imports adapt — every node shells out to
`$ADAPT_BIN`.

- **Pipelines** (`config/pipelines/<name>.yaml`) — a named DAG of canonical node names and their `after` edges.
- **Networks** (`config/networks.yaml`) — each network's ADaPT source, `inputs`, `connectors`, `image` and `streams:`
  map (alias or `false`-skip each canonical node), so one pipeline runs across networks.
- **Accounts** (`src/adapt/orchestration/accounts.py`) — the `(user, account, network)` rows a trigger fans out over.
- **Wrappers** — turn a node into its `adapt run` command, derived from the source's declared `spec`.
- **Execution** — a local subprocess (default), a `docker run`, or a Kubernetes Job (`ADAPT_EXECUTION`); see
  [docker/](docker/README.md), [k8s/](k8s/) and [localstack/](localstack/README.md).

## Install and test

```sh
python3 -m venv .venv && .venv/bin/pip install -e .     # from this folder
export ADAPT_BIN=/path/to/adapt                          # the adapt CLI every node runs
.venv/bin/python -m unittest discover -s tests
```

📖 Full documentation: https://karthick-jaganathan.github.io/ADaPT-ETL/orchestration/ — the
[architecture](https://karthick-jaganathan.github.io/ADaPT-ETL/orchestration/architecture/) (model, shared vocabulary,
wrappers, execution mechanics, secrets) and [running](https://karthick-jaganathan.github.io/ADaPT-ETL/orchestration/running/)
(triggering, the Dagster UI, execution modes, environment).
