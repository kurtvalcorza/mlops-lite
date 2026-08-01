# Phase 0 Research: 027 Unified ML Lifecycle Console

**Input**: [spec.md](./spec.md) · **Output**: decisions consumed by [plan.md](./plan.md)

This increment is unusual for a console feature: the interface is the easy half. The hard half is
that **most of what the spec asks the console to display is not currently readable over any API**.
Phase 0 therefore starts with a surface audit, then resolves the design questions that audit raises.

---

## R0 — Backend surface audit (what exists today)

Enumerated from the source tree, not from documentation.

**Gateway** — 52 routes (`gateway/app/routers/*.py`, `gateway/app/main.py`).
**Host agent** — `GET /healthz|/readyz|/health|/metrics|/engines|/engines/{id}/{health,readyz,healthz}|/jobs|/jobs/{id}`,
`POST /control/unload|/control/reload|/jobs|/jobs/{id}/cancel`, plus byte-compatible per-engine verb
paths.
**BFF allowlist** — 60 entries (`ui/lib/gw-allowlist.ts`), the console's complete proxy surface.

Mapping the spec's requirements against that surface:

| Spec need | Exists today? | Verdict |
|---|---|---|
| Aggregate health (FR-369) | `GET /platform/health` | **Reuse** |
| Live loop/badge events (FR-372) | `GET /platform/events` (SSE) | **Reuse, extend payload** |
| Models, versions, aliases (FR-383/384) | `GET /models`, `GET /models/{name}` | **Reuse, enrich** |
| Datasets + byte download (FR-419/421) | `GET /datasets…`, `…/download` (025 US3) | **Reuse** |
| Drift + quality history (FR-404) | `GET /monitor`, `GET /monitor/quality` | **Reuse** |
| Policies, suggestions (US12) | full CRUD present | **Reuse** |
| Run detail + SSE (FR-391/395) | `GET /runs/{id}`, `/runs/{id}/events` | **Reuse** |
| Serving desired-vs-resident (FR-416) | `GET /serving/llm/activation` (023 US5) | **Reuse** |
| **Per-device GPU topology** (FR-375) | **No.** `AgentHealth` carries only `gpu_free_gb`, `holder`, `holder_kind` | **NET-NEW** |
| **Engine process detail** (FR-376) | Partial. `EngineState` = `engine_id/state/gpu/optional/reason` — no pid, VRAM, model identity, request count | **NET-NEW** |
| **Admission decisions** (FR-377/378) | **No.** `AdmissionResult` is computed and discarded; nothing retains it | **NET-NEW** |
| **Journal reads** (FR-380) | **No HTTP surface.** `journal.jobs()/get()` are in-process only | **NET-NEW** |
| **Trace reads** (FR-412) | **No.** `tracing.py` only *writes* | **NET-NEW** |
| **Prediction reads** (FR-407/408) | **No.** Rows exist in Postgres; only aggregates are exposed | **NET-NEW** |
| **Experiment/run listing** (FR-391) | **No list route** — only `GET /runs/{id}` | **NET-NEW** |
| **Endpoint records** (FR-414) | **No entity.** Closest are `serving/state`, `serving/tasks`, activation | **NET-NEW (synthesized)** |
| **Capture/label listing** (FR-410/411) | Write-only (`POST /monitor/labels`) | **NET-NEW** |
| Global search (FR-368) | **No.** | **NET-NEW (composed)** |

**Finding**: ten net-new read surfaces. This is the increment's true weight — the ten-area
navigation is comparatively cheap. It also validates the "full stack" scope choice: under a UI-only
constraint, Runtime, Inference, and Traces would have rendered essentially empty.

---

## R1 — Admission is *refuse*, not *queue*

**Decision**: name the **serving admission** surface *Admission* and present it as a **decision
record**, never a queue — but show the **jobs lane as the real FIFO queue it is**. The original
blanket "ship nothing called a queue" was too broad; corrected below.

**Rationale**: the source addendum's navigation says "Admission Queue" and its entity sketch has an
`age` field, both of which imply pending *serving* requests that will later be granted. That is not
what serving admission does. It evaluates the bounds and returns a decision — admitted, or refused —
and a refused caller is done.

**Corrected against 026's merged coordinator contract.** This finding originally rested on today's
single-slot `hostagent/admission.py`: "decides in one critical section, nothing waits, a refused
caller gets a `409`." Feature 026 replaces that module, and all three of those premises changed:

| Premise (v1) | 026's merged contract |
|---|---|
| "nothing waits" | Serving admission has a **bounded** retry loop — `max_admission_attempts` with jittered backoff, plus `AwaitLoad` (joining an in-flight load) and `AwaitTransient` (waiting out an owning operation). Callers *do* wait, bounded by their own deadline. |
| "refused gets a `409`" | Refusals are **`503 gpu_busy` + `Retry-After`** (transient) or **`413 model_too_large`** (permanent). `409` is now reserved for the host agent's jobs-lane-full contract. |
| "never queues" | True of **serving**. False of **jobs**: 026 ships a persisted FIFO `jobs_lane` — `POST /jobs` returns `202 {state:"queued", queue_pos:2}`, and `/admin/queue` exposes `jobs_lane` with positions. |

**What survives, and what changes.** The core insight holds and is *strengthened*: a refused serving
request will never "come up", so presenting it in a backlog would teach the operator to wait for
something that will not happen. But bounded waiting is real, and the jobs lane is a genuine queue with
positions an operator needs — hiding it to preserve a slogan would be its own falsehood, the mirror of
the one this finding exists to prevent. So:

- **Serving** → decision record. No `pending`, no queue position, no age-until-granted. A bounded
  in-flight retry is reported as `attempt` on the record, not as a queue place.
- **Jobs** → a real queue view, with `queue_pos` read from the persisted lane. Naming it a queue is
  accurate here; refusing to would be the fake semantics, inverted.

Shipping a "queue" would therefore be exactly the fake-orchestration-semantics failure the addendum
itself warns against in §23.4 (Prefect) and §23.5 (Alertmanager) — the same error, applied to
admission. A queue view would also be actively harmful: an operator would wait for a refused request
to "come up", and it never will.

**Consequence**: `AdmissionRecord` is a **historical** record with a `decided_at`, not a pending item
with an `age`. The spec's FR-377 wording ("admission requests and decisions … and age") is satisfied
by age-since-decision. The Overview's "Pending Admissions" card (FR-371) is re-read as **recent
refusals** — a pressure signal, not a backlog.

**Alternatives rejected**: (a) building a real *serving* admission queue — a behavioural change to the
platform's non-negotiable Principle II mechanism, far outside a console increment; (b) rendering the
nav item as "Queue" with an explanatory tooltip — the label is what operators read; (c) keeping the
blanket "no queue anywhere" rule — it would suppress the jobs lane, which genuinely queues.

**Sequencing.** These surfaces read the coordinator that 026 specifies, not today's `admission.py` —
admission records do not exist today at all (`AdmissionResult` is computed and discarded, R0), so this
surface is net-new either way and should be built against the design that will be there. If 027 ships
before 026's Phase 2 lands, the coordinator-specific fields (`residents[].state`, `activeRequests`,
`reservedGb`, `unmaterializedGb`, `attempt`) resolve to `unknown` under the existing degradation rules
rather than being faked — FR-430 already requires exactly that when a source cannot answer.

---

## R2 — Per-device GPU topology

**Decision**: extend the agent's existing cached NVML reader from a single free-VRAM scalar to a
per-device snapshot, and expose it at a new agent route `GET /runtime/devices`, proxied by the
gateway.

**Rationale**: `hostagent/admission.py::GpuReader` already owns the NVML-preferred /
`nvidia-smi`-fallback / static-budget cascade behind a 1-second TTL cache, precisely so that
admission never forks per call. Per-device data must ride that same cached path — a second,
uncached NVML consumer polled by a browser at 2–5 s would reintroduce the per-poll subprocess forks
that 018 deliberately removed.

**Design points**:
- The snapshot is **read-only and side-effect free**; it never touches the admission lock's claim
  path. It may share the lock only for a consistent read.
- Each device carries `source ∈ {nvml, smi, static}`. FR-381's fallback labelling is a **data field**,
  not a UI guess.
- The reference host has one device. The payload is a **list** from day one so multi-device (FR-382)
  needs no contract change.

**Alternatives rejected**: (a) scraping Prometheus for device state — the agent's own `/metrics` is
fixed-cardinality by design (023 US7) and deliberately does not carry UUIDs or compute capability;
(b) a browser-side `nvidia-smi` shell-out — absurd, listed only because the addendum's "temperature,
when available" hints at data the current metrics do not expose.

---

## R3 — Engine process enrichment

**Decision**: extend `EngineState` with optional fields (`pid`, `vram_gb`, `model_identity`,
`registry_version`, `started_at`, `active_requests`, `device_index`) rather than adding a parallel
contract.

**Rationale**: `platformlib/contracts.py` is the shared, validated payload shape both runtimes
exchange, and 019 explicitly hardened `/health` + `/engines` conformance to it. Adding a second
engine-shaped contract would fork that. All new fields are `Optional` with defaults, so existing
validators and any older consumer keep passing — the 018/019 conformance tests stay green.

**Honesty constraint**: `model_identity` must come from the agent's *actually-loaded* identity (the
022 honest-served-identity work), never from the registry's desired pointer. Conflating them would
manufacture exactly the class of lie FR-427 exists to prevent.

---

## R4 — Journal reads

**Decision**: new agent route `GET /journal` — paged, filterable by job/engine/event-type/time,
newest-first, with a hard server-side page cap. Never an unbounded dump.

**Rationale**: the journal is an append-only fsync'd JSONL log (018 US4, hardened by 019 for
torn-tail durability) that grows without bound across a machine's life. `journal.jobs()` hydrates
in-process; exposing it wholesale would let a console page pull an arbitrarily large body through
the agent's bounded transport, whose 1 MiB JSON cap (023 US6) would then simply fail the request.
Paging is therefore not a nicety — it is required for the endpoint to work at all.

**Consequence**: pagination is opaque-cursor (sequence-based), because the log is append-only and
sequence is already the natural key.

---

## R5 — Trust boundary: the console never talks to the agent

**Decision**: **all** runtime data reaches the console via the gateway. The browser talks to the
BFF; the BFF talks to the gateway; the gateway talks to the agent with `X-Agent-Key`. No new
browser-reachable host.

**Rationale**: 023 US2 made the agent fail-closed behind an internal credential whose entire premise
is that only the gateway holds it, and 005 US1 binds every port to loopback. Letting the console
reach `:8100` would either leak that credential toward the browser or require a second key-injecting
proxy — two trust boundaries where the platform deliberately has one. It would also break the
containerized case, where `AGENT_URL` is a WSL IP the browser cannot resolve.

**Cost, accepted**: every runtime read takes one extra hop, and the gateway grows a thin
runtime-proxy module. That is the correct trade for not multiplying trust boundaries.

---

## R6 — Traces: read path

**Decision**: read traces through the `mlflow-skinny` client already pinned in the gateway image,
behind new gateway routes `GET /traces` and `GET /traces/{trace_id}`; normalize to a generic span
tree in the gateway.

**Rationale**: `mlflow-skinny==3.14.0` is already a gateway dependency (006/007), so this adds no
package. Normalizing server-side keeps FR-413 (generic, non-token-oriented presentation) enforceable
in one place rather than in every renderer, and keeps the client free of tracking-vendor payload
shapes (Principle V).

**Alternatives rejected**: proxying raw tracking-server responses to the browser — couples the
console to a vendor payload shape and pushes normalization into React.

---

## R7 — Endpoints are a synthesized read model, not a new table

**Decision**: derive the endpoint list in the gateway from data that already exists — the registry's
tasks/aliases, the active-serving pointer, the activation state machine, and the agent's engine
states. **No new persisted entity, no migration, in 027.**

**Rationale**: FR-438 and the repo's own FR-359 discipline say a schema change lands as a new
numbered migration; the cheapest correct move is to need none. Everything the endpoint list
displays (FR-414) is already derivable: modality and assigned version from the registry and pointer,
runtime and host from the agent, traffic and error rate from Prometheus. Persisting an `endpoints`
table would duplicate the serving pointer and create a second thing that can disagree with it —
manufacturing the conflict class FR-427 exists to *report*.

**Consequence carried into planning**: 027 is expected to need **zero migrations**. If one proves
genuinely necessary it lands as a new numbered file and is called out explicitly, exactly as 025's
T595 did.

---

## R8 — Where the multi-source joins happen

**Decision**: joins happen in the **gateway**; the BFF stays a security boundary plus shallow
composition.

**Rationale**: a join across gateway-Postgres, the agent, the registry, and the object store needs
credentials for all four. The BFF holds only the gateway key by design (004 US1), and giving it the
others would undo R5. The gateway already holds every credential and already talks to every
backend, so it is the only place the join is cheap and safe.

**BFF keeps**: allowlist enforcement, key injection, same-origin/CSRF guard, timeouts, correlation
ids, sensitive-field filtering, artifact byte proxying, and capability→feature-flag translation
(FR-432/433).

**Alternatives rejected**: (a) joining in the browser — needs N round trips per row and would push
credentials outward; (b) a new aggregation service — a new resident process, straight into
Principle III.

---

## R9 — Conflict detection is computed, not stored

**Decision**: `StateConflict` is produced at join time by comparing the sources already fetched, and
returned inline on the joined entity. Nothing is persisted in 027.

**Rationale**: a conflict is a statement about *this observation*, not a durable fact. Persisting it
would require reconciliation, TTLs, and a migration, and it would go stale the moment either source
moved. Automated reconciliation is explicitly MVP 3 (US12) — 027 only has to *tell the truth about
what it just read*.

**Rule**: comparison is only meaningful when both sources were observed close together; each joined
entity therefore carries the observation time of each side, and skew beyond a threshold is disclosed
rather than silently compared (the clock-skew edge case).

---

## R10 — Live updates: SSE where it exists, disciplined polling elsewhere

**Decision**: reuse the two existing SSE streams (`/platform/events`, `/runs/{id}/events`) and poll
everything else on the spec's per-resource cadence, gated on tab visibility with exponential backoff.

**Rationale**: the platform has no broker and 027 must not add one (FR-434). SSE already exists for
exactly the two highest-churn surfaces. Adding SSE for every runtime read would multiply long-lived
connections against the agent's **bounded** worker/queue transport (023 US6) — a console with ten
open streams could plausibly saturate it, which is a self-inflicted denial of service on the control
plane.

**Consequence**: `Retry-After`-aware backoff, visibility gating (FR-431), and last-known-good
retention with data age (FR-430) are **infrastructure**, written once in a shared polling hook, not
per-page.

---

## R11 — Charts without a charting dependency

**Decision**: hand-rolled SVG/CSS primitives. **No charting package.** Confirmed against SC-198.

**Rationale**: the console needs six chart forms — sparkline, time series with band, horizontal
threshold bar (drift), span waterfall (traces), parallel coordinates (studies), and matrix heatmap
(confusion). Each is tens of lines of SVG with a linear scale. A general charting library would add
an order of magnitude more code than the specific charts need, and the UI's dependency tree is
currently exactly `next`, `react`, `react-dom` — a deliberate state worth preserving under
Principle III.

**Rejected**: Recharts (pulls d3 subpackages), uPlot (small, but still a dep for six static forms),
D3 proper (a visualization grammar, vastly more than required).

**Accepted cost**: no free interactivity — brushing, zoom, and tooltips are hand-written. Scoped by
building the primitives once in a shared chart module rather than per page.

---

## R12 — Rebuild in place

**Decision**: rebuild `ui/app` in place using a `(console)` route group; delete the 021 stage
directories in the same increment; keep `ui/app/api/gw` and harden it.

**Rationale**: the user's explicit choice was full replacement, and running both IAs behind a flag
would double the surface for the length of the increment while the 021 views atrophy. The BFF, its
allowlist, the security guards, and the three existing UI test suites (`test_ui_smoke.py`,
`test_ui_security.py`, `test_ui_resilience.py`) are **not** 021-specific — they are the 004/005
security work and must survive.

**Consequence**: the 021 paths (`/serving`, `/data`, `/training`, `/models`, `/monitoring`,
`/retraining`, plus the pre-021 `/infer`, `/datasets`, `/runs`, `/monitor` redirects 021 itself
added) become redirects (FR-364). The mapping is a table in the plan, and every entry gets a test.

---

## R13 — Identity

**Decision**: single operator. No user model, no sessions, no roles.

**Rationale**: the platform authenticates with a shared API key (`GATEWAY_API_KEYS`) and has no
concept of a user. Rendering an "Owner" column would require inventing data. Confirmed with the
requester during the spec gate.

**Consequence**: owner/approver fields are **omitted** rather than filled with a placeholder; the
user menu holds settings, mode, and the key's status, not accounts; FR-409's "permission checks" for
payload reveal degrade to an explicit operator action plus the existing capture policy — which is
the real control anyway, since capture is what decides whether a payload exists at all.

---

## R14 — Environment badge

**Decision**: the badge reports **mode** — `offline` (fixtures), `live` (compose stack reachable),
`hardware` (agent reporting a real GPU) — resolved server-side from actual reachability, never from
a hand-set string.

**Rationale**: a development/staging/production ladder is meaningless on a local-first
single-machine platform, and a self-declared label can lie. The repo already has this exact
three-way taxonomy as its pytest marker discipline (`unmarked` / `live` / `hw`), so the badge
inherits a vocabulary the team already uses and CI already enforces.

---

## Open items carried into planning

1. **US2 hardware validation** (SC-201/202) cannot be satisfied in a container. Carried as explicit
   `[HW]` tasks, never silently skipped — matching the constitution's gate-zero discipline.
2. **MVP 1 size.** Ten stories is large for one increment against Principle VII. Mitigation and the
   pre-agreed cut line are recorded in the plan's Complexity Tracking.
3. **Prometheus query shape** for endpoint traffic (FR-414) — whether the gateway proxies range
   queries or precomputes. Resolved in the plan's contracts section; it does not block Phase 1.
