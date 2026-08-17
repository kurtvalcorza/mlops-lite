# Phase 1 Data Model: 028 Model-Selective Serving

No database schema changes. Every entity below is either in-process state or a wire shape.

## Terminology

**"Modality" and "engine" name the same axis** and are used interchangeably across these artifacts
and the code. The spec-side word is *modality* (what kind of work: `llm`, `vision`, `embed`, `asr`);
the code-side word is *engine* (`engine_id`, `registry._resolve_engine`, `/engines/{engine_id}`).
There is no distinction — one is the user's vocabulary and one is the implementation's
(`/speckit-analyze` finding L1). Where a document must pick one, prose uses *modality* and anything
naming a symbol uses *engine*.

---

## ResolvedModel *(new — gateway, in-process)*

What a request's `model` string becomes before anything else happens. Produced by
`gateway/app/modelresolve.py`, carried to the agent, and reported back in the response.

| field | type | notes |
|---|---|---|
| `name` | str | the registered model name; the registry is the authority |
| `version` | str | the promoted serving version at resolution time |
| `modality` | str | `llm` \| `vision` \| `embed` \| `asr` — from `registry._resolve_engine` |
| `model_key` | str | the identity admission keys on. **`name@version`** — see below |
| `pinned` | bool | true when the request carried an explicit version qualifier |
| `resolved_at` | float | for staleness reporting, not for correctness |

**`model_key` is `name@version`, not `name`.** Two versions of one model are two different sets of
weights with two different VRAM footprints; keying residency on the bare name would let a promotion
silently change what a resident `model_key` refers to, which is the same class of defect this
increment closes one level up.

**`model_key` is constructed, never parsed from tenant input.** It is built from an
already-resolved `(name, version)` pair, so no ambiguity from a name containing `@` or `:` can reach
it. The user-facing qualifier has its own grammar (FR-440a): split on the **rightmost** `:`, accept
the suffix as a version only when it is all decimal digits, otherwise treat the whole string as a
bare name. Registry versions are integers — `gateway/app/registry.py` sorts them with `int(...)` —
which is what makes that rule total.

An earlier revision of this document said the `@` separator was chosen "because MLflow model names
permit `:` in practice and the OpenAI `model` string uses `:` as the user-facing qualifier". That
asserted the ambiguity and then did not resolve it; FR-440a resolves it and FR-440b refuses the one
name shape that stays ambiguous under it.

**Validation** (FR-439–FR-445):

| condition | outcome |
|---|---|
| name matches no registered model | refuse `model_not_found` (permanent) |
| name registered, no promoted serving version | refuse `model_not_promoted` (permanent) |
| promoted, but modality ≠ the endpoint's | refuse `model_wrong_modality` (permanent) |
| qualifier names a version that does not exist | refuse `model_version_not_found` (permanent) |
| qualifier names a real version that is not promoted | refuse `model_version_not_promoted` (permanent), naming the promoted version (FR-457a) |
| `model` absent or empty | resolve the default for the **calling endpoint's modality**; never a fallback from a failed resolution (FR-444) |
| registry unreachable | refuse; **never** fall through to the modality slot (FR-445) |

---

## ResidentModel *(existing — `hostagent/coordinator.py:151`, one field added)*

Current `__slots__`: `model_key`, `state`, `vram_accounted_bytes`, `active_requests`,
`last_used_at`, `recency_seq`, `child`, `materialized`.

| added field | type | notes |
|---|---|---|
| `became_resident_at` | float | set once, when the entry reaches `resident`; **never** touched afterwards |
| `host_ram_accounted_bytes` | float | the child's attributed host RAM (PSS basis, FR-471). 0 while `loading` — the reservation carries the estimate during that window, exactly as `vram_accounted_bytes` does |

**`host_ram_accounted_bytes` is not optional bookkeeping.** FR-470's equation sums "accounted host
RAM of resident serving children", and until this field exists that sum has nothing to read.
`ResidentModel` today carries `vram_accounted_bytes` and no host-RAM analogue, so the equation was
stated against state that did not exist — it described an intent, not a computation.

The distinction from `last_used_at` is the point. `last_used_at` moves on every request, so using it
as the window start would protect a busy model forever and expose an idle one immediately — the
inverse of the intent. `became_resident_at` is written once and read only by eligibility.

It is set at the transition into `resident`, not at entry creation: an entry is created in `loading`,
and a model that spent 40 s loading has not yet been *available* for 40 s. Starting the window at
creation would let a slow load consume its own protection.

**State transitions** are unchanged from 026: `loading → resident → draining → evicting`, with
`rolling_back` on a failed load. The window affects only *eligibility for eviction*, never the
transitions themselves.

---

## Reservation *(existing — `hostagent/coordinator.py:193`, one field added)*

Current `__slots__`: `op_id`, `model_key`, `est_bytes`, `generation`, `materialized`, `waiters`.

| added field | type | notes |
|---|---|---|
| `host_ram_est_bytes` | float | the incoming child's host-RAM estimate (FR-469), held for the same window `est_bytes` holds the VRAM estimate |

`est_bytes` is VRAM-only today, so "Σ outstanding host-RAM reservations" in FR-470's equation had no
field behind it either. Both sides of that sum now have state.

Unlike the VRAM reservation, this one is **not** deducted from any live-free figure — there is no
host-RAM analogue of that bound (FR-470), so `materialized` governs only the VRAM side.

---

## VictimSelection *(new — return shape of `Coordinator._select_victims`)*

`_select_victims` currently returns `list[ResidentModel]`, with `[]` overloaded to mean three
different things (research R4). It returns a small record instead:

| field | type | notes |
|---|---|---|
| `victims` | list[ResidentModel] | non-empty when a sufficient set was found |
| `blocked_by` | str \| None | `None`, `"insufficient"`, `"transient"`, or `"residency_window"` |
| `retry_after_s` | float \| None | set only for `residency_window`: `min over sufficient sets S of (max expiry in S)` — when a **sufficient set** first becomes eligible, **not** when the earliest protected victim does. See [contracts/residency-window.md](./contracts/residency-window.md) |

This is what makes FR-456a implementable. Without `blocked_by`, a request blocked purely by the
window is indistinguishable at the call site from one blocked because the GPU is genuinely too small,
and the client is handed a generic backoff instead of the one number that would make its retry
succeed.

`retry_after_s` is a *lower* bound on when the request could succeed — it says when the window
expires, not when the model will actually be evicted. That is the honest claim; promising the
eviction would be promising something no other tenant's traffic has agreed to.

---

## CoordinatorConfig *(existing — one tunable added)*

Alongside `safety_headroom_bytes`, `drain_timeout_s`, `job_drain_timeout_s`,
`max_admission_attempts`, `admission_backoff_base_s`, `admission_backoff_cap_s`:

| added | type | default | notes |
|---|---|---|---|
| `min_residency_s` | float | see `quickstart.md` | 0 disables the window and restores exact 026 eviction behaviour — which is what the Phase-2 characterization tests set it to |
| `host_ram_budget_bytes` | float | calibrated by T817 | FR-467/FR-470. Platform RAM allowance less the measured idle-infrastructure baseline |
| `host_ram_estimate_bytes` | float | per-adapter default + per-model override | FR-469. The incoming child's estimate — mirrors the existing per-adapter `estimate_vram()` |

### The host-RAM bound, stated completely

An earlier revision named a budget but defined neither the incoming estimate nor what is summed
against it, which left FR-467 with no left-hand side. Both are now fixed:

```text
Σ accounted host RAM of resident serving children
  + Σ outstanding host-RAM reservations
  + host_ram_estimate_bytes(incoming)
  ≤ host_ram_budget_bytes
```

**One bound, not two.** The VRAM rule has a usable-budget bound *and* a live-free bound. Host RAM has
only the budget analogue: there is no device-reported "free host RAM" figure that is meaningful across
memory-mapped children, so a second bound would be arithmetic on a number that does not mean what it
appears to (FR-470).

**Measured as PSS, never RSS** (FR-471). The llama.cpp child `mmap`s its GGUF. Two children sharing
page-cache pages each report those pages in full, so summing RSS over-counts real usage and a bound
built on it would refuse placements that comfortably fit. Proportional set size attributes each
shared page once. Where PSS is unavailable the fallback subtracts shared file-backed pages and is
**recorded as a fallback**, because the two are not interchangeable.

**Why it is a precondition and VRAM is reconciled.** The coordinator admits on a VRAM estimate and
corrects it against a real post-load reading, because an over-estimate merely refuses a load that
would have fit. Host RAM has no such symmetry: once a child has allocated it, the coordinator cannot
reclaim it — no unload-and-retry gives it back within the request. So the check refuses *before* the
spawn, and refuses transiently (`gpu_busy`), never permanently.

**It is still reconciled afterwards** (FR-472) — not to help the request that just ran, which is
past helping, but so the *next* admission decides against a measured number instead of an estimate
already proven wrong.

Documented in `.specify/memory/hardware-profile.md` with the other admission tunables, since that is
where 026's tunables were recorded after the Codex review.

---

## ModelListingEntry *(existing shape — `GET /v1/models`, enriched)*

Today: `{id, object, owned_by, version}`, filtered on `serving_version` alone.

**`GET /v1/models` is a single global endpoint.** `gateway/app/routers/broker_openai.py` declares
exactly one, alongside `POST /chat/completions`, `POST /embeddings`,
`POST /audio/transcriptions`, `POST /vision/{task}`, and `GET /usage`. An earlier revision of this
document said the listing is "filtered to the surface's modality" — **there is no per-surface
listing for that to mean anything against**. The listing spans modalities and tags each entry
instead (FR-461).

| field | change | source |
|---|---|---|
| `id` | unchanged — the registered model name | registry |
| `version` | unchanged — the promoted serving version | registry |
| `modality` | **added**; tags the entry, does not filter the listing (FR-461) | registry (`_resolve_engine`) |
| `resident` | **added**; whether it is currently in VRAM (FR-462) | **host agent** — see below |

### `resident` comes from the agent, never the registry

The registry knows what is **promoted**. It knows nothing about what is **loaded**.
`gateway/app/routers/infer.py` states the rule and names the failure it prevents: *"the agent is the
only component that knows what is actually resident"*, established by 022 FR-260/261/262 after the
divergence where a response said `model: ops-bot-v2` while the prediction logged
`registry_model: qwen…`.

So the listing is a **join**: registry for identity, the agent's resident set for `resident`. An
earlier revision of this document put the field on `registry.list_models()`, which reintroduces
exactly the shape 022 closed.

**When the agent is unreachable the field is omitted** (FR-462a) — not defaulted to `false`, not
inferred from the promotion pointer. A listing that reports residency it did not observe is the same
untruth, one field over, as a response that reports a model it did not serve.

`resident` is advisory even when present: it may be stale by the time a request arrives. It exists so
a client can *prefer* a resident model, not so it can rely on one — and it must be labelled that way,
or it becomes a promise the platform did not make.

---

## Refusal codes *(new values in an existing vocabulary)*

026's `contracts/inference-openai.md` defines `unauthorized`, `quota_exhausted`, `gpu_busy`,
`model_too_large`, `metering_unavailable`. 028 adds two groups. The **resolution** refusals are
answered before any agent call, and all are permanent **except `registry_unavailable`**, which is
transient and carries `Retry-After`:

| code | HTTP | meaning |
|---|---|---|
| `model_not_found` | 404 | no such registered model |
| `model_not_promoted` | 409 | registered, nothing promoted to serving |
| `model_wrong_modality` | 400 | promoted, but not for this endpoint's modality |
| `model_version_not_found` | 404 | the qualifier names a version that does not exist |
| `model_version_not_promoted` | 409 | the version exists but is not the promoted one (FR-457) |
| `model_name_ambiguous` | 409 | a registered name ends in `:` followed by digits, so it cannot be told apart from a qualified reference to a shorter name (FR-440b) |
| `model_default_unconfigured` | 409 | `model` was omitted and the endpoint's modality has no usable designated default — nothing promoted, the pointer names a model not promoted for it, or the pointer is unset while two or more promoted models carry the task (FR-477c, FR-477d). **Embeddings/ASR/vision only**: the LLM surface resolves through `active_serving_llm_name()`, whose configured-default tier always answers (FR-477f) |
| `registry_unavailable` | 503 | resolution could not be performed; transient, carries `Retry-After` |

The **routing** refusal is produced by the agent rather than the resolver, because residency is the
agent's fact and nothing upstream can state it atomically (FR-473):

| code | HTTP | meaning |
|---|---|---|
| `not_resident` | 409 | the identity resolved and is promoted, but is not loaded on the agent, and the request carried no valid placement authorization (FR-473b in Phase 1, FR-474/FR-474a in Phase 2+) |

`not_resident` is **permanent for the request as sent** and MUST NOT carry `Retry-After`. It is 409
rather than 503 because it is a conflict with current state, not GPU contention — and the
distinction is load-bearing rather than cosmetic in both phases. In **Phase 2+** it is the trigger
for the gateway's fresh revalidation and re-issue with an authorization (FR-474a); a gateway that
reads it as 503 treats it as backoff-and-retry, never takes that step, and the two-step flow
degenerates into the auto-admit path it was written to replace. In **Phase 1** there is no placement
path at all, so a transient code would send a client into an unbounded retry against a state that
cannot change within the phase.

`model_default_unconfigured` is likewise a **configuration** problem rather than a client error: the
caller omitted `model`, which is valid on every surface, and the platform has nothing designated to
answer with. Its message must name the modality **and the pointer to set** — an operator told only
that the platform is misconfigured has not been told which knob fixes it. It is **not** reachable on
a deployment whose modality has exactly one promoted model, which is the deterministic case FR-477c
keeps working with no operator action; the three states that do reach it are all configuration —
nothing promoted, the pointer naming something not promoted, or the pointer unset while two or more
promoted models carry the task. It is also **unreachable on the LLM surface entirely**: that default
resolves through `registry.active_serving_llm_name()`, whose third tier is a configured base, so the
chain always answers and there is no unconfigured state to report (FR-477f).

`model_name_ambiguous` is a **registry-content** problem, not a client error: the caller sent a
well-formed string and the registry holds a name no rightmost-split grammar can disambiguate. It is
409 rather than 400 for that reason, and its message must point at the operator remedy — rename the
model — rather than at the request.

`registry_unavailable` is the only transient one, and it is deliberately **not** `gpu_busy`: the GPU
is fine, and a client that treats it as GPU contention would back off against the wrong resource.

Per FR-464 these are counted with a **bounded** label vocabulary:
`model_not_found | model_not_promoted | model_wrong_modality | model_version_not_found |
model_version_not_promoted | model_name_ambiguous | model_default_unconfigured | registry_unavailable |
not_resident`. The model name is
tenant-controlled and never becomes a label — the same trap `hostagent/metrics.py` already documents
for `malformed_length`.
