"""
Runs one node's `adapt run` in one of three execution modes ($ADAPT_EXECUTION, see
adapt.orchestration.settings.execution_mode):

- subprocess (default): the argv as a local subprocess with the secrets added to ITS environment only;
- docker: the same logical argv in a `docker run --rm` container of the network's image (docker_command): the host
  paths are translated to the container's (/app source folders, /warehouse and /runs mounts) and each secret is passed
  as `-e ADAPT_SECRET_<NAME>` WITHOUT a value - the value is only in the `docker` process' environment, which docker
  copies into the container - so it is never in the argv, the image or the log;
- k8s: the same logical argv as a Kubernetes Job (one pod) of the network's image (k8s_job_manifest, run_k8s): the
  source is /app/..., the output is the DuckLake warehouse on object storage (a Postgres catalog, data files on S3) and
  every secret - the source's ADAPT_SECRET_* and the S3/catalog credentials - comes from a Kubernetes Secret through
  envFrom: the pipeline sends no secret value at all, only the Secret's name.

Either way adapt's output is streamed line by line to the logger (with any secret value redacted). In subprocess and
docker modes the --summary JSON adapt always writes (on the host: /runs is a mount) is read into a small result; in k8s
mode the counts come from adapt's own log lines (the pod's --summary would die with it). A non-zero exit code raises
RunFailed with the error.

run() is the entry point; the modes' helpers are in common.py, docker.py and k8s.py (all re-exported here).
"""

import os
import shlex
import subprocess
import time
from pathlib import Path

from adapt.orchestration.runner.common import (CONTAINER_REPO_ROOT, LOG_LEVEL, REDACTED, DockerCommandError,
                                               RunFailed, _check_secret_env, _log_line, container_path,
                                               image_value, read_summary, redact, split_duckdb_output)
from adapt.orchestration.runner.docker import (CONTAINER_RUNS, CONTAINER_WAREHOUSE, DOCKER_HOST_GATEWAY,
                                               _remove_container, container_name, container_output, docker_command,
                                               docker_run_command)
from adapt.orchestration.runner.k8s import (K8S_CONTAINER, K8S_JOB_TTL_S, K8S_LABEL, K8S_POD_FATAL, RUN_END,
                                            STREAM_WRITTEN, K8sJobError, counts_from_log, delete_k8s_job,
                                            ducklake_output, k8s_catalog_schema, k8s_command, k8s_data_path,
                                            k8s_job_manifest, k8s_job_name, k8s_label_value, k8s_secret_keys, run_k8s)
from adapt.orchestration.settings import REPO_ROOT

__all__ = ["CONTAINER_REPO_ROOT", "CONTAINER_RUNS", "CONTAINER_WAREHOUSE", "DOCKER_HOST_GATEWAY", "K8S_CONTAINER",
           "K8S_JOB_TTL_S", "K8S_LABEL", "K8S_POD_FATAL", "LOG_LEVEL", "REDACTED", "REPO_ROOT", "RUN_END",
           "STREAM_WRITTEN", "DockerCommandError", "K8sJobError", "RunFailed", "container_name", "container_output",
           "container_path", "counts_from_log", "delete_k8s_job", "docker_command", "docker_run_command",
           "ducklake_output", "image_value", "k8s_catalog_schema", "k8s_command", "k8s_data_path", "k8s_job_manifest",
           "k8s_job_name", "k8s_label_value", "k8s_secret_keys", "read_summary", "redact", "run", "run_k8s",
           "split_duckdb_output"]


def run(argv, secret_env, log, summary_path, mode="subprocess", image=None, warehouse_dir=None, runs_dir=None,
        repo_root=None, name=None, user=None, labels=None, settings=None):
    """
    Runs argv (+ --summary summary_path) - locally (mode `subprocess`), in a `docker run --rm` of `image` (mode
    `docker`, see docker_command; warehouse_dir/runs_dir/repo_root default to the pipeline's) or as a Kubernetes Job of
    `image` (mode `k8s`, see run_k8s: `user` picks the DuckLake data path, `labels` label the Job). Returns
    {status, exit_code, records, streams, duration_s, execution, ...}.
    """
    _check_secret_env(secret_env)
    summary_path = Path(summary_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    if summary_path.exists():
        summary_path.unlink()
    if mode == "k8s":
        return run_k8s(argv, secret_env, log, summary_path, image, user, name=name, labels=labels, settings=settings,
                       repo_root=repo_root)
    logical = list(argv) + ["--summary", str(summary_path)]
    secrets = [value for value in secret_env.values() if value]
    docker = None
    if mode == "subprocess":
        command = logical
    elif mode == "docker":
        command, docker, name = docker_run_command(logical, secret_env, log, image, warehouse_dir, runs_dir, repo_root,
                                                   name)
    else:
        raise ValueError("unknown execution mode %r (subprocess, docker or k8s)" % mode)
    env = {**os.environ, **secret_env}
    started = time.time()
    with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          bufsize=1) as process:
        try:
            for line in process.stdout:
                line = line.rstrip("\n")
                if line:
                    _log_line(log, redact(line, secrets))
            exit_code = process.wait()
        except BaseException:
            process.kill()
            if docker:
                _remove_container(docker, name)
            raise
    summary = read_summary(summary_path) or {}
    records = {}
    for stream in summary.get("streams") or []:
        records.update(stream.get("exports") or {})
    result = {"status": summary.get("status") or ("ok" if exit_code == 0 else "failed"), "exit_code": exit_code,
              "records": records, "streams": [s.get("name") for s in summary.get("streams") or []],
              "duration_s": round(time.time() - started, 3), "started_at": started, "summary_path": str(summary_path),
              "execution": mode}
    if docker:
        result.update(image=image, container=name, docker_command=shlex.join(command))
    if exit_code != 0:
        error = redact(str(summary.get("error") or "no summary was written"), secrets)
        raise RunFailed("adapt exited with %d: %s" % (exit_code, error), result)
    return result
