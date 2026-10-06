#!/usr/bin/env bash
# Start LocalStack S3 and upload the files_demo fixtures to s3://adapt-demo/, so the adapt-s3 connector (and the
# examples/sources/readers/s3_demo source) have a bucket to read. Idempotent: safe to run again. Requires docker and the aws CLI.
#
#   orchestration/localstack/up.sh
#   # then, with the dummy LocalStack credentials in the environment:
#   ADAPT_SECRET_S3_KEY_ID=test ADAPT_SECRET_S3_SECRET=test \
#     adapt run examples/sources/readers/s3_demo --set bucket_root=s3://adapt-demo/ \
#       --stream customers --stream orders --allow-connector s3 --output jsonl:/tmp/s3out
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENDPOINT="${ADAPT_S3_ENDPOINT:-http://localhost:4566}"
BUCKET="${ADAPT_S3_BUCKET:-adapt-demo}"
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1

echo "starting LocalStack (community s3) ..."
docker compose -f "$HERE/docker-compose.yml" up -d

echo "waiting for the S3 service ..."
for i in $(seq 1 60); do
  if curl -s "$ENDPOINT/_localstack/health" 2>/dev/null | grep -qE '"s3": "(available|running)"'; then
    echo "  ready"; break
  fi
  sleep 1
done

echo "creating s3://$BUCKET/ and uploading the files_demo fixtures ..."
aws --endpoint-url "$ENDPOINT" s3 mb "s3://$BUCKET" 2>/dev/null || true
aws --endpoint-url "$ENDPOINT" s3 cp "$REPO/examples/sources/readers/files_demo/data" "s3://$BUCKET/" --recursive >/dev/null
echo "objects in s3://$BUCKET/:"
aws --endpoint-url "$ENDPOINT" s3 ls "s3://$BUCKET/" --recursive
echo
echo "done. Set ADAPT_SECRET_S3_KEY_ID=test ADAPT_SECRET_S3_SECRET=test and run examples/sources/readers/s3_demo (--set bucket_root=s3://$BUCKET/)."
