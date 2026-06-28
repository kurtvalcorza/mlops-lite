# Data versioning (US3)

Named, versioned, **immutable** dataset references backed by the MinIO `datasets` bucket.

## Why not DVC

The plan names DVC as the default. In practice DVC needs a git repo, the `dvc` CLI, and a
git-commit per version — an awkward fit for a container-internal, API-driven flow, and weight
that works against the constitution's **Lightweight Footprint** principle. **OSS & Swappable**
(Principle V) lets us deliver the same guarantees more simply via **content addressing**, and
swap back to DVC later behind the same gateway interface if a git-native workflow is wanted.

## Model

A dataset version **is the sha256 of its bytes**:

```
s3://datasets/<name>/<version>/data            # immutable content (version = sha256[:12])
s3://datasets/<name>/<version>/manifest.json   # name, version, size, sha256, format, metadata
```

- **Immutable**: a version key never changes; editing content yields a *new* version.
- **Idempotent**: re-registering identical bytes returns the existing version (no duplicate).
- **Content-addressed**: the version is reproducible from the data alone.

## Use

Via the gateway API:

```bash
# register (content base64 in the JSON body)
curl -X POST localhost:8080/datasets -H 'content-type: application/json' \
  -d '{"name":"iris","content_b64":"'"$(base64 -w0 iris.csv)"'","format":"csv"}'

curl localhost:8080/datasets                 # list all datasets + versions
curl localhost:8080/datasets/iris            # all versions of one dataset
curl localhost:8080/datasets/iris/<version>  # manifest + presigned download URL
```

Or the helper (thin client over the same endpoint):

```bash
python data/register_dataset.py iris ./iris.csv --format csv
```

Implementation: [`gateway/app/datasets.py`](../gateway/app/datasets.py) (storage) and
[`gateway/app/routers/datasets.py`](../gateway/app/routers/datasets.py) (API).
