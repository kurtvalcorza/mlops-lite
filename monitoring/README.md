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

## Local alerts + runbooks (023 US7 — FR-322/323)

Prometheus loads [`infra/prometheus/rules/mlops-lite.yml`](../infra/prometheus/rules/mlops-lite.yml)
(mounted read-only by Compose). Alerts fire **locally** — the Prometheus UI (`:9090/alerts`) and
Grafana show them; there is deliberately no Alertmanager or notification service (Principle III).
Each alert links back to its remediation section below.

### alert-wedgedengine
An engine child survived SIGKILL and is pinning the GPU (`hostagent_wedged == 1`). Remediate: find
the child (`nvidia-smi`), kill it from the host, then `POST /control/unload` for the engine (or
restart the agent — jobs get marked interrupted at next start, FR-173).

### alert-prolongedgpuhold
One tenant has held the single GPU slot > 45 min (`hostagent_slot_held`). A long fine-tune/HPO run
is legitimate — check `/jobs` first. A serving tenant holding this long with no traffic suggests a
stuck drain: `POST /control/unload` with a drain timeout.

### alert-activationdegraded
An LLM activation ended `degraded` (`gateway_activation_outcomes_total{outcome="degraded"}`):
desired and resident may diverge. Read `GET /serving/llm/activation` (also on the serving page):
`pointer_error` → re-promote or reset the pointer; `verify_failed` → the registry alias moved
mid-activation, re-promote the intended version; `unreachable` → bring the agent up, then
re-promote (the reconciler retries non-terminal states automatically; degraded waits for you).

### alert-migrationfailed
Gateway schema migration failed (`mlops_migrations_outcomes_total{outcome="error"}`); the gateway
fails readiness until resolved. Diagnose with `python scripts/migrate_db.py status`; checksum
mismatch/newer-schema verdicts mean repository↔database drift — restore from the pre-upgrade
`pg_dump` (see the script header) and forward-fix. Never hand-edit the ledger.

### alert-repeatedschedulerfailures
The policy scheduler keeps erroring (`gateway_policy_checks_total{result="error"}`). Check gateway
logs; typical causes are an unreachable store/MLflow. The loop is fail-open per policy — fix the
dependency and the next tick recovers.

### alert-agentsaturated
The agent is rejecting at its bounds (`hostagent_requests_rejected_total`: saturated/queue_timeout/
too_large). Sustained saturation on a single-operator box usually means a runaway client loop —
find it; the bounds themselves are env-tunable (AGENT_MAX_WORKERS/AGENT_QUEUE_SIZE/…) if the
workload legitimately grew.

### alert-lowdiskspace
Under 20 GB free on the volume backing the agent state dir (`hostagent_disk_free_gb`). Clear old
GGUFs/artifacts (`scripts/disk_report.sh` helps), or grow the volume — model loads and fine-tune
outputs fail unpredictably below this.

### alert-servingstoreunavailable
The gateway cannot reach the host agent (`mlops_serving_up`/`mlops_trainer_up` == 0): serving AND
jobs are down. Start/restart the agent (`bash hostagent/run.sh`, or `scripts/up_all.ps1`); check
`~/.mlops-lite/` agent logs for the refusal reason (a missing AGENT_API_KEY refuses startup by
design, FR-284).

### alert-agentscrapedown
Prometheus's DIRECT agent scrape fails (`up{job="hostagent"} == 0`) while the gateway may still
reach it — usually a stale WSL IP in `infra/prometheus/targets/hostagent.json`. Re-run
`scripts/up_all.ps1` (it rewrites the target file; Prometheus hot-reloads).

### alert-jobsinterruptedbyrestart
An agent restart marked queued/running jobs interrupted (`hostagent_jobs_interrupted_total`,
FR-173). List them (`GET /jobs`, state=interrupted) and resubmit what still matters.
