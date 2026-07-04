# Tasks: 020 Stack Remediation — object-store exit, Bento-ectomy, agent-runtime decision

**Input**: [spec.md](./spec.md) (FR-198..207, SC-127..133) · [plan.md](./plan.md) ·
[research.md](./research.md) (R1–R8) · [data-model.md](./data-model.md) ·
[contracts/](./contracts/) · [quickstart.md](./quickstart.md)

**Numbering**: continues the shared space after 019 (T401+). **[HW]** = needs the operator's
machine (Docker daemon / GPU box); everything else lands offline with the suite green.
Tests are house-mandatory: every behavior task ships its regression in the same phase.

## Phase 1: Setup

- [ ] **T401** Garage infra scaffold (research R2, contracts/store-migration.md §bootstrap):
  `infra/garage/garage.toml` (single node, `replication_factor = 1`, `s3_region` = the
  existing `AWS_DEFAULT_REGION` value, data/metadata on a named volume) +
  `infra/garage/init.sh` (idempotent one-shot: layout assign/apply → 4 buckets → key create →
  per-bucket read+write allow) + `docker-compose.yml`: `garage` service (pinned digest, volume)
  and `garage-init` one-shot — added BESIDE minio (no cutover; the env seam still points at
  minio); `scripts/gen_secrets` + `.env.example` gain the Garage key pair wiring — direction
  REVERSED vs MinIO per contracts §bootstrap: init.sh emits the CLI-minted pair, gen_secrets
  records it into `.env` (idempotent, never re-mints). Validate:
  `docker compose config` clean with both stores defined.
- [ ] **T402** [P] Children/runtime dependency pin (research R5/R7):
  `serving/children/requirements.txt` with the single `fastapi`+`uvicorn` pin (shared by US2
  children and US3's optional agent runtime); documented install line for the host venv.
  No `bentoml` removal yet (that is T412, after the golden gates).

**Phase 2 (Foundational)**: none — the four stories are independent by design (spec §Assumptions);
Setup is the only shared prerequisite (T401 → US1; T402 → US2/US3).

## Phase 3: User Story 1 — maintained object store (P1) 🎯 MVP

**Goal**: platform data on a maintained store; unmaintained component count → 0 (SC-129).
**Independent test**: quickstart §US1 (spike → migrate → cutover → rollback proof → decommission).

- [ ] **T403** [P] [US1] Migration tool: `scripts/migrate_store.py` per
  contracts/store-migration.md — boto3 mirror, idempotent (skip on present+equal-size),
  `--reverse`, streaming, pagination past 1,000 keys, JSON MigrationReport
  (data-model.md shape), exit 0 iff `parity: true`. Tests `tests/test_migrate_store.py`
  (two-FakeS3 seams): copies all, re-run `copied == 0` (SC-127), reverse direction, report
  shape, >1,000-key pagination, size-mismatch re-copy, count+bytes (never ETag) parity.
- [ ] **T404** [US1] **[HW]** Candidate spike (FR-202 gate; quickstart §US1.1, research R2
  checklist): MLflow artifact round-trip incl. one multi-hundred-MB multipart upload;
  `platformlib.store` pagination; `_missing()` 404 discrimination; duplicate-PUT write-once;
  idle RSS at rest recorded (`docker stats --no-stream`, ≥5 min idle — SC-130 gate:
  ≤ incumbent); offline suite + golden flows green against the candidate **via a temporary
  env-seam flip to the empty candidate (flows self-create data), flipped back before
  migration**. Record in `docs/on-hardware-validation-018.md`. A miss ⇒ switch to
  SeaweedFS per R1 and repeat (same plan, same tasks).
- [ ] **T405** [US1] **[HW]** Migrate → cutover → rollback proof → soak (quickstart §US1.2–4;
  FR-199/200): forward run `parity: true`, re-run `copied: 0`; flip the cutover env contract —
  **assert `S3_ENDPOINT_URL` is unset (or flip it in lockstep) and that every host consumer has
  `MLFLOW_S3_ENDPOINT_URL` exported to the Garage host port (`:3900`, not the baked `:9000`
  default); verify the client's *resolved* endpoint moved** (boto3 `client.meta.endpoint_url`), not
  merely that flows pass (contract §cutover, endpoint-precedence note); golden flows + full suite
  pass unchanged (SC-128); one rollback flip proven, then forward again; MigrationReports kept
  under `docs/`.
- [ ] **T406** [US1] **[HW]** Decommission (FR-201; operator confirms FIRST): quiesce writers
  (contract §concurrent-write: stop gateway + agent, or at minimum the policy scheduler +
  prediction/capture logging), then final forward run
  `copied: 0` everywhere; execute the contract checklist (compose services/volumes/digests,
  gen_secrets + .env.example wiring, README/runbook refs, CVE-digest note retired, **and the
  hardcoded `minio`/`:9000` source-default endpoints repointed to Garage or dropped —
  `platformlib/store.py`, `platformlib/s3io.py`, `hostagent/run.sh`, `training/flows/*`, seed
  scripts, `scripts/bootstrap.sh`, `scripts/reseed_registry.sh`, and the `"minio live"`
  health-check string in `tests/test_foundation.py`**); **both** `docker compose config` **and** a
  source-tree `grep -rin 'minio\|:9000'`
  (excluding `specs/`/`docs/` history) have zero live references to the retired store (SC-129);
  stack restarts clean on Garage alone.

**Checkpoint**: SC-127/128/129/130 all recorded; the platform runs with zero unmaintained
components and a proven rollback story (now moot).

## Phase 4: User Story 2 — Bento-ectomy (P2)

**Goal**: same bytes from slim children; `bentoml` out of the venv (SC-131).
**Independent test**: quickstart §US2 per child (capture → swap → replay byte-parity).

- [ ] **T407** [P] [US2] Vision child: `serving/children/vision_service.py` + run script —
  FastAPI multipart `POST /classify` + `GET /readyz`, dynamic-port launch contract unchanged
  (contracts/children-api.md); the torch/torchvision MobileNet code moves verbatim from
  `serving/bento/service.py` (no lease/unload remnants — the agent owns admission).
- [ ] **T408** [P] [US2] CPU children: `serving/children/embed_service.py` (+run) and
  `serving/children/tabular_service.py` (+run) — JSON `POST /embed` / `POST /predict` +
  `/readyz`; sentence-transformers / LightGBM code moves verbatim from the bento siblings.
- [ ] **T409** [US2] Golden tooling: `scripts/capture_goldens.py` per contracts/children-api.md —
  `--engine <e>` capture to `tests/goldens/<e>/` (request, status, content type, body bytes,
  probe) and `--replay` byte-diff at the agent boundary; offline unit tests with a fake child
  (capture/replay round-trip, diff detection on a 1-byte drift).
- [ ] **T410** [US2] Adapter launch flips: `hostagent/adapters/vision.py` +
  `_bento_cpu.py` → `_child_cpu.py` (launch path + `unavailable` pip-hint strings ONLY — the
  adapter contract, verbs, and error vocabulary untouched); offline adapter/contract suites
  updated for the rename and pass unchanged otherwise.
- [ ] **T411** [US2] **[HW]** Per-child golden gates (FR-203; quickstart §US2.1–3): capture
  pre-swap goldens → swap → replay byte-identical, one child at a time (vision on the GPU box;
  embed/tabular anywhere with the venv). Any diff ⇒ revert that child's launch path (old child
  stays on disk until all three pass).
- [ ] **T412** [US2] Retirement (FR-204; only after T411 passes ×3): delete `serving/bento/`;
  remove `bentoml` from the venv requirements; reinstall + `pip check`;
  `pip list | grep -i bento` empty; suite + replayed goldens green; venv package count strictly
  decreased (SC-131).

**Checkpoint**: three slim children serving identical bytes; one framework gone.

## Phase 5: User Story 3 — agent runtime decision (P2)

**Goal**: evidence-recorded runtime choice; streaming baselines met (SC-132).
**Independent test**: quickstart §US3 (dual-runtime drill on hardware).

- [x] **T413** [US3] Dual-runtime switch: `AGENT_RUNTIME=stdlib|uvicorn` in `hostagent/main.py`
  (default `stdlib`) — the ASGI app reuses the framework-free handlers
  (`forward_engine`/`forward_engine_multipart`/jobs/health/metrics/control) with SSE framing,
  `AGENT_BIND`, `X-Agent-Control`, and the error vocabulary preserved
  (contracts/children-api.md §runtime; FR-206); uvicorn imported lazily only when selected.
  Parameterize `tests/test_agent_http.py` (+ the streaming/multipart agent tests) to run the
  SAME assertions on both runtimes — transport drift is a test failure.
- [x] **T414** [P] [US3] Drill tool: `scripts/agent_stream_drill.py` — stream TTFT + stall count
  under concurrent `/health` polling, multipart RTT, mid-stream client disconnect (next request
  clean), preempt-during-stream (409-vs-drain per lease semantics); appends a
  RuntimeBaselineRecord (data-model.md) to the runbook doc; offline smoke against a fake SSE
  child.
- [ ] **T415** [US3] **[HW]** Run the drill on BOTH runtimes (quickstart §US3); record both
  RuntimeBaselineRecords + the FR-205 verdict in `docs/on-hardware-validation-018.md`. Any
  stdlib baseline miss ⇒ flip the default to `uvicorn` and re-run the full agent suite on it;
  no miss ⇒ stdlib stays. Either way the loser's deletion is queued for the next increment
  (research R7 — no permanent dual matrix). Runbook note: the `-018` doc name is deliberate
  (one [HW] session, shared records); **T419 renames it** to
  `docs/on-hardware-validation.md` once both increments' records have landed.

**Checkpoint**: the runtime choice is a recorded measurement, not a default.

## Phase 6: User Story 4 — GPU-budget portability (P3)

- [ ] **T416** [P] [US4] Budget-knob audit + consolidation + regression: repo audit pins that the
  only VRAM-budget literals are `VRAM_GB` env fallbacks — today `"12"` is duplicated across
  `hostagent/main.py`, `hostagent/jobs.py`, and the three adapters (`llama.py`, `vision.py`,
  `whisper.py`); **consolidate them to a single resolver** so no consumer can be left on a stale
  default when the knob moves (FR-207). `tests/test_agent_admission.py` gains the knob test — GPU
  unreadable + budget 16: a **15.0 GB** estimate is admitted, a **15.5 GB** one refused (the
  static-fallback threshold is 16 × 0.95 = **15.2 GB** and moves with the knob — SC-133); a grep
  regression asserts no un-consolidated budget literal remains.
- [ ] **T417** [US4] New-machine bring-up checklist in `README.md` (coordinated with 018's
  T379 refresh so the README is edited once): `VRAM_GB`, native builds
  (llama.cpp / whisper.cpp `build.sh`), CUDA-index torch/torchvision wheels,
  `scripts/gen_secrets`, renamed-host beacon self-heal note.

## Phase 7: Polish & governance

- [ ] **T418** Constitution Principle V **default-stack wording refresh** — operator decision
  (queued with 018's T378; one amendment covers both): the illustrative stack list updates to
  the real components (Garage, MLflow, Prefect-ephemeral, PyTorch+PEFT, llama.cpp/whisper.cpp +
  FastAPI children behind the agent, hand-rolled PSI + Prometheus/Grafana); rule text unchanged.
- [ ] **T419** [P] Env-surface docs: `.env.example` gains `AGENT_RUNTIME` (with the
  decision-window note) and the Garage endpoint/credential block with the cutover-contract
  cross-reference; rename `docs/on-hardware-validation-018.md` →
  `docs/on-hardware-validation.md` (both increments' records; runs LAST, after the [HW]
  session's records land — 020 owns this rename, 018's T379 does not mention it) and update
  the references in 018/020 artifacts. Scope boundary vs T406: ALL MinIO-reference removal
  belongs to T406's decommission checklist; T419 touches only the items named here.

## Dependencies & execution order

```text
Setup: T401 (store infra) ──► US1: T403 [P] → T404 [HW] → T405 [HW] → T406 [HW]   🎯 MVP
       T402 (deps pin) ────┬─► US2: T407 [P] + T408 [P] → T409 → T410 → T411 [HW] → T412
                           └─► US3: T413 + T414 [P] → T415 [HW]
US4: T416 [P] anytime; T417 with 018's T379.  Polish: T418 (operator), T419 [P] last.
```

- **Parallel**: T401 ∥ T402; T403 ∥ T404-prep; T407 ∥ T408; the whole US2 track ∥ US3 track ∥
  US1 track after Setup; T414 ∥ T413; T416 ∥ everything.
- **One hardware session** covers T404/T405/T406 (sequential), T411, T415 — and the still-open
  018 [HW] items (T365 sweep, T372 loop drill): bundle them; the runbook doc is shared.
- **MVP scope**: Phase 1 + US1 — the security-posture item retired independently of everything
  else. Each later story ships alone (Principle VII).
- **Rollbacks** (quickstart table): US1 env flip until T406; US2 per-child launch revert until
  T412; US3 `AGENT_RUNTIME=stdlib` until the loser is deleted next increment.
