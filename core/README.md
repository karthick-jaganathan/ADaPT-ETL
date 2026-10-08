# StreamWright — the `streamwright` CLI and runtime

**StreamWright turns a few YAML files into a working data pipeline.** You describe *what*
to pull — from an API, a file store or a database — and StreamWright handles the *how*:
signing in, paging, retries, shaping the data with DuckDB SQL, and writing it out.
No per-connector code, and one command runs any source.

The `streamwright` package is the heart of it: the `streamwright` command plus the runtime for
`kind: source` sources.

- **`streamwright run`** executes a source · **`streamwright validate`** checks one · **`streamwright connectors`** browses and installs connectors.
- A source is a folder — `source.yaml` (`spec`, `auth`, `http`) and one file per stream in `streams/` — or a single YAML file.
- Each stream **fetches** (HTTP requests / SDK connectors), **shapes** (sandboxed DuckDB SQL), and **writes** (files, DuckDB/DuckLake or dlt).

## Install

```bash
pip install streamwright
streamwright --help
```

This installs the `streamwright` CLI, the runtime and the connector hub — but no
connectors yet. Add the ones you need on demand:

```bash
streamwright connectors list                 # browse the hub
streamwright connectors install files        # a reader (files, s3, gcs, postgres)
streamwright connectors install google_ads   # an API connector (pulls in its SDK)
streamwright connectors install google_ads meta_ads microsoft_ads   # several at once (one pip install)
```

## Quickstart

```bash
pip install streamwright
streamwright connectors install files

# the runnable examples live in the repo — clone it to try one end-to-end
git clone https://github.com/karthick-jaganathan/streamwright.git
streamwright validate streamwright/examples
streamwright run streamwright/examples/sources/readers/files_demo \
  --set data_root=streamwright/examples/sources/readers/files_demo/data \
  --allow-connector files --output jsonl:out
```

📖 **Full documentation:** https://streamwright.web.app/docs/ — command line,
streams, outputs, logging, connectors & readers and the Python API.

- Architecture guide: https://streamwright.web.app/docs/architecture/
- Connectors & readers: https://streamwright.web.app/docs/connectors/
