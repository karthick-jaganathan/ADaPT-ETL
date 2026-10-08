import os
import shutil
import unittest
from unittest import mock

from tests.helpers import PROJECT_DIR
from streamwright.orchestration import REPO_ROOT, pipelines_dir
from streamwright.orchestration.spec import (SpecError, load_networks, load_pipeline,
                                      load_pipelines, parse_networks, parse_pipeline, pipeline_names, resolve_pipeline)
from streamwright.orchestration.sourcespec import load_source_spec

SCRATCH = PROJECT_DIR / "tests" / ".scratch"
METADATA_NODES = {"campaigns", "ad_groups", "ad_group_hierarchy", "location_targets", "keywords", "audience_targets"}


class PipelineSpecTest(unittest.TestCase):
    def test_pipeline_names(self):
        self.assertEqual(pipelines_dir(), PROJECT_DIR / "config" / "pipelines")
        self.assertEqual(pipeline_names(), ["metadata", "performance"])
        self.assertEqual(set(load_pipelines()), {"metadata", "performance"})

    def test_loads_the_metadata_dag(self):
        spec = load_pipeline("metadata")
        self.assertEqual(spec.name, "metadata")
        self.assertEqual(set(spec.nodes), METADATA_NODES)
        self.assertEqual(set(spec.edges), {("campaigns", "ad_groups"), ("campaigns", "ad_group_hierarchy"),
                                           ("campaigns", "location_targets"), ("ad_groups", "keywords"),
                                           ("ad_groups", "audience_targets")})
        self.assertEqual(spec.upstream("campaigns"), ())
        self.assertEqual(spec.upstream("ad_groups"), ("campaigns",))
        self.assertEqual(spec.upstream("keywords"), ("ad_groups",))
        self.assertEqual(set(spec.downstream("ad_groups")), {"keywords", "audience_targets"})
        order = spec.order
        for up, down in spec.edges:
            self.assertLess(order.index(up), order.index(down))
        self.assertEqual(order[0], "campaigns")

    def test_loads_the_performance_dag(self):
        spec = load_pipeline("performance")
        self.assertEqual(spec.name, "performance")
        self.assertEqual(spec.nodes, ("campaign_performance",))
        self.assertEqual(spec.edges, ())
        self.assertEqual(spec.order, ("campaign_performance",))

    def test_unknown_pipeline_raises(self):
        for name in ("nope", "../networks", None):
            with self.assertRaisesRegex(SpecError, "metadata, performance", msg=repr(name)):
                load_pipeline(name)

    def _folder(self, files):
        folder = SCRATCH / "pipelines"
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (folder / name).write_text(text)
        return folder

    def test_pipeline_dir_env(self):
        folder = self._folder({"daily.yaml": "name: daily\nnodes: {campaigns: {}, keywords: {after: [campaigns]}}\n",
                               "notes.txt": "ignored"})
        with mock.patch.dict(os.environ, {"STREAMWRIGHT_PIPELINE_DIR": str(folder)}):
            self.assertEqual(pipeline_names(), ["daily"])
            self.assertEqual(load_pipeline("daily").edges, (("campaigns", "keywords"),))
            self.assertEqual(load_pipeline().name, "daily")     # the only one
            with self.assertRaisesRegex(SpecError, "unknown pipeline 'metadata'"):
                load_pipeline("metadata")

    def test_file_name_must_match_name(self):
        folder = self._folder({"daily.yaml": "name: weekly\nnodes: {campaigns: {}}\n"})
        with mock.patch.dict(os.environ, {"STREAMWRIGHT_PIPELINE_DIR": str(folder)}):
            with self.assertRaisesRegex(SpecError, "does not match the file name"):
                load_pipeline("daily")

    def test_invalid_pipeline_file_raises(self):
        folder = self._folder({"loop.yaml": "name: loop\nnodes: {a: {after: [b]}, b: {after: [a]}}\n"})
        with mock.patch.dict(os.environ, {"STREAMWRIGHT_PIPELINE_DIR": str(folder)}):
            with self.assertRaisesRegex(SpecError, "loop.yaml: the nodes form a cycle"):
                load_pipeline("loop")

    def test_pipeline_spec_env_is_a_single_file_override(self):
        folder = self._folder({"one.yaml": "name: solo\nnodes: {campaigns: {}}\n"})
        with mock.patch.dict(os.environ, {"STREAMWRIGHT_PIPELINE_SPEC": str(folder / "one.yaml")}):
            self.assertEqual(pipeline_names(), ["solo"])
            self.assertEqual(load_pipeline().nodes, ("campaigns",))
            self.assertEqual(load_pipeline("solo").nodes, ("campaigns",))
            with self.assertRaisesRegex(SpecError, "unknown pipeline 'metadata'"):
                load_pipeline("metadata")

    def test_resolves_pipeline_on_a_network(self):
        google = load_networks()["google_ads"]
        for name in pipeline_names():
            effective = resolve_pipeline(load_pipeline(name), google)      # identity on google_ads
            self.assertEqual(set(effective.nodes), set(load_pipeline(name).nodes))
        bad = parse_pipeline({"name": "bad", "nodes": {"campaigns": {}, "ad_performance": {"after": ["campaigns"]}}})
        with self.assertRaisesRegex(SpecError, "pipeline 'bad': network 'google_ads'.*does not map .*ad_performance"):
            resolve_pipeline(bad, google)

    def test_resolves_aliases_and_skips_unsupported(self):
        networks = load_networks()
        micro = resolve_pipeline(load_pipeline("metadata"), networks["microsoft_ads"])
        self.assertEqual(set(micro.nodes), METADATA_NODES)                 # all supported (ad_group_hierarchy aliased)
        facebook = resolve_pipeline(load_pipeline("metadata"), networks["facebook_ads"])
        self.assertEqual(set(facebook.nodes), {"campaigns", "ad_groups"})  # the rest are mapped false -> skipped
        self.assertEqual(facebook.upstream("ad_groups"), ())              # facebook `after: {ad_groups: []}` override
        self.assertEqual(networks["facebook_ads"].stream_of("ad_groups"), "ad_sets")
        self.assertEqual(networks["microsoft_ads"].stream_of("ad_group_hierarchy"), "ad_group_tree")

    def test_cycle_raises(self):
        with self.assertRaisesRegex(SpecError, "cycle"):
            parse_pipeline({"name": "x", "nodes": {"a": {"after": ["c"]}, "b": {"after": ["a"]},
                                                   "c": {"after": ["b"]}, "d": {}}})

    def test_unknown_node_raises(self):
        with self.assertRaisesRegex(SpecError, "unknown node 'nope'"):
            parse_pipeline({"name": "x", "nodes": {"a": {}, "b": {"after": ["nope"]}}})

    def test_self_dependency_raises(self):
        with self.assertRaisesRegex(SpecError, "depends on itself"):
            parse_pipeline({"name": "x", "nodes": {"a": {"after": ["a"]}}})

    def test_bad_shapes_raise(self):
        for data in (None, {"name": "x"}, {"name": "x", "nodes": {}}, {"name": "x", "nodes": {"a": {"before": []}}},
                     {"name": "x", "nodes": {"A-b": {}}}, {"name": "x", "nodes": {"a": {}}, "extra": 1}):
            with self.assertRaises(SpecError, msg=repr(data)):
                parse_pipeline(data)


class NetworksTest(unittest.TestCase):
    def test_loads_google_ads(self):
        networks = load_networks()
        google = networks["google_ads"]
        self.assertEqual(google.source, (REPO_ROOT / "examples/sources/ads/google_ads").resolve())
        self.assertEqual(google.timezone, "America/New_York")
        self.assertEqual(google.connectors, ("google_ads", "gaql"))
        self.assertEqual(google.inputs["customer_ids"], "{{ row.account_id }}")
        self.assertEqual(google.stream_of("ad_group_hierarchy"), "ad_group_hierarchy")
        self.assertTrue(google.supports("keywords"))

    def test_streams_map_is_required_and_validated(self):
        with self.assertRaisesRegex(SpecError, "`streams`"):
            parse_networks({"networks": {"n": {"source": "examples/sources/ads/google_ads"}}})
        with self.assertRaisesRegex(SpecError, "is not a stream of source"):
            parse_networks({"networks": {"n": {"source": "examples/sources/ads/google_ads",
                                               "streams": {"campaigns": "nope"}}}})
        with self.assertRaisesRegex(SpecError, "`after` node 'x' is not in `streams`"):
            parse_networks({"networks": {"n": {"source": "examples/sources/ads/google_ads",
                                               "streams": {"campaigns": "campaigns"}, "after": {"x": []}}}})

    def test_missing_source_raises(self):
        with self.assertRaisesRegex(SpecError, "no source.yaml"):
            parse_networks({"networks": {"n": {"source": "does/not/exist"}}})
        with self.assertRaisesRegex(SpecError, "unknown keys"):
            parse_networks({"networks": {"n": {"source": "examples/sources/ads/google_ads", "bogus": 1}}})


class SourceSpecTest(unittest.TestCase):
    def test_google_ads_declared_inputs(self):
        spec = load_source_spec(REPO_ROOT / "examples/sources/ads/google_ads")
        self.assertEqual(set(spec.config_keys),
                         {"customer_ids", "login_customer_id", "start_date", "campaign_ids", "channel_types"})
        self.assertEqual(set(spec.secret_keys), {"developer_token", "client_id", "client_secret", "refresh_token"})
        self.assertTrue(spec.config["customer_ids"].required)
        self.assertFalse(spec.config["login_customer_id"].required)
        self.assertFalse(spec.config["start_date"].required)       # has a default
        self.assertEqual(spec.config["start_date"].default, "-30d")
        self.assertTrue(all(p.required for p in spec.secrets.values()))
        self.assertIn("campaigns", spec.streams)
        self.assertIn("keywords", spec.streams)


if __name__ == "__main__":
    unittest.main()
