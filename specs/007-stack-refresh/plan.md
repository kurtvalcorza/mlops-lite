# Implementation Plan: Stack Refresh & MLflow 3.x Upgrade

**Branch**: `007-stack-refresh` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/007-stack-refresh/spec.md` (the deferred MLflow 3.x upgrade
+ a stack-wide version refresh)

## Summary

A coordinated, behavior-preserving version refresh: (US1) move MLflow 2.18→3.x across the server + all
skinny clients with a fresh-volume backend reset + re-seed (prior run/trace history dropped — accepted;
datasets persist on MinIO), porting 006 tracing to the non-deprecated 3.x API; (US2) pin the four floating `:latest` infra images; (US3) refresh the gateway's
pure-Python deps + Python base 3.11→3.12; (US4) bump the UI on the Next 15 line; (US5) bump the safe
native CPU deps. The **Blackwell GPU/FT stack is frozen**. Phase-gated like 002/004/005/006, validated
against the full 001–006 suite each tier, never regressing.

## Technical Context

**Language/Version**: Python 3.11→**3.12** (gateway image), Node 20+ (UI, unchanged floor), YAML
(Compose). No new language or runtime.

**Primary Dependencies (verified latest, 2026-06-28)**: MLflow `2.18.0`→`3.14.0` (server +
`mlflow-skinny`, both exist on PyPI); FastAPI `0.115.6`→`0.138.1`; uvicorn `0.34.0`→`0.49.0`; pydantic
→`2.13.4`; boto3 `1.35.99`→`1.43.36`; prometheus-client `0.21.1`→`0.25.0`; httpx `0.28.1` (already
current). UI: Next `15.5.19`→latest **15.x** (NOT 16.2.9); React `19.0.0`→`19.2.x`. Native CPU: BentoML
`1.4.39`→current, Pillow `11.0.0`→current, Prefect `3.1.4`→current. **Frozen**: torch `2.11.0+cu128`,
torchvision `0.26.0+cu128`, transformers `4.46.3`, peft `0.13.2`, accelerate `1.1.1`, datasets `3.1.0`.

**Tracing-API note (FR-057)**: 3.14 keeps `MlflowClient.start_trace`/`end_trace` but the server marks
the v2 trace-start path deprecated (`_deprecated_start_trace_v2`); the idiomatic 3.x path is
`mlflow.start_span_no_context(start_time_ns=…)`. 007 ports 006 onto it (the API we deliberately avoided
under 2.18 because it didn't exist there).

**Storage**: the Postgres backend store is **reset to a fresh volume** (grilled — no `mlflow db
upgrade`); MLflow run/trace history is dropped (accepted), datasets persist on MinIO (content-addressed,
not Postgres). Re-seed the serving LLM (register + promote `@serving`) + vision after bring-up.

**Target Platform**: Win11 + WSL2 + Rancher Desktop. The gateway/MLflow run in Docker; training/bento/UI
run native in WSL. The frozen torch stack lives in the `~/mlops-train` venv (US3/US5 must not perturb it).

**Project Type**: dependency/version refresh over 002/004/005/006 — touches `infra/mlflow/Dockerfile`,
`docker-compose.yml`, `gateway/{Dockerfile,requirements.txt}`, `gateway/app/tracing.py`,
`training/requirements.txt`, `serving/bento/requirements.txt`, `scripts/native_env.lock`,
`ui/package.json` + `ui/package-lock.json`, and the tests for re-validation. No app logic changes
besides the 006 tracing port.

**Performance Goals**: none targeted; the refresh must not regress inference latency or the GPU-lock
hold time.

**Constraints**: behavior-preserving (no lifecycle/UI/API change); GPU stack frozen; MLflow run/trace
history not retained (fresh-volume reset + re-seed, grilled); single MLflow version across server +
clients; loopback/auth/BFF posture unchanged.

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Version bumps only; nothing leaves the host | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | GPU/FT stack **frozen**; no change to the VRAM mutex or torch | ✅ unchanged |
| III. Lightweight Footprint | No new service/runtime; Python 3.12 is the same runtime family; pinned images are the same images | ✅ |
| IV. Full Lifecycle Coverage | No stage added/dropped | ✅ N/A |
| V. OSS & Swappable | Same OSS components, newer versions; MLflow stays the registry/tracking | ✅ |
| VI. Reproducibility & Observability | **Strengthened** — pinning `:latest` images removes a reproducibility hole; MLflow backend reset to a fresh volume + re-seeded (run/trace history not retained, accepted; datasets intact on MinIO) | ✅ strengthened |
| VII. Phase-Gated Delivery | Five independently-runnable stories (US1–US5), each re-validated on the target machine | ✅ |
| Workflow: "no new runtime without amendment" | None introduced (Python/Node/MLflow all pre-existing) | ✅ no amendment |

**No amendment required.** 007 refreshes versions within the existing constitution and advances
Principle VI; the GPU freeze keeps Principle II untouched. Clean gate-check, mirroring 005/006.

## Project Structure

### Source Code (delta over 006)

```text
mlops-lite/
├── docker-compose.yml            # MODIFIED: pin minio/mc/prometheus/grafana (+ postgres minor); 3.x mlflow via build
├── infra/mlflow/Dockerfile       # MODIFIED: mlflow==3.x (+ psycopg2/boto3 minors); python base pin
├── gateway/
│   ├── Dockerfile                # MODIFIED: python:3.12-slim
│   ├── requirements.txt          # MODIFIED: mlflow-skinny==3.x, fastapi/uvicorn/pydantic/boto3/prom-client bumps
│   └── app/tracing.py            # MODIFIED: port to mlflow.start_span_no_context (3.x), behavior identical (FR-057)
├── training/requirements.txt     # MODIFIED: mlflow-skinny==3.x; prefect bump; torch-family LEFT FROZEN (cu128 index)
├── serving/bento/requirements.txt# MODIFIED: bentoml + pillow bumps (CPU only)
├── scripts/
│   ├── native_env.lock           # MODIFIED: record the refreshed native pins; torch family unchanged
│   ├── bootstrap.sh              # MODIFIED: always-apply requirements (|| fail-guarded) + UI lockfile-freshness re-install
│   ├── up_all.ps1               # MODIFIED: fail-fast hint -> reset_mlflow_3x.ps1 when MLflow isn't healthy
│   ├── reset_mlflow_3x.ps1      # NEW: guarded one-time 2.18->3.x backend reset (drop pgdata + up_all + reseed)
│   └── reseed_registry.sh        # NEW: re-register+promote serving LLM + re-register vision after a reset
├── ui/package.json + package-lock.json  # MODIFIED: Next 15.x latest + React 19 patch + tooling bumps
└── tests/                        # re-run; add a tiny version-assertion smoke if useful (e.g. server reports 3.x)
```

**Structure Decision**: keep each tier in its own files so a regression bisects to a tier. The only
*code* change is the 006 tracing port (`tracing.py`, FR-057); everything else is manifests/images.

## Phasing (maps to constitution VII)

- **Phase 0 — Pre-flight**: confirm `mlflow==3.14.0` / `mlflow-skinny==3.14.0` install clean in the
  gateway + mlflow images; capture the current validated image versions for US2 to pin. (No pgdata
  snapshot — fresh-volume reset, grilled.)
- **Phase 1 — MLflow 3.x (US1)**: bump server + clients to 3.14.0; **fresh `pgdata` volume** + re-seed
  (serving LLM register/promote + vision); port 006 tracing to `start_span_no_context` (FR-057);
  re-validate registry/datasets/training/drift/tracing on the clean 3.x server. Exit: SC-036 + SC-038.
- **Phase 2 — Pin images (US2)**: replace the four `:latest` tags (+ postgres minor) with validated
  pins; clean `up_all`; foundation + exposure smoke. Exit: SC-037.
- **Phase 3 — Gateway Python refresh (US3)**: `python:3.12-slim` + dep bumps; gateway suite green;
  OpenAPI/metrics unchanged. Exit: SC-039.
- **Phase 4 — UI refresh (US4)**: Next 15.x + React patch + tooling; `npm ci` + build + ui bounce; UI
  tests green. Exit: SC-040.
- **Phase 5 — Safe native bumps (US5)**: BentoML/Pillow/Prefect; vision + FT + drift green on the frozen
  torch stack. Exit: SC-041 (with the cross-cutting sweep).

Cross-cutting: a full 001–006 no-regression sweep after the last tier, plus a check that the GPU-lock
hold time and inference latency are unchanged.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Fresh `pgdata` volume + re-seed (grilled) | Zero schema-migration risk + sidesteps the Postgres-password-rotation gotcha; Kurt accepts losing MLflow run/trace history; datasets persist on MinIO; re-seed restores registry resolution | In-place `mlflow db upgrade` preserves history but adds a 2.18→3.x schema-migration risk for history that's mostly local test data — not worth it here |
| Port 006 to `start_span_no_context` (3.x) | 3.x deprecates the v2 `start_trace` path the gateway used under 2.18; building on a deprecated shim invites future breakage | Keeping the deprecated path "works for now" but leaves the gateway on an API MLflow is sunsetting |
| Single MLflow version, server + all clients | A skinny client and server on different majors is unsupported and silently corrupts behavior | Upgrading the server but not a client (or vice-versa) is a version-skew time bomb |
| Freeze the Blackwell GPU/FT stack | torch/transformers were hard-won on sm_120 + cu128; bumping risks breaking GPU inference and the LoRA→GGUF pipeline | A "bump everything" refresh would churn the most fragile, highest-risk part of the stack for no 007 benefit |
| Stay on Next 15.x (not 16) | The validated UI line; a patch bump is near-zero risk | Next 16 is a major with App-Router changes — a UI-regression risk disproportionate to a refresh |
| Pin images to validated versions (not `:latest`) | Reproducibility (Principle VI); a rebuild must pull the same stack | `:latest` is "less to write" but silently non-reproducible — the exact hole 007 closes |
