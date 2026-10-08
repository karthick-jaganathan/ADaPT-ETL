"""
What a source needs: its source.yaml's declared spec.config and spec.secrets (and its streams/*.yaml), read with
yaml.safe_load - streamwright is never imported. The default wrapper uses this to know which inputs a node must be given.
"""

import dataclasses
import functools
from pathlib import Path

import yaml


class SourceSpecError(ValueError):
    """A source folder or its source.yaml is not usable."""


@dataclasses.dataclass(frozen=True)
class Param:
    name: str
    kind: str  # "config" or "secret"
    type: str
    required: bool
    default: object = None


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    name: str
    path: Path
    config: dict   # name -> Param, in declaration order
    secrets: dict  # name -> Param, in declaration order
    streams: tuple

    @property
    def config_keys(self):
        return tuple(self.config)

    @property
    def secret_keys(self):
        return tuple(self.secrets)

    def param(self, name):
        return self.config.get(name) or self.secrets.get(name)


def _params(section, kind, path):
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SourceSpecError("%s: spec.%s must be a mapping" % (path, "config" if kind == "config" else "secrets"))
    params = {}
    for name, body in section.items():
        body = body if isinstance(body, dict) else {}
        has_default = "default" in body
        required = bool(body.get("required", not has_default))
        params[name] = Param(name=name, kind=kind, type=str(body.get("type", "string")), required=required,
                             default=body.get("default"))
    return params


@functools.lru_cache(maxsize=None)
def load_source_spec(source):
    """The declared inputs and streams of a source folder (source.yaml + streams/*.yaml)."""
    path = Path(source).resolve()
    manifest = path / "source.yaml"
    try:
        with open(manifest) as handle:
            data = yaml.safe_load(handle) or {}
    except OSError as error:
        raise SourceSpecError("cannot read %s: %s" % (manifest, error))
    if not isinstance(data, dict):
        raise SourceSpecError("%s must be a mapping" % manifest)
    spec = data.get("spec") or {}
    if not isinstance(spec, dict):
        raise SourceSpecError("%s: `spec` must be a mapping" % manifest)
    streams = tuple(sorted(p.stem for p in (path / "streams").glob("*.yaml")))
    return SourceSpec(name=str(data.get("name") or path.name), path=path,
                      config=_params(spec.get("config"), "config", manifest),
                      secrets=_params(spec.get("secrets"), "secret", manifest), streams=streams)
