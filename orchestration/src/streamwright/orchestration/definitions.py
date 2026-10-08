"""
One Dagster op graph per (named pipeline, network), as the job `ads_<pipeline>__<network>` (ads_metadata__google_ads,
ads_metadata__microsoft_ads, ...): the pipeline's canonical nodes are specialised to the network (resolve_pipeline) -
each node mapped to the network's own stream name (an alias) or skipped (networks.yaml `streams:`), wired by the
network's `after` override or the pipeline's default edges. One op per kept node (an upstream op's result dict flows
into each downstream op as ctx.upstream). Every op takes the same run config {user_id, account_id, network}, builds its
Context (row, network entry, app-level values loaded at run time), asks the node's wrapper (custom or default) for
(argv, secret_env) - the argv's `--stream` is the network's resolved stream - and runs it with the runner: a local
subprocess, with STREAMWRIGHT_EXECUTION=docker in a `docker run --rm` of the network's image ($STREAMWRIGHT_IMAGE overrides it), or
with STREAMWRIGHT_EXECUTION=k8s as a Kubernetes Job of that image writing to the DuckLake warehouse on object storage (the op
waits for the Job, streams its pod's log and fails if the Job fails; the graph's order is the same).

trigger(pipeline, user_id, account_id=None) looks the user's accounts up, resolves the pipeline on each account's
network (aliases, skips, edge overrides) and executes that network's job once per account row. Every job is in `defs`,
so `dagster dev -m streamwright.orchestration.definitions` shows each pipeline-on-network op graph.
"""

import argparse
import json
import os
import re
import shlex
import sys

from dagster import ConfigMapping, DagsterInstance, Definitions, Failure, In, MetadataValue, Out, job, op

from streamwright.orchestration import accounts as accounts_module
from streamwright.orchestration import custom_wrappers  # noqa: F401  (registers the custom node wrappers)
from streamwright.orchestration.context import make_context
from streamwright.orchestration.runner import K8sJobError, RunFailed, container_name, k8s_data_path, k8s_job_name, run
from streamwright.orchestration.settings import REPO_ROOT, streamwright_image, execution_mode, k8s_settings, runs_dir
from streamwright.orchestration.spec import (SpecError, load_networks, load_pipeline, load_pipelines, resolve_pipeline)
from streamwright.orchestration.wrappers import wrapper_for

ACCOUNT_CONFIG = {"user_id": str, "account_id": str, "network": str}


def build_op(node_name, spec, network_name):
    """
    The op of one node of a pipeline on a network: inputs named after its (effective) upstream nodes, one `result`
    output (the run's result dict). The op definition is named <pipeline>__<network>__<node> (op names are unique
    across all jobs of `defs`); the job invokes it under the node's name, so the node name is the step key.
    """
    ins = {up: In(dict, description="the %s node's result" % up) for up in spec.upstream(node_name)}

    @op(name="%s__%s__%s" % (spec.name, network_name, node_name), ins=ins, out=Out(dict),
        config_schema=ACCOUNT_CONFIG, tags={"kind": "streamwright", "pipeline": spec.name, "network": network_name},
        description="streamwright run --stream %s (after: %s)" % (node_name, ", ".join(spec.upstream(node_name)) or "-"))
    def node_op(context, **upstream):
        config = context.op_config
        ctx = make_context(config["user_id"], config["account_id"], config["network"], upstream=upstream)
        wrapper = wrapper_for(node_name, ctx.network)
        argv, secret_env = wrapper(node_name, ctx)
        command = shlex.join(argv)
        context.log.info("node %s: wrapper %s; upstream %s" % (node_name, wrapper.__name__, ", ".join(
            "%s(%s)" % (up, result.get("status")) for up, result in upstream.items()) or "-"))
        context.log.info("node %s: command: %s" % (node_name, command))
        context.log.info("node %s: secrets via environment: %s" % (node_name, ", ".join(sorted(secret_env)) or "-"))
        summary = runs_dir() / re.sub(r"[^A-Za-z0-9_.-]", "_", context.run_id) / ("%s.summary.json" % node_name)
        if ctx.warehouse_path:
            ctx.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
        mode = execution_mode()
        execution = {"mode": mode}
        if mode in ("docker", "k8s"):
            image = streamwright_image(ctx.image)
            if not image:
                raise Failure(description="node %s: STREAMWRIGHT_EXECUTION=%s but network %s has no `image:` in "
                                          "networks.yaml and $STREAMWRIGHT_IMAGE is not set" % (node_name, mode, ctx.network))
        if mode == "docker":
            execution.update(image=image, warehouse_dir=ctx.warehouse_path.parent, runs_dir=runs_dir(),
                             repo_root=REPO_ROOT,
                             name=container_name(ctx.user_id, ctx.account_id, node_name, context.run_id[:8]))
            context.log.info("node %s: execution docker, image %s" % (node_name, image))
        elif mode == "k8s":
            settings = k8s_settings()
            name = k8s_job_name(spec.name, ctx.user_id, ctx.account_id, node_name, context.run_id[:8])
            execution.update(image=image, user=ctx.user_id, repo_root=REPO_ROOT, name=name, settings=settings,
                             labels={"pipeline": spec.name, "user": ctx.user_id, "account": ctx.account_id,
                                     "network": ctx.network, "node": node_name, "run": context.run_id})
            context.log.info("node %s: execution k8s, Job %s/%s (context %s), image %s, DuckLake catalog %s, data %s"
                             % (node_name, settings["namespace"], name, settings["context"], image,
                                settings["catalog"], k8s_data_path(ctx.user_id, settings)))
        else:
            context.log.info("node %s: execution subprocess (%s)" % (node_name, argv[0]))
        try:
            result = run(argv, secret_env, context.log, summary, **execution)
        except K8sJobError as error:
            raise Failure(description="node %s: %s" % (node_name, error))
        except RunFailed as error:
            raise Failure(description="node %s: %s" % (node_name, error),
                          metadata={"summary": MetadataValue.path(error.result["summary_path"])})
        result.update(node=node_name, wrapper=wrapper.__name__, command=command)
        metadata = {"records": MetadataValue.json(result["records"]), "command": MetadataValue.text(command),
                    "wrapper": wrapper.__name__, "execution": mode}
        if mode == "docker":
            metadata.update(image=result["image"], docker_command=MetadataValue.text(result["docker_command"]))
        elif mode == "k8s":
            metadata.update(image=result["image"], job="%s/%s" % (result["namespace"], result["job"]),
                            pod=result["pod"] or "-", pod_command=MetadataValue.text(result["pod_command"]))
        context.add_output_metadata(metadata)
        return result

    return node_op


def job_name(pipeline, network):
    """The Dagster job of a pipeline on a network: ads_<pipeline>__<network>."""
    return "ads_%s__%s" % (pipeline, network)


def build_job(spec, network, name=None):
    """
    A job running the pipeline's effective nodes on `network` in dependency order; its run config is just
    {user_id, account_id, network}. `spec` is the network-resolved PipelineSpec (resolve_pipeline).
    """
    network_name = network if isinstance(network, str) else network.name
    ops = {node_name: build_op(node_name, spec, network_name) for node_name in spec.nodes}
    fan_out = ConfigMapping(config_schema=ACCOUNT_CONFIG,
                            config_fn=lambda config: {"ops": {n: {"config": dict(config)} for n in spec.nodes}})

    @job(name=name or job_name(spec.name, network_name), config=fan_out,
         tags={"pipeline": spec.name, "network": network_name},
         description="pipeline %s on %s: %s" % (spec.name, network_name,
                                                ", ".join("%s -> %s" % edge for edge in spec.edges) or "(no edges)"))
    def pipeline_job():
        outputs = {}
        for node_name in spec.order:
            outputs[node_name] = ops[node_name].alias(node_name)(**{up: outputs[up] for up in spec.upstream(node_name)})

    return pipeline_job


def _build_all_jobs(pipelines, networks):
    """Every (pipeline, network) the network supports -> its job; a pair the network cannot run is skipped here."""
    jobs = {}
    for pipeline_name, pipeline_spec in pipelines.items():
        for network_name, network in networks.items():
            try:
                effective = resolve_pipeline(pipeline_spec, network)
            except SpecError:
                continue  # this network does not map / support this pipeline's nodes: no job for the pair
            jobs[(pipeline_name, network_name)] = build_job(effective, network)
    return jobs


PIPELINES = load_pipelines()                                 # pipeline name -> PipelineSpec
NETWORKS = load_networks()                                   # network name -> Network
JOBS = _build_all_jobs(PIPELINES, NETWORKS)                  # (pipeline, network) -> its job (ads_<pipeline>__<network>)
defs = Definitions(jobs=list(JOBS.values()))


def job_for(pipeline, network):
    """The job of a pipeline on a network (built now if it was not pre-built, e.g. a file added after import)."""
    if (pipeline, network) in JOBS:
        return JOBS[(pipeline, network)]
    networks = load_networks()
    if network not in networks:
        raise KeyError("network %r is not in networks.yaml" % network)
    return build_job(resolve_pipeline(load_pipeline(pipeline), networks[network]), networks[network])


def run_config_for(row):
    """The run config of one account row: identifiers only - no secret, no token, no other column of the row."""
    return {"user_id": str(row["user_id"]), "account_id": str(row["account_id"]), "network": str(row["network"])}


def plan(pipeline, user_id, account_id=None, accounts=None):
    """
    What trigger() would run: a (row, effective PipelineSpec, job) per matching account row, after checking the
    pipeline exists, each row's network is in networks.yaml and the pipeline resolves on that network (its nodes are
    mapped: aliased, supported or skipped).
    """
    pipeline_spec = load_pipeline(pipeline)
    networks = load_networks()
    plans = []
    for row in accounts_module.find_accounts(user_id, account_id, accounts):
        network = row["network"]
        if network not in networks:
            raise KeyError("account %s: network %r is not in networks.yaml" % (row["account_id"], network))
        effective = resolve_pipeline(pipeline_spec, networks[network])
        job_def = JOBS.get((pipeline, network)) or build_job(effective, networks[network])
        plans.append((row, effective, job_def))
    return plans


def _ui_instance():
    """
    The Dagster instance trigger() records runs into: the persistent one at $DAGSTER_HOME when it is set (so the runs
    appear in `dagster dev`'s UI), else None (execute_in_process' own ephemeral instance - nothing is stored).
    """
    return DagsterInstance.get() if os.environ.get("DAGSTER_HOME") else None


def trigger(pipeline, user_id, account_id=None):
    """
    Runs the named pipeline for each of the user's accounts (or one account): for each account's network it runs the
    network-resolved op graph (ads_<pipeline>__<network>) in dependency order, in this process (execute_in_process: the
    prototype's executor). Returns one {pipeline, job, network, run_id, success, nodes} per run. When $DAGSTER_HOME is
    set the runs are recorded there, so `dagster dev` (pointed at the same $DAGSTER_HOME) shows them in its UI.
    """
    if not isinstance(pipeline, str):
        raise SpecError("trigger(pipeline, user_id, account_id=None): pipeline must be a pipeline name, got %r"
                        % (pipeline,))
    instance = _ui_instance()
    results = []
    for row, effective, job_def in plan(pipeline, user_id, account_id):
        run_config = run_config_for(row)
        outcome = job_def.execute_in_process(run_config=run_config, raise_on_error=False, instance=instance,
                                             tags={"pipeline": pipeline,
                                                   "user_id": run_config["user_id"],
                                                   "account_id": run_config["account_id"],
                                                   "network": run_config["network"]})
        nodes = {}
        for node_name in effective.nodes:
            try:
                nodes[node_name] = outcome.output_for_node(node_name)
            except Exception:  # the node failed
                nodes[node_name] = None
        results.append({"pipeline": pipeline, "job": job_def.name, "network": row["network"],
                        "run_config": run_config, "run_id": outcome.run_id, "success": outcome.success,
                        "nodes": nodes})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a named pipeline for a user's accounts.")
    parser.add_argument("pipeline", help="a pipeline name: %s" % ", ".join(PIPELINES))
    parser.add_argument("user_id")
    parser.add_argument("account_id", nargs="?")
    args = parser.parse_args(argv)
    results = trigger(args.pipeline, args.user_id, args.account_id)
    print(json.dumps(results, indent=2, default=str))
    return 0 if all(result["success"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
