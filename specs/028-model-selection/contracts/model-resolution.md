# Contract: Model Resolution

**Owner**: `gateway/app/modelresolve.py` (new) · **Consumers**: `gateway/app/routers/broker_openai.py`
· **Requirements**: FR-439–FR-445, FR-440a–FR-440c, FR-457–FR-457f, FR-461, FR-464, FR-475–FR-475b, FR-477–FR-477f

Resolution turns the request's `model` string into a `ResolvedModel` or a refusal. It runs **before**
any call to the host agent, so a refusal here costs no GPU work.

## The linearization point

Everything below is read against **FR-475**: a version is "current" for an operation **iff it was the
promoted version at that operation's authorizing read**. Three reads, three bounds —

| operation | authorizing read | staleness |
|---|---|---|
| **pinned** request | read-through, immediately before the decision (FR-457b) | none beyond the read itself |
| **placement** | fresh revalidation issuing the authorization (FR-457e) | bounded by the authorization's validity (FR-474b) |
| **unpinned, already-resident** request | the cached read (FR-457c) | bounded by the TTL (FR-440) |

Nothing here claims currency at a later instant. An earlier revision of this contract did — see the
grammar note below.

## Request grammar

```text
model := ""                     ; absent or empty → the default for THIS ENDPOINT'S modality (FR-444)
       | <name>                 ; → the version promoted AT THE AUTHORIZING READ (FR-440, FR-475)
       | <name> ":" <version>   ; an ASSERTION that <version> is promoted, checked by
                                ; read-through immediately before the decision (FR-457, FR-457b)
```

`<name>` is a registered model name. `<version>` is a run of decimal digits. **Parsing splits on the
rightmost `:`** and accepts the suffix as a version only when it is all digits; otherwise the whole
string is a bare name (FR-440a). A registered name that is still ambiguous under that rule — one
ending in `:` followed by digits — is refused `model_name_ambiguous` (FR-440b).

**Ordering matters, and it is the whole of FR-440b's reachability.** The resolver MUST look the
**unsplit** string up as an exact registered name *before* applying the split, and refuse
`model_name_ambiguous` when that lookup hits a name ending in `:` followed by digits:

```text
1. exact-name lookup on the unsplit string
     → hit, and the name ends in ":" + digits  → 409 model_name_ambiguous   (FR-440b)
     → hit, otherwise                          → bare name, resolve as <name>
     → miss                                    → 2
2. rightmost-":" split per FR-440a
```

Without step 1 the refusal is **unreachable**: FR-440a's rule is total, so `svc:chat:3` splits to
version 3 of `svc:chat`, that model does not exist, and the resolver answers `model_not_found`. The
distinct code is never returned, T776's reachability requirement cannot be satisfied, and the
operator never learns the registry holds a name the platform cannot serve.

> **Corrected after the third PR #88 review.** This grammar previously read
> `<name> ; → that model's promoted serving version`, unqualified, which contradicted FR-440's
> TTL-bounded normative statement and FR-474c's admission that freshness is bounded. It also said a
> tenant request *"can never place a version that 022's gated promotion path did not promote"* —
> stronger than the design delivers. Both now reference the single linearization point.

**The qualifier is an assertion, not a selection.** `support-assistant:3` means *"serve version 3, or
tell me you can't"* — never *"load version 3"*. A tenant request can therefore never place a version
that 022's gated promotion path had not promoted **at the request's authorizing read**, and 011's
evaluation gate remains the only way a version becomes servable at all (FR-475b).

## Outcomes

| input | outcome |
|---|---|
| `""` / absent | `ResolvedModel` for the **calling endpoint's modality** default, from that modality's designated authority (FR-444, FR-477); response names what answered. A single platform-wide default would refuse every omitting caller on three of the four surfaces |
| `""` / absent, **LLM surface** | whatever `registry.active_serving_llm_name()` returns — `ActiveServingLLM` pointer, else sole-promoted adoption, else `DEFAULT_LLM`. Never `prefer_name=SERVING_MODEL`, which names only the third tier (FR-477c, FR-477f) |
| `""` / absent, non-LLM, pointer **set** and promoted | that identity — `prefer_name` wins over any other promoted model carrying the task |
| `""` / absent, non-LLM, pointer **unset**, exactly one promoted model carries the task | that model (FR-477c) — the ordering is unobservable with one candidate |
| `""` / absent, non-LLM, pointer **unset**, **two or more** promoted carry the task | **409 `model_default_unconfigured`** naming the modality *and the pointer to set* (FR-477c, FR-477d) — never an arbitrary first match |
| `""` / absent, non-LLM, pointer **set** but that model is not promoted for the modality | **409 `model_default_unconfigured`** — no fall-back to first match |
| `""` / absent, non-LLM, modality has **no** promoted serving model | **409 `model_default_unconfigured`** naming the modality (FR-477b, FR-477d) |
| `<name>`, promoted at the authorizing read, right modality | `ResolvedModel(name, promoted_version, modality, pinned=false)` — cache permitted only when the identity is already resident (FR-457c) |
| `<name>:<v>` where `<v>` **is** promoted at read-through | `ResolvedModel(..., pinned=true)` — never answered from cache (FR-457b) |
| `<name>:<v>` where `<v>` exists but is **not** promoted | `409 model_version_not_promoted`, body names the promoted version |
| `<name>:<v>` where `<v>` does not exist | `404 model_version_not_found` |
| `<name>` not registered | `404 model_not_found` |
| `<name>` registered, nothing promoted | `409 model_not_promoted` |
| `<name>` promoted for another modality | `400 model_wrong_modality`, body names the modality it is promoted for |
| a registered name ends in `:` + digits | `409 model_name_ambiguous` — indistinguishable from a qualified reference to a shorter name under FR-440a's rightmost-split rule |
| registry unreachable | `503 registry_unavailable` with `Retry-After` |

Every refusal body carries `{"error": {"code": ..., "message": ..., "type": "invalid_request_error"}}`
in the shape 026's contract already uses, so an unmodified OpenAI client surfaces it.

## Why `model_not_found` and `model_not_promoted` are different codes

The operator remedy differs — register it, versus promote it — and a client that cannot tell them
apart reports "unknown model" for a model the operator can see in the registry. This is the same
reasoning that separates `413 model_too_large` from `503 gpu_busy` in 026's contract: the status is
chosen by what the caller should *do*, not by what went wrong internally.

## Why FR-457a requires naming the promoted version

`model_version_not_promoted` has two very different causes:

- the client pinned a version that was **never** promoted — a client bug, permanent; or
- the client pinned the version that **was** promoted when it started, and promotion has since moved
  — an operational event, and re-resolving fixes it.

The response names the currently promoted version, so a client can tell these apart without an
operator. Without that, a well-behaved client retries forever against a moved pointer.

## The four defaults

An omitted `model` resolves per modality, and each modality needs a **designated authority** — one
place that names a concrete registry identity the runtime will also agree on (FR-477).

| modality | authority | ships today? | governs routing today? |
|---|---|---|---|
| LLM | `registry.active_serving_llm_name()` — the persisted **`ActiveServingLLM`** Postgres pointer, else sole-promoted adoption, else `DEFAULT_LLM`/`SERVING_MODEL` | yes, all three tiers | yes |
| ASR | `ASR_SERVING_MODEL` (`routers/transcribe.py:25`) | yes | **no — attribution only** |
| vision | `VISION_SERVING_MODEL` (`routers/vision.py:24`) | yes | **no — attribution only** |
| embeddings | `EMBED_SERVING_MODEL` | **no — new in 028** | n/a |

**The LLM row is a chain, not a pointer, and 028 calls it rather than restating it.**
`active_serving_llm_name()` (`gateway/app/registry.py:169-195`) resolves the explicit DB pointer
first, then adopts the sole promoted text-generation model
(`platformlib/llmresolve.py:121-134`), then falls back to `DEFAULT_LLM`. `SERVING_MODEL` is **tier
three**, not the authority. Resolving an omitted chat request through
`prefer_name=SERVING_MODEL` would name the base while a gated go-live has pointed
`ActiveServingLLM` at a fine-tune — FR-473a would then refuse a valid request, and skipping the
guard would recreate the divergence 022 closed. An earlier revision of this table presented
`SERVING_MODEL` as the LLM's routing pointer; it is the fallback base.

Three of the four pointers already exist, and the ASR and vision ones are already passed as
`prefer_name`. What they do *not* do is select what the request is routed to — both are read inside
best-effort `_resolve_*_version()` helpers documented as *"never raises"*, whose job is labelling a
prediction log. Nothing selects routing today, because the endpoint posts to a fixed engine slot. So
028 **promotes those two existing pointers from attribution to routing authority** and adds one for
embeddings, which has none (`EMBED_MODEL`, `routers/embed.py:43`, is a status string, not a registry
identity). An earlier revision of this table proposed creating a pointer for each of the three, which
would have duplicated shipped config under a second name.

**The three rules below govern embeddings, ASR, and vision only.** The LLM surface defers wholly to
`active_serving_llm_name()` above. Folding it in would regress rather than tighten: with several
promoted LLMs and no pointer, `adopt_active_llm` returns `None` and both the gateway *and* the agent
fall back to `DEFAULT_LLM`, so the pair still agrees and there is nothing to refuse.

**Pointer set** → `registry.resolve_serving_target(task, prefer_name=<pointer>)`. A second promoted
model sharing the task cannot displace the configured one, and the *"otherwise the first match is
used"* branch MUST NOT be taken. A pointer naming a model that is **not** promoted for the modality
is refused rather than served by whatever was found.

**Pointer unset** — every existing deployment. `prefer_name` is then `None`, so that same first-match
branch is the *only* path to an identity: resolution runs before any agent call (FR-439), so the
gateway cannot ask the runtime what it is serving. The rule is stated on the **candidate set**, not
on the ordering:

| promoted models carrying the task | unset-pointer outcome |
|---|---|
| exactly one | resolve to it — "first match" and "the designated model" are the same identity, the ordering is unobservable, and omission keeps working with **no operator action** |
| two or more | **409 `model_default_unconfigured`**, naming the modality *and the pointer to set* |
| none | **409 `model_default_unconfigured`** — already surfaced today as "not configured" |

Only the middle row differs from today's behaviour, and there today's behaviour is an arbitrary pick
among promoted models — the silent substitution this increment exists to end, not a working
configuration being taken away. That is what makes FR-444's back-compatibility requirement and
FR-477b's refusal compatible rather than contradictory.

**The repo already decided this once.** `registry.list_tasks()` (`gateway/app/registry.py:394-408`)
resolves the same ambiguity for the LLM listing: the sole promoted model is adopted when the pointer
is unset, and with several promoted and no active pointer it advertises **no** live LLM rather than
*"an arbitrary/nondeterministic `text_gen[0]` that contradicts what the agent serves"* (022, FR-276).
028 carries that across to the three non-LLM defaults rather than inventing a rule. The LLM's own
*resolution* path answers it differently — several-promoted falls back to `DEFAULT_LLM` instead of
refusing — and that is consistent, not contradictory: a listing that cannot name the live model must
say so, while a resolution has a configured base to fall back to **and the agent falls back to the
same one**.

**Configuration does not guarantee resolver/runtime agreement — on three of the four surfaces
(FR-477e).** For ASR, vision, and embeddings the gateway pointer and the agent-side identity are
separate, independently set variables with nothing binding them: `ASR_SERVING_MODEL` vs
`WHISPER_MODEL`/`WHISPER_MODEL_ALIAS` (`hostagent/adapters/whisper.py:44,45`),
`VISION_SERVING_MODEL` vs `VISION_MODEL` (`vision.py:36`), and the embeddings child naming no
registry identity at all. Agreement there is **detected, not prevented**: FR-473a's compare-and-pin
runs at the agent and refuses a divergence rather than serving it.

**The LLM surface is bound, and that is the point of 022 (FR-477f).** The gateway's
`active_serving_llm_name()` and the agent's `hostagent/serving_llm.py::resolve()` read the **same
persisted `ActiveServingLLM` pointer**, and the agent's resolver is documented as mirroring the
gateway's chain exactly — pointer, then adoption, then env default, matching even on a store outage.
Shared authority is what makes them agree; FR-473a's guard remains a backstop, not the mechanism. An
earlier revision listed `SERVING_MODEL` vs `MODEL`/`MODEL_ALIAS` here as an unbound pair, which is
true only of the chain's third tier.

`registry.resolve_serving_target(task)` is **not** an authority. It finds versions tagged for a task
and, when several models share one, its own docstring says *"otherwise the first match is used"* —
deterministic only while exactly one promoted model carries the tag, and silent when that stops being
true.

**Why this only becomes load-bearing now.** Omission works today because each endpoint posts to a
fixed engine slot, so no identity is ever chosen and nothing can disagree. Once FR-439 requires
resolving an omitted `model` to a concrete identity *before* the agent call, an arbitrary pick can
differ from the runtime that answers — and FR-473a's guard then refuses a request that works today,
or FR-458 reports an identity that did not serve. That is the 022 divergence, re-entering through
the default path (FR-477a).

## Caching

The cache is **split by volatility**, because the two halves of a resolution have different
correctness requirements:

| cached | volatility | policy |
|---|---|---|
| `name → modality` | a model's modality does not change | cacheable, long TTL |
| `name → promoted version` | moves whenever the `serving` alias moves | **never used to authorize a pin**; short TTL, unpinned only, and **only when the identity is already resident** (FR-457c) |

**A pinned request always reads through (FR-457b).** `name:version` asserts something about the
pointer *now* — "serve version 3, or tell me you can't". Answering that from a cache can authorize a
version the platform has already demoted, which is precisely what FR-457 exists to prevent. The
read-through cost lands only on pinned requests, which are the rare ones.

**An unpinned request may be answered from cache (FR-457c) only when the resolved identity is
already resident**, bounded by the TTL. The tolerance is not laxity: the tenant asked for "the
current model", and FR-458 requires the response to name the version that actually answered — so a
slightly stale resolution produces a truthful answer about a slightly older model, never a false
claim about a newer one. The already-resident qualifier is not a detail of the cache: truthful
reporting repairs a stale *answer*, but nothing repairs a stale **placement**, so a resolution that
would cause a load revalidates fresh regardless of the cache (FR-457e).

**Eager invalidation is an optimisation, not the guarantee (FR-457d).** An earlier revision of this
contract leaned on `registry.promote` invalidating in-process and called out-of-band alias moves
"unusual". They are not — the repo ships four scripts that move the alias directly:
`scripts/retag_serving_llm.py:49`, `scripts/seed_asr_model.py:33`,
`scripts/seed_embedding_model.py:52`, `scripts/seed_tabular_model.py:78`, none of which passes
through `registry.promote`. Eager invalidation therefore covers **one of five** writers. **The TTL is
the staleness bound**, and it must be documented as such.

**Only successful resolutions are cached.** A tenant-supplied string that resolves to nothing must
never create an entry — the key space would then be attacker-controlled and unbounded. This is the
identical trap `hostagent/metrics.py` documents for `malformed_length` metric labels, one layer up.

## Metrics

A single counter with a **bounded** `reason` vocabulary:
`model_not_found | model_not_promoted | model_wrong_modality | model_version_not_found |
model_version_not_promoted | model_name_ambiguous | model_default_unconfigured |
registry_unavailable`.

`model_name_ambiguous` is worth its own value rather than folding into `model_not_found`: it is the
one refusal in the set that indicts the **registry's contents** rather than the request, and an
operator watching it wants to see it separately.

`not_resident` is deliberately **absent** from this vocabulary. It is a routing refusal the agent
produces, not a resolution outcome — resolution succeeded — so it is counted on the placement side
of FR-464's split. Its code and status are defined in
[data-model.md](../data-model.md#refusal-codes-new-values-in-an-existing-vocabulary).

The model name is **never** a label. It is tenant-controlled, so labelling with it mints a permanent
time series per string any client has ever sent.

## Tests this contract owes

1. Each row of the outcomes table, asserting the **code** and, for the two version cases, the body
   naming the promoted version.
2. A resolution failure never reaches the agent — asserted by a fake agent client that fails the test
   if called.
3. A failed resolution does not grow the cache.
4. Every row runs in **both** positions of `BROKER_COORDINATOR_ADMISSION` (research R7). The default
   is off, and the default is what an operator has.
5. **The stale-pin case.** Resolve `name:3` (promoted, cached), move the alias to version 4 **without
   going through `registry.promote`** — as `scripts/retag_serving_llm.py` does — then request
   `name:3` again *within the TTL*. It must refuse `model_version_not_promoted` naming 4. A test that
   moves the alias via `registry.promote` exercises the eager-invalidation path and would pass
   against the defect.
6. The unpinned counterpart of the same setup may serve the stale version **only when it is already
   resident**, and its response must name the version it served — never the newly promoted one it
   did not.
7. **Each modality's default**, all five unset/set states above, asserted **per surface**. The
   back-compat half is the one that can regress and MUST come first: pointer unset with exactly one
   promoted model still answers (**omission keeps working**, FR-444/FR-477c). A suite that covers
   only the refusal states passes against an implementation that has broken every omitting caller on
   three surfaces. The two-or-more-promoted refusal must assert that the message names **the pointer
   to set**, not merely that the request was refused.
9. **The LLM default follows the persisted pointer, not the env fallback.** Set `ActiveServingLLM`
   to a fine-tune while `SERVING_MODEL` still names the base, then send a chat request with `model`
   omitted: it must resolve to the **fine-tune**. An implementation that resolves through
   `prefer_name=SERVING_MODEL` returns the base here and is then refused by FR-473a's guard against
   the agent that is serving the fine-tune — the 022 divergence, re-entered through the default path.
   Assert the resolved identity, not merely that the request succeeded. Also assert the tiers below
   it: pointer unset with one promoted LLM adopts it, and with several promoted it falls back to
   `DEFAULT_LLM` **without** refusing — the non-LLM `model_default_unconfigured` rule must not reach
   this surface.
8. **The ambiguous-name case is reached.** Register a model literally named `svc:chat:3`, request it,
   and assert `409 model_name_ambiguous` — not `404 model_not_found`. A resolver that applies
   FR-440a's split without the exact-name lookup first returns 404 and passes no test that only
   checks "it is refused", which is why the assertion is on the code.
