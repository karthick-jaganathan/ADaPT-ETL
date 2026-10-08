"""
The declarative files: the named pipelines (config/pipelines/<name>.yaml, each a DAG - node order only) and
config/networks.yaml (a network -> its StreamWright source, how to fill the source's inputs, and the SHARED-VOCABULARY
`streams:` map that aliases/skips each canonical node per network). All are validated here; none knows anything about a
particular network. A pipeline is specialised to a network with resolve_pipeline (aliases, skips, edge overrides).

Selecting a pipeline: pipeline_names() lists the *.yaml of $STREAMWRIGHT_PIPELINE_DIR (default <project dir>/config/pipelines)
and load_pipeline(name) loads <pipelines dir>/<name>.yaml. $STREAMWRIGHT_PIPELINE_SPEC (a path to one pipeline file) replaces
the folder: that file is then the only pipeline, under its `name:`.
"""

import dataclasses
import re
from pathlib import Path

import yaml

from streamwright.orchestration.settings import REPO_ROOT, networks_path, pipeline_spec_override, pipelines_dir
from streamwright.orchestration.sourcespec import load_source_spec

NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class SpecError(ValueError):
    """A pipeline file or networks.yaml is invalid, or a pipeline is unknown."""


@dataclasses.dataclass(frozen=True)
class PipelineSpec:
    """The DAG: nodes in declaration order, each node's upstream nodes, and a topological order."""
    name: str
    nodes: tuple
    deps: dict  # node -> tuple of upstream nodes (its `after`)
    order: tuple

    @property
    def edges(self):
        """(upstream, downstream) pairs."""
        return tuple((up, node) for node in self.nodes for up in self.deps[node])

    def upstream(self, node):
        if node not in self.deps:
            raise SpecError("unknown node %r" % node)
        return self.deps[node]

    def downstream(self, node):
        if node not in self.deps:
            raise SpecError("unknown node %r" % node)
        return tuple(down for down in self.nodes if node in self.deps[down])


@dataclasses.dataclass(frozen=True)
class Network:
    """A network's entry in networks.yaml."""
    name: str
    source: Path
    timezone: str
    connectors: tuple
    inputs: dict  # input name -> template (a string) or literal
    streams: dict  # canonical node -> the source's stream name (a str), or False when the network has no such stream
    after: dict = dataclasses.field(default_factory=dict)  # canonical node -> upstream nodes (per-network edge override)
    image: str = None  # the container image of STREAMWRIGHT_EXECUTION=docker ($STREAMWRIGHT_IMAGE overrides it)

    def stream_of(self, node):
        """The source stream a canonical node maps to (default: the node name); False when unsupported, None if unmapped."""
        return self.streams.get(node, node)

    def supports(self, node):
        """Whether this network has the canonical node (it is mapped to a stream, not to false)."""
        return self.streams.get(node, node) is not False


def _load_yaml(path):
    path = Path(path)
    try:
        with open(path) as handle:
            return yaml.safe_load(handle)
    except OSError as error:
        raise SpecError("cannot read %s: %s" % (path, error))
    except yaml.YAMLError as error:
        raise SpecError("%s is not valid YAML: %s" % (path, error))


def topological_order(nodes, deps):
    """Kahn's algorithm; ties keep the declaration order. A cycle raises SpecError naming its nodes."""
    remaining = {node: set(deps[node]) for node in nodes}
    order = []
    while remaining:
        ready = [node for node in nodes if node in remaining and not remaining[node]]
        if not ready:
            raise SpecError("the nodes form a cycle: %s" % ", ".join(node for node in nodes if node in remaining))
        for node in ready:
            order.append(node)
            del remaining[node]
        for pending in remaining.values():
            pending.difference_update(ready)
    return tuple(order)


def parse_pipeline(data):
    """A PipelineSpec from a pipeline file's parsed content."""
    if not isinstance(data, dict):
        raise SpecError("a pipeline must be a mapping with `name` and `nodes`")
    unknown = set(data) - {"name", "nodes"}
    if unknown:
        raise SpecError("unknown keys %s" % ", ".join(sorted(unknown)))
    name = data.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise SpecError("`name` must match %s, got %r" % (NAME.pattern, name))
    raw = data.get("nodes")
    if not isinstance(raw, dict) or not raw:
        raise SpecError("`nodes` must be a non-empty mapping of node -> {after: [...]}")
    nodes = tuple(raw)
    deps = {}
    for node, body in raw.items():
        if not isinstance(node, str) or not NAME.match(node):
            raise SpecError("node name %r must match %s" % (node, NAME.pattern))
        body = {} if body is None else body
        if not isinstance(body, dict) or set(body) - {"after"}:
            raise SpecError("node %r: only `after: [nodes]` is allowed, got %r" % (node, body))
        after = body.get("after", [])
        if isinstance(after, str):
            after = [after]
        if not isinstance(after, list) or not all(isinstance(up, str) for up in after):
            raise SpecError("node %r: `after` must be a list of node names" % node)
        for up in after:
            if up == node:
                raise SpecError("node %r depends on itself" % node)
            if up not in raw:
                raise SpecError("node %r runs after unknown node %r" % (node, up))
        if len(set(after)) != len(after):
            raise SpecError("node %r lists an upstream node twice: %r" % (node, after))
        deps[node] = tuple(after)
    return PipelineSpec(name=name, nodes=nodes, deps=deps, order=topological_order(nodes, deps))


def load_pipeline_file(path):
    """The PipelineSpec of one pipeline file (any path)."""
    path = Path(path)
    try:
        return parse_pipeline(_load_yaml(path))
    except SpecError as error:
        raise SpecError("%s: %s" % (path, error)) from None


def pipeline_files():
    """pipeline name -> its file: the *.yaml of the pipelines folder (a file's `name:` must be its file name), or only
    $STREAMWRIGHT_PIPELINE_SPEC when that is set."""
    override = pipeline_spec_override()
    if override is not None:
        return {load_pipeline_file(override).name: override}
    folder = pipelines_dir()
    if not folder.is_dir():
        raise SpecError("the pipelines folder %s does not exist" % folder)
    files = {}
    for path in sorted(folder.glob("*.yaml")):
        if not NAME.match(path.stem):
            raise SpecError("%s: a pipeline file name must match %s.yaml" % (path, NAME.pattern))
        files[path.stem] = path
    if not files:
        raise SpecError("the pipelines folder %s has no <name>.yaml" % folder)
    return files


def pipeline_names():
    """The names of the pipelines, sorted (e.g. ['metadata', 'performance'])."""
    return sorted(pipeline_files())


def load_pipeline(name=None, path=None):
    """
    The pipeline `name` (pipelines/<name>.yaml, or the $STREAMWRIGHT_PIPELINE_SPEC file when its `name:` is `name`), or the
    pipeline file at `path`. With neither, the only pipeline there is ($STREAMWRIGHT_PIPELINE_SPEC, or a one-file folder).
    """
    if path is not None:
        return load_pipeline_file(path)
    files = pipeline_files()
    if name is None:
        if len(files) != 1:
            raise SpecError("several pipelines (%s): name the one to load" % ", ".join(sorted(files)))
        name = next(iter(files))
    if not isinstance(name, str) or name not in files:
        raise SpecError("unknown pipeline %r (pipelines: %s)" % (name, ", ".join(sorted(files))))
    spec = load_pipeline_file(files[name])
    if spec.name != name:
        raise SpecError("%s: `name: %s` does not match the file name (%s)" % (files[name], spec.name, name))
    return spec


def load_pipelines():
    """pipeline name -> PipelineSpec, for every pipeline."""
    return {name: load_pipeline(name) for name in pipeline_names()}


def resolve_pipeline(pipeline, network):
    """
    The effective DAG of `pipeline` on `network`: the canonical nodes the network supports (network.streams[node] is
    not False), wired by the network's `after` override or, by default, the pipeline's edges with any skipped upstream
    node dropped. Every pipeline node must be mapped by the network's `streams:`. Returns a PipelineSpec of the kept
    nodes; raises SpecError on an unmapped node, on no supported node, or on an edge to a skipped/unknown node.
    """
    unmapped = [node for node in pipeline.nodes if node not in network.streams]
    if unmapped:
        raise SpecError("pipeline %r: network %r's `streams:` does not map %s %s (map each to a stream name or false)"
                        % (pipeline.name, network.name, "node" if len(unmapped) == 1 else "nodes",
                           ", ".join(unmapped)))
    kept = tuple(node for node in pipeline.nodes if network.streams[node] is not False)
    if not kept:
        raise SpecError("pipeline %r: network %r supports none of its nodes (all mapped to false)" % (
            pipeline.name, network.name))
    kept_set = set(kept)
    deps = {}
    for node in kept:
        if node in network.after:
            ups = network.after[node]
            missing = [up for up in ups if up not in kept_set]
            if missing:
                raise SpecError("pipeline %r on network %r: node %r's `after` lists %s, which %s not a supported node"
                                % (pipeline.name, network.name, node, ", ".join(missing),
                                   "is" if len(missing) == 1 else "are"))
        else:
            ups = tuple(up for up in pipeline.deps[node] if up in kept_set)
        deps[node] = tuple(ups)
    return PipelineSpec(name=pipeline.name, nodes=kept, deps=deps, order=topological_order(kept, deps))


def parse_networks(data, base=REPO_ROOT):
    """network name -> Network from networks.yaml's parsed content; source paths are relative to `base`."""
    if not isinstance(data, dict) or not isinstance(data.get("networks"), dict) or not data["networks"]:
        raise SpecError("networks.yaml must have a non-empty `networks:` mapping")
    networks = {}
    for name, body in data["networks"].items():
        if not isinstance(name, str) or not NAME.match(name):
            raise SpecError("network name %r must match %s" % (name, NAME.pattern))
        if not isinstance(body, dict):
            raise SpecError("network %r must be a mapping" % name)
        unknown = set(body) - {"source", "timezone", "connectors", "inputs", "streams", "after", "image"}
        if unknown:
            raise SpecError("network %r: unknown keys %s" % (name, ", ".join(sorted(unknown))))
        if not isinstance(body.get("source"), str):
            raise SpecError("network %r: `source` (a source folder) is required" % name)
        source = (Path(base) / Path(body["source"]).expanduser()).resolve()
        if not (source / "source.yaml").is_file():
            raise SpecError("network %r: %s has no source.yaml" % (name, source))
        connectors = body.get("connectors") or []
        if not isinstance(connectors, list) or not all(isinstance(c, str) for c in connectors):
            raise SpecError("network %r: `connectors` must be a list of connector names" % name)
        inputs = body.get("inputs") or {}
        if not isinstance(inputs, dict) or not all(isinstance(key, str) for key in inputs):
            raise SpecError("network %r: `inputs` must be a mapping of input name -> value" % name)
        streams = _parse_streams(name, source, body.get("streams"))
        after = _parse_after(name, streams, body.get("after"))
        timezone = body.get("timezone") or "UTC"
        image = body.get("image")
        if image is not None and (not isinstance(image, str) or not image.strip()):
            raise SpecError("network %r: `image` must be a container image reference, got %r" % (name, image))
        networks[name] = Network(name=name, source=source, timezone=str(timezone), connectors=tuple(connectors),
                                 inputs=dict(inputs), streams=streams, after=after,
                                 image=image.strip() if image else None)
    return networks


def _parse_streams(name, source, raw):
    """A network's `streams:`: canonical node -> source stream name (validated against the source) or False."""
    if not isinstance(raw, dict) or not raw:
        raise SpecError("network %r: `streams` must map every canonical node to a source stream name or false" % name)
    source_spec = load_source_spec(source)
    source_streams = set(source_spec.streams)
    streams = {}
    for node, value in raw.items():
        if not isinstance(node, str) or not NAME.match(node):
            raise SpecError("network %r: `streams` key %r must match %s" % (name, node, NAME.pattern))
        if value is False:
            streams[node] = False
        elif isinstance(value, str) and value:
            if source_streams and value not in source_streams:
                raise SpecError("network %r: node %r -> %r is not a stream of source %s (its streams: %s)" % (
                    name, node, value, source_spec.name, ", ".join(sorted(source_streams))))
            streams[node] = value
        else:
            raise SpecError("network %r: node %r must map to a stream name or false, got %r" % (name, node, value))
    return streams


def _parse_after(name, streams, raw):
    """A network's optional `after:`: canonical node -> its upstream nodes (overriding the pipeline's edges)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SpecError("network %r: `after` must be a mapping of node -> [upstream nodes]" % name)
    after = {}
    for node, ups in raw.items():
        if node not in streams:
            raise SpecError("network %r: `after` node %r is not in `streams`" % (name, node))
        if isinstance(ups, str):
            ups = [ups]
        if not isinstance(ups, list) or not all(isinstance(up, str) for up in ups):
            raise SpecError("network %r: `after` of %r must be a list of node names" % (name, node))
        for up in ups:
            if up == node:
                raise SpecError("network %r: node %r depends on itself" % (name, node))
            if up not in streams:
                raise SpecError("network %r: `after` of %r references unknown node %r" % (name, node, up))
        if len(set(ups)) != len(ups):
            raise SpecError("network %r: `after` of %r lists a node twice" % (name, node))
        after[node] = tuple(ups)
    return after


def load_networks(path=None):
    path = Path(path or networks_path())
    return parse_networks(_load_yaml(path))
