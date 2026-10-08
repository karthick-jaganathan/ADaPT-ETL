"""
A stand-in for the docker CLI (tests only): `docker run ... <image> run <streamwright args>`. It maps the -v mounts back to
host paths, writes the --summary at the host path of /runs/..., echoes every -e variable's value it got from ITS
environment (as docker would copy it into the container) and the streamwright args it would run, and exits $FAKE_STREAMWRIGHT_EXIT.
"""

import json
import os
import sys

VALUE_OPTIONS = {"--name", "--pull", "--add-host", "-v", "-e", "--label", "--user", "--network"}

argv = sys.argv[1:]
assert argv[0] == "run", argv
index, mounts, variables = 1, {}, []
while argv[index].startswith("-"):
    option = argv[index]
    if option in VALUE_OPTIONS:
        value = argv[index + 1]
        if option == "-v":
            host, container = value.split(":")[:2]
            mounts[container] = host
        elif option == "-e":
            assert "=" not in value, "a -e with a value: %s" % option
            variables.append(value)
        index += 2
    else:
        index += 1
image, streamwright = argv[index], argv[index + 1:]
summary = streamwright[streamwright.index("--summary") + 1]
assert summary.startswith("/runs/"), summary
host_summary = os.path.join(mounts["/runs"], summary[len("/runs/"):])
stream = streamwright[streamwright.index("--stream") + 1] if "--stream" in streamwright else "x"
exit_code = int(os.environ.get("FAKE_STREAMWRIGHT_EXIT", "0"))
print("[2026-10-05 00:00:00,000] INFO fake-docker: image %s streamwright %s" % (image, " ".join(streamwright)), flush=True)
for variable in variables:
    print("[2026-10-05 00:00:00,000] INFO streamwright.source: %s=%s" % (variable, os.environ.get(variable, "")), flush=True)
with open(host_summary, "w") as handle:
    json.dump({"status": "ok" if exit_code == 0 else "failed", "error": None if exit_code == 0 else "boom",
               "streams": [{"name": stream, "exports": {stream: 3}}]}, handle)
sys.exit(exit_code)
