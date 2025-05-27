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

from typing import Any, Dict, List, Optional, Callable

import adapt.utils


__all__ = [
    "init"
]


def class_name(string, prefix="Type"):
    # type: (str, str) -> str
    return prefix + ''.join(x.capitalize() or '_' for x in string.split('_'))


def get_type(name):
    return globals()[class_name(name)]


def get_args(params, prefix="_"):
    # type: (Dict, str) -> Dict
    return dict([("%s%s" % (prefix, k), v) for k, v in params.items()
                 if k not in ("type",)])


def call(name, params, store=None):
    # type: (str, Dict, adapt.utils.Store) -> Any[Callable, Dict, str, bool]
    _args = get_args(params)
    type_ = get_type(name)
    # print("call", type_, params)
    if getattr(type_, "has_store_access", False):
        _args["_store"] = store
    return get_type(name).call(**_args)


def init(config, external_input=None):
    # type: (dict, adapt.utils.Store) -> Any[Callable, Dict, str, bool]
    return call(config["type"], config, store=external_input)


# * ----------------
# * TYPE DEFINITIONS
# * ----------------

class _FormatAs(object):
    _format_as_func_map = {
        "DOUBLE_QUOTED_LIST": lambda x: "({})".format(', '.join('"{}"'.format(str(item)) for item in x)),
        "SINGLE_QUOTED_LIST": lambda x: "({})".format(', '.join("'{}'".format(str(item)) for item in x)),
        "INT_LIST": lambda x: "({})".format(', '.join(str(item) for item in x)),
        "STRING_DOUBLE_QUOTED": lambda x: '"{}"'.format(str(x)),
        "STRING_SINGLE_QUOTED": lambda x: "'{}'".format(str(x)),
        "STRING_UNQUOTED": lambda x: str(x),
        "INT": lambda x: int(x),
    }

    @staticmethod
    def call(_value, _split_on=None, _format_as=None):
        if _format_as is not None and _format_as not in _FormatAs._format_as_func_map:
            raise Exception("Invalid quote type: {!r}".format(_format_as))
        if _split_on is not None:
            _value = _value.split(_split_on)
        if _format_as is not None:
            _value = _FormatAs._format_as_func_map[_format_as](_value)
        return _value


class TypeConstant(object):

    @staticmethod
    def call(_value, _split_on=None, _format_as=None):
        return _FormatAs.call(_value, _split_on, _format_as)


IGNORE_POISON_PILL = "##IGNORE##"
NOT_FOUND_POISON_PILL = "##NOT_FOUND##"


class TypeExternalInput(object):
    has_store_access = True

    @staticmethod
    def call(_key, _store, _required=False, _ignore_if=IGNORE_POISON_PILL, _split_on=None, _format_as=None):
        # type: (str, adapt.utils.Store, bool, Any, str, str) -> Any[Dict, str, bool]
        val = _store.get(_key, required=_required, poison_pill=NOT_FOUND_POISON_PILL)
        if val == NOT_FOUND_POISON_PILL:
            raise Exception("Required external input {!r} not found".format(_key))
        if val == _ignore_if:
            return IGNORE_POISON_PILL
        return _FormatAs.call(val, _split_on, _format_as)


class TypeQueryBuilder(object):
    has_store_access = True

    @staticmethod
    def call(_query, _filters, _store):
        # type: (dict, dict, adapt.utils.Store) -> str
        """
        Flexible query builder that constructs queries from base query + optional filter parts.
        Supports conditional WHERE clauses based on available input parameters.
        """
        query_parts = [call(_query['type'], _query, _store).strip()]
        filters = call(_filters['type'], _filters, _store)
        if filters:
            query_parts.append('WHERE ' + filters)
        return " ".join(query_parts)


class TypeDict(object):
    has_store_access = True

    @staticmethod
    def call(_items, _store):
        # type: (dict, adapt.utils.Store) -> dict
        return {
            key: call(props['type'], props, _store)
            for key, props in _items.items()
        }


class TypeCallable(object):

    @staticmethod
    def call(_module, _class, _method):
        # type: (str, str, str) -> Callable
        import importlib
        module = importlib.import_module(_module)
        class_ = getattr(module, _class)
        method = getattr(class_, _method)
        return method


class TypeInstance(object):
    has_store_access = True

    @staticmethod
    def call(_module, _class, _arguments, _store):
        # type: (str, str, Dict, adapt.utils.Store) -> object
        init_args = call(_arguments['type'], _arguments, _store)
        import importlib
        module = importlib.import_module(_module)
        class_ = getattr(module, _class)(**init_args)
        return class_


class TypeInitializer(object):
    has_store_access = True

    @staticmethod
    def call(_client, _arguments, _store):
        # type: (dict, dict, adapt.utils.Store) -> object
        call_method = call(_client['type'], _client, _store)
        params = call(_arguments['type'], _arguments, _store)
        return call_method(**params)


class TypeFromAuthorizer(object):
    has_store_access = True

    @staticmethod
    def call(_method, _arguments, _store):
        # type: (str, Dict, adapt.utils.Store) -> object
        auth_client = _store.get("authorization", required=True)
        args = call(_arguments['type'], _arguments, _store)
        return getattr(auth_client, _method)(**args)


class TypeList(object):
    has_store_access = True

    @staticmethod
    def call(_items, _store):
        # type: (List, adapt.utils.Store) -> List
        return [
            call(item['type'], item, _store)
            if (isinstance(item, dict) and "type" in item) else item
            for item in _items
        ]


class TypeFilter(object):
    has_store_access = True

    @staticmethod
    def call(_items, _schema, _store, _json_dumps=None):
        key_, op_, val_ = _schema["key"], _schema["operator"], _schema["value"]
        result = []
        for key, props in _items.items():
            val = props["value"]
            if isinstance(val, dict) and "type" in val:
                val = call(val['type'], val, _store)
            if val == IGNORE_POISON_PILL:
                continue
            result.append({
                key_: key,
                op_: props["operator"],
                val_: val
            })
        if _json_dumps:
            import json
            result = json.dumps(result)
        return result


class TypeSqlQuery(object):
    has_store_access = False

    @staticmethod
    def call(_query):
        # type: (dict) -> str
        return _query


class TypeSqlFilter(object):
    has_store_access = True
    
    @staticmethod
    def call(_items, _store, _json_dumps=None):
        result = []
        for key, props in _items.items():
            val = props["value"]
            if isinstance(val, dict) and "type" in val:
                val = call(val['type'], val, _store)
            if val == IGNORE_POISON_PILL:
                continue
            result.append(f"{key} {props['operator']} {val}")
        return " AND ".join(result)


class TypePipeline(object):
    has_store_access = True

    @staticmethod
    def call(_name, _client, _store, _forward_to=None, _arguments=None):
        # type: (str, dict, adapt.utils.Store, Optional[dict], Optional[dict]) -> object
        _callable = call(_client['type'], _client, _store)

        params = {}
        if _arguments:
            params = call(_arguments['type'], _arguments, _store)

        forwards = []
        if _forward_to:
            forwards = [
                {"forward_to": name, "name": props["as_arg"]}
                for name, props in _forward_to.items()
            ]
        return {
            "name": _name,
            "processor": _callable,
            "arguments": params,
            "forward_to": forwards
        }


# * ---------------------
# * END: TYPE DEFINITIONS
# * ---------------------


def test_arguments_processor():
    external_input = adapt.utils.Store()
    external_input.add(key='refresh_token', value='77963b7a931377ad4ab5ad6a9cd718aa')
    external_input.add(key='customer_id', value='123456789')

    sample_args = {
        "type": "dict",
        "items": {
            "version": {
                "type": "constant",
                "value": "v1"
            },
            "config_dict": {
                "type": "dict",
                "items": {
                    "token": {
                        "type": "external_input",
                        "key": "refresh_token"
                    },
                    "account_id": {
                        "type": "external_input",
                        "key": "customer_id"
                    },
                    "debug": {
                        "type": "constant",
                        "value": False
                    }
                }
            }
        }
    }

    params = init(sample_args, external_input)
    print("testing TypeDict:", params)
    assert params['version'] == 'v1'
    assert params['config_dict']['token'] == '77963b7a931377ad4ab5ad6a9cd718aa'
    assert params['config_dict']['account_id'] == '123456789'
    assert params['config_dict']['debug'] is False


def test_filter():
    sample_args = {
        "type": "filter",
        "json_dumps": False,
        "schema": {
            "key": "name",
            "operator": "op",
            "value": "val"
        },
        "items": {
            "status": {
                "operator": "IN",
                "value": ["ACTIVE", "PAUSED"]
            },
            "id": {
                "operator": "IN",
                "value": {
                    "type": "external_input",
                    "key": "campaign_ids"
                }
            },
            "goal": {
                "operator": "IN",
                "value": {
                    "type": "external_input",
                    "key": "goals",
                    "ignore_if": None
                }
            }
        }
    }

    external_input = adapt.utils.Store()
    external_input.add(key='campaign_ids', value=['123456789', '987654321'])

    params = init(sample_args, external_input)
    print("testing TypeFilter:", params)
    assert isinstance(params, list)
    assert len(params) == 2
    assert isinstance(params[0], dict)
    assert sorted(list(params[0].keys())) == sorted(['name', 'op', 'val'])


if __name__ == "__main__":
    test_arguments_processor()
    test_filter()
