#!/usr/bin/env bash
# Sets up the in-cluster object store + catalog that ADAPT_EXECUTION=k8s writes the DuckLake warehouse to, on the
# kind cluster (kubectl context kind-adapt by default):
#
#   namespace adapt
#   localstack        LocalStack S3 (localstack.adapt.svc.cluster.local:4566), bucket s3://adapt-warehouse
#   catalog-postgres  Postgres 16 (catalog-postgres.adapt.svc.cluster.local:5432), database adaptcat, user adapt
#   Secret catalog-postgres-auth  POSTGRES_PASSWORD (generated once, then reused)
#   Secret adapt-secrets          what every Job pod gets with envFrom:
#       ADAPT_SECRET_DEVELOPER_TOKEN / _CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN  (read from the google_ads secrets
#                                                                                    file NOW - never committed)
#       ADAPT_DUCKLAKE_S3_KEY_ID / ADAPT_DUCKLAKE_S3_SECRET                         (test/test: LocalStack)
#       ADAPT_DUCKLAKE_CATALOG_PASSWORD                                             (= POSTGRES_PASSWORD)
#
# The secret values never touch an argv, a file or the output: a small Python reads the secrets file and prints the
# Secret objects to `kubectl apply --server-side -f -` on a pipe (server-side apply keeps no last-applied annotation,
# so the values are only in the Secrets' data). Idempotent: run it again to refresh the Secret from the file.
#
#   bash orchestration/k8s/setup.sh
#
# Environment: ADAPT_K8S_CONTEXT (kind-adapt), ADAPT_K8S_SECRET (adapt-secrets), ADAPT_APP_CONFIG_GOOGLE_ADS (the
# secrets file, ~/.adapt/google-secrets.yaml), KIND_BIN / KIND_CLUSTER (kind, adapt: the LocalStack and Postgres images
# are loaded from the local docker onto the kind node when they are there; SKIP_KIND_LOAD=1 lets the node pull them),
# PYTHON (a python with PyYAML: orchestration/.venv/bin/python).
#
# The pipeline image itself (adapt-pipeline:local, imagePullPolicy Never) is built and loaded separately:
#   bash orchestration/docker/build.sh && kind load docker-image adapt-pipeline:local --name adapt
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$HERE")"
CONTEXT="${ADAPT_K8S_CONTEXT:-kind-adapt}"
NAMESPACE=adapt   # the namespace of the manifests
SECRET="${ADAPT_K8S_SECRET:-adapt-secrets}"
GOOGLE_SECRETS="${ADAPT_APP_CONFIG_GOOGLE_ADS:-$HOME/.adapt/google-secrets.yaml}"
KIND_BIN="${KIND_BIN:-$(command -v kind || echo /tmp/bin/kind)}"
KIND_CLUSTER="${KIND_CLUSTER:-adapt}"
PYTHON="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"
PIPELINE_IMAGE="${ADAPT_IMAGE:-adapt-pipeline:local}"

k() { kubectl --context "$CONTEXT" "$@"; }
say() { printf '\n== %s\n' "$*"; }

[[ -f "$GOOGLE_SECRETS" ]] || { echo "no secrets file at $GOOGLE_SECRETS" >&2; exit 1; }
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

say "Secrets catalog-postgres-auth and $SECRET (values from $GOOGLE_SECRETS; not printed)"
# Reuse the catalog password the running Postgres was initialised with; generate one the first time.
CATALOG_PASSWORD="$(k -n "$NAMESPACE" get secret catalog-postgres-auth \
  -o go-template='{{index .data "POSTGRES_PASSWORD" | base64decode}}' 2>/dev/null || true)"
[[ -n "$CATALOG_PASSWORD" ]] || CATALOG_PASSWORD="$(openssl rand -hex 24)"
export CATALOG_PASSWORD
"$PYTHON" - "$GOOGLE_SECRETS" "$NAMESPACE" "$SECRET" <<'PY' | k apply --server-side --force-conflicts -f -
import json
import os
import sys

import yaml

path, namespace, name = sys.argv[1:4]
with open(os.path.expanduser(path)) as handle:
    values = yaml.safe_load(handle) or {}
google = ("developer_token", "client_id", "client_secret", "refresh_token")
missing = [key for key in google if not values.get(key)]
if missing:
    sys.exit("%s has no %s" % (path, ", ".join(missing)))
password = os.environ["CATALOG_PASSWORD"]
labels = {"app.kubernetes.io/part-of": "adapt-pipeline"}
adapt = {"ADAPT_SECRET_%s" % key.upper(): str(values[key]) for key in google}
adapt.update(ADAPT_DUCKLAKE_S3_KEY_ID=os.environ.get("ADAPT_K8S_S3_KEY_ID", "test"),
             ADAPT_DUCKLAKE_S3_SECRET=os.environ.get("ADAPT_K8S_S3_SECRET", "test"),
             ADAPT_DUCKLAKE_CATALOG_PASSWORD=password)


def secret(secret_name, data):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": secret_name, "namespace": namespace, "labels": labels}, "stringData": data}


print(json.dumps({"apiVersion": "v1", "kind": "List",
                  "items": [secret("catalog-postgres-auth", {"POSTGRES_PASSWORD": password}), secret(name, adapt)]}))
PY
unset CATALOG_PASSWORD

say "LocalStack S3 and the catalog Postgres"
k apply -f "$HERE/localstack.yaml" -f "$HERE/catalog-postgres.yaml"
k -n "$NAMESPACE" rollout status deploy/catalog-postgres --timeout=300s
k -n "$NAMESPACE" rollout status deploy/localstack --timeout=600s

say "bucket s3://adapt-warehouse"
k -n "$NAMESPACE" exec deploy/localstack -- sh -c \
  'awslocal s3api head-bucket --bucket adapt-warehouse >/dev/null 2>&1 || awslocal s3 mb s3://adapt-warehouse'
k -n "$NAMESPACE" exec deploy/localstack -- awslocal s3 ls

say "catalog database"
k -n "$NAMESPACE" exec deploy/catalog-postgres -- psql -U adapt -d adaptcat -Atc \
  "SELECT 'database ' || current_database() || ' ready for user ' || current_user"

say "resources"
k -n "$NAMESPACE" get pods,svc,secret -o wide
echo
echo "Secret $SECRET keys (no values):"
# shellcheck disable=SC2016  # a go-template, not a shell expansion
k -n "$NAMESPACE" get secret "$SECRET" -o go-template='{{range $key, $_ := .data}}  {{$key}}{{"\n"}}{{end}}'
