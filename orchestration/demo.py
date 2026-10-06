"""
Live end-to-end demo: trigger BOTH named pipelines - metadata, then performance (config/pipelines/<name>.yaml, the jobs
ads_metadata and ads_performance) - for user u1's Google Ads account 1000000001.

    ADAPT_BIN=/tmp/adapt-sdk-venv/bin/adapt .venv/bin/python demo.py 2>&1 | tee demo_run.log
    .venv/bin/python demo.py performance              # only the named pipeline(s)
    ADAPT_EXECUTION=docker .venv/bin/python demo.py 2>&1 | tee demo_docker_run.log   # each node in a container
    ADAPT_EXECUTION=k8s .venv/bin/python demo.py metadata u1 1000000001 2>&1 | tee demo_k8s_run.log  # k8s Jobs

Each pipeline runs its op graph in dependency order (each node one `adapt run --stream <node>`, locally - the default
subprocess mode - or, with ADAPT_EXECUTION=docker, in a `docker run --rm` of the network's image, or, with
ADAPT_EXECUTION=k8s, as a Kubernetes Job of that image writing a DuckLake on the in-cluster S3 + Postgres catalog of
k8s/setup.sh); the demo prints the order the nodes ran in, each node's wrapper, command and records, and the rows in
the warehouse (k8s: the data files in the bucket and the catalog's tables). Finally it shows -
without running it - the command the custom campaigns wrapper builds for an account the stub campaigns DB has campaign
ids for.
"""

import subprocess
import sys

import duckdb

from adapt.orchestration import accounts, execution_mode, k8s_settings
from adapt.orchestration.context import make_context
from adapt.orchestration.custom_wrappers import CAMPAIGN_DB
from adapt.orchestration.definitions import job_name, trigger
from adapt.orchestration.runner import k8s_catalog_schema, k8s_data_path
from adapt.orchestration.spec import load_pipeline, load_pipelines
from adapt.orchestration.wrappers import wrapper_for

USER_ID, ACCOUNT_ID = "u1", "1000000001"
PIPELINES = ("metadata", "performance")


def show_ducklake(user_id):
    """k8s mode: the user's DuckLake - its data files in the in-cluster bucket and its tables in the catalog."""
    settings = k8s_settings()
    kubectl = [settings["kubectl"], "--context", settings["context"], "--namespace", settings["namespace"]]
    data_path = k8s_data_path(user_id, settings)
    schema = k8s_catalog_schema(user_id)
    print("\n=== DuckLake data in %s (kubectl exec deploy/localstack -- awslocal s3 ls --recursive):" % data_path)
    listing = subprocess.run(kubectl + ["exec", "deploy/localstack", "--", "awslocal", "s3", "ls", data_path,
                                        "--recursive"], capture_output=True, text=True)
    print("\n".join("    " + line for line in (listing.stdout or listing.stderr).splitlines()))
    query = ("SELECT s.schema_name || '.' || t.table_name, coalesce(sum(f.record_count), 0) FROM %s.ducklake_table t "
             "JOIN %s.ducklake_schema s USING (schema_id) LEFT JOIN %s.ducklake_data_file f ON f.table_id = t.table_id "
             "AND f.end_snapshot IS NULL WHERE t.end_snapshot IS NULL GROUP BY 1 ORDER BY 1" % (schema, schema, schema))
    print("=== DuckLake catalog tables (Postgres schema %s; kubectl exec deploy/catalog-postgres -- psql):" % schema)
    tables = subprocess.run(kubectl + ["exec", "deploy/catalog-postgres", "--", "psql", "-U", "adapt", "-d", "adaptcat",
                                       "-AtF", " ", "-c", query], capture_output=True, text=True)
    for line in (tables.stdout or tables.stderr).splitlines():
        name, _, rows = line.rpartition(" ")
        print("    %-50s %6s rows" % (name, rows) if name else "    " + line)


def run_pipeline(name, user_id=USER_ID, account_id=ACCOUNT_ID):
    spec = load_pipeline(name)
    print("\n\n######## pipeline %s (job %s): order %s" % (name, job_name(name), " -> ".join(spec.order)))
    print("=== edges: %s" % (", ".join("%s -> %s" % edge for edge in spec.edges) or "-"))
    print("=== execution: %s" % execution_mode())
    results = trigger(name, user_id, account_id)
    ok = True
    for result in results:
        print("\n=== pipeline %s job %s run %s %s: %s" % (
            result["pipeline"], result["job"], result["run_id"], result["run_config"],
            "SUCCESS" if result["success"] else "FAILED"))
        ok = ok and result["success"]
        ran = sorted((out for out in result["nodes"].values() if out), key=lambda out: out["started_at"])
        print("=== node order (as executed): %s" % " -> ".join(out["node"] for out in ran))
        for position, out in enumerate(ran, 1):
            upstream = ", ".join(spec.upstream(out["node"])) or "-"
            print("\n[%d] %s (after: %s) wrapper=%s status=%s records=%s %.1fs" % (
                position, out["node"], upstream, out["wrapper"], out["status"], out["records"], out["duration_s"]))
            print("    $ %s" % out["command"])
            if out.get("execution") == "docker":
                print("    in container (image %s): $ %s" % (out["image"], out["docker_command"]))
            elif out.get("execution") == "k8s":
                print("    as Kubernetes Job %s/%s (pod %s on %s, image %s): $ %s" % (
                    out["namespace"], out["job"], out["pod"], out["node_name"], out["image"], out["pod_command"]))
        missing = [node for node, out in result["nodes"].items() if not out]
        if missing:
            print("=== nodes that did not run: %s" % ", ".join(missing))
        if execution_mode() == "k8s":
            show_ducklake(user_id)
            continue
        ctx = make_context(user_id, result["run_config"]["account_id"], result["run_config"]["network"],
                           app_loader=lambda network: {})
        if not ctx.warehouse_path.exists():
            print("\n=== no warehouse at %s" % ctx.warehouse_path)
            continue
        with duckdb.connect(str(ctx.warehouse_path), read_only=True) as con:
            rows = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = ? "
                               "ORDER BY table_name", [ctx.schema]).fetchall()
            print("\n=== warehouse %s schema %s:" % (ctx.warehouse_path, ctx.schema))
            for (table,) in rows:
                count = con.execute('SELECT count(*) FROM "%s"."%s"' % (ctx.schema, table)).fetchone()[0]
                print("    %-28s %6d rows" % (table, count))
    return ok


def main(argv=None):
    """demo.py [PIPELINE ...] [USER_ID [ACCOUNT_ID]]: the arguments that are not pipeline names are the user/account."""
    args = sys.argv[1:] if argv is None else argv
    known = load_pipelines()
    names = [arg for arg in args if arg in known] or PIPELINES
    rest = [arg for arg in args if arg not in known]
    if len(rest) > 2:
        sys.exit("usage: demo.py [PIPELINE ...] [USER_ID [ACCOUNT_ID]] (pipelines: %s)" % ", ".join(known))
    user_id, account_id = (rest + [USER_ID, ACCOUNT_ID][len(rest):])[:2]
    outcomes = {name: run_pipeline(name, user_id, account_id) for name in names}

    db_account = next(iter(CAMPAIGN_DB))
    demo_row = {"user_id": "demo", "account_id": db_account, "network": "google_ads"}
    ctx = make_context("demo", db_account, "google_ads", accounts=[demo_row], app_loader=accounts.app_config)
    wrapper = wrapper_for("campaigns", "google_ads")
    command, secret_env = wrapper("campaigns", ctx)
    print("\n=== custom wrapper %s for account %s (in CAMPAIGN_DB: %s) - built, not run:" % (
        wrapper.__name__, db_account, CAMPAIGN_DB[db_account]))
    print("    $ %s" % " ".join(command))
    print("    secrets via environment: %s" % ", ".join(sorted(secret_env)))
    print()
    for name, ok in outcomes.items():
        print("=== pipeline %s: %s" % (name, "ALL NODES SUCCEEDED" if ok else "FAILED"))
    return 0 if all(outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
