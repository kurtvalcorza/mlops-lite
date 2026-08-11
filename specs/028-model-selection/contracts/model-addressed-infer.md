# Contract: Model-Addressed Inference (gateway ↔ host agent)

**Requirements**: FR-446–FR-450, FR-458–FR-460, FR-473–FR-473c · **Phase**: 1 (field **asserted**) / 2 (field drives placement)

> **Corrected after the second PR #88 review.** This contract previously said the field was merely
> *"accepted"* in Phase 1 and *"honoured"* in Phase 2. That left Phase 1 without closure of the
> defect. **Phase 1 asserts the field at the agent** — a guard, not placement.
>
> **Corrected again after the PR #88 code review.** The paragraph above used to justify that by
> describing the gateway as reading residency from a separate `GET /health`
> (`gateway/app/serving.py:49`) and then issuing the inference as a second call. **That read does not
> happen on this surface.** `gateway/app/routers/broker_openai.py` posts straight to
> `{SERVING_URL}/infer` (line 260) and `{SERVING_URL}/infer/stream` (line 301); `serving.health()`
> exists and is used by other routers, not by the OpenAI path. The accurate statement is stronger,
> not weaker: **the surface asserts identity nowhere at all** — it posts to a fixed modality slot and
> whatever occupies it answers. A gateway-side fix would have to *introduce* the residency read, and
> the check-then-use pair with it. That is why the assert belongs at the agent, and it is what the
> tests below must construct rather than observe.

## The change

`POST /engines/{engine_id}/infer` gains an optional request field:

```json
{ "model_key": "support-assistant@3", "prompt": "...", "max_tokens": 256 }
```

**No new agent route.** The agent's top-level route set is asserted by
`tests/test_console_allowlist.py` and frozen by 024's
[preservation contract](../../024-deepen-modules-seams/contracts/preservation.md). Adding a field to
an existing verb keeps that tripwire intact; adding `/models/{key}/infer` would require editing the
test that exists to catch exactly this kind of surface growth.

## Semantics

| condition | agent behaviour |
|---|---|
| `model_key` absent | today's behaviour — serve whatever occupies the engine. Preserves every existing caller |
| `model_key` names the resident model | serve it |
| `model_key` names a model in a transient state (`loading`/`draining`/`evicting`/`rolling_back`) | await the owning operation (026 T675's branch) or refuse `gpu_busy` — **never** fall through to another resident (FR-449) |
| `model_key` names a non-resident model, Phase 1 | refuse **`not_resident`** — permanent, no `Retry-After`: Phase 1 has no placement path, so a transient code would send the client into an unbounded retry (FR-473b) |
| `model_key` names the resident model, any phase | **compare and take the claim under the resident-set lock, then dispatch with the lock released** (FR-473a) — `_lock` guards state only and is never held across load/unload (`coordinator.py:24-29`, enforced by `LifecycleGuard`), and the claim is what keeps the matched resident alive for the generation |
| `model_key` names a non-resident model, Phase 2+, **no placement authorization** | refuse `not_resident` — **never** auto-admit (FR-474) |
| `model_key` names a non-resident model, Phase 2+, **with a valid placement authorization** | `Coordinator.admit_serving(model_key, est_bytes)`, then serve (FR-451–FR-454) |
| placement authorization present but lapsed, or naming a different version | refuse; do **not** treat a lapsed authorization as still good (FR-474b) |
| `model_key` names a model this engine cannot host | refuse — a routing error, not a placement one |

### Why placement is two-step

Only the gateway can read the registry; only the agent knows residency atomically. An agent that
admits whatever `model_key` arrives will place a version resolved from a **stale cache**, which
defeats FR-457e's fresh-read requirement — the check would exist in the gateway and be unenforceable
at the point that acts on it.

So the flow is explicit (FR-474a):

1. **Inference, unauthorized.** Carries the expected identity, no placement authorization. Served if
   resident; otherwise refused `not_resident` — a code deliberately distinct from contention, because
   the gateway's next move differs.
2. **Fresh revalidation.** The gateway re-reads the promotion pointer. If the identity is no longer
   promoted, the tenant is refused and **nothing is placed**.
3. **Inference, authorized.** Re-issued carrying an authorization naming the exact `(name, version)`
   validated, with a short validity bound. The agent places **that** version and no other.

**This narrows the window; it does not close it** (FR-474c). The registry read and the admission are
in different processes, so the alias can still move between steps 2 and 3. The guarantee is the one
that matters: tenant traffic can never place a version resolved from a cache of arbitrary age — only
one confirmed promoted within the authorization's validity. Claiming exactness here would be
claiming a cross-process atomicity the design does not have.

**`model_key` absent must keep working.** `gateway/app/routers/infer.py`,
`gateway/app/platform_health.py`, and `training/flows/batch_infer.py` all call the engine today
without one (research R8). Making the field required would break three callers this increment does
not scope.

## The response is the truth

The agent returns the identity that **produced** the tokens:

```json
{ "completion": "...", "serving_model": "support-assistant", "serving_version": "3", ... }
```

The gateway reports *that*, not the caller's string. Today
`broker_openai.py:274` reads `payload.get("serving_model") or body.model` — the `or` is precisely the
substitution-hiding branch, because it falls back to echoing the request when the agent did not say
what answered. After 028 an absent `serving_model` is an **error**, not a cue to echo (FR-460).

Streaming carries the same identity on its chunks and terminal event (FR-459). Today
`broker_openai.py:320` puts `body.model` on every streamed chunk unconditionally — the streaming path
is currently *less* truthful than the non-streaming one, and a client that only ever streams cannot
detect a substitution at all.

## Concurrency

Concurrent requests for the same non-resident `model_key` produce **one** load; later arrivals join
the in-flight operation as waiters (FR-450). The coordinator already implements this —
`Reservation.waiters` and `_adopt_disposition` (`coordinator.py:560`) — so this is a call-site
obligation, not new machinery.

## Tests this contract owes

1. A request naming a resident model reaches that model's child; a request naming a different
   resident reaches the other. Asserted on the child, not on the response echo.
2. With model A resident, a request naming non-resident B returns a refusal — **and the test asserts
   the response body is not A's completion**. A status-only assertion would pass against the defect.
3. Absent `model_key` behaves exactly as before, for all three existing callers.
4. Two concurrent requests for the same non-resident key produce exactly one load.
5. A streamed response's chunks name the served identity, not the request string.
6. **The TOCTOU case.** Resolve against model A, drive a swap to B **concurrently with the in-flight
   request**, and confirm the request is refused rather than answered by B. A test that never
   interleaves a swap passes against the race, which is why this is its own case rather than a
   variant of case 2. **The interleaving point must be constructed, not observed**: the OpenAI path
   performs no residency read today, so the case is written against the two-call check-then-use pair
   a *gateway-side* fix would introduce, and must fail against that implementation while passing
   against the agent-side compare-and-claim.
7. The assert never waits for a load, evicts anything, or consults the VRAM bounds (FR-473b) —
   asserted with a coordinator stub that fails the test if admission is called.
