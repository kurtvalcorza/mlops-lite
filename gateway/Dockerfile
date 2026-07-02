# Build context is the REPO ROOT (docker-compose.yml sets context: . / dockerfile:
# gateway/Dockerfile) so the image is self-contained (Codex round 4, 018): it COPYs BOTH the app
# and the shared platformlib package — a bind mount alone left any run of the built image outside
# compose failing with ModuleNotFoundError: platformlib.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway/app ./app
COPY platformlib ./platformlib

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
