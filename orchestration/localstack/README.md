# LocalStack S3 for adapt-s3

Test the `adapt-s3` connector against a real S3 API locally, with no AWS account. LocalStack also backs
`ADAPT_EXECUTION=docker` runs that write a DuckLake warehouse to S3 (the `k8s` mode deploys its own in-cluster
LocalStack under [../k8s](../k8s)).

## Why
The `adapt-s3` connector reads objects over DuckDB's `httpfs`. LocalStack serves an S3-compatible endpoint, so the same
connector — with `endpoint`, `url_style: path` and `use_ssl: false` in its `auth` — reads from a local bucket exactly as
it would from AWS. The endpoint is set in the source's `auth`, never in the object URL (a URL with a `?` is refused,
so a path cannot redirect the read or leak credentials).

## Start it and fill the bucket
```bash
orchestration/localstack/up.sh      # starts LocalStack, makes s3://adapt-demo/, uploads the files_demo fixtures
```
This uses the community image `localstack/localstack:3` (no license needed) and only the S3 service. The dummy
credentials are `test` / `test`.

## Run a source against it
`examples/sources/readers/s3_demo` targets real AWS. To read the uploaded fixtures from LocalStack instead, add these three
lines to its `auth:` (the adapt-s3 connector supports them):

```yaml
  endpoint: localhost:4566   # LocalStack
  url_style: path
  use_ssl: false
```
then:
```bash
export ADAPT_SECRET_S3_KEY_ID=test ADAPT_SECRET_S3_SECRET=test
adapt run examples/sources/readers/s3_demo --set bucket_root=s3://adapt-demo/ \
  --stream customers --stream orders --allow-connector s3 --output jsonl:/tmp/s3out
# customers (3, from customers/2026-10-01.jsonl) and orders (one row per region CSV)
```
The `events` stream needs Parquet under `events/day=.../part-*.parquet`, which the files_demo fixtures do not include;
point `bucket_root` at your own bucket to read it.

## Stop it
```bash
docker compose -f orchestration/localstack/docker-compose.yml down
```

## MinIO / real AWS
With the three `auth` lines above, point `endpoint` at MinIO (`localhost:9000`) instead of LocalStack; for real AWS S3
drop them again and pass real credentials. On EKS, prefer the instance/IRSA credential chain once the connector
supports it (a planned enhancement) so no static keys are needed.

Other AWS services: LocalStack community also emulates SQS, SNS, DynamoDB, etc.; this project only needs S3. Google
Cloud Storage has no LocalStack equivalent — use `fake-gcs-server` for the `adapt-gcs` connector.
