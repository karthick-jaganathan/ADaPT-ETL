"""
The account lookup and app-level secrets: which (user, account, network) rows exist, and the per-network API
credentials. Both come from an external provider - never from source.

Two providers, each read at run time (API first, a local file for development only, else empty):

  accounts   STREAMWRIGHT_ACCOUNTS_URL (HTTP GET) -> else STREAMWRIGHT_ACCOUNTS_FILE (default ~/.streamwright/accounts.yaml) -> else []
             A `clients` map (client -> name, region, accounts[]) or a flat `accounts` list. Each account carries its
             own `token` (the user's OAuth token) - so tokens live on the account, never in `secrets`.
  secrets    STREAMWRIGHT_SECRETS_URL  (HTTP GET) -> else STREAMWRIGHT_SECRETS_FILE  (default ~/.streamwright/secrets.yaml)  -> else {}
             A `secrets` map network -> {default, regions} of app-level API credentials (developer_token, client_id,
             client_secret, ...). NO tokens here. `app_config(network, region)` overlays `regions[region]` on `default`.

An account's `region` (from its client) selects which region's secrets a run uses: see context.make_context, which
maps the row's network + region onto `app_config`.

PROTOTYPE: files are the development/testing path. In production both providers are the accounts API (commondb):
STREAMWRIGHT_ACCOUNTS_URL / STREAMWRIGHT_SECRETS_URL return the same documents over HTTP.
"""

import json
import os
from pathlib import Path

import requests
import yaml

ACCOUNTS_FILE = "~/.streamwright/accounts.yaml"
SECRETS_FILE = "~/.streamwright/secrets.yaml"
HTTP_TIMEOUT = 15


class AccountError(LookupError):
    """No account row matches."""


def _load_document(url_env, file_env, default_file):
    """
    The raw document from a provider: the API ($<url_env>) when set, else a local file ($<file_env> or default_file,
    development only). A missing file gives None; the caller decides the empty shape.
    """
    url = os.environ.get(url_env)
    if url:
        response = requests.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return response.json()
    path = os.environ.get(file_env) or default_file
    path = Path(path).expanduser()
    if not path.is_file():
        return None
    with open(path) as handle:
        if path.suffix == ".json":
            return json.load(handle)
        return yaml.safe_load(handle)


def _rows_from_clients(clients):
    """Flatten a `clients` map (client-id -> {name, region, accounts[]}) into account rows."""
    rows = []
    for client_id, client in (clients or {}).items():
        if not isinstance(client, dict):
            raise ValueError("client %r must be a mapping" % client_id)
        region = client.get("region")
        name = client.get("name")
        for account in client.get("accounts") or []:
            account_id = account.get("account_id") or account.get("customer_id")
            row = {"user_id": str(client_id), "client_name": name, "region": region,
                   "account_id": None if account_id is None else str(account_id)}
            for key, value in account.items():
                if key != "account_id":
                    row[key] = value   # the account's own `token` (the user's OAuth token) rides on the row
            rows.append(row)
    return rows


def load_accounts():
    """
    The account rows, from the accounts provider. A `clients` map is flattened to rows (each account keeps its own `token`); a flat `accounts` list is used as-is. No provider configured gives [].
    """
    document = _load_document("STREAMWRIGHT_ACCOUNTS_URL", "STREAMWRIGHT_ACCOUNTS_FILE", ACCOUNTS_FILE)
    if not document:
        return []
    if isinstance(document, dict):
        if "clients" in document:
            return _rows_from_clients(document["clients"])
        document = document.get("accounts", [])
    if not isinstance(document, list):
        raise ValueError("accounts provider must yield a `clients` map or an `accounts` list")
    return [dict(row) for row in document]


def find_accounts(user_id, account_id=None, accounts=None):
    """The rows of a user (and of one of their accounts when account_id is given)."""
    rows = [dict(row) for row in (load_accounts() if accounts is None else accounts)
            if row["user_id"] == user_id and (account_id is None or str(row["account_id"]) == str(account_id))]
    if not rows:
        raise AccountError("no account for user %r%s" % (user_id, "" if account_id is None
                                                           else " and account %r" % account_id))
    return rows


def get_account(user_id, account_id, network, accounts=None):
    """The one row of (user, account, network)."""
    rows = [row for row in find_accounts(user_id, account_id, accounts) if row["network"] == network]
    if len(rows) != 1:
        raise AccountError("%d rows for user %r, account %r, network %r" % (len(rows), user_id, account_id, network))
    return rows[0]


def app_config(network, region=None):
    """
    A network's app-level credentials from the secrets provider, resolved for `region`: the network's `default` values
    with `regions[region]` overlaid on top. A network whose entry is a flat mapping (no default/regions) is used as is.
    An absent provider or network gives {}: the wrapper then fails on the first required input that references it,
    naming the input (never a value). NO tokens here - a run's token comes from the account row (row.token).
    """
    document = _load_document("STREAMWRIGHT_SECRETS_URL", "STREAMWRIGHT_SECRETS_FILE", SECRETS_FILE) or {}
    secrets = document.get("secrets", document) if isinstance(document, dict) else {}
    entry = secrets.get(network)
    if not isinstance(entry, dict):
        return {}
    if "default" in entry or "regions" in entry:
        values = dict(entry.get("default") or {})
        if region:
            values.update(entry.get("regions", {}).get(region) or {})
    else:
        values = dict(entry)
    return {str(key): value for key, value in values.items() if value is not None}
