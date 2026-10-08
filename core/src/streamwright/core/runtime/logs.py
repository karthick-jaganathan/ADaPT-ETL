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
Logging, progress and run metrics of `streamwright run`, on Python's standard model: named loggers and standard levels. Logs
go to stderr; stdout is for Singer messages. streamwright's loggers:

- `streamwright.source` (INFO): run, stream and window progress, summaries, warnings; DEBUG: internal details.
- `streamwright.network` (WARNING by default, so quiet): INFO, one line per API call - HTTP requests and connector SDK calls -
  and the throttling waits; WARNING, retries; DEBUG, the HTTP headers (credentials masked) and bodies.
- `streamwright.output`: the files and tables written.
SDK loggers keep their own names (Connector.network_loggers lists a connector's, for `streamwright connectors`).

- LogSetup: one command's logging - streamwright's stderr handler in the text or JSON format (--log-format) or a
  logging.config.dictConfig file's handlers (--log-config), the levels (--log-level for the `streamwright` loggers,
  --log NAME=LEVEL for any), and the redaction (the run's Redactor plus credentials masked by name) and truncation
  (--log-max-chars) of every log line of every logger, for every handler.
- fields(): a log line's structured fields (extra=), which JSON lines show (FIELDS).
- RunMetrics: a run's counts and timings in one place, fed by the runner, the HTTP clients and the connector context. It
  logs each stream's start and end, completed windows and partitions, the progress of long reads and the run's end,
  and gives the run summary that --summary FILE writes.
"""

import collections
import datetime
import json
import logging
import logging.config
import os
import re
import sys
import tempfile
import time
from urllib.parse import unquote_plus


__all__ = ["LogSetup", "JsonFormatter", "RunMetrics", "StreamMetrics", "fields", "describe", "sensitive",
           "mask_headers", "mask_url", "mask_form", "mask_text", "http_summary", "http_details", "request_details",
           "sdk_summary", "write_summary", "level_number", "NETWORK", "LEVELS", "FORMATS", "DEFAULT_MAX_CHARS",
           "PROGRESS_SECONDS", "PROGRESS_PAGES", "TEXT_FORMAT", "FIELDS"]

LOG = logging.getLogger("streamwright.source")
NETWORK = logging.getLogger("streamwright.network")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
FORMATS = ("text", "json")
DEFAULT_MAX_CHARS = 20000
# a request that keeps paging logs its progress this often
PROGRESS_SECONDS = 30.0
PROGRESS_PAGES = 100
TEXT_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
# the structured fields of streamwright's log lines, in the order JSON lines show them; each has one type
FIELDS = ("event", "stream", "partition", "window", "request", "export", "exports", "connector", "call", "method",
          "url", "page", "records", "pages", "requests", "retries", "failed_partitions", "streams", "outputs",
          "duration_ms", "status", "code", "attempt", "bytes", "path", "table", "outcome")


def level_number(level):
    """A standard level (a name, any case, NOTSET included, or a number) as a number; None stays None."""
    if level is None or isinstance(level, int):
        return level
    name = str(level).strip().upper()
    if name not in LEVELS + ("NOTSET",):
        raise ValueError("unknown level %r (levels: %s)" % (level, ", ".join(LEVELS)))
    return getattr(logging, name)


def fields(**values):
    """extra= for a log line: its structured fields (FIELDS), without the empty ones."""
    return dict((name, value) for name, value in values.items() if value is not None and value != {})


def _count(number):
    return "{:,}".format(number)


def _ms(seconds):
    return int(round(seconds * 1000))


def _iso(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def describe(where, request=True):
    """
    "stream 'x', request 'y', partition {...}, window 2026-09-01..2026-09-07": the parts `where` has (a mapping with
    stream, request, partition and window keys; `request`: whether to name the request).
    """
    parts = ["stream %r" % where["stream"]] if where.get("stream") else []
    if request and where.get("request"):
        parts.append("request %r" % where["request"])
    if where.get("partition"):
        parts.append("partition %s" % json.dumps(where["partition"], sort_keys=True, default=str))
    if where.get("window"):
        parts.append("window %s..%s" % (where["window"]["start"], where["window"]["end"]))
    return ", ".join(parts)


def _prefix(where):
    text = describe(where) if where else ""
    return text + ": " if text else ""


# * ------------------------------
# * credentials masked by name
# * ------------------------------

_CREDENTIAL_NAMES = ("authorization", "proxy-authorization", "cookie", "set-cookie", "sig")
_CREDENTIAL_PARTS = ("token", "key", "secret", "password", "signature", "credential")
# name=value in URLs (also XML-escaped, &amp;, and JSON-escaped as Go and .NET write & and ?: \u0026, \u003f) and
# other text
_ESCAPED_LEAD = r"\\u00(?:26|3[fF])"
_PARAMETER = re.compile(r"(?P<lead>[?&;]|%s)(?P<name>(?:(?!%s)[^=&;#?\s\"'<>])+)=(?P<value>(?:(?!%s)[^&#\s\"'<>])*)"
                        % ((_ESCAPED_LEAD,) * 3))
_FORM_PARAMETER = re.compile(r"(?P<lead>^|&)(?P<name>[^=&]+)=(?P<value>[^&]*)")
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic)(\s+)([A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE)
# the password of scheme://user:password@host
# (found at its `://`, after a scheme character: a match that started at the scheme, at a word boundary, was tried at
# every part of a long dotted or dashed run, each scanning to its end, which made masking quadratic)
_USERINFO = re.compile(r"(?<=[A-Za-z0-9+.\-])(://[^/?#@:\s\"'<>]*:)([^/?#@\s\"'<>]+)@")


def sensitive(name):
    """
    Whether a header or parameter holds a credential: Authorization, Proxy-Authorization, Cookie, Set-Cookie, sig
    (signed URLs), and names containing token, key, secret, password, signature or credential (developer-token,
    api_key, client_secret, X-Amz-Signature, ...).
    """
    lowered = unquote_plus(str(name)).strip().lower()
    return lowered in _CREDENTIAL_NAMES or any(part in lowered for part in _CREDENTIAL_PARTS)


def _masked_parameter(match):
    value = match.group("value")
    if value and sensitive(match.group("name")):
        after = value[len(value.rstrip(":,.;)]}")):]  # what follows a URL in a sentence stays (masking twice too)
        return "%s%s=***%s" % (match.group("lead"), match.group("name"), after)
    return match.group(0)


def _masked_scheme(match):
    value = match.group(3)
    if len(value) >= 8 and any(character.isdigit() for character in value):
        return "%s%s***" % (match.group(1), match.group(2))
    return match.group(0)


def mask_headers(headers):
    """The headers (a mapping) with the values of credentials (see sensitive) replaced by ***."""
    return collections.OrderedDict((name, "***" if sensitive(name) else value) for name, value in
                                   (headers or {}).items())


def mask_url(url):
    """The URL with its password and the values of its credential parameters (see sensitive) replaced by ***."""
    return mask_text(str(url))


def mask_form(text):
    """A form-encoded body (a=1&b=2) with the values of its credential fields replaced by ***."""
    return _FORM_PARAMETER.sub(_masked_parameter, text)


def mask_text(text):
    """
    Any text with credentials masked by name: the values of credential parameters in URLs (see sensitive), URL
    passwords and Bearer / Basic authorization values. Every log line goes through it (LogSetup), after the run's
    Redactor.
    """
    if "=" in text:
        text = _PARAMETER.sub(_masked_parameter, text)
    if "://" in text:
        text = _USERINFO.sub(r"\1***@", text)
    return _AUTH_SCHEME.sub(_masked_scheme, text)


def _headers_text(headers, redact):
    return redact(json.dumps(mask_headers(dict(headers or {})), default=str))


def _body_text(response):
    try:
        return response.content.decode(response.encoding or "utf-8", "replace")
    except LookupError:  # an unknown charset
        return response.content.decode("utf-8", "replace")


# * ----------------
# * network lines
# * ----------------

def http_summary(method, url, response=None, error=None, seconds=0.0, attempt=1, where=None, size=None):
    """
    The INFO line of one HTTP request on `streamwright.network`: its method, URL (pass it masked and redacted), status (or
    the error's type), response bytes, time and attempt.
    """
    if not NETWORK.isEnabledFor(logging.INFO):
        return
    status = None
    if response is not None:
        status = response.status_code
        if size is None:
            size = len(response.content or b"")
        outcome = "%d, %s bytes" % (status, _count(size))
    else:
        outcome = type(error).__name__ if error is not None else "no response"
    NETWORK.info("%s%s %s: %s, %.2f s, attempt %d", _prefix(where), method, url, outcome, seconds, attempt,
                 extra=fields(event="http_request", method=method, url=url, status=status, bytes=size,
                              duration_ms=_ms(seconds), attempt=attempt, **(where or {})))


def http_details(response, redact, body=True):
    """
    The DEBUG lines of one HTTP exchange of the requests library on `streamwright.network`: the request's method, URL,
    headers and body, then the response's status, headers and (`body`) body. Credentials in headers, URLs and form
    bodies are masked by name and secrets redacted; LogSetup truncates long bodies.
    """
    if not NETWORK.isEnabledFor(logging.DEBUG):
        return
    sent = response.request
    url = redact(mask_url(sent.url or ""))
    content = sent.body
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    if content and "x-www-form-urlencoded" in (sent.headers.get("Content-Type") or ""):
        content = mask_form(content)
    NETWORK.debug("%s %s: request headers %s%s", sent.method, url, _headers_text(sent.headers, redact),
                  "\nrequest body: %s" % redact(content) if content else "")
    NETWORK.debug("%s %s: response %s %s, headers %s%s", sent.method, url, response.status_code, response.reason,
                  _headers_text(response.headers, redact),
                  "\nresponse body: %s" % redact(_body_text(response)) if body else "")


def request_details(method, url, headers, redact):
    """The DEBUG line of a request that got no response: its method, URL (masked and redacted) and headers."""
    if NETWORK.isEnabledFor(logging.DEBUG):
        NETWORK.debug("%s %s: request headers %s", method, url, _headers_text(headers, redact))


def sdk_summary(connector, call, where=None, page=None, records=None, seconds=0.0, attempt=1):
    """
    The INFO line of one response of a connector's SDK request on `streamwright.network`: the connector, service.method, the
    stream, request, partition and window, the page number and its records (for requests that read records), the
    time and the attempt of the call.
    """
    if not NETWORK.isEnabledFor(logging.INFO):
        return
    what = "%s %s" % (connector, call)
    if records is not None:
        what += " page %d: %s record(s)," % (page, _count(records))
    else:
        what += ":"
    NETWORK.info("%s%s %.2f s, attempt %d", _prefix(where), what, seconds, attempt,
                 extra=fields(event="sdk_call", connector=connector, call=call,
                              page=page if records is not None else None, records=records,
                              duration_ms=_ms(seconds), attempt=attempt, **(where or {})))


# * ---------------------
# * formats and handlers
# * ---------------------

def _plain(value, redact):
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return dict((str(key), _plain(item, redact)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [_plain(item, redact) for item in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return redact(str(value))


class JsonFormatter(logging.Formatter):
    """
    One JSON object per line: time (ISO 8601, UTC), level, logger, message, the structured fields (FIELDS) of streamwright's
    lines when they have them, and exception (a traceback) when there is one. `setup`: the LogSetup whose Redactor
    redacts the fields (default: the one in effect; a --log-config file can name this class: `"()":
    streamwright.core.runtime.logs.JsonFormatter`).
    """

    def __init__(self, setup=None):
        super(JsonFormatter, self).__init__()
        self.setup = setup

    def redact(self, text):
        setup = self.setup if self.setup is not None else (_ACTIVE[-1] if _ACTIVE else None)
        redact = getattr(setup, "redact", None)
        return redact(text) if redact is not None else text

    def format(self, record):
        entry = collections.OrderedDict()
        entry["time"] = _iso(datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc))
        entry["level"] = record.levelname
        entry["logger"] = record.name
        entry["message"] = record.getMessage()
        if record.name == "streamwright" or record.name.startswith("streamwright."):  # other libraries' attributes are theirs
            for name in FIELDS:
                value = getattr(record, name, None)
                if value is not None:
                    entry[name] = _plain(value, self.redact)
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        return json.dumps(entry, default=str)


_ACTIVE = []  # the LogSetups in effect, the last one current
_NEW_LOGGER = {"level": logging.NOTSET, "handlers": [], "filters": [], "propagate": True, "disabled": False}


def _loggers():
    """The root logger and every logger made so far."""
    return [logging.getLogger()] + [logger for logger in list(logging.Logger.manager.loggerDict.values())
                                    if isinstance(logger, logging.Logger)]


def _settings():
    """Every logger's settings: level, handlers, filters, propagate, disabled."""
    return dict((logger, {"level": logger.level, "handlers": list(logger.handlers), "filters": list(logger.filters),
                          "propagate": logger.propagate, "disabled": logger.disabled}) for logger in _loggers())


def _restored(before, configured, current):
    """
    The handlers (or filters) to give back to a logger: the ones it had before, minus the ones the setup added, plus
    the ones added while the command ran (by libraries).
    """
    def among(item, items):
        return any(item is other for other in items)
    return [item for item in current if among(item, before) or not among(item, configured)] + \
        [item for item in before if not among(item, configured) and not among(item, current)]


def _dict_config(config):
    """
    Applies a logging.config.dictConfig mapping and returns the handlers it made (also those no logger uses), which
    the setup closes when it ends. The handlers that exist stay open: a full (non-incremental) configuration closes
    every handler (logging.config._clearExistingHandlers), and a closed handler that is put back on its logger
    afterwards loses lines (a FileHandler opened with mode 'w' drops them, a MemoryHandler its target).
    """
    configurator = logging.config.dictConfigClass(config)
    clear = getattr(logging.config, "_clearExistingHandlers", None)
    if clear is not None:
        logging.config._clearExistingHandlers = lambda: None
    try:
        configurator.configure()
    except BaseException:
        for handler in _made_handlers(configurator):
            _close(handler)
        raise
    finally:
        if clear is not None:
            logging.config._clearExistingHandlers = clear
    return _made_handlers(configurator)


def _made_handlers(configurator):
    """The handlers a configurator made (configure() puts each in place of its configuration)."""
    handlers = configurator.config.get("handlers") or {}
    return [handler for handler in dict.values(handlers) if isinstance(handler, logging.Handler)]


def _close(handler):
    try:
        handler.close()
    except Exception:  # (closing a file the config opened)
        pass


def _handlers():
    """The handlers of every logger, each once."""
    found = []
    for logger in _loggers():
        found += [handler for handler in logger.handlers if not any(handler is other for other in found)]
    return found


class _Scrub(logging.Filter):
    """A handler filter: redacts and truncates the lines that were not made while the LogSetup was in effect."""

    def __init__(self, setup):
        super(_Scrub, self).__init__()
        self.setup = setup

    def filter(self, record):
        if not getattr(record, "streamwright_scrubbed", False):
            self.setup.scrub(record)
        return True


class _ToRoot(logging.Handler):
    """
    logging.lastResort while a LogSetup is in effect: a line that reaches no handler, because its logger is below one
    that does not propagate (google-api-core makes the `google` logger so), goes to the root logger's handlers, as if
    it had propagated: `--log google.ads.googleads.client=DEBUG` shows the client's lines.
    """

    def __init__(self, fallback):
        super(_ToRoot, self).__init__(logging.NOTSET)
        self.fallback = fallback

    def handle(self, record):
        handlers = logging.getLogger().handlers or ([self.fallback] if self.fallback is not None else [])
        for handler in handlers:
            if record.levelno >= handler.level:
                handler.handle(record)
        return True

    def emit(self, record):  # (handle() passes the lines on)
        pass


class LogSetup(object):
    """
    The logging of one streamwright command, on Python's standard model (named loggers, standard levels):

    - handlers: streamwright's own, on the root logger, writing `log_format` (text or json) lines to stderr (or `stream`); or,
      with `config` (a logging.config.dictConfig mapping: --log-config), the config's handlers instead. The handlers
      that exist stay open (dictConfig would close them all), and existing loggers stay enabled unless the config says
      `disable_existing_loggers: true` - except the `streamwright` loggers and the ones `loggers` names, which always log.
    - levels: `level` for the `streamwright` loggers (--log-level; default INFO, or the config's); `streamwright.network` WARNING
      (quiet) unless the config sets it, or `level` when that is higher; then `loggers`, (name, level) pairs (--log
      NAME=LEVEL, in order; `root` is the root logger). The root logger is at WARNING when streamwright's handler is its
      first, as with logging.basicConfig.
    - redaction and truncation: every line of every logger, for every handler, is redacted - with `redact`, the run's
      Redactor once run() knows the secrets, then credentials masked by name (mask_text) - and its message cut to
      `max_chars` (0: never). Lines are scrubbed when they are made (a log record factory), and every handler gets a
      filter that scrubs lines made otherwise.
    - lines that reach no handler go to the root logger's handlers (_ToRoot).

    close() puts logging back as it was - what the setup changed: levels, handlers (the ones the config made are
    closed), filters, propagation, disabled loggers - and leaves what libraries changed while the command ran.
    """

    def __init__(self, level=None, log_format="text", loggers=(), config=None, max_chars=DEFAULT_MAX_CHARS,
                 stream=None):
        self.max_chars = max_chars
        self.redact = None
        self.handler = None
        self.filter = _Scrub(self)
        self._before = _settings()  # every logger's settings before the setup ...
        self._configured = {}  # ... and after it
        self._made = []  # the config's handlers
        self._filtered = []
        self._factory = logging.getLogRecordFactory()
        self._last_resort = logging.lastResort
        self._to_root = _ToRoot(self._last_resort)
        _ACTIVE.append(self)
        try:
            self._configure(level_number(level), log_format, loggers, config, stream)
        except BaseException:
            self._configured = _settings()
            self.close()
            raise
        self._configured = _settings()

    def _configure(self, level, log_format, loggers, config, stream):
        root = logging.getLogger()
        if config is not None:
            self._made = _dict_config(dict({"disable_existing_loggers": False}, **config))
        else:
            self.handler = logging.StreamHandler(sys.stderr if stream is None else stream)
            self.handler.setFormatter(JsonFormatter(self) if log_format == "json" else logging.Formatter(TEXT_FORMAT))
            if not root.handlers:  # as logging.basicConfig: other libraries log their warnings and errors
                root.setLevel(logging.WARNING)
            root.addHandler(self.handler)
        streamwright = logging.getLogger("streamwright")
        if level is not None or config is None or streamwright.level == logging.NOTSET:
            streamwright.setLevel(logging.INFO if level is None else level)
        if config is None or NETWORK.level == logging.NOTSET:
            NETWORK.setLevel(logging.WARNING)
        if level is not None and level > NETWORK.level:  # quieter than the network logger: that too
            NETWORK.setLevel(level)
        for name, value in loggers:
            (root if name == "root" else logging.getLogger(name)).setLevel(level_number(value))
        if config is not None:  # (`disable_existing_loggers: true` disables the loggers made before the config)
            for logger in _loggers():
                if logger.name == "streamwright" or logger.name.startswith("streamwright.") or \
                        any(logger.name == name or name == "root" and logger is root for name, _ in loggers):
                    logger.disabled = False
        for handler in _handlers():
            handler.addFilter(self.filter)
            self._filtered.append(handler)
        logging.lastResort = self._to_root
        logging.setLogRecordFactory(self._make_record)

    def clean(self, text):
        """The text redacted (the run's Redactor) and with credentials masked by name."""
        if self.redact is not None:
            text = self.redact(text)
        return mask_text(text)

    def message(self, text):
        """A message as log lines show it: clean, then cut to `max_chars`."""
        message = self.clean(text)
        if self.max_chars and len(message) > self.max_chars:
            message = "%s... [truncated %d chars]" % (message[:self.max_chars], len(message) - self.max_chars)
        return message

    def scrub(self, record):
        """Redacts a log record's message, traceback and stack, and truncates its message (once)."""
        try:
            message = record.getMessage()
        except Exception:  # arguments that do not fit the message: both are shown, redacted
            message = "%s %r" % (record.msg, record.args)
        record.msg, record.args = self.message(message), ()
        if record.exc_info and not record.exc_text:
            # formatters reuse exc_text, so the redacted traceback is what every handler writes
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.clean(record.exc_text)
        if record.stack_info:
            record.stack_info = self.clean(record.stack_info)
        record.streamwright_scrubbed = True

    def _make_record(self, *args, **kwargs):
        record = self._factory(*args, **kwargs)
        try:
            self.scrub(record)
        except Exception:  # never a line that may hold a secret
            record.msg, record.args, record.exc_info, record.exc_text = "(a log line that could not be redacted)", \
                (), None, None
            record.streamwright_scrubbed = True
        return record

    def close(self):
        if logging.getLogRecordFactory() == self._make_record:
            logging.setLogRecordFactory(self._factory)
        if logging.lastResort is self._to_root:
            logging.lastResort = self._last_resort
        for handler in self._filtered:
            handler.removeFilter(self.filter)
        current = _settings()
        for logger, configured in self._configured.items():
            before, now = self._before.get(logger, _NEW_LOGGER), current.get(logger)
            if now is None:
                continue
            if now["level"] == configured["level"]:  # (else a library set it while the command ran: it stays)
                logger.setLevel(before["level"])
            for name in ("propagate", "disabled"):
                if now[name] == configured[name]:
                    setattr(logger, name, before[name])
            logger.handlers = _restored(before["handlers"], configured["handlers"], now["handlers"])
            logger.filters = _restored(before["filters"], configured["filters"], now["filters"])
        for handler in self._made:
            _close(handler)
        for handler in [self.handler] + self._made:
            try:
                if handler is not None:
                    handler.flush()
            except (OSError, ValueError):
                pass
        self._filtered, self._made = [], []
        if self in _ACTIVE:
            _ACTIVE.remove(self)


# * -------------
# * run metrics
# * -------------

def _retries(count):
    return "%s retr%s" % (_count(count), "y" if count == 1 else "ies")


def _breakdown(counts):
    """" (a: 1, b: 2,345)" for a mapping of counts."""
    return " (%s)" % ", ".join("%s: %s" % (name, _count(count)) for name, count in counts.items()) if counts else ""


def _seconds(value):
    return None if value is None else round(value, 3)


class StreamMetrics(object):
    """One stream's counts (RunMetrics.start_stream); as_dict() is its entry in the run summary."""

    def __init__(self, name, mode, partitions, started):
        self.name = name
        self.mode = mode
        self.partitions = partitions
        self.failed_partitions = 0
        self.windows = 0  # the windows read, over all partitions (incremental streams)
        self.pages = 0
        self.requests = collections.OrderedDict()  # request name -> HTTP requests or SDK calls sent, retries included
        self.retries = 0
        self.records_read = 0  # the records of all its requests' pages
        self.exports = collections.OrderedDict()  # export -> records written
        self.started = started
        self.duration = None

    def as_dict(self):
        return collections.OrderedDict([
            ("name", self.name), ("mode", self.mode), ("partitions", self.partitions),
            ("failed_partitions", self.failed_partitions), ("windows", self.windows), ("pages", self.pages),
            ("requests", dict(self.requests)), ("retries", self.retries), ("records_read", self.records_read),
            ("exports", dict(self.exports)), ("duration_s", _seconds(self.duration))])


class _Read(object):
    """
    The pages one request reads for a unit of work - a window or partition of a page-mode stream, a stream partition
    of a run-mode request: progress lines while it goes, a line when it completes.
    """

    def __init__(self, metrics, where):
        self.metrics = metrics
        self.where = where
        self.stats = metrics.streams.get(where.get("stream"))
        self.pages = self.records = 0
        self.started = self.reported_at = metrics.clock()
        self.reported_pages = 0

    def count(self, pages):
        """The pages (lists of records), counted as they go."""
        for page in pages:
            self.page(len(page))
            yield page

    def page(self, records):
        self.pages += 1
        self.records += records
        if self.stats is not None:
            self.stats.pages += 1
            self.stats.records_read += records
        now = self.metrics.clock()
        if self.pages - self.reported_pages >= self.metrics.progress_pages or \
                now - self.reported_at >= self.metrics.progress_seconds:
            self.reported_pages, self.reported_at = self.pages, now
            LOG.info("stream %r, request %r: %s page(s), %s record(s) so far", self.where.get("stream"),
                     self.where.get("request"), _count(self.pages), _count(self.records),
                     extra=fields(event="progress", pages=self.pages, records=self.records,
                                  duration_ms=_ms(now - self.started), **self.where))

    def done(self, request=True):
        """
        Logs the completed unit: its pages, records and time (`request`: whether the line names the request). A
        stream's only unit - no partition values, no window - has none: the stream's end says it all.
        """
        if not self.where.get("partition") and not self.where.get("window"):
            return
        seconds = self.metrics.clock() - self.started
        LOG.info("%s: %s page(s), %s record(s), %.1f s", describe(self.where, request), _count(self.pages),
                 _count(self.records), seconds, extra=fields(event="read", pages=self.pages, records=self.records,
                                                             duration_ms=_ms(seconds), **self.where))


class RunMetrics(object):
    """
    A run's counts and timings, in one place. The runner reports its streams (start_stream, end_stream), the reads of
    their requests (read), records written and bookmarks; the HTTP clients and the connector context count the requests
    they send (request). It logs at INFO each stream's start and end, completed windows and partitions, the progress
    of reads that keep paging - every `progress_seconds` or `progress_pages` pages - and the run's end (finish), and
    summary() is the run summary (--summary FILE). `clock`: monotonic seconds.
    """

    def __init__(self, clock=time.monotonic, progress_seconds=PROGRESS_SECONDS, progress_pages=PROGRESS_PAGES):
        self.clock = clock
        self.progress_seconds = progress_seconds
        self.progress_pages = progress_pages
        self.started_at = _now()
        self.started = clock()
        self.finished_at = None
        self.duration = None
        self.status = None
        self.source = None
        self.streams = collections.OrderedDict()  # name -> StreamMetrics
        self.outputs = []  # the output's summary() entries
        self.state = None  # the last state the run wrote
        self.error = None

    def start_stream(self, name, mode, partitions, requests):
        """A stream starts: `partitions` (a number) and the names of its `requests`."""
        stats = self.streams[name] = StreamMetrics(name, mode, partitions, self.clock())
        LOG.info("stream %r: starting (mode %s, %d partition(s), requests: %s)", name, mode, partitions,
                 ", ".join(requests) or "none", extra=fields(event="stream_start", stream=name))
        return stats

    def end_stream(self, name, completed=True):
        """A stream ends; one that `completed` logs its records written, requests, retries and failed partitions."""
        stats = self.streams.get(name)
        if stats is None:
            return
        stats.duration = self.clock() - stats.started
        if not completed:
            return
        written, requests = sum(stats.exports.values()), sum(stats.requests.values())
        LOG.info("stream %r: %s record(s) written%s, %s request(s)%s, %s, %s failed partition(s), %.1f s", name,
                 _count(written), _breakdown(stats.exports), _count(requests), _breakdown(stats.requests),
                 _retries(stats.retries), _count(stats.failed_partitions), stats.duration,
                 extra=fields(event="stream_end", stream=name, records=written, exports=dict(stats.exports),
                              pages=stats.pages, requests=requests, retries=stats.retries,
                              failed_partitions=stats.failed_partitions, duration_ms=_ms(stats.duration)))

    def read(self, where):
        """A unit of work of a request starts (`where`: its stream, request, partition and window)."""
        return _Read(self, where)

    def request(self, where, retry=False):
        """An HTTP request or SDK call was sent for `where` (its stream and request); `retry`: it retries one."""
        stats = self.streams.get((where or {}).get("stream"))
        if stats is None:
            return
        name = where.get("request")
        stats.requests[name] = stats.requests.get(name, 0) + 1
        if retry:
            stats.retries += 1

    def exports(self):
        """Records written per export, over all streams."""
        written = collections.OrderedDict()
        for stats in self.streams.values():
            for export, count in stats.exports.items():
                written[export] = written.get(export, 0) + count
        return written

    def finish(self, status, log=True):
        """The run ends: `status` is ok, failed or interrupted; `log`: log the run's end line."""
        self.status = status
        self.finished_at = _now()
        self.duration = self.clock() - self.started
        if not log:
            return
        streams = list(self.streams.values())
        written = self.exports()
        requests = sum(sum(stats.requests.values()) for stats in streams)
        retries = sum(stats.retries for stats in streams)
        failed = sum(stats.failed_partitions for stats in streams)
        LOG.info("run %s %.1f s: %d stream(s), %s record(s) written%s, %s request(s), %s, %s failed partition(s), "
                 "%d output(s) written", "finished in" if status == "ok" else status + " after", self.duration,
                 len(streams), _count(sum(written.values())), _breakdown(written), _count(requests),
                 _retries(retries), _count(failed), len(self.outputs),
                 extra=fields(event="run_end", outcome=status, streams=len(streams), records=sum(written.values()),
                              exports=dict(written), requests=requests, retries=retries, failed_partitions=failed,
                              outputs=len(self.outputs), duration_ms=_ms(self.duration)))

    def summary(self):
        """
        The run summary: status (ok or failed), started_at, finished_at, duration_s, source, streams (StreamMetrics),
        outputs (the output's summary() entries), state (the bookmarks the run wrote), and error when it failed.
        """
        if self.finished_at is None:
            self.finish(self.status or "failed", log=False)
        result = collections.OrderedDict([
            ("status", "ok" if self.status == "ok" else "failed"), ("started_at", _iso(self.started_at)),
            ("finished_at", _iso(self.finished_at)), ("duration_s", _seconds(self.duration)), ("source", self.source),
            ("streams", [stats.as_dict() for stats in self.streams.values()]), ("outputs", list(self.outputs)),
            ("state", self.state)])
        if self.status != "ok":
            result["error"] = self.error or ("interrupted" if self.status == "interrupted" else "the run failed")
        return result


def write_summary(path, summary):
    """Writes the run summary as JSON to `path`, atomically (a temporary file in its folder, then renamed)."""
    folder = os.path.dirname(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".part", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2, default=str)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
