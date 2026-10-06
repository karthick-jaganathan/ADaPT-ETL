---
layout: default
title: Architecture
parent: Orchestration
nav_order: 2
permalink: /orchestration/architecture/
---

# adapt.orchestration — architecture & design

How the orchestration subsystem works in depth. For how it fits the whole ADaPT system (with sequence diagrams) see
the [Architecture guide]({{ site.baseurl }}/architecture/); for day-to-day commands see
[Running]({{ site.baseurl }}/orchestration/running/).

- **Network-agnostic:** Google Ads is just one entry in `config/networks.yaml`.
- **Never imports adapt:** every node shells out to the `adapt` CLI (`$ADAPT_BIN`).
- **Package:** `adapt.orchestration` (distribution `adapt-orchestration`; `adapt` is a namespace package shared with
  adapt-core).

{: .note }
**Prototype stubs:** `config/networks.yaml` (the network registry) and `src/adapt/orchestration/accounts.py` (the
account lookup) are stand-ins. In production both come from a database / registry service.

## The model

Five parts work together:

| Part | Role |
|---|---|
| Pipelines (`config/pipelines/<name>.yaml`) | named DAGs (order) |
| Networks (`config/networks.yaml`) | what to run and with which inputs |
| Wrappers | per-node command building |
| Accounts | the lookup |
| Op graphs + trigger | one Dagster job per (pipeline, network), run per account |

### Pipelines: `config/pipelines/<name>.yaml` = named DAGs (order)

A pipeline is a named DAG; each file of `config/pipelines/` is one.

- Its `name:` must be its file name.
- Nodes are **canonical** node names; `after` lists upstream nodes (of the same pipeline).
- Validation rejects unknown node references, self-dependencies and cycles.
- The spec exposes `nodes`, `edges`, `order` (topological) and `upstream(node)`.

Two example pipelines:

```yaml
# config/pipelines/metadata.yaml - the account's entities (jobs ads_metadata__<network>)
name: metadata
nodes:
  campaigns:          {}
  ad_groups:          {after: [campaigns]}
  ad_group_hierarchy: {after: [campaigns]}
  location_targets:   {after: [campaigns]}
  keywords:           {after: [ad_groups]}
  audience_targets:   {after: [ad_groups]}

# config/pipelines/performance.yaml - the account's reports (jobs ads_performance__<network>)
name: performance
nodes:
  campaign_performance: {}
```

**To add a pipeline**, drop a `config/pipelines/<name>.yaml` with `name: <name>` and its nodes — no code change.

- It becomes the job `ads_<name>__<network>` for each network that supports it.
- `trigger("<name>", user_id)` runs it.
- A node may be in several pipelines: its op definition is named `<pipeline>__<network>__<node>`; the node name stays
  the step key.

**Selecting a pipeline:**

- `spec.pipeline_names()` lists the pipelines (`*.yaml` of `$ADAPT_PIPELINE_DIR`, default `config/pipelines/`).
- `spec.load_pipeline(name)` loads one.
- `$ADAPT_PIPELINE_SPEC` — a path to one pipeline file — replaces the folder.

**Resolving it on a network:** when a pipeline is triggered, it is resolved onto each account's network
(`spec.resolve_pipeline`):

- every node must be mapped by that network's `streams:` — else the trigger raises before anything runs, naming the
  node;
- a node mapped to a stream name runs under that name (an alias);
- a node mapped `false` is skipped.

### Shared vocabulary: one pipeline across networks that name streams differently

Pipelines use **canonical node names**. Each network's `streams:` map (in `config/networks.yaml`) maps every canonical
node to:

- **its own source stream name** (an alias), or
- **`false`**: the network has no such stream, so the node is skipped for it.

So the single `metadata` pipeline runs on all of google_ads, microsoft_ads and facebook_ads, even though they name (or
lack) streams differently:

| canonical node | google_ads | microsoft_ads | facebook_ads |
|---|---|---|---|
| campaigns | campaigns | campaigns | campaigns |
| ad_groups | ad_groups | ad_groups | `ad_sets` (alias) |
| ad_group_hierarchy | ad_group_hierarchy | `ad_group_tree` (alias) | `false` (skipped) |
| keywords / audience_targets / location_targets | ✓ | ✓ | `false` (skipped) |
| campaign_performance | campaign_performance | campaign_performance | `campaign_insights` (alias) |

- **Explicit false:** only a `false` skips a node. An *unmapped* node is an error, so a typo is caught.
- **Edge overrides:** a network may override the dependency edges with `after:` (node → upstream nodes) when its
  parentage differs. E.g. facebook's `ad_groups` (ad sets) are fetched per account independently of campaigns, so
  facebook sets `after: {ad_groups: []}`.
- **Skipped nodes:** a skipped node drops out of other nodes' dependencies automatically.
- **One job per network:** each (pipeline, network) becomes its own Dagster job `ads_<pipeline>__<network>`;
  `trigger` picks the job for each account's network.

### Networks: `config/networks.yaml` = what to run and with which inputs

A network names:

- its source folder (relative to the repo root);
- its default `timezone`;
- the `connectors` the run may load (`--allow-connector`);
- the container `image`;
- its `streams:` map (and optional `after:` overrides, above);
- `inputs`: one value per input the source declares.

Each input is a literal, or a small template over:

| Reference | Value |
|---|---|
| {% raw %}`{{ row.NAME }}`{% endraw %} | the account row |
| {% raw %}`{{ app.NAME }}`{% endraw %} | app-level values/secrets |
| {% raw %}`{{ ctx.NAME }}`{% endraw %} | user_id, account_id, network, timezone, schema |

A reference that is not set leaves the input unset (e.g. `login_customer_id` for an account without a manager).

### Wrappers: per-node command building

A wrapper is `wrapper(node, ctx) -> (argv, secret_env)`.

#### `default_wrapper`

`default_wrapper` derives the parameters from the **source's declared spec**.

- `sourcespec` reads `source.yaml`'s `spec.config` and `spec.secrets`.
- For each declared input, the value comes from the network's `inputs` (rendered over row/app/ctx) or is left to the
  source's own default.
- These are errors:
  - a **required input with no value**;
  - an `inputs` key the source does not declare;
  - a node whose resolved stream is not a stream of the source.
- Config inputs become `--set name=value`.
- Secret inputs become `secret_env["ADAPT_SECRET_<NAME>"]`.

The argv, with warehouse `warehouse/<user>.duckdb` and schema `<network>_<account>`:
`adapt run <source> --stream <stream> --set ... --timezone <tz> --output duckdb:<warehouse>:<schema> --allow-connector ...`

#### Custom wrappers: `@node`

- `@node(name, network=None)` registers a **custom wrapper** for a node (on one network, or any).
- The framework calls the registered wrapper for (node, network), else (node, any), else the default.
- A custom wrapper typically does its own lookups (a DB, `ctx.upstream` — the upstream ops' results).
- It then calls `default_wrapper(node, ctx, overrides={...})`, so its extra values are still checked against the
  source's spec.

Example: `campaigns_from_db` (campaigns on google_ads) looks the account's campaign ids up in a stub campaigns DB
(`CAMPAIGN_DB`) and adds `--set campaign_ids=1,2,3`. An account not in the DB gets the default (unfiltered) command.

### Accounts: the lookup

- `accounts.ACCOUNTS` is a list of rows `{user_id, account_id, network, ...}`.
- In production it is a query against the accounts database with the same row shape.
- `app_config(network)` loads app-level values/secrets at run time — for google_ads from `~/.adapt/google-secrets.yaml`
  (`$ADAPT_APP_CONFIG_GOOGLE_ADS` overrides the path).
- The per-user OAuth refresh token:
  - in production comes from the DB row ({% raw %}`refresh_token: "secret:{{ row.refresh_token }}"`{% endraw %});
  - in this prototype is `app.refresh_token` from the secrets file.

### Op graphs + trigger

`definitions.py` builds one Dagster job per (pipeline, network), `ads_<pipeline>__<network>`.

**Jobs**

- `definitions.JOBS` is keyed by `(pipeline, network)`; all jobs are in `defs`, each tagged `pipeline:`/`network:`.
- A job has one op per kept node, wired with the resolved edges.
- An upstream op's result dict is an input of its downstream ops, and reaches the wrapper as `ctx.upstream`.

**Ops**

- Every op takes the same run config `{user_id, account_id, network}` (the job's config mapping fans it out).
- An op builds its Context, calls its wrapper (whose `--stream` is the network's resolved stream) and runs the command.
- It returns `{status, records, streams, command, wrapper, ...}`.

**`trigger(pipeline, user_id, account_id=None)`**

1. Finds the matching account rows.
2. Resolves the pipeline on each row's network.
3. For each row, runs `ads_<pipeline>__<network>.execute_in_process(...)` — the whole graph in dependency order in
   one call (the prototype's executor; one DuckDB writer at a time).
4. Returns one `{pipeline, job, network, run_config, run_id, success, nodes}` per account row.

When `$DAGSTER_HOME` is set, the runs are recorded there, so `dagster dev` (pointed at the same `$DAGSTER_HOME`) shows
them in its UI.

## Execution modes: mechanics

`ADAPT_EXECUTION` selects where each node's `adapt run` executes. The op graph, dependency order and run config are
identical across modes — only *where* `adapt run` runs changes.

### `subprocess` (default)

The local adapt CLI `$ADAPT_BIN`, writing a DuckDB file under `warehouse/`.

### `docker`

A `docker run --rm` container of the network's `image:` (`$ADAPT_IMAGE` overrides it).

- The wrapper builds the same logical argv. The runner translates it:
  - source → `/app/examples/sources/...`;
  - `--output duckdb:/warehouse/...`;
  - `--summary /runs/...`;
  - with the warehouse and runs folders mounted.
- Each secret is passed as `-e ADAPT_SECRET_<NAME>` **without a value**.

See [docker/README.md](https://github.com/karthick-jaganathan/ADaPT-ETL/blob/master/orchestration/docker/README.md).

### `k8s`

A Kubernetes Job per node, created with `kubectl`.

- The Job: one pod, `backoffLimit: 0`, `restartPolicy: Never`, `imagePullPolicy: Never`.
- Context `$ADAPT_K8S_CONTEXT` (default `kind-adapt`); namespace `$ADAPT_K8S_NAMESPACE` (default `adapt`).
- The pod runs
  `adapt run /app/examples/sources/<source> --stream <stream> ... --output "ducklake:postgres:dbname=adaptcat host=catalog-postgres.adapt.svc.cluster.local port=5432 user=adapt:<network>_<account>"`.
- The warehouse is a **DuckLake**: Parquet data on S3 under `s3://adapt-warehouse/<user>/`, the catalog in Postgres
  (schema `lake_<user>`).

The op:

1. checks the Secret has every key the pod needs (by name);
2. creates the Job;
3. waits for the pod;
4. streams its log into the Dagster log (redacted);
5. waits for the Job;
6. reads the record counts from adapt's log lines;
7. deletes the Job, and fails the op if the Job failed.

A downstream node's Job is created only after its upstream Jobs completed (the op graph is unchanged). See
[k8s/](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/orchestration/k8s).

Settings (`adapt.orchestration.K8S_SETTINGS`):

| Variable | Notes |
|---|---|
| `ADAPT_K8S_SECRET` | adapt-secrets |
| `ADAPT_K8S_CATALOG` | the DSN, no password |
| `ADAPT_K8S_DATA_ROOT` | `s3://adapt-warehouse` |
| `ADAPT_K8S_S3_ENDPOINT` / `_URL_STYLE` / `_USE_SSL` / `_REGION` | the S3 settings |
| `ADAPT_K8S_TIMEOUT` | the Job's `activeDeadlineSeconds`, 1800 |
| `ADAPT_K8S_START_TIMEOUT` | 300 |
| `ADAPT_K8S_KEEP_JOBS=1` | keep finished Jobs for inspection; their TTL removes them after an hour |

The Kubernetes path is a local simulation (a `kind` cluster with in-cluster LocalStack + Postgres) of a real EKS
deployment.

## Security: secrets

Secrets are stored nowhere in the pipeline: they are resolved inside the op and reach adapt only as environment
variables.

### Nothing stored

- No secret value is in `config/networks.yaml`, `config/pipelines/*.yaml`, code or run config.
- `inputs` only reference where a value comes from ({% raw %}`secret:{{ app.developer_token }}`{% endraw %}).
- The run config is `{user_id, account_id, network}` only.
- A `secret:` value for a config input is rejected (it would end up on the command line).

### Resolved at execution time

- Secrets are resolved at execution time, inside the op (`app_config` / the account row).
- They are passed to adapt only as `ADAPT_SECRET_<NAME>` variables in the child process' environment — never `--set`,
  never `--secrets`, never argv.

### Per execution mode

- **docker:** the child is `docker run`, given `-e ADAPT_SECRET_<NAME>` by name only, so docker copies the value from
  its environment into the container. No value is in the docker argv or the image.
- **k8s:** the pipeline sends no secret value at all.
  - Each Job pod gets `envFrom` the Kubernetes Secret `adapt-secrets` (the source's `ADAPT_SECRET_*` plus the S3
    credentials and the catalog password).
  - `k8s/setup.sh` creates it at setup time through a pipe to `kubectl apply --server-side` (no argv, no file, no
    last-applied annotation).
  - The Job manifest holds only the Secret's name and non-secret settings; the runner refuses to build one containing
    a secret value.

### Logs and outputs

- The logged command and the op outputs are secret-free (`default_wrapper` refuses to build an argv containing a
  secret value).
- adapt's output is redacted (`***`) for the run's secret values before it reaches the log.
- Error messages name the missing input, never a value.
- `Context.app` and `Context.row` are excluded from its repr.
