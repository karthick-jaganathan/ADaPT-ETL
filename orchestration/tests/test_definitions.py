import json
import logging
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ACCOUNTS, FAKE_APP, PROJECT_DIR
from streamwright.orchestration import accounts, definitions
from streamwright.orchestration.accounts import AccountError
from streamwright.orchestration.runner import RunFailed, run
from streamwright.orchestration.spec import SpecError, parse_pipeline
from dagster import Definitions

FAKE_STREAMWRIGHT = str(Path(__file__).resolve().parent / "fake_streamwright.py")
SCRATCH = PROJECT_DIR / "tests" / ".scratch"


class ListLog:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda message: self.lines.append((level, message))


def job_deps(job_def):
    return {invocation.alias or invocation.name: {name: d.node for name, d in inputs.items()}
            for invocation, inputs in job_def.graph.dependencies.items()}


class OpGraphTest(unittest.TestCase):
    def test_metadata_dependency_edges(self):
        deps = job_deps(definitions.JOBS[("metadata", "google_ads")])
        self.assertEqual(deps["campaigns"], {})
        self.assertEqual(deps["ad_groups"], {"campaigns": "campaigns"})
        self.assertEqual(deps["keywords"], {"ad_groups": "ad_groups"})
        self.assertEqual(deps["audience_targets"], {"ad_groups": "ad_groups"})
        for name in ("ad_group_hierarchy", "location_targets"):
            self.assertEqual(deps[name], {"campaigns": "campaigns"})
        self.assertEqual(set(deps), set(definitions.PIPELINES["metadata"].nodes))
        self.assertEqual({node.name for node in definitions.JOBS[("metadata", "google_ads")].graph.nodes},
                         set(definitions.PIPELINES["metadata"].nodes))     # node names are the step keys

    def test_per_network_jobs_alias_and_skip(self):
        facebook = definitions.JOBS[("metadata", "facebook_ads")]
        self.assertEqual(facebook.name, "ads_metadata__facebook_ads")
        self.assertEqual(set(job_deps(facebook)), {"campaigns", "ad_groups"})   # the rest skipped (streams: false)
        self.assertEqual(job_deps(facebook)["ad_groups"], {})                   # facebook `after: {ad_groups: []}`
        micro = definitions.JOBS[("metadata", "microsoft_ads")]
        self.assertEqual(set(job_deps(micro)), set(definitions.PIPELINES["metadata"].nodes))  # all supported (aliased)

    def test_performance_graph(self):
        self.assertEqual(job_deps(definitions.JOBS[("performance", "google_ads")]), {"campaign_performance": {}})

    def test_definitions_hold_one_job_per_pipeline_and_network(self):
        self.assertEqual(set(definitions.PIPELINES), {"metadata", "performance"})
        for (pipeline, network), job_def in definitions.JOBS.items():
            self.assertIs(definitions.defs.get_job_def("ads_%s__%s" % (pipeline, network)), job_def)
            self.assertIs(definitions.job_for(pipeline, network), job_def)
            self.assertEqual(job_def.tags["pipeline"], pipeline)
            self.assertEqual(job_def.tags["network"], network)

    def test_a_node_may_be_in_several_pipelines(self):
        google = definitions.NETWORKS["google_ads"]
        daily = parse_pipeline({"name": "daily", "nodes": {"campaigns": {}, "keywords": {"after": ["campaigns"]}}})
        jobs = [definitions.JOBS[("metadata", "google_ads")], definitions.build_job(daily, google)]
        names = [job_def.name for job_def in Definitions(jobs=jobs).get_repository_def().get_all_jobs()]
        self.assertEqual(sorted(names), ["ads_daily__google_ads", "ads_metadata__google_ads"])
        self.assertEqual(job_deps(jobs[1]), {"campaigns": {}, "keywords": {"campaigns": "campaigns"}})
        self.assertEqual({node.name for node in jobs[1].graph.node_defs},
                         {"daily__google_ads__campaigns", "daily__google_ads__keywords"})

    def test_job_for_unknown_pipeline_raises(self):
        with self.assertRaisesRegex(SpecError, "unknown pipeline 'nope'"):
            definitions.job_for("nope", "google_ads")


class TriggerTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(accounts, "load_accounts", lambda: [dict(row) for row in ACCOUNTS])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _configs(self, *args):
        return [definitions.run_config_for(row) for row, _effective, _job in definitions.plan(*args)]

    def test_plan_for_u1(self):
        configs = self._configs("metadata", "u1")
        self.assertEqual(configs, [{"user_id": "u1", "account_id": "1000000001", "network": "google_ads"}])
        self.assertEqual(self._configs("metadata", "u1", "1000000001"), configs)
        self.assertEqual(self._configs("performance", "u1"), configs)

    def test_unknown_user_or_account(self):
        with self.assertRaises(AccountError):
            definitions.plan("metadata", "nobody")
        with self.assertRaises(AccountError):
            definitions.plan("metadata", "u1", "0000000000")

    def test_unknown_pipeline_raises(self):
        with self.assertRaisesRegex(SpecError, "unknown pipeline 'nope'"):
            definitions.plan("nope", "u1")
        with self.assertRaisesRegex(SpecError, "unknown pipeline 'u1'"):
            definitions.trigger("u1", "1000000001")      # the pre-pipeline call shape fails clearly
        with self.assertRaisesRegex(SpecError, "pipeline must be a pipeline name"):
            definitions.trigger(None, "u1")

    def test_node_not_mapped_by_the_network_raises(self):
        folder = SCRATCH / "pipelines"
        folder.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        (folder / "bad.yaml").write_text("name: bad\nnodes: {campaigns: {}, ad_performance: {after: [campaigns]}}\n")
        with mock.patch.dict(os.environ, {"STREAMWRIGHT_PIPELINE_DIR": str(folder)}):
            with self.assertRaisesRegex(SpecError, "does not map .*ad_performance"):
                definitions.plan("bad", "u1")
            with self.assertRaisesRegex(SpecError, "does not map .*ad_performance"):
                definitions.trigger("bad", "u1")

    def test_run_config_holds_no_secrets(self):
        row = dict(ACCOUNTS[0], refresh_token=FAKE_APP["refresh_token"])   # a production-like row with a token
        config = definitions.run_config_for(row)
        self.assertEqual(set(config), {"user_id", "account_id", "network"})
        text = json.dumps(self._configs("metadata", "u1"))
        for value in FAKE_APP.values():
            self.assertNotIn(value, json.dumps(config))
            self.assertNotIn(value, text)

    def _trigger(self, pipeline):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        wrapper_script = SCRATCH / "streamwright"
        wrapper_script.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, FAKE_STREAMWRIGHT))
        wrapper_script.chmod(0o755)
        env = {"STREAMWRIGHT_BIN": str(wrapper_script), "STREAMWRIGHT_PIPELINE_WAREHOUSE_DIR": str(SCRATCH / "warehouse"),
               "STREAMWRIGHT_PIPELINE_RUNS_DIR": str(SCRATCH / "runs"), "STREAMWRIGHT_EXECUTION": "subprocess"}
        with mock.patch.dict(os.environ, env), mock.patch.object(accounts, "app_config", lambda n, region=None: dict(FAKE_APP)), \
                mock.patch.object(accounts, "load_accounts", lambda: [dict(row) for row in ACCOUNTS]):
            return definitions.trigger(pipeline, "u1")

    def test_trigger_selects_the_pipelines_job(self):
        calls = []
        metadata_job = definitions.JOBS[("metadata", "google_ads")]
        performance_job = definitions.JOBS[("performance", "google_ads")]
        real = metadata_job.execute_in_process

        def spy(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)
        with mock.patch.object(metadata_job, "execute_in_process", side_effect=spy) as metadata, \
                mock.patch.object(performance_job, "execute_in_process") as performance:
            results = self._trigger("metadata")
        self.assertEqual(metadata.call_count, 1)
        performance.assert_not_called()
        self.assertEqual(calls[0]["tags"]["pipeline"], "metadata")
        self.assertEqual(calls[0]["run_config"], {"user_id": "u1", "account_id": "1000000001", "network": "google_ads"})
        self.assertEqual((results[0]["pipeline"], results[0]["job"]), ("metadata", "ads_metadata__google_ads"))

    def test_trigger_performance(self):
        results = self._trigger("performance")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertEqual((results[0]["pipeline"], results[0]["job"]), ("performance", "ads_performance__google_ads"))
        self.assertEqual(set(results[0]["nodes"]), {"campaign_performance"})
        self.assertEqual(results[0]["nodes"]["campaign_performance"]["records"], {"campaign_performance": 5})

    def test_trigger_executes_the_graph_in_order(self):
        """The whole metadata op graph against a stand-in streamwright: dependency order, upstream flow, secrets only in env."""
        results = self._trigger("metadata")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result["success"])
        nodes = result["nodes"]
        spec = definitions.PIPELINES["metadata"]
        self.assertEqual(set(nodes), set(spec.nodes))
        for up, down in spec.edges:
            self.assertLessEqual(nodes[up]["started_at"], nodes[down]["started_at"])
        self.assertEqual(nodes["campaigns"]["wrapper"], "campaigns_from_db")
        self.assertEqual(nodes["keywords"]["wrapper"], "default_wrapper")
        self.assertEqual(nodes["keywords"]["records"], {"keywords": 5})
        dumped = json.dumps(results, default=str)
        for value in FAKE_APP.values():
            self.assertNotIn(value, dumped)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)

    def test_secrets_go_through_env_and_are_redacted(self):
        log = ListLog()
        result = run([sys.executable, FAKE_STREAMWRIGHT, "--stream", "campaigns"],
                     {"STREAMWRIGHT_SECRET_DEVELOPER_TOKEN": FAKE_APP["developer_token"]}, log, SCRATCH / "s.json")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["records"], {"campaigns": 5})
        self.assertIn(("info", "[2026-10-05 00:00:00,000] INFO streamwright.source: got token ***"), log.lines)
        self.assertEqual(log.lines[1][0], "warning")
        self.assertNotIn(FAKE_APP["developer_token"], repr(log.lines))
        self.assertNotIn("STREAMWRIGHT_SECRET_DEVELOPER_TOKEN", os.environ)

    def test_non_zero_exit_raises(self):
        with mock.patch.dict(os.environ, {"FAKE_STREAMWRIGHT_EXIT": "1"}):
            with self.assertRaises(RunFailed) as caught:
                run([sys.executable, FAKE_STREAMWRIGHT], {"STREAMWRIGHT_SECRET_DEVELOPER_TOKEN": FAKE_APP["developer_token"]},
                    logging.getLogger("test"), SCRATCH / "f.json")
        self.assertIn("exited with 1", str(caught.exception))
        self.assertNotIn(FAKE_APP["developer_token"], str(caught.exception))
        self.assertEqual(caught.exception.result["status"], "failed")

    def test_only_streamwright_secret_env(self):
        with self.assertRaises(ValueError):
            run([sys.executable, FAKE_STREAMWRIGHT], {"PATH": "/x"}, logging.getLogger("test"), SCRATCH / "p.json")


if __name__ == "__main__":
    unittest.main()
