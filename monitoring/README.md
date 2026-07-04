# Monitoring & the drift → retrain loop (US5)

Closes the lifecycle: detect distribution drift between a reference and a live dataset, surface it,
and — on breach — automatically launch a retraining run.

## Why PSI instead of Evidently

The plan names Evidently. Evidently pulls pandas + scipy + plotly, and that weight lands in the
**gateway image**, which lives on the constrained Windows C: drive (the real ~50 GB limit) — against
the **Lightweight Footprint** principle. **OSS & Swappable** (Principle V) lets us compute the same
signal dependency-free with the **Population Stability Index (PSI)**. Swapping Evidently back in only
touches [`gateway/app/monitoring.py`](../gateway/app/monitoring.py).

PSI per numeric feature (reference deciles as bins):

| PSI | Interpretation |
|-----|----------------|
| < 0.10 | no significant shift |
| 0.10–0.25 | moderate shift |
| > 0.25 | significant drift (default threshold) |

Dataset drift is declared when any feature crosses the threshold.

## The loop

```
reference dataset  ─┐
                    ├─► POST /monitor/check ─► PSI per feature ─► report (MinIO results bucket)
current dataset    ─┘                                              │
                                                                   ▼  (if drift & retrain spec)
                              POST /train (host agent) ◄── drift breach
```

- Reports are stored in the MinIO `results` bucket and listed via `GET /monitor`.
- `max_psi` and a `dataset_drift` flag are exported as Prometheus gauges
  (`gateway_drift_score`, `gateway_dataset_drift`) and visualized in Grafana
  ([`infra/grafana/provisioning/dashboards/`](../infra/grafana/provisioning/dashboards/)).

> **Since 018:** the drift/quality breach → retrain path above is the *mechanism*; the closed
> **policy loop** (018 US3) is the *orchestration*. Declarative per-model policies (API + UI editor,
> `POST/GET/PUT/DELETE /policies`) run these checks on a schedule via a gateway-lifespan task, launch
> the modality-correct retrain on the latest dataset (the byte-compatible `/train` now served by the
> **GPU host agent**), gate it (011), and promote per the policy's mode (`manual` / `suggest` /
> `auto-on-green`, consuming 016 shadow verdicts). Retrain launches are idempotent (019).
>
> **Storage (018 US4, in progress):** the high-churn monitoring state (predictions, labels, captured
> inputs) is migrating off O(N) MinIO object scans onto the resident `gateway` Postgres DB —
> `platformlib.store`'s relational client + schema landed in T373; the write-path cutovers follow in
> T374–T376. Until they land, predictions/labels/reports are still written to the MinIO `results` bucket
> as described above.

## Use

```bash
# register a reference + a (shifted) current dataset, then:
python monitoring/drift.py --ref baseline@<ver> --cur live@<ver> \
    --retrain qa-demo@<ver>:qwen0_5b-qa-lora        # breach → launches training

curl localhost:8080/monitor                          # recent reports
```

Implementation: [`gateway/app/monitoring.py`](../gateway/app/monitoring.py) (drift + storage) and
[`gateway/app/routers/monitor.py`](../gateway/app/routers/monitor.py) (API + retrain trigger).

## Output-side: quality monitoring with ground truth (013)

PSI above is **input-distribution** drift only — it can't see whether the model's *predictions* are
getting worse. 013 adds the **output** half (concept drift), in the same dependency-light style and
reusing the same retrain trigger:

- Every served prediction is logged fire-and-forget / fail-open with a stable prediction id + the
  resolved model version (wired into the `infer` / `stream` / `vision` routes).
- `POST /monitor/labels` attaches **(usually delayed)** ground-truth labels, keyed by prediction id
  (thin client: [`data/submit_labels.py`](../data/submit_labels.py)).
- `POST /monitor/quality/check` computes a per-modality quality metric over a time window (reusing
  011's metric libs), compares it to a **like-for-like baseline**, and on a breach fires the **same**
  `_launch_retrain` trigger as input-drift (OR-combined, with a cooldown). `GET /monitor/quality`
  lists reports; the Grafana dashboard carries the quality series alongside `gateway_drift_score`.

Pure-Python + the 011 metric libs; `monitoring.py` (PSI) is untouched and quality is CPU-side
aggregation off the request path — never a second resident model (Principle II). Implementation:
[`gateway/app/quality.py`](../gateway/app/quality.py).
