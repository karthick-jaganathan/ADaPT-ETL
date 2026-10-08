import contextlib
import datetime
import io
import json
import logging
import os
import re
import subprocess
import sys
import time

import pytest

from streamwright.core import cli
from streamwright.core.runtime import components, logs
from streamwright.core.net.http import Redactor, error_excerpt
from streamwright.core.runtime.logs import LogSetup, RunMetrics
from streamwright.core.runtime.components import Connector, ConnectorContext
from streamwright.core.engine.runner import SourceError, SourceRunner, _summary as job_summary
from streamwright.core.runtime.testing import FakeClock, MemoryOutput, page_stream

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TODAY = datetime.date(2026, 10, 3)
ID = "SELECT (record->>'id')::BIGINT AS id FROM raw_items"
SECRETS = {"client_secret": "client-secret-xyz", "refresh_token": "refresh-token-abc"}
# what the fake OAuth server hands out while a run goes: none of it may ever be logged
TOKENS = ("tok-live-1", "tok-live-2", "rotated-refresh-77", "id-token-55")
TEXT_LINE = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}\] (DEBUG|INFO|WARNING|ERROR) [\w.]+: ")
NETWORK_INFO = ["--log", "streamwright.network=INFO"]
NETWORK_DEBUG = ["--log", "streamwright.network=DEBUG"]


# * -------
# * helpers
# * -------

def items_stream(name="items", paginator=None, params=None, **stream):
    """A page-mode stream: GET /items (request `raw_items`, records at `data`), keyed on id."""
    request = {"name": "raw_items", "http": {"path": "/items", "params": dict(params or {})}}
    if paginator:
        request["paginator"] = paginator
    return page_stream(name, request, ID, records={"path": "data"}, primary_key=["id"], **stream)


OFFSETS = {"type": "offset", "offset_param": "o", "limit_param": "l", "page_size": 2}


def http_source(api, *streams, **http):
    return {"kind": "source", "name": "demo",
            "http": dict({"base_url": api.url, "retry": {"codes": [429, 500], "max_attempts": 3, "max_delay": "0s"}},
                         **http),
            "streams": list(streams) or [items_stream()]}


def oauth_source(api, *streams):
    """http_source with OAuth refresh tokens: POST /token on the fake API hands out the TOKENS."""
    source = http_source(api, *streams)
    source["auth"] = {"type": "oauth2_refresh_token", "token_url": api.url + "/token", "client_id": "cid",
                      "client_secret": "{{ secrets.client_secret }}", "refresh_token": "{{ secrets.refresh_token }}"}
    return source


def oauth_routes(api):
    """
    /token: tok-live-1, then tok-live-2 (with a rotated refresh token and an ID token); /items: two pages, where the
    second answers 401 (the token is refreshed), then 500 (retried), each echoing the token it got.
    """
    tokens = iter(TOKENS[:2])
    api.routes[("POST", "/token")] = lambda request: (200, {
        "access_token": next(tokens), "expires_in": 3600, "token_type": "bearer", "refresh_token": TOKENS[2],
        "id_token": TOKENS[3]})
    seen = []

    def items(request):
        seen.append(request["headers"]["Authorization"])
        echo = {"error": "bad token %s" % seen[-1].split()[-1]}
        if len(seen) == 2:
            return 401, echo
        if len(seen) == 3:
            return 500, echo
        offset = int(request["params"]["o"])
        return 200, {"data": [{"id": offset + 1}, {"id": offset + 2}][:2 if offset == 0 else 1]}, \
            {"Set-Cookie": "session=%s" % seen[-1].split()[-1]}
    api.routes[("GET", "/items")] = items
    return seen


def write_source(tmp_path, monkeypatch, source, secrets=None):
    """A CLI-runnable copy of `source`, its secrets (default: SECRETS) in the environment."""
    secrets = SECRETS if secrets is None else secrets
    path = tmp_path / "source.yaml"
    path.write_text(json.dumps(dict(source, spec={"secrets": dict((name, {"type": "string"}) for name in secrets)})))
    for name, value in secrets.items():
        monkeypatch.setenv("STREAMWRIGHT_SECRET_" + name.upper(), value)
    return path


def run_cli(capsys, *arguments):
    """(exit status, stdout, stderr) of `streamwright run ARGUMENTS`."""
    code = cli.main(["run"] + [str(argument) for argument in arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def json_lines(text):
    return [json.loads(line) for line in text.splitlines()]


def events(text, event):
    return [line for line in json_lines(text) if line.get("event") == event]


def network_lines(text):
    return [line.split("streamwright.network: ", 1)[1] for line in text.splitlines() if " streamwright.network: " in line]


@contextlib.contextmanager
def logging_to(level=None, log_format="text", loggers=(), max_chars=logs.DEFAULT_MAX_CHARS):
    """streamwright's logging into a StringIO, redacting with the Redactor it yields with it (for SourceRunner)."""
    stream = io.StringIO()
    setup = LogSetup(level, log_format, list(loggers), None, max_chars, stream=stream)
    setup.redact = Redactor()
    try:
        yield stream, setup.redact
    finally:
        setup.close()


def assert_no_secrets(text, values=tuple(SECRETS.values()) + TOKENS):
    leaked = [value for value in values if value in text]
    assert not leaked, "leaked %s in:\n%s" % (leaked, text)


def write_config(tmp_path, config, name="logging.yaml"):
    path = tmp_path / name
    path.write_text(json.dumps(config))  # (JSON is YAML)
    return path


# * --------------------------------------------
# * loggers, levels, --log-level, --log-format
# * --------------------------------------------

def test_text_lines_and_the_default_levels(api, tmp_path, monkeypatch, capsys):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}, {"id": 2}]})
    stream = items_stream(partitions=[{"name": "account", "values": ["a1", "a2"]}],
                          params={"account": "{{ partition.account }}"})
    path = write_source(tmp_path, monkeypatch, http_source(api, stream), secrets={})
    code, out, err = run_cli(capsys, path)
    assert code == 0 and out.count('"type": "RECORD"') == 4
    lines = err.splitlines()
    assert all(TEXT_LINE.match(line) for line in lines), err
    messages = [line.split(": ", 1)[1] for line in lines if " INFO streamwright.source: " in line]
    assert messages[0] == "stream 'items': starting (mode page, 2 partition(s), requests: raw_items)"
    assert re.match(r'stream \'items\', partition \{"account": "a1"\}: 1 page\(s\), 2 record\(s\), \d+\.\d s$',
                    messages[1])
    assert re.match(r"stream 'items': 4 record\(s\) written \(items: 4\), 2 request\(s\) \(raw_items: 2\), 0 retries, "
                    r"0 failed partition\(s\), \d+\.\d s$", messages[3])
    assert re.match(r"run finished in \d+\.\d s: 1 stream\(s\), 4 record\(s\) written \(items: 4\), 2 request\(s\), "
                    r"0 retries, 0 failed partition\(s\), 1 output\(s\) written$", messages[-1])
    assert "streamwright.network" not in err and " DEBUG " not in err  # streamwright.network is at WARNING by default

    code, _, err = run_cli(capsys, path, "--log-level", "warning")
    assert code == 0 and " INFO " not in err and " WARNING streamwright.source: " in err  # (the http:// warning)
    code, _, err = run_cli(capsys, path, "--log-level", "ERROR")
    assert code == 0 and err == ""


def test_the_levels_of_the_streamwright_loggers():
    streamwright = logging.getLogger("streamwright")
    with logging_to():
        assert (streamwright.level, logs.NETWORK.level) == (logging.INFO, logging.WARNING)
    with logging_to(level="DEBUG"):
        assert (streamwright.level, logs.NETWORK.level) == (logging.DEBUG, logging.WARNING)  # the network stays quiet
    with logging_to(level="ERROR", loggers=[("some.sdk", logging.DEBUG)]):
        assert (streamwright.level, logs.NETWORK.level) == (logging.ERROR, logging.ERROR)  # quieter: the network too
        assert logging.getLogger("some.sdk").level == logging.DEBUG
    with logging_to(loggers=[("streamwright.network", logging.DEBUG), ("root", logging.INFO)]):
        assert (logs.NETWORK.level, logging.getLogger().level) == (logging.DEBUG, logging.INFO)
    assert (streamwright.level, logs.NETWORK.level, logging.getLogger("some.sdk").level) == (logging.NOTSET,) * 3
    assert logs.level_number("warning") == logging.WARNING and logs.level_number(None) is None
    with pytest.raises(ValueError, match="unknown level 'LOUD'"):
        logs.level_number("LOUD")


@pytest.mark.parametrize("value", ["streamwright.network", "streamwright.network=LOUD", "=INFO", "streamwright.network="])
def test_log_takes_name_equals_level(capsys, value):
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "source.yaml", "--log", value])
    assert error.value.code == 2 and "expected NAME=LEVEL" in capsys.readouterr().err


def test_json_lines_parse_and_carry_fields(api, tmp_path, monkeypatch, capsys):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    stream = items_stream(partitions=[{"name": "account", "values": ["a1"]}],
                          incremental={"cursor_field": "id", "start": "today"},
                          params={"account": "{{ partition.account }}", "day": "{{ window.start }}"})
    path = write_source(tmp_path, monkeypatch, http_source(api, stream), secrets={})
    code, _, err = run_cli(capsys, path, "--log-format", "json", *NETWORK_INFO)
    assert code == 0
    lines = json_lines(err)
    for line in lines:
        assert list(line)[:4] == ["time", "level", "logger", "message"]
        assert line["time"].endswith("Z") and datetime.datetime.fromisoformat(line["time"].replace("Z", "+00:00"))
    today = datetime.date.today().isoformat()
    (start,) = events(err, "stream_start")
    assert start["stream"] == "items" and start["logger"] == "streamwright.source" and start["level"] == "INFO"
    (request,) = events(err, "http_request")
    assert request["logger"] == "streamwright.network"
    assert dict((key, request[key]) for key in ("stream", "request", "partition", "window", "method", "status",
                                                "attempt", "bytes")) == {
        "stream": "items", "request": "raw_items", "partition": {"account": "a1"},
        "window": {"start": today, "end": today}, "method": "GET", "status": 200, "attempt": 1,
        "bytes": len(json.dumps({"data": [{"id": 1}]}))}
    assert request["url"].startswith(api.url + "/items?") and isinstance(request["duration_ms"], int)
    (read,) = events(err, "read")
    assert (read["pages"], read["records"], read["window"]) == (1, 1, {"start": today, "end": today})
    (end,) = events(err, "stream_end")
    assert (end["records"], end["exports"], end["requests"], end["retries"], end["failed_partitions"]) == (
        1, {"items": 1}, 1, 0, 0)
    (run_end,) = events(err, "run_end")
    assert (run_end["outcome"], run_end["streams"], run_end["records"], run_end["outputs"]) == ("ok", 1, 1, 1)


def test_long_messages_are_cut_with_a_marker(api, tmp_path, monkeypatch, capsys):
    long_name = "x" * 3000
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1, "name": long_name}]})
    path = write_source(tmp_path, monkeypatch, http_source(api), secrets={})
    code, _, err = run_cli(capsys, path, *NETWORK_DEBUG + ["--log-max-chars", "300"])
    assert code == 0
    body = next(line for line in err.split("\n[") if "response body: " in line)
    assert re.search(r"\.\.\. \[truncated \d+ chars\]$", body) and long_name not in err
    message = body.split("streamwright.network: ", 1)[1]
    assert len(message) == 300 + len("... [truncated %s chars]" % message.rsplit(" ", 2)[-2])
    code, _, err = run_cli(capsys, path, *NETWORK_DEBUG + ["--log-max-chars", "0"])  # never cut
    assert code == 0 and long_name in err and "truncated" not in err


def test_the_log_setup_truncates_redacts_and_restores():
    factory, last_resort = logging.getLogRecordFactory(), logging.lastResort
    logger = logging.getLogger("some.library")
    with logging_to(max_chars=10) as (stream, redact):
        redact.add("hunter22")
        logger.warning("%s", "a" * 25)
        logger.warning("password hunter22, url https://x.test/a?sig=abc123&b=1 and Bearer abc.def-123456")
        logger.warning("ok")
    lines = [line.split("some.library: ", 1)[1] for line in stream.getvalue().splitlines()]
    assert lines[0] == "aaaaaaaaaa... [truncated 15 chars]" and lines[2] == "ok"
    assert lines[1].startswith("password *") and "hunter22" not in stream.getvalue()
    assert logging.getLogRecordFactory() is factory and logging.lastResort is last_resort
    with logging_to() as (stream, _):
        logger.warning("url https://x.test/a?sig=abc123&b=1 and Bearer abc.def-123456 for user:pw@host")
    assert "?sig=***&b=1" in stream.getvalue() and "Bearer ***" in stream.getvalue()


def test_lines_below_a_logger_that_does_not_propagate_reach_the_handler():
    """google-api-core sets the `google` logger not to propagate: --log google.ads.googleads.client=DEBUG works."""
    parent, child = logging.getLogger("quiet_sdk"), logging.getLogger("quiet_sdk.client")
    parent.propagate = False
    try:
        with logging_to(loggers=[("quiet_sdk.client", logging.DEBUG)]) as (stream, redact):
            redact.add("token-in-sdk-1")
            child.debug("sent token-in-sdk-1")
        assert stream.getvalue().endswith("DEBUG quiet_sdk.client: sent ***\n")
        with logging_to() as (stream, _):  # its own level (NOTSET): the root logger's WARNING
            child.debug("not shown")
            child.warning("shown")
        assert "not shown" not in stream.getvalue() and "WARNING quiet_sdk.client: shown" in stream.getvalue()
    finally:
        parent.propagate = True


def test_json_lines_redact_their_fields_and_tracebacks():
    with logging_to(log_format="json") as (stream, redact):
        redact.add('pa"ss\\word-1')
        logs.LOG.info("call", extra=logs.fields(stream="s", partition={"key": 'pa"ss\\word-1'}, records=3))
        try:
            raise ValueError("failed with pa\"ss\\word-1")
        except ValueError:
            logs.LOG.error("boom", exc_info=True)
        logging.getLogger("other.library").warning("x", extra={"stream": "not shown"})
    first, second, third = json_lines(stream.getvalue())
    assert (first["partition"], first["records"], first["stream"]) == ({"key": "***"}, 3, "s")
    assert "ValueError: failed with ***" in second["exception"] and "word-1" not in stream.getvalue()
    assert "stream" not in third  # fields only for streamwright's own lines


def test_masking_helpers():
    assert [logs.sensitive(name) for name in ("Authorization", "developer-token", "X-Api-Key", "client_secret",
                                              "Set-Cookie", "sig", "X-Amz-Signature", "Content-Type", "account")] == \
        [True] * 7 + [False] * 2
    assert logs.mask_url("https://user:pw@host/p?api_key=1&page=2&access_token=t") == \
        "https://user:***@host/p?api_key=***&page=2&access_token=***"
    assert logs.mask_text("cannot connect to postgresql://etl:pa55@db:5432/x (https://host:8080/a@b)") == \
        "cannot connect to postgresql://etl:***@db:5432/x (https://host:8080/a@b)"
    assert logs.mask_text("<Url>https://h/r.zip?sv=1&amp;sig=SAS&amp;se=2</Url>") == \
        "<Url>https://h/r.zip?sv=1&amp;sig=***&amp;se=2</Url>"
    assert logs.mask_form("grant_type=refresh_token&refresh_token=r1&client_id=c") == \
        "grant_type=refresh_token&refresh_token=***&client_id=c"
    assert logs.mask_headers({"Cookie": "a", "Accept": "b"}) == {"Cookie": "***", "Accept": "b"}
    assert logs.mask_text("GET https://h/a?api_key=abc: HTTP 400 (see https://h/b?token=t1).") == \
        "GET https://h/a?api_key=***: HTTP 400 (see https://h/b?token=***)."
    assert logs.mask_text(logs.mask_text("GET https://h/a?sig=s1: 200")) == "GET https://h/a?sig=***: 200"
    assert logs.mask_text("the bearer token was refused; Basic authentication") == \
        "the bearer token was refused; Basic authentication"


# * -----------------------------
# * streamwright.network: HTTP requests
# * -----------------------------

def test_network_lines_for_http_with_retries_and_waits(api, caplog):
    replies = [(500, {}), (200, {"data": [{"id": 1}, {"id": 2}]}), (200, {"data": [{"id": 3}]})]
    sizes = [len(json.dumps(reply[1])) for reply in replies]
    api.routes[("GET", "/items")] = lambda request: replies.pop(0)
    source = http_source(api, items_stream(paginator=OFFSETS), rate_limit={"requests": 2, "per": "10s"})
    clock = FakeClock()
    with logging_to(loggers=[("streamwright.network", logging.INFO)]) as (stream, redact):
        with caplog.at_level(logging.INFO):
            SourceRunner(source, {}, {}, output=MemoryOutput(), today=TODAY, clock=clock, sleep=clock.sleep,
                         redact=redact).run()
    url = api.url + "/items?o=%d&l=2"
    assert [line for line in network_lines(stream.getvalue()) if " GET " in line] == [
        "stream 'items', request 'raw_items': GET %s: 500, %d bytes, 0.00 s, attempt 1" % (url % 0, sizes[0]),
        "stream 'items', request 'raw_items': GET %s: 200, %d bytes, 0.00 s, attempt 2" % (url % 0, sizes[1]),
        "stream 'items', request 'raw_items': GET %s: 200, %d bytes, 0.00 s, attempt 1" % (url % 2, sizes[2])]
    retry = next(record for record in caplog.records if getattr(record, "event", None) == "retry")
    assert (retry.name, retry.levelname) == ("streamwright.network", "WARNING")
    assert (retry.attempt, retry.status, retry.request) == (2, 500, "raw_items")
    wait = next(record for record in caplog.records if getattr(record, "event", None) == "rate_limit")
    assert (wait.name, wait.levelname, wait.getMessage()) == ("streamwright.network", "INFO",
                                                              "rate limit reached; waiting 10.0s")
    assert wait.duration_ms == 10000 and clock.sleeps == [0, 10.0]  # (max_delay: 0s)


def test_retries_show_by_default_and_waits_with_network_info(api, tmp_path, monkeypatch, capsys):
    replies = [(500, {}), (200, {"data": [{"id": 1}]})]
    api.routes[("GET", "/items")] = lambda request: replies.pop(0)
    path = write_source(tmp_path, monkeypatch, http_source(api), secrets={})
    code, _, err = run_cli(capsys, path)
    assert code == 0 and "WARNING streamwright.network: GET %s/items failed: HTTP 500: {}; retrying in 0.0s (attempt 2 of " \
                         "3)" % api.url in err
    assert "INFO streamwright.network" not in err


def test_network_debug_masks_headers_and_shows_bodies(api, tmp_path, monkeypatch, capsys):
    oauth_routes(api)
    source = oauth_source(api, items_stream(paginator=OFFSETS, params={"api_key": "public-key-9"}))
    source["http"]["headers"] = {"X-Api-Key": "header-key-8", "Accept": "application/json"}
    code, _, err = run_cli(capsys, write_source(tmp_path, monkeypatch, source), *NETWORK_DEBUG)
    assert code == 0
    assert "INFO streamwright.network: POST %s/token: 200, " % api.url in err
    assert "request body: grant_type=refresh_token&refresh_token=***&client_id=cid&client_secret=***" in err
    assert '"X-Api-Key": "***"' in err and '"Authorization": "***"' in err and '"Accept": "application/json"' in err
    assert '"Set-Cookie": "***"' in err and '"Cookie": "***"' in err
    assert "/items?api_key=***&o=0&l=2: 200" in err and 'response body: {"data": [{"id": 1}, {"id": 2}]}' in err
    token_response = next(line for line in err.split("\n[") if "/token: response 200" in line)
    assert "body" not in token_response  # a token response is credentials
    assert_no_secrets(err, tuple(SECRETS.values()) + TOKENS + ("public-key-9", "header-key-8"))


@pytest.mark.parametrize("logging_options", [
    [], NETWORK_INFO, NETWORK_DEBUG + ["--log-level", "DEBUG"], NETWORK_DEBUG + ["--log", "root=DEBUG"]])
@pytest.mark.parametrize("log_format", ["text", "json"])
def test_no_secret_or_token_is_ever_logged(api, tmp_path, monkeypatch, capsys, logging_options, log_format):
    seen = oauth_routes(api)
    path = write_source(tmp_path, monkeypatch, oauth_source(api, items_stream(paginator=OFFSETS)))
    arguments = logging_options + ["--log-format", log_format, "--summary", tmp_path / "run.json"]
    code, out, err = run_cli(capsys, path, *arguments)
    assert code == 0 and out.count('"type": "RECORD"') == 3 and len(seen) == 4
    assert seen[1].endswith(TOKENS[0]) and seen[2].endswith(TOKENS[1])  # the token was refreshed after the 401
    assert_no_secrets(err + (tmp_path / "run.json").read_text())
    if logging_options:
        assert "***" in err
    if "root=DEBUG" in logging_options:
        assert "urllib3.connectionpool" in err  # every library's lines, redacted too
    if log_format == "json":
        json_lines(err)

    api.routes[("POST", "/token")] = lambda request: (200, {"access_token": TOKENS[0], "expires_in": 3600})
    api.routes[("GET", "/items")] = lambda request: (400, {"error": "token %s is bad" % TOKENS[0]})
    code, _, err = run_cli(capsys, path, *arguments)
    assert code == 1 and "HTTP 400" in err
    assert_no_secrets(err + (tmp_path / "run.json").read_text())


def test_a_real_process_logs_json_lines_without_secrets(api, tmp_path, monkeypatch):
    oauth_routes(api)
    path = write_source(tmp_path, monkeypatch, oauth_source(api, items_stream(paginator=OFFSETS)))
    # a separate process: the command sets up logging itself, with no other handler (as it does for users)
    result = subprocess.run([sys.executable, "-m", "streamwright.core.cli", "run", str(path), "--log-format", "json",
                             "--log-level", "DEBUG"] + NETWORK_DEBUG, cwd=str(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    assert result.returncode == 0, result.stderr
    lines = json_lines(result.stderr)
    assert {"streamwright.source", "streamwright.network"} <= {line["logger"] for line in lines}
    assert all(line["logger"].startswith("streamwright.") for line in lines)
    assert [line["event"] for line in lines if line.get("event") in ("stream_start", "stream_end", "run_end")] == [
        "stream_start", "stream_end", "run_end"]
    assert sum(1 for line in lines if line.get("event") == "http_request") == 6  # 2 to /token (the 401), 4 to /items
    assert_no_secrets(result.stderr)


# * ------------
# * --log-config
# * ------------

def file_config(path, formatter=None):
    """A logging.config.dictConfig mapping: the root logger's lines to the file `path`."""
    return {"version": 1,
            "formatters": {"lines": formatter or {"format": logs.TEXT_FORMAT}},
            "handlers": {"file": {"class": "logging.FileHandler", "filename": str(path), "formatter": "lines",
                                  "encoding": "utf-8"}},
            "root": {"level": "WARNING", "handlers": ["file"]}}


def test_a_log_config_file_replaces_the_handler_and_is_redacted_too(api, tmp_path, monkeypatch, capsys):
    oauth_routes(api)
    path = write_source(tmp_path, monkeypatch, oauth_source(api, items_stream(paginator=OFFSETS)))
    log_file = tmp_path / "streamwright.log"
    config = write_config(tmp_path, file_config(log_file))
    root_handlers = list(logging.getLogger().handlers)
    code, out, err = run_cli(capsys, path, "--log-config", config, *NETWORK_DEBUG)
    assert code == 0 and out.count('"type": "RECORD"') == 3
    assert "streamwright.source" not in err and "streamwright.network" not in err  # the config's handler, not streamwright's
    text = log_file.read_text()
    assert all(TEXT_LINE.match(line) for line in text.splitlines() if line.startswith("[")), text
    assert "INFO streamwright.source: stream 'items': starting" in text  # streamwright's levels apply on top of the config
    assert "DEBUG streamwright.network: POST %s/token: request headers" % api.url in text
    assert 'response body: {"data": [{"id": 1}, {"id": 2}]}' in text
    assert_no_secrets(text)
    assert logging.getLogger().handlers == root_handlers  # put back; the config's file is closed

    # JSON lines through the config, its own levels for streamwright's loggers, and --log-level on top
    json_file = tmp_path / "streamwright.jsonl"
    config = file_config(json_file, {"()": "streamwright.core.runtime.logs.JsonFormatter"})
    config["loggers"] = {"streamwright": {"level": "WARNING"}, "streamwright.network": {"level": "INFO"}}
    oauth_routes(api)
    code, _, _ = run_cli(capsys, path, "--log-config", write_config(tmp_path, config, "logging.json"))
    lines = json_lines(json_file.read_text())
    assert code == 0 and not [line for line in lines if line["logger"] == "streamwright.source" and line["level"] == "INFO"]
    assert [line["event"] for line in lines if line["level"] == "INFO"].count("http_request") == 6
    assert_no_secrets(json_file.read_text())
    oauth_routes(api)
    code, _, _ = run_cli(capsys, path, "--log-config", tmp_path / "logging.json", "--log-level", "INFO")
    lines = json_lines(json_file.read_text())
    assert code == 0 and [line for line in lines if line["logger"] == "streamwright.source" and line["level"] == "INFO"]


@pytest.mark.parametrize("content,problem", [
    ("[1, 2]", "expected a mapping in logging.config.dictConfig's schema"),
    ("handlers: {}", "dictionary doesn't specify a version"),
    ("version: 1\nhandlers: {h: {class: no.such.Handler}}", "Unable to configure handler 'h'"),
    ("version: 1\nroot: {level: LOUD}", "Unknown level: 'LOUD'"),
])
def test_log_config_problems_stop_the_command(tmp_path, capsys, content, problem):
    config = tmp_path / "logging.yaml"
    config.write_text(content)
    handlers, factory = list(logging.getLogger().handlers), logging.getLogRecordFactory()
    assert cli.main(["run", str(tmp_path / "source.yaml"), "--log-config", str(config)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("streamwright: error: --log-config %s: " % config) and problem in err
    assert logging.getLogger().handlers == handlers and logging.getLogRecordFactory() is factory
    assert cli.main(["run", "x.yaml", "--log-config", str(tmp_path / "missing.yaml")]) == 2
    assert "No such file or directory" in capsys.readouterr().err


def test_log_format_is_for_streamwrights_own_handler(tmp_path, capsys):
    config = write_config(tmp_path, file_config(tmp_path / "x.log"))
    assert cli.main(["run", "x.yaml", "--log-config", str(config), "--log-format", "json"]) == 2
    assert "--log-format is the format of streamwright's own handler, which --log-config replaces" in \
        capsys.readouterr().err
    assert not (tmp_path / "x.log").exists()


# * --------------------------
# * an SDK connector and its logs
# * --------------------------

WIRE = logging.getLogger("token_sdk.wire")
PRIVATE = logging.getLogger("token_sdk.private")  # a logger with a handler of its own
SDK_TOKEN = "sdk-token-42"


class TokenConnector(Connector):
    """Gets an access token when it connects (and logs it, as SDKs do); its SDK logs its requests on WIRE."""

    name = "token_sdk"
    auth_required = ("api_secret",)
    network_loggers = ("token_sdk.wire",)

    def connect(self, auth, context):
        # as google-api-core does to the `google` logger when a client is made: its lines reach no handler
        logging.getLogger("token_sdk").propagate = False
        context.secret(SDK_TOKEN)
        WIRE.warning("connected with token %s (secret %s)", SDK_TOKEN, auth["api_secret"])
        return {"token": SDK_TOKEN}

    def request(self, client, request, context):
        def send():
            WIRE.debug("POST /v1/%s Authorization: %s body=%s", request["method"], client["token"],
                       json.dumps(request["arguments"], default=str))
            WIRE.info("200 OK for access_token=%s", client["token"])
            PRIVATE.debug("private %s", client["token"])
            return [{"rows": [{"id": 1}, {"id": 2}]}, {"rows": [{"id": 3}]}]
        for response in context.call(send):
            yield response


@pytest.fixture
def token_sdk(tmp_path, monkeypatch):
    """
    The connector, registered, and a CLI-runnable source that reads its Reports.list in two pages. Its SDK's top logger
    stops propagating when it connects; PRIVATE writes to a handler of its own.
    """
    connector = TokenConnector()
    components.register(connector)
    private = io.StringIO()
    handler = logging.StreamHandler(private)
    PRIVATE.addHandler(handler)
    PRIVATE.setLevel(logging.DEBUG)
    stream = page_stream("rows", {"name": "raw_rows", "sdk": "token_sdk", "service": "Reports", "method": "list",
                                  "arguments": {"day": "{{ today }}"}},
                         "SELECT (record->>'id')::BIGINT AS id FROM raw_rows", records={"path": "rows"})
    source = {"kind": "source", "name": "sdk_demo", "auth": {"provider": "token_sdk",
                                                             "api_secret": "{{ secrets.api_secret }}"},
              "streams": [stream]}
    yield write_source(tmp_path, monkeypatch, source, secrets={"api_secret": "api-secret-31"}), private
    PRIVATE.removeHandler(handler)
    PRIVATE.setLevel(logging.NOTSET)
    logging.getLogger("token_sdk").propagate = True
    components.unregister("token_sdk")


@pytest.mark.parametrize("logging_options", [[], NETWORK_INFO, NETWORK_DEBUG,
                                             NETWORK_DEBUG + ["--log", "token_sdk.wire=DEBUG"]])
def test_sdk_calls_are_logged_and_sdk_tokens_redacted(token_sdk, capsys, logging_options):
    path, private = token_sdk
    code, out, err = run_cli(capsys, path, *logging_options)
    assert code == 0 and out.count('"type": "RECORD"') == 3
    assert "WARNING token_sdk.wire: connected with token *** (secret ***)" in err  # whatever the levels
    assert_no_secrets(err + private.getvalue(), (SDK_TOKEN, "api-secret-31"))
    assert private.getvalue() == "private ***\n"
    pages = [line for line in network_lines(err) if "Reports.list" in line]
    if not logging_options:
        assert pages == [] and "streamwright.network" not in err
        return
    assert [re.sub(r"\d+\.\d\d s", "T s", line) for line in pages] == [
        "stream 'rows', request 'raw_rows': token_sdk Reports.list page 1: 2 record(s), T s, attempt 1",
        "stream 'rows', request 'raw_rows': token_sdk Reports.list page 2: 1 record(s), T s, attempt 1"]
    assert "INFO streamwright.network: token_sdk: connected in " in err
    if "token_sdk.wire=DEBUG" in logging_options:  # the SDK's own lines only when its logger is named
        assert 'DEBUG token_sdk.wire: POST /v1/list Authorization: *** body={"day": "' in err
        assert "INFO token_sdk.wire: 200 OK for access_token=***" in err
    else:
        assert "token_sdk.wire: POST" not in err and "200 OK" not in err
    assert WIRE.level == logging.NOTSET  # put back


def test_sdk_call_lines_in_json(token_sdk, capsys):
    path, _ = token_sdk
    code, _, err = run_cli(capsys, path, "--log-format", "json", *NETWORK_INFO)
    assert code == 0
    calls = events(err, "sdk_call")
    assert [(c["connector"], c["call"], c["stream"], c["request"], c["page"], c["records"], c["attempt"])
            for c in calls] == [("token_sdk", "Reports.list", "rows", "raw_rows", 1, 2, 1),
                                ("token_sdk", "Reports.list", "rows", "raw_rows", 2, 1, 1)]
    connect = events(err, "connect")
    assert connect[0]["connector"] == "token_sdk" and isinstance(connect[0]["duration_ms"], int)


def test_a_log_config_file_gets_sdk_lines_redacted(token_sdk, tmp_path, capsys):
    path, _ = token_sdk
    log_file = tmp_path / "sdk.log"
    config = file_config(log_file)
    config["loggers"] = {"token_sdk.wire": {"level": "DEBUG"}}
    code, _, err = run_cli(capsys, path, "--log-config", write_config(tmp_path, config), *NETWORK_DEBUG)
    text = log_file.read_text()
    assert code == 0 and err == ""
    assert "DEBUG token_sdk.wire: POST /v1/list Authorization: ***" in text and "Reports.list page 1" in text
    assert_no_secrets(text, (SDK_TOKEN, "api-secret-31"))


def test_connectors_lists_the_sdk_loggers(token_sdk, capsys, monkeypatch):
    available, load = components.available, components.load

    def load_or_fail(name, allowed=None, kind="connector"):
        if name == "broken":
            raise components.ComponentLoadError("connector 'broken' could not be loaded: ImportError: no SDK")
        return load(name, allowed, kind)
    monkeypatch.setattr(components, "available",
                        lambda kind="connector": available(kind) + ["broken"] * (kind == "connector"))
    monkeypatch.setattr(components, "load", load_or_fail)
    assert cli.main(["connectors"]) == 0
    lines = capsys.readouterr().out.splitlines()
    stripped = [line.strip() for line in lines]
    assert "token_sdk (SDK loggers: token_sdk.wire)" in stripped
    assert "broken — connector 'broken' could not be loaded: ImportError: no SDK" in stripped


def test_connector_tokens_are_masked_through_the_context():
    context = ConnectorContext(TokenConnector(), Redactor())
    context.secret("tok-from-sdk")
    context.secret(None)
    assert context.redact("Authorization: tok-from-sdk") == "Authorization: ***"


# * ----------------------------
# * progress and run metrics
# * ----------------------------

def paged_items(api, clock=None, seconds=0, pages=7):
    """GET /items: `pages` pages of 2 records (offsets), then an empty one; each request moves `clock` on."""
    def items(request):
        if clock is not None:
            clock.now += seconds
        offset = int(request["params"]["o"])
        return 200, {"data": [{"id": offset}, {"id": offset + 1}] if offset < 2 * pages else []}
    api.routes[("GET", "/items")] = items


@pytest.mark.parametrize("seconds,pages,expected", [
    (12, logs.PROGRESS_PAGES, [(3, 6), (6, 12)]),  # every 30 seconds
    (0, 3, [(3, 6), (6, 12)]),  # every 3 pages
])
def test_progress_lines_while_a_request_keeps_paging(api, caplog, seconds, pages, expected):
    clock = FakeClock()
    paged_items(api, clock, seconds)
    metrics = RunMetrics(clock=clock, progress_seconds=logs.PROGRESS_SECONDS, progress_pages=pages)
    with caplog.at_level(logging.INFO, logger="streamwright.source"):
        SourceRunner(http_source(api, items_stream(paginator=OFFSETS)), {}, {}, output=MemoryOutput(), today=TODAY,
                     clock=clock, sleep=clock.sleep, metrics=metrics).run()
    progress = [record for record in caplog.records if getattr(record, "event", None) == "progress"]
    assert [record.getMessage() for record in progress] == [
        "stream 'items', request 'raw_items': %d page(s), %d record(s) so far" % pair for pair in expected]
    assert progress[0].duration_ms == 36000 * (seconds > 0)
    assert metrics.streams["items"].pages == 8 and metrics.streams["items"].records_read == 14


def test_metrics_count_requests_retries_pages_records_and_partitions(api, caplog):
    calls = []

    def items(request):
        calls.append(dict(request["params"]))
        account, offset = request["params"]["account"], request["params"]["o"]
        if account == "a2":
            return 400, {"error": "no access"}
        if offset == "2" and [call["o"] for call in calls].count("2") == 1:
            return 500, {}
        return 200, {"data": [{"id": int(offset) + 1}, {"id": int(offset) + 2}][:2 if offset == "0" else 1]}
    api.routes[("GET", "/items")] = items
    api.routes[("GET", "/daily")] = lambda request: (200, {"data": [{"day": request["params"]["day"]}]})
    items = items_stream(paginator=OFFSETS, params={"account": "{{ partition.account }}"},
                         partitions=[{"name": "account", "values": ["a1", "a2"]}], on_partition_error="skip")
    daily = {"name": "daily", "partitions": [{"name": "account", "values": ["a1"]}],
             "incremental": {"cursor_field": "day", "start": "2026-10-01", "window": "1d"},
             "requests": [{"name": "raw_daily", "http": {"path": "/daily", "params": {"day": "{{ window.start }}"}},
                           "records": {"path": "data"}}],
             "transform": {"mode": "run", "steps": [{"name": "daily", "select": (
                 "SELECT (record->>'day')::DATE AS day, count(*) AS n FROM raw_daily GROUP BY 1")}]},
             "export": {"daily": {"step": "daily", "primary_key": ["day"]}}}
    clock = FakeClock()
    metrics = RunMetrics(clock=clock)
    output = MemoryOutput()
    with caplog.at_level(logging.INFO, logger="streamwright.source"):
        SourceRunner(http_source(api, items, daily), {}, {}, output=output, today=TODAY, clock=clock,
                     sleep=clock.sleep, metrics=metrics).run()
    summary = metrics.summary()
    assert [dict(stream, duration_s=None) for stream in summary["streams"]] == [
        {"name": "items", "mode": "page", "partitions": 2, "failed_partitions": 1, "windows": 0, "pages": 2,
         "requests": {"raw_items": 4}, "retries": 1, "records_read": 3, "exports": {"items": 3}, "duration_s": None},
        {"name": "daily", "mode": "run", "partitions": 1, "failed_partitions": 0, "windows": 3, "pages": 3,
         "requests": {"raw_daily": 3}, "retries": 0, "records_read": 3, "exports": {"daily": 3}, "duration_s": None}]
    assert summary["state"] == output.states[-1] == {"bookmarks": {"daily": {'{"account": "a1"}': "2026-10-02"}}}
    assert output.summary() == [{"export": "items", "records": 3}, {"export": "daily", "records": 3}]
    messages = [record.getMessage() for record in caplog.records]
    assert 'stream \'items\', partition {"account": "a1"}: 2 page(s), 3 record(s), 0.0 s' in messages
    assert 'stream \'daily\', request \'raw_daily\', partition {"account": "a1"}, window 2026-10-01..2026-10-03: ' \
           '3 page(s), 3 record(s), 0.0 s' in messages
    assert "stream 'daily': starting (mode run, 1 partition(s), requests: raw_daily)" in messages
    assert "stream 'items': 3 record(s) written (items: 3), 4 request(s) (raw_items: 4), 1 retry, 1 failed " \
           "partition(s), 0.0 s" in messages


def test_a_stream_that_fails_keeps_its_counts(api):
    api.routes[("GET", "/items")] = lambda request: (400, {"error": "bad"})
    metrics = RunMetrics()
    with pytest.raises(SourceError):
        SourceRunner(http_source(api), {}, {}, output=MemoryOutput(), today=TODAY, metrics=metrics).run()
    metrics.finish("failed", log=False)
    (stream,) = metrics.summary()["streams"]
    assert (stream["requests"], stream["pages"], stream["duration_s"] is not None) == ({"raw_items": 1}, 0, True)


# * ---------
# * --summary
# * ---------

def test_the_summary_of_a_run(api, tmp_path, monkeypatch, capsys):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}, {"id": 2}]})
    stream = items_stream(incremental={"cursor_field": "id", "start": "today"}, params={"day": "{{ window.start }}"})
    path = write_source(tmp_path, monkeypatch, http_source(api, stream), secrets={})
    summary_path = tmp_path / "out" / "run.json"
    (tmp_path / "out").mkdir()
    code, _, _ = run_cli(capsys, path, "--output", "jsonl:%s" % (tmp_path / "files"), "--summary", summary_path)
    assert code == 0
    summary = json.loads(summary_path.read_text())
    assert list(summary) == ["status", "started_at", "finished_at", "duration_s", "source", "streams", "outputs",
                             "state"]
    assert (summary["status"], summary["source"]) == ("ok", "demo")
    assert summary["started_at"] <= summary["finished_at"] and summary["duration_s"] >= 0
    (stream,) = summary["streams"]
    assert dict(stream, duration_s=None) == {
        "name": "items", "mode": "page", "partitions": 1, "failed_partitions": 0, "windows": 1, "pages": 1,
        "requests": {"raw_items": 1}, "retries": 0, "records_read": 2, "exports": {"items": 2}, "duration_s": None}
    assert [(entry["export"], entry["records"]) for entry in summary["outputs"]] == [("items", 2)]
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    assert summary["state"] == {"bookmarks": {"items": {"{}": yesterday}}}
    assert os.listdir(str(tmp_path / "out")) == ["run.json"]  # written atomically: no temporary file is left


def test_the_summary_of_a_failed_run(api, tmp_path, monkeypatch, capsys):
    api.routes[("POST", "/token")] = lambda request: (200, {"access_token": TOKENS[0], "expires_in": 3600})
    api.routes[("GET", "/items")] = lambda request: (400, {"error": "token %s is not allowed" % TOKENS[0]})
    path = write_source(tmp_path, monkeypatch, oauth_source(api))
    code, _, err = run_cli(capsys, path, "--summary", tmp_path / "run.json")
    assert code == 1
    summary = json.loads((tmp_path / "run.json").read_text())
    assert summary["status"] == "failed" and all(entry["records"] == 0 for entry in summary["outputs"])
    assert summary["error"].startswith("stream 'items', partition {}: request 'raw_items': GET %s/items failed: "
                                       "HTTP 400: " % api.url)
    assert "***" in summary["error"] and summary["streams"][0]["requests"] == {"raw_items": 1}
    assert re.search(r"INFO streamwright.source: run failed after \d+\.\d s: 1 stream\(s\)", err)
    assert_no_secrets(err + json.dumps(summary))


KEY = "Zq9XvW7uTr5sPo3nLm1K"  # a secret without a common part (no 4 characters of it appear elsewhere)


def assert_no_part_of(secret, text):
    parts = [secret[start:start + 4] for start in range(len(secret) - 3)]
    leaked = [part for part in parts if part in text]
    assert not leaked, "leaked %s in:\n%s" % (leaked, text)


@pytest.mark.parametrize("failing", ["request", "token request"])
def test_an_error_body_is_redacted_before_it_is_cut(api, tmp_path, monkeypatch, capsys, failing):
    """A secret that crosses the 300th character of an error's body is redacted whole, never cut to a prefix."""
    def echo(request):  # the secret starts at the body's 295th character: '{"error": "' is 11
        return 400, {"error": "x" * 284 + KEY + " is not allowed"}
    if failing == "request":
        source = http_source(api)
        source["auth"] = {"type": "api_key", "name": "X-Api-Key", "value": "{{ secrets.api_key }}"}
        api.routes[("GET", "/items")] = echo
        path = write_source(tmp_path, monkeypatch, source, secrets={"api_key": KEY})
    else:
        api.routes[("POST", "/token")] = echo
        path = write_source(tmp_path, monkeypatch, oauth_source(api),
                            secrets={"client_secret": KEY, "refresh_token": "refresh-token-abc"})
    code, _, err = run_cli(capsys, path, "--summary", tmp_path / "run.json", *NETWORK_DEBUG)
    summary = (tmp_path / "run.json").read_text()
    assert code == 1 and "HTTP 400: {\"error\": \"xxxxxxxxxx" in json.loads(summary)["error"]
    assert "x" * 284 + "***" in err
    assert_no_part_of(KEY, err + summary)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_the_summary_of_an_interrupted_run(api, tmp_path, monkeypatch, capsys, interruption):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    path = write_source(tmp_path, monkeypatch, http_source(api), secrets={})
    original = SourceRunner.run_stream

    def run_stream(self, stream):
        original(self, stream)
        raise interruption()
    monkeypatch.setattr(SourceRunner, "run_stream", run_stream)
    arguments = ["run", str(path), "--summary", str(tmp_path / "run.json")]
    if interruption is KeyboardInterrupt:
        assert cli.main(arguments) == 130
    else:
        with pytest.raises(SystemExit):
            cli.main(arguments)
    summary = json.loads((tmp_path / "run.json").read_text())
    assert (summary["status"], summary["error"]) == ("failed", "interrupted")
    assert summary["streams"][0]["exports"] == {"items": 1}
    assert "run interrupted after" in capsys.readouterr().err


def test_the_summary_of_a_run_that_cannot_start(api, tmp_path, monkeypatch, capsys):
    path = write_source(tmp_path, monkeypatch, oauth_source(api))
    monkeypatch.delenv("STREAMWRIGHT_SECRET_CLIENT_SECRET")
    code, _, err = run_cli(capsys, path, "--summary", tmp_path / "run.json")
    assert code == 2
    summary = json.loads((tmp_path / "run.json").read_text())
    assert (summary["status"], summary["source"], summary["streams"]) == ("failed", "demo", [])
    assert "missing required secret 'client_secret'" in summary["error"] and "run failed" not in err

    code, _, err = run_cli(capsys, path, "--summary", tmp_path / "nowhere" / "run.json")
    assert code == 2 and "the folder %s does not exist" % (tmp_path / "nowhere") in err and api.requests == []


# * ----------------------------------
# * streamwright validate, logging restored
# * ----------------------------------

def test_validate_takes_the_log_options(tmp_path, capsys):
    example = os.path.join(REPO_ROOT, "examples", "sources", "readers", "files_demo")
    assert cli.main(["validate", "--log-format", "json", "--strict", example, "--log-level", "WARNING",
                     "--log", "streamwright.network=DEBUG", "--log-max-chars", "100"]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: source\nname: x\nstreams: []\n")
    config = write_config(tmp_path, file_config(tmp_path / "validate.log"))
    assert cli.main(["validate", "--log-level=DEBUG", "--format", "github", str(bad), "--log-config", str(config)]) != 0
    assert "::error" in capsys.readouterr().out
    assert cli.main(["validate", "--log-config", str(config), "--log-format", "text", example]) == 2


def test_the_command_restores_logging(token_sdk, capsys):
    path, _ = token_sdk
    names = ("streamwright", "streamwright.network", "token_sdk.wire", "root")
    factory, last_resort = logging.getLogRecordFactory(), logging.lastResort
    handlers = list(logging.getLogger().handlers)
    levels = [logging.getLogger(name if name != "root" else None).level for name in names]
    assert run_cli(capsys, path, "--log-level", "DEBUG", "--log", "token_sdk.wire=DEBUG", "--log", "root=DEBUG")[0] == 0
    assert logging.getLogRecordFactory() is factory and logging.lastResort is last_resort
    assert logging.getLogger().handlers == handlers
    assert [logging.getLogger(name if name != "root" else None).level for name in names] == levels
    assert all(getattr(handler, "filters", []) == [] or all(not isinstance(f, logs._Scrub) for f in handler.filters)
               for handler in handlers)


def test_validate_help_lists_the_log_options(capsys):
    assert cli.main(["validate", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: streamwright validate") and "--strict" in out and "logging options:" in out
    for option in ("--log-level LEVEL", "--log NAME=LEVEL", "--log-format {text,json}", "--log-config FILE",
                   "--log-max-chars N"):
        assert "\n  %s" % option in out, option
    assert "the level of streamwright's loggers" in out and "cut log messages longer than N characters" in out


# * ------------------------------------------------------
# * --log-config: the handlers and loggers that exist stay
# * ------------------------------------------------------

def test_a_log_config_leaves_the_handlers_that_exist_working(api, tmp_path, monkeypatch, capsys):
    """dictConfig closes every handler; the ones that were there before the command must still work after it."""
    import logging.handlers
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    path = write_source(tmp_path, monkeypatch, http_source(api), secrets={})
    root, app = logging.getLogger(), logging.getLogger("an_app")
    file_handler = logging.FileHandler(str(tmp_path / "app.log"), mode="w", encoding="utf-8")
    target = logging.handlers.BufferingHandler(100)
    memory = logging.handlers.MemoryHandler(100, flushLevel=logging.CRITICAL, target=target)
    root.addHandler(file_handler)
    app.addHandler(memory)
    try:
        app.warning("before")
        code, _, _ = run_cli(capsys, path, "--log-config", write_config(tmp_path, file_config(tmp_path / "streamwright.log")))
        assert code == 0 and "stream 'items': starting" in (tmp_path / "streamwright.log").read_text()
        assert root.handlers.count(file_handler) == 1 and app.handlers == [memory]
        app.warning("after")
        memory.flush()
        file_handler.flush()
        assert (tmp_path / "app.log").read_text().splitlines() == ["before", "after"]
        assert memory.target is target and [record.getMessage() for record in target.buffer] == ["before", "after"]
        # the handlers the config made are closed, also one that no logger used
        made = [ref() for ref in logging._handlerList if ref() is not None and
                getattr(ref(), "baseFilename", "").startswith(str(tmp_path)) and ref() is not file_handler]
        assert [(os.path.basename(handler.baseFilename), handler.stream) for handler in made] == [("streamwright.log", None)]
        closed = []

        class Unused(logging.Handler):
            def close(self):
                closed.append(self)
                super(Unused, self).close()
        LogSetup(config={"version": 1, "handlers": {"unused": {"()": Unused}}}).close()
        assert len(closed) == 1 and file_handler.stream is not None
    finally:
        root.removeHandler(file_handler)
        app.removeHandler(memory)
        file_handler.close()
        memory.close()


def test_a_log_config_that_disables_existing_loggers_keeps_streamwrights(api, tmp_path, monkeypatch, capsys):
    """`disable_existing_loggers: true` cannot silence streamwright's loggers, --log's, or the run summary's error."""
    api.routes[("GET", "/items")] = lambda request: (400, {"error": "key %s is not allowed" % KEY})
    source = http_source(api)
    source["auth"] = {"type": "api_key", "name": "X-Api-Key", "value": "{{ secrets.api_key }}"}
    path = write_source(tmp_path, monkeypatch, source, secrets={"api_key": KEY})
    library = logging.getLogger("a_library.wire")  # made before the config, named by --log
    log_file = tmp_path / "streamwright.log"
    config = dict(file_config(log_file), disable_existing_loggers=True)
    original = cli._run

    def run(*arguments):
        library.info("a library line")
        return original(*arguments)
    monkeypatch.setattr(cli, "_run", run)
    code, _, _ = run_cli(capsys, path, "--log-config", write_config(tmp_path, config), "--log-level", "DEBUG",
                         "--log", "a_library.wire=INFO", "--summary", tmp_path / "run.json")
    assert code == 1
    text = log_file.read_text()
    assert "INFO a_library.wire: a library line" in text
    assert "ERROR streamwright.source: stream 'items', partition {}: request 'raw_items': GET %s/items failed: HTTP 400: " \
        '{"error": "key *** is not allowed"}' % api.url in text
    assert "DEBUG streamwright.source: details" in text
    summary = json.loads((tmp_path / "run.json").read_text())
    assert summary["error"] == "stream 'items', partition {}: request 'raw_items': GET %s/items failed: HTTP 400: " \
        '{"error": "key *** is not allowed"}' % api.url
    assert_no_part_of(KEY, text + json.dumps(summary))
    assert not logging.getLogger("streamwright.source").disabled and not library.disabled  # (as before the command)


def test_the_summary_error_does_not_need_a_log_line(api, tmp_path, monkeypatch, capsys):
    api.routes[("GET", "/items")] = lambda request: (400, {"error": "key %s is not allowed" % KEY})
    source = http_source(api)
    source["auth"] = {"type": "api_key", "name": "X-Api-Key", "value": "{{ secrets.api_key }}"}
    path = write_source(tmp_path, monkeypatch, source, secrets={"api_key": KEY})
    code, _, err = run_cli(capsys, path, "--log", "streamwright=CRITICAL", "--summary", tmp_path / "run.json")
    assert code == 1 and "ERROR" not in err
    summary = json.loads((tmp_path / "run.json").read_text())
    assert summary["error"].startswith("stream 'items', partition {}: request 'raw_items': GET %s/items failed: "
                                       "HTTP 400: " % api.url)
    assert_no_part_of(KEY, json.dumps(summary))


# * ---------------------------------------
# * masking: JSON-escaped URLs, long lines
# * ---------------------------------------

def test_credentials_after_json_escaped_separators_are_masked():
    """Go and .NET write & and ? in JSON as \\u0026 and \\u003f: a pre-signed URL's signature in a body."""
    url = "https://files.example/report.csv?X-Id=1&sig=S3cr3tSig%2B&se=2026-10-04&X-Api-Key=K3y"
    body = json.dumps({"url": url, "next": "/r?page=2"}).replace("&", "\\u0026")
    assert "\\u0026sig=S3cr3tSig" in body
    assert logs.mask_text(body) == body.replace("S3cr3tSig%2B", "***").replace("K3y", "***")
    escaped = '{"url": "https://files.example/r.csv\\u003Fapi_key=K3y\\u0026a=1", "x": "a\\u003fsig=S1g"}'
    assert logs.mask_text(escaped) == \
        '{"url": "https://files.example/r.csv\\u003Fapi_key=***\\u0026a=1", "x": "a\\u003fsig=***"}'
    assert logs.mask_text("a=1\\u0026b=2&c=3") == "a=1\\u0026b=2&c=3"


# the masking of URL passwords before it was linear (a scheme could start at any word)
WORD_USERINFO = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://[^/?#@:\s\"'<>]*:)([^/?#@\s\"'<>]+)@")


def test_masking_a_long_dotted_line_is_linear():
    line = "x://" + ".".join("a%d" % number for number in range(41000))
    assert len(line) > 270000
    started = time.perf_counter()
    assert logs.mask_text(line) == line
    assert time.perf_counter() - started < 0.1
    for text in ("https://user:pw@host/x", "see postgres://u:p@db:5432/x.", "a.b-c+d://u:p@h", "(https://u:p@h)",
                 "x https://h/p?q=1", '"ftp://a:b@c"', "-http://u:p@h", "é http://u:p@h", "http://u@h", "mailto:x@y",
                 "http://u:p@h,https://v:q@i", "a://b:c://d:e@f", "s3://k:s@b/p?x=1&y=2", "://u:p@h"):
        assert logs._USERINFO.sub(r"\1***@", text) == WORD_USERINFO.sub(r"\1***@", text), text
    # (and where a scheme follows a word character, which the word boundary missed)
    assert logs.mask_text("1http://u:p@h _http://u:p@h") == "1http://u:***@h _http://u:***@h"


def test_error_bodies_and_job_statuses_are_redacted_before_they_are_cut():
    redact = Redactor([KEY])
    assert error_excerpt("x" * 295 + KEY, redact) == "x" * 295 + "***"
    assert error_excerpt("x" * 290 + "?token=" + KEY, Redactor()) == "x" * 290 + "?token=***"
    assert error_excerpt("y" * 400, redact) == "y" * 300
    assert_no_part_of(KEY, job_summary({"message": "x" * 280 + KEY}, redact))
