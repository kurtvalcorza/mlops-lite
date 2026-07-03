# Phase 0 Research: 018 Platform Re-Architecture

**Input**: [plan.md](plan.md) Technical Context unknowns + two clarify-deferred items + the
constitution wording question. Each entry: Decision / Rationale / Alternatives considered.

## R1. GPU telemetry: `pynvml` with TTL cache

- **Decision**: The agent reads free VRAM via NVML bindings (`pynvml`, host venv only), wrapped
  in a ~1s TTL cache; admission takes a fresh read, health/status endpoints serve the cached
  value. `nvidia-smi` remains only as a fallback probe when NVML init fails (and in Gate Zero).
- **Rationale**: Closes the fork-storm finding (review §4.4): today every `/health` forks
  `nvidia-smi` (5s timeout) and the UI polls every 4s. NVML is an in-process C call —
  microseconds, no subprocess, no PATH/driver-CLI coupling. SC-110's "zero per-poll forks"
  requires it.
- **Alternatives**: keep `nvidia-smi` + cache (still forks on cache miss; slower; parses text);
  DCGM exporter container (new resident service — Principle III violation; also blocked by the
  no-GPU-passthrough container engine).

## R2. `platformlib/` is stdlib-only

- **Decision**: The shared contracts package uses dataclasses + explicit `validate()` helpers;
  no pydantic dependency. The gateway keeps converting to pydantic response models at its edge,
  as it already does.
- **Rationale**: The package is imported by two runtimes with independently-pinned environments
  (gateway image vs `~/mlops-train` host venv). A pydantic version-skew between them would turn
  the *contracts* package into the compatibility hazard it exists to remove. Stdlib has no skew.
- **Alternatives**: pydantic in both (version-pin coupling across runtimes; native venv pin is
  hostage to BentoML's own pydantic requirement); JSON Schema files + validators (a third
  artifact class to keep in sync; heavier than the problem).

## R3. Agent port strategy during migration: `:8100`, per-engine URL flips

- **Decision**: The agent binds one new stable port `:8100` (`AGENT_BIND`, default `0.0.0.0`
  like the legacy daemons — the containers reach it via host-gateway). Each fold-in phase
  flips only that engine's gateway env (`SERVING_URL` → `http://…:8100/engines/llm`, etc.).
  At completion a single `AGENT_URL` replaces the six daemon URLs; 8090–8095/8099 are freed and
  the legacy entries deleted from `platformlib.topology`.
- **Rationale**: Coexistence is mandatory (strangler migration): the agent cannot take :8090
  while the llama supervisor still owns it. Per-engine flips keep every phase independently
  revertible by env change alone.
- **Alternatives**: reuse :8090 and fold LLM first (couples the first fold-in to a port
  handover, complicating rollback); dynamic agent port (contradicts "single stable endpoint",
  clarify Q1).

## R4. Relational schema bootstrap: idempotent DDL at client init

- **Decision**: `platformlib.store` applies `CREATE TABLE IF NOT EXISTS …` (+ `CREATE UNIQUE
  INDEX IF NOT EXISTS`) at first connection; the same DDL is mirrored in
  `infra/postgres/init.sql` for fresh installs. No migration framework. Schema version recorded
  in a one-row `meta` table; additive changes only within 018.
- **Rationale**: House style (hand-rolled, dependency-light — the 014 validation precedent);
  single-operator, single-instance database; Alembic would be a new dependency and a new
  concept for zero concurrent-migration risk.
- **Alternatives**: Alembic (Principle III weight, overkill); schema only in `init.sql`
  (breaks existing installs whose Postgres volume predates 018 — the bootstrap must run against
  live databases too).

## R5. Scheduler placement: gateway lifespan task

- **Decision**: The policy scheduler is an asyncio background task in the gateway's lifespan,
  ticking per-policy intervals; checks run through the existing monitor/quality code paths.
  Single-replica assumptions (already load-bearing) make in-process state acceptable; parked
  retrains and audit records are durable (journal/store), so a gateway restart resumes cleanly.
- **Rationale**: The gateway is the only always-on, always-reachable component (survives host
  GPU work and agent restarts); the loop's decisions (breach → retrain → suggest) are
  control-plane calls the gateway already owns. "The agent executes, the gateway decides" —
  mirrors today's trainer split (spec Assumptions).
- **Alternatives**: scheduler in the agent (dies with host restarts; agent should stay a
  GPU-executor, not a decider); cron container (new resident service); OS cron (host coupling
  the constitution avoids).

## R6. Agent control-surface auth (clarify-deferred)

- **Decision**: House posture unchanged: the agent binds like the legacy daemons it replaces
  (`AGENT_BIND`, default `0.0.0.0` — the gateway/Prometheus containers must reach :8100 via
  host-gateway; loopback-only broke that, Codex review); state-changing control
  routes (unload/swap, job submit/cancel) honor the existing opt-in shared-secret header
  (`X-Swap-Control` generalizes to `X-Agent-Control`); read routes (health, metrics, engine
  list) stay open like today's probes. The gateway forwards the secret exactly as 017 does.
- **Rationale**: Single-operator, localhost-bound platform (002/005 hardening decisions);
  consolidation doesn't change the threat model — it shrinks the number of listening control
  surfaces from six to one.
- **Alternatives**: mandatory auth on all agent routes (breaks the open-probe pattern
  supervise.py and Prometheus rely on); mTLS (absurd weight for loopback).

## R7. Absent engine binary → `unavailable` engine state (clarify-deferred)

- **Decision**: An adapter whose binary/model prerequisites are missing reports state
  `unavailable` (with reason) on the agent's engine listing and health; requests to it return
  503 with that reason; platform health continues to exclude optional engines (ASR) from
  `all_healthy`. ASR remains opt-in exactly as today (`SUPERVISE_DAEMONS` semantics become an
  agent engine-enable list).
- **Rationale**: Preserves 009's opt-in ASR behavior and the bring-up property that a missing
  whisper build never stalls the platform; makes the condition visible instead of implicit.
- **Alternatives**: fail agent startup on any missing engine (breaks opt-in); silently hide the
  engine (operator can't see why a panel is missing).

## R8. Constitution wording (operator decision — mirrors 017's T342)

- **Decision to confirm with the operator**: a **v1.5.0 description refresh** of Principle II's
  mechanism sentence — from "enforced by a single, race-free GPU lease" *as realized by the
  cross-daemon lockfile* to "…realized as the host agent's in-process single-slot admission
  (the lease)". The **rule text does not change**: at most one GPU tenant at any instant,
  live-VRAM admission, CPU-only exemption, operator-confirmed serving swap, training never
  preempted — all verbatim.
- **Rationale**: v1.4.x's history notes describe file-lease mechanics; after retirement that
  description is stale. 017 set the precedent (T342) that description refreshes are ratified
  explicitly by the operator, not slipped in.
- **Alternatives**: no amendment at all (the constitution's normative text is arguably already
  implementation-neutral; acceptable if the operator prefers — flagged either way in tasks).

## R9. Journal format pre-US4

- **Decision**: Append-only JSONL (`state dir/journal.jsonl`), one line per job state
  transition, replayed at agent start to rebuild in-memory job tables; rotated at size
  threshold with the active tail preserved. The state dir is the same fixed non-`/tmp` location
  chosen for the transitional lease file (FR-166). At US4 the journal writes go to Postgres via
  `platformlib.store` (clarify Q4) and the JSONL path is retired.
- **Rationale**: Durable-enough for restart-survival (SC-109) with zero dependencies; JSONL
  replay is the house pattern (run_flow result files, capture logs).
- **Alternatives**: SQLite on host (a second database class for a two-phase interim; US4
  obsoletes it); in-memory until US4 (fails SC-109 and FR-173).

## R10. What "engine child" means for BentoML services (assumption, confirmed viable)

- **Decision**: Phase-one fold-in wraps the existing BentoML run scripts as agent children
  (adapter = spawn + readiness probe + forward), preserving their HTTP surfaces internally;
  replacing BentoML with plain adapters is explicitly out of 018 (spec Assumptions).
- **Rationale**: Least-churn path honoring "the agent stays torch-free"; the vision service
  already implements lease semantics that simply stop being its job (the adapter strips the
  in-service lease calls in the same phase, per lockfile-interop rules).
- **Alternatives**: rewrite embed/tabular/vision as in-agent code (torch in the agent —
  violates the isolation decision); keep them as peer daemons forever (leaves three lease
  implementations alive — defeats the increment).
