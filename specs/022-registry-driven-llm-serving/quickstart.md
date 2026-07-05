# Quickstart: Registry-Driven LLM Serving — validation drills

Per-story drills proving the LLM becomes a registry-driven, console-operable engine. Backend behavior
is covered by `pytest` (offline, fake registry/store); the live serve/switch/identity behaviors and
Principle II are validated on hardware with the stack up. Contracts:
[serving-resolution](./contracts/serving-resolution.md),
[agent-identity-and-allowlist](./contracts/agent-identity-and-allowlist.md).

Prereqs: `pytest` green (backend suite), `next build` green (console), stack up
(`scripts/up_all.ps1` + host agent), at least two registered text-generation models — a base (e.g.
`qwen2.5-7b-instruct-q4_k_m`) and a fine-tune (e.g. `ops-bot-v2`) — with their bases registered.

## US1 — Promote an LLM and it actually serves (P1)

1. From `/models`, promote text-generation version A to serving. Expected: within one live-status
   cadence the console shows A as the serving LLM; `POST /infer` is produced by A — **no `.env` edit,
   no manual agent restart**.
2. Promote version B. Expected: a controlled reload; subsequent `/infer` is produced by B.
3. Backend: `pytest` proves the resolver maps the active pointer → `@serving` version → artifact, and
   that a set-serving triggers exactly one reload request.

## US2 — Honest served-model identity (P1)

1. With a non-default LLM (e.g. `ops-bot-v2`) serving, read `GET /serving/state`. Expected:
   `serving_model` + `serving_version` name **that** model+version (agent-reported), not the config
   default.
2. Run several `/infer` calls, then run a quality check for that model+version. Expected: the logged
   predictions are attributed to that model+version and are included in its window (not `qwen…`).
3. Regression of the live bug: confirm `/serving/state`, the `/infer` response `model`, and the logged
   prediction identity **all agree** — no `model: ops-bot-v2` vs `serving_version: 23` divergence.

## US3 — Serve LoRA fine-tunes (base + adapter) (P2)

1. Promote a LoRA fine-tune whose training instilled a distinguishing behavior (e.g. the `ops-bot`
   sign-off). Expected: the resolver loads `-m <base> --lora <adapter>` (base resolved from lineage,
   no operator entry); `/infer` shows the fine-tuned behavior.
2. Promote back to a full base version. Expected: the adapter is dropped; base behavior serves.
3. Promote a fine-tune whose base is unregistered/absent. Expected: the promote is refused with a
   clear reason; the currently-served LLM is unchanged (never wedged).

## US4 — Fine-tunes are first-class serving targets (P2)

1. Register a new text-generation fine-tune. Expected: `GET /serving/tasks` shows it with
   `task=text-generation` (not null) + base/adapter `kind` + lineage; `/models` lists it as promotable.
2. Run `scripts/backfill_llm_task_tags.py`. Expected: pre-existing untagged LLM versions
   (`ops-bot-v1/v2`) become `task=text-generation` and selectable; the backfill is idempotent
   (re-run changes nothing) and never clobbers other tags.
3. With a fine-tune promoted, open `/serving`. Expected: a working LLM inference panel (not a
   read-only "no renderer" placeholder).

## US5 — Safe switch under the single-GPU lease (P2/P3, [HW])

1. With a serving model resident, switch the served LLM from the console. Expected: the
   `ConfirmDialog` names the model to be displaced; on confirm the reload is sequential.
2. Observe lease/VRAM through the switch. Expected: **never two models resident** (Principle II /
   SC-147) — check `serving/state` + `nvidia-smi` across the evict→load.
3. Start a training run (holds the GPU), then request a served-LLM switch. Expected: refused/deferred
   with a clear reason; the training job completes uninterrupted (SC-150).
4. Switch to the already-serving version. Expected: idempotent no-op (no gratuitous evict/reload).

## Regression gate (all stories)

- `pytest` backend suite green (resolver, identity reporting, promote→reload, backfill) — no
  pre-existing test regressed (Principle II admission tests still pass).
- `next lint` + `next build` green for the console.
- Grep check: every gateway call in `ui/` resolves to a `gw-allowlist.ts` entry; the allow-list delta
  equals the additions in [agent-identity-and-allowlist.md](./contracts/agent-identity-and-allowlist.md)
  (ideally empty — prefer reusing existing routes).
- **[HW]**: on the RTX 5070 Ti — a full promote→serve→infer→monitor loop for a fine-tune, the
  never-two-resident switch, and the job-never-preempted refusal, all live.
