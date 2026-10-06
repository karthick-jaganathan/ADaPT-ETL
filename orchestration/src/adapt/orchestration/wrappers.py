"""
Per-node wrappers: a wrapper turns (node, Context) into the node's `adapt run` argv and the secret environment
variables to run it with - `(argv, secret_env)`.

- default_wrapper: reads the source's declared inputs (sourcespec: spec.config + spec.secrets), fills each from the
  network's `inputs` (networks.yaml templates over row/app/ctx) or leaves it to the source's default, and fails on a
  required input with no value. Config inputs become `--set NAME=VALUE`; secret inputs become
  ADAPT_SECRET_<NAME> entries of secret_env (never argv).
- @node(name, network=None) registers a custom wrapper for a node (for one network, or any): e.g. one that looks
  something up in a database or reads ctx.upstream, then calls default_wrapper with `overrides`.

The framework calls whatever wrapper is registered for (node, network), else (node, any network), else the default.
"""

import re

from adapt.orchestration.settings import adapt_bin
from adapt.orchestration.sourcespec import load_source_spec

SECRET_PREFIX = "secret:"
SECRET_ENV_PREFIX = "ADAPT_SECRET_"
REFERENCE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
CTX_FIELDS = ("user_id", "account_id", "network", "timezone", "schema")

_REGISTRY = {}


class WrapperError(ValueError):
    """A node's command cannot be built (a missing required input, an unknown input, a misplaced secret...)."""


def node(name, network=None):
    """Registers the decorated `wrapper(node, ctx) -> (argv, secret_env)` for node `name` (on `network`, or any)."""
    def register(function):
        key = (name, network)
        if key in _REGISTRY and _REGISTRY[key] is not function:
            raise WrapperError("node %r%s already has the wrapper %s" % (
                name, "" if network is None else " (network %s)" % network, _REGISTRY[key].__name__))
        _REGISTRY[key] = function
        return function
    return register


def unregister(name, network=None):
    _REGISTRY.pop((name, network), None)


def wrapper_for(name, network):
    """The wrapper of a node on a network: a custom one (network-specific first), else default_wrapper."""
    return _REGISTRY.get((name, network)) or _REGISTRY.get((name, None)) or default_wrapper


def _text(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value) or None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return text if text != "" else None


def _lookup(namespace, key, ctx):
    if namespace == "row":
        return ctx.row.get(key)
    if namespace == "app":
        return ctx.app.get(key)
    if namespace == "ctx":
        if key not in CTX_FIELDS:
            raise WrapperError("{{ ctx.%s }}: ctx has %s" % (key, ", ".join(CTX_FIELDS)))
        return getattr(ctx, key)
    raise WrapperError("{{ %s.%s }}: references are row.NAME, app.NAME or ctx.NAME" % (namespace, key))


def render(template, ctx):
    """
    A template's value as a --set string, or None when it is empty or references something unset (so the input stays
    unset). Literals (numbers, lists) are stringified; lists become a,b,c.
    """
    if not isinstance(template, str):
        return _text(template)
    whole = REFERENCE.fullmatch(template.strip())
    if whole:
        return _text(_lookup(whole.group(1), whole.group(2), ctx))
    missing = []

    def substitute(match):
        value = _text(_lookup(match.group(1), match.group(2), ctx))
        if value is None:
            missing.append(match.group(0))
            return ""
        return value

    text = REFERENCE.sub(substitute, template)
    if "{{" in text or "}}" in text:
        raise WrapperError("malformed template %r" % template)
    return None if missing else _text(text)


def resolve_inputs(node_name, ctx, overrides=None):
    """
    (config, secrets): the value of every input the source declares, from `overrides` (a custom wrapper's) or the
    network's `inputs`; inputs left unset fall back to the source's own default. Errors never contain values.
    """
    spec = load_source_spec(ctx.source)
    overrides = dict(overrides or {})
    for name in list(ctx.inputs) + list(overrides):
        if spec.param(name) is None:
            raise WrapperError("node %r: input %r is not declared by source %s (declared: %s)" % (
                node_name, name, spec.name, ", ".join(spec.config_keys + spec.secret_keys)))
    config, secrets = {}, {}
    for param in list(spec.config.values()) + list(spec.secrets.values()):
        if param.name in overrides:
            raw, origin = overrides[param.name], "the %s wrapper" % node_name
        elif param.name in ctx.inputs:
            raw, origin = ctx.inputs[param.name], "networks.yaml (network %s)" % ctx.network
        else:
            raw, origin = None, None
        marked_secret = isinstance(raw, str) and raw.startswith(SECRET_PREFIX)
        if marked_secret:
            raw = raw[len(SECRET_PREFIX):]
        if marked_secret and param.kind == "config":
            raise WrapperError("node %r: %r is a config input of source %s, so it would go on the command line; "
                               "it cannot be a secret: value" % (node_name, param.name, spec.name))
        value = render(raw, ctx) if raw is not None else None
        if value is None:
            if param.required:
                raise WrapperError(
                    "node %r: source %s requires %s %r but %s and it has no default" % (
                        node_name, spec.name, param.kind, param.name,
                        "%s gives it no value" % origin if origin else "networks.yaml (network %s) does not map it"
                        % ctx.network))
            continue
        (secrets if param.kind == "secret" else config)[param.name] = value
    return config, secrets


def secret_env_name(name):
    return SECRET_ENV_PREFIX + re.sub(r"[^A-Z0-9_]", "_", name.upper())


def default_wrapper(node_name, ctx, overrides=None):
    """
    `adapt run <source> --stream <stream> --set ... --timezone <tz> --output duckdb:<warehouse>:<schema>
    --allow-connector ...` and {ADAPT_SECRET_<NAME>: value} for the source's secrets. The node maps to the source's
    `<stream>` via the network (ctx.stream_of: an alias when the network names it differently).
    """
    spec = load_source_spec(ctx.source)
    stream = ctx.stream_of(node_name)
    if spec.streams and stream not in spec.streams:
        raise WrapperError("node %r maps to stream %r, which is not a stream of source %s (streams: %s)" % (
            node_name, stream, spec.name, ", ".join(spec.streams)))
    config, secrets = resolve_inputs(node_name, ctx, overrides)
    argv = [adapt_bin(), "run", str(ctx.source), "--stream", stream]
    for name, value in config.items():
        argv += ["--set", "%s=%s" % (name, value)]
    argv += ["--timezone", ctx.timezone, "--output", "duckdb:%s:%s" % (ctx.warehouse_path, ctx.schema)]
    for connector in ctx.connectors:
        argv += ["--allow-connector", connector]
    secret_env = {secret_env_name(name): value for name, value in secrets.items()}
    assert_no_secrets(argv, secret_env)
    return argv, secret_env


def assert_no_secrets(argv, secret_env):
    """Defense in depth: no secret value may appear anywhere in the argv (which is logged)."""
    for value in secret_env.values():
        if value and any(value in arg for arg in argv):
            raise WrapperError("a secret value would appear on the command line; refusing to build the command")
