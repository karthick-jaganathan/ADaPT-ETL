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
The files of a source (`kind: source`): one YAML file, or a source folder.

    google_ads/
    ├── source.yaml                  kind, name, description, spec, auth, http: everything except the streams
    └── streams/
        ├── campaigns.yaml           one stream per file; the file name is the stream name
        └── campaign_performance.yaml

A folder is read as one source, so its streams share the inputs, the sign-in and the rate limit, can use each
other as `from_stream` parents. A source.yaml without a streams/ folder next to it, like any
other file, is a single-file source that lists its `streams`. Used by streamwright-validate and by the
runtime (streamwright run).
"""

import os

from streamwright.core.config.reader import load_yaml


__all__ = ["SourceFilesError", "SourceLayout", "SOURCE_FILES", "STREAMS_FOLDER", "MODELS_FOLDER", "find_source",
           "source_folder", "assemble", "load_source", "missing_path_message"]

SOURCE_FILES = ("source.yaml", "source.yml")
STREAMS_FOLDER = "streams"
MODELS_FOLDER = "models"
MODELS_REMOVED_HINT = "models were replaced by `transform` steps: a stream's SQL steps join and aggregate its own " \
                      "`requests`, and its `export` names the outputs"
YAML_SUFFIXES = (".yaml", ".yml")
# the folders of a source folder that hold one file per stream: (folder, what one file holds, source key)
_PART_FOLDERS = ((STREAMS_FOLDER, "stream", "streams"),)


class SourceFilesError(ValueError):
    """The path is not a source, or the files of a source folder do not make one source."""


class SourceLayout(object):
    """The files of a source: one file (`folder` is None), or a source folder with one file per stream."""

    def __init__(self, source_file, folder=None, stream_files=(), problems=()):
        self.source_file = source_file
        self.folder = folder
        self.stream_files = list(stream_files)  # [(stream name, path)], in file name order
        self.problems = list(problems)          # [(path, message)]: what in streams/ or models/ cannot be read

    @property
    def files(self):
        return [self.source_file] + [path for _, path in self.stream_files]

    def stream_of(self, path):
        """The name of the stream whose file is `path`, or None."""
        return _name_of(self.stream_files, path)


def _name_of(files, path):
    target = os.path.abspath(path)
    for name, file_path in files:
        if os.path.abspath(file_path) == target:
            return name
    return None


def _source_file_in(folder):
    found = [os.path.join(folder, name) for name in SOURCE_FILES if os.path.isfile(os.path.join(folder, name))]
    if len(found) > 1:
        raise SourceFilesError("%s has both %s; keep one" % (folder, " and ".join(SOURCE_FILES)))
    return found[0] if found else None


def _is_source_folder(folder):
    return os.path.isdir(os.path.join(folder, STREAMS_FOLDER)) and \
        any(os.path.isfile(os.path.join(folder, name)) for name in SOURCE_FILES)


def source_folder(path):
    """
    The source folder that `path` belongs to, or None: the folder itself, its source.yaml, its streams/ or models/
    folder, or a file in them (even one named source.yaml: there it is a stream). A source folder has a
    source.yaml (or source.yml) and a streams/ folder.
    """
    part_folders = [folder for folder, _, _ in _PART_FOLDERS] + [MODELS_FOLDER]
    if os.path.isdir(path):
        if os.path.basename(os.path.abspath(path)) in part_folders:
            parent = os.path.normpath(os.path.join(path, os.pardir))
            if _is_source_folder(parent):
                return parent
        return path if _is_source_folder(path) else None
    parent, name = os.path.split(path)
    parent = parent or os.curdir
    if name.endswith(YAML_SUFFIXES) and os.path.basename(os.path.abspath(parent)) in part_folders:
        folder = os.path.normpath(os.path.join(parent, os.pardir))
        if _is_source_folder(folder):
            return folder
    if name in SOURCE_FILES and _is_source_folder(parent):
        return parent
    return None


def _part_files(folder, part_folder, what):
    """The (name, path) of each file in `folder` (e.g. streams/: one stream per file), and the entries that are not."""
    files, problems, names = [], [], {}
    for entry in sorted(os.listdir(folder)):
        path = os.path.join(folder, entry)
        if entry.startswith("."):
            continue
        if os.path.isdir(path):
            problems.append((path, "folders inside %s/ are not read: put each %s file directly in %s/" % (
                part_folder, what, part_folder)))
            continue
        name, suffix = os.path.splitext(entry)
        if suffix not in YAML_SUFFIXES:
            continue  # e.g. a README
        if name in names:
            problems.append((path, "%s %r already has the file %s" % (what, name, os.path.basename(names[name]))))
            continue
        names[name] = path
        files.append((name, path))
    return files, problems


def missing_path_message(path):
    """Why a `path` that does not exist cannot be read; names the source folder it may stand for (x.yaml -> x/)."""
    stem, suffix = os.path.splitext(os.path.normpath(path))
    if suffix in YAML_SUFFIXES and os.path.isdir(stem) and source_folder(stem) == stem:
        return "no such file or folder; did you mean the source folder %s?" % stem
    return "no such file or folder"


def find_source(path):
    """
    The SourceLayout of the source at `path`: a source folder (given as the folder, its source.yaml, its streams/
    folder or one of its stream files), a folder with only a source.yaml, or a file. Raises SourceFilesError when
    there is none.
    """
    if not os.path.exists(path):
        raise SourceFilesError("%s: %s" % (path, missing_path_message(path)))
    folder = source_folder(path)
    if folder is not None:
        stream_files, problems = _part_files(os.path.join(folder, STREAMS_FOLDER), STREAMS_FOLDER, "stream")
        if os.path.isdir(os.path.join(folder, MODELS_FOLDER)):
            problems.append((os.path.join(folder, MODELS_FOLDER), MODELS_REMOVED_HINT))
        return SourceLayout(_source_file_in(folder), folder, stream_files, problems)
    if os.path.isdir(path):
        source_file = _source_file_in(path)
        if source_file is None:
            raise SourceFilesError("%s is a folder without a source.yaml" % path)
        return SourceLayout(source_file)
    return SourceLayout(path)


def assemble(layout, source, documents):
    """
    Adds a source folder's streams to its source.yaml document `source`, in place. `documents` are the documents of
    layout.stream_files, in order; each becomes a stream named after its file. Returns (problems, stream_paths):
    problems as [(path, container, key, message)], and the file of each item of source["streams"].
    """
    problems, paths = [], {}
    parts = ((layout.stream_files, documents[:len(layout.stream_files)]),)
    for (part_folder, what, key), (files, part_documents) in zip(_PART_FOLDERS, parts):
        if isinstance(source, dict) and key in source:
            problems.append((layout.source_file, source, key, "in a source folder each %s is its own file: move "
                             "these %ss to %s/<name>.yaml" % (what, what, part_folder)))
        assembled, paths[key] = [], []
        for (name, path), document in zip(files, part_documents):
            if document is None:
                problems.append((path, None, None, "the %s file is empty" % what))
                continue
            if not isinstance(document, dict):
                problems.append((path, document, None, "a %s file holds one %s (a mapping), got %s" % (
                    what, what, "a list" if isinstance(document, list) else "text or a number")))
                continue
            if "name" in document:
                problems.append((path, document, "name", "remove `name`: the file name is the %s name (%s)" % (
                    what, name)))
            # name first, keeping the mapping itself: streamwright-validate maps it back to its YAML node
            items = [(item_key, value) for item_key, value in document.items() if item_key != "name"]
            document.clear()
            document["name"] = name
            document.update(items)
            assembled.append(document)
            paths[key].append(path)
        if isinstance(source, dict) and assembled:
            source[key] = assembled
    return problems, paths["streams"]


def load_source(path):
    """Reads the source at `path` (a path, see find_source, or a SourceLayout) as one document."""
    layout = path if isinstance(path, SourceLayout) else find_source(path)
    source = load_yaml(layout.source_file)
    if layout.folder is None:
        return source
    problems = list(layout.problems)
    found, _ = assemble(layout, source, [load_yaml(part_path) for part_path in layout.files[1:]])
    problems += [(problem_path, message) for problem_path, _, _, message in found]
    if not isinstance(source, dict):
        problems.append((layout.source_file, "expected a mapping"))
    elif "streams" not in source:
        problems.append((os.path.join(layout.folder, STREAMS_FOLDER), "no stream files"))
    if problems:
        raise SourceFilesError("; ".join("%s: %s" % problem for problem in problems))
    return source
