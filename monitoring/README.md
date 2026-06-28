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
                                          POST trainer /train  ◄── drift breach
```

- Reports are stored in the MinIO `results` bucket and listed via `GET /monitor`.
- `max_psi` and a `dataset_drift` flag are exported as Prometheus gauges
  (`gateway_drift_score`, `gateway_dataset_drift`) and visualized in Grafana
  ([`infra/grafana/provisioning/dashboards/`](../infra/grafana/provisioning/dashboards/)).

## Use

```bash
# register a reference + a (shifted) current dataset, then:
python monitoring/drift.py --ref baseline@<ver> --cur live@<ver> \
    --retrain qa-demo@<ver>:qwen0_5b-qa-lora        # breach → launches training

curl localhost:8080/monitor                          # recent reports
```

Implementation: [`gateway/app/monitoring.py`](../gateway/app/monitoring.py) (drift + storage) and
[`gateway/app/routers/monitor.py`](../gateway/app/routers/monitor.py) (API + retrain trigger).
