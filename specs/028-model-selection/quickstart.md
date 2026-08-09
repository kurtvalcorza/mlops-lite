# Quickstart / Validation: 028 Model-Selective Serving

How to prove each phase actually works. Phase 1 needs no GPU; Phases 2 and 3 are hardware-gated per
Principle VII and are not "done" on a passing suite alone.

## Tunables introduced

| tunable | where | default | notes |
|---|---|---|---|
| `min_residency_s` | coordinator config; documented in `.specify/memory/hardware-profile.md` with 026's other admission tunables | **60 s** — long enough that an alternating workload cannot thrash, short enough that a genuine model switch is not an outage | `0` disables the window and restores exact 026 behaviour |
| `vram_estimate_multiplier` | coordinator/adapter config | **1.2** | applied to artifact size; must be ≥ 1 (research R3 — under-estimating is the dangerous direction) |
| `model_resolution_ttl_s` | gateway | **30 s** | eagerly invalidated by `registry.promote`, so this bounds only out-of-band alias moves |
| `host_ram_budget_bytes` | coordinator config; `hardware-profile.md` | **calibrated in T817, not guessed** | FR-467. An admission **precondition**, checked before the spawn — host RAM cannot be reclaimed once a child has allocated it |

## Phase 1 — resolution and truth (no GPU required)

```bash
.venv/Scripts/python.exe -m pytest tests/test_broker_openai_resolution.py -q
```

Run the same file with the flag in both positions — the default is off, and the default is what an
operator has:

```bash
BROKER_COORDINATOR_ADMISSION=0 .venv/Scripts/python.exe -m pytest tests/test_broker_openai_resolution.py -q
```

**The one check that decides whether the P1 finding is closed.** With model A resident, ask for a
different promoted model B and confirm you do **not** get A's answer:

```bash
curl -s -X POST localhost:8000/v1/chat/completions \
  -H 'Authorization: Bearer <tenant-key>' -H 'Content-Type: application/json' \
  -d '{"model":"<B>","messages":[{"role":"user","content":"hello"}]}'
```

Expected: a refusal naming B. **Not** a completion. Assert on the body, not the status — before 028
this returned `200` with A's tokens and `"model": "<A>"`, so a status-only check passes against the
defect.

Then confirm the listing no longer advertises what cannot be selected:

```bash
curl -s localhost:8000/v1/models -H 'Authorization: Bearer <tenant-key>'
```

Every `id` returned must be selectable on this surface. Send one request per listed id; each must be
served by that model or refused for a *placement* reason — never served by a different model
(**SC-204**).

## Phase 2 — model-keyed admission and the window (GPU required)

```bash
BROKER_COORDINATOR_ADMISSION=1 .venv/Scripts/python.exe -m pytest tests/test_agent_coordinator.py tests/test_broker_coadmission.py -q
```

**Characterization first.** Set `min_residency_s=0` and confirm 026's victim-selection suite passes
**unchanged**. A regression in ordinary eviction must be distinguishable from a regression in the
window; if this step is skipped the two are indistinguishable for the rest of the phase.

**On-demand placement.** With A resident and B promoted-but-absent, request B and watch the resident
set turn over:

```bash
curl -s localhost:8000/admin/queue -H 'Authorization: Bearer <admin-key>' | python -m json.tool
```

(`/admin/queue` proxies the agent's `/gpu/queue` verbatim.) Confirm B becomes resident, that `accounted`
plus `reserved` never exceeds `usable_capacity` at any poll, and that the journal records the
placement and any eviction with its deciding bound (**FR-463**, **SC-207**).

**The window.** With `min_residency_s=60`, load A, then immediately request B where B cannot co-fit:

- expect `503` with code `gpu_busy` and a `Retry-After` close to the remaining window — assert the
  **number**, not merely its presence;
- sleep that long, retry once, expect success (**FR-456a**);
- drive an alternating A/B workload for N windows and count evictions in the journal: ≤ N, not one
  per request (**SC-209**).

**Job exclusivity.** Start a training job, then request any model. Expect `gpu_busy` with
`Retry-After`, the job **not** preempted, and no eviction attempted (**FR-455**).

## Phase 3 — LLM co-residency (GPU required)

Two LLMs that both fit within `usable_capacity` are both resident, and neither request evicts the
other:

```bash
curl -s localhost:8000/admin/queue -H 'Authorization: Bearer <admin-key>' | python -m json.tool
```

Expect two entries in `residents`, both `resident`, with `accounted + reserved ≤ usable_capacity`
throughout. Interleave requests to both and confirm each answer names its own model.

**Also measure host RAM, then enforce it.** Principle III bounds idle infrastructure at ~3 GB.
Record resident host RAM with one LLM and with two, set `host_ram_budget_bytes` from those figures
(FR-468), and confirm the bound actually refuses: set the budget below the two-child figure and check
that the second placement is **refused**, not admitted. If the real delta puts the platform outside
the budget, that is a Phase-3 finding to report, not a number to omit.

Unlike VRAM, this is checked **before** the spawn and never reconciled afterwards — once a child has
allocated host RAM the coordinator cannot take it back.

## What "done" requires

- Phase 1: suite green in **both** flag positions, and the two `curl` checks above performed against
  a running gateway — not inferred from unit tests.
- Phases 2 and 3: the above **on the target GPU host**. A passing suite on a machine without the GPU
  is not evidence that co-residency or eviction works; 026 phase-gated for this reason.
- A recorded **baseline** test count from before the change to diff against. "No regressions" without
  a starting number is not a claim, per the workspace testing standard.
