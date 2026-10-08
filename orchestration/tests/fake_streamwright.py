"""A stand-in for the streamwright CLI (tests only): writes a --summary, echoes a secret it got from the environment."""

import json
import os
import sys

argv = sys.argv[1:]
summary = argv[argv.index("--summary") + 1]
stream = argv[argv.index("--stream") + 1] if "--stream" in argv else "x"
exit_code = int(os.environ.get("FAKE_STREAMWRIGHT_EXIT", "0"))
token = os.environ.get("STREAMWRIGHT_SECRET_DEVELOPER_TOKEN", "")
print("[2026-10-05 00:00:00,000] INFO streamwright.source: got token %s" % token, flush=True)
print("[2026-10-05 00:00:00,001] WARNING streamwright.source: a warning", flush=True)
with open(summary, "w") as handle:
    json.dump({"status": "ok" if exit_code == 0 else "failed",
               "streams": [{"name": stream, "exports": {stream: 5}}],
               "error": None if exit_code == 0 else "boom with %s" % token}, handle)
sys.exit(exit_code)
