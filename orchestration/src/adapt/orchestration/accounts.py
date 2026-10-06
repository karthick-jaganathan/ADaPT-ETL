"""
The account lookup: which (user, account, network) rows exist, and the app-level config/secrets of a network.

PROTOTYPE: ACCOUNTS is a Python list. In production this is a query against the accounts database (commondb): the same
row shape (user_id, account_id, network, plus per-account columns such as login_customer_id), with the user's own
OAuth refresh token on the row (and networks.yaml mapping `refresh_token: "secret:{{ row.refresh_token }}"`). For this
prototype the refresh token is app-level: it comes from the secrets file through `app.refresh_token`.
"""

import os
from pathlib import Path

import yaml

ACCOUNTS = [
    {"user_id": "u1", "account_id": "1000000001", "network": "google_ads",
     "login_customer_id": "2000000002", "timezone": "America/Los_Angeles"},  # per-user: overrides the network default
    {"user_id": "u2", "account_id": "1000000003", "network": "microsoft_ads",
     "customer_id": "2000000004", "timezone": "America/New_York"},
    {"user_id": "u3", "account_id": "1000000005", "network": "facebook_ads",
     "timezone": "America/New_York"},
]

# network -> the file holding its app-level values/secrets ($ADAPT_APP_CONFIG_<NETWORK> overrides the path).
APP_CONFIG_FILES = {
    "google_ads": "~/.adapt/google-secrets.yaml",       # developer_token, client_id, client_secret, refresh_token
    "microsoft_ads": "~/.adapt/microsoft-secrets.yaml",  # developer_token, client_id, client_secret, refresh_token
    "facebook_ads": "~/.adapt/facebook-secrets.yaml",    # access_token, app_id, app_secret
}


class AccountError(LookupError):
    """No account row matches."""


def find_accounts(user_id, account_id=None, accounts=None):
    """The rows of a user (and of one of their accounts when account_id is given)."""
    rows = [dict(row) for row in (ACCOUNTS if accounts is None else accounts)
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


def app_config(network):
    """
    A network's app-level values and secrets, read at run time (never stored in run config). An absent file gives {}:
    the wrapper then fails on the first required input that references it, naming the input (never a value).
    """
    path = os.environ.get("ADAPT_APP_CONFIG_%s" % network.upper()) or APP_CONFIG_FILES.get(network)
    if not path:
        return {}
    path = Path(path).expanduser()
    if not path.is_file():
        return {}
    with open(path) as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("%s must be a mapping of name: value" % path)
    return {str(key): value for key, value in data.items() if value is not None}
