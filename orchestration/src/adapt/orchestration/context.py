"""
A node's run context: who (user, account, network), what (the source, its connectors and input mapping), where (the
warehouse and schema), plus the account row, the app-level values and the upstream nodes' outputs - everything a
wrapper needs to build the node's command.
"""

import dataclasses
import os
import re
from pathlib import Path

from adapt.orchestration import accounts as accounts_module
from adapt.orchestration.settings import warehouse_dir
from adapt.orchestration.spec import load_networks


@dataclasses.dataclass
class Context:
    user_id: str
    account_id: str
    network: str
    source: Path
    timezone: str
    app: dict = dataclasses.field(repr=False)  # app-level values AND secrets: never repr'd, logged or stored
    row: dict = dataclasses.field(repr=False)  # the account row (in production it may hold the user's token)
    upstream: dict = dataclasses.field(default_factory=dict)  # upstream node -> its op output
    warehouse_path: Path = None
    schema: str = None
    connectors: tuple = ()
    inputs: dict = dataclasses.field(default_factory=dict)  # networks.yaml `inputs` (templates, no values)
    streams: dict = dataclasses.field(default_factory=dict)  # canonical node -> source stream name (alias), or False
    image: str = None  # networks.yaml `image` (the container image of ADAPT_EXECUTION=docker)

    def stream_of(self, node):
        """The source stream a node maps to on this network (default: the node name itself)."""
        resolved = self.streams.get(node, node)
        return node if resolved is False else resolved


def _identifier(text):
    return re.sub(r"[^a-z0-9_]", "_", str(text).lower())


def warehouse_for(user_id):
    """One DuckDB file per user."""
    return (warehouse_dir() / ("%s.duckdb" % _identifier(user_id))).resolve()


def schema_for(network, account_id):
    """One schema per (network, account): `<network>_<account>`, never the warehouse file's stem."""
    return "%s_%s" % (_identifier(network), _identifier(account_id))


def make_context(user_id, account_id, network, upstream=None, networks=None, accounts=None, app_loader=None):
    """
    The Context of one (user, account, network): the row from the account lookup, the network's entry from
    networks.yaml and the app-level values - loaded now, at run time, so secrets never travel in run config.
    """
    networks = load_networks() if networks is None else networks
    if network not in networks:
        raise KeyError("network %r is not in networks.yaml (known: %s)" % (network, ", ".join(sorted(networks))))
    entry = networks[network]
    row = accounts_module.get_account(user_id, account_id, network, accounts)
    region = row.get("region") or os.environ.get("ADAPT_REGION")
    app = (app_loader or accounts_module.app_config)(network, region)
    warehouse = warehouse_for(user_id)
    schema = schema_for(network, account_id)
    if schema == warehouse.stem:
        raise ValueError("schema %r has the name of the warehouse file %s" % (schema, warehouse))
    return Context(user_id=str(user_id), account_id=str(account_id), network=network, source=entry.source,
                   timezone=str(row.get("timezone") or entry.timezone), app=app, row=row,
                   upstream=dict(upstream or {}), warehouse_path=warehouse, schema=schema,
                   connectors=entry.connectors, inputs=dict(entry.inputs), streams=dict(entry.streams),
                   image=entry.image)
