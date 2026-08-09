# Contract: Minimum Residency Window

**Owner**: `hostagent/coordinator.py` · **Requirements**: FR-456, FR-456a, FR-456b, FR-456c, SC-209
· **Phase**: 2

## The rule

A model that has reached `resident` is **not an eligible eviction victim** until
`min_residency_s` has elapsed since `became_resident_at`, regardless of who requests the placement
and regardless of how idle it is.

## Why it exists

With per-model residency, two tenants alternating between models that cannot co-fit would evict each
other on every request. The coordinator would do it willingly — `_select_victims` sorts idle-first,
and a model that just answered and went idle is the *most* attractive victim. The result is one load
per request, which on a large model is seconds of GPU time producing nothing.

The window converts that into: **at most one eviction of a given model per window** (FR-456b), with
the losing request told exactly how long to wait.

## Interaction with eligibility

`_select_victims` filters `state == RESIDENT and materialized` today. The window is a third
conjunct. Because it can make an otherwise-sufficient victim set unavailable, `_select_victims` must
report **why** it came back empty rather than returning a bare `[]`:

| `blocked_by` | meaning | answer |
|---|---|---|
| `None` | victims found | proceed to eviction |
| `insufficient` | even evicting everything eligible does not satisfy both bounds | `413 model_too_large` if `est_bytes > capacity − headroom`, else `503 gpu_busy` |
| `transient` | candidates exist but are draining/evicting/unmaterialized | `503 gpu_busy`, generic `Retry-After` |
| `residency_window` | a sufficient set exists but is inside its window | `503 gpu_busy`, `Retry-After` = remaining time on the **earliest** sufficient victim's window |

The last row is the one that would be silently lost if `_select_victims` kept returning a bare `[]` —
and losing it turns a client that retries once into a client that polls.

## `Retry-After` is a lower bound, honestly labelled

The value says **when the window expires**, not when the eviction will happen. Another tenant's
traffic may keep the model busy past that point. Promising the eviction would be promising something
no other tenant agreed to; the contract promises only that the *window* is no longer the obstacle.

## What the window does not touch

- **Exclusive jobs** (FR-456c). A job's whole-GPU claim is governed by FR-455 — never preempted,
  which is strictly stronger than a window. A job's *release* is not an eviction and is not delayed.
- **Operator-initiated swap.** `POST /control/reload` is the operator's path and is `CONTROL_SECRET`-
  gated; the window bounds *tenant-initiated* thrash. An operator who has decided to swap is not
  thrashing. Whether the operator path should also honour the window is a deliberate **no**: it would
  make a deliberate operational action fail for a reason the operator cannot see from the console.
- **Idle-release.** A model releasing itself for inactivity is not an eviction by a competing
  placement, and 026's idle-release path is unchanged.

## `min_residency_s = 0`

Disables the window and restores exact 026 eviction behaviour. This is what the Phase-2
characterization tests set it to, so a regression in ordinary victim selection is distinguishable
from a regression in the window.

## Tests this contract owes

1. A model resident for less than the window is not selected as a victim, even when it is idle and
   is the LRU candidate.
2. A placement blocked **only** by the window returns `gpu_busy` with `Retry-After` equal to the
   remaining window time, within tolerance — asserted on the number, not merely on its presence.
3. Honouring that `Retry-After` and retrying once succeeds.
4. An alternating two-model workload over N windows performs ≤ N evictions, not one per request
   (SC-209).
5. `min_residency_s = 0` reproduces 026's victim selection exactly — the characterization suite
   passes unchanged.
6. The window never delays or blocks a job's admission or release (FR-456c).
7. `became_resident_at` is set on the transition **into** `resident`, not at entry creation: a model
   that spends a long time `loading` does not arrive with its window already partly spent.
