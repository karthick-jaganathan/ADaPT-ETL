import dataclasses
import unittest

from tests.helpers import FAKE_APP, GOOGLE_SECRET_ENV, google_context
from adapt.orchestration import custom_wrappers
from adapt.orchestration.wrappers import WrapperError, default_wrapper, node, render, unregister, wrapper_for


def sets(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--set"]


def assert_no_secret_values(test, argv):
    joined = " ".join(argv)
    for value in FAKE_APP.values():
        test.assertNotIn(value, joined)


class DefaultWrapperTest(unittest.TestCase):
    def test_ad_groups_command(self):
        ctx = google_context()
        argv, secret_env = default_wrapper("ad_groups", ctx)
        self.assertEqual(argv[0], "/tmp/adapt-sdk-venv/bin/adapt")
        self.assertEqual(argv[1:3], ["run", str(ctx.source)])
        self.assertIn(["--stream", "ad_groups"], [argv[i:i + 2] for i in range(len(argv))])
        self.assertIn("customer_ids=1000000001", sets(argv))
        self.assertIn("login_customer_id=2000000002", sets(argv))
        self.assertIn("start_date=-7d", sets(argv))
        self.assertNotIn("campaign_ids", " ".join(sets(argv)))
        self.assertEqual(argv[argv.index("--timezone") + 1], "America/New_York")
        output = argv[argv.index("--output") + 1]
        self.assertTrue(output.startswith("duckdb:"), output)
        self.assertTrue(output.endswith(".duckdb:google_ads_1000000001"), output)
        connectors = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--allow-connector"]
        self.assertEqual(connectors, ["google_ads", "gaql"])
        self.assertEqual(set(secret_env), GOOGLE_SECRET_ENV)
        self.assertEqual(secret_env["ADAPT_SECRET_DEVELOPER_TOKEN"], FAKE_APP["developer_token"])
        self.assertEqual(secret_env["ADAPT_SECRET_REFRESH_TOKEN"], FAKE_APP["refresh_token"])
        assert_no_secret_values(self, argv)
        self.assertNotIn("--secrets", argv)

    def test_optional_reference_unset_is_skipped(self):
        ctx = google_context(user_id="u2", account_id="1112223333")   # its row has no login_customer_id
        argv, _ = default_wrapper("keywords", ctx)
        self.assertNotIn("login_customer_id", " ".join(sets(argv)))
        self.assertIn("customer_ids=1112223333", sets(argv))

    def test_required_input_missing_raises(self):
        ctx = google_context()
        ctx.inputs = {k: v for k, v in ctx.inputs.items() if k != "customer_ids"}
        with self.assertRaisesRegex(WrapperError, "requires config 'customer_ids'"):
            default_wrapper("ad_groups", ctx)

    def test_required_secret_missing_raises_without_values(self):
        app = dict(FAKE_APP)
        del app["client_secret"]
        with self.assertRaisesRegex(WrapperError, "requires secret 'client_secret'") as caught:
            default_wrapper("ad_groups", google_context(app=app))
        for value in FAKE_APP.values():
            self.assertNotIn(value, str(caught.exception))

    def test_unknown_input_raises(self):
        ctx = google_context()
        ctx.inputs = dict(ctx.inputs, not_a_param="x")
        with self.assertRaisesRegex(WrapperError, "not declared"):
            default_wrapper("ad_groups", ctx)

    def test_config_marked_secret_raises(self):
        ctx = google_context()
        ctx.inputs = dict(ctx.inputs, start_date="secret:{{ app.developer_token }}")
        with self.assertRaisesRegex(WrapperError, "command line"):
            default_wrapper("ad_groups", ctx)

    def test_unknown_stream_raises(self):
        with self.assertRaisesRegex(WrapperError, "not a stream"):
            default_wrapper("no_such_stream", google_context())

    def test_adapt_bin_env(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"ADAPT_BIN": "/opt/adapt"}):
            argv, _ = default_wrapper("keywords", google_context())
        self.assertEqual(argv[0], "/opt/adapt")

    def test_render(self):
        ctx = google_context()
        self.assertEqual(render("{{ row.account_id }}", ctx), "1000000001")
        self.assertEqual(render("acct-{{ ctx.account_id }}-{{ ctx.network }}", ctx), "acct-1000000001-google_ads")
        self.assertIsNone(render("{{ row.missing }}", ctx))
        self.assertEqual(render([1, 2, 3], ctx), "1,2,3")
        with self.assertRaises(WrapperError):
            render("{{ nope.x }}", ctx)


class CustomWrapperTest(unittest.TestCase):
    def test_campaigns_uses_the_registered_custom_wrapper(self):
        self.assertIs(wrapper_for("campaigns", "google_ads"), custom_wrappers.campaigns_from_db)
        self.assertIs(wrapper_for("campaigns", "other_network"), default_wrapper)
        self.assertIs(wrapper_for("ad_groups", "google_ads"), default_wrapper)

    def test_account_in_campaign_db_gets_campaign_ids(self):
        self.assertEqual(custom_wrappers.CAMPAIGN_DB["1112223333"], [1, 2, 3])
        ctx = google_context(user_id="u2", account_id="1112223333")
        argv, secret_env = wrapper_for("campaigns", "google_ads")("campaigns", ctx)
        self.assertIn("campaign_ids=1,2,3", sets(argv))
        self.assertIn("customer_ids=1112223333", sets(argv))
        self.assertEqual(argv[argv.index("--stream") + 1], "campaigns")
        self.assertEqual(set(secret_env), GOOGLE_SECRET_ENV)
        assert_no_secret_values(self, argv)

    def test_account_not_in_campaign_db_uses_the_default(self):
        self.assertNotIn("1000000001", custom_wrappers.CAMPAIGN_DB)
        ctx = google_context()
        custom = wrapper_for("campaigns", "google_ads")("campaigns", ctx)
        self.assertEqual(custom, default_wrapper("campaigns", ctx))
        self.assertNotIn("campaign_ids", " ".join(custom[0]))

    def test_registry_any_network_and_override(self):
        @node("keywords")
        def keywords_any(node_name, ctx):
            return default_wrapper(node_name, ctx, overrides={"start_date": ctx.upstream["ad_groups"]["since"]})
        try:
            self.assertIs(wrapper_for("keywords", "google_ads"), keywords_any)
            ctx = dataclasses.replace(google_context(), upstream={"ad_groups": {"since": "2026-09-01"}})
            argv, _ = wrapper_for("keywords", "google_ads")("keywords", ctx)
            self.assertIn("start_date=2026-09-01", sets(argv))
            with self.assertRaises(WrapperError):
                node("keywords")(lambda n, c: None)
        finally:
            unregister("keywords")
        self.assertIs(wrapper_for("keywords", "google_ads"), default_wrapper)


if __name__ == "__main__":
    unittest.main()
