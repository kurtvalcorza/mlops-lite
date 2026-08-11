# Contract: Model Resolution

**Owner**: `gateway/app/modelresolve.py` (new) · **Consumers**: `gateway/app/routers/broker_openai.py`
· **Requirements**: FR-439–FR-445, FR-440a–FR-440c, FR-457–FR-457f, FR-461, FR-464, FR-475–FR-475b

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
| `""` / absent | `ResolvedModel` for the **calling endpoint's modality** default; response names what answered (FR-444 — a single platform-wide default would refuse every omitting caller on three of the four surfaces) |
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
model_version_not_promoted | model_name_ambiguous | registry_unavailable`.

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
7. **The ambiguous-name case is reached.** Register a model literally named `svc:chat:3`, request it,
   and assert `409 model_name_ambiguous` — not `404 model_not_found`. A resolver that applies
   FR-440a's split without the exact-name lookup first returns 404 and passes no test that only
   checks "it is refused", which is why the assertion is on the code.
