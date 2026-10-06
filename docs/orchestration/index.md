---
layout: default
title: Orchestration
nav_order: 7
has_children: true
permalink: /orchestration/
---

# adapt.orchestration — running the adapt CLI as Dagster pipelines

Named declarative DAGs (**pipelines**) run the `adapt` CLI per **user** and **network**, as Dagster op graphs.

- **Network-agnostic:** Google Ads is just one entry in `config/networks.yaml`.
- **Never imports adapt:** every node shells out to the adapt CLI (`$ADAPT_BIN`, default
  `/tmp/adapt-sdk-venv/bin/adapt`).
- **Package:** `adapt.orchestration` (distribution `adapt-orchestration`), in the
  [`orchestration/`](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration) folder of the
  repository.
- **Status:** a **prototype**.

{: .note }
**Prototype stubs:** `config/networks.yaml` (the network registry) and `src/adapt/orchestration/accounts.py` (the
account lookup) are stand-ins; in production both come from a database / registry service.

| Page | What it covers |
|---|---|
| [Architecture]({{ site.baseurl }}/orchestration/architecture/) | the pipeline/network model, the shared vocabulary, wrappers, op graphs and the trigger, the execution-mode mechanics and how secrets flow |
| [Running]({{ site.baseurl }}/orchestration/running/) | installing and testing the package, triggering pipelines, seeing CLI runs in the Dagster UI, the execution modes (`subprocess`, `docker`, `k8s`) and the environment variables |

For how orchestration fits the whole ADaPT system (with sequence diagrams), see the
[Architecture guide]({{ site.baseurl }}/architecture/). Every node runs [adapt-core]({{ site.baseurl }}/adapt-core/)'s
`adapt run`.

## In one minute

- **Pipelines** (`config/pipelines/<name>.yaml`) — a named DAG of **canonical** node names and their `after` edges.
- **Networks** (`config/networks.yaml`) — map each network to an ADaPT source, with:
  - its `inputs`, `connectors` and container `image`;
  - a `streams:` map (the [*shared vocabulary*]({{ site.baseurl }}/orchestration/architecture/#shared-vocabulary-one-pipeline-across-networks-that-name-streams-differently)):
    alias or `false`-skip each canonical node.

  So one pipeline runs across many networks that name (or lack) streams differently.
- **Accounts** (`accounts.py`) — the `(user, account, network)` rows a trigger fans out over.
- **Wrappers** — turn a node into its `adapt run` command, derived from the source's declared `spec`.
- **Execution** — each node runs as a local subprocess (default), a `docker run`, or a Kubernetes Job
  (`ADAPT_EXECUTION`; see [Execution modes]({{ site.baseurl }}/orchestration/running/#execution-modes)).

- Each `(pipeline, network)` is its own Dagster job `ads_<pipeline>__<network>`.
- `trigger(pipeline, user)` runs it for each of the user's accounts.

See [Architecture]({{ site.baseurl }}/orchestration/architecture/) for the full model.

## Security in short

Secrets never travel as data.

- They are resolved at run time.
- They are passed to `adapt` only as `ADAPT_SECRET_*` environment variables (via a Kubernetes `Secret` in k8s mode).
- They are never on the command line, in the image, in the run config, or in a log.

See [Security: secrets]({{ site.baseurl }}/orchestration/architecture/#security-secrets) for the details.

## Layout

```
orchestration/
  README.md                a short pointer to these pages
  pyproject.toml           the adapt-orchestration package (src layout) + [tool.dagster] module_name
  requirements.txt         dagster, dagster-webserver, duckdb, pyyaml
  config/
    pipelines/             the named pipelines, one DAG per file (node = canonical name, `after` = upstream nodes)
    networks.yaml          network -> ADaPT source, timezone, connectors, image, inputs, streams map (+ after overrides)
  demo.py                  live end-to-end demo
  docker/                  the adapt-pipeline:local image for ADAPT_EXECUTION=docker/k8s
  k8s/                     ADAPT_EXECUTION=k8s: setup.sh + in-cluster S3 (LocalStack) + DuckLake catalog (Postgres)
  localstack/              a LocalStack S3 helper (docker compose) for trying the s3 reader locally
  src/adapt/orchestration/ spec.py, sourcespec.py, accounts.py, context.py, wrappers.py, custom_wrappers.py,
                           runner/ (subprocess/docker/k8s), definitions.py (the Dagster jobs + trigger), settings.py
  tests/                   unittest suite (a stand-in adapt CLI for the graph/runner tests)
```

On GitHub: [config/](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration/config),
[src/adapt/orchestration/](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration/src/adapt/orchestration),
[docker/](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/orchestration/docker/README.md),
[k8s/](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration/k8s),
[localstack/](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/orchestration/localstack/README.md) and
[tests/](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration/tests).
