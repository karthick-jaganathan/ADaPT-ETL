# adapt-core — the `adapt` CLI and runtime

**ADaPT turns a few YAML files into a working data pipeline.** You describe *what*
to pull — from an API, a file store or a database — and ADaPT handles the *how*:
signing in, paging, retries, shaping the data with DuckDB SQL, and writing it out.
No per-connector code, and one command runs any source.

`adapt-core` is the heart of it: the `adapt` command plus the runtime for
`kind: source` sources.

- **`adapt run`** executes a source · **`adapt validate`** checks one · **`adapt connectors`** browses and installs connectors.
- A source is a folder — `source.yaml` (`spec`, `auth`, `http`) and one file per stream in `streams/` — or a single YAML file.
- Each stream **fetches** (HTTP requests / SDK connectors), **shapes** (sandboxed DuckDB SQL), and **writes** (files, DuckDB/DuckLake or dlt).

## Install

```bash
pip install adapt-core
adapt --help
```

This installs the `adapt` CLI, the runtime and the connector hub — but no
connectors yet. Add the ones you need on demand:

```bash
adapt connectors list                 # browse the hub
adapt connectors install files        # a reader (files, s3, gcs, postgres)
adapt connectors install google_ads   # an API connector (pulls in its SDK)
```

## Quickstart

```bash
pip install adapt-core
adapt connectors install files

# the runnable examples live in the repo — clone it to try one end-to-end
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
adapt validate ADaPT-ETL/examples
adapt run ADaPT-ETL/examples/sources/readers/files_demo \
  --set data_root=ADaPT-ETL/examples/sources/readers/files_demo/data \
  --allow-connector files --output jsonl:out
```

📖 **Full documentation:** https://adapt-9e96d.web.app/docs/ — command line,
streams, outputs, logging, connectors & readers and the Python API.

- Architecture guide: https://adapt-9e96d.web.app/docs/architecture/
- Connectors & readers: https://adapt-9e96d.web.app/docs/connectors/
