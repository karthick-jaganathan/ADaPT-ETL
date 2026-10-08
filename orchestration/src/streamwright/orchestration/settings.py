"""
The settings of the orchestration project: its paths (PROJECT_DIR = orchestration/, REPO_ROOT, the config/ files,
warehouse/ and runs/) and the environment variables that override them, the streamwright CLI and the execution mode
(subprocess, docker or k8s) with its settings. Every value is read from the environment when it is asked for.
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[3]  # orchestration/ (this file: orchestration/src/streamwright/orchestration/)
REPO_ROOT = Path(__file__).resolve().parents[4]


def pipelines_dir():
    """The folder of named pipelines (<name>.yaml each): $STREAMWRIGHT_PIPELINE_DIR, default <project dir>/config/pipelines."""
    return Path(os.environ.get("STREAMWRIGHT_PIPELINE_DIR") or PROJECT_DIR / "config" / "pipelines")


def pipeline_spec_override():
    """$STREAMWRIGHT_PIPELINE_SPEC: one pipeline file used INSTEAD of the pipelines folder (None when unset)."""
    value = os.environ.get("STREAMWRIGHT_PIPELINE_SPEC")
    return Path(value) if value else None


def networks_path():
    """The networks file: $STREAMWRIGHT_PIPELINE_NETWORKS, default <project dir>/config/networks.yaml."""
    return Path(os.environ.get("STREAMWRIGHT_PIPELINE_NETWORKS") or PROJECT_DIR / "config" / "networks.yaml")


def warehouse_dir():
    """Where the DuckDB warehouses go: $STREAMWRIGHT_PIPELINE_WAREHOUSE_DIR, default <project dir>/warehouse."""
    return Path(os.environ.get("STREAMWRIGHT_PIPELINE_WAREHOUSE_DIR") or PROJECT_DIR / "warehouse")


def runs_dir():
    """Where each node's --summary JSON goes: $STREAMWRIGHT_PIPELINE_RUNS_DIR, default <project dir>/runs."""
    return Path(os.environ.get("STREAMWRIGHT_PIPELINE_RUNS_DIR") or PROJECT_DIR / "runs")


def streamwright_bin():
    """The streamwright CLI: $STREAMWRIGHT_BIN, default /tmp/streamwright-sdk-venv/bin/streamwright."""
    return os.environ.get("STREAMWRIGHT_BIN") or "/tmp/streamwright-sdk-venv/bin/streamwright"


EXECUTION_MODES = ("subprocess", "docker", "k8s")


def execution_mode(environ=None):
    """
    Where a node's `streamwright run` runs: $STREAMWRIGHT_EXECUTION - `subprocess` (default, the local CLI), `docker` (a `docker run`
    of the network's image) or `k8s` (a Kubernetes Job of the network's image, see k8s_settings).
    """
    environ = os.environ if environ is None else environ
    mode = (environ.get("STREAMWRIGHT_EXECUTION") or "subprocess").strip().lower()
    if mode not in EXECUTION_MODES:
        raise ValueError("STREAMWRIGHT_EXECUTION must be one of %s, got %r" % (", ".join(EXECUTION_MODES), mode))
    return mode


def streamwright_image(network_image=None, environ=None):
    """The container image of a node in docker mode: $STREAMWRIGHT_IMAGE, else the network's networks.yaml `image:`."""
    environ = os.environ if environ is None else environ
    return environ.get("STREAMWRIGHT_IMAGE") or network_image


def docker_bin(environ=None):
    """The docker CLI: $STREAMWRIGHT_DOCKER_BIN, default `docker` (on the PATH)."""
    environ = os.environ if environ is None else environ
    return environ.get("STREAMWRIGHT_DOCKER_BIN") or "docker"


# STREAMWRIGHT_EXECUTION=k8s: environment variable -> (setting, default). The defaults match the in-cluster simulation of
# orchestration/k8s/ (setup.sh): LocalStack for S3 and Postgres for the DuckLake catalog, in namespace streamwright.
# None of these is a secret: the credentials are in the Kubernetes Secret (`secret`), which each Job pod gets with
# envFrom - the pipeline never reads or sends them.
K8S_SETTINGS = {
    "STREAMWRIGHT_KUBECTL_BIN": ("kubectl", "kubectl"),
    "STREAMWRIGHT_K8S_CONTEXT": ("context", "kind-streamwright"),
    "STREAMWRIGHT_K8S_NAMESPACE": ("namespace", "streamwright"),
    "STREAMWRIGHT_K8S_SECRET": ("secret", "streamwright-secrets"),
    "STREAMWRIGHT_K8S_CATALOG": ("catalog", "postgres:dbname=streamwrightcat host=catalog-postgres.streamwright.svc.cluster.local "
                                     "port=5432 user=streamwright"),
    "STREAMWRIGHT_K8S_DATA_ROOT": ("data_root", "s3://streamwright-warehouse"),
    "STREAMWRIGHT_K8S_S3_ENDPOINT": ("s3_endpoint", "localstack.streamwright.svc.cluster.local:4566"),
    "STREAMWRIGHT_K8S_S3_URL_STYLE": ("s3_url_style", "path"),
    "STREAMWRIGHT_K8S_S3_USE_SSL": ("s3_use_ssl", "false"),
    "STREAMWRIGHT_K8S_S3_REGION": ("s3_region", "us-east-1"),
    "STREAMWRIGHT_K8S_TIMEOUT": ("timeout_s", "1800"),         # the Job's activeDeadlineSeconds
    "STREAMWRIGHT_K8S_START_TIMEOUT": ("start_timeout_s", "300"),  # how long a pod may take to start its container
    "STREAMWRIGHT_K8S_KEEP_JOBS": ("keep_jobs", ""),           # 1: keep each finished Job (its TTL removes it later)
}
# The Secret keys every Job needs besides the source's STREAMWRIGHT_SECRET_* ones (S3 credentials, the catalog's password).
K8S_REQUIRED_SECRET_KEYS = ("STREAMWRIGHT_DUCKLAKE_S3_KEY_ID", "STREAMWRIGHT_DUCKLAKE_S3_SECRET", "STREAMWRIGHT_DUCKLAKE_CATALOG_PASSWORD")


def k8s_settings(environ=None):
    """
    The settings of STREAMWRIGHT_EXECUTION=k8s ($STREAMWRIGHT_K8S_* / $STREAMWRIGHT_KUBECTL_BIN, see K8S_SETTINGS): the kubectl context and
    namespace the Jobs run in, the Secret their pods get, the DuckLake catalog DSN (Postgres; no password), the data
    root (each user's data goes to <data_root>/<user>/), the S3 endpoint settings and the timeouts.
    """
    environ = os.environ if environ is None else environ
    settings = {key: (environ.get(variable) or default) for variable, (key, default) in K8S_SETTINGS.items()}
    for key in ("timeout_s", "start_timeout_s"):
        settings[key] = int(settings[key])
    settings["keep_jobs"] = settings["keep_jobs"].strip().lower() in ("1", "true", "yes")
    settings["data_root"] = settings["data_root"].rstrip("/")
    return settings
