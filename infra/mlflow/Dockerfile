FROM python:3.11-slim

# MLflow server with PostgreSQL backend + S3 (MinIO) artifact store.
RUN pip install --no-cache-dir \
    mlflow==2.18.0 \
    psycopg2-binary==2.9.10 \
    boto3==1.35.99

EXPOSE 5000
