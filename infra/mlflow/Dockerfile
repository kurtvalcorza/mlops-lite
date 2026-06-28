FROM python:3.11-slim

# MLflow server with PostgreSQL backend + S3 (MinIO) artifact store.
# 007 US1: MLflow 2.18.0 -> 3.14.0 (same version as the skinny clients). psycopg2/boto3 left at the
# validated minors — 3.14 needs no newer floor and matching boto3 across server+clients avoids skew.
RUN pip install --no-cache-dir \
    mlflow==3.14.0 \
    psycopg2-binary==2.9.10 \
    boto3==1.35.99

EXPOSE 5000
