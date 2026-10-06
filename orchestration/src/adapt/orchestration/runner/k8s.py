"""
ADAPT_EXECUTION=k8s: a node's logical argv as a Kubernetes Job (one pod) of the network's image (k8s_job_manifest,
run_k8s): the source is /app/..., the output is the DuckLake warehouse on object storage (a Postgres catalog, data files
on S3) and every secret - the source's ADAPT_SECRET_* and the S3/catalog credentials - comes from a Kubernetes Secret
through envFrom: the pipeline sends no secret value at all, only the Secret's name. The counts come from adapt's own
log lines (the pod's --summary would die with it).
"""

import hashlib
import json
import re
import shlex
import subprocess
import time
import uuid

from adapt.orchestration.runner.common import (CONTAINER_REPO_ROOT, DockerCommandError, RunFailed, _check_secret_env,
                                               _level, _log_line, container_path, image_value, redact,
                                               split_duckdb_output)
from adapt.orchestration.settings import K8S_REQUIRED_SECRET_KEYS, REPO_ROOT, k8s_settings

K8S_CONTAINER = "adapt"
K8S_JOB_TTL_S = 3600         # a Job the runner could not delete (or ADAPT_K8S_KEEP_JOBS kept) goes after an hour
K8S_LABEL = "adapt-pipeline/"
K8S_POD_FATAL = {"ErrImageNeverPull", "ErrImagePull", "ImagePullBackOff", "InvalidImageName",
                 "CreateContainerConfigError", "CreateContainerError"}
# adapt's end-of-stream and end-of-run log lines (adapt.core.runtime.logs): the counts of a k8s run
STREAM_WRITTEN = re.compile(r"\bstream '([^']+)': ([\d,]+) record\(s\) written(?: \(([^)]*)\))?")
RUN_END = re.compile(r"\brun (finished in|failed after|interrupted after) [\d.]+ s: (\d+) stream\(s\), ([\d,]+) "
                     r"record\(s\) written(?: \(([^)]*)\))?")
_DSN_PASSWORD = re.compile(r"(^|[\s?&])password\s*=", re.IGNORECASE)


class K8sJobError(ValueError):
    """A node's Kubernetes Job cannot be built or run (no image, a missing Secret key, a pod that cannot start...)."""


def _identifier(text):
    return re.sub(r"[^a-z0-9_]", "_", str(text).lower())


def k8s_job_name(*parts):
    """A unique, DNS-1123 Job name: adapt-<part>-<part>-... (63 chars at most; a long one ends with a hash)."""
    name = "-".join(["adapt"] + [str(part) for part in parts if part])
    name = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", name.lower())).strip("-")
    if len(name) > 63:
        name = "%s-%s" % (name[:54].rstrip("-"), hashlib.sha1(name.encode()).hexdigest()[:8])
    return name


def k8s_label_value(value):
    """A valid label value (63 chars of [A-Za-z0-9_.-], alphanumeric at both ends)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(value))[:63].strip("-_.") or "none"


def k8s_data_path(user, settings):
    """Where a user's DuckLake data files go: <data root>/<user>/ (e.g. s3://adapt-warehouse/u1/)."""
    return "%s/%s/" % (settings["data_root"], _identifier(user))


def k8s_catalog_schema(user):
    """The Postgres schema of a user's DuckLake catalog tables (one catalog per user, as one DATA_PATH per user)."""
    return "lake_%s" % _identifier(user)


def ducklake_output(value, catalog):
    """`--output duckdb:<file>[:<schema>]` -> `ducklake:<catalog>[:<schema>]` (the same schema, in the DuckLake)."""
    if value.startswith("ducklake:"):
        return value
    if not value.startswith("duckdb:"):
        raise K8sJobError("ADAPT_EXECUTION=k8s writes the warehouse to DuckLake: --output %r would stay in the pod"
                          % value.partition(":")[0])
    _, schema = split_duckdb_output(value)
    return "ducklake:%s" % catalog if schema is None else "ducklake:%s:%s" % (catalog, schema)


def k8s_command(argv, catalog, repo_root=REPO_ROOT):
    """
    The pod's command of a node's logical argv `<adapt> run <source> --flag value ...`: `adapt run /app/<source> ...`,
    with <repo root> paths in --set values as /app/..., --output duckdb:... as the DuckLake output on `catalog` (same
    schema) and no --summary (the pod's file system dies with it). Other flags are passed through unchanged.
    """
    argv = [str(arg) for arg in argv]
    if len(argv) < 3 or argv[1] != "run":
        raise K8sJobError("expected `<adapt> run <source> ...`, got %r" % argv[:3])
    if _DSN_PASSWORD.search(catalog):
        raise K8sJobError("the DuckLake catalog DSN may not hold a password (it is in the Secret)")
    try:
        command = ["adapt", "run", container_path(argv[2], repo_root, CONTAINER_REPO_ROOT)]
    except DockerCommandError as error:
        raise K8sJobError(str(error))
    rest, index, output = argv[3:], 0, False
    while index < len(rest):
        token = rest[index]
        if token in ("--set", "--output", "--summary") and index + 1 < len(rest):
            value = rest[index + 1]
            if token == "--set":
                command += [token, image_value(value, repo_root)]
            elif token == "--output":
                command += [token, ducklake_output(value, catalog)]
                output = True
            index += 2
        else:
            command.append(token)
            index += 1
    if not output:
        command += ["--output", "ducklake:%s" % catalog]
    return command


def k8s_job_manifest(command, secret_env, image, name, user, settings, labels=None):
    """
    The Job of one node: one pod (backoffLimit 0, restartPolicy Never) of `image` (imagePullPolicy Never: the image is
    loaded onto the nodes) running `command` (k8s_command). Its environment:

    - envFrom the Secret settings["secret"]: the source's ADAPT_SECRET_* and the S3 credentials and catalog password -
      only the Secret's NAME is here; secret_env's values (the local copy) are never put in the manifest;
    - plain values: the user's DuckLake data path and catalog schema, and the S3 endpoint settings.
    """
    _check_secret_env(secret_env)
    if not image:
        raise K8sJobError("ADAPT_EXECUTION=k8s needs an image: set the network's `image:` in networks.yaml or "
                          "$ADAPT_IMAGE")
    labels = {**{K8S_LABEL + key: k8s_label_value(value) for key, value in (labels or {}).items()},
              "app.kubernetes.io/name": "adapt-pipeline", "app.kubernetes.io/component": "node"}
    env = [{"name": "ADAPT_DUCKLAKE_DATA_PATH", "value": k8s_data_path(user, settings)},
           {"name": "ADAPT_DUCKLAKE_CATALOG_SCHEMA", "value": k8s_catalog_schema(user)},
           {"name": "ADAPT_DUCKLAKE_S3_ENDPOINT", "value": settings["s3_endpoint"]},
           {"name": "ADAPT_DUCKLAKE_S3_URL_STYLE", "value": settings["s3_url_style"]},
           {"name": "ADAPT_DUCKLAKE_S3_USE_SSL", "value": settings["s3_use_ssl"]},
           {"name": "ADAPT_DUCKLAKE_S3_REGION", "value": settings["s3_region"]}]
    container = {
        "name": K8S_CONTAINER, "image": image, "imagePullPolicy": "Never", "command": list(command),
        "envFrom": [{"secretRef": {"name": settings["secret"]}}], "env": env,
        "terminationMessagePolicy": "FallbackToLogsOnError",
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "2Gi"}},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]}},
    }
    manifest = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": settings["namespace"], "labels": labels},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": K8S_JOB_TTL_S,
                 "activeDeadlineSeconds": settings["timeout_s"],
                 "template": {"metadata": {"labels": labels},
                              "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                       "containers": [container]}}},
    }
    text = json.dumps(manifest)
    for value in secret_env.values():
        if value and value in text:
            raise K8sJobError("a secret value would appear in the Job manifest; refusing to build it")
    return manifest


def _kubectl(settings, *args, stdin=None, check=True, timeout=60):
    """kubectl --context C --namespace N ARGS (its output captured, never logged; no secret in its environment)."""
    command = [settings["kubectl"], "--context", settings["context"], "--namespace", settings["namespace"]]
    process = subprocess.run(command + list(args), input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and process.returncode != 0:
        raise K8sJobError("kubectl %s failed: %s" % (" ".join(args[:2]), process.stderr.strip()[:500]))
    return process.stdout


def k8s_secret_keys(settings):
    """The KEYS of the Secret (a go-template that prints no value)."""
    out = _kubectl(settings, "get", "secret", settings["secret"], "-o",
                   'go-template={{range $key, $_ := .data}}{{$key}}{{"\\n"}}{{end}}')
    return {line.strip() for line in out.splitlines() if line.strip()}


def _job_pod(settings, name):
    pods = json.loads(_kubectl(settings, "get", "pods", "-l", "job-name=%s" % name, "-o", "json")).get("items") or []
    return pods[0] if pods else None


def _container_state(pod):
    statuses = (pod or {}).get("status", {}).get("containerStatuses") or []
    return (statuses[0].get("state") or {}) if statuses else {}


def _wait_for_container(settings, name, log, poll_s):
    """The Job's pod, once its container runs or ended; K8sJobError if it cannot start (no image, no Secret...)."""
    deadline = time.time() + settings["start_timeout_s"]
    announced = None
    while True:
        pod = _job_pod(settings, name)
        state = _container_state(pod)
        if pod and pod["metadata"]["name"] != announced:
            announced = pod["metadata"]["name"]
            log.info("k8s: Job %s: pod %s" % (name, announced))
        if "running" in state or "terminated" in state:
            return pod
        waiting = state.get("waiting") or {}
        if waiting.get("reason") in K8S_POD_FATAL:
            raise K8sJobError("pod %s cannot start: %s: %s" % (announced, waiting["reason"], waiting.get("message")))
        if pod and pod.get("status", {}).get("phase") == "Failed":
            return pod
        if time.time() > deadline:
            raise K8sJobError("the pod of Job %s did not start within %ds (%s)" % (
                name, settings["start_timeout_s"], waiting.get("reason") or "no pod yet"))
        time.sleep(poll_s)


def _stream_pod_logs(settings, pod_name, log, secrets, lines):
    """Follows the container's log (kubectl logs -f) until it ends: each line redacted to `log` and to `lines`."""
    command = [settings["kubectl"], "--context", settings["context"], "--namespace", settings["namespace"],
               "logs", "-f", "pod/%s" % pod_name, "-c", K8S_CONTAINER]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
        try:
            for line in process.stdout:
                line = redact(line.rstrip("\n"), secrets)
                if line:
                    lines.append(line)
                    _log_line(log, line)
            process.wait()
        except BaseException:
            process.kill()
            raise


def _wait_for_job(settings, name, poll_s):
    """The finished Job's status: (succeeded, the Failed condition's reason/message or None)."""
    deadline = time.time() + settings["timeout_s"] + 120
    while True:
        status = json.loads(_kubectl(settings, "get", "job", name, "-o", "json")).get("status") or {}
        for condition in status.get("conditions") or []:
            if condition.get("status") != "True":
                continue
            if condition.get("type") in ("Complete", "SuccessCriteriaMet"):
                return True, None
            if condition.get("type") in ("Failed", "FailureTarget"):
                return False, "%s: %s" % (condition.get("reason"), condition.get("message"))
        if time.time() > deadline:
            raise K8sJobError("Job %s did not finish within %ds" % (name, settings["timeout_s"] + 120))
        time.sleep(poll_s)


def delete_k8s_job(settings, name):
    """Best effort: deletes the Job and its pod (background propagation)."""
    try:
        _kubectl(settings, "delete", "job", name, "--ignore-not-found", "--wait=false",
                 "--cascade=background", check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _counts(text):
    """"a: 1, b: 2,345" -> {"a": 1, "b": 2345}."""
    counts = {}
    for part in re.split(r", (?=[^,:]+: )", text or ""):
        name, sep, count = part.rpartition(": ")
        if sep and count.replace(",", "").isdigit():
            counts[name] = int(count.replace(",", ""))
    return counts


def counts_from_log(lines):
    """
    (streams, records) from adapt's log lines: each `stream 'S': N record(s) written (export: n, ...)` line, and the
    `run finished ...: N stream(s), M record(s) written (export: n, ...)` line's per-export counts when it is there.
    """
    streams, records, run_end = [], {}, None
    for line in lines:
        match = STREAM_WRITTEN.search(line)
        if match:
            exports = _counts(match.group(3))
            streams.append({"name": match.group(1), "exports": exports})
            records.update(exports)
        match = RUN_END.search(line)
        if match:
            run_end = _counts(match.group(4))
    return streams, (run_end if run_end is not None else records)


def run_k8s(argv, secret_env, log, summary_path, image, user, name=None, labels=None, settings=None,
            repo_root=None, poll_s=1.0):
    """
    Runs a node's logical argv as a Kubernetes Job (k8s_command, k8s_job_manifest): checks the Secret has every key the
    pod needs (by name: the source's ADAPT_SECRET_* and the S3/catalog credentials), creates the Job, waits for its pod,
    streams the pod's log to `log`, waits for the Job to finish, then deletes it (unless ADAPT_K8S_KEEP_JOBS). The
    counts come from adapt's log lines; a small summary JSON is written to summary_path. RunFailed if the Job failed.
    """
    settings = settings or k8s_settings()
    name = name or k8s_job_name(uuid.uuid4().hex[:12])
    secrets = [value for value in secret_env.values() if value]
    command = k8s_command(argv, settings["catalog"], repo_root or REPO_ROOT)
    manifest = k8s_job_manifest(command, secret_env, image, name, user, settings, labels)
    missing = sorted((set(secret_env) | set(K8S_REQUIRED_SECRET_KEYS)) - k8s_secret_keys(settings))
    if missing:
        raise K8sJobError("Secret %s/%s has no %s (orchestration/k8s/setup.sh creates it)" % (
            settings["namespace"], settings["secret"], ", ".join(missing)))
    where = "%s/%s" % (settings["namespace"], name)
    log.info("k8s: Job %s (context %s, image %s, envFrom Secret %s): $ %s" % (
        where, settings["context"], image, settings["secret"], redact(shlex.join(command), secrets)))
    started = time.time()
    _kubectl(settings, "create", "-f", "-", stdin=json.dumps(manifest))
    lines, pod, interrupted = [], None, True
    try:
        pod = _wait_for_container(settings, name, log, poll_s)
        _stream_pod_logs(settings, pod["metadata"]["name"], log, secrets, lines)
        succeeded, failure = _wait_for_job(settings, name, poll_s)
        pod = _job_pod(settings, name) or pod
        interrupted = False
    finally:
        if interrupted or not settings["keep_jobs"]:
            delete_k8s_job(settings, name)
    terminated = _container_state(pod).get("terminated") or {}
    exit_code = terminated.get("exitCode")
    exit_code = exit_code if exit_code is not None else (0 if succeeded else -1)
    streams, records = counts_from_log(lines)
    error = None
    if not succeeded or exit_code != 0:
        errors = [line for line in lines if _level(line) in ("ERROR", "CRITICAL")]
        error = redact(errors[-1] if errors else (terminated.get("reason") or failure or "the Job failed"), secrets)
    result = {"status": "ok" if succeeded and exit_code == 0 else "failed", "exit_code": exit_code,
              "records": records, "streams": [stream["name"] for stream in streams],
              "duration_s": round(time.time() - started, 3), "started_at": started, "summary_path": str(summary_path),
              "execution": "k8s", "image": image, "job": name, "namespace": settings["namespace"],
              "context": settings["context"], "pod": (pod or {}).get("metadata", {}).get("name"),
              "node_name": (pod or {}).get("spec", {}).get("nodeName"), "pod_command": shlex.join(command),
              "job_kept": settings["keep_jobs"]}
    with open(summary_path, "w") as handle:
        json.dump({"status": result["status"], "error": error, "streams": streams, "execution": "k8s",
                   "job": name, "namespace": settings["namespace"], "pod": result["pod"], "exit_code": exit_code},
                  handle, indent=2)
    log.info("k8s: Job %s %s (exit code %s)%s" % (where, "completed" if result["status"] == "ok" else "FAILED",
                                                  exit_code, "; kept" if settings["keep_jobs"] else "; deleted"))
    if result["status"] != "ok":
        raise RunFailed("Job %s failed (exit code %s): %s" % (where, exit_code, error), result)
    return result
