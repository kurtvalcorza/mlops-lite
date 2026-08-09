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
increment closes one level up. The `@` separator is chosen because MLflow model names permit `:` in
practice and the OpenAI `model` string uses `:` as the user-facing qualifier — keeping the wire
separator distinct from the request separator means a malformed request string can never be mistaken
for a well-formed key.

**Validation** (FR-439–FR-445):

| condition | outcome |
|---|---|
| name matches no registered model | refuse `model_not_found` (permanent) |
| name registered, no promoted serving version | refuse `model_not_promoted` (permanent) |
| promoted, but modality ≠ the endpoint's | refuse `model_wrong_modality` (permanent) |
| qualifier names a version that does not exist | refuse `model_version_not_found` (permanent) |
| qualifier names a real version that is not promoted | refuse `model_version_not_promoted` (permanent), naming the promoted version (FR-457a) |
| `model` absent or empty | resolve the platform default; never a fallback from a failed resolution (FR-444) |
| registry unreachable | refuse; **never** fall through to the modality slot (FR-445) |

---

## ResidentModel *(existing — `hostagent/coordinator.py:151`, one field added)*

Current `__slots__`: `model_key`, `state`, `vram_accounted_bytes`, `active_requests`,
`last_used_at`, `recency_seq`, `child`, `materialized`.

| added field | type | notes |
|---|---|---|
| `became_resident_at` | float | set once, when the entry reaches `resident`; **never** touched afterwards |

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

## VictimSelection *(new — return shape of `Coordinator._select_victims`)*

`_select_victims` currently returns `list[ResidentModel]`, with `[]` overloaded to mean three
different things (research R4). It returns a small record instead:

| field | type | notes |
|---|---|---|
| `victims` | list[ResidentModel] | non-empty when a sufficient set was found |
| `blocked_by` | str \| None | `None`, `"insufficient"`, `"transient"`, or `"residency_window"` |
| `retry_after_s` | float \| None | set only for `residency_window`: time remaining on the earliest sufficient victim's window |

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
| `host_ram_budget_bytes` | float | see `quickstart.md` | FR-467. Checked as an admission **precondition**, alongside the two VRAM bounds |

**Why host RAM is a precondition and VRAM is reconciled.** The coordinator admits on a VRAM estimate
and corrects it against a real post-load reading, because an over-estimate merely refuses a load that
would have fit. Host RAM has no such symmetry: once a child has allocated it, the coordinator cannot
reclaim it — there is no unload-and-retry that gives it back within the request. So the host-RAM
check refuses *before* the spawn, and refuses transiently (`gpu_busy`), never permanently.

Documented in `.specify/memory/hardware-profile.md` with the other admission tunables, since that is
where 026's tunables were recorded after the Codex review.

---

## ModelListingEntry *(existing shape — `GET /v1/models`, enriched)*

Today: `{id, object, owned_by, version}`, filtered on `serving_version` alone.

| field | change |
|---|---|
| `id` | unchanged — the registered model name |
| `version` | unchanged — the promoted serving version |
| `modality` | **added**; the listing is filtered to the surface's modality (FR-461) |
| `resident` | **added**; whether it is currently in VRAM (FR-462) |

`resident` is advisory and may be stale by the time a request arrives. It exists so a client can
*prefer* a resident model, not so it can rely on one — and it must be labelled that way, or it
becomes a promise the platform did not make.

---

## Refusal codes *(new values in an existing vocabulary)*

026's `contracts/inference-openai.md` defines `unauthorized`, `quota_exhausted`, `gpu_busy`,
`model_too_large`, `metering_unavailable`. 028 adds the **resolution** refusals — all permanent, all
answered before any agent call:

| code | HTTP | meaning |
|---|---|---|
| `model_not_found` | 404 | no such registered model |
| `model_not_promoted` | 409 | registered, nothing promoted to serving |
| `model_wrong_modality` | 400 | promoted, but not for this endpoint's modality |
| `model_version_not_found` | 404 | the qualifier names a version that does not exist |
| `model_version_not_promoted` | 409 | the version exists but is not the promoted one (FR-457) |
| `registry_unavailable` | 503 | resolution could not be performed; transient, carries `Retry-After` |

`registry_unavailable` is the only transient one, and it is deliberately **not** `gpu_busy`: the GPU
is fine, and a client that treats it as GPU contention would back off against the wrong resource.

Per FR-464 these are counted with a **bounded** label vocabulary. The model name is tenant-controlled
and never becomes a label — the same trap `hostagent/metrics.py` already documents for
`malformed_length`.
