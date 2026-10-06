"""
What the execution modes share: RunFailed, redacting secret values, adapt's log lines to the logger, the --summary
JSON, the secret_env check and the host -> /app path translation of the container modes (docker, k8s).
"""

import json
import re
from pathlib import Path, PurePosixPath

from adapt.orchestration.wrappers import SECRET_ENV_PREFIX

LOG_LEVEL = re.compile(r"^\[[^\]]*\]\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
REDACTED = "***"

CONTAINER_REPO_ROOT = PurePosixPath("/app")      # the image ships <repo>/examples/sources at /app/examples/sources


class RunFailed(RuntimeError):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class DockerCommandError(ValueError):
    """A node's argv cannot be translated to a `docker run` (a path outside its mount, no image...)."""


def redact(text, secrets):
    for value in secrets:
        if value:
            text = text.replace(value, REDACTED)
    return text


def _level(line):
    match = LOG_LEVEL.match(line)
    return match.group(1) if match else "INFO"


def _log_line(log, line):
    level = _level(line)
    if level in ("ERROR", "CRITICAL"):
        log.error(line)
    elif level == "WARNING":
        log.warning(line)
    elif level == "DEBUG":
        log.debug(line)
    else:
        log.info(line)


def read_summary(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _check_secret_env(secret_env):
    bad = [name for name in secret_env if not name.startswith(SECRET_ENV_PREFIX)]
    if bad:
        raise ValueError("secret_env may only hold %s* variables, got %s" % (SECRET_ENV_PREFIX, ", ".join(bad)))


def container_path(host_path, host_root, container_root):
    """A host path under host_root -> the same relative path under container_root (else DockerCommandError)."""
    path, root = Path(host_path).resolve(), Path(host_root).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise DockerCommandError("%s is outside %s, so it is not visible in the container at %s" % (
            path, root, container_root))
    return str(container_root.joinpath(*relative.parts))


def image_value(value, repo_root):
    """A --set value with every <repo root> path rewritten to its /app path (other values are unchanged)."""
    roots = {str(Path(repo_root)), str(Path(repo_root).resolve())}
    for root in sorted(roots, key=len, reverse=True):
        value = re.sub(re.escape(root) + r"(?=/|,|$)", str(CONTAINER_REPO_ROOT), value)
    return value


def split_duckdb_output(value):
    """`duckdb:<path>[:<schema>]` -> (path, schema or None)."""
    body = value[len("duckdb:"):]
    path, sep, schema = body.rpartition(":")
    if not sep or "/" in schema or not path:
        path, schema = body, None
    return path, schema
