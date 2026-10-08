"""
A stand-in for kubectl (tests only), with its state in $FAKE_KUBECTL_STATE: `create -f -` stores the Job manifest it
reads on stdin; `get pods -l job-name=J` shows one pod whose container ended with $FAKE_STREAMWRIGHT_EXIT; `logs -f pod/P`
prints streamwright-like lines for the Job's --stream (the pod's command, the envFrom Secret's name, the records); `get job J`
shows the Complete or Failed condition; `get secret` prints the keys of $FAKE_KUBECTL_SECRET_KEYS (comma separated);
`delete job J` marks it deleted. Every call is appended to calls.jsonl (argv only, never stdin), every create and
delete to events.jsonl.
"""

import json
import os
import sys
import time

STATE = os.environ["FAKE_KUBECTL_STATE"]
DEFAULT_KEYS = ("STREAMWRIGHT_SECRET_DEVELOPER_TOKEN,STREAMWRIGHT_SECRET_CLIENT_ID,STREAMWRIGHT_SECRET_CLIENT_SECRET,"
                "STREAMWRIGHT_SECRET_REFRESH_TOKEN,STREAMWRIGHT_DUCKLAKE_S3_KEY_ID,STREAMWRIGHT_DUCKLAKE_S3_SECRET,"
                "STREAMWRIGHT_DUCKLAKE_CATALOG_PASSWORD")
EXIT = int(os.environ.get("FAKE_STREAMWRIGHT_EXIT", "0"))

argv = sys.argv[1:]
assert argv[0] == "--context" and argv[2] == "--namespace", argv
context, namespace, args = argv[1], argv[3], argv[4:]
os.makedirs(STATE, exist_ok=True)
with open(os.path.join(STATE, "calls.jsonl"), "a") as handle:
    handle.write(json.dumps({"args": args, "context": context, "namespace": namespace, "time": time.time()}) + "\n")


def event(verb, name):
    with open(os.path.join(STATE, "events.jsonl"), "a") as handle:
        handle.write(json.dumps([verb, name]) + "\n")


def job_path(name):
    return os.path.join(STATE, "job-%s.json" % name)


def load_job(name):
    with open(job_path(name)) as handle:
        return json.load(handle)


if args[:2] == ["get", "secret"]:
    keys = os.environ.get("FAKE_KUBECTL_SECRET_KEYS", DEFAULT_KEYS)
    for key in filter(None, keys.split(",")):
        print(key)
elif args[:3] == ["create", "-f", "-"]:
    manifest = json.load(sys.stdin)
    with open(job_path(manifest["metadata"]["name"]), "w") as handle:
        json.dump(manifest, handle)
    event("create", manifest["metadata"]["name"])
    print("job.batch/%s created" % manifest["metadata"]["name"])
elif args[:2] == ["get", "pods"]:
    name = args[args.index("-l") + 1].split("=", 1)[1]
    pod = {"metadata": {"name": "%s-abcde" % name}, "spec": {"nodeName": "fake-node"},
           "status": {"phase": "Succeeded" if EXIT == 0 else "Failed",
                      "containerStatuses": [{"state": {"terminated": {"exitCode": EXIT}}}]}}
    print(json.dumps({"items": [pod] if os.path.exists(job_path(name)) else []}))
elif args[0] == "logs":
    name = args[args.index("-f") + 1][len("pod/"):].rsplit("-", 1)[0]
    container = load_job(name)["spec"]["template"]["spec"]["containers"][0]
    command = container["command"]
    stream = command[command.index("--stream") + 1] if "--stream" in command else "x"
    print("[2026-10-05 00:00:00,000] INFO fake-pod: $ %s" % " ".join(command))
    print("[2026-10-05 00:00:00,000] INFO fake-pod: envFrom %s" % container["envFrom"][0]["secretRef"]["name"])
    if EXIT == 0:
        print("[2026-10-05 00:00:01,000] INFO streamwright.source: stream '%s': 1,234 record(s) written (%s: 1,234), "
              "1 request(s) (raw: 1), 0 retries, 0 failed partition(s), 0.1 s" % (stream, stream))
        print("[2026-10-05 00:00:01,000] INFO streamwright.source: run finished in 0.2 s: 1 stream(s), 1,234 record(s) "
              "written (%s: 1,234), 1 request(s), 0 retries, 0 failed partition(s), 1 output(s) written" % stream)
    else:
        print("[2026-10-05 00:00:01,000] ERROR streamwright.source: boom")
elif args[:2] == ["get", "job"]:
    condition = {"type": "Complete", "status": "True"} if EXIT == 0 else \
        {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded", "message": "Job has reached the limit"}
    print(json.dumps({"status": {"conditions": [condition]}}))
elif args[:2] == ["delete", "job"]:
    open(os.path.join(STATE, "deleted-%s" % args[2]), "w").close()
    event("delete", args[2])
else:
    sys.exit("fake kubectl: unexpected %r" % args)
