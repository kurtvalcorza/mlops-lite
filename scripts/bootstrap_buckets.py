"""Reference bucket bootstrap (T008) using boto3 — equivalent to the compose `createbuckets`
service. Useful when running MLflow/clients outside compose.

Usage: MINIO_ENDPOINT=http://localhost:9000 python scripts/bootstrap_buckets.py
"""
import os

import boto3
from botocore.client import Config

ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
# Credentials from the environment — no hardcoded default (FR-017). Source .env / run gen_secrets.
ACCESS = os.environ["MINIO_ROOT_USER"]
SECRET = os.environ["MINIO_ROOT_PASSWORD"]
BUCKETS = ["datasets", "models", "results", "mlflow"]


def main() -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    for bucket in BUCKETS:
        if bucket in existing:
            print(f"= {bucket} (exists)")
        else:
            s3.create_bucket(Bucket=bucket)
            print(f"+ {bucket} (created)")


if __name__ == "__main__":
    main()
