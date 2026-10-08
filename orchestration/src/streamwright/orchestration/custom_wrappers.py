"""
Custom node wrappers: per-node logic that the default wrapper cannot express. The framework does not know about these
nodes - registering a wrapper with @node is all it takes; every other node uses the default wrapper.

campaigns (google_ads): which campaigns to pull for an account comes from a campaigns database, then the command is
the default one plus `--set campaign_ids=...` (the google_ads source's `campaigns` stream filters on campaign_ids).
"""

from streamwright.orchestration.wrappers import default_wrapper, node

# STUB of the campaigns database: account id -> the campaign ids to pull. In production this is a query (commondb).
# An account that is not here pulls every campaign (no filter).
CAMPAIGN_DB = {
    "1112223333": [1, 2, 3],
}


def campaign_ids_for(account_id):
    """The campaign ids to pull for an account ([] when the database has none: no filter)."""
    return list(CAMPAIGN_DB.get(str(account_id), []))


@node("campaigns", network="google_ads")
def campaigns_from_db(node_name, ctx):
    ids = campaign_ids_for(ctx.account_id)
    if not ids:
        return default_wrapper(node_name, ctx)
    return default_wrapper(node_name, ctx, overrides={"campaign_ids": ids})
