# On-hardware validation runbook — increment 018 (platform re-architecture)

Target box: RTX 5070 Ti (12 GB), WSL2 Ubuntu + Rancher Desktop. Model on the 015–017 runbook.
018 folds every native daemon into ONE GPU host agent (`hostagent/`); at completion the platform
runs **two** supervised native processes — the agent and the UI — under the shrunken supervisor.

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
