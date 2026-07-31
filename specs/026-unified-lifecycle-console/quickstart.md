# Quickstart: Validating 026 Unified ML Lifecycle Console

Runnable validation for the console and the read surfaces it depends on. Layered by the repository's
existing test taxonomy — **offline** (no stack), **live** (Compose up), **hardware** (`[HW]`, needs
the GPU box). Each layer is independently runnable; a failure at one layer does not require the next.

Details live in [contracts/](./contracts/) and [data-model.md](./data-model.md) — not repeated here.

---

## Layer 1 — Offline (no stack, no GPU)

The gate CI runs. Everything here is web-free or fixture-backed.

```bash
python -m pip install -r requirements-dev.txt
ruff check .
pytest                       # unmarked = the full offline suite
```

Targeted:

```bash
pytest tests/test_runtime_api.py       # device/admission/journal shapes over fake NVML + fake journal
pytest tests/test_console_joins.py     # join correctness + StateConflict detection
pytest tests/test_console_read_api.py  # gateway read routes + envelope conformance
pytest tests/test_ui_redirects.py      # every retired 021 path resolves (SC-186)
```

Console build and lint:

```bash
cd ui && npm ci && npm run lint && npm run build
```

**Expected**: Ruff clean; offline suite green with **no regression** against the pre-026 baseline
(SC-200); the production build succeeds and reports **no new runtime dependency** — `ui/package.json`
dependencies must still be exactly `next`, `react`, `react-dom` (SC-198).

**Offline mode check (FR-429)**: with no stack running, start the console and confirm the mode badge
reads `offline` and every live surface shows `unknown` with a data age — **not** zeros.

```bash
cd ui && npm run dev        # then open http://127.0.0.1:3000
```

---

## Layer 2 — Live (Compose stack, no GPU required)

```bash
./scripts/gen_secrets.ps1     # once
./scripts/up_all.ps1          # infra + supervised {agent, ui}, waits for all_healthy
export GATEWAY_API_KEY=mll_...
pytest -m live
```

### 2.1 Shell and Overview (US1)

Open `http://127.0.0.1:3000`. Confirm:

- Landing is **Overview**, ten primary areas present with their secondary navigation (FR-362).
- Summary cards populate, **each showing its own data age** (FR-371/430).
- Active-work table shows gateway jobs, agent jobs, and tracking runs in one normalized shape
  (FR-372) — launch a fine-tune and a batch job together to exercise it.
- Health indicator aggregates seven services and opens a per-service panel (FR-369).
- Mode badge reads `live`.

### 2.2 Redirects (SC-186)

Every row of [data-model.md §10](./data-model.md) resolves — no not-found:

```bash
for p in / /serving /data /training /models /monitoring /retraining /infer /datasets /runs /monitor /health; do
  printf '%-14s -> %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -L "http://127.0.0.1:3000$p")"
done
```

### 2.3 Read surfaces

```bash
K="X-API-Key: $GATEWAY_API_KEY"
curl -s -H "$K" localhost:8080/console/health        | head -c 400
curl -s -H "$K" localhost:8080/console/catalog       | head -c 400
curl -s -H "$K" localhost:8080/console/endpoints     | head -c 400
curl -s -H "$K" localhost:8080/runtime/hosts         | head -c 400
curl -s -H "$K" "localhost:8080/runtime/journal?limit=5"
```

Confirm every response carries the `observed` envelope and that `degraded` is `[]` on a healthy
stack.

### 2.4 Degradation matrix (FR-428 / SC-193)

The core resilience proof. Stop each service in turn and confirm the documented behaviour from
[data-model.md §11](./data-model.md):

```bash
docker compose stop mlflow      # tracking → deployments/runtime still work; registry degrades
docker compose start mlflow
docker compose stop garage      # artifact previews disabled; everything else fine
docker compose start garage
docker compose stop prometheus  # current state retained; historical charts gone
docker compose start prometheus
```

Agent loss is the load-bearing case:

```bash
# stop the native agent only (leave Compose up)
curl -s -H "$K" localhost:8080/runtime/hosts
```

**Expected**: `200` with `data: null` and `degraded: ["agent"]`. Runtime reads `unknown` in the
console. **Jobs are NOT reported stopped** — asserting that would be the exact failure FR-428
forbids. An empty `devices: []` here is a **bug**, not a pass: the console would legitimately render
it as "no GPU".

### 2.5 Payload safety (SC-192 / SC-197)

Serve traffic, open a prediction with a captured payload:

- Input and output are **hidden by default**; reveal requires an explicit action (FR-409).
- No payload value appears in any URL — reveal is `POST` with the id in the body.
- No object-store or gateway credential appears in any client-delivered payload:

```bash
curl -s http://127.0.0.1:3000/ | grep -iE 'mll_|AKIA|SECRET|GARAGE|X-Agent-Key' || echo "clean"
```

### 2.6 Conflict disclosure (SC-194)

Induce the gateway-says-running / agent-has-no-process case: start a job, then kill the agent's child
process directly. Open the job.

**Expected**: normalized state `Orphaned`, a conflict banner naming both sources with their
observation times and the last consistent timestamp, and actions to refresh and inspect the journal
(FR-427). A silently-chosen single answer is a failure.

### 2.7 Resident footprint (SC-199 — Principle III)

The constitution caps idle infrastructure at ~3 GB RAM. 026 adds no resident service, so the console
must not move this number — but "must not" is only a claim until measured.

```bash
# idle: stack up, no jobs running, console open on Overview
docker stats --no-stream                     # per-container MEM USAGE column
docker stats --no-stream | awk 'NR>1 {print $4, $5}'   # totals input, sum by hand or with awk
ps -o rss=,comm= -C node | awk '{s+=$1} END {printf "ui(node) RSS: %.0f MB\n", s/1024}'
```

**Expected**: total idle Compose memory unchanged versus the pre-026 baseline within noise, and the
console's own Node process comparable to the 021 console. **Record both numbers in the increment's
runbook** — SC-199 is a constitutional criterion, and an unrecorded measurement is not a pass.

A regression here means a polling or retention defect (the live-fetch layer holding unbounded
history), not a rendering cost — check `use-live.ts` retention bounds first.

### 2.8 Normalized state and gate evidence (SC-190 / SC-191)

```bash
curl -s -H "$K" localhost:8080/console/jobs | head -c 600
```

**Expected (SC-190)**: every row carries a `normalizedState` from the closed vocabulary **and**
retains `gatewayState` / `agentState` / `trackingRunState` — both halves, not one.

Then open a version that failed its gate:

**Expected (SC-191)**: the failing rule, its threshold, the observed value, and the incumbent
compared against are all visible **without leaving the evaluation view** (FR-400).

### 2.9 Datasets, artifact integrity, administration (US8/US9)

```bash
curl -s -H "$K" localhost:8080/console/datasets | head -c 400
curl -s -H "$K" "localhost:8080/console/artifacts?verify=true" | head -c 400
curl -s -H "$K" localhost:8080/console/admin/database | head -c 400
```

**Expected**: datasets show digest, validation status, and referencing runs/models (FR-419);
artifacts distinguish all four integrity states — in particular `not-verified` ("we did not check")
must be distinct from `verification-unavailable` ("no checksum was ever recorded"), never collapsed
(FR-420); the database view lists applied migrations with checksum state and never triggers an apply
(FR-426).

Alerts must carry **no** delivery field (FR-424):

```bash
curl -s -H "$K" localhost:8080/console/alerts | grep -iE 'notified|delivered|recipient|acknowledg' \
  && echo "FAIL: delivery claim present" || echo "ok: no delivery claim"
```

Dashboard fallback (FR-425): block the embed (or set a restrictive frame policy) and confirm the
console renders the external-open fallback rather than an empty frame.

### 2.10 Polling discipline (SC-196)

With a live surface open, hide the browser tab; confirm in the network panel that polling **ceases
entirely** (FR-431), and that on return the surface refreshes before presenting anything as current.

---

## Layer 3 — Hardware `[HW]` (RTX 5070 Ti box)

Cannot be satisfied in a container or from hosted CI. Constitution gate zero.

```bash
RUN_HW_TESTS=1 pytest -m hw
```

### 3.1 Device truth (SC-201)

With an LLM engine resident, compare the console against the agent's own state:

```bash
curl -s -H "X-Agent-Key: $AGENT_API_KEY" localhost:8100/runtime/devices
nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv
```

**Expected**: per-device VRAM, resident engine identity, and `registry_version` displayed by the
console match the agent exactly, and `source` reads `nvml`. `model_identity` must be the
**agent-reported loaded** identity — verify it differs from the desired pointer during an in-flight
activation, which is precisely when the two legitimately diverge.

### 3.2 Contention and refusal (SC-202/187)

1. Start a fine-tune (acquires the GPU as `kind="job"`).
2. Issue a vision classify → expect `409`.
3. Open **Runtime → Admission**.

**Expected**: the refusal reads *"Refused: job `job_…` holds the GPU. A running job is never
preempted."* — the holder and the rule, in prose, not a status code (FR-378). Throughout, the runtime
view shows the correct holder (SC-202).

Then exhaust VRAM instead: request a version whose estimated VRAM exceeds the free block. Expect
reason `vram` and an explanation naming the required amount, the largest free block, and the device.

### 3.3 Compatibility verdicts (SC-188)

For each of the five modalities, open a version's compatibility panel and confirm the verdict
distinguishes **`incompatible`** (structural — unresolvable adapter base, missing artifact) from
**`not-currently-eligible`** (transient — VRAM or a holder). Stop the agent and confirm the verdict
becomes **`unknown`**, never `incompatible`.

### 3.4 Fallback labelling (FR-381)

Force the static-budget path (make the live read fail). Confirm the console labels the values
**fallback-derived** and does not present them as measured.

---

## Acceptance summary

| Layer | Proves | Criteria |
|---|---|---|
| Offline | Builds, no new dependency, joins and redirects correct | SC-186, SC-198, SC-200 |
| Live | Ten areas, degradation, conflicts, payload safety, polling, footprint | SC-184/185, SC-189→197, SC-199, SC-203 |
| Hardware | Device truth, admission explanations, compatibility | SC-187, SC-188, SC-201, SC-202 |

**A green offline+live run is not a complete pass.** SC-201 and SC-202 gate on hardware and remain
open until run on the target machine — flagged, never silently skipped.
