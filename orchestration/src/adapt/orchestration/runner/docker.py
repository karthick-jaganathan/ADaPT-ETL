"""
ADAPT_EXECUTION=docker: a node's logical argv as a `docker run --rm` of the network's image (docker_command): the host
paths are translated to the container's (/app source folders, /warehouse and /runs mounts) and each secret is passed as
`-e ADAPT_SECRET_<NAME>` WITHOUT a value - the value is only in the `docker` process' environment, which docker copies
into the container - so it is never in the argv, the image or the log.
"""

import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path, PurePosixPath

from adapt.orchestration.runner.common import (CONTAINER_REPO_ROOT, DockerCommandError, _check_secret_env,
                                               container_path, image_value, redact, split_duckdb_output)
from adapt.orchestration.settings import REPO_ROOT, docker_bin
from adapt.orchestration.settings import runs_dir as default_runs_dir
from adapt.orchestration.settings import warehouse_dir as default_warehouse_dir

CONTAINER_WAREHOUSE = PurePosixPath("/warehouse")  # -v <host warehouse dir>:/warehouse
CONTAINER_RUNS = PurePosixPath("/runs")            # -v <host runs dir>:/runs
DOCKER_HOST_GATEWAY = "host.docker.internal:host-gateway"


def container_output(value, warehouse_dir):
    """`duckdb:<host warehouse file>[:<schema>]` -> `duckdb:/warehouse/<file>[:<schema>]`; other outputs unchanged."""
    if not value.startswith("duckdb:"):
        return value
    path, schema = split_duckdb_output(value)
    translated = "duckdb:%s" % container_path(path, warehouse_dir, CONTAINER_WAREHOUSE)
    return translated if schema is None else "%s:%s" % (translated, schema)


def container_name(*parts):
    """A readable, docker-valid container name: adapt-pipe-<part>-<part>-..."""
    name = "-".join(["adapt-pipe"] + [str(part) for part in parts if part])
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", name)[:128]


def docker_command(argv, secret_env, image, warehouse_dir, runs_dir, repo_root=REPO_ROOT, name=None, environ=None,
                   docker=None):
    """
    The `docker run` argv of a node's logical argv `<adapt> run <source> --flag value ...` (with its --summary), run in
    a --rm container of `image` (whose ENTRYPOINT is adapt):

    - the source folder and <repo root> paths in --set values become /app/... (the image ships the source folders at
      their repository-relative paths);
    - the warehouse dir is mounted at /warehouse and the runs dir at /runs, and --output duckdb:... and --summary are
      rewritten to them, so the DuckDB file and the --summary JSON persist on the host;
    - every secret is `-e ADAPT_SECRET_<NAME>` WITHOUT a value: docker copies the value from its own environment
      (run() sets it there), so no secret value is ever in this argv;
    - other flags (--stream, --timezone, --allow-connector, ...) are passed through unchanged.

    $ADAPT_DOCKER_ARGS adds docker run options (e.g. "--user 1000:1000 --network adapt"), before the image.
    """
    environ = os.environ if environ is None else environ
    _check_secret_env(secret_env)
    if not image:
        raise DockerCommandError("ADAPT_EXECUTION=docker needs an image: set the network's `image:` in networks.yaml "
                                 "or $ADAPT_IMAGE")
    argv = [str(arg) for arg in argv]
    if len(argv) < 3 or argv[1] != "run":
        raise DockerCommandError("expected `<adapt> run <source> ...`, got %r" % argv[:3])
    warehouse_dir, runs_dir = Path(warehouse_dir).resolve(), Path(runs_dir).resolve()
    run_args = ["run", container_path(argv[2], repo_root, CONTAINER_REPO_ROOT)]
    rest = argv[3:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in ("--set", "--output", "--summary") and index + 1 < len(rest):
            value = rest[index + 1]
            if token == "--set":
                value = image_value(value, repo_root)
            elif token == "--output":
                value = container_output(value, warehouse_dir)
            else:
                value = container_path(value, runs_dir, CONTAINER_RUNS)
            run_args += [token, value]
            index += 2
        else:
            run_args.append(token)
            index += 1

    command = [docker or docker_bin(environ), "run", "--rm", "--init", "--pull", "never"]
    if name:
        command += ["--name", name]
    command += ["--add-host", DOCKER_HOST_GATEWAY,
                "-v", "%s:%s" % (warehouse_dir, CONTAINER_WAREHOUSE),
                "-v", "%s:%s" % (runs_dir, CONTAINER_RUNS)]
    for variable in sorted(secret_env):
        command += ["-e", variable]
    command += shlex.split(environ.get("ADAPT_DOCKER_ARGS") or "")
    command += [image] + run_args
    for value in secret_env.values():
        if value and any(value in arg for arg in command):
            raise DockerCommandError("a secret value would appear on the docker command line; refusing to run it")
    return command


def _remove_container(docker, name):
    """Best effort: a killed `docker run` client does not stop its container, so remove it (and stop adapt)."""
    try:
        subprocess.run([docker, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                       check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def docker_run_command(logical, secret_env, log, image, warehouse_dir=None, runs_dir=None, repo_root=None, name=None):
    """
    run()'s docker mode: (the `docker run` argv of the logical argv, the docker CLI, the container name), the
    warehouse dir created; warehouse_dir/runs_dir/repo_root default to the pipeline's. Logs the (redacted) argv.
    """
    secrets = [value for value in secret_env.values() if value]
    docker = docker_bin()
    name = name or container_name(uuid.uuid4().hex[:12])
    warehouse_dir = Path(warehouse_dir or default_warehouse_dir())
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    command = docker_command(logical, secret_env, image, warehouse_dir, runs_dir or default_runs_dir(),
                             repo_root or REPO_ROOT, name=name, docker=docker)
    log.info("docker: %s" % redact(shlex.join(command), secrets))
    return command, docker, name
