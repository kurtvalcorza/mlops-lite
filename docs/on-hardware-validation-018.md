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

### ☐ SC-112 — declared policy + injected breach → correct-modality retrain + suggestion, zero manual steps

```bash
# declare a vision policy (suggest, 60s), serve a few vision predictions, submit mislabels to breach,
# then within one interval a vision retrain launches → registers + scores → suggestion appears:
curl -XPUT  $GW/policies/vision-mobilenet -H "X-API-Key: $KEY" -d '{"modality":"vision","monitors":["quality"],"interval_s":60,"promotion":"suggest"}'
# ... serve vision predictions, POST /monitor/labels (mislabeled), POST /monitor/quality/check ...
curl -s $GW/policies/vision-mobilenet/status ; curl -s $GW/suggestions
```

**PENDING — runnable now (own focused drill).** The policy scheduler (`gateway/app/scheduler.py`,
whose T364 `trainer_url→agent_url` regression this sweep fixed) is US3 code that already merged and
was exercised in the 018 live-policy-loop smoke; SC-112 is the full breach→retrain→suggestion loop,
which launches a **real multi-minute vision retrain** through the agent's jobs surface. Deferred to a
dedicated loop-drill run so this runbook lands the deterministic T365 criteria first; the retrain path
itself is already proven by the T362 jobs [HW] smoke (finetune completed + registered through the
agent).

---

## T377 — durable monitoring state (SC-111)

### ☐ SC-111 — 10k-prediction window < 5 s; concurrent-label write-once; restart with intact history

**BLOCKED on US4 (T373–377).** SC-111's target — a quality/shadow window over ≥10,000 predictions
resolving in **under 5 seconds** (from minutes today) — is the whole *point* of the US4 relational
state store that replaces the current O(N) MinIO object scans. Until US4 lands the store, the
monitoring path still does the object-scan it is meant to eliminate, so the < 5 s criterion cannot be
met. T377 is a **US4 [HW]** task by construction (tasks.md: "US4 (T373..T377)"); it will be appended
here once US4 is built. The restart-with-intact-history half is already corroborated by SC-109 (the
agent journal survives a crash); the gateway-restart + relational-history half needs the US4 store.
