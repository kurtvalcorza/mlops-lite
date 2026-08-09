# Contract: Model Resolution

**Owner**: `gateway/app/modelresolve.py` (new) · **Consumers**: `gateway/app/routers/broker_openai.py`
· **Requirements**: FR-439–FR-445, FR-457, FR-457a, FR-461, FR-464

Resolution turns the request's `model` string into a `ResolvedModel` or a refusal. It runs **before**
any call to the host agent, so a refusal here costs no GPU work.

## Request grammar

```text
model := ""                     ; absent or empty → the platform default (FR-444)
       | <name>                 ; → that model's promoted serving version
       | <name> ":" <version>   ; an ASSERTION that <version> is the promoted one (FR-457)
```

`<name>` is a registered model name. `<version>` is a registry version string.

**The qualifier is an assertion, not a selection.** `support-assistant:3` means *"serve version 3, or
tell me you can't"* — never *"load version 3"*. A tenant request can therefore never place a version
that 022's gated promotion path did not promote, and 011's evaluation gate remains the only way a
version becomes servable.

## Outcomes

| input | outcome |
|---|---|
| `""` / absent | `ResolvedModel` for the platform default; response names what answered |
| `<name>`, promoted, right modality | `ResolvedModel(name, promoted_version, modality, pinned=false)` |
| `<name>:<v>` where `<v>` **is** the promoted version | `ResolvedModel(..., pinned=true)` |
| `<name>:<v>` where `<v>` exists but is **not** promoted | `409 model_version_not_promoted`, body names the promoted version |
| `<name>:<v>` where `<v>` does not exist | `404 model_version_not_found` |
| `<name>` not registered | `404 model_not_found` |
| `<name>` registered, nothing promoted | `409 model_not_promoted` |
| `<name>` promoted for another modality | `400 model_wrong_modality`, body names the modality it is promoted for |
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

`name → (version, modality)` is cached with a bounded TTL and invalidated eagerly by
`registry.promote`, which runs in the same process.

**Only successful resolutions are cached.** A tenant-supplied string that resolves to nothing must
never create an entry — the key space would then be attacker-controlled and unbounded. This is the
identical trap `hostagent/metrics.py` documents for `malformed_length` metric labels, one layer up.

## Metrics

A single counter with a **bounded** `reason` vocabulary:
`model_not_found | model_not_promoted | model_wrong_modality | model_version_not_found |
model_version_not_promoted | registry_unavailable`.

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
