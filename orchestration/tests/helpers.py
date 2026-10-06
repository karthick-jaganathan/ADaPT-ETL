"""Shared test fixtures: fake (non-real) secrets and a Context for the google_ads network."""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR / "src"))
os.environ.setdefault("ADAPT_BIN", "/tmp/adapt-sdk-venv/bin/adapt")

from adapt.orchestration.context import make_context  # noqa: E402

FAKE_APP = {"developer_token": "DEV-TOKEN-S3CRET", "client_id": "CLIENT-ID-S3CRET",
            "client_secret": "CLIENT-SECRET-S3CRET", "refresh_token": "REFRESH-TOKEN-S3CRET"}
GOOGLE_SECRET_ENV = {"ADAPT_SECRET_DEVELOPER_TOKEN", "ADAPT_SECRET_CLIENT_ID", "ADAPT_SECRET_CLIENT_SECRET",
                     "ADAPT_SECRET_REFRESH_TOKEN"}
ACCOUNTS = [
    {"user_id": "u1", "account_id": "1000000001", "network": "google_ads", "login_customer_id": "2000000002"},
    {"user_id": "u2", "account_id": "1112223333", "network": "google_ads"},
]


def google_context(user_id="u1", account_id="1000000001", app=None, upstream=None):
    return make_context(user_id, account_id, "google_ads", upstream=upstream, accounts=ACCOUNTS,
                        app_loader=lambda network: dict(FAKE_APP if app is None else app))
