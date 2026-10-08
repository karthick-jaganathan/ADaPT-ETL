#!/usr/bin/env bash
# Sets up the in-cluster object store + catalog that STREAMWRIGHT_EXECUTION=k8s writes the DuckLake warehouse to, on the
# kind cluster (kubectl context kind-streamwright by default):
#
#   namespace streamwright
#   localstack        LocalStack S3 (localstack.streamwright.svc.cluster.local:4566), bucket s3://streamwright-warehouse
#   catalog-postgres  Postgres 16 (catalog-postgres.streamwright.svc.cluster.local:5432), database streamwrightcat, user streamwright
#   Secret catalog-postgres-auth  POSTGRES_PASSWORD (generated once, then reused)
#   Secret streamwright-secrets          what every Job pod gets with envFrom:
#       STREAMWRIGHT_SECRET_DEVELOPER_TOKEN / _CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN  (read from the google_ads secrets
#                                                                                    file NOW - never committed)
#       STREAMWRIGHT_DUCKLAKE_S3_KEY_ID / STREAMWRIGHT_DUCKLAKE_S3_SECRET                         (test/test: LocalStack)
#       STREAMWRIGHT_DUCKLAKE_CATALOG_PASSWORD                                             (= POSTGRES_PASSWORD)
#
# The secret values never touch an argv, a file or the output: a small Python reads the secrets file and prints the
# Secret objects to `kubectl apply --server-side -f -` on a pipe (server-side apply keeps no last-applied annotation,
# so the values are only in the Secrets' data). Idempotent: run it again to refresh the Secret from the file.
#
#   bash orchestration/k8s/setup.sh
#
# Environment: STREAMWRIGHT_K8S_CONTEXT (kind-streamwright), STREAMWRIGHT_K8S_SECRET (streamwright-secrets), STREAMWRIGHT_SECRETS_FILE
# (~/.streamwright/secrets.yaml) and STREAMWRIGHT_ACCOUNTS_FILE (~/.streamwright/accounts.yaml) - the providers the Secret is built from -
# STREAMWRIGHT_K8S_USER (u1, whose google_ads account's token is used), KIND_BIN / KIND_CLUSTER (kind, streamwright: the LocalStack
# and Postgres images are loaded from the local docker onto the kind node when they are there; SKIP_KIND_LOAD=1 lets
# the node pull them), PYTHON (a python with PyYAML: orchestration/.venv/bin/python).
#
# The pipeline image itself (streamwright-pipeline:local, imagePullPolicy Never) is built and loaded separately:
#   bash orchestration/docker/build.sh && kind load docker-image streamwright-pipeline:local --name streamwright
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$HERE")"
CONTEXT="${STREAMWRIGHT_K8S_CONTEXT:-kind-streamwright}"
NAMESPACE=streamwright   # the namespace of the manifests
SECRET="${STREAMWRIGHT_K8S_SECRET:-streamwright-secrets}"
export STREAMWRIGHT_SECRETS_FILE="${STREAMWRIGHT_SECRETS_FILE:-$HOME/.streamwright/secrets.yaml}"
export STREAMWRIGHT_ACCOUNTS_FILE="${STREAMWRIGHT_ACCOUNTS_FILE:-$HOME/.streamwright/accounts.yaml}"
DEMO_USER="${STREAMWRIGHT_K8S_USER:-u1}"
KIND_BIN="${KIND_BIN:-$(command -v kind || echo /tmp/bin/kind)}"
KIND_CLUSTER="${KIND_CLUSTER:-streamwright}"
PYTHON="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"
PIPELINE_IMAGE="${STREAMWRIGHT_IMAGE:-streamwright-pipeline:local}"

k() { kubectl --context "$CONTEXT" "$@"; }
say() { printf '\n== %s\n' "$*"; }

[[ -f "$STREAMWRIGHT_SECRETS_FILE" ]] || { echo "no secrets file at $STREAMWRIGHT_SECRETS_FILE" >&2; exit 1; }
[[ -f "$STREAMWRIGHT_ACCOUNTS_FILE" ]] || { echo "no accounts file at $STREAMWRIGHT_ACCOUNTS_FILE" >&2; exit 1; }
[[ -x "$PYTHON" ]] || PYTHON=python3

say "namespace $NAMESPACE (context $CONTEXT)"
k apply -f "$HERE/namespace.yaml"

if [[ "$CONTEXT" == kind-* && -x "$KIND_BIN" && "${SKIP_KIND_LOAD:-}" != 1 ]]; then
  node="${KIND_CLUSTER}-control-plane"
  for image in localstack/localstack:3 postgres:16; do
    if docker exec "$node" crictl inspecti "docker.io/$image" >/dev/null 2>&1; then
      echo "image $image: already on $node"
    elif docker image inspect "$image" >/dev/null 2>&1; then
      say "kind load $image"
      "$KIND_BIN" load docker-image "$image" --name "$KIND_CLUSTER"
    else
      echo "image $image: not in the local docker; the node pulls it"
    fi
  done
  if ! docker exec "$node" crictl inspecti "docker.io/library/$PIPELINE_IMAGE" >/dev/null 2>&1; then
    echo "WARNING: $PIPELINE_IMAGE is not on $node: the Jobs (imagePullPolicy Never) cannot start until" \
         "\`$KIND_BIN load docker-image $PIPELINE_IMAGE --name $KIND_CLUSTER\`" >&2
  fi
fi

say "Secrets catalog-postgres-auth and $SECRET (from $STREAMWRIGHT_SECRETS_FILE + $STREAMWRIGHT_ACCOUNTS_FILE; not printed)"
# Reuse the catalog password the running Postgres was initialised with; generate one the first time.
CATALOG_PASSWORD="$(k -n "$NAMESPACE" get secret catalog-postgres-auth \
  -o go-template='{{index .data "POSTGRES_PASSWORD" | base64decode}}' 2>/dev/null || true)"
[[ -n "$CATALOG_PASSWORD" ]] || CATALOG_PASSWORD="$(openssl rand -hex 24)"
export CATALOG_PASSWORD
PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
"$PYTHON" - "$DEMO_USER" "$NAMESPACE" "$SECRET" <<'PY' | k apply --server-side --force-conflicts -f -
import json
import os
import sys

from streamwright.orchestration import accounts

user, namespace, name = sys.argv[1:4]
network = "google_ads"
rows = [row for row in accounts.load_accounts() if row["user_id"] == user and row["network"] == network]
if not rows:
    sys.exit("no %s account for user %r in %s" % (network, user, os.environ.get("STREAMWRIGHT_ACCOUNTS_FILE")))
row = rows[0]
app = accounts.app_config(network, row.get("region"))   # app creds, resolved for the account's region
values = {"developer_token": app.get("developer_token"), "client_id": app.get("client_id"),
          "client_secret": app.get("client_secret"), "refresh_token": row.get("token")}  # the token lives on the account
missing = [key for key, value in values.items() if not value]
if missing:
    sys.exit("cannot build %s: no %s (secrets/accounts for %s)" % (name, ", ".join(missing), user))
password = os.environ["CATALOG_PASSWORD"]
labels = {"app.kubernetes.io/part-of": "streamwright-pipeline"}
streamwright = {"STREAMWRIGHT_SECRET_%s" % key.upper(): str(value) for key, value in values.items()}
streamwright.update(STREAMWRIGHT_DUCKLAKE_S3_KEY_ID=os.environ.get("STREAMWRIGHT_K8S_S3_KEY_ID", "test"),
             STREAMWRIGHT_DUCKLAKE_S3_SECRET=os.environ.get("STREAMWRIGHT_K8S_S3_SECRET", "test"),
             STREAMWRIGHT_DUCKLAKE_CATALOG_PASSWORD=password)


def secret(secret_name, data):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": secret_name, "namespace": namespace, "labels": labels}, "stringData": data}


print(json.dumps({"apiVersion": "v1", "kind": "List",
                  "items": [secret("catalog-postgres-auth", {"POSTGRES_PASSWORD": password}), secret(name, streamwright)]}))
PY
unset CATALOG_PASSWORD

say "LocalStack S3 and the catalog Postgres"
k apply -f "$HERE/localstack.yaml" -f "$HERE/catalog-postgres.yaml"
k -n "$NAMESPACE" rollout status deploy/catalog-postgres --timeout=300s
k -n "$NAMESPACE" rollout status deploy/localstack --timeout=600s

say "bucket s3://streamwright-warehouse"
k -n "$NAMESPACE" exec deploy/localstack -- sh -c \
  'awslocal s3api head-bucket --bucket streamwright-warehouse >/dev/null 2>&1 || awslocal s3 mb s3://streamwright-warehouse'
k -n "$NAMESPACE" exec deploy/localstack -- awslocal s3 ls

say "catalog database"
k -n "$NAMESPACE" exec deploy/catalog-postgres -- psql -U streamwright -d streamwrightcat -Atc \
  "SELECT 'database ' || current_database() || ' ready for user ' || current_user"

say "resources"
k -n "$NAMESPACE" get pods,svc,secret -o wide
echo
echo "Secret $SECRET keys (no values):"
# shellcheck disable=SC2016  # a go-template, not a shell expansion
k -n "$NAMESPACE" get secret "$SECRET" -o go-template='{{range $key, $_ := .data}}  {{$key}}{{"\n"}}{{end}}'
