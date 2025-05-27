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

from typing import Any, Dict, List, Optional, Tuple, Generator
import inspect

from adapt.serializer import serializer_typing


__all__ = [
    "Serializer"
]


def _is_generator(obj):
    return inspect.isgenerator(obj) or inspect.isgeneratorfunction(obj)


_INLINE_TOKEN = "inline"
_DERIVED_TOKEN = "derived"
_CONSTANTS_TOKEN = "constants"
_IGNORE_TOKEN = "ignore"


class _SerializerTyping(object):
    """
    Provides the serializing type conversion methods
    till the end of the serialization process.
    """

    def __init__(self, config, key_stack=None):
        # type: (dict, set) -> None
        self.config = config
        self._serializers = {}
        self._keys = set()
        # key_stack is used to keep track of all the keys
        self._key_stack = set() if key_stack is None else key_stack
        self.make()
        self._update_key_stack(self._keys)

    def _add_key(self, token, key):
        if token not in (_IGNORE_TOKEN, "array", "extended_array"):
            self._keys.add(key)

    def _update_keys(self, keys):
        self._keys.update(keys)

    def _update_key_stack(self, keys):
        self._key_stack.update(keys)

    def get_master_keys(self):
        return self._key_stack

    def get_keys(self):
        return self._keys

    def _add_serializer(self, token, name, serializer):
        if (token, name) in self._serializers:
            raise Exception("Duplicate serializer found for "
                            "token: %s, name: %s" % (token, name))
        self._serializers[(token, name)] = serializer
        self._add_key(token, name)

    def get_serializer(self, token, name):
        return self._serializers[(token, name)]

    def make(self):
        self._mk_serializer(_INLINE_TOKEN)
        self._mk_serializer(_DERIVED_TOKEN)
        self._mk_constant(_CONSTANTS_TOKEN)

    @classmethod
    def _init(cls, config, key_stack=None):
        return cls(config, key_stack=key_stack)

    def _mk_serializer(self, token):
        for field in self.config.get(token, []):
            if "type" in field and field["type"] in ("array", "extended_array"):
                serializer = self._init(config=field, key_stack=self._key_stack)
                self._add_serializer(field["type"], field["name"], serializer)
                self._update_keys(serializer.get_keys())
                continue
            f = self._get_transformer(field["transform"]["type"], field["transform"])
            self._add_serializer(token, field["name"], f)
            self._mk_ignore(field, token=_IGNORE_TOKEN)

    def _mk_constant(self, token):
        for field in self.config.get(token, []):
            f = self._get_transformer("constant", field)
            self._add_serializer(token, field["name"], f)

    def _mk_ignore(self, field, token):
        if "ignore" in field:
            ignore_field = field["ignore"]["when"].get("field") or field["from"]
            field["ignore"]["when"]["field"] = ignore_field
            f = self._get_transformer("ignore", field["ignore"])
            self._add_serializer(token, field["name"], f)

    def _get_transformer(self, name, field):
        return serializer_typing.init_type(name, field)


class Serializer(object):
    """
    Serializes the given records based on the config definition.
    """

    _NESTED_OBJECT_NOT_FOUND = "#$OBJECT_NOT_FOUND$"

    def __init__(self, serializer, dict_normalize=False):
        # type: (_SerializerTyping, Optional[bool]) -> None
        self._serializer = serializer
        self._dict_normalize = dict_normalize

    def _normalized_store(self):
        return dict.fromkeys(self._serializer.get_master_keys())

    @staticmethod
    def _is_object_not_found(row):
        return row == "$OBJECT_NOT_FOUND$"

    def _get_inner_object(self, row, field):
        if "object" not in field:
            return row
        for key in field["object"].split("."):
            if key in row:
                row = row[key]
            else:
                return self._NESTED_OBJECT_NOT_FOUND
        return row

    def get_config(self, token):
        return self._serializer.config.get(token, [])

    @classmethod
    def _handel_array(cls, records, serializer, dict_normalize=False):
        serializer = cls(serializer=serializer, dict_normalize=dict_normalize)
        return serializer.serialize_records(records=records)

    @staticmethod
    def _extend_records(records):
        def _extend_with(record):
            yield record
            for _record in records:
                _record.update(record)
                yield _record
        return _extend_with

    def _process_inline(self, row, store):
        """
        processes the inline fields i.e., resolves the actual row into
        a serialized form based on config definition
        """
        extender = None
        for field in self.get_config(_INLINE_TOKEN):
            if "type" in field and field["type"] == "array":
                # processing nested array objects and storing them in the store
                # as array with only the fields defined in that section
                _serializer = self._get_serializer("array", field)
                serialized_rows = self._handel_array(row[field["from"]],
                                                     serializer=_serializer,
                                                     dict_normalize=False)
                self._push(store, field, list(serialized_rows))
                continue
            if "type" in field and field["type"] == "extended_array":
                # processing nested array objects and returning them as a new
                # record, instead of storing as array of the key field in the
                # store object
                _serializer = self._get_serializer("extended_array", field)
                serialized_rows = self._handel_array(
                    row[field["from"]],
                    serializer=_serializer,
                    dict_normalize=self._dict_normalize
                )
                extender = self._extend_records(serialized_rows)
                continue
            _row = self._get_inner_object(row, field)
            if _row == self._NESTED_OBJECT_NOT_FOUND:
                self._push(store, field, None)
                continue
            self._serialize(_row, store, field)
        return extender

    def _get_serializer(self, token, field):
        return self._serializer.get_serializer(token, field["name"])

    def _serialize(self, row, store, field):
        """
        processes the inline fields i.e., resolves the actual row into
        a serialized form based on config definition
        """
        if self._ignore(row, field, store):
            return
        _type = field["transform"]["type"]
        serialize = self._get_serializer(_INLINE_TOKEN, field)
        value = row if _type in ("case", ) else row[field["from"]]
        self._push(store, field, serialize(value))

    def _process_constants(self, store):
        """
        processes the constant value fields
        """
        for field in self.get_config(_CONSTANTS_TOKEN):
            value = self._get_serializer(_CONSTANTS_TOKEN, field)()
            self._push(store, field, value)

    def _process_derived(self, store):
        """
        processes the derived fields
        """
        for field in self.get_config(_DERIVED_TOKEN):
            if self._ignore(store, field, store):
                continue
            serialize = self._get_serializer(_DERIVED_TOKEN, field)
            value = store[field["from"]] if "from" in field else store
            self._push(store, field, serialize(value))

    def _push(self, store, field, value):
        """
        pushes the value into the store
        """
        store[field["name"]] = value

    def _ignore(self, row, field, store):
        """
        if ignore is not present in the field, then return False
        if ignore is present, and if it meets the criteria, then value of
        the ignore field will be set, and returns True
        """
        if "ignore" not in field:
            return False
        value = None
        serialize = self._get_serializer(_IGNORE_TOKEN, field)
        try:
            # field will be set to none if it is not found
            value = serialize(row)
        except KeyError:
            pass

        # if value is "##$IGNORE_PIL", then do not ignore the field
        if value == "##$IGNORE_PIL":
            return False
        self._push(store, field, value)
        return True

    def _build_store(self, store):
        # type: (Optional[dict]) -> dict
        store = {} if store is None else store
        if self._dict_normalize:
            _store = self._normalized_store()
            _store.update(store)
            store = _store
        return store

    def serialize(self, row, store=None):
        # type: (dict, Optional[dict]) -> dict
        """
        transforms the given raw row into a serialized form
        """
        store = self._build_store(store)
        inline_extender = self._process_inline(row, store)
        self._process_derived(store)
        self._process_constants(store)
        if inline_extender:
            return inline_extender(store)
        return store

    def serialize_records(self, records):
        # type: (Any[List[dict], Tuple[dict], Generator[dict]]) -> Generator
        for row in records:
            serialize_row = self.serialize(row)
            if _is_generator(serialize_row):
                for _row in serialize_row:
                    yield _row
            else:
                yield serialize_row

    @classmethod
    def init(cls, config, dict_normalize=False):
        # type: (Dict, Optional[bool]) -> Serializer
        return cls(_SerializerTyping(config), dict_normalize=dict_normalize)

    @classmethod
    def lazy_run(cls, config, records, dict_normalize=False):
        # type: (Dict, Any[List[dict], Generator], Optional[bool]) -> Generator
        return cls.init(config, dict_normalize=dict_normalize).serialize_records(records)
