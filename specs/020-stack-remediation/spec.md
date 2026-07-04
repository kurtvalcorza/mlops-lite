# Feature Specification: 020 Stack Remediation — object-store exit, Bento-ectomy, agent-runtime decision

**Feature Branch**: `020-stack-remediation`

**Created**: 2026-07-04

**Status**: Draft — sourced from the 2026-07 tech-stack review (post-018 fold-ins), with the three
changes below approved by the operator. Requirement IDs continue the shared space
(FR-198+, SC-127+, tasks T401+).

**Input**: The tech-stack review asked one question — is each layer the best fit for a
single-machine, hybrid-GPU, local-only, single-operator multimodal MLOps platform? — and returned
keep-verdicts on 9 of 11 layers (topology, gateway, llama.cpp, whisper.cpp, training stack, MLflow,
Postgres, observability, UI). Three items need action:

| # | Finding | Operator decision |
|---|---------|-------------------|
| 1 | The object store's open-source edition is **archived upstream** (repo read-only since early 2026; no security patches, no binaries). The platform is running an unmaintained storage engine, already pinned to a frozen CVE-hotfix digest. | **Exit.** Replace with a maintained S3-compatible store; default candidate Garage (single small binary, Principle III fit; AGPL acceptable for this internal single-operator platform), fallback SeaweedFS (Apache-2.0) if the validation spike finds compatibility gaps. |
| 2 | The BentoML service framework inside the vision/embed/tabular children is redundant since 018: the host agent owns lifecycle, admission, ports, and health; Bento decorates one route per child at the cost of a full framework in the venv. | **Retire** ("Bento-ectomy"). Slim children, same byte-compatible routes; model runtimes unchanged. |
| 3 | The agent's HTTP surface runs on the stdlib server — defensible (zero deps, six review rounds survived) but now fronting SSE streaming and multipart passthrough with no on-hardware evidence either way. | **Measure, then upgrade if warranted.** The upgrade (an ASGI server library inside the existing host process — not a new resident service) is pre-approved; the on-hardware baselines decide, and the measurement is recorded either way. |

Cross-cutting: the GPU-memory budget must remain fully parameterized so the platform moves to a
machine with a different-size GPU (e.g. 12 GB → 16 GB) by configuration alone.

> **Scope note**: 020 changes *which components* provide storage, child-service scaffolding, and the
> agent's HTTP runtime — it adds **no new capability, no new resident service, and no new data
> shape**. Every externally observable behavior (routes, payloads, bucket/prefix layout, error
> vocabulary, metrics names) is preserved. Principle II (one GPU tenant) and Principle III (idle
> footprint) are hard boundaries: the replacement store's idle footprint must not exceed the
> incumbent's, and no change may touch admission semantics.

> **Rollback stance**: each of the three changes is independently reversible until its
> decommission step — the store cutover by a configuration flip while both stores hold the data,
> the child swap per-engine by the previous child process, the runtime by keeping the stdlib
> server path intact until the measurement verdict is recorded.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Platform data lives on a maintained object store (Priority: P1)

The operator runs the platform against a maintained S3-compatible store. All existing data —
datasets, registered model artifacts, experiment/run artifacts, results (drift/quality reports,
predictions, labels, captured inputs, shadow verdicts) — is migrated losslessly with the same
bucket and prefix layout. After a verified cutover, the unmaintained store is removed from the
stack entirely. Until the operator confirms decommission, flipping back is a configuration change.

**Why this priority**: this is the only *security-posture* item — an archived upstream means no
patches will ever come, and object storage sits under the registry, datasets, monitoring, and the
promotion loop. Everything else in 020 is simplification; this one is risk retirement.

**Independent Test**: stand the replacement store up beside the incumbent; run the candidate
validation spike (US1a below); migrate; run the full offline suite and the golden live flows
against the replacement; verify per-bucket object-count and byte parity; flip back and confirm the
platform still runs on the incumbent (rollback proof); flip forward and decommission.

**Acceptance Scenarios**:

1. **Given** both stores are up and migration has run, **When** the operator compares the
   migration report, **Then** every bucket shows equal object counts and byte totals, and a
   repeated migration run reports zero new objects copied (idempotent).
2. **Given** the platform is cut over to the replacement store, **When** the full offline suite
   and the golden flows (dataset register → fine-tune → gate → promote → infer → drift/quality
   check → policy tick) run, **Then** all pass with no endpoint behavior change.
3. **Given** the cutover is live but not yet decommissioned, **When** the operator flips the
   configuration back to the incumbent, **Then** the platform serves from the incumbent again with
   no code change (rollback window intact).
4. **Given** the operator confirms decommission, **When** the incumbent store is removed, **Then**
   no service, image reference, bootstrap step, document, or hardcoded source-default endpoint still
   points at it (verified by `docker compose config` **and** a source-tree grep), and the running
   stack contains zero unmaintained components.
5. **Given** the candidate validation spike fails on a compatibility gap (registry artifact
   round-trip or any storage-client path), **When** the operator switches to the fallback
   candidate, **Then** the same spike, migration, and cutover steps apply unchanged (the plan is
   candidate-agnostic).

---

### User Story 2 — Vision/embed/tabular children run without the retired framework (Priority: P2)

The vision, embeddings, and tabular engines keep serving exactly the same requests and responses,
but their child processes are slim single-route services with the serving framework removed from
the host environment. Nothing above the agent's adapter boundary changes: same routes, same
payloads, same readiness probe, same dynamic-port spawn, same error vocabulary.

**Why this priority**: pure simplification with real payoff — one whole framework (plus its
dependency tree) leaves the host venv, shrinking install weight, upgrade surface, and the number
of things a future review must reason about. It carries no new risk if byte-compatibility is
pinned before the swap.

**Independent Test**: capture golden request/response pairs per child (classify, embed, tabular
predict, plus the readiness probe) against the current children; swap one child at a time; replay
the goldens and diff byte-for-byte at the agent boundary; confirm the framework is absent from the
host environment when all three are swapped.

**Acceptance Scenarios**:

1. **Given** a golden request set captured pre-swap, **When** the same requests run against a
   swapped child, **Then** responses are byte-identical at the agent boundary (status, body,
   content type).
2. **Given** the agent's adapter contract (spawn → dynamic port → readiness probe → forward),
   **When** a swapped child starts, **Then** the adapter needs no changes beyond the child's
   launch command — lifecycle, admission, idle-reap, and health behave identically.
3. **Given** all three children are swapped, **When** the host environment is inspected, **Then**
   the retired framework and its exclusive transitive dependencies are gone, and the model
   runtimes (vision/CNN, sentence-embedding, gradient-boosted trees) are unchanged.
4. **Given** a swapped child fails to become ready, **When** the agent reports it, **Then** the
   `unavailable`/`wedged`/`cold` state machine and the 5xx error vocabulary are indistinguishable
   from the pre-swap behavior.

---

### User Story 3 — The agent's HTTP runtime choice is evidence-based (Priority: P2)

The operator gets on-hardware measurements of the agent's HTTP surface under its real duties —
token streaming for LLM inference, multipart audio/image forwards, health/metrics polling during a
load, concurrent requests during a swap — compared against the runbook baselines. If any baseline
misses, the pre-approved upgrade (an ASGI server as a library inside the same host process) is
applied with routes, contract, bind posture, and control-secret behavior unchanged. Either way the
measurement and the verdict are recorded in the on-hardware validation runbook.

**Why this priority**: streaming robustness is user-visible (LLM tokens), but there is no evidence
today that the stdlib server actually falls short — upgrading blind would churn a six-times-reviewed
surface without cause, and not measuring would leave a known unknown at the platform's front door.

**Independent Test**: run the runtime drill from the quickstart on the GPU box: measure stream
time-to-first-token and inter-token stall count under a concurrent health-poll load, multipart
round-trip latency, and error behavior when a client disconnects mid-stream; compare to baselines;
record verdict; if upgrading, re-run the same drill and the agent contract tests.

**Acceptance Scenarios**:

1. **Given** the runtime drill has run on hardware, **When** the operator reads the runbook,
   **Then** it contains the measured values, the baselines, and an explicit keep/upgrade verdict.
2. **Given** the verdict is "upgrade", **When** the ASGI runtime lands, **Then** every agent
   contract test and byte-compat route test passes unchanged, no new resident process exists, and
   the drill re-run meets all baselines.
3. **Given** the verdict is "keep", **When** 020 closes, **Then** the stdlib server remains with
   the measurement on record — the decision is documented, not deferred.
4. **Given** a client disconnects mid-stream, **When** the agent handles it (either runtime),
   **Then** the engine child is unaffected and the next request succeeds (no wedged worker).

---

### User Story 4 — The GPU budget moves with the hardware (Priority: P3)

The operator brings the platform up on a machine with a different GPU memory size (e.g. 16 GB
instead of 12 GB) by changing documented configuration only. No code contains a hardcoded memory
budget; live GPU readings remain the primary admission input, with the configured budget as the
fallback.

**Why this priority**: cheap insurance already mostly true by construction — this story pins it
with a check and a bring-up document so it stays true.

**Independent Test**: audit for hardcoded budget values outside configuration defaults; bring the
stack up with a different budget value and verify admission honors it (static-fallback refusal
threshold moves accordingly); confirm the bring-up doc lists every knob a new machine needs.

**Acceptance Scenarios**:

1. **Given** a repo-wide audit, **When** searching for memory-budget literals, **Then** the only
   ones that exist are `VRAM_GB` env-var fallbacks (today the string `"12"` appears as the default
   in `hostagent/main.py`, `hostagent/jobs.py`, and the three adapters) — no budget literal exists
   that is *not* a fallback for the `VRAM_GB` env, and the audit task consolidates the duplicated
   fallbacks to a single resolver so a machine with a different budget cannot be left partially on
   the old default.
2. **Given** the budget knob set to a new value with the GPU unreadable (static fallback),
   **When** a load whose estimate exceeds the new budget is requested, **Then** it is refused at
   the new threshold — and admitted when the estimate fits it.
3. **Given** the bring-up document, **When** a new machine is provisioned by following only it,
   **Then** the platform starts with correct budget, native binaries, and secrets.

---

### Edge Cases

- **Migration interrupted mid-copy**: both stores stay live during migration; the copy is
  idempotent and resumable; cutover happens only after the parity report is clean — a partial copy
  can never be cut over to silently.
- **The endpoint seam is two variables, not one**: both storage clients resolve
  `S3_ENDPOINT_URL` before `MLFLOW_S3_ENDPOINT_URL` (then a hardcoded `minio:9000` fallback), and
  the host port changes `9000 → 3900`. A cutover that flips only `MLFLOW_S3_ENDPOINT_URL` while
  `S3_ENDPOINT_URL` is set — or that lets a host consumer fall back to its baked `:9000` default —
  silently keeps serving from (or breaks against) the retired store. The cutover step asserts
  `S3_ENDPOINT_URL` is unset (or flips it too) and verifies the client's *resolved* endpoint moved,
  not merely that flows pass (contracts/store-migration.md §cutover).
- **Compatibility gap found late** (after migration, before decommission): rollback is a
  configuration flip; the incumbent still holds all data written before cutover. Writes made after
  cutover are re-mirrored back before flipping (the migration tool runs in either direction).
- **Disk headroom**: two stores co-reside during the migration window; the plan must check the
  drive budget (Principle III's constrained-drive stance) before starting and clean up promptly
  after decommission.
- **Captured-input TTL/retention**: retention is enforced by the application (sampled + capped +
  TTL), not by store lifecycle rules — so it transfers to any S3-compatible store unchanged; the
  spike must still confirm list/delete pagination behaves identically.
- **A swapped child's port/probe drift**: the adapter contract pins dynamic-port spawn + readiness
  probe; the golden set includes the probe so a drifted child fails the swap gate, not production.
- **Streaming under swap pressure**: the runtime drill must include a stream in flight while a
  swap/preempt request arrives — the 409-vs-drain behavior must match the documented lease
  semantics on both runtimes.
- **Concurrent migration and live writes**: prediction/label/capture writes continue during
  migration; the final pre-cutover pass re-mirrors the delta so late writes are not stranded.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-198**: The platform MUST run every object-storage-backed capability (datasets, registry
  artifacts, experiment artifacts, results incl. predictions/labels/captured inputs/shadow
  verdicts, policy store until its relational cutover) against a **maintained** S3-compatible
  store, preserving the existing bucket and prefix layout exactly.
- **FR-199**: Migration MUST be verifiably lossless and idempotent: a per-bucket report of object
  counts and byte totals on both sides, equality required for cutover; a re-run copies nothing new;
  the tool runs in both directions (rollback re-mirroring).
- **FR-200**: Cutover MUST be reversible by configuration alone until the operator explicitly
  confirms decommission; both stores retain data through the rollback window.
- **FR-201**: After confirmed decommission, the retired store MUST be fully removed — service
  definition, pinned image digest, bootstrap/bucket-creation references, secrets wiring,
  documentation, **and the hardcoded endpoint fallbacks in source** (the `minio`/`:9000` defaults
  in the storage clients and host run/flow scripts, repointed to the replacement or dropped so a
  missing env fails loud); the running stack contains zero unmaintained components and a source-tree
  grep for the retired store's name/port returns zero live references.
- **FR-202**: A candidate **validation spike** MUST gate migration: registry artifact round-trip,
  every storage-client code path (upload, download, list with pagination, delete, prefix listing,
  conditional/duplicate-key behavior relied on by write-once semantics), and the offline suite
  against the candidate. A failed spike switches to the fallback candidate with the same plan.
- **FR-203**: The vision, embeddings, and tabular children MUST serve byte-compatible routes
  (paths, payloads, status codes, content types, readiness probe) without the retired serving
  framework; the agent adapter boundary is unchanged except the child launch command.
- **FR-204**: The retired serving framework and its exclusive transitive dependencies MUST be
  removed from the host environment; the model runtimes themselves are unchanged.
- **FR-205**: The agent HTTP runtime decision MUST be evidence-based: an on-hardware drill
  measuring stream time-to-first-token, inter-token stalls under concurrent polling, multipart
  round-trip, mid-stream disconnect handling, and behavior during swap contention — compared
  against runbook baselines, with the measurements and keep/upgrade verdict recorded.
- **FR-206**: If the upgrade verdict fires, the replacement runtime MUST run as a library inside
  the existing host agent process (no new resident service), preserving routes, the agent API
  contract, bind posture, control-secret enforcement, and the error vocabulary; the drill re-runs
  clean after the swap.
- **FR-207**: The GPU memory budget MUST remain fully parameterized: the only budget literals in
  the tree are `VRAM_GB` env-var fallbacks, and those MUST resolve through a single resolver (not
  independently duplicated defaults that could drift) so a machine set to a different budget cannot
  be left partially on the old value; live GPU readings stay the primary admission input; the
  bring-up documentation lists every knob required for a machine with different GPU memory.

### Key Entities

- **Object store**: the S3-compatible service holding the four platform buckets
  (datasets / models / results / experiment artifacts); exactly one is authoritative at any time.
- **Migration report**: the per-bucket parity evidence (object counts, byte totals, both sides,
  timestamp, direction) that gates cutover and decommission.
- **Runtime baseline record**: the runbook entry holding the drill's measured values, the
  baselines, and the keep/upgrade verdict for the agent's HTTP runtime.
- **Golden request set**: the captured per-child request/response pairs (including readiness
  probes) that gate each child swap.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-127**: 100% of objects migrate with parity evidence — per-bucket counts and byte totals
  equal on both sides; a migration re-run copies zero objects.
- **SC-128**: The full offline suite and the golden live flows pass unchanged against the
  replacement store — zero endpoint behavior changes, zero test edits attributable to the store.
- **SC-129**: Unmaintained components in the running stack drop from one to **zero** after
  decommission — enforced across code, not just compose: both `docker compose config` and a
  source-tree grep for the retired store's name/port return zero live references.
- **SC-130**: Idle memory footprint of the storage service does not increase versus the incumbent
  (Principle III), measured at rest on the target machine.
- **SC-131**: The three child swaps remove the serving framework from the host environment
  entirely, with golden request/response pairs byte-identical per child; host-venv package count
  strictly decreases.
- **SC-132**: The agent runtime verdict is recorded with measurements; streaming meets the runbook
  baseline (time-to-first-token and stall count) under concurrent polling on whichever runtime the
  verdict selects; a mid-stream client disconnect never wedges the next request.
- **SC-133**: A machine with different GPU memory is brought up with configuration changes only —
  zero code edits — and static-fallback admission refuses/admits at the new threshold.

## Assumptions

- Single-operator, local-only platform: AGPL licensing on the default store candidate is
  acceptable (nothing is redistributed); the fallback candidate is permissively licensed if that
  posture ever changes.
- The registry server fronts all artifact access (serve-artifacts proxy), so no client depends on
  store-issued presigned URLs — candidate compatibility is exercised through the storage clients
  actually in use, not the full S3 surface.
- Both stores fit the constrained drive simultaneously for the migration window; the plan verifies
  headroom before starting.
- Retention/TTL for captured inputs is application-enforced (sampled + capped + TTL), so no
  store-side lifecycle rules need porting.
- The on-hardware drill rides the same session as the pending 018 hardware sweep (the runbook
  document already planned there), so 020 adds drill content, not a new hardware trip.
- The relational cutover of high-churn state (US4 of 018, tasks T373–T377) proceeds independently;
  020 neither depends on it nor blocks it — whichever lands second simply has fewer prefixes to
  care about.
