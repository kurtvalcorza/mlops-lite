# Implementation Plan: Model-Quality Monitoring with Ground Truth

**Branch**: `013-quality-monitoring` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/013-quality-monitoring/spec.md` — extend the monitoring stage
from **input-distribution drift** (the existing pure-Python PSI path) to **output / ground-truth quality**.

## Summary

The platform's monitoring today is **input-only**: `gateway/app/monitoring.py` computes PSI over feature
distributions, exports `gateway_drift_score` / `gateway_dataset_drift`, writes drift reports to MinIO
`results`, renders them on Grafana, and `POST /monitor/check` closes the loop via `_launch_retrain`. 013
adds the **output half**: (US1) log served predictions with a prediction id + model version and attach
**delayed** ground-truth labels; (US2) compute **windowed per-modality quality** (accuracy/WER/etc.,
reusing 011's metric libs), export gauges, write quality reports, and **extend** the Grafana dashboard;
(US3) fire the **existing** retrain path on a **quality** breach, defining how it **combines** with the
input-PSI signal. Mostly new code, **no heavy deps**, **no new service/runtime**, GPU/FT stack **frozen**.
Phase-gated like 005/006/007, re-validated against the full prior suite each tier, never regressing.

## Technical Context

**Language/Version**: Python 3.12 (gateway, post-007), reusing existing FastAPI/boto3/prometheus-client.
No new language or runtime. Quality scoring reuses **011's metric libraries** (per-modality
accuracy/F1/WER), kept pure-Python / dependency-light to honor Principle III (the same reason Evidently was
dropped for PSI).

**Primary Dependencies**: none new of weight. Reuses `prometheus_client.Gauge/Counter` (already used by
`monitoring.py` / `monitor.py`), the MinIO/S3 client (`datasets._s3`), `httpx` (the existing trainer call),
pydantic models, and 011's in-tree metric libs. **No Evidently, no pandas/scipy/plotly** (Principle III).

**Storage**: predictions + labels persist in the **existing** stores — MinIO `results` bucket (alongside
`drift/` reports, e.g. under `predictions/` + `labels/` + `quality/` prefixes) and/or the existing Postgres
— **no new datastore**. Records are keyed by `prediction_id` and tagged with `model_version` so the
prediction↔label join and the per-version attribution both hold. *(Prediction-vs-label store split is a
US1 design point; default mirrors the PSI module's `results`-bucket-object approach.)*

**Target Platform**: Win11 + WSL2 + Rancher Desktop. The gateway/MLflow/MinIO/Prometheus/Grafana run in
Docker; training/bento/UI run native in WSL (per the constitution's hybrid-GPU + native-non-GPU
amendments). 013 touches the **gateway** (logging + quality + label ingestion + the quality→retrain
trigger) and the **Grafana** dashboard JSON; no GPU code.

**Performance Goals**: prediction logging MUST be **off the request path** (fire-and-forget, fail-open,
the 006-tracing pattern) so it adds no measurable inference-latency or GPU-lock-hold regression; quality
computation is a periodic/triggered CPU-side aggregation, never synchronous on `/infer`.

**Constraints**: additive (the input-PSI path is unchanged); **no second resident model** (Principle II);
dependency-light (Principle III); loopback/auth/BFF posture unchanged; reuse the existing retrain trigger
and Prometheus/Grafana/MinIO surfaces.

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | All logging/scoring on-host in the existing gateway + MinIO/Postgres; nothing leaves the box | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | Quality is CPU-side aggregation off the request path; **no second resident model**, VRAM mutex untouched | ✅ unchanged |
| III. Lightweight Footprint | **No heavy deps** — pure-Python + 011's metric libs (the Evidently-avoidance pattern); records ride the existing `results` bucket | ✅ |
| IV. Full Lifecycle Coverage | Monitoring stage already present; 013 **completes** it (input *and* output drift), no stage added/dropped | ✅ strengthened |
| V. OSS & Swappable | Same OSS surfaces (MinIO/Prometheus/Grafana/MLflow versions); quality lib swappable behind a clear interface, like PSI behind `monitoring.py` | ✅ |
| VI. Reproducibility & Observability | **Advanced** — adds output/ground-truth quality tracking + gauges + reports on top of input-drift; every quality verdict is a stored, attributable report | ✅ advanced |
| VII. Phase-Gated Delivery | Three independently-runnable stories (US1 capture, US2 compute, US3 trigger), each re-validated on the target machine | ✅ |
| Workflow: "no new runtime without amendment" | None introduced (Python gateway + existing MinIO/Postgres/Prometheus/Grafana) | ✅ no amendment |

**No amendment required.** 013 advances Principle VI (and completes Principle IV's feedback loop on the
output side) within the existing constitution; the GPU freeze + one-model-in-VRAM keep Principle II
untouched; dependency-light honors Principle III. Clean gate-check, mirroring 005/006/007.

> **Constitution version note**: gated against the constitution as it stands (**v1.3.0** on disk, last
> amended 2026-06-28; the roadmap references a v1.4.0 line). 013 needs **no amendment** under either — it
> introduces no new runtime, service, or stage; it advances Principle VI. If a v1.4.0 ratification predates
> implementation, re-run this gate (expected: still clean).

## Project Structure

### Source Code (delta over 011/the current monitoring stack)

```text
mlops-lite/
├── gateway/
│   └── app/
│       ├── quality.py             # NEW: prediction/label logging + windowed quality compute
│       │                          #      (the PSI-module sibling: pure-Python, results-bucket reports,
│       │                          #       Prometheus gauges); reuses 011's metric libs
│       ├── monitoring.py          # UNCHANGED: input-PSI drift stays exactly as is
│       ├── routers/
│       │   ├── monitor.py         # MODIFIED: add label ingestion + quality check/report endpoints;
│       │   │                      #           combine policy at the quality→retrain trigger (reuse
│       │   │                      #           `_launch_retrain`); input-PSI `/monitor/check` unchanged
│       │   ├── infer.py           # MODIFIED: fire-and-forget prediction logging (off request path)
│       │   ├── stream.py          # MODIFIED: prediction logging for the SSE path (frame-safe)
│       │   └── vision.py          # MODIFIED: prediction logging for /vision/classify
│       └── (reuse) datasets._s3, tracing.py pattern (bg worker / fail-open / lazy init)
├── infra/grafana/provisioning/dashboards/mlops-lite.json  # MODIFIED: add model-quality panel(s)
│                                                          #           beside the input-drift panels
└── tests/                         # NEW: quality-logging / label-join / windowed-metric / quality-breach /
                                   #      combine-policy + a no-regression sweep of the input-PSI path
```

**Structure Decision**: keep quality in its **own module** (`quality.py`) as a sibling of `monitoring.py`
so the **input-drift path is provably untouched** and a regression bisects cleanly to either the input or
the output half. Prediction logging reuses the **006 tracing pattern** (background worker, lazy init,
fail-open, span/record outside the GPU lock) so it stays off the request path. The retrain trigger reuses
`_launch_retrain`; only the threshold + combine policy are new.

## Phasing (maps to constitution VII)

- **Phase 0 — Pre-flight**: confirm 011's metric libs are importable as a library and cover the needed
  modalities (classification accuracy/F1, ASR-style WER); confirm the MinIO `results` bucket + the existing
  Postgres are reachable for the new prediction/label records; pick the prediction/label store layout
  (default: `results` bucket prefixes, mirroring `drift/`). No GPU touched.
- **Phase 1 — Prediction + label logging (US1, P1)**: add `quality.py` logging (prediction id + model
  version, fire-and-forget, fail-open) wired into `infer.py` / `stream.py` / `vision.py`; add the
  label-ingestion path (`POST /monitor/labels` + a thin batch client looping it) into `monitor.py`; persist
  to the existing stores keyed by prediction id. Exit: SC-075 + SC-076.
- **Phase 2 — Windowed quality compute (US2, P1)**: join predictions↔labels over a **sliding count-based
  window** (last N labeled pairs, N configurable), recomputed on demand via `POST /monitor/quality/check`
  (+ optional periodic); compute per-modality quality via 011's libs (labeled pairs only; thin window ⇒
  *insufficient data*), export Prometheus gauges (by model version/modality), write quality reports to
  `results`, and **extend** the Grafana dashboard with a model-quality panel. Exit: SC-077 + SC-078.
- **Phase 3 — Quality-breach → retrain + combine policy (US3, P2)**: on a window whose metric drops more
  than a configurable X% below the 011 registered/eval baseline, fire the existing `_launch_retrain`
  (counter labeled *quality*, fail-soft), and implement the **combine policy** with the input-PSI breach
  (**OR + configurable cooldown/debounce**), keeping each signal independently observable. Exit: SC-079.
- **Cross-cutting — No-regression sweep**: the input-PSI drift path + its retrain, the six UI tabs, SSE
  framing, the loopback/auth/BFF contract, and the one-model-in-VRAM mutex all unchanged; logging adds no
  resident model and no measurable latency/GPU-lock regression. Exit: SC-080.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Separate `quality.py` sibling of `monitoring.py` | Keeps the input-PSI path provably untouched; a regression bisects to input vs output half | Folding quality into `monitoring.py` muddies the two signals and risks regressing the working PSI path |
| Fire-and-forget, fail-open prediction logging (006 pattern) | Logging MUST NOT regress inference latency or the GPU-lock hold; serving must survive a logging-store outage | Synchronous logging on the request path would tax every `/infer` and couple serving to the results store |
| Reuse 011's metric libs, no Evidently | Principle III — Evidently was deliberately removed for PSI; per-modality metrics already exist in-tree | A heavy eval dep re-opens the exact footprint hole 011/PSI closed, on the constrained C: drive |
| Records in the existing `results`/Postgres | No new datastore; mirrors the PSI module's `results`-bucket reports | A dedicated label DB/service adds idle RAM + a new component against Principle III |
| Reuse `_launch_retrain` for the quality breach | One retrain mechanism, one trainer-busy guard; the quality signal is just a second trigger source | A parallel retrain path duplicates the trigger and risks uncoordinated double-retrains |
| Define an explicit input-PSI × quality combine policy | Two complementary breach signals must not double-fire retrains; operators must see *why* one fired | Letting both fire independently invites redundant retrains and an unobservable decision |

> **Grilled decisions (2026-06-28)** — carried from spec.md, resolved (013 build-ready): (a) label
> ingestion = `POST /monitor/labels` endpoint + a thin batch client looping it (mirroring
> `data/register_dataset.py`); (b) windowing = **sliding count-based** (last N labeled pairs, N
> configurable), on-demand recompute via `POST /monitor/quality/check` (+ optional periodic); (c) threshold
> = **relative-to-baseline** (windowed metric > configurable X% below the 011 registered/eval baseline,
> per-modality metrics reuse 011's libs); (d) combine policy = **OR + cooldown** (either input-PSI or
> quality breach fires `_launch_retrain`, configurable debounce to prevent retrain storms), each signal
> independently observable.
