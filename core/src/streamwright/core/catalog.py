#!/usr/bin/env python
"""The connector catalog: discover and install connectors from the StreamWright hub.

A catalog is a static index (bundled with streamwright, optionally refreshed from a
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

from streamwright.core.runtime import components

HUB_ENV = "STREAMWRIGHT_HUB_URL"
CACHE = Path(os.path.expanduser("~/.streamwright/connectors.json"))


def _bundled():
    text = resources.files("streamwright.core").joinpath("connectors.json").read_text()
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
    """The catalog connectors whose key, title, summary or family contains every word of `text`."""
    words = (text or "").lower().split()
    hits = {}
    for key, entry in connectors(hub_url).items():
        haystack = " ".join([key, entry.get("title", ""), entry.get("summary", ""),
                             entry.get("family", "")]).lower()
        if all(word in haystack for word in words):
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
    """Lines for `streamwright connectors list`: every catalog connector with install state + trust."""
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
    """Resolve one connector `key` in the catalog and install it (see install_many)."""
    return install_many([key], assume_yes=assume_yes, hub_url=hub_url, pip_args=pip_args)


def install_many(keys, assume_yes=False, hub_url=None, pip_args=()):
    """
    Resolve connector `keys` in the catalog and install them with ONE pip command, so pip resolves their
    dependencies together. Every key is checked first: an unknown key, or a connector without an installable
    source, installs nothing. Image-distributed connectors are reported, not installed. Non-"official"
    connectors need one confirmation for all of them (or `assume_yes`), and are refused non-interactively.
    """
    keys = list(dict.fromkeys(keys))  # in order, without repeats
    if not keys:
        sys.stderr.write("streamwright: 'connectors install' needs at least one connector KEY\n")
        return 2
    catalog = connectors(hub_url)
    unknown = [key for key in keys if key not in catalog]
    if unknown:
        sys.stderr.write("streamwright: unknown connector%s %s (try: streamwright connectors list)\n" % (
            "s" if len(unknown) > 1 else "", ", ".join(repr(key) for key in unknown)))
        return 2

    specs, untrusted = [], []
    for key in keys:
        entry = catalog[key]
        inst = entry.get("install", {})
        if "image" in inst:
            print("%r is distributed as an image, not a Python package:" % key)
            print("  %s" % inst["image"])
            print("Run it with the docker or k8s execution mode (it is not pip-installed).")
            continue
        spec = inst.get("pip") or inst.get("git")
        if not spec:
            sys.stderr.write("streamwright: connector %r has no installable source\n" % key)
            return 2
        specs.append(spec)
        trust = entry.get("trust", "community")
        if trust != "official":
            untrusted.append((key, trust, _source_of(inst)))
    if not specs:
        return 0

    if untrusted and not assume_yes:
        names = ", ".join("%s connector %r" % (trust, key) for key, trust, _ in untrusted)
        if not sys.stdin.isatty():
            sys.stderr.write("streamwright: refusing to install %s non-interactively; pass --yes to confirm (%s)\n"
                             % (names, "; ".join(source for _, _, source in untrusted)))
            return 2
        sys.stderr.write("\n  WARNING: installing runs third-party code from:\n")
        for key, trust, source in untrusted:
            sys.stderr.write("    %s (%s): %s\n" % (key, trust, source))
        answer = input("  Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted")
            return 1

    cmd = [sys.executable, "-m", "pip", "install", *pip_args, *specs]
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)
