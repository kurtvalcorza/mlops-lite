# Phase 0 Research: 028 Model-Selective Serving

Every finding below was read out of the tree at `master` (`6587608`) rather than recalled. Line
references are to that commit.

---

## R1 — Where resolution lives

**Decision**: In the gateway, in a new `gateway/app/modelresolve.py`. The resolved
`(name, version, modality)` crosses the wire to the agent; the agent never resolves a name.

**Rationale**: The host agent is deliberately **stdlib-only** — `hostagent/metrics.py` opens by
saying so ("no client library in the stdlib-only host process"), and the whole `hostagent/` package
holds no MLflow import. It therefore *cannot* ask which version is promoted. The gateway already owns
that knowledge in `gateway/app/registry.py` (`list_models`, `get_serving`, `_serving_version`,
`_resolve_engine`). Putting resolution anywhere else means either giving the agent a registry client
— breaking the property that keeps it independently restartable when the gateway is down (018's
anti-SPOF point) — or resolving twice.

**Alternatives considered**: (a) Agent-side resolution with a cached copy of the promotion pointer —
rejected: a stale copy silently serves the wrong version, which is a *worse* version of the defect
being fixed. (b) Resolution in `platformlib` shared by both — rejected: `platformlib` is
store/contracts, and the agent still could not reach MLflow.

---

## R2 — How a request is addressed to a specific model on the wire

**Decision**: A `model_key` field on the **existing** engine verb route
(`POST /engines/{engine_id}/infer`), not a new top-level agent route. The agent dispatches to the
child hosting that key; if no such child is resident it returns the placement outcome, never another
model's output.

**Rationale**: `tests/test_console_allowlist.py` asserts the agent's route set, and 024's
[contracts/preservation.md](../024-deepen-modules-seams/contracts/preservation.md) freezes it —
`/control/unload` and `/control/reload` are named there as secret-gated and unchanged. A new
top-level route is a contract amendment with a review cost, for no capability a request field does
not already give. Riding the existing route also means the console allowlist test keeps working as
the tripwire it was built to be, rather than being edited to accommodate this increment.

**Alternatives considered**: (a) `POST /models/{model_key}/infer` — cleaner REST, but a new
top-level resource on a frozen surface. (b) Reusing `POST /control/reload` for tenant-triggered
loads — rejected on a security boundary: that route is `CONTROL_SECRET`-gated and tied to the
promotion pointer; handing tenant traffic a path through it either leaks the secret to the request
path or dilutes what the gate means.

---

## R3 — Per-model VRAM estimate (the genuine gap)

**Finding**: No per-model estimate exists. `hostagent/lifecycle.py:10` documents the adapter
interface as `estimate_vram() -> float  # admission estimate (GB); 0 for CPU engines`, and
`lifecycle.py:111` passes `self.adapter.estimate_vram()` — a **per-adapter constant**. Admission has
never needed to know that model A is 4 GB and model B is 13 GB, because only one LLM was ever
admitted.

**Decision**: Estimate from the model version's serialized artifact size (for the llama.cpp child,
the GGUF file size), times a small configured multiplier for runtime overhead, with a per-model
override available in configuration. The coordinator's existing **post-load reconciliation to a real
per-PID reading** (`coordinator.py` `set_child` / `materialized`) corrects the error, so the estimate
governs *admission*, not accounting.

**Rationale**: For a memory-mapped quantized LLM the on-disk size is a close and always-available
lower bound, and it is already known to the gateway at resolution time from the registry's artifact
metadata. Crucially, the design does not need the estimate to be *right* — it needs it to be
**conservative and reconciled**, and 026 already built the reconciliation.

**Consequence to carry into tasks**: an estimate that is too low is the dangerous direction (it
admits a model that then over-commits the device). The multiplier must be ≥ 1 and the reconciliation
path must be exercised in test with a deliberately under-estimated model.

**Alternatives considered**: (a) Load-and-measure — cannot work: admission decides *whether* to load.
(b) A static per-model table maintained by the operator — rejected as the default (it goes stale
silently), kept as the override.

---

## R4 — Minimum-residency window mechanics

**Findings from source**:

- `ResidentModel.__slots__` is `("model_key", "state", "vram_accounted_bytes", "active_requests",
  "last_used_at", "recency_seq", "child", "materialized")` (`coordinator.py:155`). There is **no**
  field recording *when the model became resident* — `last_used_at` moves on every touch, so it
  cannot serve as the window's start.
- `_select_victims` (`coordinator.py:574`) filters eligibility to
  `r.state == RESIDENT and r.materialized`, sorts `(active_requests > 0, recency_seq)` — idle-first
  then LRU — and returns `[]` when no eligible set satisfies both bounds.
- `_stage1` distinguishes a genuinely oversized model from contention with
  `if est_bytes > capacity - headroom` (`coordinator.py:789`), reached only after
  `_select_victims` has already returned `[]`.

**Decision**: Add `became_resident_at` to `ResidentModel`, and `min_residency_s` to the coordinator
config alongside `safety_headroom_bytes`, `drain_timeout_s`, `job_drain_timeout_s`,
`max_admission_attempts`, and the backoff pair. Extend `_select_victims` eligibility with
`clock() - r.became_resident_at >= min_residency_s`.

**The subtlety that must not be missed**: with the window added, `_select_victims` returns `[]` for
**three** distinct reasons, and today's code can only tell two of them apart —

| why `[]` | correct answer | today |
|---|---|---|
| even evicting everything eligible is not enough | `413 model_too_large` if it exceeds `capacity − headroom`, else `503 gpu_busy` | correct |
| every candidate is `draining`/`evicting`/not materialized | `503 gpu_busy`, generic `Retry-After` | correct |
| **candidates exist and would suffice, but are inside their window** | `503 gpu_busy`, `Retry-After` = **when a sufficient victim *set* becomes eligible** (corrected — see below) | would fall through to the generic path |

So `_select_victims` must report *why* it returned empty — not merely that it did. FR-456a's
computed `Retry-After` is unimplementable otherwise, and a generic backoff here is not a cosmetic
loss: it is the difference between a client that retries once successfully and one that polls.

> **Correction, PR #88 review (finding 2).** This item originally specified `Retry-After` as the
> *"time remaining on the earliest sufficient victim's window"*. **That is wrong whenever a placement
> needs more than one victim.** Eviction is cumulative, so a victim set is unusable until the **last**
> of its members leaves its window; taking the earliest sends the client back too soon, it is refused
> again, and the retry-once-and-succeed property the whole mechanism exists for is lost. The correct
> value is `min over sufficient sets S of (max expiry in S)`, evaluated over the evictor's own greedy
> order so the answer matches what the evictor would actually pick. Worked in
> [contracts/residency-window.md](./contracts/residency-window.md); pinned by T797a, which fails
> against the old rule.

**Alternatives considered**: (a) Reusing `last_used_at` as the window start — rejected, it is
touched on every request, so a busy model would be permanently protected and an idle one immediately
evictable, which inverts the intent. (b) Enforcing the window at the shim rather than the coordinator
— rejected: victim selection is the coordinator's, and a bound enforced outside the lock is a bound
with a race.

---

## R5 — Modality on the listing

**Finding**: `registry.list_models()` (`registry.py:97`) returns only
`{"name": ..., "serving_version": ...}` — no modality. `registry._resolve_engine(c, mv)`
(`registry.py:250`) derives the engine from a model *version*. `GET /v1/models`
(`broker_openai.py:193`) filters on `serving_version` alone, which is why it advertises embedding and
vision models on a surface whose only endpoint is chat.

**Decision (revised)**: Tag each entry with its engine/modality and join residency **from the host
agent**. Do **not** filter per surface.

> **Correction, PR #88 review (finding 3).** The original said "filter per surface". There is only
> **one** `/v1/models` endpoint — verified against the router's decorators — so "the surface's
> modality" names nothing. The listing spans modalities and tags each entry, and FR-461a states that
> a listed id may still be refused `model_wrong_modality` by a given endpoint, which is what the tag
> is for. The same review caught that residency was sourced from `registry.list_models()`; the
> registry knows what is *promoted*, not what is *loaded*, and `gateway/app/routers/infer.py` is
> explicit that the agent is the only component that knows. FR-462/FR-462a move it to the agent and
> require the field omitted when the agent is unreachable.

---

## R6 — Resolution caching

**Decision (revised — see the correction below)**: Split the cache by volatility. `name → modality`
is cacheable with a long TTL. `name → promoted version` is served from cache **only for unpinned
requests**, bounded by a short TTL; a **pinned** request always reads through.

**Rationale**: Resolution is on the per-request path, and an MLflow round-trip per chat request is a
real latency cost for the common case. But a pin is an assertion about the pointer *now*
(FR-457), so answering it from cache can authorize a version already demoted. Splitting puts the
read-through cost on the rare request and keeps the cache for the common one.

> **Correction, PR #88 review (finding 1).** The original version of this item said eager
> invalidation from `registry.promote` meant *"the TTL bounds only the unusual staleness — an
> out-of-band alias move — not the normal one."* **That premise is false.** The repo ships four
> scripts that move the `serving` alias by calling `set_registered_model_alias` directly, none
> through `registry.promote`: `scripts/retag_serving_llm.py:49`, `scripts/seed_asr_model.py:33`,
> `scripts/seed_embedding_model.py:52`, `scripts/seed_tabular_model.py:78`. Out-of-band moves are a
> scripted, ordinary path here, so eager invalidation covers **one of five writers** and the TTL is
> the guarantee, not a backstop. FR-457b/c/d encode the corrected design.

**Bound to state in tasks**: the cache is keyed by model name, which is registry-controlled, not
tenant-controlled. A tenant-supplied string that resolves to nothing must **not** create an entry, or
the cache becomes an unbounded map keyed by attacker input — the same cardinality trap FR-464 names
for metric labels.

---

## R7 — Behaviour when `BROKER_COORDINATOR_ADMISSION` is off

**Finding**: The flag defaults **off** (`coordadmission.py:56`, `os.getenv(..., "0")`), and its
docstring is explicit that with it off "the agent's behaviour is byte-identical to 018's".

**Decision**: Phase 1 must be correct in both positions. With the flag off there is one serving slot,
so resolution still validates the request and still refuses a model that is not the resident one —
the tenant gets a truthful refusal rather than a silent substitution. **This is the whole P1 fix, and
it does not depend on the flag.** Phases 2 and 3 require the flag on.

**Consequence**: the phase-1 test matrix runs the resolution tests in both flag positions. A test
suite that only ever runs with the flag on would leave the default configuration — the one an
operator actually has — unverified.

---

## R8 — What else speaks the old contract

Per the workspace's "name what still speaks the old contract" rule, `settings.SERVING_URL` has four
readers besides the OpenAI router, and each must be checked before that constant stops being the sole
serving address:

| reader | use | disposition |
|---|---|---|
| `gateway/app/serving.py:19` | identity + `/control/reload` client | unchanged — operator/promotion path, not tenant traffic |
| `gateway/app/platform_health.py:15` | health probe | unchanged — probes the engine, not a model |
| `gateway/app/routers/infer.py` | the native `/infer` surface | **must be decided in Phase 2** — it reports `serving_model` from the agent and has the same substitution shape as the OpenAI surface |
| `training/flows/batch_infer.py:44` | batch inference against `SERVING_URL` | **must be decided in Phase 2** — a batch job naming a model has the same problem |

Neither of the last two is in this increment's spec scope, and neither should be silently changed by
it. They are recorded here so the omission is deliberate and visible rather than discovered later.

---

## Residual unknowns

**None blocking.** The two owner decisions (FR-456 thrash bound, FR-457 pinning policy) were taken at
specification time. R3's estimate multiplier and R6's TTL are tunables with defensible defaults, to
be set in configuration and stated in `quickstart.md` — not open questions.
