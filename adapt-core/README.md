# adapt-core — the `adapt` CLI and runtime for `kind: source`

adapt-core is the `adapt` command — `adapt run` runs a source, `adapt validate` checks it, `adapt connectors` lists the
installed connectors — and the runtime behind it for `kind: source` sources: a source folder (`source.yaml` with
`spec`, `auth` and `http`, and one file per stream in `streams/`) or a single YAML file, with HTTP requests, SDK
[connectors](../connectors/README.md), sandboxed DuckDB SQL steps, and outputs to files, DuckDB/DuckLake or dlt.

## Install

```bash
make install            # from the repository root; or: cd adapt-core && make install MODE=dev
adapt --help
```

## Quickstart

```bash
pip install ./connectors/readers/files        # from the repository root
adapt validate examples
adapt run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data --allow-connector files --output jsonl:out
```

📖 Full documentation: https://karthick-jaganathan.github.io/ADaPT-ETL/adapt-core/ — command line, streams, outputs,
logging, connectors & readers and the Python API.

- [Architecture guide](https://karthick-jaganathan.github.io/ADaPT-ETL/architecture/) — how adapt-core, the connectors
  and orchestration fit together.
- [Source format design](https://karthick-jaganathan.github.io/ADaPT-ETL/design/source-format/) — the full
  `source.yaml` + `streams/` reference.
