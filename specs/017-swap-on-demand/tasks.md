# Tasks: Swap-on-Demand (preemptive GPU lease for serving)

**Input**: Design documents from `specs/017-swap-on-demand/` (spec.md, plan.md, research.md, data-model.md,
contracts/unload-now-endpoint.md, contracts/preempt-flag.md, quickstart.md).

> **Status (2026-06-30):** **BUILT (offline) — on-hardware SCs + T342 governance pending the operator.**
> All code + offline unit tests landed and green (no regression). On-hardware SCs (SC-100/102/103/104, the
> real GPU swap / `nvidia-smi` one-model checks / live drain / UI end-to-end) and the `tsc`/`next build`
> UI check run on the RTX 5070 Ti box. **T342 (constitution wording) is NOT done** — it needs operator
> confirmation (research.md D6); the code is constitution-compliant as-is (Principle II preserved). IDs
> continue the shared space (T324+). **Independent of 015/016.**

## Format: `[ID] [P?] [Story] Description`

- **[P]** = parallelizable (different files, no dependency). `[USx]` maps to the spec's user stories.

---

## Phase 1: Setup — config

- [x] **T324** Add config to `.env.example` + readers: `SWAP_DRAIN_TIMEOUT_S` (per-supervisor drain bound)
  and confirm the `preempt` request flag defaults **false**. Document that default = 008 refuse-if-held.

---

## Phase 2: Foundational — `unload-now` endpoints + holder-kind (BLOCKS US1)

- [x] **T325** Add `POST /unload-now` to `serving/llama/supervisor.py`: unload the model (reuse `_unload`)
  + `gpu_lease.release(LEASE_TENANT)`; idempotent (`idle` if not resident); authenticated like other
  control routes. *(Basic unload here; US3 adds drain.)*
- [x] **T326** [P] Add `POST /unload-now` to `serving/whispercpp/supervisor.py` (same shape).
- [x] **T327** [P] Add `unload-now` to `serving/bento/service.py` (vision — a lease tenant since 008; reuse
  its idle-release/unload path).
- [x] **T328** Gateway **holder-kind** resolution: from the lease / `/serving/state`, expose whether the
  current holder is a **serving** tenant (preemptable) vs **training/HPO/batch** (not) — the orchestrator
  needs this for US2.

**Checkpoint**: the gateway can unload any serving holder and tell serving holders from training holders.

---

## Phase 3: User Story 1 — operator swaps a resident serving model (P1) 🎯 MVP

**Goal**: `preempt=true` evicts a resident *serving* model and loads the target — sequential, one model in
VRAM (FR-154/157/158).

**Independent Test**: LLM resident + `preempt=true` classify → LLM evicted, vision loads + returns; default
(no preempt) still refuses; `nvidia-smi` one-model.

- [x] **T329** [P] [US1] `tests/test_swap_orchestration.py` — gateway swap: resolve holder → `unload-now` →
  wait-for-free → forward; concurrent `preempt=true` serialize; default (no preempt) = refuse-if-held
  (mock daemons + lease).
- [x] **T330** [US1] Add the optional **`preempt`** field (default false) to the serving request models in
  `gateway/app/routers/{infer,stream,vision,transcribe}.py` (+ `serving.py`).
- [x] **T331** [US1] Gateway **swap orchestrator** (`gateway/app/serving.py` or a small helper): on
  `preempt=true` + a serving holder → `POST /unload-now` to the holder → wait until the lease frees
  (`current_holder()` clears) → forward to the target daemon. One model in VRAM (FR-158).
- [x] **T332** [US1] Target-load-failure handling: if the target fails to load after eviction, surface the
  load error (FR-159); a holder death mid-swap is covered by 008's stale-reclaim.
- [x] **T333** [US1] UI (FR-160): replace the Infer tab's 008/A1 "classify disabled — GPU busy" with a
  cost-stating **"Swap & classify"** confirm that sends `preempt=true`; pass the flag through
  `ui/lib/gw-allowlist.ts`.
- [ ] **T334** [US1] On-hardware: LLM resident → `preempt=true` classify swaps; default refuses;
  `nvidia-smi` never two resident (**SC-100, SC-101, SC-104**).

**Checkpoint**: operator-confirmed serving swap works end-to-end.

---

## Phase 4: User Story 2 — training is never preempted (P1, guardrail)

**Goal**: a `preempt=true` serving request against a training/HPO/batch holder is **refused**; the job is
untouched (FR-155).

**Independent Test**: training holds the lease + `preempt=true` serving request → `409` "not preemptable";
the run completes unaffected.

- [x] **T335** [P] [US2] `tests/test_no_preempt_training.py` — training holder + `preempt=true` ⇒ refused;
  no `unload-now` is sent to a training holder (mock).
- [x] **T336** [US2] In the swap orchestrator (T331): if holder-kind (T328) is training/HPO/batch → **refuse
  with `409 { "detail": "training in progress — not preemptable" }`** — never send `unload-now` (FR-155).
- [ ] **T337** [US2] On-hardware: start a fine-tune → `preempt=true` serving request is refused; the
  fine-tune **completes unaffected** (**SC-102**).

**Checkpoint**: long-running GPU work can never be evicted by a swap.

---

## Phase 5: User Story 3 — in-flight requests drain, not dropped (P2)

**Goal**: `unload-now` drains in-flight request(s) within a bounded timeout before unloading (FR-156).

**Independent Test**: long inference on the holder + `unload-now` → the inference completes (or is cut only
past the timeout) before unload.

- [x] **T338** [P] [US3] `tests/test_unload_now_drain.py` — `unload-now` waits for an in-flight request then
  releases; past `SWAP_DRAIN_TIMEOUT_S` it hard-unloads (mock in-flight).
- [x] **T339** [US3] Add **in-flight detection** (an active-request counter or equivalent) to each serving
  supervisor and make `unload-now` **drain** (T325–327) up to `SWAP_DRAIN_TIMEOUT_S`, then hard-unload.
- [ ] **T340** [US3] On-hardware: a mid-request swap drains the in-flight request before the swap proceeds
  (**SC-103**).

**Checkpoint**: swaps don't silently drop in-flight work.

---

## Phase 6: Polish & cross-cutting

- [x] **T341** [P] Docs: README (the `preempt` flag + Swap & classify) + update the 008 GPU-lease section
  (cooperative + operator-confirmed preemptive serving); flip spec/plan/tasks Status → BUILT.
- [ ] **T342** **Constitution**: resolve the v1.4.x wording question (research.md D6) — refresh the "no
  swap/evict" line to note operator-confirmed serving preemption (rule unchanged) **or** leave historical;
  if refreshing, run the constitution flow + re-ratify. **Confirm with the operator first.**
- [x] **T343** No-regression: full **001–016** suite green (GPU-tenant tests in isolation); confirm the
  **default (non-preempt) path is byte-for-byte 008** (**SC-105 / SC-101**).
- [x] **T344** [P] Confirm **no new dependency / service** (reuses each supervisor's `_unload` + the 008
  lease); update the deps note.
- [ ] **T345** PR + **dual-bot review loop** (`@claude` + `@codex`) → fix → merge when clean.

---

## Dependencies & Execution Order

- **Phase 1 (config)** → no deps.
- **Phase 2 (`unload-now` + holder-kind)** → after Phase 1; **BLOCKS US1**.
- **US1 (Phase 3)** → after Phase 2. The MVP — the core swap.
- **US2 (Phase 4)** → after US1's orchestrator exists (adds the training refusal to it).
- **US3 (Phase 5)** → after Phase 2 (refines `unload-now` to drain); independent of US1/US2 logic.
- **Polish (Phase 6)** → after the user stories. T342 (constitution) needs operator confirmation.

### Parallel opportunities

- T326/T327 (whisper/vision `unload-now`) in parallel with T325's pattern; test files
  T329/T335/T338 in parallel with their targets stubbed.

## Notes

- **One model in VRAM at any instant** — the swap is sequential evict→free→load (Principle II / FR-158).
- **Training is never preempted** (FR-155) — refuse, never `unload-now` a training holder.
- **Default (no `preempt`) is byte-for-byte 008** (FR-161) — preserve refuse-if-held exactly.
- `unload-now` is **destructive** → authenticate it like other control routes.
- Validate GPU-tenant tests **in isolation** (the single lease serializes them — expected).
