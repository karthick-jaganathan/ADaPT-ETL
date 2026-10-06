"""ADAPT_EXECUTION=k8s: the Job manifest built from a node's logical argv (no cluster), and runs through a stand-in kubectl."""

import json
import os
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FAKE_APP, GOOGLE_SECRET_ENV, PROJECT_DIR, google_context
from adapt.orchestration import REPO_ROOT, accounts, definitions, execution_mode, k8s_settings
from adapt.orchestration.runner import (K8sJobError, RunFailed, counts_from_log, k8s_command,
                                        k8s_job_manifest, k8s_job_name, run)
from adapt.orchestration.wrappers import wrapper_for

FAKE_KUBECTL = str(Path(__file__).resolve().parent / "fake_kubectl.py")
SCRATCH = PROJECT_DIR / "tests" / ".scratch_k8s"
IMAGE = "adapt-pipeline:local"
CATALOG = "postgres:dbname=adaptcat host=catalog-postgres.adapt.svc.cluster.local port=5432 user=adapt"
DUCKLAKE_OUTPUT = "ducklake:%s:google_ads_1000000001" % CATALOG
PLAIN_ENV = {"ADAPT_DUCKLAKE_DATA_PATH": "s3://adapt-warehouse/u1/",
             "ADAPT_DUCKLAKE_CATALOG_SCHEMA": "lake_u1",
             "ADAPT_DUCKLAKE_S3_ENDPOINT": "localstack.adapt.svc.cluster.local:4566",
             "ADAPT_DUCKLAKE_S3_URL_STYLE": "path",
             "ADAPT_DUCKLAKE_S3_USE_SSL": "false",
             "ADAPT_DUCKLAKE_S3_REGION": "us-east-1"}
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class ListLog:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda message: self.lines.append((level, message))


def pairs(argv, flag):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


def build(node="campaigns", ctx=None, environ=None, image=IMAGE, summary=True):
    """(manifest, pod command, secret_env) of a google_ads node, as definitions.py would build it in k8s mode."""
    ctx = ctx or google_context()
    settings = k8s_settings({} if environ is None else environ)
    argv, secret_env = wrapper_for(node, ctx.network)(node, ctx)
    if summary:
        argv = argv + ["--summary", str(PROJECT_DIR / "runs" / "run-1" / ("%s.summary.json" % node))]
    command = k8s_command(argv, settings["catalog"], REPO_ROOT)
    name = k8s_job_name("metadata", ctx.user_id, ctx.account_id, node, "run-1")
    manifest = k8s_job_manifest(command, secret_env, image, name, ctx.user_id, settings,
                                labels={"pipeline": "metadata", "user": ctx.user_id, "node": node})
    return manifest, command, secret_env


def container_of(manifest):
    return manifest["spec"]["template"]["spec"]["containers"][0]


class K8sManifestTest(unittest.TestCase):
    def test_campaigns_job(self):
        manifest, command, _ = build("campaigns")
        self.assertEqual((manifest["apiVersion"], manifest["kind"]), ("batch/v1", "Job"))
        self.assertEqual(manifest["metadata"]["name"], "adapt-metadata-u1-1000000001-campaigns-run-1")
        self.assertEqual(manifest["metadata"]["namespace"], "adapt")
        self.assertEqual(manifest["spec"]["backoffLimit"], 0)
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], 1800)
        self.assertGreater(manifest["spec"]["ttlSecondsAfterFinished"], 0)
        pod = manifest["spec"]["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(len(pod["containers"]), 1)
        container = container_of(manifest)
        self.assertEqual(container["image"], IMAGE)
        self.assertEqual(container["imagePullPolicy"], "Never")
        self.assertEqual(container["command"], command)
        self.assertEqual(command[:3], ["adapt", "run", "/app/examples/sources/ads/google_ads"])
        self.assertEqual(pairs(command, "--stream"), ["campaigns"])
        self.assertEqual(pairs(command, "--output"), [DUCKLAKE_OUTPUT])
        self.assertEqual(pairs(command, "--timezone"), ["America/New_York"])
        self.assertEqual(pairs(command, "--allow-connector"), ["google_ads", "gaql"])
        self.assertIn("customer_ids=1000000001", pairs(command, "--set"))
        self.assertIn("login_customer_id=2000000002", pairs(command, "--set"))
        self.assertNotIn("--summary", command)
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "adapt-secrets"}}])
        self.assertEqual({entry["name"]: entry["value"] for entry in container["env"]}, PLAIN_ENV)
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        labels = manifest["metadata"]["labels"]
        self.assertEqual(labels["adapt-pipeline/node"], "campaigns")
        self.assertEqual(labels["app.kubernetes.io/name"], "adapt-pipeline")
        self.assertEqual(manifest["spec"]["template"]["metadata"]["labels"], labels)

    def test_no_secret_value_in_any_node_manifest(self):
        for node in definitions.PIPELINES["metadata"].nodes:
            manifest, command, secret_env = build(node)
            self.assertEqual(set(secret_env), GOOGLE_SECRET_ENV)
            dumped = json.dumps(manifest)
            for value in FAKE_APP.values():
                self.assertNotIn(value, dumped)
            self.assertEqual(pairs(command, "--stream"), [node])
            names = [entry["name"] for entry in container_of(manifest)["env"]]
            self.assertFalse([name for name in names if name.startswith("ADAPT_SECRET_")
                              or re.search(r"KEY_ID|_SECRET$|PASSWORD|TOKEN", name)], names)
            self.assertTrue(all("value" in entry and "valueFrom" not in entry for entry in container_of(manifest)["env"]))
            self.assertFalse([arg for arg in command if str(REPO_ROOT) in arg], command)

    def test_settings_come_from_the_environment(self):
        environ = {"ADAPT_K8S_NAMESPACE": "etl", "ADAPT_K8S_SECRET": "etl-secrets", "ADAPT_K8S_DATA_ROOT": "s3://b/p/",
                   "ADAPT_K8S_CATALOG": "postgres:dbname=c host=h user=u", "ADAPT_K8S_S3_ENDPOINT": "s3.local:9000",
                   "ADAPT_K8S_TIMEOUT": "60", "ADAPT_K8S_KEEP_JOBS": "1"}
        self.assertTrue(k8s_settings(environ)["keep_jobs"])
        self.assertFalse(k8s_settings({})["keep_jobs"])
        manifest, command, _ = build("keywords", environ=environ)
        self.assertEqual(manifest["metadata"]["namespace"], "etl")
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], 60)
        container = container_of(manifest)
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "etl-secrets"}}])
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        self.assertEqual(env["ADAPT_DUCKLAKE_DATA_PATH"], "s3://b/p/u1/")
        self.assertEqual(env["ADAPT_DUCKLAKE_S3_ENDPOINT"], "s3.local:9000")
        self.assertEqual(pairs(command, "--output"), ["ducklake:postgres:dbname=c host=h user=u:google_ads_1000000001"])

    def test_output_translation(self):
        logical = ["adapt", "run", str(REPO_ROOT / "examples/sources/readers/files_demo"),
                   "--set", "root=%s/examples/sources/readers/files_demo/data" % REPO_ROOT]
        command = k8s_command(logical + ["--output", "duckdb:/w/u9.duckdb"], CATALOG)
        self.assertEqual(pairs(command, "--output"), ["ducklake:%s" % CATALOG])
        self.assertEqual(pairs(command, "--set"), ["root=/app/examples/sources/readers/files_demo/data"])
        self.assertEqual(pairs(k8s_command(logical, CATALOG), "--output"), ["ducklake:%s" % CATALOG])
        kept = "ducklake:postgres:dbname=x:s"
        self.assertEqual(pairs(k8s_command(logical + ["--output", kept], CATALOG), "--output"), [kept])

    def test_refusals(self):
        with self.assertRaises(K8sJobError):   # no image
            build("campaigns", image=None)
        source = str(REPO_ROOT / "examples/sources/ads/google_ads")
        with self.assertRaises(K8sJobError):   # an output that would stay in the pod
            k8s_command(["adapt", "run", source, "--output", "jsonl:/x"], CATALOG)
        with self.assertRaises(K8sJobError):   # a password in the catalog DSN
            k8s_command(["adapt", "run", source], CATALOG + " password=x")
        with self.assertRaises(K8sJobError):   # a source outside the repository (not in the image)
            k8s_command(["adapt", "run", "/somewhere/else"], CATALOG)
        with self.assertRaises(K8sJobError):
            k8s_command(["adapt", "validate", source], CATALOG)
        ctx = google_context()
        argv, secret_env = wrapper_for("campaigns", "google_ads")("campaigns", ctx)
        command = k8s_command(argv + ["--set", "x=%s" % FAKE_APP["client_id"]], CATALOG)
        with self.assertRaises(K8sJobError):   # a secret value would be in the manifest
            k8s_job_manifest(command, secret_env, IMAGE, "j", "u1", k8s_settings({}))
        with self.assertRaises(ValueError):
            k8s_job_manifest(command, {"PATH": "/x"}, IMAGE, "j", "u1", k8s_settings({}))

    def test_job_names(self):
        self.assertEqual(k8s_job_name("metadata", "U_1", "1000000001", "ad_group_hierarchy", "c7b36f2a"),
                         "adapt-metadata-u-1-1000000001-ad-group-hierarchy-c7b36f2a")
        long_a = k8s_job_name("performance", "a-very-long-user-name", "1000000001", "campaign_performance", "run1")
        long_b = k8s_job_name("performance", "a-very-long-user-name", "1000000001", "campaign_performance", "run2")
        for name in (long_a, long_b):
            self.assertLessEqual(len(name), 63)
            self.assertRegex(name, DNS_LABEL)
        self.assertNotEqual(long_a, long_b)

    def test_counts_from_log(self):
        lines = ["[t] INFO adapt.source: stream 'campaigns': 2,345 record(s) written (campaigns: 2,345, extra: 1), "
                 "3 request(s) (raw: 3), 0 retries, 0 failed partition(s), 1.0 s",
                 "[t] INFO adapt.source: run finished in 3.0 s: 1 stream(s), 2,346 record(s) written (campaigns: 2,345, "
                 "extra: 1), 3 request(s), 0 retries, 0 failed partition(s), 1 output(s) written"]
        streams, records = counts_from_log(lines)
        self.assertEqual(streams, [{"name": "campaigns", "exports": {"campaigns": 2345, "extra": 1}}])
        self.assertEqual(records, {"campaigns": 2345, "extra": 1})
        self.assertEqual(counts_from_log(lines[:1])[1], {"campaigns": 2345, "extra": 1})
        self.assertEqual(counts_from_log(["[t] INFO adapt.source: stream 'x': 0 record(s) written, 1 request(s)"]),
                         ([{"name": "x", "exports": {}}], {}))

    def test_execution_mode(self):
        self.assertEqual(execution_mode({"ADAPT_EXECUTION": "K8s"}), "k8s")


class K8sRunTest(unittest.TestCase):
    """run(mode='k8s') and the whole op graph through a stand-in kubectl (no cluster)."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.state = SCRATCH / "kubectl-state"
        self.kubectl = SCRATCH / "kubectl"
        self.kubectl.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, FAKE_KUBECTL))
        self.kubectl.chmod(0o755)
        self.env = {"ADAPT_KUBECTL_BIN": str(self.kubectl), "FAKE_KUBECTL_STATE": str(self.state)}

    def calls(self):
        with open(self.state / "calls.jsonl") as handle:
            return [json.loads(line) for line in handle]

    def submitted(self, name):
        with open(self.state / ("job-%s.json" % name)) as handle:
            return json.load(handle)

    def _run(self, extra_env=None, secret_env=None, **kwargs):
        argv = ["adapt", "run", str(REPO_ROOT / "examples/sources/ads/google_ads"), "--stream", "campaigns",
                "--output", "duckdb:%s:google_ads_1" % (SCRATCH / "wh" / "u1.duckdb")]
        secret_env = {"ADAPT_SECRET_DEVELOPER_TOKEN": FAKE_APP["developer_token"]} if secret_env is None else secret_env
        log = ListLog()
        with mock.patch.dict(os.environ, {**self.env, **(extra_env or {})}):
            result = run(argv, secret_env, log, SCRATCH / "runs" / "r1" / "campaigns.summary.json", mode="k8s",
                         image=IMAGE, user="u1", name="adapt-test-campaigns", labels={"node": "campaigns"}, **kwargs)
        return result, log

    def test_a_node_runs_as_a_job(self):
        result, log = self._run()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["execution"], "k8s")
        self.assertEqual((result["job"], result["namespace"], result["pod"]),
                         ("adapt-test-campaigns", "adapt", "adapt-test-campaigns-abcde"))
        self.assertEqual(result["records"], {"campaigns": 1234})
        self.assertEqual(result["streams"], ["campaigns"])
        self.assertTrue(result["pod_command"].startswith("adapt run /app/examples/sources/ads/google_ads --stream campaigns"))
        verbs = [call["args"][:2] for call in self.calls()]
        self.assertEqual(verbs[:2], [["get", "secret"], ["create", "-f"]])
        self.assertIn(["logs", "-f"], verbs)
        self.assertEqual(verbs[-1], ["delete", "job"])
        self.assertEqual({call["context"] for call in self.calls()}, {"kind-adapt"})
        self.assertTrue((self.state / "deleted-adapt-test-campaigns").exists())
        manifest = self.submitted("adapt-test-campaigns")
        self.assertEqual(container_of(manifest)["imagePullPolicy"], "Never")
        self.assertEqual(pairs(container_of(manifest)["command"], "--output"),
                         ["ducklake:%s:google_ads_1" % CATALOG])
        messages = [message for _, message in log.lines]
        self.assertTrue(messages[0].startswith("k8s: Job adapt/adapt-test-campaigns (context kind-adapt"), messages[0])
        self.assertIn("[2026-10-05 00:00:00,000] INFO fake-pod: envFrom adapt-secrets", messages)
        self.assertTrue(any("run finished in" in message for message in messages))
        self.assertIn("completed (exit code 0); deleted", messages[-1])
        dumped = json.dumps(manifest) + repr(log.lines) + json.dumps(result) + json.dumps(self.calls())
        self.assertNotIn(FAKE_APP["developer_token"], dumped)
        with open(result["summary_path"]) as handle:
            summary = json.load(handle)
        self.assertEqual((summary["status"], summary["streams"]),
                         ("ok", [{"name": "campaigns", "exports": {"campaigns": 1234}}]))

    def test_a_failed_job_raises_and_is_deleted(self):
        with self.assertRaises(RunFailed) as caught:
            self._run({"FAKE_ADAPT_EXIT": "3"})
        self.assertIn("Job adapt/adapt-test-campaigns failed (exit code 3)", str(caught.exception))
        self.assertIn("boom", str(caught.exception))
        self.assertEqual(caught.exception.result["status"], "failed")
        self.assertTrue((self.state / "deleted-adapt-test-campaigns").exists())

    def test_keep_jobs(self):
        result, log = self._run({"ADAPT_K8S_KEEP_JOBS": "1"})
        self.assertTrue(result["job_kept"])
        self.assertFalse((self.state / "deleted-adapt-test-campaigns").exists())
        self.assertIn("; kept", log.lines[-1][1])

    def test_a_missing_secret_key_fails_before_the_job(self):
        keys = "ADAPT_SECRET_DEVELOPER_TOKEN,ADAPT_DUCKLAKE_S3_KEY_ID,ADAPT_DUCKLAKE_S3_SECRET"
        with self.assertRaises(K8sJobError) as caught:
            self._run({"FAKE_KUBECTL_SECRET_KEYS": keys})
        self.assertIn("Secret adapt/adapt-secrets has no ADAPT_DUCKLAKE_CATALOG_PASSWORD", str(caught.exception))
        self.assertNotIn(["create", "-f", "-"], [call["args"][:3] for call in self.calls()])

    def _trigger(self, extra_env=None):
        env = {**self.env, "ADAPT_EXECUTION": "k8s", "ADAPT_IMAGE": "",
               "ADAPT_PIPELINE_WAREHOUSE_DIR": str(SCRATCH / "warehouse"),
               "ADAPT_PIPELINE_RUNS_DIR": str(SCRATCH / "runs")}
        env.update(extra_env or {})
        with mock.patch.dict(os.environ, env), mock.patch.object(accounts, "app_config", lambda n: dict(FAKE_APP)):
            return definitions.trigger("metadata", "u1")

    def test_trigger_runs_every_node_as_a_job_in_dependency_order(self):
        results = self._trigger()
        self.assertTrue(results[0]["success"])
        spec = definitions.PIPELINES["metadata"]
        nodes = results[0]["nodes"]
        self.assertEqual(set(nodes), set(spec.nodes))
        run_id = results[0]["run_id"][:8]
        for name, out in nodes.items():
            self.assertEqual(out["execution"], "k8s")
            self.assertEqual(out["image"], IMAGE)
            self.assertEqual(out["job"], k8s_job_name("metadata", "u1", "1000000001", name, run_id))
            self.assertEqual(out["records"], {name: 1234})
            command = container_of(self.submitted(out["job"]))["command"]
            self.assertEqual(command[:5], ["adapt", "run", "/app/examples/sources/ads/google_ads", "--stream", name])
            self.assertEqual(pairs(command, "--output"), [DUCKLAKE_OUTPUT])
            self.assertEqual(self.submitted(out["job"])["metadata"]["labels"]["adapt-pipeline/run"],
                             results[0]["run_id"])
        # an upstream node's Job is created, finished and deleted before a downstream node's Job is created
        events = [tuple(json.loads(line)) for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(len([event for event in events if event[0] == "create"]), len(spec.nodes))
        for up, down in spec.edges:
            self.assertLess(events.index(("delete", nodes[up]["job"])), events.index(("create", nodes[down]["job"])))
            self.assertLessEqual(nodes[up]["started_at"] + nodes[up]["duration_s"], nodes[down]["started_at"])
        dumped = json.dumps(results, default=str) + (self.state / "calls.jsonl").read_text()
        dumped += "".join(path.read_text() for path in self.state.glob("job-*.json"))
        for value in FAKE_APP.values():
            self.assertNotIn(value, dumped)

    def test_a_failed_job_fails_its_op_and_skips_the_downstream_nodes(self):
        results = self._trigger({"FAKE_ADAPT_EXIT": "1"})
        self.assertFalse(results[0]["success"])
        self.assertEqual(set(results[0]["nodes"].values()), {None})
        self.assertEqual(len(list(self.state.glob("job-*.json"))), 1)   # campaigns only: nothing ran after it


if __name__ == "__main__":
    unittest.main()
