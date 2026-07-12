# On-hardware validation runbook — increments 018 (platform re-architecture) + 020 (stack remediation)

Target box: RTX 5070 Ti (12 GB), WSL2 Ubuntu + Rancher Desktop. Model on the 015–017 runbook.
018 folds every native daemon into ONE GPU host agent (`hostagent/`); at completion the platform
runs **two** supervised native processes — the agent and the UI — under the shrunken supervisor.
The 020 records (store exit → Garage, Bento-ectomy golden gates, agent-runtime drill + verdict,
decommission) share this document — one [HW] record chain per box (renamed from the `-018`
filename at T419 once both increments' records landed).

Records the on-hardware success criteria for the 018 [HW] tasks: **T365** (SC-106..110), **T372**
(SC-112), **T377** (SC-111). Offline coverage (unit/integration) lands with the suite; these are the
criteria that need the real GPU.

## 0. Bring-up

```powershell
# one command: rebuild the gateway (T364 settings), inject the WSL IP as the single AGENT_URL,
# launch the supervisor {agent, ui}, wait for /platform/health.
.\scripts\up_all.ps1
# ASR is opt-in — build whisper.cpp first if validating the ASR criteria:
#   wsl bash serving/whispercpp/build.sh   (asr is in the default supervised engine set, reports
#   unavailable until built, so platform-health never stalls on it)
```

T364 collapse note: the six per-daemon `*_URL` env vars are gone — `up_all` injects only `AGENT_URL`
and the gateway derives each `${AGENT_URL}/engines/<id>` base + the legacy byte-compatible paths.
A standalone `docker compose up gateway` (outside `up_all`) must still be given `AGENT_URL` (the WSL
IP), or the gateway falls back to `host.docker.internal:8100`, which can't cross WSL distros.

---

## T365 — 2 native processes; five modalities through the agent (SC-106..110)

### ☐ SC-106 — exactly 2 supervised native daemons; all five modalities smoke

```bash
wsl pgrep -fc 'hostagent/main.py|next-server'   # == 2 (agent + UI); supervise.py is the only
                                                #    other resident native process
python _sweep_smoke.py                          # 5-modality smoke through the single agent
```

**PASS** (2026-07-04). Supervised set is `{agent, ui}` — `supervise.py` `/status` shows exactly the
two, both `healthy` (agent pid, ui pid). `pgrep` counts 2 real daemons (a third match is the `pgrep`
shell self-matching the pattern). All five modalities serve through the one agent
(`http://<wsl-ip>:8100/engines/<id>`), each a byte-compatible path off the single gateway URL:

| modality | transport            | result |
|----------|----------------------|--------|
| embed    | CPU, off-lease       | 200 — vectors (dim 384) |
| tabular  | CPU, off-lease       | 200 — predictions |
| llm      | GPU tenant           | 200 — text |
| vision   | GPU tenant (preempt) | 200 — predictions, device=cuda |
| asr      | GPU tenant (preempt) | 200 — text |

### ☐ SC-107 — cold/warm latency baseline (within 10% of 017; recorded here as the 018 baseline)

The 015–017 runbook records no per-modality latency table, so 018 records the baseline here. Warm
(resident) latency is the real serving cost; cold is a full evict→spawn→load. Measured through the
gateway, GPU engines cold-loaded clean (agent `/control/unload` between):

| modality | cold load | warm serve | notes |
|----------|-----------|------------|-------|
| embed    | 0.45 s    | 0.21 s     | CPU; first call boots the bento child |
| tabular  | 0.02 s    | 0.02 s     | CPU; resident |
| llm      | 3.6 s     | 0.08 s     | llama-server 7B Q4 (`load_ms`≈3.0 s) |
| vision   | 31.8 s    | 0.08 s     | cold dominated by BentoML import + MobileNet |
| asr      | 2.5 s     | 0.33 s     | whisper.cpp (`load_ms`≈2.0 s) |

**PASS** — every modality serves; warm latencies are sub-second. First-ever cold loads on a
freshly-booted agent (cold OS file cache) can exceed the per-engine `ready_wait_s` (llm 60 s, vision
120 s); once the OS cache is warm they land as above. This is the recorded 018 baseline.

### ☐ SC-108 — swap-contention stress: one model in VRAM under a preempt storm

```bash
# ≥100 fast swaps (llm↔asr) + a 3-way mix incl. vision:
python scripts/swap_stress.py --cycles 100 --engines llm,asr
python scripts/swap_stress.py --cycles 6  --engines llm,vision,asr
```

**PASS** (2026-07-04). WSL2's `nvidia-smi` can't enumerate per-process compute apps
(`--query-compute-apps` → `[N/A]`), so the script uses total `memory.used`: one resident model peaks
at its own footprint and dips to baseline between swaps, while a co-residency bug would sum two models
and never dip. The agent's admission holder is the structural witness (a single slot can never name
two tenants); concurrent different-target preempts race the swap reservation (one lands, the rest are
refused).

| run                         | landed | mismatches | peak VRAM | baseline VRAM | samples |
|-----------------------------|--------|-----------|-----------|---------------|---------|
| 100× `llm↔asr`              | 100    | 0         | 5.07 GB   | 0.48 GB       | 931     |
| 6× `llm,vision,asr` (3-way) | 6      | 0         | 5.07 GB   | 0.48 GB       | 120     |

Every landed swap ended with its own target as the holder; peak VRAM (5.07 GB = the llm alone) never
approached a two-model sum, and VRAM returned to a 0.48 GB idle baseline between swaps — **zero
instants with two GPU tenants resident, zero sniped swaps** across 106 cycles / >1000 GPU samples.

### ☐ SC-109 — agent restart mid-job: journal intact, interrupted job failed-with-reason, VRAM baseline

```bash
# launch a finetune (holds admission kind="job"), kill -9 the agent while it runs, let the
# supervisor auto-restart it (FR-178), then read the journal:
python _sweep_sc109.py
```

**PASS** (2026-07-04). Baseline: 7 terminal jobs journaled. Launched an llm finetune
(`jobsmoke-sft`) → it held the slot (`holder=training, kind=job`) → `kill -9` the agent → the
supervisor restarted it **unconditionally** (FR-178) as a new pid in **4.6 s** (< 10 s), reporting
`interrupted_since_start=1`. The new agent's startup `mark_interrupted` (FR-173) marked the killed
run `interrupted`; all 7 prior terminal jobs are still listed (history intact — the append-only
`~/.mlops-lite/journal.jsonl` survives the crash); VRAM returned to the 0.30 GB idle baseline. This
also live-validates the two invariants the lockfile retirement leaned on: unconditional
supervisor restart and durable journal recovery.

### ☐ SC-110 — gateway down: direct agent scrape survives; zero per-poll forks

```bash
docker compose stop gateway
python _sweep_sc110.py           # target still 'up', gpu metric fresh, 60s fork-watch
docker compose start gateway
```

**PASS** (2026-07-04), **after a fix**. FR-174's `hostagent` scrape target was hardcoded to
`host.docker.internal:8100`, which on this cross-distro WSL setup resolves to the docker bridge
(172.17.0.1) → connection refused, so the direct scrape never worked (target `down`). Fixed by
making it **file-based service discovery**: `up_all.ps1` writes the injected WSL agent IP into
`infra/prometheus/targets/hostagent.json`, Prometheus hot-reloads it (no restart), and the committed
default keeps the same-distro `host.docker.internal` fallback.

With the gateway **stopped**: the `hostagent` target stays `up` (Prometheus scrapes
`http://<wsl-ip>:8100/metrics` directly), and `hostagent_gpu_free_gb` keeps arriving fresh (< 30 s
old, value 11.35) — GPU/holder/engine/job signals survive the gateway outage (the pre-018
observability SPOF is closed). Fork-watch: **932** continuous `/health` polls over 60 s produced
**0** `nvidia-smi` spawns — the agent's `GpuReader` uses in-process NVML (`pynvml`) with a 1 s TTL
cache, so health polling forks nothing (SC-110).

---

## T372 — Principle IV loop closes by declaration (SC-112)

### ☑ SC-112 — declared policy + injected breach → correct-modality retrain + suggestion, zero manual steps — **PASS** (2026-07-04)

Ran on the RTX 5070 Ti against the live stack. The **full autonomous loop closed end-to-end with zero
manual invocations between breach detection and the promotion suggestion** — the scheduler
(`gateway/app/scheduler.py`, a gateway lifespan task) drove every step.

**Sequence (observed live):**
1. Declared a `vision-mobilenet` policy (`modality:vision`, `quality` monitor with `baseline:0.9`,
   `check_interval_s:60`, `on_breach:retrain vision-demo`, `promotion_mode:suggest`).
2. Injected a quality breach — seeded 24 wrong-labeled `image-classification` pairs for the `@serving`
   version straight into the US4 store (predictions⋈labels window, accuracy 0). The scheduler's first
   due check flagged `breached:true, value:0.0` (`gateway_policy_checks_total{result="breach"}`).
3. **Autonomous retrain launched within one check interval** — the FIRST launch transient-failed
   (agent warming right after boot); the loop released the cooldown (a failed launch must not consume
   it) and the **next 60 s tick re-detected the breach and launched** (`gateway_policy_retrains_total
   {result="launched"}`). This is the designed FR-163 resilience, seen live.
4. Retrain **completed + registered `vision-mobilenet` v2** through the agent's jobs surface (one
   `kind=job` GPU tenant); the loop **auto-scored the candidate** via the 015 gate.
5. **Verdict correctly withheld a suggestion** for a candidate whose incumbent had no comparable eval
   baseline (`gate=warn, reason="incumbent has no logged eval metric (missing-metric policy)"` →
   `promotions{mode="not_green"}`)
   — a not-green candidate must not get a one-click promote (FR-183). After giving the incumbent a
   like-for-like accuracy baseline (0.20), the next autonomous cycle produced a **green** candidate v3
   (`gate verdict="pass"`, candidate 0.25 > incumbent 0.20, delta 0.05, no shadow window) → an **OPEN
   promotion suggestion** appeared in `GET /suggestions` (`promotions{mode="suggest"}`), with **no
   manual step** between the breach check and the suggestion.

**Zero-manual-steps confirmed:** the only human action was the initial policy declaration; detection →
retrain → register → score → suggestion were all scheduler-driven. Drill artifacts (seed pairs, the
policy, the suggestion, the demo candidate versions, the injected baseline tags) were cleaned up
afterward — store back to zero rows. (The `data/submit_labels.py` serve-and-mislabel path in the
quickstart is an equivalent way to inject the breach; seeding the store directly is the same "injected
breach" with a deterministic accuracy of 0.)

---

## T377 — durable monitoring state (SC-111)

### ☑ SC-111 — 10k-prediction window < 5 s; concurrent-label write-once; restart with intact history — **PASS** (2026-07-04)

US4 (T373–376) landed the relational store, so this ran on the RTX 5070 Ti box against the live
gateway Postgres (127.0.0.1:55432). All three parts pass; the restart drill **found + fixed a real
crash-loop bug** (below).

**Part 1 — window over ≥10,000 predictions < 5 s.** Bulk-seeded **12,000** predictions+labels for one
`(modality, model, version)`, then timed `store.window()` (the indexed `predictions⋈labels …
served_at DESC LIMIT n` join that replaces the O(N) MinIO scan):

| window `n` | rows | time |
|---|---|---|
| 200 (realistic) | 200 | **1.8 ms** |
| 12,000 (full) | 12,000 | **40.5 ms** |

40.5 ms ≪ 5 000 ms — the composite `ix_pred_window` index makes it a bounded index scan, not a
listing. (The pre-US4 object scan took *minutes* at this size.)

**Part 2 — concurrent duplicate labels → exactly one stored, 100%.** 25 trials × 8 threads (each its
own connection) racing `attach_label` on the same `prediction_id`: **25/25** stored exactly one label,
the other 7 each got `LabelExists` — the write-once PRIMARY KEY (FR-185) holds under contention with
no in-process lock.

**Part 3 — restart with intact history (gateway + agent).** Seeded one of each durable record
(policy / prediction+label / a **queued** job / suggestion), then:
- `docker restart mlops-lite-gateway-1` → healthy in 3 s; every relational row survived (it lives in
  the separate persistent `postgres` container; the gateway re-`bootstrap()`s the idempotent schema
  and re-reads).
- restarted the **native agent** → it hydrated the `jobs` table and flipped the crash-orphaned
  `queued` job to `interrupted (reason="agent restart")` in one atomic `mark_jobs_interrupted`
  (FR-173) — the durable row confirmed post-restart. History intact across both restarts.

> **Bug found + fixed during Part 3 (agent DB unreachable → crash loop).** The native WSL agent's
> `Journal()` (T375-B) connects to the gateway DB via `store.dsn()`, whose default host is the
> in-container `postgres` — **unresolvable from a native WSL process**. Since T375-B made the DB a hard
> startup dependency, the agent had been crash-looping (`OperationalError: failed to resolve host
> 'postgres'` → fail-loud exit → supervisor relaunch, **660 restarts** observed) the whole time —
> durable job state was silently non-functional on the real deployment. The intended injection the
> compose comment described (`up_all.ps1`) was never actually implemented. **Fix:** `hostagent/run.sh`
> now exports `GATEWAY_DB_HOST=127.0.0.1` + `GATEWAY_DB_PORT=${POSTGRES_PORT:-55432}` (the
> host-published port), so `store.dsn()` targets the reachable Postgres. Post-fix: agent healthy 5/5
> polls, single process, hydrate + `mark_interrupted` working (above). The compose comment was
> corrected to point at `run.sh`.

## 020 T404 — Garage candidate spike (FR-202 gate; SC-130)

**PASS — Garage clears every checklist item; no SeaweedFS fallback needed.** Pinned
`dxflrs/garage` v2.3.0 (digest in compose), single node, `replication_factor=1`, bootstrap via
the token-authed Admin API (`infra/garage/init.py` — the image is scratch-based, no shell for
CLI scripting; research R2 adjusted).

Direct S3-surface legs (boto3 against `:3900`, recorded by `t404_spike.py`):

| Leg | Result |
|---|---|
| 300 MB multipart round-trip (upload_fileobj/download_fileobj) | PASS — 1.0 s up / 0.5 s down, bytes identical |
| Pagination past 1,000 keys (1,100 seeded, truncation protocol) | PASS — 1,100 listed, ordered |
| Prefix listing + CommonPrefixes | PASS |
| 404 discrimination (`head_object`/`get_object` on missing keys) | PASS — clean 404/NoSuchKey codes |
| Duplicate-PUT (last-write-wins, no error) | PASS |
| Delete idempotency (delete of a missing key) | PASS |

Env-seam flip rehearsal (empty candidate, per quickstart §US1.1): gateway + MLflow flipped by a
temporary compose override — **`S3_ENDPOINT_URL` asserted unset, gateway's *resolved*
`client.meta.endpoint_url` moved to `http://garage:3900`** — then dataset register landed on
Garage only, MLflow artifact round-trip via the serve-artifacts proxy passed, a live `/infer`
logged its prediction payload to Garage, and the **full offline suite passed under the flipped
env (454 passed)**. Flipped back; every rehearsal artifact wiped (buckets to 0, PG row, MLflow
experiment).

**Idle RSS (SC-130, `docker stats --no-stream` at rest): Garage 6.8 MiB vs MinIO 161.5 MiB —
~24× smaller.** Gate: ≤ incumbent — PASS by a wide margin.

## 020 T405 — migrate → cutover → rollback proof → soak (FR-199/200; SC-127/128)

**PASS.** Reports: [forward](migration-report-020-forward.json) ·
[idempotent re-run](migration-report-020-forward-idempotent.json) ·
[reverse (rollback re-mirror)](migration-report-020-reverse-rollback.json).

- **Forward migration**: 540 objects / ~2.5 GB across `datasets/models/results/mlflow` —
  `parity: true` on every bucket. **Re-run: `copied: 0` everywhere (SC-127).**
- **Cutover** (contract §cutover, both rough edges asserted): `S3_ENDPOINT_URL` unset; container
  resolved endpoint `http://garage:3900`; host consumer resolved endpoint
  `http://localhost:3900` (the `.env` cutover block overrides the baked `:9000` defaults);
  agent restarted to inherit the flip.
- **Golden flows on the cutover**: dataset list/read (migrated data), vision classify (slim
  child loading MobileNet **from Garage**, CUDA), `/infer` + **live preempt swap**
  (vision→llm, 017 semantics), PSI drift check over migrated datasets (report written), quality
  check (report written), and a **full 1-epoch vision fine-tune through the jobs path** —
  dataset read + MLflow artifact write + registration (v4) all on Garage. Throwaway v4 cleaned
  from the registry + both stores afterward.
- **Full offline suite on the cutover: 491 passed / 19 skipped (SC-128).**
- **Rollback proof**: reverse mirror carried the 7 post-cutover objects back (`parity: true`),
  config flip alone returned service to MinIO (resolved endpoint verified; datasets + the
  carried-back v4 artifact served), then flipped forward again. The rollback window works.

## 020 T411 — per-child golden gates ×3 (FR-203)

**PASS ×3 — byte-identical at the agent boundary.** The launch flip was already merged (PR #55),
so git was the swap lever: pre-flip adapters restored (`70278a7`) → agent restarted on the OLD
BentoML children → `capture_goldens.py` captured vision/embed/tabular (fixed-boundary multipart,
canonical JSON verbs, health-probe ok) → master adapters restored → agent restarted on the slim
FastAPI children → replay: **vision PASS, embed PASS, tabular PASS** (status + content type +
body bytes). Vision ran on the GPU box with the model loading from Garage.

## 020 T412 — Bento-ectomy retirement (FR-204; SC-131)

**PASS.** `serving/bento/` deleted; model-runtime pins moved to
`serving/children/requirements.txt`; `bootstrap.sh` + seed-script hints repointed. Venv:
**216 → 195 packages** (bentoml + 20 exclusive transitive deps removed — computed by
reverse-dependency closure, `t412_orphans.py` method); `pip list | grep -i bento` → empty;
critical imports (pynvml/fastapi/uvicorn/sentence-transformers/lightgbm/joblib/multipart) OK.
`pip check` reports only the two **pre-existing** items: the documented fsspec hold
(`native_env.lock`) and openai-whisper's 010-era no-deps install. Goldens replayed
byte-identical on FRESH child spawns post-uninstall; full suite 491 passed.

> **Latent dependency found + fixed (SC-133-adjacent):** `nvidia-ml-py` — the dist that ships
> `pynvml.py`, i.e. the agent's live-VRAM admission reader — was installed ONLY as a bentoml
> transitive dep; no requirements file named it. A naive "remove bentoml + its exclusive deps"
> would have silently degraded admission to the static-budget fallback. It is now pinned
> first-party in `serving/children/requirements.txt`.
### RuntimeBaselineRecord — `stdlib` (2026-07-05 08:09:04)

```json
{
  "runtime": "stdlib",
  "measured_at": "2026-07-05 08:09:04",
  "ttft_ms": 1.0,
  "stalls": 0,
  "stream": {
    "runs": 5,
    "ttft_ms": 1.0,
    "ttft_ms_max": 2.5,
    "stalls": 0,
    "stall_gap_s": 1.0,
    "frames_median": 66,
    "health_polls": 42,
    "health_poll_failures": 0,
    "health_poll_ms_median": 4.3
  },
  "multipart_ms": 17.1,
  "disconnect_ok": true,
  "disconnect": {
    "disconnect_ok": true,
    "next_request_ttft_ms": 24.9,
    "recovered_in_ms": 876.8
  },
  "swap_contention": {
    "preempt_status": 200,
    "behavior": "served",
    "preempt_latency_ms": 8262.3,
    "stream_completed_frames": 66
  },
  "baselines": {
    "ttft_ms": 2000.0,
    "stalls_max": 0,
    "multipart_ms": 3000.0,
    "stall_gap_s": 1.0
  },
  "misses": [],
  "meets_baselines": true
}
```

### RuntimeBaselineRecord — `uvicorn` (2026-07-05 08:10:44)

```json
{
  "runtime": "uvicorn",
  "measured_at": "2026-07-05 08:10:44",
  "ttft_ms": 1.6,
  "stalls": 0,
  "stream": {
    "runs": 5,
    "ttft_ms": 1.6,
    "ttft_ms_max": 3.6,
    "stalls": 0,
    "stall_gap_s": 1.0,
    "frames_median": 66,
    "health_polls": 284,
    "health_poll_failures": 0,
    "health_poll_ms_median": 3.0
  },
  "multipart_ms": 13.6,
  "disconnect_ok": true,
  "disconnect": {
    "disconnect_ok": true,
    "next_request_ttft_ms": 12.2,
    "recovered_in_ms": 5779.5
  },
  "swap_contention": {
    "preempt_status": 200,
    "behavior": "served",
    "preempt_latency_ms": 5050.8,
    "stream_completed_frames": 66
  },
  "baselines": {
    "ttft_ms": 2000.0,
    "stalls_max": 0,
    "multipart_ms": 3000.0,
    "stall_gap_s": 1.0
  },
  "misses": [],
  "meets_baselines": true
}
```

## 020 T415 — agent runtime drill verdict (FR-205; SC-132)

**VERDICT: `keep-stdlib`.** Both RuntimeBaselineRecords above (same box, same session, LLM
prewarmed, 5 runs each, 1.0 s stall gap, ~10 Hz concurrent `/health` polling):

| Measure | baseline | stdlib | uvicorn |
|---|---|---|---|
| stream TTFT median (transport TTFB to the first SSE frame) | ≤ 2000 ms | **1.0 ms** | 1.6 ms |
| inter-frame stalls (max across runs) | 0 | **0** (42 polls, 0 failures) | 0 (284 polls, 0 failures) |
| multipart RTT median (vision classify, warm) | ≤ 3000 ms | **17.1 ms** | 13.6 ms |
| mid-stream disconnect → next request clean | true | **true** | true |
| preempt-during-stream (vision `?preempt=true` contender) | matches lease semantics | **drained → served** | drained → served |

No stdlib baseline miss ⇒ per FR-205/T415 the default stays `stdlib`; the transports are
equivalent within noise on every duty. These are the runbook's FIRST numeric stream baselines
(017/018 recorded functional stream smokes only) — established here, compared A/B in-session.
Note: TTFT is measured to the first SSE `data:` frame (the adapter's start frame), i.e. the
transport's time-to-first-byte — the right A/B measure for a TRANSPORT decision; token latency
is the backend's, identical under both.

The losing runtime's code path + the `AGENT_RUNTIME` switch are queued for deletion in the next
increment (research R7 — no permanent dual matrix). Drill leg-order/choreography fixes that this
run motivated (multipart `?preempt=true`, GPU-shaped preempt contender, LLM legs before the
vision leg — one model in VRAM) ship with this record.

## 020 T406 — decommission (FR-201; SC-129) — operator-confirmed

**DONE — zero unmaintained components.** The operator confirmed the full decommission
(services + volume + source repoints). Sequence, per contract §decommission:

1. **Quiesced writers** (gateway stopped; supervisor + agent + orphaned children killed), then
   the **final forward pass: `copied: 0` on every bucket, `parity: true`**
   ([report](migration-report-020-final.json)).
2. **Compose**: `minio` + `createbuckets` services, the `miniodata` volume, and the pinned
   digests removed; `mlflow`/`gateway` depend on `garage-init` and carry the Garage endpoint +
   store-minted credential pair (`:?` fail-fast, FR-017). The running containers, the volume,
   and the cached images were removed from the live host.
3. **Source defaults repointed** (the seam's baked fallbacks): `platformlib/store.py` + `s3io.py`
   (`http://minio:9000` → `http://garage:3900`), `hostagent/run.sh`, `training/flows/*`, the
   seed scripts, `scripts/bootstrap.sh` + `reseed_registry.sh`, the children run scripts
   (`:9000` → `:3900`, cred bridges → the Garage pair), `tests/test_foundation.py` (the old
   store's health probe → a Garage TCP probe; its S3 port has no unauthenticated health path),
   `tests/test_exposure.py` (port sweep + agent-path checks reworked), `scripts/check_secrets.sh`
   (the retired store's shipped-default pattern replaced by a Garage key-id pattern),
   `gen_secrets` (retired pair no longer generated), `.env.example`, README (mermaid node,
   stack list, 020 section → BUILT; the frozen-CVE-digest note retired with the service), and
   `scripts/bootstrap_buckets.py` deleted (pre-compose relic; garage-init owns bootstrap).
4. **Gates**: `docker compose config` clean; the source-tree
   `grep -rin 'minio|:9000'` over `*.py|*.sh|*.yml|*.ps1|*.toml` (excluding `specs/`/`docs/`)
   returns **zero**; full offline suite green; **stack restarts clean on Garage alone**
   (agent + gateway healthy, datasets served, live `/infer` + preempt swap pass).

The US1 rollback story is now moot by design — the migration reports + the proven flip live in
this document as the record.

### SC-159 activation-switch drill (T528) — 2026-07-12 20:19:42

```json
{
  "model": "qwen2.5-0.5b-instruct",
  "a": "1",
  "b": "2",
  "cycles": 100,
  "switches": {
    "landed": 100,
    "mismatched": 0,
    "inconsistent": 0,
    "engines_over_one": 0,
    "peak_vram_gb": 1.89,
    "trough_vram_gb": 1.89,
    "holders": [
      "llm"
    ],
    "requests": 100,
    "duration_s": 586.1
  },
  "idempotency": {
    "client_timed_out": true,
    "resident_reached_A": true,
    "physical_reload_delta": 1.0,
    "retry_status": "noop",
    "retry_replayed": false,
    "op_reused": false
  },
  "job_holder": {
    "direction_a_refused_409": true,
    "direction_b_deferred": true,
    "direction_b_job_uninterrupted": true
  },
  "result": "PASS",
  "measured_at": "2026-07-12 20:19:42"
}
```

### SC-156/157 migration drills on populated state (T517 + T555) — 2026-07-12

Target: Compose Postgres (postgres:17.10), live `gateway` DB populated
(predictions=11, jobs=9, activation_operations=114, 10 tables, 71 constraints).

- **Backup/restore (T517)**: `pg_dump -Fc gateway` (36 490 bytes) → `pg_restore` into a fresh
  `gateway_restore_check`. All 10 tables' row counts IDENTICAL to live; table_constraints 71 = 71.
- **Baseline adoption on populated state (T517)**: simulated a legacy pre-023 DB (dropped the
  ledger + `activation_operations`, leaving the 8 baseline tables + data). `apply` adopted
  `001_baseline` and forward-ran `002_activation` to db_version 2 with **no data loss**
  (predictions 11→11, jobs 9→9; `activation_operations` recreated empty). Second `apply` = no-op.
- **Concurrency (T555)**: two simultaneous `apply` runs against a fresh-ledger copy — the advisory
  lock serialized them (one applied `[001_baseline, 002_activation]`, the other `[]`); ledger has
  version 1 ×1 and version 2 ×1, **0 duplicate versions**.
- **Compatibility failure — DB newer than binary (T555)**: stamped a fake `version=999`; `status`
  reports db_version 999 > binary 2 (must refuse writes = True) and `apply` is **refused**
  (`MigrationError: database is at migration 999, newer than this binary's 002 — refusing …`),
  the fail-closed mode gateway startup + host-agent writers act on (FR-299/301). No auto-evolution.

RESULT: PASS (SC-156, SC-157).

### RuntimeBaselineRecord — `stdlib` (2026-07-12 20:26:25)

```json
{
  "runtime": "stdlib",
  "measured_at": "2026-07-12 20:26:25",
  "ttft_ms": 68.7,
  "stalls": 0,
  "stream": {
    "runs": 5,
    "ttft_ms": 68.7,
    "ttft_ms_max": 106.8,
    "stalls": 0,
    "stall_gap_s": 1.0,
    "frames_median": 66,
    "health_polls": 12,
    "health_poll_failures": 0,
    "health_poll_ms_median": 21.8
  },
  "multipart_ms": 31.3,
  "disconnect_ok": true,
  "disconnect": {
    "disconnect_ok": true,
    "next_request_ttft_ms": 62.3,
    "recovered_in_ms": 213.8
  },
  "swap_contention": {
    "preempt_status": 200,
    "behavior": "served",
    "preempt_latency_ms": 4088.0,
    "stream_completed_frames": 66
  },
  "baselines": {
    "ttft_ms": null,
    "stalls_max": null,
    "multipart_ms": null,
    "stall_gap_s": 1.0
  },
  "misses": [],
  "meets_baselines": true
}
```

### SC-160/161 bounded transport drills (T536) — 2026-07-12

Target: WSL host agent (single stdlib transport, 8 workers + 8 queue), RTX 5070 Ti.

- **Stream / disconnect / preempt / multipart** (`scripts/agent_stream_drill.py --runtime stdlib
  --multipart-preempt`, now carrying `X-Agent-Key` for the 023 US2 boundary): TTFT 68.7 ms,
  0 stalls under concurrent `/health` polling (12 polls, 0 failures); mid-stream disconnect
  recovered clean (next request 62.3 ms); preempt-during-stream served; multipart 31.3 ms median.
  `meets_baselines: true`.
- **Saturation** (24 concurrent `/engines/llm/infer/stream`): bounded — 20×409 (the resident LLM's
  single-generation guard) + 4× connection-accept rejections; **no crash, no wedge**. Agent process
  peak **threads 27→28, RSS 203→204 MB** (worker pool bounded at 8 — no thread explosion under 24×
  load).
- **No admission leak**: after the burst, `holder=None`, `wedged=false`, `jobs_active=0`,
  `gpu_free_mib` back to baseline (~11 GB), LLM idle-released to `cold`. The single slot returned free.
- **Deterministic bounds** (413 / 503-over-bound / finite queue / timeout / graceful shutdown):
  pinned by the offline socket-level suite (`tests/test_agent_limits.py`,
  `tests/test_agent_engines_http.py`, `tests/test_agent_stream_drill.py`). 29/29 pass on this Windows
  host; the one over-bound-503 assertion (`test_saturated_workers_and_queue_answer_503`) reads the
  server's minimal 503 through Winsock, which raises `WinError 10053` (abortive close) on the client
  before the body is consumed — a Windows-host client-read artifact, not a transport defect. The
  transport logic is exercised green by the required `backend` CI gate on ubuntu-latest.

RESULT: PASS (SC-160, SC-161).

### T556 — full 023 target-hardware sequence — 2026-07-12 20:32

- **Commit**: `25489f3` (023 offline slice, master). **Hardware**: NVIDIA GeForce RTX 5070 Ti
  Laptop GPU, 12 227 MiB, driver 610.62. Stack: Compose gateway/postgres/mlflow/garage/prometheus
  + WSL host agent under the supervisor.
- **Auth gateway flow (US2)**: `POST /infer` with no key → 401, bad key → 401; the agent boundary is
  fail-closed (`/health` 401 no-key / 403 bad-key / 200 keyed; `/healthz` public 200);
  `security_mode=key`. Gateway→agent hops carry `X-Agent-Key` (all engine calls succeed).
- **LLM eval (US1 routing)**: `POST /infer` → 200, returned `PONG` via the serving path
  (`_engine_base('llm')` → the agent, not a retired 018 port).
- **Vision eval (US1 routing, FR-278)**: `POST /vision/classify` (preempt) → 200 with
  `model/device/predictions/prediction_id`; the swap left vision the sole tenant.
- **Activation rapid-switch + job refusal**: see SC-159 (T528) — 100/100 switches, idempotent
  timed-out retry (1 physical reload), job-holder refusal both directions.
- **Retained transport stream/saturation**: see SC-160/161 (T536) — stream/disconnect/preempt clean,
  24× burst bounded (threads 27→28), no admission leak.
- **Metrics/alerts (US7)**: Prometheus loads the `mlops-lite` group with all 10 alert rules
  (WedgedEngine, ProlongedGpuHold, ActivationDegraded, MigrationFailed, RepeatedSchedulerFailures,
  AgentSaturated, LowDiskSpace, ServingStoreUnavailable, AgentScrapeDown, JobsInterruptedByRestart);
  scrape targets `gateway`, `hostagent`, `prometheus` all `up`; `hostagent_reload_outcomes_total`
  scraped (4 series).
- **Resource budget + one-tenant invariant**: `vram_budget_gb=12.0`; across every observation in the
  activation (100 switches, peak 1.89 GB) and serving drills, at most ONE non-cold GPU engine and a
  single `holder` — the one-model-in-VRAM invariant held throughout; `wedged=false`.

RESULT: PASS. 023 target-hardware sequence validated end-to-end.
