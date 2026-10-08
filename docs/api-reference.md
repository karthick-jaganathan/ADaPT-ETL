---
layout: default
title: API Reference
nav_order: 9
description: "The StreamWright commands and Python entry points"
permalink: /api-reference/
---

# API Reference

The commands of streamwright and its Python entry points. Every option of `streamwright run` (outputs, file names, logging,
the run summary) is described in the
[streamwright docs]({{ site.baseurl }}/core/cli/); the
source format in the
[design]({{ site.baseurl }}/design/source-format/).

## Commands

### `streamwright run SOURCE`

Runs a source folder (or its `source.yaml`), or a single-file source. Records go to stdout as Singer messages unless
`--output` names files or a warehouse; logs go to stderr.

| Option | Meaning |
|---|---|
| `--config FILE` | one client's settings: `config` values for `spec.config`, and the `streams` to run |
| `--set NAME=VALUE` | one `spec.config` value (repeatable; lists as `a,b,c`) |
| `--secrets FILE` | values for `spec.secrets`; overrides `STREAMWRIGHT_SECRET_<NAME>` environment variables |
| `--state FILE` | state from a previous run, for incremental streams |
| `--timezone TZ` | the client's IANA time zone for `today` and incremental windows |
| `--stream NAME` | run only this stream, or the stream of this export (repeatable) |
| `--output` | `singer` (default), `jsonl:DIR`, `csv:DIR`, `tsv:DIR`, `parquet:DIR`, `duckdb:PATH[:SCHEMA]`, `ducklake:CATALOG[:SCHEMA]`, `dlt:DESTINATION[:DATASET]` |
| `--file-name TEMPLATE` | with a file output: each export's file name |
| `--allow-connector NAME` | allow only these connectors (repeatable) |
| `--summary FILE` | also write the run summary as JSON |
| `--log-level`, `--log`, `--log-format`, `--log-config`, `--log-max-chars` | logging |

Exit status: `0` success, `1` the run failed, `2` invalid source, inputs or options, or a missing or disallowed connector
(nothing is fetched), `130` interrupted.

### `streamwright validate [PATH ...]`

Check sources without running them. A source folder is checked as one source; directories are searched recursively.
`streamwright validate` also runs the checks of the installed connectors and query builders.

| Option | Meaning |
|---|---|
| `--kind source` | the kind to assume for files that do not declare `kind` |
| `--allow-connector NAME` | only allow these connectors in `auth.provider` and `sdk` (repeatable) |
| `--strict` | exit with status 1 on warnings too |
| `--format text\|json\|github` | output format; `github` prints GitHub Actions annotations |
| `--export-schema DIR` | write `source.schema.json` and `stream.schema.json` to `DIR`, then exit |

Without a PATH, `$STREAMWRIGHT_CONFIGS` is checked. Exit status: `0` no errors, `1` errors (or warnings with `--strict`),
`2` usage error.

### `streamwright connectors`

Lists the installed connectors, with the names of their SDK loggers (for `--log`).

## Python

### Validating

```python
from streamwright.core.validation.engine import ERROR, validate_source, validate_paths

issues = validate_source("examples/sources/ads/google_ads")       # a source folder, its source.yaml, or a source file
errors = [issue for issue in issues if issue.severity == ERROR]
files, issues = validate_paths(["examples/sources"])           # files and directories, recursively
```

`streamwright.core.validation.engine`:

- `validate_source(path, kind=None, allowed_connectors=None, source_check=None)`, `validate_file(...)`,
  `validate_text(text, filename=None, ...)` return a list of `Issue`; `validate_paths(paths, ...)` returns
  `(files_checked, issues)`.
- `Issue` has `severity` (`"error"` or `"warning"`), `code`, `message`, `file`, `path`, `line` and `column`;
  `to_dict()` and `github_annotation()` format it.
- `export_schemas(directory)` writes the JSON Schemas; `main(argv=None)` is the `streamwright validate` command.

### Loading and running a source

```python
from streamwright.core.validation.engine import validate_source
from streamwright.core.outputs.output import SingerOutput
from streamwright.core.engine.runner import SourceRunner
from streamwright.core.config.loader import load_source

path = "examples/sources/readers/files_demo"                 # needs streamwright-files
assert not [issue for issue in validate_source(path) if issue.severity == "error"]
runner = SourceRunner(load_source(path), config={"data_root": path + "/data"}, secrets={},
                      output=SingerOutput())
state = runner.run()                                         # or run(["orders"]) for some streams
```

- `streamwright.core.config.loader.load_source(path)` returns the source as one mapping (a folder's stream files become its
  `streams`); `find_source(path)` returns its `SourceLayout`. Both raise `SourceFilesError` for a path that is not a
  source.
- `streamwright.core.engine.runner.SourceRunner(source, config, secrets, state=None, output=None, ...)`; `run(selected=None)` runs
  the selected streams (stream or export names; default: all) and returns the new state.
