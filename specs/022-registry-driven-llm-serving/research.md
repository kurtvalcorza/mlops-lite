# Research: Registry-Driven LLM Serving & Operator Model Selection

Decisions R1–R8. The four quirks and their fix were established empirically while driving the live
021 console end-to-end (a fine-tune trained, promoted, and — only via a host-level `.env` override +
manual agent restart — served). This file records the **technical** resolutions behind making the
LLM a registry-driven engine. No NEEDS CLARIFICATION markers remain.

**Reference pattern (confirmed in code):** every non-LLM child is pinned to a model **name** via env
and resolves that name's `@serving` **version's artifact from MLflow on each cold load** —
`serving/children/tabular_service.py:37-79` ("resolves the `@serving` version's object each cold
load so a promotion is picked up after an unload/reload"), `embed_service.py:34`. The `llama.cpp`
adapter (`hostagent/adapters/llama.py:42-49`) is the sole outlier: it loads a fixed GGUF **file
path** (`MODEL`/`LORA` env) and never touches the registry. 022 makes the LLM adopt the same
cold-load resolution, extended to base+adapter.

## R1. How the single active serving-LLM is selected (across model names)

- **Decision**: Introduce a **platform-scoped active-serving-LLM pointer** — one record naming the
  active text-generation **model** (e.g. `qwen2.5-7b-instruct-q4_k_m` or `ops-bot-v2`) — persisted
  in the relational store (018 US4). The console's promote/"set serving" action writes it; the
  engine resolves *that model → its `@serving` version → artifact*. It defaults to the configured
  base model, so today's behavior is unchanged until an operator selects otherwise.
- **Rationale**: The other children pin **one** model name via a build-time env constant
  (`VISION_MODEL`) and only ever promote new *versions* of it. The LLM is different: operators switch
  between **distinct models** (a base vs a fine-tune with its own name), so a single env-pinned name
  cannot express the selection. A platform pointer spans names, is operator-settable at runtime (no
  restart), reuses the per-model gated promote for the *version* choice, and is a single source of
  truth for "which LLM is live."
- **Alternatives considered**: (a) env-pinned name like vision — rejected, can't switch between
  qwen and a differently-named fine-tune. (b) `resolve_serving_target("text-generation")` picks the
  first/most-recent `@serving` text-generation model — rejected, implicit and ambiguous when several
  are promoted. (c) A dedicated MLflow alias on a synthetic "llm" model — rejected, overloads the
  registry alias semantics; the store pointer is simpler and already the home of platform-scoped
  serving state.

## R2. Base + LoRA adapter artifact resolution

- **Decision**: Register local **base GGUFs as first-class full-model text-generation versions**
  (`kind=full-model`, `source=<base gguf>`, `task=text-generation`). A fine-tune version keeps its
  adapter GGUF `source` + a `base_model` lineage tag that **references a registered base**. The
  resolver returns `{base_gguf, adapter_gguf?}` from the registry alone; the engine spawns
  `llama-server -m base [--lora adapter]`.
- **Rationale**: Registry-native and reproducible (Principle VI) — the served configuration
  (base + adapter + version) is recorded, not implied by a filesystem path. Resolution is a pure
  registry read, matching the children.
- **Alternatives considered**: a static base-id→path config map (rejected — drifts from the
  registry, not reproducible); operator hand-enters the base at serve time (rejected — FR-264);
  merge adapter into base at promote time to make a standalone GGUF (rejected — expensive, loses the
  adapter/base separation and the small-artifact benefit; `--lora` at serve time is what llama.cpp
  already supports, per the c28ca97 prototype).

## R3. Where the cold-load resolution lives

- **Decision**: A new `hostagent/serving_llm.py` resolver, called by the `llama.cpp` adapter in
  `spawn()`/`ensure_loaded()`, sets `self.model` (base GGUF) and `self.lora` (adapter GGUF, or None)
  from R1's active pointer + R2's registry lookup **before** launching `llama-server`. This
  generalizes the c28ca97 `LORA` env knob from a static value to a registry lookup.
- **Rationale**: Exactly the children's "resolve `@serving` each cold load" placement
  (`tabular_service.py:79`) — a promotion/selection is picked up on the next controlled (re)load with
  no process restart. The agent runs under the training venv, so it has the MLflow client to resolve.
- **Alternatives considered**: the gateway pushes the resolved artifact to the agent (rejected —
  inverts the established child-resolves-its-own-artifact pattern and duplicates resolution);
  resolve in the gateway and pass a file path in the spawn request (rejected — same inversion).

## R4. Reload-on-select (make a promote take effect live)

- **Decision**: Selecting/promoting the serving LLM issues an **agent reload of the `llm` engine** —
  evict the current holder → load the resolved target — through the existing admission/swap
  (`hostagent/swap.py:preempt_for`). If a **serving** model is resident, the console gates it behind
  the established operator-confirmed swap (021 `ConfirmDialog`, naming the holder). A running
  training/HPO/batch **job is never preempted** — the select is refused/deferred with the existing
  409 reason. If the target is already resident, it is an idempotent no-op.
- **Rationale**: Reuses 017/018 swap wholesale; honors Principle II (sequential, one-tenant); gives
  the operator the "it's live now" feedback the current idle-release-only path can't. The swap
  target-probe (`available()`) already guards a bad artifact from evicting a working holder.
- **Alternatives considered**: wait for idle-release then next-load resolves the new target
  (rejected — the change isn't visibly live; poor operator UX and racy to demonstrate); restart the
  agent (rejected — that's the SSH-level quirk 022 removes).

## R5. Honest served identity

- **Decision**: The agent's `/engines/llm/health` (and the aggregate health) report the **actually
  loaded** `model_name` + `registry_version` (from R3's resolution). The gateway's `serving.gpu_state`
  (`/serving/state`) and prediction logging (`routers/infer.py`, `stream.py`) consume the
  **agent-reported** identity instead of the fixed `SERVING_MODEL` + independently-resolved version.
  The served identity is stamped on each logged prediction so the quality window (013) keys on the
  right model+version.
- **Rationale**: FR-260/261/262 — kills the divergence reproduced live (`model: ops-bot-v2` while the
  gateway logged `registry_model: qwen…`, `serving_version: 23`) that silently mis-attributes a
  served fine-tune's predictions and corrupts its monitoring window.
- **Alternatives considered**: the gateway re-resolves the active pointer itself (rejected — that is
  the current divergence bug: the gateway's belief can lag the agent's actual load, especially
  mid-swap or on a failed reload). The agent is the only component that knows what is truly resident.

## R6. task-tag stamping + legacy backfill

- **Decision**: The LLM fine-tune flow (`training/flows/finetune.py`) stamps `task=text-generation`,
  `serving_engine=llama.cpp`, and base/parent lineage at registration (mirroring what vision/embed
  already tag). A one-time `scripts/backfill_llm_task_tags.py` fixes already-registered untagged
  text-generation versions (identified by `kind=lora-adapter`/`format=gguf`), so `ops-bot-v1/v2` and
  peers become discoverable/selectable.
- **Rationale**: FR-266/267 — closes the `task:null` → "no renderer" gap observed live and the
  recurring seed-without-task-tag issue. A registry record that is correct at write time is better
  than a lazy gateway inference.
- **Alternatives considered**: infer `task` lazily in `resolve_serving_target` from `kind` (kept as a
  **fallback** for defense-in-depth, but not the primary fix — the registry record itself should be
  correct so every consumer, not just the gateway, sees the right task).

## R7. Base availability & the disk budget

- **Decision**: The resolver requires the referenced base GGUF to be **present** (a registered base
  version whose `source` resolves to a local/zoo GGUF). If a promoted fine-tune's base cannot be
  resolved or is missing, the promotion/select is **refused with a clear reason** and the currently
  served LLM is unchanged (FR-265). No base is auto-downloaded.
- **Rationale**: Principle I/III — no surprise network pulls, no busting the disk budget; the base
  zoo is bounded and operator-curated. Fails loud and safe rather than silently downloading GBs.
- **Alternatives considered**: auto-download the base from HF on demand (rejected — Principle I
  offline guarantee + Principle III disk budget); merge-on-promote to avoid needing the base at serve
  time (rejected per R2).

## R8. Testing posture

- **Decision**: Backend gets `pytest` coverage in the existing suite — the resolver (active pointer →
  base+adapter), honest-identity health reporting, promote→reload wiring, task-tag stamping, and the
  backfill (idempotent, non-clobber). The console keeps the 021 posture (`next lint` + `next build`
  type-check; browser quickstart drills). The live swap/serve/identity behaviors and Principle II
  (never two-resident, jobs-never-preempted) get an **on-hardware [HW]** drill on the RTX 5070 Ti,
  mirroring 017/018 swap validation.
- **Rationale**: The backend change is the load-bearing risk and is unit-testable offline against a
  fake registry/store; the GPU-lease and live-serve behaviors are only trustworthy on hardware.
- **Alternatives considered**: introduce a UI e2e harness (rejected for scope, per 021 — recorded as
  a future feature); skip [HW] (rejected — the swap-under-lease is exactly where a Principle II
  regression would hide).
