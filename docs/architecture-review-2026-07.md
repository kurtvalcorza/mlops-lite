# MLOps-Lite — Architecture Review (2026-07)

**Scope**: full-repo design evaluation at `b999020` (post-017, constitution v1.4.1).
**Method**: subsystem-by-subsystem read of the gateway, serving layer + GPU lease, training/
orchestration/monitoring, UI/BFF, infra, and test suite, checked against the constitution and the
001–017 spec history. Findings carry `file:line` references. Recommendations are framed the way
this repo works: small correctness fixes first, then architecture revisions, then candidate
spec-kit increments (018+).

---

## 1. Verdict

The architecture is in unusually good shape for its size. The single-GPU-tenant lease is genuinely
well engineered, the module discipline (pure core + injected I/O) makes a 218-test suite possible
on a platform whose core resource can't exist in CI, and the spec history shows deferred debts
(008→017 swap, 011→016 shadow-replay) being tracked and actually closed.

The two structural problems worth architectural attention are:

1. **Principle IV is not yet true.** The lifecycle loop is *half-open*: nothing schedules the
   drift/quality checks, a triggered retrain can only launch an **LLM** run regardless of which
   modality breached, input-drift detection can't read any dataset format the trainable modalities
   use, and shadow-replay verdicts are consumed by nothing. "Data → train → serve → monitor →
   retrain" currently requires a human at three of the arrows.
2. **Duplication has started to diverge in correctness, not just style.** The whisper supervisor
   gained a stuck-child recovery path the llama supervisor still lacks; the swap broker and the
   gateway keep separate copies of tenant→URL maps; four flows carry four copies of the scoring
   shim. In a codebase whose central invariant (one GPU tenant) is enforced by *protocol
   convention across processes*, copy-drift is the most likely source of the next co-residency or
   deadlock bug.

Everything else below is either a targeted fix or an opportunity, not a redesign. The constitution
itself has held up well; nothing here requires an amendment except (optionally) the auto-promotion
policy in §6.3.

---

## 2. Architecture snapshot (as built)

- **Control plane (Docker Compose, loopback-bound)**: FastAPI gateway (:8080) — stateless
  proxy/orchestrator with no durable local state; MLflow 3.14 (:5500) on Postgres + MinIO;
  Prometheus (:9090) + Grafana (:3001). Deliberately frugal dependency set — all metrics,
  validation, PSI drift, and eval scoring are hand-rolled pure Python (`gateway/requirements.txt`).
- **Data plane (native WSL daemons, supervised by `supervisor/supervise.py`)**: llama.cpp
  supervisor (:8090/:8081), trainer daemon (:8091), BentoML vision (:8092, GPU),
  embeddings (:8093, CPU/off-lease), tabular (:8094, CPU/off-lease), whisper.cpp supervisor
  (:8095/:8082, opt-in), Next.js operator UI (:3000).
- **The GPU lease** (`serving/gpu_lease.py`): one lockfile + flock sidecar shared by four tenants
  (`llm-serving`, `asr`, `vision`, `training`). Atomic `O_CREAT|O_EXCL` claim inside a flock'd
  read-decide-write window; liveness by PID + `/proc` start-time (defeats PID recycling); a
  `vram_pid` field tracks the actual model subprocess so supervisor crashes don't free VRAM the
  orphaned child still holds; live `nvidia-smi` admission with a static-budget fallback. No TTL by
  design — training is never evicted by a timer.
- **Lifecycle mechanics**: content-addressed dataset registry on MinIO (DVC consciously swapped
  out, `gateway/app/datasets.py:1-17`); subprocess-per-run training under an HTTP daemon (Prefect
  is an optional decorator shim, not a scheduler); score-at-registration (015) feeding the
  promotion gate, champion-challenger compare, and the HPO objective without re-scoring; alias-based
  promotion through a single gated choke point (`registry.py:104`); advisory shadow-replay over
  captured real traffic (016); operator-confirmed preemptive swap (017).

## 3. What is working well (keep these)

1. **The lease protocol** — belt-and-suspenders atomicity, PID-recycling defense, vram-owner
   tracking, acquire-time self-heal, and stale-tolerant read paths kept strictly display-only
   (`gpu_lease.py:291-302`). This is the platform's crown jewel and it reads like it.
2. **Pure-core + injected-I/O module shape** (`swap.py`, `shadow.py`, `batch.py`,
   `evaluation.py`, `validation.py`) — every orchestration seam (`state_fn`, `http_post`,
   `sleep`, `batch_active_fn`) is injectable, which is why 017's preemption logic is unit-tested
   despite needing three daemons and a GPU in real life.
3. **Registry-tag-driven modality surface** — serving targets, UI panels, benchmarks, metrics,
   and scorers all key off `task`/`serving_engine` tags, so a new modality is mostly additive.
4. **Fail-open observability writes with bounded backpressure** — tracing and prediction logging
   never block or break the serving path (`tracing.py:57`, `quality.py` `_log_sem`).
5. **The BFF security layering** — origin guard → allowlist → key injection → header strip, in
   that order, with the allowlist as an explicit capability manifest (`ui/lib/gw-allowlist.ts`).
6. **Test-suite shape** — offline/live split via skip-guard fixtures (`tests/conftest.py`) keeps
   one suite honest across CI (no GPU) and the target machine (full stack).
7. **Constitution + hardware profile as the single machine-coupling point** — retargeting story
   is real, not aspirational.
8. **Spec discipline** — deferred items are named, tracked, and have actually been closed
   (008's A2 → 017; 011's shadow-replay → 016).

---

## 4. Findings

Ordered by architectural weight, not severity of any single line.

### 4.1 The feedback loop is half-open (Principle IV)

The loop as implemented: drift/quality **check** (manual trigger) → breach → retrain launch →
train → register → score-at-registration (automatic) → **stop**. Specific gaps:

- **No scheduler.** `/monitor/check` and `/monitor/quality/check` only run when a human or an
  external caller invokes them (`routers/monitor.py:58`, `:120`). There is no cron/timer anywhere
  in-repo. The README's "automatically launch a retraining run" is conditional on an out-of-band
  trigger.
- **Retrain is LLM-only.** `RetrainSpec` has no `modality` field (`routers/monitor.py:34-39`) and
  the trainer defaults absent modality to `"llm"` (`training/flow_dispatch.py:26`). A vision or
  ASR quality breach launches an *LLM* fine-tune. The UI hardcodes LLM-shaped retrain params
  (`ui/app/monitor/page.tsx:60-67`).
- **Retrain trains on pinned data.** The spec pins a fixed `dataset_version`, so the triggered
  run trains on the same bytes every time — the drifted distribution never feeds back in unless a
  caller rewrites the spec.
- **Input-drift detection can't see the real datasets.** PSI reads numeric CSV only
  (`monitoring.py:37-53`); all four trainable modalities consume JSONL text/image/audio. Input
  drift is effectively tabular-only — and tabular has no fine-tune flow. Only the 013 quality
  half (delayed ground-truth labels) covers the modalities that can actually retrain.
- **Breaches can be dropped.** A busy GPU 409s the retrain launch and nothing queues or retries
  it (`routers/monitor.py:79-82`); the cooldown then suppresses the next attempt.
- **Double-fire race between the two signals.** The quality path reserves the cooldown *before*
  launching (`try_reserve_retrain`, `monitor.py:144`); the PSI path launches first and notes
  after (`monitor.py:73-78`). Two concurrent checks can both launch.
- **Shadow-replay verdicts are inert.** Advisory by design (`shadow.py:1-14`), but nothing —
  not even the models UI — consumes a "challenger wins" verdict, so 016's output has no
  downstream edge in the graph.

### 4.2 Duplication with observed correctness divergence

- **llama vs whisper supervisors** are declared mirror copies
  (`serving/whispercpp/supervisor.py:5-6`) but have already diverged: whisper reaps a
  resident-but-unready child before relaunching (`whispercpp/supervisor.py:105-110`); llama does
  not (`serving/llama/supervisor.py:83`) — an orphaned llama-server holding :8081 puts a
  restarted supervisor into an EADDRINUSE → 507 loop. A lease-protocol fix must currently be
  hand-applied in two supervisors plus the trainer's copy of the vram-owner dance
  (`trainer.py:101-108`).
- **Tenant identity and topology as scattered string literals.** Holder-label and holder→URL maps
  are duplicated across `gateway/app/serving.py:24` and `gateway/app/swap.py:53-67`; tenant names
  (`llm-serving`, `asr`, `vision`, `training`) are raw strings in every lease caller. No shared
  enum/registry module.
- **Flows never converged on `_common.py`.** The LLM flow keeps verbatim copies of the Prefect
  shim, S3 client, config constants, and JSONL fetch (`flows/finetune.py:29-123` vs
  `flows/_common.py:15-74` — admitted at `_common.py:9-11`). `_score_at_registration` is
  copy-pasted in all four flows (`finetune.py:242`, `vision_finetune.py:188`,
  `asr_finetune.py:185`, `embeddings_finetune.py:136`). The trainer daemon repeats the same
  read-validate-lock-lease-spawn-unwind dance four times (`trainer.py:317/362/395/458-486`), and
  the subprocess result protocol exists twice (`run_flow.py:32` vs `trainer.py:244`).
- **Smaller copies**: `resolve_api_key` in three CLIs (`monitoring/drift.py:21`,
  `data/register_dataset.py:22`, `data/submit_labels.py:24`); winner-direction logic in
  `shadow.build_verdict` (`shadow.py:70-94`) vs `evaluation.compare` (`evaluation.py:516-521`);
  the reference-extraction expression in `scoring/__init__.py:50` vs `evaluation.py:311`.

### 4.3 Configuration and daemon contracts are implicit

- **No central settings.** `TRAINER_URL` is read via `os.getenv` in seven gateway files,
  `SERVING_URL` in five, `SERVING_MODEL` in two (`serving.py:19`, `evaluation.py:45`). Ports
  8081/8082/8090–8096/8099 are hand-allocated across daemons with no registry — and this already
  produced a real on-hardware bug (LLM scorer vs supervisor status server on :8099, fixed by
  moving to :8096, `training/scoring/llm.py:24-27`).
- **Daemon `/health` JSON shapes are unversioned implicit contracts** consumed field-by-field in
  `serving.py:59-65`, `swap.py:227`, `platform_metrics.py:36-38`, `vision.py:82`. A field rename
  in a daemon breaks the gateway silently.
- **Dead and stale artifacts confuse operators**: `docker-compose.gpu.yml` is an empty `{}`
  placeholder whose documented usage is a no-op (`docker-compose.gpu.yml:6-16`); `init.sql`
  creates a `gateway` database no code connects to; the gateway reports `version="1.2.0"`
  (`main.py:32`) with a hand-maintained endpoint list at `/`; the README status paragraph stops
  at increment 014.

### 4.4 Observability funnels through one point and over-polls

- **Prometheus scrapes only the gateway** (`infra/prometheus/prometheus.yml`). All native-daemon
  signals are gateway-re-exported gauges (`platform_metrics.py`), so a gateway outage blinds the
  entire observability plane precisely when you need it. No alert rules, no alertmanager.
- **Every `/metrics` scrape does sequential blocking daemon probes** (up to ~4s,
  `platform_metrics.py:24-40`); `platform_health.aggregate` probes six daemons serially
  (`platform_health.py:43`, ~18s worst case) instead of `asyncio.gather`.
- **nvidia-smi fork storm**: each supervisor `/health` shells `nvidia-smi`
  (`gpu_lease.py:71-80` via `supervisor.py:271-272`), and the UI polls `serving/state` every 4s —
  a continuous subprocess fork chain across gateway → supervisor → nvidia-smi.
- **Dashboard blind spots**: no panels for HPO (012), batch (014), shadow-replay (016),
  swap/preemption (017), request error rate, or disk usage (the profile's scarcest resource);
  `mlops_serving_up`/`mlops_trainer_up` are exported but unvisualized.

### 4.5 High-churn state on object storage; job state in RAM

- **O(N) sequential S3 round-trips with a fresh boto3 client per call** in quality/shadow window
  resolution (`datasets.py:35-45`, `quality.py:404`, `shadow.py:99`), and a full prefix list on
  every captured input (`quality.py:309-321`). Fine at demo scale; quadratic pain as prediction
  logs grow.
- **Write-once label semantics rely on an in-process lock** because S3 has no compare-and-swap
  (`quality.py:70-73`) — single-replica-safe only, and the retrain cooldown clock is likewise
  in-process (`quality.py:513`).
- **Silent truncation past 1000 objects** in `monitoring.latest_reports` (`monitoring.py:124`) and
  `datasets.list_datasets`/`_versions` (`datasets.py:92,108`) — `quality._list_keys` paginates
  correctly, the others don't.
- **Trainer job state is in-memory dicts** (`trainer.py:59-63`): a daemon restart loses all
  run/study/batch/shadow records and the gateway's `GET /runs/{id}` 404s on jobs that ran for
  many minutes.

### 4.6 Swap/preemption edge cases

- **The batch guard fails open** (`swap.py:216-231`): if the trainer is unreachable,
  `batch_active` reads `False`, so a preempt can evict a serving supervisor that an active GPU
  batch is driving — exactly the case FR-155 forbids. This guard should fail closed.
- **The handoff is not transactional**: between `_wait_for_free` observing a cleared holder
  (`swap.py:203`) and the caller's forward acquire, any other tenant can win the freed lease.
  Acceptable best-effort semantics for a single operator, but worth stating in the spec.
- **A wedged (D-state) child pins the GPU forever by design** (`supervisor.py:143-144`,
  anti-TTL rationale at `gpu_lease.py:102-107`). Correct trade-off, but there is no operator
  affordance to see or clear it short of shell access.
- The `/tmp` lease path (`gpu_lease.py:47`) assumes every GPU daemon shares one filesystem view —
  the same cross-distro reality that forces IP injection in `serve_up.ps1` could hand two
  daemons different `/tmp`s and silently void the mutex.

### 4.7 Ops and portability

- **Full bring-up is PowerShell-only** (`make up-all` → `scripts/up_all.ps1`); a Linux-only
  operator has no supported path to a running stack. `Makefile` has no `test`/`lint`/`typecheck`
  targets; `smoke` runs one file, not the suite.
- **Compose hygiene**: no resource limits on any service (Principle III unenforced at the
  runtime level); no healthchecks on minio/prometheus/grafana; gateway `depends_on: mlflow` uses
  `service_started`, racing MLflow readiness.
- **UI thin spots**: 409/busy detection string-matches the thrown error text
  (`ui/app/runs/page.tsx:165`) instead of a structured status field on `gwGet`/`gwPost`; HPO
  per-trial visualization is a documented fast-follow; no JS/TS test runner at all (UI covered
  only by Python HTTP probes).
- **Gateway robustness minors**: detached `asyncio.ensure_future` logging tasks can be GC'd
  mid-flight (`vision.py:107`, `transcribe.py:94`, `stream.py:160`); deprecated
  `asyncio.get_event_loop()` in `swap.py:85`; unbounded in-memory base64 bodies on
  `/datasets`, `/vision/classify`, `/transcribe`; `evaluation.compare` accepts and ignores
  `benchmark`/`metric_name` params (`evaluation.py:495`, `models.py:44-47`).

---

## 5. Recommended revisions (existing code)

### P0 — correctness, small diffs, do before new features

| # | Fix | Where |
|---|-----|-------|
| 1 | Batch guard fails **closed**: trainer unreachable ⇒ refuse preempt | `swap.py:216-231` |
| 2 | Port whisper's reap-before-relaunch into the llama supervisor | `llama/supervisor.py:83` (mirror `whispercpp/supervisor.py:105-110`) |
| 3 | PSI retrain path reserves the cooldown *before* launch (same as quality path) | `routers/monitor.py:73-78` |
| 4 | Retain references to detached logging tasks (or use a small task set) | `vision.py:107`, `transcribe.py:94`, `stream.py:160` |
| 5 | Move the lease file off `/tmp` to a fixed shared state dir; each daemon asserts at startup it sees the same lease inode (write-beacon check) | `gpu_lease.py:47` |
| 6 | Fix pagination in `latest_reports` / dataset listings (reuse `quality._list_keys`) | `monitoring.py:124`, `datasets.py:92,108` |

### P1 — architecture health (mechanical consolidation, no behavior change)

1. **Shared supervisor library.** Extract `_ensure_loaded` / `_unload` / `_unload_now` /
   `_idle_watcher` / lease-registration into one base module used by llama and whisper (the
   whisper file already says it mirrors llama "EXACTLY" — make that true by construction).
   The trainer's vram-owner sequence should call the same helper.
2. **One topology module.** A single `platform_topology.py` (importable by gateway *and* native
   daemons) owning: tenant name constants, port assignments, holder→URL/label maps,
   `NON_PREEMPTABLE`. Deletes the `serving.py`/`swap.py` map duplication and ends ad-hoc port
   allocation.
3. **Gateway settings object.** One pydantic-settings module for `TRAINER_URL`, `SERVING_URL`,
   `BENTO_URL`, `EMBED_URL`, `TABULAR_URL`, `ASR_URL`, `SERVING_MODEL`, `MLFLOW_TRACKING_URI` —
   plus typed models for the daemon `/health` payloads the gateway consumes.
4. **Shared storage module.** Promote `_s3`/`_get_json`/`_put_json`/`_list_keys`/`_missing` out
   of `quality.py`/`datasets.py` privates into `gateway/app/store.py` with a module-level
   client (ends the fresh-client-per-op pattern and the cross-module private reach-through
   `shadow.py:67-190`).
5. **Flow convergence.** `finetune.py` adopts `_common.py`; one shared
   `score_at_registration` call-site helper; collapse the trainer's four POST handlers into one
   parameterized launch path; unify the two subprocess result protocols.
6. **Trainer job journal.** Append job records (`_runs`/`_studies`/`_batches`/`_shadows`) to a
   JSONL journal in the state dir and reload on start — restart no longer orphans job history.
7. **Parallelize probes; cache GPU reads.** `asyncio.gather` in `platform_health.aggregate`;
   a 1–2 s TTL cache in `free_vram_gb()` so health polling stops forking `nvidia-smi`
   continuously; a shared `httpx.AsyncClient` per daemon.

### P2 — adopt the Postgres that already exists (targeted, optional)

`init.sql` provisions a `gateway` database that nothing uses. Moving the **high-churn, joined**
state there — prediction logs, labels, captured-input index (not payloads), trainer job records —
would eliminate the O(N) S3 scan-and-join in quality/shadow windows, give real write-once label
semantics (unique constraint instead of an in-process lock), and fix truncation — with **zero new
resident services** (Postgres is already up for MLflow; Principle III satisfied). Keep MinIO for
payloads and reports. If this isn't wanted, delete the `gateway` database from `init.sql` so the
schema stops implying otherwise.

---

## 6. Recommended new features (candidate increments)

Framed as spec-kit increments in dependency order. 018 is the one that matters most: it makes
Principle IV true.

### 6.1 `018-close-the-loop` — scheduled monitoring + modality-aware retrain (P1)

The highest-value increment available: it converts three manual arrows of the lifecycle into the
closed loop the constitution promises, using only components already resident.

- **In-gateway scheduler**: an asyncio background task (no new service, no cron dependency)
  running drift + quality checks per configurable interval, per monitored model/modality.
- **`RetrainSpec.modality`** + per-modality default hyperparameters; drift/quality breach
  launches the *breached* modality's flow. UI monitor page gains a modality selector.
- **Dynamic dataset resolution**: `dataset_version: "latest"` (or "current-window") so retraining
  consumes the drifted data, not a pinned snapshot.
- **Queue-of-one retry**: a breach that 409s on a busy GPU parks a single pending retrain and
  retries with backoff instead of dropping the signal (constitution-safe: still never preempts
  training).
- Fixes the PSI reserve-order race as part of unifying the two trigger paths (P0 #3).

### 6.2 `019-modality-drift` — input drift for the modalities that can retrain (P2)

PSI-on-CSV covers none of the trainable modalities. Add embedding-space drift using the
**already-resident, off-lease CPU embed service**: sample captured inputs (016's capture
pipeline already exists), embed reference vs current windows, compare via centroid cosine
distance + PSI over top principal dimensions. Text prompts and transcripts are directly
embeddable; vision can start with lightweight image statistics. No new service, no GPU use, and
the 018 trigger machinery consumes it unchanged.

### 6.3 `020-promotion-policy` — make shadow verdicts actionable (P2, needs a constitution note)

Keep the gate manual by default, but let 016/015 outputs *do* something:

- Surface the latest shadow verdict on the models page next to the gate verdict.
- Opt-in **promote-on-green policy**: gate pass + shadow "challenger wins" (when a window
  exists) ⇒ either auto-promote with audit trail, or (default) raise a "promotion suggested"
  state the operator confirms with one click.
- This is the platform's last mile from "MLOps platform" to "self-improving loop"; it warrants
  an explicit governance sentence since today every `_register` promises manual promotion.

### 6.4 `021-observability-hardening` (P2)

- Prometheus scrapes the native daemons directly (llama supervisor already serves `/metrics`;
  add the equivalent to trainer/whisper/bento or run one tiny host exporter) so the gateway is
  no longer the observability SPOF.
- Alert rules: daemon down, GPU holder unchanged past a threshold (the wedged D-state case),
  quality breach, retrain-launch failure, disk-free below budget.
- Dashboard panels for swap/preempt outcomes, batch runs, HPO studies, shadow verdicts, request
  error rate, and disk usage. Wire the already-exported `mlops_*_up` gauges.

### 6.5 `022-linux-bringup-parity` (P3)

Port `up_all.ps1`/`serve_up.ps1` to a cross-platform Python entrypoint (the IP-injection logic
is the only Windows-coupled part); add `make test` (pytest), `make lint`, `make typecheck`
(`tsc --noEmit` is currently a manual runbook step); make `docker-compose.gpu.yml` real or
delete it; add compose resource limits + missing healthchecks; refresh the README status block.

### Explicit non-recommendations

Consistent with Principles I–III, this review deliberately does **not** recommend: Kubernetes or
any multi-node story, a message broker or Redis, an always-on Prefect/Optuna server, replacing
the file-based lease with a service, or multi-replica gateway support. The single-box,
single-operator assumptions are load-bearing simplifications, and the in-process
cooldowns/locks are correct *because* of them.

---

## 7. Constitution alignment summary

| Principle | Status | Notes |
|---|---|---|
| I. Local-first, single machine | ✅ | No cloud dependency found; offline-capable after pulls |
| II. Single GPU tenant (non-negotiable) | ✅ with edges | Lease protocol sound; edges: fail-open batch guard (§4.6), `/tmp` namespace fragility, duplicated supervisor protocol code (§4.2) |
| III. Lightweight footprint | ✅ | Hand-rolled metrics/PSI keep the gateway thin; no compose-level enforcement (limits) |
| IV. Full lifecycle + feedback loop | ⚠️ **half-open** | §4.1 — the retrain loop needs 018 to be true as written |
| V. OSS & swappable | ✅ | DVC swap-out documented and clean; MLflow/MinIO behind thin modules |
| VI. Reproducibility & observability | ⚠️ | Tracking strong; observability plane is a gateway-funneled SPOF (§4.4) |
| VII. Phase-gated delivery | ✅ | 17 increments, deferred items closed on schedule |
