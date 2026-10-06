"""ADAPT_EXECUTION=docker: the `docker run` argv built from a node's logical argv, and runs through a stand-in docker."""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FAKE_APP, GOOGLE_SECRET_ENV, PROJECT_DIR, google_context
from adapt.orchestration import REPO_ROOT, accounts, adapt_image, definitions, execution_mode
from adapt.orchestration.runner import DockerCommandError, RunFailed, container_name, docker_command, run
from adapt.orchestration.spec import SpecError, load_networks, parse_networks
from adapt.orchestration.wrappers import wrapper_for

FAKE_DOCKER = str(Path(__file__).resolve().parent / "fake_docker.py")
SCRATCH = PROJECT_DIR / "tests" / ".scratch_docker"
IMAGE = "adapt-pipeline:local"


class ListLog:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda message: self.lines.append((level, message))


def pairs(argv, flag):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


def build(node="campaigns", ctx=None, environ=None, runs=None, **kwargs):
    """(docker argv, secret_env, ctx) of a google_ads node, as definitions.py would run it."""
    ctx = ctx or google_context()
    argv, secret_env = wrapper_for(node, ctx.network)(node, ctx)
    runs = Path(runs or PROJECT_DIR / "runs")
    logical = argv + ["--summary", str(runs / "run-1" / ("%s.summary.json" % node))]
    command = docker_command(logical, secret_env, kwargs.pop("image", IMAGE), ctx.warehouse_path.parent, runs,
                             REPO_ROOT, name=container_name("u1", "1000000001", node, "run-1"),
                             environ={} if environ is None else environ, docker="docker", **kwargs)
    return command, secret_env, ctx


class DockerCommandTest(unittest.TestCase):
    def test_campaigns_docker_run(self):
        command, secret_env, ctx = build("campaigns")
        self.assertEqual(command[:6], ["docker", "run", "--rm", "--init", "--pull", "never"])
        self.assertEqual(pairs(command, "--name"), ["adapt-pipe-u1-1000000001-campaigns-run-1"])
        self.assertEqual(pairs(command, "--add-host"), ["host.docker.internal:host-gateway"])
        self.assertEqual(pairs(command, "-v"), ["%s:/warehouse" % ctx.warehouse_path.parent.resolve(),
                                                "%s:/runs" % (PROJECT_DIR / "runs").resolve()])
        image_at = command.index(IMAGE)
        self.assertEqual(command[image_at + 1:image_at + 3], ["run", "/app/examples/sources/ads/google_ads"])
        adapt = command[image_at + 1:]
        self.assertEqual(pairs(adapt, "--stream"), ["campaigns"])
        self.assertEqual(pairs(adapt, "--output"), ["duckdb:/warehouse/u1.duckdb:google_ads_1000000001"])
        self.assertEqual(pairs(adapt, "--summary"), ["/runs/run-1/campaigns.summary.json"])
        self.assertEqual(pairs(adapt, "--timezone"), ["America/New_York"])
        self.assertEqual(pairs(adapt, "--allow-connector"), ["google_ads", "gaql"])
        self.assertIn("customer_ids=1000000001", pairs(adapt, "--set"))
        self.assertIn("login_customer_id=2000000002", pairs(adapt, "--set"))
        # docker options come before the image; adapt's after it
        self.assertTrue(all(command.index(flag) < image_at for flag in ("--rm", "--init", "-v", "-e", "--add-host")))

    def test_secrets_are_passed_by_name_only(self):
        command, secret_env, _ = build("ad_groups")
        self.assertEqual(set(secret_env), GOOGLE_SECRET_ENV)
        self.assertEqual(sorted(pairs(command, "-e")), sorted(GOOGLE_SECRET_ENV))
        self.assertIn("ADAPT_SECRET_DEVELOPER_TOKEN", pairs(command, "-e"))
        joined = " ".join(command)
        for value in FAKE_APP.values():
            self.assertNotIn(value, joined)
        self.assertFalse([arg for arg in command if arg.startswith("ADAPT_SECRET_") and "=" in arg])

    def test_no_host_path_leaks_into_the_adapt_args(self):
        command, _, _ = build("keywords")
        adapt = command[command.index(IMAGE) + 1:]
        self.assertFalse([arg for arg in adapt if str(REPO_ROOT) in arg], adapt)

    def test_set_values_with_repo_paths_become_app_paths(self):
        logical = ["adapt", "run", str(REPO_ROOT / "examples/sources/readers/files_demo"),
                   "--set", "root=%s/examples/sources/readers/files_demo/data" % REPO_ROOT, "--set", "mode=full",
                   "--output", "duckdb:%s:files" % (SCRATCH / "wh" / "u9.duckdb"),
                   "--summary", str(SCRATCH / "runs" / "r" / "files.summary.json")]
        command = docker_command(logical, {}, IMAGE, SCRATCH / "wh", SCRATCH / "runs", REPO_ROOT, environ={})
        adapt = command[command.index(IMAGE) + 1:]
        self.assertEqual(adapt[:2], ["run", "/app/examples/sources/readers/files_demo"])
        self.assertEqual(pairs(adapt, "--set"), ["root=/app/examples/sources/readers/files_demo/data", "mode=full"])
        self.assertEqual(pairs(adapt, "--output"), ["duckdb:/warehouse/u9.duckdb:files"])
        self.assertEqual(pairs(adapt, "--summary"), ["/runs/r/files.summary.json"])
        self.assertNotIn("--name", command)
        self.assertNotIn("-e", command)

    def test_adapt_docker_args_go_before_the_image(self):
        command, _, _ = build("campaigns", environ={"ADAPT_DOCKER_ARGS": "--user 1000:1000 --network adapt"})
        image_at = command.index(IMAGE)
        self.assertEqual(command[image_at - 4:image_at], ["--user", "1000:1000", "--network", "adapt"])

    def test_refusals(self):
        with self.assertRaises(DockerCommandError):   # no image
            build("campaigns", image=None)
        with self.assertRaises(DockerCommandError):   # the summary outside the runs mount
            docker_command(["adapt", "run", str(REPO_ROOT / "examples/sources/ads/google_ads"),
                            "--summary", str(SCRATCH / "elsewhere" / "s.json")],
                           {}, IMAGE, SCRATCH, PROJECT_DIR / "runs", REPO_ROOT, environ={})
        ctx = google_context()
        argv, secret_env = wrapper_for("campaigns", "google_ads")("campaigns", ctx)
        with self.assertRaises(DockerCommandError):   # a source outside the repository (not in the image)
            docker_command(["adapt", "run", "/somewhere/else"], {}, IMAGE, SCRATCH, SCRATCH, REPO_ROOT, environ={})
        with self.assertRaises(DockerCommandError):   # a secret value on the command line
            docker_command(argv + ["--set", "x=%s" % FAKE_APP["client_id"], "--summary", str(SCRATCH / "s.json")],
                           secret_env, IMAGE, ctx.warehouse_path.parent, SCRATCH, REPO_ROOT, environ={})
        with self.assertRaises(ValueError):
            docker_command(argv, {"PATH": "/x"}, IMAGE, ctx.warehouse_path.parent, SCRATCH, REPO_ROOT, environ={})

    def test_container_name(self):
        self.assertEqual(container_name("u 1", "1000000001", "campaigns", "ab/cd"),
                         "adapt-pipe-u-1-1000000001-campaigns-ab-cd")


class ImageAndModeTest(unittest.TestCase):
    def test_google_ads_has_the_pipeline_image(self):
        self.assertEqual(load_networks()["google_ads"].image, IMAGE)
        self.assertEqual(google_context().image, IMAGE)

    def test_image_is_optional_and_validated(self):
        body = {"source": "examples/sources/ads/google_ads", "streams": {"campaigns": "campaigns"}}
        self.assertIsNone(parse_networks({"networks": {"g": dict(body)}})["g"].image)
        with self.assertRaises(SpecError):
            parse_networks({"networks": {"g": dict(body, image=["x"])}})

    def test_adapt_image_overrides_the_network_image(self):
        self.assertEqual(adapt_image(IMAGE, {}), IMAGE)
        self.assertEqual(adapt_image(IMAGE, {"ADAPT_IMAGE": "other:1"}), "other:1")
        self.assertIsNone(adapt_image(None, {}))

    def test_execution_mode(self):
        self.assertEqual(execution_mode({}), "subprocess")
        self.assertEqual(execution_mode({"ADAPT_EXECUTION": "Docker"}), "docker")
        self.assertEqual(execution_mode({"ADAPT_EXECUTION": "k8s"}), "k8s")
        with self.assertRaises(ValueError):
            execution_mode({"ADAPT_EXECUTION": "lambda"})


class DockerRunTest(unittest.TestCase):
    """run(mode='docker') and the whole op graph through a stand-in docker CLI (no real container)."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.docker = SCRATCH / "docker"
        self.docker.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, FAKE_DOCKER))
        self.docker.chmod(0o755)

    def test_secret_values_reach_the_container_through_the_docker_env_only(self):
        log = ListLog()
        source = REPO_ROOT / "examples/sources/ads/google_ads"
        argv = ["adapt", "run", str(source), "--stream", "campaigns",
                "--output", "duckdb:%s:g" % (SCRATCH / "wh" / "u1.duckdb")]
        secret_env = {"ADAPT_SECRET_DEVELOPER_TOKEN": FAKE_APP["developer_token"]}
        with mock.patch.dict(os.environ, {"ADAPT_DOCKER_BIN": str(self.docker)}):
            result = run(argv, secret_env, log, SCRATCH / "runs" / "r1" / "campaigns.summary.json", mode="docker",
                         image=IMAGE, warehouse_dir=SCRATCH / "wh", runs_dir=SCRATCH / "runs", repo_root=REPO_ROOT,
                         name="adapt-pipe-test")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["execution"], "docker")
        self.assertEqual(result["image"], IMAGE)
        self.assertEqual(result["records"], {"campaigns": 3})
        messages = [message for _, message in log.lines]
        self.assertTrue(messages[0].startswith("docker: "), messages[0])
        self.assertIn("-e ADAPT_SECRET_DEVELOPER_TOKEN ", messages[0])
        self.assertIn("[2026-10-05 00:00:00,000] INFO adapt.source: ADAPT_SECRET_DEVELOPER_TOKEN=***", messages)
        self.assertTrue(any("run /app/examples/sources/ads/google_ads --stream campaigns" in m for m in messages))
        self.assertNotIn(FAKE_APP["developer_token"], repr(log.lines) + json.dumps(result))
        self.assertNotIn("ADAPT_SECRET_DEVELOPER_TOKEN", os.environ)

    def test_non_zero_exit_raises(self):
        argv = ["adapt", "run", str(REPO_ROOT / "examples/sources/ads/google_ads"), "--stream", "campaigns"]
        env = {"ADAPT_DOCKER_BIN": str(self.docker), "FAKE_ADAPT_EXIT": "2"}
        with mock.patch.dict(os.environ, env), self.assertRaises(RunFailed) as caught:
            run(argv, {}, ListLog(), SCRATCH / "runs" / "f.json", mode="docker", image=IMAGE,
                warehouse_dir=SCRATCH / "wh", runs_dir=SCRATCH / "runs")
        self.assertIn("exited with 2: boom", str(caught.exception))

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            run(["adapt", "run", "x"], {}, ListLog(), SCRATCH / "u.json", mode="lambda")

    def _trigger(self, extra_env=None):
        env = {"ADAPT_EXECUTION": "docker", "ADAPT_DOCKER_BIN": str(self.docker), "ADAPT_IMAGE": "",
               "ADAPT_PIPELINE_WAREHOUSE_DIR": str(SCRATCH / "warehouse"),
               "ADAPT_PIPELINE_RUNS_DIR": str(SCRATCH / "runs")}
        env.update(extra_env or {})
        with mock.patch.dict(os.environ, env), mock.patch.object(accounts, "app_config", lambda n: dict(FAKE_APP)):
            return definitions.trigger("metadata", "u1")

    def test_trigger_runs_every_node_in_a_container(self):
        results = self._trigger()
        self.assertTrue(results[0]["success"])
        nodes = results[0]["nodes"]
        self.assertEqual(set(nodes), set(definitions.PIPELINES["metadata"].nodes))
        for name, out in nodes.items():
            self.assertEqual(out["execution"], "docker")
            self.assertEqual(out["image"], IMAGE)
            self.assertIn(" %s run /app/examples/sources/ads/google_ads --stream %s " % (IMAGE, name),
                          out["docker_command"])
            self.assertIn("--output duckdb:/warehouse/u1.duckdb:google_ads_1000000001", out["docker_command"])
        for up, down in definitions.PIPELINES["metadata"].edges:
            self.assertLessEqual(nodes[up]["started_at"], nodes[down]["started_at"])
        dumped = json.dumps(results, default=str)
        for value in FAKE_APP.values():
            self.assertNotIn(value, dumped)

    def test_adapt_image_env_overrides_the_network_image(self):
        results = self._trigger({"ADAPT_IMAGE": "custom-image:7"})
        self.assertTrue(results[0]["success"])
        self.assertEqual({out["image"] for out in results[0]["nodes"].values()}, {"custom-image:7"})


if __name__ == "__main__":
    unittest.main()
