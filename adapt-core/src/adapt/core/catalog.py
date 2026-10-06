#!/usr/bin/env python
"""The connector catalog: discover and install connectors from the ADaPT hub.

A catalog is a static index (bundled with adapt-core, optionally refreshed from a
remote hub URL) that maps a connector key to how it's installed - a PyPI
requirement, a pinned git URL, or an image. The key is the same name used in
source YAML (`provider`/`sdk`); the install source is just a delivery detail, so
a package can move from git to PyPI without changing what users type.

Security: non-"official" connectors run third-party code, so `install` asks for
confirmation (or `--yes`) and refuses non-interactive installs without it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from importlib import resources
except ImportError:  # pragma: no cover
    import importlib_resources as resources

from adapt.core.runtime import components

HUB_ENV = "ADAPT_HUB_URL"
CACHE = Path(os.path.expanduser("~/.adapt/connectors.json"))


def _bundled():
    text = resources.files("adapt.core").joinpath("connectors.json").read_text()
    return json.loads(text)


def _remote(url):
    import requests

    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data))
    except OSError:
        pass
    return data


def load(hub_url=None):
    """The catalog: the remote hub if configured (cached), else the bundled default."""
    url = hub_url or os.environ.get(HUB_ENV)
    if url:
        try:
            return _remote(url)
        except Exception:  # offline or hub down: fall back
            pass
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            pass
    return _bundled()


def connectors(hub_url=None):
    return load(hub_url).get("connectors", {})


def installed():
    try:
        return set(components.available())
    except Exception:
        return set()


def search(text, hub_url=None):
    text = (text or "").lower()
    hits = {}
    for key, entry in connectors(hub_url).items():
        haystack = " ".join([key, entry.get("title", ""), entry.get("summary", ""),
                             entry.get("family", "")]).lower()
        if text in haystack:
            hits[key] = entry
    return hits


def _source_of(install):
    if "pip" in install:
        return "PyPI: " + install["pip"]
    if "git" in install:
        return "git: " + install["git"]
    if "image" in install:
        return "image: " + install["image"]
    return "unknown"


def listing(hub_url=None):
    """Lines for `adapt connectors list`: every catalog connector with install state + trust."""
    have = installed()
    lines = []
    by_family = {}
    for key, entry in connectors(hub_url).items():
        by_family.setdefault(entry.get("family", "other"), []).append((key, entry))
    for family in sorted(by_family):
        lines.append("%s:" % family)
        for key, entry in sorted(by_family[family]):
            mark = "[installed]" if key in have else "          "
            trust = entry.get("trust", "community")
            lines.append("  %s %-16s %-10s %s" % (mark, key, trust, entry.get("summary", "")))
    return lines


def install(key, assume_yes=False, hub_url=None, pip_args=()):
    """Resolve `key` in the catalog and install it (pip for pip/git; images are run, not installed)."""
    entry = connectors(hub_url).get(key)
    if not entry:
        sys.stderr.write("adapt: unknown connector %r (try: adapt connectors list)\n" % key)
        return 2
    inst = entry.get("install", {})
    trust = entry.get("trust", "community")

    if "image" in inst:
        print("%r is distributed as an image, not a Python package:" % key)
        print("  %s" % inst["image"])
        print("Run it with the docker or k8s execution mode (it is not pip-installed).")
        return 0

    spec = inst.get("pip") or inst.get("git")
    if not spec:
        sys.stderr.write("adapt: connector %r has no installable source\n" % key)
        return 2

    if trust != "official" and not assume_yes:
        source = _source_of(inst)
        if not sys.stdin.isatty():
            sys.stderr.write(
                "adapt: refusing to install %s connector %r non-interactively; pass --yes to confirm (%s)\n"
                % (trust, key, source))
            return 2
        sys.stderr.write(
            "\n  WARNING: %r is a %s connector. Installing runs third-party code from:\n    %s\n"
            % (key, trust, source))
        answer = input("  Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted")
            return 1

    cmd = [sys.executable, "-m", "pip", "install", *pip_args, spec]
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd)
