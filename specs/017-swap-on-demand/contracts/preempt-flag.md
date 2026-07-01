# Contract: `preempt` flag on serving routes + gateway swap orchestration — FR-154/155/157

## The flag

Serving requests (`/infer`, `/infer/stream`, `/vision/classify`, `/transcribe`) accept an optional
**`preempt`** field (default **false**). It is the explicit opt-in to swap a resident **serving** model.

```json
{ "...normal serving body...", "preempt": false }
```

- **`preempt` omitted/false** ⇒ behavior is **identical to 008** — refuse-if-held (`409 GPU busy`) when a
  model is resident. **No regression** (SC-101).
- **`preempt: true`** ⇒ the gateway runs the swap orchestration below.

## Gateway swap orchestration

```
on a serving request with preempt=true:
  holder = current_holder()                       # via /serving/state
  if holder is None:                  serve normally (no swap)
  elif holder is a SERVING tenant:    POST /unload-now to the holder
                                      wait until the lease frees (FR-157)
                                      forward the request to the TARGET serving daemon
  elif holder is TRAINING/HPO/BATCH:  REFUSE — 409 { "detail": "training in progress — not preemptable" }
```

| Case | Response |
|---|---|
| Swap succeeds | `200` with the normal serving result (after the swap) |
| Holder is training/HPO/batch | `409 { "detail": "training in progress — not preemptable" }` (FR-155 / SC-102) |
| Target fails to load after eviction | the target's load error surfaced (FR-159) — not a silent wedge |
| No holder | normal serve (no swap) |

## Invariants

- **One model in VRAM** throughout the swap — sequential evict → free → load (FR-158 / SC-100). Concurrent
  `preempt=true` requests serialize on the lease.
- **Training is never preempted** (FR-155 / SC-102).
- **Default (no `preempt`) is byte-for-byte 008** (FR-161 / SC-101).

## UI (FR-160)

The Infer tab replaces the 008/A1 "classify disabled — GPU busy" state with a **"Swap & classify"** action
that states the cost ("evicts the resident LLM, ~2.5s reload") and sends `preempt=true` on confirm. The BFF
allowlist passes the flag through.
