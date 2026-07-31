# Contract — Interactive Sessions API

Gateway routes, `Authorization: Bearer <tenant-key>`. A session is a GPU lease with idle-cull + TTL guard
rails (research R9). → FR-020, FR-021, FR-022.

> **GATED — the admission class is undecided (T665).** This contract deliberately does **not** say how a
> session is admitted. An earlier revision called it a *serving-class lease*, which would have made a
> notebook an ordinary evictable co-resident — but a kernel's VRAM is allocated by tenant code after
> admission, so it has none of the fixed, reconcilable footprint FR-024's accounting depends on;
> co-residing one either overcommits VRAM or destroys live tenant state when eviction selects it. The
> class (exclusive · sandboxed job · a distinct class with a hard per-session cap) is a P5 decision with a
> possible constitution implication — see [research.md](../research.md) R9, `spec.md` Dependencies, and
> T665. **Everything below is the session's shape and guard rails, which hold under any class; nothing
> below authorizes implementing session admission before T665 closes.**

## POST /sessions  *(start)* → FR-020
`{ "idle_timeout_s": 600, "ttl_s": 7200 }` → `201 { "session_id", "notebook_url", "expires_at" }`.
Subject to owner policy (may be disabled per tenant).

## GET /sessions/{id} → `{ id, state, last_gpu_activity_at, last_heartbeat_at, expires_at }`
(state: active|idle|released|expired). Both timestamps are exposed because only the first one is what
idle-cull acts on — a tenant asking "why did my GPU go away while my notebook was open?" needs to see the
difference rather than infer it.

## POST /sessions/{id}/heartbeat  *(liveness ping)*
Keeps the **session/kernel** alive. It does **not** reset the GPU idle timer — see below.

## DELETE /sessions/{id}  *(end)* → releases the GPU immediately.

## Guard rails (enforced by the host agent)

**Two independent timers, and only one of them a client can pet.**

| Timer | Reset by | Expiry |
|---|---|---|
| GPU idle | **admitted GPU work only** — a cell that actually runs on the device | GPU lease released, `state=released` (FR-021, SC-007) |
| Session liveness | `POST /heartbeat`, or any GPU work | session torn down; no GPU lease is held by then |
| TTL (`ttl_s`) | nothing — absolute | `state=expired`, GPU freed (FR-021) |

Notebook clients heartbeat automatically, on a fixed interval, whether or not any cell is running — that
is what a kernel liveness ping *is*. So if a heartbeat reset the GPU idle timer, an abandoned notebook
left open in a browser tab would hold its GPU lease for the full `ttl_s` while doing nothing, and
SC-007's "idle session releases within its idle window" would be unobservable in exactly the case it
exists to cover. Idle-cull therefore keys on GPU activity the coordinator actually admitted, never on
client-supplied liveness.

Releasing the GPU lease does **not** by itself kill the session: a `released` session keeps its kernel and
re-acquires a lease on the next GPU cell (`released → active`), subject to admission like any other
request — so it may be queued or refused, and whatever T665 decides applies. Only TTL expiry and an
explicit `DELETE` are terminal; see [data-model.md](../data-model.md) §session transitions. This is the
point of separating the timers — an idle notebook gives back the GPU without losing the tenant's in-memory
work, so giving it back is not a decision the tenant has to weigh.

- Under contention a session is the **lowest-priority** GPU holder (R9) — under any admission class.

## Notebook-as-job helper → FR-022
Training from a session SHOULD use the jobs path, not the kernel. Client helper
`broker.finetune(...)` / `%%gpu` submits a job (see jobs-api) and streams logs into the notebook, so the GPU
is held only for the run — not the whole session.
