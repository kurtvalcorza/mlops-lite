# Quickstart / Validation: 028 Model-Selective Serving

How to prove each phase actually works. Phase 1 needs no GPU; Phases 2 and 3 are hardware-gated per
Principle VII and are not "done" on a passing suite alone.

## Tunables introduced

| tunable | where | default | notes |
|---|---|---|---|
| `min_residency_s` | coordinator config; documented in `.specify/memory/hardware-profile.md` with 026's other admission tunables | **60 s** — long enough that an alternating workload cannot thrash, short enough that a genuine model switch is not an outage | `0` disables the window and restores exact 026 behaviour |
| `vram_estimate_multiplier` | coordinator/adapter config | **1.2** | applied to artifact size; must be ≥ 1 (research R3 — under-estimating is the dangerous direction) |
| `model_resolution_ttl_s` | gateway | **30 s** | **the staleness bound itself**, not a backstop (FR-457d). Four of the five alias writers in this repo bypass `registry.promote` (`retag_serving_llm.py:49`, `seed_asr_model.py:33`, `seed_embedding_model.py:52`, `seed_tabular_model.py:78`), so eager invalidation covers one writer in five — size this for the ordinary case, not an unusual one. Applies only to serving an **already-resident** version; pins read through and placements revalidate fresh |
| `SERVING_MODEL`, `ASR_SERVING_MODEL`, `VISION_SERVING_MODEL`, `EMBED_SERVING_MODEL` | gateway env | **unset** for the three non-LLM pointers, which is what every existing deployment has | FR-477c. Only `EMBED_SERVING_MODEL` is new; the other three ship today, and the two non-LLM ones are **attribution-only** (`transcribe.py:25`, `vision.py:24`) until this increment gives them routing authority. Unset resolves iff **exactly one** promoted model carries the modality's task — two or more is `409 model_default_unconfigured` naming the pointer. Read the operator note below before deploying |
| `host_ram_budget_bytes` | coordinator config; `hardware-profile.md` | **calibrated in T817, not guessed** | FR-467. An admission **precondition**, checked before the spawn: terminating a child does reclaim its memory, but nothing gives host RAM back *within* the request, so transient overcommit during load is the failure being avoided |
| `host_ram_estimate_bytes` | per-adapter default with per-model override | **derived from T817's measurement** | FR-469. The precondition's left-hand side; measured **PSS, never RSS** (FR-471), or an RSS sum double-counts mmap'd GGUF pages and refuses placements that fit |

**Operator note — the one behaviour change to an omitting caller.** Requests that omit `model` keep
working untouched on any modality with exactly one promoted serving model, with no pointer set. The
single case that changes is a modality where **two or more** promoted models carry the task and no
pointer is set: today one of them is picked arbitrarily by `resolve_serving_target`'s *"otherwise the
first match is used"* branch, and after 028 the request is refused **409
`model_default_unconfigured`** until the pointer names which one. Check before deploying:

```bash
python -c "from collections import defaultdict; from gateway.app import registry; d=defaultdict(list); [d[e['task']].append(e['model']) for e in registry.list_tasks()]; print(dict(d))"
```

Any task listing more than one model needs its pointer set first. `text-generation` will under-report
here — `list_tasks()` already filters it to the active LLM pointer (FR-276) — which is fine, because
that is the one modality whose pointer has governed routing all along.

This is the only refusal in the increment that can turn a currently-working request into an error,
which is why it is called out here rather than left to the contract. It is also not a new policy:
`registry.list_tasks()` already resolves this exact ambiguity the same way for the LLM listing —
sole promoted model wins, several with no active pointer advertises **nothing** rather than *"an
arbitrary/nondeterministic `text_gen[0]` that contradicts what the agent serves"* (`registry.py:394-408`).

## Phase 1 — resolution and truth (no GPU required)

```bash
.venv/Scripts/python.exe -m pytest tests/test_broker_openai_resolution.py -q
```

Run the same file with the flag in **both** positions. The first command above inherits the default,
which is `"0"` — so it is the off position, and the off position is what an operator actually has.
The on position must be set explicitly:

```bash
BROKER_COORDINATOR_ADMISSION=0 .venv/Scripts/python.exe -m pytest tests/test_broker_openai_resolution.py -q
BROKER_COORDINATOR_ADMISSION=1 .venv/Scripts/python.exe -m pytest tests/test_broker_openai_resolution.py -q
```

An earlier revision of this procedure showed two commands that both ran with the flag off, so the
on position was never exercised despite the heading.

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

The listing spans **all** modalities and tags each entry — there is only one `/v1/models`, so it is
not filtered per surface (FR-461). Send one request per listed id **to the endpoint its modality
names**; each must be served by that model, or refused for a placement reason, or — if you send it to
the wrong endpoint on purpose — refused `model_wrong_modality`. What must never happen is being
served by a *different* model (**SC-204**).

Confirm residency is sourced from the agent, not the registry: stop the agent and re-list. The
`resident` field must be **absent**, not `false` (FR-462a).

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

- expect `503` with code `gpu_busy` and a `Retry-After` equal to when a **sufficient victim set**
  becomes eligible — assert the **number**, not merely its presence, and include a multi-victim case,
  since a single-victim-only check passes against the superseded earliest-victim rule;
- sleep that long, retry once, expect success — **with no competing traffic in the interval**.
  `Retry-After` is a lower bound (FR-456a): it says when the window stops being the obstacle, not
  that no other tenant will take the slot;
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

Unlike VRAM, this is checked **before** the spawn — once a child has allocated host RAM the
coordinator cannot take it back. It **is** still reconciled to the measured value after the spawn
(FR-472), which reclaims nothing for the request that just ran but leaves the *next* admission
deciding against a real number. An earlier revision of this file said it was "never reconciled
afterwards", which contradicted FR-472.

Measure on a **PSS** basis (FR-471). Two children sharing mmap'd GGUF pages each report those pages
in full under RSS, so an RSS sum over-counts and the bound would refuse placements that fit.

## What "done" requires

- Phase 1: suite green in **both** flag positions, and the two `curl` checks above performed against
  a running gateway — not inferred from unit tests.
- Phases 2 and 3: the above **on the target GPU host**. A passing suite on a machine without the GPU
  is not evidence that co-residency or eviction works; 026 phase-gated for this reason.
- A recorded **baseline** test count from before the change to diff against. "No regressions" without
  a starting number is not a claim, per the workspace testing standard.
