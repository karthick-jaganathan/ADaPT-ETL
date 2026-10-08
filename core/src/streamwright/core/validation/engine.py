#!/usr/bin/env python
# /*************************************************************************
# * Copyright 2025 Karthick Jaganathan
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# * https://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
# **************************************************************************/

"""
streamwright-validate: static checks for StreamWright source configurations (`kind: source`).

Validates sources without running them: YAML problems, unknown or misspelled keys, missing keys, wrong value
types, and patterns known to fail (or to silently produce wrong data) at run time. Each finding points to the
file, line and YAML path:

    source.yaml:12:5: error [unknown-key] auth.provder: unknown key 'provder' (did you mean 'provider'?); ...

A source folder (source.yaml plus one file per stream in streams/) is checked as one source, and its findings
point to the file they are in. This module holds the YAML loading, reporting and entry points; the source
checks themselves are in source_validator.SourceChecker.

Exit status: 0 = no errors, 1 = errors (or warnings with --strict), 2 = usage error.
"""

import argparse
import difflib
import json
import os
import sys

import yaml

from streamwright.core.config import loader as source_files
from streamwright.core.validation import schema


__all__ = ["Issue", "validate_file", "validate_source", "validate_paths", "main"]

ERROR = "error"
WARNING = "warning"


class Issue(object):

    __slots__ = ("severity", "code", "message", "file", "path", "line", "column")

    def __init__(self, severity, code, message, file=None, path="", line=None, column=None):
        self.severity = severity
        self.code = code
        self.message = message
        self.file = file
        self.path = path
        self.line = line
        self.column = column

    def to_dict(self):
        return dict((k, getattr(self, k)) for k in self.__slots__)

    def __str__(self):
        where = self.file or "<input>"
        if self.line is not None:
            where += ":%d:%d" % (self.line, self.column)
        path = "%s: " % self.path if self.path else ""
        return "%s: %s [%s] %s%s" % (where, self.severity, self.code, path, self.message)

    def github_annotation(self):
        """GitHub Actions workflow command, shown inline on pull requests."""
        def escape(text, property_value=False):
            text = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            return text.replace(":", "%3A").replace(",", "%2C") if property_value else text
        props = ["file=%s" % escape(self.file or "", True)]
        if self.line is not None:
            props += ["line=%d" % self.line, "col=%d" % self.column]
        props.append("title=%s" % escape("streamwright-validate " + self.code, True))
        message = "%s: %s" % (self.path, self.message) if self.path else self.message
        return "::%s %s::%s" % (self.severity, ",".join(props), escape(message))

    def __repr__(self):
        return "Issue(%s)" % self


# * ---------------------------------
# * YAML loading with source locations
# * ---------------------------------

class _MarkedLoader(yaml.SafeLoader):
    """SafeLoader that remembers which YAML node every mapping/list came from."""

    def __init__(self, stream):
        super(_MarkedLoader, self).__init__(stream)
        self.streamwright_nodes = {}          # id(constructed dict/list) -> yaml node
        self.streamwright_anchor_marks = {}   # anchor name -> mark of its definition
        self.streamwright_aliases = set()     # anchor names referenced by *alias or <<: *alias
        self.streamwright_findings = []       # (severity, code, mark, message)

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent):
            self.streamwright_aliases.add(event.anchor)
            return super(_MarkedLoader, self).compose_node(parent, index)
        node = super(_MarkedLoader, self).compose_node(parent, index)
        if event.anchor is not None:
            self.streamwright_anchor_marks.setdefault(event.anchor, event.start_mark)
        return node

    def compose_mapping_node(self, anchor):
        node = super(_MarkedLoader, self).compose_mapping_node(anchor)
        seen = set()
        for key_node, _ in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == "tag:yaml.org,2002:merge":
                continue
            if key_node.value in seen:
                self.streamwright_findings.append((
                    ERROR, "duplicate-key", key_node.start_mark,
                    "duplicate key %r: YAML keeps only the last value" % key_node.value))
            seen.add(key_node.value)
        return node

    def construct_yaml_map(self, node):
        data = {}
        self.streamwright_nodes[id(data)] = node
        yield data
        data.update(self.construct_mapping(node))

    def construct_yaml_seq(self, node):
        data = []
        self.streamwright_nodes[id(data)] = node
        yield data
        data.extend(self.construct_sequence(node))

    def construct_yaml_bool(self, node):
        value = super(_MarkedLoader, self).construct_yaml_bool(node)
        if node.value.lower() in ("yes", "no", "on", "off"):
            self.streamwright_findings.append((
                WARNING, "yaml-boolean", node.start_mark,
                "%r is read as the boolean %s (YAML 1.1); write true/false, or quote it "
                "if you meant text" % (node.value, str(value).lower())))
        return value


_MarkedLoader.add_constructor("tag:yaml.org,2002:map", _MarkedLoader.construct_yaml_map)
_MarkedLoader.add_constructor("tag:yaml.org,2002:seq", _MarkedLoader.construct_yaml_seq)
_MarkedLoader.add_constructor("tag:yaml.org,2002:bool", _MarkedLoader.construct_yaml_bool)
# YAML 1.1 resolves a bare `=` to the "value" type; read it as text, like config_reader.load_yaml does
_MarkedLoader.add_constructor("tag:yaml.org,2002:value", yaml.SafeLoader.construct_scalar)


class _Loaders(object):
    """The YAML nodes and findings of the files of a source folder, which are checked as one document."""

    def __init__(self, loaders):
        self.streamwright_nodes = {}
        self.streamwright_findings = []
        self.streamwright_anchor_marks = {}  # anchors belong to their file, so unused ones are found per file below
        self.streamwright_aliases = set()
        for loader in loaders:
            self.streamwright_nodes.update(loader.streamwright_nodes)
            self.streamwright_findings.extend(loader.streamwright_findings)
            for anchor, mark in loader.streamwright_anchor_marks.items():
                if anchor not in loader.streamwright_aliases:
                    self.streamwright_findings.append((WARNING, "unused-anchor", mark,
                                                "anchor &%s is defined but never used" % anchor))


# * -------
# * helpers
# * -------

def _render_path(parts):
    out = ""
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool):
            out += "[%d]" % part
        elif isinstance(part, str) and part.isidentifier():
            out += ("." if out else "") + part
        else:
            out += "[%r]" % (part,)
    return out


def _describe(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text %r" % value if len(value) <= 40 else "text"
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    return type(value).__name__


def _suggest(word, candidates):
    if not isinstance(word, str):
        return ""
    match = difflib.get_close_matches(word, [c for c in candidates if isinstance(c, str)], n=1, cutoff=0.6)
    return " (did you mean %r?)" % match[0] if match else ""


class _Options(object):

    def __init__(self, kind=None, allowed_connectors=None, source_check=None):
        self.kind = kind
        self.allowed_connectors = allowed_connectors
        # source_check(checker, document) adds checks after the built-in ones pass (e.g. the connectors')
        self.source_check = source_check


# * ----------
# * the checks
# * ----------

class _Checker(object):
    """Reporting and generic checks; the checks of `kind: source` are in source_validator.SourceChecker."""

    def __init__(self, filename, loader, options):
        self.file = filename
        self.nodes = loader.streamwright_nodes
        self.loader = loader
        self.options = options
        self.issues = []
        # source folders: the files read, and the file of each stream (index in `streams` -> path)
        self.folder = None
        self.files = frozenset()
        self.stream_files = {}

    # reporting -------------------------------------------------------------

    def _mark(self, container, key):
        node = self.nodes.get(id(container)) if container is not None else None
        if node is None:
            return None
        if isinstance(node, yaml.MappingNode) and key is not None:
            for key_node, value_node in reversed(node.value):
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == str(key):
                    return key_node.start_mark
        if isinstance(node, yaml.SequenceNode) and isinstance(key, int) and 0 <= key < len(node.value):
            return node.value[key].start_mark
        return node.start_mark

    def _add(self, severity, code, path, message, container=None, key=None, mark=None, file=None):
        mark = mark or self._mark(container, key)
        path = tuple(path)
        if file is None:
            file = self.file
            part_files = {"streams": self.stream_files}.get(path[0] if path else None)
            if len(path) > 1 and part_files and path[1] in part_files:
                # a stream of a source folder: report it in its file, with the path inside that file
                file, path = part_files[path[1]], path[2:]
            if mark is not None and getattr(mark, "name", None) in self.files:
                file = mark.name
        line, column = (mark.line + 1, mark.column + 1) if mark is not None else (None, None)
        self.issues.append(Issue(severity, code, message, file, _render_path(path), line, column))

    def error(self, code, path, message, container=None, key=None):
        self._add(ERROR, code, path, message, container, key)

    def warn(self, code, path, message, container=None, key=None):
        self._add(WARNING, code, path, message, container, key)

    # generic value checks ----------------------------------------------------

    def _expect(self, ok, expected, value, path, container, key):
        if not ok:
            self.error("bad-value", path, "expected %s, got %s" % (expected, _describe(value)), container, key)
        return ok

    def require_keys(self, mapping, keys, path, what):
        for key in keys:
            if key not in mapping:
                self.error("missing-key", path, "%s needs `%s`" % (what, key), mapping)

    def unknown_keys(self, mapping, allowed, path, severity, consequence):
        for key in mapping:
            if key not in allowed:
                self._add(severity, "unknown-key", path + (key,),
                          "unknown key %r%s; %s" % (key, _suggest(key, allowed), consequence), mapping, key)

    # documents ---------------------------------------------------------------

    def check_preamble(self, data):
        """The YAML findings and the document's shape; returns False when the document cannot be checked further."""
        for severity, code, mark, message in self.loader.streamwright_findings:
            self._add(severity, code, (), message, mark=mark)
        for anchor, mark in sorted(self.loader.streamwright_anchor_marks.items(), key=lambda kv: kv[1].line):
            if anchor not in self.loader.streamwright_aliases:
                self._add(WARNING, "unused-anchor", (), "anchor &%s is defined but never used" % anchor, mark=mark)

        if data is None:
            self.error("empty-document", (), "the file is empty")
            return False
        if not isinstance(data, dict):
            self.error("bad-value", (), "a configuration must be a mapping, got %s" % _describe(data), data)
            return False
        return True


# * -----------
# * entry points
# * -----------

def _read(path):
    """(text, issues): the text is None when the file cannot be read."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return stream.read(), []
    except (IOError, OSError, UnicodeDecodeError) as exc:
        return None, [Issue(ERROR, "unreadable-file", str(exc), path)]


def _parse(text, filename):
    """(loader, data, issues) for YAML text, read with source locations; the loader is None when it is not YAML."""
    try:
        loader = _MarkedLoader(text)
    except yaml.YAMLError as exc:  # e.g. a control character
        return None, None, [Issue(ERROR, "yaml-syntax", str(exc), filename)]
    if filename:
        loader.name = filename  # every mark, and so every finding, knows its file
    try:
        return loader, loader.get_single_data(), []
    except yaml.MarkedYAMLError as exc:
        loader.dispose()
        mark = exc.problem_mark or exc.context_mark
        message = " ".join(str(p) for p in (exc.context, exc.problem) if p)
        line, column = (mark.line + 1, mark.column + 1) if mark is not None else (None, None)
        lines = text.splitlines()
        if line is not None and line <= len(lines) and "{{" in lines[line - 1]:
            message += "; quote template values, e.g. \"{{ config.x }}\""
        return None, None, [Issue(ERROR, "yaml-syntax", message, filename, "", line, column)]
    except yaml.YAMLError as exc:
        loader.dispose()
        return None, None, [Issue(ERROR, "yaml-syntax", str(exc), filename)]


def _sorted(issues, files=()):
    order = dict((path, index) for index, path in enumerate(files))
    return sorted(issues, key=lambda i: (order.get(i.file, -1), i.line or 0, i.column or 0))


def _source_checker():
    # source_validator imports this module, so it is imported when first used
    from streamwright.core.validation.source import SourceChecker
    return SourceChecker


def _check_kind(checker, data):
    """A single file: reports a missing or other `kind`; True when the file is to be checked as a source."""
    if not isinstance(data, dict):
        return True  # check_document reports it
    kind = data.get("kind", checker.options.kind)
    if kind == schema.KIND:
        return True
    if kind is None:
        checker.error("missing-key", (), "missing `kind: %s` (or pass --kind %s)" % (
            schema.KIND, schema.KIND), data)
        return True
    checker.check_preamble(data)
    in_file = "kind" in data
    checker.error("unknown-kind", ("kind",) if in_file else (), "unknown kind %r%s; streamwright-validate checks `kind: %s` "
                  "configurations" % (kind, _suggest(kind, (schema.KIND,)), schema.KIND),
                  data, "kind" if in_file else None)
    return False


def validate_text(text, filename=None, kind=None, allowed_connectors=None, source_check=None):
    """
    Validates YAML text; returns a list of Issue. For sources without errors, source_check(checker, document) can
    add more (see SourceChecker).
    """
    options = _Options(kind, allowed_connectors, source_check)
    loader, data, issues = _parse(text, filename)
    if loader is None:
        return issues
    try:
        checker = _source_checker()(filename, loader, options)
        if _check_kind(checker, data):
            checker.check_document(data)
        return _sorted(checker.issues)
    finally:
        loader.dispose()


def _validate_folder(layout, options):
    """A source folder: source.yaml and the stream files, checked as one source."""
    SourceChecker = _source_checker()
    issues = [Issue(ERROR, "source-folder", message, path) for path, message in layout.problems]
    loaders, documents = [], []
    try:
        for path in layout.files:
            text, problems = _read(path)
            loader, data, problems = (None, None, problems) if text is None else _parse(text, path)
            issues.extend(problems)
            if loader is not None:
                loaders.append(loader)
            documents.append(data)
        if len(loaders) < len(documents):  # a file is unreadable or not YAML: the source cannot be put together
            return _sorted(issues, layout.files)

        source = documents[0]
        problems, stream_paths = source_files.assemble(layout, source, documents[1:])
        combined = _Loaders(loaders)
        checker = SourceChecker(layout.source_file, combined, options)
        checker.folder = layout.folder
        checker.files = frozenset(layout.files)
        checker.stream_files = dict(enumerate(stream_paths))
        for key, paths in (("streams", stream_paths),):
            if paths and isinstance(source, dict):
                # the list is made here, not read: give it a node whose items are the files' mappings
                top = combined.streamwright_nodes[id(source)]
                combined.streamwright_nodes[id(source[key])] = yaml.SequenceNode(
                    "tag:yaml.org,2002:seq", [combined.streamwright_nodes[id(item)] for item in source[key]],
                    top.start_mark, top.end_mark)
        for path, container, key, message in problems:
            checker._add(ERROR, "source-folder", (), message, container, key, file=path)
        if isinstance(source, dict) and source.get("kind", options.kind) != schema.KIND:
            if "kind" in source:
                checker.error("bad-value", ("kind",), "a source folder's source.yaml is `kind: source`, got %r" % (
                    source["kind"],), source, "kind")
                return _sorted(issues + checker.issues, layout.files)
            checker.error("missing-key", (), "missing `kind: source`", source)
        checker.check_document(source)
        return _sorted(issues + checker.issues, layout.files)
    finally:
        for loader in loaders:
            loader.dispose()


def validate_source(path, kind=None, allowed_connectors=None, source_check=None):
    """
    Validates the source at `path`: a source folder as one source (given as the folder, its source.yaml or one of
    its stream files; or a SourceLayout), or a single file. Returns a list of Issue.
    """
    if isinstance(path, source_files.SourceLayout):
        layout = path
    else:
        if not os.path.exists(path):
            return [Issue(ERROR, "unreadable-file", source_files.missing_path_message(path), path)]
        try:
            layout = source_files.find_source(path)
        except source_files.SourceFilesError as exc:
            return [Issue(ERROR, "source-folder", str(exc), path)]
    if layout.folder is not None:
        return _validate_folder(layout, _Options(kind, allowed_connectors, source_check))
    text, issues = _read(layout.source_file)
    if text is None:
        return issues
    return validate_text(text, layout.source_file, kind, allowed_connectors, source_check)


def validate_file(path, kind=None, allowed_connectors=None, source_check=None):
    """Validates one YAML file (for a file of a source folder: the whole folder); returns a list of Issue."""
    if source_files.source_folder(path) is not None:
        return validate_source(path, kind, allowed_connectors, source_check)
    text, issues = _read(path)
    if text is None:
        return issues
    return validate_text(text, path, kind, allowed_connectors, source_check)


def _units(path):
    """What to check under `path`, in order: (folder, True) for a source folder, checked as one, or (file, False)."""
    if not os.path.isdir(path):
        folder = source_files.source_folder(path)
        return [(folder, True) if folder is not None else (path, False)]
    units = []
    for root, dirs, files in os.walk(path):
        folder = source_files.source_folder(root)  # the folder itself, or the source of a streams/ folder
        if folder is not None:
            units.append((folder, True))
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.endswith((".yaml", ".yml")):
                file_path = os.path.join(root, name)
                folder = source_files.source_folder(file_path)  # e.g. a streams/ folder given on its own
                units.append((folder, True) if folder is not None else (file_path, False))
    return units


def validate_paths(paths, kind=None, allowed_connectors=None, source_check=None):
    """
    Validates files and directories (recursively; each source folder as one source); returns
    (files_checked, issues).
    """
    files, issues, folders = [], [], set()
    for path in paths:
        if not os.path.exists(path):
            issues.append(Issue(ERROR, "unreadable-file", source_files.missing_path_message(path), path))
            continue
        for unit, is_folder in _units(path):
            if not is_folder:
                files.append(unit)
                issues.extend(validate_file(unit, kind, allowed_connectors, source_check))
                continue
            if os.path.realpath(unit) in folders:
                continue
            folders.add(os.path.realpath(unit))
            try:
                layout = source_files.find_source(unit)
            except source_files.SourceFilesError as exc:
                issues.append(Issue(ERROR, "source-folder", str(exc), unit))
                continue
            files.extend(layout.files)
            issues.extend(validate_source(layout, kind, allowed_connectors, source_check))
    return files, issues


def export_schemas(directory):
    """Writes source.schema.json and stream.schema.json (the stream files of source folders) to directory."""
    schemas = [("%s.schema.json" % schema.KIND, schema.json_schema()),
               ("stream.schema.json", schema.stream_json_schema())]
    written = []
    for relative, document in schemas:
        target = os.path.join(directory, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
        written.append(target)
    return written


def _parser(prog="streamwright-validate"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Validate StreamWright source configurations (`kind: source`) without running them.")
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="files or directories to validate, recursively; a source folder (source.yaml and "
                             "streams/) is checked as one source (default: $STREAMWRIGHT_CONFIGS)")
    parser.add_argument("--kind", choices=(schema.KIND,),
                        help="kind to assume for files that do not declare `kind`")
    parser.add_argument("--allow-connector", action="append", dest="allowed_connectors", metavar="NAME",
                        help="sources: only allow these connectors in `auth.provider` and `sdk` (repeatable)")
    parser.add_argument("--strict", action="store_true", help="exit with status 1 on warnings too")
    parser.add_argument("--format", choices=("text", "json", "github"), default="text",
                        help="output format; `github` prints GitHub Actions annotations")
    parser.add_argument("--export-schema", metavar="DIR",
                        help="write the JSON Schemas of sources and stream files to DIR for editor autocomplete, "
                             "then exit")
    return parser


def main(argv=None, source_check=None, prog="streamwright validate", non_strict_codes=()):
    """The `streamwright validate` command; it passes a source_check that also runs the connectors' checks.

    non_strict_codes: warning codes that --strict does NOT escalate (e.g. an environment missing a connector
    the config references is not a defect in the config).
    """
    parser = _parser(prog)
    args = parser.parse_args(argv)

    if args.export_schema:
        for path in export_schemas(args.export_schema):
            print(path)
        return 0

    configs_env = os.environ.get("STREAMWRIGHT_CONFIGS")
    paths = args.paths or ([configs_env] if configs_env else [])
    if not paths:
        parser.print_usage(sys.stderr)
        print("%s: error: give a PATH or set STREAMWRIGHT_CONFIGS" % prog, file=sys.stderr)
        return 2

    files, issues = validate_paths(paths, args.kind, args.allowed_connectors, source_check)
    errors = sum(1 for i in issues if i.severity == ERROR)
    warnings = len(issues) - errors
    strict_warnings = sum(1 for i in issues if i.severity != ERROR and i.code not in non_strict_codes)

    if args.format == "json":
        print(json.dumps({"files": len(files), "errors": errors, "warnings": warnings,
                          "issues": [i.to_dict() for i in issues]}, indent=2))
    else:
        for issue in issues:
            print(issue.github_annotation() if args.format == "github" else issue)
        print("%d file(s) checked: %d error(s), %d warning(s)" % (len(files), errors, warnings))

    return 1 if errors or (args.strict and strict_warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
