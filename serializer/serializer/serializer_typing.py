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

from typing import Any, List, Dict


__all__ = [
    "TypeDate",
    "TypeFloat",
    "TypeInteger",
    "TypeBool",
    "TypeString",
    "TypeEnum",
    "TypeCase",
    "TypeConstant",
    "TypeIgnore",
    "init_type",
]


def class_name(string, prefix="Type"):
    return prefix + ''.join(x.capitalize() or '_' for x in string.split('_'))


def get_type(name):
    return globals()[class_name(name)]


def args(params):
    return dict([( "_%s" % k, v) for k, v in params.items()
                 if k not in ("type",)])


def init_type(name, params):
    return get_type(name)(**args(params))


# ------------------------------------------------------------------
#                           TYPE DEFINITIONS
# ------------------------------------------------------------------

class TypeDate(object):
    """ converting date from one format to another"""

    def __init__(self, _format):
        self.input = _format["input"]
        self.output = _format["output"]

    def __call__(self, value):
        from datetime import datetime
        return datetime.strptime(value, self.input).strftime(self.output)


class TypeDateParser(object):
    """ converting date from one format to another"""

    def __init__(self, _format):
        self.output = _format["output"]

    def __call__(self, value):
        from dateutil import parser
        date = parser.parse(value).date()
        return date.strftime(self.output)


class TypeFloat(object):
    """ converting float from one format to another"""

    def __init__(self, _precision):
        self.precision = _precision

    def __call__(self, value):
        return round(float(str(value)), self.precision)


class TypeInteger(object):
    """ converting into integer """
    def __init__(self):
        pass

    def __call__(self, value):
        return int(value)


class TypeBool(object):
    """ converting into boolean """
    def __init__(self):
        pass

    def __call__(self, value):
        return bool(value)


class TypeString(object):
    """ converting into string """
    def __init__(self):
        pass

    def __call__(self, value):
        return str(value)


class TypeEnum(object):
    """ mapping a value to another value """

    def __init__(self, _mappings, _on_error="##ON_ERROR_TOKEN##"):
        self.mappings = _mappings
        self.on_error = _on_error

    def __call__(self, value):
        if value in self.mappings:
            return self.mappings[value]
        elif self.on_error == "##ON_ERROR_TOKEN##":
            raise Exception("Value {!r} not found in mappings {!r}".format(value, self.mappings))
        else:
            return self.on_error


class TypeCurrency(object):
    """ multiplying a value by a multiplier"""
    def __init__(self, _multiplier, _rounding=2):
        self.multiplier = _multiplier
        self.rounding = _rounding

    def __call__(self, value):
        return round(float(value) * self.multiplier, self.rounding)


class _TypeCaseProxy(object):

    symbols = {
        "greater_than": lambda self, value: value and value > self.when,
        "less_than": lambda self, value: value and value < self.when,
        "equal": lambda self, value: value == self.when,
        "not_equal": lambda self, value: value != self.when,
        "in": lambda self, value: value in self.when,
        "not_in": lambda self, value: value not in self.when,
        "null": lambda _, value: value is None,
        "not_null": lambda _, value: value is not None,
    }

    @classmethod
    def _derive_op_name(cls, when):
        # type: (Dict) -> str
        op = (set(when.keys()) - {"field"}) & set(cls.symbols.keys())
        if len(op) != 1:
            raise ValueError(f"Invalid ignore operator: {op}")
        return list(op)[0]

    @classmethod
    def create(cls, when, then, poison_pill):
        def method(symbol):
            def __call__(self, value):
                if symbol(self, value[self.field]):
                    if isinstance(self.then._then, dict) and \
                            "field" in self.then._then:
                        return self.then(value)
                    else:
                        return self.then(value[self.field])
                else:
                    return self._poison_pill
            return __call__

        def init():
            def __init__(self, _when, _then, _field=None, _poison_pill=poison_pill):
                self.when = _when
                self.then = _then
                self.field = _field
                self._poison_pill = _poison_pill
            return __init__

        op_name = cls._derive_op_name(when)
        new_cls = type(class_name(op_name), (object,), {})
        setattr(new_cls, "__call__", method(cls.symbols[op_name]))
        setattr(new_cls, "__init__", init())
        obj = new_cls(when[op_name], then, when["field"], poison_pill)
        return obj


class _TypeThen(object):

        def __init__(self, _then):
            # type: (Dict) -> None
            self._then = _then
            if isinstance(_then, dict):
                from copy import deepcopy
                then = deepcopy(_then)
                then.pop("field", None)
                self.then = init_type(_then['type'], params=then)
            else:
                self.then = lambda _: _then

        def __call__(self, value):
            # type: (Any) -> Any
            if isinstance(self._then, dict) and "field" in self._then:
                value = value[self._then["field"]]
            return self.then(value)


class _TypeCase(object):

        def __init__(self, _when, _then):
            # type: (Dict, Any) -> None
            self.when = _when
            self.case = _TypeCaseProxy.create(_when, _TypeThen(_then), "##CASE_PIL")

        def __call__(self, value):
            # type: (Dict) -> Any
            if self.when["field"] not in value:
                return "##CASE_PIL"
            return self.case(value)


class TypeCase(object):

    """ deriving a field from multiple fields

    Example: deriving 'budget_level' from 'daily_budget' and 'lifetime_budget'

        cases:
            - when:
                field: daily_budget
                not_in: [0, null, "", "0"]
                then: daily
            - when:
                field: lifetime_budget
                not_in: [0, null, "", "0"]
                then: lifetime
    """

    def __init__(self, _cases, _default=None):
        # type: (List[Dict], Any) -> None
        self.cases = [_TypeCase(**args(case)) for case in _cases]
        self.default = _default

    def __call__(self, value):
        # type: (dict) -> Any
        for case in self.cases:
            output = case(value)
            if output != "##CASE_PIL":
                return output
        return self.default


class TypeIgnore(object):

    """ if value is "##$IGNORE_PIL", then do not ignore the field

    Example: ignoring a field based on a condition

        ignore:
            when:
                field: pricing_type
                not_equal: Bid Cap
            then: null
    """

    def __init__(self, _when, _then):
        # type: (Dict, Any) -> None
        self.field = _when["field"]
        self.case = _TypeCaseProxy.create(_when, _TypeThen(_then), "##$IGNORE_PIL")

    def __call__(self, value):
        # type: (Any) -> Any
        return self.case(value)


class TypeConstant(object):

    """ SAMPLE:
    ----------------
        constants:
            - name: budget_typeid
            value: 1
    """

    def __init__(self, _value, **kwargs):
        # type: (Any, Dict) -> None
        """
        Initialize a constant value, param kwargs: unused
        """
        self.value = _value

    def __call__(self):
        return self.value


# ------------------------------------------------------------------
#                       END: TYPE DEFINITIONS
# ------------------------------------------------------------------
