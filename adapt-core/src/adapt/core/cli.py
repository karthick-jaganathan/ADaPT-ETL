#!/usr/bin/env python
# /*************************************************************************
# * Copyright 2025 Karthick Jaganathan
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# * https://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
# **************************************************************************/

"""
The `adapt` command.

  adapt run SOURCE [--config FILE] [--set NAME=VALUE] [--secrets FILE] [--state FILE] [--stream NAME] [--output ...]
            [--file-name TEMPLATE] [--allow-connector NAME] [--summary FILE] [logging options]
            (SOURCE: a source folder - source.yaml and streams/ - or a source file)
  adapt connectors              (installed connectors, with the names of their SDK loggers)
  adapt validate PATH...        (adapt-validate, plus the checks of the installed connectors and query builders)

Logging options (adapt run and adapt validate): --log-level LEVEL, --log NAME=LEVEL, --log-format text|json,
--log-config FILE, --log-max-chars N. Loggers: adapt.source (progress, summaries, warnings), adapt.network (INFO: a
line per API call; DEBUG: headers and bodies; WARNING by default), adapt.output (what was written), and the SDKs' own
loggers by their names. Logs go to stderr (stdout is for Singer messages), every line of every logger redacted:
secrets and the tokens a run obtains are ***.

Exit status: 0 success, 1 the run failed, 2 invalid source or inputs.
"""

import argparse
import datetime
import json
import logging
import os
import sys
import zoneinfo

import yaml

from adapt.core.runtime import components, logs
from adapt.core.engine import sql
from adapt.core.net.http import Redactor
from adapt.core.config.inputs import (InputError, read_config_file, read_values_file, resolve_inputs, secrets_from_env,
                                 SECRET_ENV_PREFIX)
from adapt.core.outputs.output import FILE_FORMATS, FILE_NAME_OUTPUTS, export_files, open_output
from adapt.core.engine.runner import SourceRunner
from adapt.core.validation import engine, schema
from adapt.core.config import loader


__all__ = ["main"]

LOG = logging.getLogger("adapt.source")


class _RedactFilter(logging.Filter):
    """Redacts secrets from log records - messages, tracebacks (with chained exceptions) and other libraries' logs."""

    def __init__(self, redact):
        super(_RedactFilter, self).__init__()
        self.redact = redact

    def filter(self, record):
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        if record.exc_info and not record.exc_text:
            # formatters reuse exc_text, so the redacted traceback is what gets written
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def _discard(output):
    """Throws away a failed run's output; a failure here must not hide the run's own error."""
    try:
        output.close(failed=True)
    except Exception as exc:
        LOG.warning("could not discard the output: %s", exc)


def _locate(document, path):
    """(container, key) of the deepest part of `path` in the document: where a finding is shown (file and line)."""
    container, key, node = None, None, document
    for part in path:
        if isinstance(node, dict) and part in node or \
                isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            container, key, node = node, part, node[part]
        else:
            break
    return container, key


def _component_checks(checker, source):
    """
    adapt validate: what `adapt run` checks before it starts, against the installed connectors and query builders (every
    request of every stream), and the streams' SQL steps (compiled by DuckDB: sql.check_source).
    """
    for path, message, missing in components.component_problems(source, checker.options.allowed_connectors):
        container, key = _locate(source, path)
        if missing:
            checker.warn("connector-not-installed", path, "%s; the source is not checked against it" % message,
                         container, key)
        else:
            checker.error("connector-check", path, message, container, key)
    for path, message in sql.check_source(source):
        container, key = _locate(source, path)
        checker.error("sql-check", path, message, container, key)


def _key_value(text):
    if "=" not in text:
        raise argparse.ArgumentTypeError("expected NAME=VALUE, got %r" % text)
    name, _, value = text.partition("=")
    return name.strip(), value


def _max_chars(text):
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError("expected a number of characters (0: no limit), got %r" % text)
    return value


def _logger_level(text):
    name, _, level = text.partition("=")
    name = name.strip()
    try:
        number = logs.level_number(level) if name and level.strip() else None
    except ValueError:
        number = None
    if number is None:
        raise argparse.ArgumentTypeError("expected NAME=LEVEL, a logger (e.g. adapt.network, or root) and a level "
                                         "(%s), got %r" % (", ".join(logs.LEVELS), text))
    return name, number


def _add_log_options(parser):
    """The logging options of adapt run and adapt validate."""
    parser.add_argument("--log-level", type=str.upper, choices=logs.LEVELS, metavar="LEVEL",
                        help="the level of adapt's loggers (adapt.source, adapt.output, ...): %s; default INFO. "
                             "adapt.network stays at WARNING (or this level, when higher) unless --log names it"
                             % ", ".join(logs.LEVELS))
    parser.add_argument("--log", action="append", default=[], type=_logger_level, metavar="NAME=LEVEL",
                        help="the level of any logger by its name (repeatable; root: the root logger, so every "
                             "logger without a level of its own), e.g. adapt.network=INFO (a line per API call), "
                             "adapt.network=DEBUG (also headers and bodies), google.ads.googleads.client=DEBUG (an "
                             "SDK's own logs: adapt connectors lists the connectors' SDK loggers)")
    parser.add_argument("--log-format", choices=logs.FORMATS,
                        help="text (default): [time] LEVEL logger: message; json: one JSON object per line with time "
                             "(UTC), level, logger, message and the line's fields (stream, partition, window, "
                             "request, export, records, pages, duration_ms, status, attempt, bytes, path, table, ...)")
    parser.add_argument("--log-config", metavar="FILE",
                        help="a Python logging configuration (logging.config.dictConfig's schema, version: 1) in YAML "
                             "or JSON, for handlers and formatters of your own (files, syslog, ...); it replaces "
                             "adapt's handler, and --log-level and --log apply on top. Every handler's lines are "
                             "redacted and cut too. JSON lines: a formatter with \"()\": "
                             "adapt.core.runtime.logs.JsonFormatter")
    parser.add_argument("--log-max-chars", type=_max_chars, default=logs.DEFAULT_MAX_CHARS, metavar="N",
                        help="cut log messages longer than N characters (bodies, SDK payloads), marking them "
                             "`... [truncated N chars]` (default %d; 0: never)" % logs.DEFAULT_MAX_CHARS)


def _log_options():
    """The logging options `adapt validate` takes, before or after adapt-validate's own."""
    parser = argparse.ArgumentParser(prog="adapt validate", add_help=False, allow_abbrev=False)
    _add_log_options(parser)
    return parser


def _validate_parser():
    """`adapt validate --help`: adapt-validate's options, then the logging options (as adapt run's)."""
    parser = engine._parser("adapt validate")
    _add_log_options(parser.add_argument_group("logging options"))
    return parser


def _parser():
    parser = argparse.ArgumentParser(prog="adapt", description="Run and check ADaPT sources (`kind: source`).")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    run = commands.add_parser("run", help="run a source and write its records",
                              description="Run a source. Records go to stdout as Singer messages unless --output "
                                          "names a folder for files or a warehouse; logs go to stderr.")
    run.add_argument("source", metavar="SOURCE",
                     help="a source folder (source.yaml and one file per stream in streams/) or a source file")
    run.add_argument("--config", metavar="FILE",
                     help="YAML/JSON file with `config` (values for spec.config) and `streams` (streams/exports to "
                          "run), e.g. one client's settings for the source")
    run.add_argument("--set", action="append", default=[], type=_key_value, metavar="NAME=VALUE",
                     help="a spec.config value (repeatable; lists as a,b,c); overrides --config")
    run.add_argument("--secrets", metavar="FILE",
                     help="YAML/JSON file with values for spec.secrets; overrides %s<NAME> environment variables"
                          % SECRET_ENV_PREFIX)
    run.add_argument("--state", metavar="FILE", help="state from a previous run (a JSON file) for incremental streams")
    run.add_argument("--timezone", metavar="TZ",
                     help="the client's IANA time zone (e.g. America/New_York) for `today` and incremental windows, "
                          "so a daily run reads the client's local day; default: the machine's local date")
    run.add_argument("--stream", action="append", dest="streams", metavar="NAME",
                     help="run only this stream, or the stream of this export (repeatable; replaces the --config "
                          "file's `streams`); the streams its `from_stream` partitions come from run too, and every "
                          "stream that runs writes all its exports")
    run.add_argument("--output", default="singer", metavar="singer|jsonl|csv|tsv|parquet|dlt|duckdb|ducklake",
                     help="singer (default): messages on stdout; jsonl:DIR, csv:DIR, tsv:DIR or parquet:DIR: one file "
                          "per export plus state.json, written atomically (parquet: typed columns, zstd); "
                          "dlt:DESTINATION[:DATASET]: load into a dlt destination (e.g. dlt:duckdb, "
                          "dlt:bigquery:marketing), which also keeps the state; duckdb:PATH[:SCHEMA] or "
                          "ducklake:CATALOG[:SCHEMA]: load into a DuckDB file or DuckLake catalog (a file, or "
                          "postgres:DSN for a Postgres catalog; data files on S3 with ADAPT_DUCKLAKE_DATA_PATH)")
    run.add_argument("--file-name", metavar="TEMPLATE",
                     help="with --output jsonl:DIR, csv:DIR, tsv:DIR or parquet:DIR: the path of each export's file "
                          "inside DIR, with {{ export }}, {{ source }}, {{ today }} (YYYY-MM-DD), {{ timestamp }} "
                          "(the run's start, UTC, YYYYMMDDTHHMMSSZ) and {{ config.NAME }}, e.g. "
                          "\"{{ config.client }}/{{ export }}_{{ today }}.jsonl\"; subfolders are made, every export "
                          "needs its own path, and a file of the same name is replaced (default: "
                          "EXPORT.TIMESTAMP.UNIQUE.jsonl, or .csv, .tsv, .parquet)")
    run.add_argument("--allow-connector", action="append", dest="allowed_connectors", metavar="NAME",
                     help="allow only this connector (repeatable); by default any installed connector can be used")
    run.add_argument("--summary", metavar="FILE",
                     help="also write the run's summary as JSON to FILE, atomically, whatever the outcome: status "
                          "(ok or failed), times, the streams' counts (partitions, windows, pages, requests, retries, "
                          "records read and written), the outputs written, the state's bookmarks and the error")
    _add_log_options(run)

    commands.add_parser("connectors",
                        help="list the installed connectors, with the names of their SDK loggers (for --log)")

    validate = commands.add_parser("validate", help="check configuration files: adapt-validate, plus the checks of "
                                                     "the installed connectors and query builders", add_help=False)
    validate.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _load_state(path):
    if not path:
        return None
    if not os.path.exists(path):
        LOG.warning("state file %s does not exist; starting from each stream's `start`", path)
        return None
    with open(path, "r", encoding="utf-8") as stream:
        state = json.load(stream)
    # accept a Singer STATE message as well as its value
    return state.get("value", state) if state.get("type") == "STATE" else state


def _summary_problem(path):
    """Why --summary FILE cannot be written (checked before the run starts), or None."""
    if not path:
        return None
    if os.path.isdir(path):
        return "--summary %s is a folder" % path
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        return "--summary %s: the folder %s does not exist" % (path, folder)
    return None


def _summarized(output):
    """The output's summary() entries: what it wrote (none for outputs that write nothing when a run fails)."""
    summary = getattr(output, "summary", None)
    if summary is None:
        return []
    try:
        return list(summary())
    except Exception as exc:  # the run's own outcome matters more
        LOG.warning("could not summarize the output: %s", exc)
        return []


def run(args, logging_setup=None):
    """
    adapt run. Whatever the outcome, a run that started logs its end (RunMetrics.finish), and --summary FILE is
    written, with the error of a failed or interrupted run: the errors _run reports (_failed) - not log lines, which
    the loggers' levels and handlers can drop - redacted and cut as log lines are. `logging_setup`: the command's
    logs.LogSetup, which gets the run's Redactor and the network loggers of the source's connector.
    """
    problem = _summary_problem(getattr(args, "summary", None))
    if problem:
        LOG.error("%s", problem)
        return 2
    redact = Redactor()
    if logging_setup is not None:  # every log line from now on, as the secrets become known
        logging_setup.redact = redact
    metrics = logs.RunMetrics()
    errors = []  # the run's errors (_failed), for the summary
    code = None
    try:
        code = _run(args, metrics, redact, logging_setup, errors)
    except BaseException as exc:
        if metrics.status is None:  # it stopped before the run did
            metrics.status = "failed" if isinstance(exc, Exception) else "interrupted"
            if isinstance(exc, Exception):
                errors.append("%s: %s" % (type(exc).__name__, exc))
        raise
    finally:
        if getattr(args, "summary", None):
            shown = logging_setup.message if logging_setup is not None else \
                (lambda text: logs.mask_text(redact(text)))
            messages = []
            for message in errors:
                message = shown(redact(message))
                if message not in messages:
                    messages.append(message)
            metrics.error = "; ".join(messages) or None
            try:
                logs.write_summary(args.summary, metrics.summary())
            except (OSError, TypeError, ValueError) as exc:
                LOG.error("could not write the summary %s: %s", args.summary, exc)
                code = 1 if code == 0 else code
    return code


def _failed(errors, message, *args):
    """Logs an error of `adapt run` and keeps it in `errors`, for the run summary."""
    LOG.error(message, *args)
    errors.append(message % args if args else message)


def _today_in_zone(started, timezone):
    """The client's local date (for `today` and relative dates/windows): --timezone (an IANA name) or the machine's."""
    try:
        zone = zoneinfo.ZoneInfo(timezone) if timezone else None
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise InputError("unknown --timezone %r: use an IANA name such as America/New_York" % timezone)
    return started.astimezone(zone).date() if zone else datetime.date.today()


def _resolve_config_and_secrets(spec, provided_config, provided_secrets, today):
    """(config, secrets) resolved from the spec's declared inputs, reporting every section's problems together."""
    problems, values = [], {}
    for section, label, provided in (("config", "config", provided_config),
                                     ("secrets", "secret", provided_secrets)):
        try:
            values[section] = resolve_inputs(spec.get(section), provided, label, today=today,
                                             hide_values=section == "secrets")
        except InputError as exc:
            problems.append(str(exc))
    if problems:
        raise InputError("; ".join(problems))
    # secrets from files and Kubernetes mounts often end with a newline
    secrets = dict((k, v.strip() if isinstance(v, str) else v) for k, v in values["secrets"].items())
    return values["config"], secrets


def _run(args, metrics, redact, logging_setup, errors):
    try:
        layout = loader.find_source(args.source)
    except loader.SourceFilesError as exc:
        _failed(errors, "%s", exc)
        return 2
    name = layout.stream_of(args.source)
    if os.path.isfile(args.source) and name is not None:
        _failed(errors, "%s is a stream of the source folder %s; run the folder: adapt run %s --stream %s",
                args.source, layout.folder, layout.folder, name)
        return 2
    issues = engine.validate_source(layout, kind=schema.KIND, allowed_connectors=args.allowed_connectors)
    for issue in issues:
        if issue.severity == engine.ERROR:
            _failed(errors, "%s", issue)
        else:
            LOG.warning("%s", issue)
    if any(issue.severity == engine.ERROR for issue in issues):
        _failed(errors, "%s is not a valid source; fix the errors above (adapt validate %s)", args.source,
                args.source)
        return 2
    try:
        source = loader.load_source(layout)
        metrics.source = source.get("name")
        if source.get("kind") != schema.KIND:
            _failed(errors, "%s is not a `kind: source` file", args.source)
            return 2
        # every stream's SQL is compiled (and its connectors and query builders checked) before any request
        plan = sql.plan_source(source)
        problems = components.check_source(source, args.allowed_connectors) + plan.messages()
        if problems:
            raise InputError("; ".join(problems))
        spec = source.get("spec") or {}
        provided_config, file_streams = read_config_file(args.config) if args.config else ({}, None)
        provided_config.update(dict(args.set))
        selected = args.streams or file_streams
        names = [s["name"] for s in source["streams"]]
        exports = [export for stream in source["streams"] for export in sql.exports_of(stream)]
        unknown = [name for name in selected or () if name not in names and name not in exports]
        if unknown:
            raise InputError("unknown stream(s): %s (streams/exports: %s)" % (
                ", ".join(unknown), ", ".join(sorted(set(names) | set(exports)))))
        provided_secrets = secrets_from_env(list((spec.get("secrets") or {}).keys()))
        if args.secrets:
            provided_secrets.update(read_values_file(args.secrets, warn=LOG.warning))
        for value in provided_secrets.values():
            redact.add(value)
        started = datetime.datetime.now(datetime.timezone.utc)
        today = _today_in_zone(started, args.timezone)
        config, secrets = _resolve_config_and_secrets(spec, provided_config, provided_secrets, today)
        for value in secrets.values():
            redact.add(value)
        file_names = None
        if args.file_name is not None:
            kind, _, directory = args.output.partition(":")
            if kind not in FILE_FORMATS or not directory:
                raise InputError(FILE_NAME_OUTPUTS)
            # the selected streams run with their `from_stream` parents, and all of them write their exports
            running = plan.closure(selected or names)
            file_names = export_files(args.file_name, [export for name in running
                                                       for export in sql.exports_of(plan.streams[name].stream)],
                                      {"source": source["name"], "today": today, "config": config,
                                       "timestamp": started.strftime("%Y%m%dT%H%M%SZ")}, directory)
        state = _load_state(args.state)
        output = open_output(args.output, source=source, file_names=file_names)
    except (InputError, ValueError, yaml.YAMLError, OSError) as exc:
        _failed(errors, "%s", exc)
        return 2

    log_filter = _RedactFilter(redact)
    handlers = list(logging.getLogger().handlers)
    for handler in handlers:
        handler.addFilter(log_filter)
    closing = False
    try:
        if args.state is None and hasattr(output, "initial_state"):  # e.g. dlt keeps the state in the destination
            state = output.initial_state()
        runner = SourceRunner(source, config, secrets, state=state, output=output, today=today, redact=redact,
                              allowed_connectors=args.allowed_connectors, metrics=metrics)
        runner.run(selected)
        closing = True  # closing writes the files or loads the data, and can fail too
        output.close()
        metrics.outputs = _summarized(output)
        metrics.finish("ok")
    except KeyboardInterrupt:
        if not closing:
            _discard(output)
        metrics.outputs = _summarized(output)
        _failed(errors, "interrupted")
        metrics.finish("interrupted")
        return 130
    except Exception as exc:  # report every failure (redacted) instead of a traceback with secrets
        if not closing:
            _discard(output)
        metrics.outputs = _summarized(output)
        _failed(errors, "%s", redact(exc))
        LOG.debug("details", exc_info=True)
        metrics.finish("failed")
        return 1
    except BaseException:
        if not closing:
            _discard(output)
        metrics.outputs = _summarized(output)
        metrics.finish("interrupted")
        raise
    finally:
        for handler in handlers:
            handler.removeFilter(log_filter)
    return 0


def _read_log_config(path):
    """A --log-config file: a logging.config.dictConfig mapping, in YAML or JSON."""
    with open(path, "r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("expected a mapping in logging.config.dictConfig's schema (version: 1, handlers, ...)")
    return config


def _log_setup(options):
    """The command's logs.LogSetup from its logging options; None when they cannot be used (reported on stderr)."""
    if options.log_config and options.log_format:
        return _log_error("--log-format is the format of adapt's own handler, which --log-config replaces: set the "
                          "formatters in the config (JSON lines: \"()\": adapt.core.runtime.logs.JsonFormatter)")
    try:
        config = _read_log_config(options.log_config) if options.log_config else None
        return logs.LogSetup(options.log_level, options.log_format or "text", options.log, config,
                             options.log_max_chars)
    except (OSError, ValueError, TypeError, AttributeError, ImportError, yaml.YAMLError) as exc:
        # (what reading the file and logging.config report; its errors name the part, their causes what is wrong)
        cause = " (%s)" % exc.__cause__ if exc.__cause__ is not None else ""
        return _log_error("%s%s%s" % ("--log-config %s: " % options.log_config if options.log_config else "", exc,
                                      cause))


def _log_error(message):
    sys.stderr.write("adapt: error: %s\n" % message)  # (logging is not set up)
    return None


def _connector_lines():
    """`adapt connectors`: installed connectors grouped by category, each with its description and SDK loggers."""
    groups = {}
    for name in components.available():
        try:
            connector = components.load(name)
        except components.ComponentLoadError as exc:
            groups.setdefault("other", []).append("  %s — %s" % (name, exc))
            continue
        loggers = getattr(connector, "network_loggers", None) or ()
        loggers = (loggers,) if isinstance(loggers, str) else tuple(loggers)
        parts = [name]
        summary = getattr(connector, "summary", "") or ""
        if summary:
            parts.append("— " + summary)
        if loggers:
            parts.append("(SDK loggers: %s)" % ", ".join(loggers))
        category = getattr(connector, "category", "other") or "other"
        groups.setdefault(category, []).append("  " + " ".join(parts))
    lines = []
    for category in sorted(groups):
        lines.append("%s:" % category)
        lines.extend(sorted(groups[category]))
    return lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["validate"]:  # every other argument is adapt-validate's, including options before the paths
        options, rest = _log_options().parse_known_args(argv[1:])
        if any(argument in ("-h", "--help") for argument in rest):
            _validate_parser().print_help()
            return 0
        setup = _log_setup(options)
        if setup is None:
            return 2
        try:
            return engine.main(rest, source_check=_component_checks, prog="adapt validate",
                               non_strict_codes=("connector-not-installed",))
        finally:
            setup.close()
    args = _parser().parse_args(argv)
    if args.command == "connectors":
        lines = _connector_lines()
        print("\n".join(lines) if lines else "no connectors installed (e.g. pip install adapt-google-ads)")
        return 0
    setup = _log_setup(args)
    if setup is None:
        return 2
    try:
        return run(args, setup)
    finally:
        setup.close()


if __name__ == "__main__":
    sys.exit(main())
