# Tasks: Registry-Driven LLM Serving & Operator Model Selection

**Input**: [spec.md](./spec.md) (FR-254..274, SC-143..151) · [plan.md](./plan.md) ·
[research.md](./research.md) (R1–R8) · [data-model.md](./data-model.md) ·
[contracts/](./contracts/) · [quickstart.md](./quickstart.md)

**Numbering**: continues the shared space after 021 (T461+).

**Backend + frontend** (unlike 021): this feature owns the host-agent LLM resolution, the registry
serving-LLM pointer + descriptors, the gateway served-identity reporting, and the console surface.

**Testing posture** (research R8): the Python backend is covered by `pytest` in the existing suite
(offline, fake registry/store) — the resolver, honest-identity reporting, promote→reload wiring,
task-tag stamping, and the backfill. The `ui/` package keeps the 021 gate: `npm run lint` +
`npm run build` (type-check) + the browser quickstart drill. The single-GPU-lease behaviors
(never-two-resident switch, job-never-preempted) get an on-hardware **[HW]** drill. No pre-existing
test may regress — especially the Principle II admission tests.

**[P]** = parallelizable (different files, no incomplete-task dependency).

## Phase 1: Setup (shared, mechanical — unblocks everything)

- [ ] **T461** Add the **ActiveServingLLM** pointer + served-identity persistence to the relational
  store: schema + accessors in `platformlib/store` (get/set the active text-generation `model_name`,
  with `selected_at`/`selected_by`; unset ⇒ resolve to the configured default base). Per
  [data-model.md](./data-model.md). Validate: get/set round-trips; a fresh store resolves to the
  default base; `pytest` covers the accessor.
- [ ] **T462** [P] `scripts/register_base_gguf.py` — register the local base GGUFs (Qwen 0.5B / 7B in
  `~/models/gguf/`) as `kind=full-model` text-generation registry versions (`task=text-generation`,
  `serving_engine=llama.cpp`, `source` → the local GGUF), so a fine-tune's `base_model` lineage
  resolves to a first-class registry record (research R2/R7). Idempotent (re-run registers nothing
  new). Validate: bases appear in `GET /models` as text-generation full-model versions with a
  resolvable local source.

## Phase 2: Foundational (blocking prerequisite for resolution + the live switch)

- [ ] **T463** `hostagent/serving_llm.py` — the cold-load resolver (research R1/R2/R3): given the
  ActiveServingLLM pointer, resolve its `@serving` version → `{model_name, version, base_gguf,
  adapter_gguf|null}` per [contracts/serving-resolution.md](./contracts/serving-resolution.md):
  `full-model` → `base_gguf=source`; `lora-adapter` → `adapter_gguf=source` + `base_gguf=resolve(base_model
  → registered base version.source)`. Raise a clear resolution error on missing `@serving`/artifact/
  base. Pure read of {store pointer, MLflow}; no network fetch. Unit-test against a fake registry/store
  (full-model, adapter, missing-base, unset-pointer→default). Blocks US1/US3.
- [ ] **T464** Gateway `registry.py` — active-serving-LLM pointer get/set (store-backed, via T461);
  `resolve_serving_target` stamps/reads `task` for LLM versions and resolves an adapter's base; expose
  a version's base-vs-adapter `kind` + lineage in the models/tasks surfaces. Unit-test the resolution +
  pointer round-trip. Blocks US1/US4.

---

## Phase 3: User Story 1 — Promote an LLM and it actually serves (P1) 🎯 MVP

**Goal**: promoting a text-generation version from the console changes the live served LLM via a
controlled reload — no `.env` edit, no manual restart (FR-254/255).
**Independent test**: [quickstart.md](./quickstart.md) §US1.

- [ ] **T465** [US1] `hostagent/adapters/llama.py` — call the T463 resolver in `spawn()` /
  `ensure_loaded()` to bind `self.model` (base) + `self.lora` (adapter, or None) from the registry
  before launching `llama-server` (spawn already appends `--lora` when set — commit `c28ca97`). This
  **supersedes** the static `MODEL`/`LORA` env knob (env becomes a fallback/default only). Also set
  the adapter's **response identity** (`self.alias` / the `model` field echoed by `forward()` and
  `stream()`) to the resolved `model_name` (+version) on each load — not the static `MODEL_ALIAS` env
  — so the `/infer` response's `model` matches the registry record (FR-262; spec review PR #64 §2).
  `available()` verifies the resolved artifacts. Validate: with a promoted version, a cold load serves
  it and the response `model` names it; `pytest` covers resolve→spawn arg construction + the alias.
- [ ] **T466** [US1] Reload-on-select under admission — TWO cases (admission tenancy is per-engine,
  not per-model; spec review PR #64 §1): **cross-tenant** (non-LLM/job holder) reuses
  `hostagent/swap.py:preempt_for` (evict → free → load); **same-tenant model switch** (the `llm`
  engine already resident with a *different* model — where `preempt_for` is a no-op) performs an
  explicit **force-reload: unload the `llm` child → `ensure_loaded()`** so it re-spawns with the newly
  resolved artifact (FR-255 immediate / SC-144). Idempotent no-op only when the resolved
  model+version is already resident; target-probed before any unload/evict (FR-256/257). `pytest`
  covers the same-tenant force-reload path (promote B while A resident → B actually loads).
- [ ] **T467** [US1] Make the **gated promote itself the go-live action** (Clarifications 2026-07-05:
  promote = go live, one action — no separate "select" gesture): promoting a text-generation version
  through `POST models/:name/promote` (011/015) MUST also write the ActiveServingLLM pointer (T464) and
  request the T466 immediate agent reload. Prefer extending the existing promote path over a new
  serving-control route; add one only if the contract requires it
  ([contracts/agent-identity-and-allowlist.md](./contracts/agent-identity-and-allowlist.md)).
- [ ] **T468** [US1] Validate US1 end-to-end against quickstart §US1 (promote A → `/infer` is A;
  promote B → `/infer` is B; no host-level action); backend `pytest` (resolver + reload wiring) green.

**Checkpoint**: US1 alone makes the LLM console-operable — the core value.

---

## Phase 4: User Story 2 — Honest served-model identity (P1)

**Goal**: `/serving/state`, the inference response, and every logged prediction name the model+version
actually resident, so monitoring scores the right model (FR-260/261/262).
**Independent test**: quickstart §US2.

- [ ] **T469** [US2] Agent reports the actually-loaded `model_name` + `registry_version` (from T463
  resolution) in `/engines/llm/health` and the aggregate health
  ([contracts/agent-identity-and-allowlist.md](./contracts/agent-identity-and-allowlist.md)).
- [ ] **T470** [US2] Gateway `serving.py gpu_state` (`GET /serving/state`) consumes the agent-reported
  `model_name` + `registry_version` instead of the fixed `SERVING_MODEL`; degrades to `unknown` when
  the agent is unreachable (never a stale config guess).
- [ ] **T471** [US2] Prediction logging in `gateway/app/routers/infer.py` + `stream.py` attributes each
  served prediction to the agent-reported identity (not `SERVING_MODEL`), so the 013 quality window
  keys on the correct model+version. No prediction logged under a model the agent is not serving.
- [ ] **T472** [US2] Validate US2 against quickstart §US2 (serving-state + `/infer` `model` + logged
  prediction all agree; a quality check scores the right model+version — the live divergence bug is
  gone); `pytest` identity tests green.

---

## Phase 5: User Story 3 — Serve LoRA fine-tunes (base + adapter) (P2)

**Goal**: promoting a LoRA fine-tune serves its trained behavior (adapter on its resolved base);
back to a base restores base behavior (FR-263/264/265).
**Independent test**: quickstart §US3.

- [ ] **T473** [US3] Adapter path in the T463 resolver: `lora-adapter` version → `base_model` lineage
  → registered base version `source` → spawn `-m <base> --lora <adapter>`. Unit-test the base
  resolution + arg construction (adapter, chained-parent, base-is-another-registered-version).
- [ ] **T474** [US3] Refuse-on-unresolvable-base (FR-265): a resolution error (missing/absent base or
  artifact) refuses the promote/select with a clear reason and leaves the currently-served LLM
  unchanged — never a wedged/empty serving state. `pytest` covers the refusal.
- [ ] **T475** [US3] Validate US3 against quickstart §US3 (promote fine-tune → trained behavior serves;
  promote base → gone; unresolvable base → refused, unchanged); `pytest` green.

---

## Phase 6: User Story 4 — Fine-tunes are first-class serving targets (P2)

**Goal**: newly-registered LLM fine-tunes are task-tagged + lineage-carrying and render as real,
selectable LLM panels; legacy untagged versions are backfilled (FR-266/267/268/270).
**Independent test**: quickstart §US4.

- [ ] **T476** [US4] `training/flows/finetune.py` — stamp `task=text-generation`,
  `serving_engine=llama.cpp`, and base/parent lineage at registration (mirrors vision/embed tagging),
  so a new fine-tune registers with a non-null task. `pytest` covers the registration tags.
- [ ] **T477** [P] [US4] `scripts/backfill_llm_task_tags.py` — backfill legacy untagged
  text-generation versions (identified by `kind=lora-adapter`/`format=gguf`) with `task`/`serving_engine`;
  idempotent + non-clobber (never overwrites an existing tag). Validate on `ops-bot-v1/v2`: they become
  `task=text-generation`; re-run is a no-op.
- [ ] **T478** [P] [US4] Gateway resolve fallback: infer `task` from `kind` (lora-adapter/gguf →
  text-generation) as defense-in-depth in `resolve_serving_target`/`serving/tasks`, so no valid LLM
  version is stranded as `task:null` even before the backfill runs.
- [ ] **T479** [US4] Surface a version's base-vs-adapter `kind` + lineage in `GET /serving/tasks` and
  the models detail so the console renders a working LLM panel (not "no renderer") and shows what would
  be promoted (FR-268/270). **Also make `serving/tasks` authoritative to the active-serving-LLM
  pointer (FR-276; spec review PR #64 §3):** `registry.list_tasks()` emits one row per model with ANY
  promoted `@serving` alias, and a promote only moves *that model's* alias (a previously-promoted LLM's
  stale `@serving` alias is left set), so two text-generation models can both appear. The
  text-generation serving target MUST be filtered/preferred to the **active pointer's** model (mirror
  `resolve_serving_target`'s existing `prefer_name` disambiguation) so the console shows exactly one
  live LLM, no stale duplicate. `pytest` covers the two-promoted-LLMs dedup.
- [ ] **T480** [US4] Validate US4 against quickstart §US4 (new fine-tune task-tagged; backfill
  idempotent + selectable; promoted fine-tune renders a working panel); `pytest` green.

---

## Phase 7: User Story 5 — Safe switch under the single-GPU lease (P2/P3) [HW]

**Goal**: switching the served LLM honors Principle II — sequential reload, operator-confirmed
preempt of a serving holder, job never preempted (FR-257/258/259).
**Independent test**: quickstart §US5.

- [ ] **T481** [US5] Switch under admission: displacing a resident **serving** holder goes through the
  operator-confirmed swap; a **job** holder (training/HPO/batch) is refused/deferred with the existing
  409 reason (never preempted); never two-resident. Reuses `hostagent/swap.py:preempt_for`;
  `pytest`/agent-level test the refuse-if-job path.
- [ ] **T482** [US5] Console switch surface (`ui/components/models/*`, `ui/components/serving/*`): the
  **promote action IS the switch** (Clarifications 2026-07-05 — no separate "set serving" control);
  when promoting a text-generation version would displace a resident serving model, gate it behind the
  021 `ConfirmDialog` naming the holder. Show the **resident-vs-promoted** LLM delta (FR-269) and the
  base/adapter nature + lineage (FR-268); reflect a completed switch. Validate: `npm run build` green.
- [ ] **T483** [P] [US5] `ui/lib/gw-allowlist.ts` — add any new serving-control route the switch UI
  calls (prefer reusing existing entries; keep the delta minimal and equal to
  [contracts/agent-identity-and-allowlist.md](./contracts/agent-identity-and-allowlist.md)).
- [ ] **T484** [US5] [HW] Validate US5 on the RTX 5070 Ti (quickstart §US5): the never-two-resident
  switch (watch `serving/state` + `nvidia-smi` across evict→load, SC-147); a switch refused while a
  training job holds the GPU with the job completing uninterrupted (SC-150); the idempotent no-op.

---

## Phase 8: Polish & cross-cutting

- [ ] **T485** [P] Full backend regression + boundary guards: `pytest` green across the resolver,
  identity reporting, reload wiring, task-tag stamping, and backfill; confirm **no pre-existing test
  regressed** — especially the Principle II admission/swap tests (SC-147/149). Add two boundary
  checks: (a) **FR-273** — assert the feature adds **no new always-on resident process** and stays
  within the VRAM/RAM/disk budget (Principle III); (b) **FR-275** — assert the auto-promote/retraining
  policy path **cannot** switch the served LLM (served-LLM switching is operator-initiated only).
- [ ] **T486** [P] Console gate: `npm run lint` + `npm run build` green; allow-list conformance grep —
  every gateway call in `ui/` resolves to a `gw-allowlist.ts` entry and the delta equals the contract
  (ideally empty — reuse preferred) (FR-272).
- [ ] **T487** [P] Docs: update the README serving section — the LLM is now registry-driven like the
  other engines (promote → serve), base+adapter serving, and how to select the serving LLM; note the
  `LORA`/`MODEL` env knob (commit `c28ca97`) is now a fallback default, superseded by registry
  resolution.
- [ ] **T488** Constitution/agent-context: confirm **no amendment required** — Principle II's RULE is
  unchanged (the switch reuses the existing one-tenant swap; this feature only chooses *which* model
  serves). Keep the managed `CLAUDE.md` marker on the 022 plan.
- [ ] **T489** [HW] Full-loop drill on the RTX 5070 Ti: data → train → promote → **serve live** →
  monitor for a fine-tune, entirely through the console (the exact flow hit manually this session,
  now hands-off) — proves all four quirks are closed and the quality window scores the served
  fine-tune correctly.

## Dependencies & order

- **Setup (T461–T462)** unblocks everything; **Foundational (T463–T464)** — the resolver + registry
  pointer — unblocks every user story.
- **US1 (T465–T468)** depends on Setup+Foundational; it is the MVP.
- **US2 (T469–T472)** depends on Foundational (needs the resolver's identity); strongly complements
  US1 (P1 pair) — together they are the correctness-complete MVP.
- **US3 (T473–T475)** depends on Foundational (base resolution in T463) + US1 (a promoted LLM serves).
- **US4 (T476–T480)** depends on Foundational (task descriptors); independent of US1–US3 otherwise
  (registration/backfill/discoverability).
- **US5 (T481–T484)** depends on US1 (the switch) + the 021 ConfirmDialog; the [HW] drill is last.
- **Polish (T485–T489)** last.

## Parallel execution examples

- After T461: **T462** runs in parallel (distinct file/script).
- Within US4: **T477, T478** parallel (backfill script vs gateway fallback — distinct files).
- Within US5: **T483** (allow-list) parallels **T482** (UI) once the route names are fixed.
- In Polish: **T485, T486, T487** parallel (backend suite / UI gate / docs — distinct surfaces).
- Whole stories **US4** (registration/backfill/discoverability) can proceed in parallel with **US1–US3**
  once Foundational lands, since it owns the registration + tagging surfaces, not the serve path.

## Implementation strategy

- **MVP = US1 + US2** (the P1 pair): promoting an LLM from the console makes it serve, with honest
  identity so its predictions are attributed correctly. Shipping T461–T472 already removes the
  SSH-level workaround and fixes the monitoring-corruption bug.
- Then **US3** (serve LoRA fine-tunes — makes a trained fine-tune usable live) and **US4** (make every
  fine-tune discoverable/selectable), then **US5** (harden the switch under contention, [HW]), then
  Polish + the full-loop [HW] drill.
- Every phase ends green on `pytest` (backend) + `lint`/`build` (console) and its quickstart drill; no
  phase relaxes Principle II — the switch is always the existing one-tenant controlled reload.
