# Contract: Model-Addressed Inference (gateway ↔ host agent)

**Requirements**: FR-446–FR-450, FR-458–FR-460 · **Phase**: 1 (field accepted) / 2 (field honoured)

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
| `model_key` names a non-resident model, Phase 1 | refuse `gpu_busy` |
| `model_key` names a non-resident model, Phase 2+ | `Coordinator.admit_serving(model_key, est_bytes)`, then serve (FR-451–FR-454) |
| `model_key` names a model this engine cannot host | refuse — a routing error, not a placement one |

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
