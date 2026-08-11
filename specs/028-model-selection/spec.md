# Feature Specification: 028 Model-Selective Serving

**Feature Branch**: `028-model-selection`

**Created**: 2026-08-09

**Status**: Draft

**Input**: User description: "Honor the `model` parameter on the OpenAI-compatible broker surface.
Today `POST /v1/chat/completions`, `/v1/embeddings`, `/v1/audio/transcriptions`, and
`/v1/vision/{task}` accept a `model` field,
validate nothing against it, and proxy unconditionally to the single modality slot
`SERVING_URL = {AGENT_URL}/engines/llm`; the response then echoes back either the agent-reported
`serving_model` or the caller's own string, so a tenant asking for model A can be served model B
with no signal. `GET /v1/models` lists every registry model with a promoted serving version,
advertising names that no request can actually select."

> **Correction to the input, 2026-08-09 (`/speckit-analyze` finding F1).** The description as
> originally given named a `POST /v1/completions` endpoint. **No such endpoint exists** —
> `gateway/app/routers/broker_openai.py` exposes `GET /models`, `POST /chat/completions`,
> `POST /embeddings`, `POST /audio/transcriptions`, `POST /vision/{task}`, and `GET /usage`. It also
> omitted the ASR and vision surfaces, which do exist and do accept `model`. The quote above is
> corrected to the real surface; the error was in the description, not in the code.

## Summary

026 specified a broker whose admission is **model-targeted**. Its inference contract says so
explicitly: *"If the target model isn't resident, admission loads it if it fits the VRAM budget
(co-resident), else evicts idle/LRU serving tenants to fit"*
([026 contracts/inference-openai.md](../026-lan-gpu-broker/contracts/inference-openai.md)). The
admission core was built to match — `hostagent/coordinator.py` keys its `residents` map, its
generation tokens, its claims, and its eviction victim-selection on a `model_key`.

The surface above it was not. Three seams were left unjoined, and together they mean **no request
has ever selected a model**:

1. **The OpenAI router never reads `model` for routing.** `chat_completions` posts to a fixed
   `SERVING_URL` ([gateway/app/routers/broker_openai.py:260](../../gateway/app/routers/broker_openai.py)),
   which `settings.py:28` defines as `f"{AGENT_URL}/engines/llm"` — a **modality slot**, not a model.
   `body.model` appears only in the response echo and in refusal text.
2. **The admission shim collapses `model_key` to `engine_id`.** `hostagent/coordadmission.py` presents
   the legacy `acquire(engine_id, kind, est_gb)` surface over the coordinator, so the coordinator's
   per-model residency degenerates to per-engine residency: co-residency today is across *engines*
   (llm / vision / embed / asr), never across two LLMs. 026's own T632 records this honestly —
   *"Single-resident-model serving path (no co-residency yet)"*.
3. **The listing advertises what cannot be selected.** `GET /v1/models` returns every registry model
   carrying a promoted serving version. A tenant reads that list, picks a name, sends it, and is
   served whatever occupies the llm slot — with the response's `model` field reporting the *served*
   identity, so a careful client can detect the substitution only by comparing what it asked for
   against what came back.

The consequence is not cosmetic. A tenant that requests a safety-tuned model and receives a base
model gets no error, no header, and no log line saying a substitution happened. Every per-model
claim the platform makes downstream — metering attribution, quality monitoring baselines, shadow
replay comparisons — is computed against the model that *answered*, while the tenant's own record of
what it asked for says something else.

028 joins the three seams. `model` becomes the request's routing key: resolved against the registry,
carried into admission as the coordinator's `model_key`, and answered by that model or refused with a
reason that names why. Nothing in the admission core needs to be rebuilt — it was designed for this
and has been waiting for a caller that passes a model.

**This increment does not amend the constitution.** Principle II (v1.6.1) already permits bounded
co-residency of serving tenants within the usable VRAM budget and already requires that admission
"evict resident serving tenants by a defined policy (idle-first / least-recently-used) to make room,
or refuse with a clear reason if it cannot fit even alone." 028 implements that clause for LLMs; it
does not widen it. Training exclusivity and never-preempt are untouched.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The named model is the model that answers (Priority: P1) 🎯 MVP

A tenant sends `{"model": "support-assistant", ...}` to `/v1/chat/completions`. If
`support-assistant` is resident, it answers. If some other model occupies the LLM engine, the request
does **not** silently receive that other model's output: it is either served by
`support-assistant` (US2's on-demand load) or refused. A request naming a model the platform does not
have, or has but has not promoted, is refused before any GPU work is attempted.

**Why this priority**: This is the increment's entire point and the P1 finding it answers. Shipping
only this story ends the silent substitution — the failure mode with no signal — even if every
non-resident request is refused rather than loaded. A truthful refusal is strictly better than an
untruthful success.

**Independent Test**: With model A resident, send a chat request naming model B (promoted, not
resident) and confirm the response is a refusal naming B, not a completion produced by A. Send a
request naming A and confirm it is served and the response's `model` field reads A. Send a request
naming a string that is in no registry and confirm it is refused without the agent being contacted.

**Acceptance Scenarios**:

1. **Given** model A is resident and promoted, **When** a tenant requests `model: A`, **Then** the
   completion is produced by A and the response's `model` field names A and its resolved version.
2. **Given** model A is resident and model B is promoted but not resident, **When** a tenant requests
   `model: B` and on-demand loading is unavailable or refused, **Then** the response is a refusal
   whose machine-readable code distinguishes "B could not be placed right now" from "B does not
   exist", and **no** completion produced by A is returned.
3. **Given** a name matching no registered model, **When** a tenant requests it, **Then** the broker
   refuses with a permanent, non-retryable code and makes no request to the host agent.
4. **Given** a registered model with **no** promoted serving version, **When** a tenant requests it,
   **Then** the broker refuses with a code distinct from "unknown model", because the operator's
   remedy differs — promote it, versus register it.
5. **Given** a tenant omits `model` entirely, **When** the request is otherwise valid, **Then** the
   broker applies the documented default **for the modality of the endpoint called** (FR-444, FR-477
   — there are four defaults, not one platform-wide model) and the response's `model` field names
   what actually answered, so an omitted field never reads as a granted request.

---

### User Story 2 - A promoted model that is not resident is loaded on demand (Priority: P2)

A tenant names a promoted model that is not currently in VRAM. Admission attempts to make it
resident: it is admitted co-resident if both VRAM bounds allow, or after evicting idle/least-recently-used
serving tenants if they must go, or the request is refused with a reason that distinguishes *contention*
(retry later) from *impossible* (this model does not fit an empty GPU). The tenant's request then
completes against the model it named.

**Why this priority**: This is what makes the surface usable rather than merely honest. Without it a
multi-model registry serves exactly one model and refuses the rest, which is truthful but not the
broker 026 described. It is P2 because US1's refusal path is a coherent shippable product on its own.

**Independent Test**: Start with model A resident and B promoted-but-absent. Request B. Confirm B
becomes resident (co-resident if both fit, otherwise A is evicted after draining its in-flight
requests), that B answers, and that the admission journal records the placement decision and any
eviction with its reason.

**Acceptance Scenarios**:

1. **Given** model B is promoted and not resident and both VRAM bounds admit it alongside the current
   residents, **When** a tenant requests B, **Then** B is loaded co-resident, no resident is evicted,
   and B answers the request.
2. **Given** B cannot fit alongside the current residents but would fit alone, **When** a tenant
   requests B, **Then** admission evicts serving tenants by the idle-first/LRU policy, drains their
   in-flight requests before unloading, loads B, and B answers.
3. **Given** B's estimate exceeds the usable capacity less safety headroom — it would not fit an
   **empty** GPU, **When** a tenant requests B, **Then** the refusal is the permanent
   `model_too_large` code, never the transient contention code.
4. **Given** an exclusive training/HPO/batch job holds the GPU, **When** a tenant requests any model,
   **Then** the refusal is the transient contention code with `Retry-After`, the job is **not**
   preempted, and no eviction is attempted.
5. **Given** two tenants concurrently request two models that cannot co-fit, **When** both are
   admitted in sequence, **Then** the accounted resident set never exceeds the usable budget at any
   observed instant, and neither request receives another model's output.

---

### User Story 3 - The listing and the response tell the same truth (Priority: P3)

`GET /v1/models` lists exactly the models a request may select, and every response — streaming and
non-streaming alike — reports the identity that actually produced the tokens. A client can compare
what it asked for against what it got, on every response, without reading logs.

**Why this priority**: The discovery surface is what makes the other two stories usable by an
unmodified OpenAI client, and closes the "advertises names no request can select" half of the
finding. It is P3 because a tenant who already knows the model name is unblocked by US1 and US2.

**Independent Test**: Call `/v1/models`, then send one request per listed id and confirm each is
either served by that exact model or refused for a placement reason — never served by a different
model. Confirm the streaming path reports the same resolved identity as the non-streaming path for
the same request.

**Acceptance Scenarios**:

1. **Given** the registry holds promoted and unpromoted models, **When** a tenant calls
   `/v1/models`, **Then** every listed id is selectable and no promoted-and-selectable model is
   omitted.
2. **Given** a streaming chat request, **When** the tenant reads the SSE chunks, **Then** the `model`
   field on the chunks names the resolved serving identity, not the caller's request string echoed
   back.
3. **Given** a request whose named model is resolved to a specific version, **When** the response is
   returned, **Then** the response carries the resolved version alongside the name, so two requests
   answered by different versions of one model are distinguishable by the client.

---

### Edge Cases

- **A name that is valid for the wrong modality.** A tenant sends an embedding model's name to
  `/v1/chat/completions`. This must be refused as a modality mismatch, distinctly from "unknown
  model" — the name exists and is promoted, but not for this surface.
- **Promotion changes between resolution and admission.** A model is promoted to version 3 while a
  request that resolved version 2 is in flight. The request completes against the version it
  resolved, and the response reports that version; it is not silently upgraded.
- **The named model is resident but `draining` or `evicting`.** The request must await the owning
  operation rather than starting a competing load (the transient-state branch 026 T675 already
  built), or be refused as contention — never fall through to a different resident.
- **Repeated alternating requests for two models that cannot co-fit.** Two tenants alternating
  between A and B would otherwise thrash the GPU, each load evicting the other. The minimum residency
  period (FR-456) makes the second request wait rather than swap, and its `Retry-After` says how
  long. The tenant whose model is resident is not punished for arriving first.
- **A tenant names the model currently being loaded by another tenant's request.** The second
  request must join the in-flight load rather than issue a second one.
- **Registry unreachable.** Resolution cannot consult the registry. The broker must refuse rather
  than fall back to the modality slot, because falling back is exactly the silent substitution this
  increment exists to end.
- **An empty string or omitted `model`.** Distinct from an unknown name: this is the documented
  default path, and must be answered with the served identity named in the response.
- **Metering and quota when a request is refused after an eviction has already occurred.** GPU work
  was done (a drain and an unload) for a request that produced no tokens; the ledger must not record
  the refusal as billable output, and the eviction must still be journaled.

## Requirements *(mandatory)*

### Functional Requirements

**Resolution — turning a request string into a model identity**

- **FR-439**: The broker MUST resolve the request's `model` field to a concrete
  `(model name, version)` pair before any GPU work is requested, using the registry as the authority.
- **FR-440**: A bare model name MUST resolve to **the version promoted as of a registry read no older
  than the configured resolution TTL**, and the response MUST name the version actually served
  (FR-458). This is the **normative** statement of bare-name resolution and it governs FR-457c, which
  states the same bound from the cache's side rather than granting a competing allowance. The system
  MUST also accept an explicit version qualifier, subject to FR-457, and MUST refuse a qualifier that
  names a version that does not exist.
- **FR-440c**: The TTL bound in FR-440 is the **only** permitted *cache* staleness, and it applies
  solely to *serving an already-resident* version. Any resolution that would cause a **placement**
  revalidates fresh (FR-457e), and any **pinned** request reads through (FR-457b). All three are
  read against the single linearization point in **FR-475**: each is current as of its own
  authorizing read, and none claims currency at a later instant. An earlier revision stated bare-name
  resolution as unqualified ("resolves to the promoted version") while FR-457c separately permitted
  serving a stale-but-resident version; those were two policies, and this is one.
- **FR-440a**: The qualifier grammar MUST be **unambiguous for names that themselves contain `:`**.
  Parsing MUST split on the **rightmost** `:` and accept the suffix as a version only when it is a
  run of decimal digits; otherwise the entire string is a bare model name. Registry versions are
  integers — `gateway/app/registry.py` sorts them with `int(...)` in two places — so this rule is
  total and deterministic: `svc:chat:3` is version 3 of `svc:chat`, while `svc:chat` is the bare name
  `svc:chat`. An earlier revision asserted that names may contain `:` and then used `:` as the
  separator without saying how to split, which is not a grammar.
- **FR-440b**: A model whose name is ambiguous under FR-440a **cannot be served**: a registered name
  ending in `:` followed by digits is indistinguishable from a qualified reference to a shorter name.
  Resolution MUST refuse such a name with a distinct code rather than guessing, and the refusal MUST
  be **reachable**. Reachability requires an explicit step, because FR-440a's split rule is *total*:
  applied alone it resolves `svc:chat:3` to version 3 of `svc:chat` and, finding no such model,
  answers `model_not_found` — so the distinct code is never returned and the operator never learns
  the registry holds an unservable name. Resolution MUST therefore look the **unsplit** string up in
  the registry as an exact name before applying the split, and refuse `model_name_ambiguous` when
  that lookup hits a registered model whose name ends in `:` followed by digits. This is a
  registry-content problem the operator must fix, not a client error.
- **FR-441**: A `model` naming nothing in the registry MUST be refused with a **permanent**,
  machine-readable code, before any request is made to the host agent.
- **FR-442**: A `model` naming a registered model with no promoted serving version MUST be refused
  with a code **distinct** from FR-441's, because the operator remedy differs.
- **FR-443**: A `model` naming a model promoted for a different modality than the endpoint serves
  MUST be refused with a code distinct from both FR-441 and FR-442.
- **FR-444**: An omitted or empty `model` MUST resolve to the documented default serving model **for
  the modality of the endpoint being called**, and the response MUST name what actually answered. The
  default MUST NOT be reached by falling back from a failed resolution. A **single platform-wide**
  default MUST NOT be used: `model: str = ""` is today's declared default on the embeddings,
  transcription, and vision handlers (`gateway/app/routers/broker_openai.py:220`, `:373`, `:464`), so
  a platform default carrying one modality would resolve every omitting caller on the other three
  surfaces to a model FR-443 then refuses `model_wrong_modality`. Omission MUST remain a working
  request on every surface where it works today; a per-modality default is what makes FR-443 and
  back-compatibility hold at once.
- **FR-445**: When the registry cannot be consulted, the broker MUST refuse the request. It MUST NOT
  fall back to serving whatever occupies the modality slot.

**Routing — carrying the identity through admission**

> Atomicity of the identity assert is **FR-473–FR-473c**, under *Atomicity and placement
> authorization* below. It is placed there rather than here because `scripts/check_specs.py`
> (FR-296) requires FR identifiers to be **defined in ascending order**, and these were written
> after the routing block. The gate is right to insist: an out-of-order definition is how a
> duplicate or skipped identifier gets in unnoticed.

- **FR-446**: The resolved identity MUST be carried into host-agent admission as the coordinator's
  `model_key`, so residency, generation tokens, claims, and eviction victim-selection all key on the
  model the tenant named.
- **FR-447**: The admission shim MUST stop collapsing `model_key` to `engine_id` for the serving
  path, while preserving the exclusive-job path unchanged (a job takes the whole GPU and is never
  preempted).
- **FR-448**: A request whose resolved model is resident and serving MUST be routed to that model's
  child process, and MUST NOT be routed to any other resident.
- **FR-449**: A request whose resolved model is in a transient state (`loading`, `draining`,
  `evicting`, `rolling_back`) MUST await the owning operation or be refused as contention. It MUST
  NOT start a competing load and MUST NOT be answered by a different resident.
- **FR-450**: Concurrent requests naming the same non-resident model MUST result in **one** load,
  with the later requests joining the in-flight operation.

**Placement — making a named model resident**

- **FR-451**: A resolved model that is promoted **at its authorizing revalidation** (FR-475) but not
  resident MUST be admitted if both constitutional bounds allow — the accounted resident set plus
  reservations within the usable budget, and the incoming load within live free VRAM less safety
  headroom. The authorizing read may be relied upon for no longer than the authorization's validity
  bound (FR-474b).
- **FR-452**: When the model cannot be placed alongside current residents but would fit alone,
  admission MUST evict resident **serving** tenants by the idle-first / least-recently-used policy,
  draining each victim's in-flight requests before unloading it.
- **FR-453**: A model whose estimate exceeds the usable capacity less safety headroom MUST be refused
  with the **permanent** `model_too_large` code. This code MUST NOT be returned for contention.
- **FR-454**: A model that could fit but cannot be placed right now — an exclusive job holds the GPU,
  another tenant's reservation is outstanding, a drain timed out, or an unaccounted external consumer
  holds VRAM — MUST be refused with the **transient** `gpu_busy` code carrying `Retry-After`.
- **FR-455**: An exclusive training / HPO / batch job MUST NOT be preempted or evicted by any
  tenant-initiated model placement, and no eviction MUST be attempted while a job barrier is up.
- **FR-456**: A model that has just become resident MUST be protected from eviction for a **minimum
  residency period** — a configured tunable alongside 026's other admission tunables. Within that
  period the model is not an eligible eviction victim, whoever requests the placement and however
  idle it is.
- **FR-456a**: A placement that could only proceed by evicting a model still inside its minimum
  residency period MUST be refused with the **transient** `gpu_busy` code, and its `Retry-After` MUST
  be the earliest time at which a **sufficient victim set** becomes eligible — not the earliest time
  at which any single protected victim becomes eligible. When a placement needs several victims to
  free enough VRAM, the set is not evictable until the **last** of them leaves its window, so a value
  taken from the earliest sends the client back to be refused again.
- **FR-456b**: The minimum residency period MUST bound eviction rate independently of the request
  pattern: a given resident model MUST NOT be evicted more than once per period, regardless of how
  many tenants request a competing model or in what order. This is the property that makes an
  alternating two-model workload cost a bounded number of loads per unit time rather than one per
  request.
- **FR-456c**: The minimum residency period MUST NOT apply to the exclusive-job path. A job's whole-GPU
  claim is governed by FR-455 (never preempted), which is strictly stronger; and a job's *release* is
  not an eviction.
- **FR-457**: A version qualifier MUST be an **assertion, not a selection**. `name:version` is served
  only when that version is the promoted serving version **at the pin's read-through** (FR-457b) —
  the linearization point FR-475 defines, and the tightest bound available, since the read is
  immediately before the decision. A qualifier naming any other registered version MUST be refused
  with a machine-readable code distinct from "version does not exist". A tenant request therefore can
  never place a version that 022's gated promotion path did not promote **at its authorizing read**.
- **FR-457a**: The refusal in FR-457 MUST name the version that **is** promoted, so a client whose
  pin failed because promotion moved underneath it can tell that case apart from having pinned a
  version that was never promoted.
- **FR-457b**: A **pinned** request MUST be validated against a **fresh** read of the promotion
  pointer. A cached promoted-version answer MUST NOT be used to authorize a pin, because FR-457's
  assertion is about what is promoted *now*; serving a pin from a stale cache authorizes a version
  the platform may have already demoted.
- **FR-457c**: *(subordinate to FR-440, which is normative.)* An **unpinned** request MAY resolve
  from the cache **only when the resolved identity is already resident**, within FR-440's TTL bound.
  Its tolerance comes from what the tenant asked for: "the current model" is answered truthfully by
  naming the version actually served (FR-458), whereas a pin is a claim about the pointer itself. The
  staleness bound is the cache TTL — **not** eager invalidation.
- **FR-457e**: A resolution that would cause a **placement** MUST be revalidated against a fresh read
  of the promotion pointer before admission, cached or not. Serving a stale-but-resident version is a
  truthful answer about a slightly older model; **loading** one is the platform putting an
  unpromoted version into VRAM on tenant traffic, which is precisely what 022's gated promotion
  exists to prevent. Reporting the served version truthfully does not repair that — the placement
  already happened.
- **FR-457f**: The revalidation in FR-457e costs one registry read per **placement**, not per
  request. Placements are rare relative to requests, so the cache keeps its purpose: the common case
  is a resident model requested repeatedly, and that path is unchanged.
- **FR-457d**: The specification MUST NOT rely on eager invalidation from the gateway's promotion
  path as the freshness mechanism. The `serving` alias has writers outside it —
  `scripts/retag_serving_llm.py`, `scripts/seed_asr_model.py`, `scripts/seed_embedding_model.py`,
  and `scripts/seed_tabular_model.py` all call `set_registered_model_alias` directly. Eager
  invalidation is an optimization for one writer; the TTL is the guarantee.

**Truthfulness — the response and the listing**

- **FR-458**: Every non-streaming response MUST report the resolved name **and** version that
  produced it.
- **FR-459**: Every streaming response MUST report the same resolved identity as the equivalent
  non-streaming response, on its chunks and its terminal event.
- **FR-460**: A response MUST NOT echo the caller's request string as its `model` field when the
  request was answered by anything other than that model.
- **FR-461**: `GET /v1/models` is a **single global endpoint** — the platform exposes exactly one,
  not one per modality. It MUST therefore list the selectable identities across **all** modalities
  and MUST tag each entry with the modality it is promoted for, so a client can choose one that the
  endpoint it intends to call will accept. It MUST NOT list a name whose every request would be
  refused as unknown or unpromoted. It MUST NOT be "filtered to the surface's modality" — there is
  no per-surface listing for that phrase to mean anything against.
- **FR-461a**: Because the listing spans modalities while each inference endpoint serves one, a name
  that is valid in the listing may still be refused `model_wrong_modality` (FR-443) by a given
  endpoint. The modality tag is what lets a client avoid that, and the listing's documentation MUST
  say so rather than implying every listed id is valid everywhere.
- **FR-462**: `GET /v1/models` MUST indicate each listed model's current residency, so a client can
  prefer a resident model and avoid provoking an eviction it does not need. **Residency MUST be
  sourced from the host agent**, which is the only component that knows what is actually resident
  (022 FR-260/261/262; `gateway/app/routers/infer.py` states this and names the pre-022 divergence
  it prevents). It MUST NOT be derived from the registry, which knows what is *promoted* and nothing
  about what is loaded.
- **FR-462a**: When the agent cannot be reached, the residency field MUST be **omitted** rather than
  defaulted, guessed, or inferred from the promotion pointer. A listing that reports residency it
  did not observe is the same class of untruth this increment exists to remove.

**Observability and accounting**

- **FR-463**: Every placement decision made on behalf of a tenant request — admitted co-resident,
  admitted after eviction, refused with reason — MUST be recorded in the admission journal with the
  resolved model identity and the deciding bound.
- **FR-464**: Refusal counters MUST distinguish the resolution refusals (unknown, unpromoted,
  wrong-modality) from the placement refusals (`gpu_busy`, `model_too_large`), using a **bounded**
  label vocabulary — the model name is tenant-controlled and MUST NOT become a metric label.
- **FR-465**: A request refused after an eviction has already occurred MUST NOT be metered as
  billable output, and the eviction MUST still be journaled.
- **FR-466**: The README's disclosure that `model` selects nothing MUST be replaced by an accurate
  description of what `model` now selects, in the same increment that changes the behavior.

**Resource bounds**

- **FR-467**: Multiple co-resident serving children MUST NOT push the platform outside Principle
  III's host-RAM budget. A configured **host-RAM bound** MUST be enforced as an admission
  precondition alongside the two VRAM bounds: a placement whose child would take the platform past
  it MUST be refused with the transient contention code, not admitted and reconciled afterwards.
  Host RAM, unlike VRAM, cannot be reclaimed by the coordinator once a child has allocated it.
- **FR-468**: The host-RAM figure the bound is checked against MUST be **measured**, not assumed —
  recorded for one resident child and for the co-resident case, so the bound is calibrated against
  the platform's real footprint rather than an estimate.
- **FR-469**: The **incoming model's host-RAM estimate** MUST be defined, not left implicit. It is a
  per-adapter default (mirroring the existing `estimate_vram()` per-adapter constant) with a
  per-model override, calibrated from FR-468's measurements. Without a defined estimate FR-467's
  precondition has no left-hand side and cannot be implemented.
- **FR-470**: The **accounting equation** MUST be stated:
  `Σ accounted host RAM of resident serving children + Σ outstanding host-RAM reservations +
  incoming estimate ≤ host_ram_budget_bytes`, where the budget is the platform's RAM allowance less
  the measured idle-infrastructure baseline (Principle III's ~3 GB). This mirrors the VRAM
  usable-budget bound; there is deliberately **no** second live-free-style bound, because host RAM
  has no equivalent of a device-reported free figure that is meaningful across mmap'd children.
- **FR-471**: The **measurement basis** MUST be proportional set size (PSS), or an equivalent that
  attributes shared pages once. Resident set size (RSS) MUST NOT be used as the basis: the llama.cpp
  child memory-maps its GGUF, so two children sharing page-cache pages each report those pages in
  full and their RSS sum over-counts real usage — a bound built on RSS would refuse placements that
  fit. Where PSS is unavailable, the fallback MUST subtract shared file-backed pages and MUST be
  recorded as a fallback.
- **FR-472**: After a child spawns, its accounted host RAM MUST be **reconciled to the measured
  value**. Unlike VRAM this cannot reclaim anything for the request that just ran — the point is
  that the *next* admission decides against a real number rather than against an estimate that has
  already been proven wrong.

**Atomicity and placement authorization**

*Numbered here, after the resource bounds, because `scripts/check_specs.py` (FR-296) requires
ascending definition order and these were written last. They govern the routing behaviour described
at FR-446–FR-450.*

- **FR-473**: The resolved identity MUST be asserted **atomically with dispatch, at the agent**. The
  OpenAI surface today asserts identity **nowhere**: `gateway/app/routers/broker_openai.py` posts
  straight to `{SERVING_URL}/infer` (line 260) and `{SERVING_URL}/infer/stream` (line 301), with no
  residency read on the path at all — `SERVING_URL` is a fixed modality slot, and whatever occupies
  it answers. Moving the check to the gateway does not fix that, it only makes the window narrower:
  a gateway-side check *is* a residency read followed by a separate inference call, and between the
  two the resident model can change via an operator swap, a promotion-triggered reload, or
  idle-release, so a request that passed the check for model A is still answered by model B. The
  assert therefore belongs where residency is known and dispatch happens — at the agent.
- **FR-473a**: The request MUST therefore carry the expected identity, and the agent MUST refuse when
  the engine it would dispatch to does not host it. The comparison and the **acquisition of the claim
  that pins the matched resident** MUST occur together under the lock guarding the resident set; the
  dispatch itself MUST then run with that lock **released**, against the pin. A comparison
  performed anywhere else in the agent reintroduces a narrower version of the same window, and a
  comparison that does not take a pin leaves the model evictable or swappable between the check and
  the dispatch — which is the same window by another route. **Which lock and which pin depends on
  the admission mode, and both modes MUST be specified — see FR-476.**
  > **Corrected after the PR #88 code review.** This requirement previously demanded the comparison
  > and *the dispatch* occur under one lock. That contradicts `hostagent/coordinator.py:24-29`, which
  > states that `_lock` **guards state only and is never held across `load`/`unload`** and has
  > `LifecycleGuard` (026 T635) raise rather than deadlock when it is — so the requirement as written
  > was unimplementable against the code it governs, and would additionally serialize every
  > generation behind one lock, unbounded on the streaming path. The **claim** is the mechanism 026
  > already provides: the ref-count that keeps a resident alive across a generation, established for
  > precisely the case where a model must not be evicted while a request it authorized is still
  > running. Identity settles atomically under the lock; the long I/O runs outside it.
- **FR-473b**: This assertion is **not** placement, and MUST NOT wait for a load, evict anything, or
  consult the VRAM bounds. It is a guard, and it is what allows the truthfulness guarantee to be
  delivered before on-demand placement exists. Because the guard cannot place, a promoted but
  non-resident model MUST be refused in Phase 1 with the **permanent** `not_resident` code (FR-474a)
  and MUST NOT carry `Retry-After`.
  > **Corrected after the PR #88 code review.** Phase 1 previously refused this case with
  > `gpu_busy`, which 026 defines as **transient** contention carrying `Retry-After`. Phase 1 has no
  > placement path — that is the phase boundary — so the condition holds for the entire life of the
  > phase and no retry can ever succeed. A client honouring the header backs off forever against a
  > permanent state. `not_resident` already describes exactly this and is permanent; Phase 2 makes
  > the same condition actionable by adding the authorization flow, not by relabelling it.
- **FR-473c**: Post-hoc detection — comparing the agent's reported `serving_model` against the
  resolved identity **after** the response arrives — MUST NOT be used as the mechanism. It burns the
  GPU work, and on the streaming path the tokens have already reached the client by the time the
  mismatch is visible.
- **FR-474**: The agent MUST NOT place a non-resident `model_key` on the strength of the key alone.
  Only the gateway can read the registry and only the agent knows residency atomically, so an agent
  that auto-admits whatever key arrives will place a version resolved from a **stale cache** —
  defeating FR-457e, which requires a fresh promotion read before any placement.
- **FR-474a**: Placement MUST therefore be a **two-step, explicitly authorized** flow. Step one is an
  ordinary inference carrying the expected identity and **no** placement authorization: the agent
  serves it if resident, and otherwise refuses with a distinct `not_resident` code that is not
  confusable with contention. Step two, taken by the gateway only after a **fresh** promotion-pointer
  read confirms the identity, re-issues the request carrying an explicit placement authorization.
- **FR-474b**: The authorization MUST name the exact `(name, version)` it validated and MUST carry a
  short validity bound. The agent MUST place **that** version and no other, and MUST refuse an
  authorization whose validity has lapsed rather than treating it as still good.
- **FR-474c**: This narrows the window; it does not eliminate it. The registry read and the admission
  are in different processes, so the alias can still move between them. What the design guarantees is
  the property that matters: tenant traffic can never place a version resolved from a cache of
  arbitrary age — only one confirmed promoted within the authorization's validity bound. The
  specification MUST state this limit rather than implying exactness.

**The linearization point — what "current" means**

- **FR-475**: The specification MUST define **exactly one linearization point** for promotion
  currency, and every requirement that says "current", "currently promoted", or "only promoted" MUST
  be read against it. That point is **the registry read that authorizes the operation**:
  - for a **pinned** request, the read-through of FR-457b;
  - for a **placement**, the fresh revalidation of FR-457e that issues the authorization;
  - for an **unpinned, already-resident** request, the cached read, whose age is bounded by FR-440's
    TTL.

  A version is "current" for an operation **iff it was the promoted version at that operation's
  authorizing read**. Nothing in this specification claims currency at any later instant, and no
  requirement may be read as promising it.
- **FR-475a**: Consequently the guarantees are **bounded, not instantaneous**, and MUST be stated
  that way wherever they appear:
  - FR-440/FR-440c — a bare name resolves to the version promoted at its authorizing read, whose age
    is bounded by the TTL and which applies solely to serving an already-resident version.
  - FR-457 — a pin is served iff the named version was promoted at its read-through, which is
    immediately before the decision and is the tightest bound available.
  - FR-451 — a placement admits the version promoted at its authorizing revalidation, relied upon for
    no longer than the authorization's validity.
  - The 022 dependency — a tenant request can place only a version that **was promoted at its
    authorizing read**, never one resolved from a cache of arbitrary age.
- **FR-475b**: This is a **statement of what the design achieves**, not a weakening of 022's gate.
  The gate's property is that no version becomes servable without passing 011's evaluation gate, and
  that is unaffected by when the pointer is read: an unpromoted version is never authorized at any
  instant. What FR-475 bounds is only the staleness of a *promotion*, and a version promoted moments
  ago being placed is the system working, not a violation.

**The identity pin, in both admission modes**

*Numbered last for the same FR-296 ascending-order reason as FR-473. They govern FR-473a's mechanism
and FR-444's default, and were written after the PR #89 review found each specified against only one
of the two configurations the platform actually ships.*

- **FR-476**: The identity guard of FR-473a MUST be defined as an **identity pin**: an object
  acquired under the lock that guards the identity being compared, guaranteeing that the runtime
  which answers is the one whose identity was matched, and released on **every** terminal path —
  success, error, client disconnect, and stream termination alike. The pin is an abstraction with
  **two required implementations**, because the platform ships two admission modes and Phase 1 is
  required to hold in both (T791):
  - **`BROKER_COORDINATOR_ADMISSION=1`** — the coordinator is the serving authority. The lock is the
    coordinator's resident-set lock and the pin is 026's **claim**, the ref-count that already exists
    to keep a resident alive across a generation.
  - **`BROKER_COORDINATOR_ADMISSION=0`, the default** — the coordinator is **not** the runtime
    admission authority and holds no resident entry for the live engine, so **no claim can be minted
    there**. `hostagent/main.py::_build_admission()` returns the legacy `adm.Admission`; the
    coordinator is constructed separately and later by `build_broker()`, so `_register_runtime()` is
    a no-op during `build_agent()` because `_RUNTIME_LIFECYCLE` does not yet exist. In this mode the
    comparison MUST be against the **runtime's own child/model identity**, pinned under the
    **runtime's** lock — which `coordadmission.py` already documents as the owner of request
    lifetime under the legacy shim.
- **FR-476a**: The two implementations MUST satisfy one **stated invariant**, so the guard is a
  property of the platform rather than of a configuration: *once an identity is accepted for
  dispatch, no concurrent swap, reload, eviction, or idle-release can cause a different model to
  answer that request, and the pin is released on every terminal path.* A requirement whose only
  mechanism exists in the non-default configuration is not implemented in the configuration an
  operator actually runs.

**Per-modality default authority**

- **FR-477**: Each of the four modality defaults required by FR-444 MUST have a **single designated
  authority** that names a concrete registry identity, and the resolver and the runtime that answers
  MUST be guaranteed to agree on it. Only the LLM modality has one today —
  `registry.DEFAULT_LLM` (`SERVING_MODEL`) with `registry.active_serving_llm_name()`. Embeddings,
  ASR, and vision have none: `registry.resolve_serving_target(task)` finds versions tagged for a task
  and, when several models share it, documents that *"otherwise the first match is used"*. That is
  **discovery, not a designated default** — deterministic only while exactly one promoted model
  carries the tag.
- **FR-477a**: The reason this becomes load-bearing now: omission works today because each endpoint
  posts to a fixed engine slot, so no identity is ever chosen. Once FR-439 requires an omitted
  `model` to resolve to a concrete identity **before** the agent call, an arbitrary pick can differ
  from the runtime that answers — and FR-473a's guard would then refuse a request that works today,
  or FR-458 would report an identity that did not serve. That is the identity divergence 022 closed,
  reintroduced through the default path.
- **FR-477b**: Each modality's default MUST therefore specify: the **source** of the identity; the
  behaviour when **no** default exists; the behaviour when **several** promoted models carry the
  modality and none is designated; and the mechanism guaranteeing **resolver/runtime agreement**. A
  missing default MUST be refused with a permanent, machine-readable code naming the modality — never
  silently resolved to whichever model is found first.

### Key Entities

- **Resolved model identity**: the `(name, version)` pair a request string resolves to, plus the
  modality it is promoted for. This is what admission keys on and what the response reports.
- **Resident model**: an entry in the coordinator's `residents` map — a model currently in VRAM with
  a state, accounted VRAM, an active-request count, and a last-used timestamp. Already exists; 028
  makes tenant requests key on it.
- **Placement decision**: the outcome of admitting a resolved identity — granted, shared with an
  existing resident, granted after eviction, or refused with a deciding bound. Already journaled by
  026; 028 attributes it to a resolved model rather than an engine.
- **Model listing entry**: what `/v1/models` publishes — a selectable identity with its promoted
  version, modality, and residency.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-204**: In a suite of requests naming every model the listing advertises, **100%** are either
  served by the model named or refused with a reason — **zero** are served by a different model.
  This is the finding this increment closes, and it is a count, not a proportion to improve.
- **SC-205**: For every refusal, a client can determine from the response alone — without operator
  logs — whether retrying will help. Transient and permanent refusals are distinguishable in 100% of
  cases.
- **SC-206**: A tenant naming a promoted, non-resident model that fits the GPU receives that model's
  output without operator involvement, in a single request.
- **SC-207**: Across a mixed workload of concurrent requests for multiple models, the accounted
  resident set never exceeds the usable VRAM budget, and no exclusive job is preempted — verified by
  reading the admission state endpoint alone, as 026 SC established.
- **SC-208**: A client comparing its request's `model` against the response's `model` finds them
  equal on every successful request, on both the streaming and non-streaming paths.
- **SC-209**: An alternating two-model workload that cannot co-fit performs **at most one eviction of
  a given model per minimum-residency period**, however many requests arrive — so the cycle count is
  set by the configured period, not by request volume. Requests refused during a period carry a
  `Retry-After` that, if honored, succeeds on the first retry **when no competing traffic arrives in
  the interval**. That qualifier is not a weakening: `Retry-After` is a lower bound (FR-456a), it
  says when the *window* stops being the obstacle, and no bound stated by one tenant can promise
  what another tenant's traffic will do. A criterion asserting unconditional first-retry success
  would be asserting something the system cannot deliver.
- **SC-210**: No documentation in the repository states that `model` selects nothing once this
  increment ships.
- **SC-211**: Under the heaviest co-residency the platform admits, measured host RAM stays within
  Principle III's budget — and the figure is a recorded measurement, not an estimate. A placement
  that would breach it is refused rather than admitted.

## Assumptions

- **The admission core does not need rebuilding.** `hostagent/coordinator.py` already keys residency,
  generations, claims, and eviction on `model_key`, and 026 T675 already built the transient-state
  branch. 028 changes the callers that pass `engine_id` in that slot, not the state machine.
- **Model identity is the registry's model name.** The registry is the authority for what names
  exist, what versions they have, and which version is promoted for serving. No new naming scheme is
  introduced.
- **Co-residency of multiple LLMs is in scope.** 026's inference contract specifies it and
  constitution v1.6.1 permits it. 028 does not treat "one LLM at a time" as a constraint to preserve;
  it treats it as the unimplemented half of 026.
- **Embeddings and other CPU-only models hold no GPU lease** (Principle II) and therefore resolve and
  route without a placement decision, though they still resolve and still report a truthful identity.
- **`BROKER_COORDINATOR_ADMISSION` remains the phase gate.** Per-model routing depends on the
  coordinator path; with the flag off the platform keeps 018 behavior. The increment must state what
  the surface does in both positions of that flag rather than assume it is on.
- **The existing refusal vocabulary is reused where it fits.** 026's contract already defines
  `gpu_busy`, `model_too_large`, `quota_exhausted`, and `unauthorized`; the resolution refusals this
  increment adds are new codes alongside them, not replacements.
- **Hardware validation is required before this is considered delivered.** Co-residency and eviction
  are GPU behaviors; per Principle VII and 026's own phase gating, a passing test suite on a machine
  without the GPU is not evidence that this works.

## Dependencies

- **026 LAN GPU Broker** — supplies the coordinator, the admission journal, the refusal vocabulary,
  and the contract this increment implements. Its Phase 5/6 remain gated; 028 depends on none of the
  gated scope.
- **022 Registry-Driven LLM Serving** — supplies the promotion pointer and the gated
  `POST /control/reload` path. FR-457 keeps that gate intact: a tenant request can place only a
  version that **was promoted at its authorizing read** (FR-475), so tenant-initiated placement
  widens *when* a promoted model loads, never *what* may load. The gate's property — that no version
  becomes servable without passing 011's evaluation gate — is unaffected by *when* the pointer is
  read, because an unpromoted version is never authorized at any instant (FR-475b).
- **The registry (MLflow)** must be reachable for resolution; FR-445 defines the behavior when it is
  not.

## Out of Scope

- Changing what the promotion pointer means, or who may promote.
- Multi-node or multi-GPU placement (Principle I).
- Per-model quota or pricing. Metering stays as 026 built it; only the attribution of a refusal
  changes (FR-465).
- **Re-keying ASR and vision placement to `model_key`.** Decided unconditionally out of scope
  (`/speckit-analyze` finding A1 — the earlier "unless the model-keyed shim makes it free" was a
  condition with no decision procedure, so it could never be closed). Their placement continues to
  go through per-engine admission.

  **In scope for those two surfaces**: resolution and truthful reporting. `POST /v1/audio/transcriptions`
  and `POST /v1/vision/{task}` both accept `model` today and neither validates it, so they carry the
  same silent-substitution defect as chat. FR-439–FR-445 and FR-458–FR-460 apply to them as written.
