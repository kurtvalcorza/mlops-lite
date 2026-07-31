# Phase 1 Data Model: 027 Unified ML Lifecycle Console

**Input**: [spec.md](./spec.md) Key Entities · [research.md](./research.md) · **Consumed by**:
[contracts/](./contracts/), tasks.md

Nothing here is persisted. Every entity below is a **projection** assembled at read time from
existing systems of record (research R7/R9). The interesting content is not the field lists — it is
the **projection rules**: which source owns each field, and where disagreement is a *conflict* to
report rather than a *precedence* to apply.

---

## 1. Ownership matrix

The single most important table in this increment. A field must be projected from its owner; reading
it from a convenient secondary source is how a console starts lying.

| Field group | Owner (authoritative) | Enrichment (never overrides) |
|---|---|---|
| Experiments, runs, params, metrics | tracking | gateway job metadata |
| Logged / registered models, versions, aliases | tracking (registry) | artifact metadata, gateway policies |
| Traces | tracking | gateway prediction records |
| Predictions, labels, captures | gateway Postgres | traces |
| Evaluation results, gate verdicts | gateway (+ object store) | tracking metrics |
| Jobs (submission, policy, lifecycle intent) | gateway | agent execution record |
| Job execution (process, device, transitions) | **host agent** | gateway job row |
| GPU devices, VRAM, utilization | **host agent** (NVML) | metrics |
| Engine residency and **served identity** | **host agent** | registry desired pointer |
| Admission decisions | **host agent** | — |
| Drift, quality windows | gateway | metrics |
| Policies, suggestions | gateway Postgres | registry tags |
| Artifacts, datasets, digests | object store (via gateway) | tracking references |
| Time series, alert-rule state | metrics | — |

**Rule of thumb encoded in code review**: if a field's value is available from two systems, the
projection must name which one it took and record the other for conflict detection — never silently
prefer one.

---

## 2. PlatformHealth

```typescript
type HealthState = "healthy" | "degraded" | "critical" | "unknown";

interface ServiceHealth {
  service: "gateway" | "tracking" | "database" | "objectstore" | "agent" | "metrics" | "gpu";
  state: HealthState;
  required: boolean;        // false => impairment degrades, never criticals (FR-370)
  detail?: string;
  observedAt: string;
}

interface PlatformHealth {
  overall: HealthState;
  services: ServiceHealth[];
  mode: "offline" | "live" | "hardware";   // resolved from reachability, never declared (R14)
  observedAt: string;
}
```

**Aggregation rule (FR-369/370)**: `critical` iff any **required** service is down such that training
or inference cannot operate safely — concretely: gateway or database unreachable. Agent loss is
`degraded`, not `critical`: CPU modalities (embeddings, tabular) still serve, and asserting
otherwise would overstate the outage. `unknown` when the observation itself is stale beyond
threshold — distinct from `critical`, because "I cannot see" is not "it is broken".

---

## 3. PlatformJob and the state normalization

```typescript
type JobState =
  | "Draft" | "Queued" | "AdmissionCheck" | "Admitted" | "Starting" | "Running"
  | "Completing" | "Succeeded" | "Failed" | "Cancelled" | "Rejected"
  | "Orphaned" | "Unknown";

interface PlatformJob {
  id: string;
  type: "training" | "hpo" | "batch" | "shadow" | "evaluation" | "inference";
  normalizedState: JobState;
  gatewayState?: string;    // native, preserved (FR-392)
  agentState?: string;      // native, preserved
  trackingRunState?: string;
  runId?: string;
  studyId?: string;
  modelId?: string;
  assignedHost?: string;
  assignedDevice?: number;
  admissionReason?: string; // populated when Rejected (FR-378)
  createdAt: string; startedAt?: string; completedAt?: string;
  observed: { gatewayAt?: string; agentAt?: string; trackingAt?: string };
  conflict?: StateConflict;
}
```

### Normalization table (FR-392 / SC-190)

Every row maps to exactly one normalized state, and each source's native string is preserved
alongside it — SC-190 requires both halves: total mapping coverage *and* the native state remaining
inspectable.

| Gateway | Agent | Tracking run | → normalized |
|---|---|---|---|
| accepted, not dispatched | — | — | `Queued` |
| dispatched | admission pending | — | `AdmissionCheck` |
| dispatched | refused | — | `Rejected` (+ `admissionReason`) |
| dispatched | admitted, no child | — | `Admitted` |
| dispatched | child spawned, not ready | — | `Starting` |
| running | running | RUNNING | `Running` |
| running | finished, artifacts pending | RUNNING | `Completing` |
| done | done | FINISHED | `Succeeded` |
| failed / done | failed | FAILED | `Failed` |
| cancelled | cancelled | KILLED | `Cancelled` |
| running | **no record** | RUNNING | `Orphaned` + **conflict** |
| any | unreachable | any | keep last known, mark `Unknown`, **never** infer stopped (FR-428) |

`Orphaned` is the one state the console **derives** rather than reads. It is precisely the
gateway-says-running/agent-has-no-process case from the spec (US10 scenario 1) and must always carry
a `StateConflict`, never stand alone.

---

## 4. StateConflict

```typescript
interface StateConflict {
  entity: "job" | "model" | "endpoint" | "engine";
  entityId: string;
  sources: { source: string; state: string; observedAt: string }[];
  lastConsistentAt?: string;
  skewExceeded?: boolean;   // observations too far apart to compare (clock-skew edge case)
  suggestedAction: "refresh" | "inspect-journal" | "reconcile";
}
```

**Detection rule**: only compare observations taken within the skew threshold. Beyond it, emit
`skewExceeded: true` and **suppress the conflict claim** — a stale read disagreeing with a fresh one
is not evidence of inconsistency, and reporting it as such would train operators to ignore the
banner. `reconcile` is surfaced but inert in 027 (MVP 3 owns automated reconciliation).

---

## 5. PlatformModel and compatibility

```typescript
interface PlatformModel {
  id: string; name: string;
  // FR-385: all five served modalities, plus `unknown` so an unrecognized or missing
  // task tag renders as unknown rather than being filtered out of the catalog.
  modality: "text-generation" | "image-classification" | "embedding" | "asr" | "tabular" | "unknown";
  loggedModelId?: string; registeredModelName?: string; version?: string;
  aliases: string[]; sourceRunId?: string;
  artifactUri: string; artifactDigest?: string; artifactSizeBytes?: number;
  artifactPresent: boolean;                     // object-store existence, not assumed (FR-384)
  // FR-402: a version with no logged metric that is not the serving version is
  // `not-evaluated` — the platform's documented refusal to score it, never an error.
  evaluationState: "passed" | "failed" | "warning" | "not-evaluated" | "incomplete";
  deploymentIds: string[];
  lineage?: { baseModel?: string; baseResolvable: boolean; parentRunId?: string };
  compatibility?: RuntimeCompatibility;
}

interface RuntimeCompatibility {
  requiredEngine: string;
  acceleratorRequired: boolean;
  requiredComputeCapability?: string;
  artifactAvailable: boolean;
  hostCompatible: boolean;

  estimatedVramGb?: number;
  usableBudgetGb?: number;       // VRAM_GB less the safety reserve
  accountedResidentGb?: number;  // current accounted resident set
  liveFreeVramGb?: number;       // measured; already excludes residents
  headroomGb?: number;

  budgetCheck: "pass" | "fail" | "unknown";    // est + accounted <= usable budget
  liveVramCheck: "pass" | "fail" | "unknown";  // est + headroom <= live free
  fitsAlone: boolean;                          // est <= usable budget on an empty GPU
  jobExclusive: boolean;                       // a training/HPO/batch job holds the whole GPU

  verdict: "eligible" | "not-currently-eligible" | "incompatible" | "unknown";
  reasons: string[];
}
```

**Verdict rule (FR-388)** — the distinction that matters, expressed against the constitution's two
checks:

- `incompatible` — **structural**: engine unavailable, compute capability mismatch, artifact missing,
  adapter base unresolvable, **or `fitsAlone === false`** (the model exceeds the usable budget even
  on an empty GPU, so neither eviction nor waiting can help). Waiting will not help.
- `not-currently-eligible` — **transient**: `budgetCheck` or `liveVramCheck` fails while
  `fitsAlone` is true, or `jobExclusive` is true. Eviction, idle-release, or job completion will
  help.
- `unknown` — the agent is unreachable, or either check is `unknown`. **Never** collapse this into
  either of the above; an unreachable agent is not a compatibility fact.

**Both checks are reported separately**, never merged into one number. Telling an operator "not
enough VRAM" when the real constraint is the accounted budget — or the reverse — sends them to the
wrong remedy: eviction fixes a budget failure, whereas a live-VRAM failure with headroom exhausted
usually means a leaked or unaccounted allocation.

`estimatedVramGb` comes from the platform's existing `*_EST_GB` configuration, not a guess.
`baseResolvable: false` forces `incompatible`, mirroring the platform's actual refusal to promote an
adapter whose base cannot be resolved (FR-389).

---

## 6. Runtime entities

```typescript
interface RuntimeDevice {
  index: number; name: string; uuid?: string; computeCapability?: string;
  totalVramGb?: number; freeVramGb?: number; usedVramGb?: number;
  utilizationPct?: number; temperatureC?: number;
  processes: { pid: number; vramGb?: number; engineId?: string }[];
  source: "nvml" | "smi" | "static";   // FR-381 — provenance is DATA, not a UI guess
  observedAt: string;
}

interface EngineProcess {
  engineId: string; modality: string;
  state: "cold" | "loading" | "ready" | "draining" | "unavailable" | "wedged";
  gpu: boolean; optional: boolean; reason?: string;
  pid?: number; deviceIndex?: number; vramGb?: number;
  modelIdentity?: string;      // AGENT-REPORTED loaded identity (022) — never the desired pointer
  registryVersion?: string;
  startedAt?: string; activeRequests?: number;
}

interface AdmissionRecord {
  id: string; tenant: string; kind: "serving" | "job"; requestedGb: number;
  decision: "admitted" | "admitted-after-eviction" | "refused";
  reason?: "job-exclusive" | "budget" | "live-vram" | "cannot-fit-alone";

  // The co-resident set at decision time. Constitution v1.6.1 admits BOUNDED CO-RESIDENCY of
  // serving tenants, so this is a list — there is no single "holder" for serving any more.
  residents?: { tenant: string; kind: "serving" | "job"; vramGb: number }[];
  evicted?: { tenant: string; policy: "idle-first" | "lru"; freedGb: number }[];

  // The TWO distinct checks the constitution requires. They are not interchangeable.
  usableBudgetGb?: number;      // VRAM_GB less the safety reserve
  accountedResidentGb?: number; // sum of the accounted resident set
  liveFreeGb?: number;          // measured free VRAM (already excludes residents)
  headroomGb?: number;

  deviceIndex?: number;
  explanation: string;          // rendered server-side, human-readable (FR-378)
  decidedAt: string;            // a DECISION time, not a queue age (R1)
}
```

**Two checks, not one (constitution v1.6.1).** A load is admissible only if **both** hold:

1. **Budget** — `accountedResidentGb + requestedGb ≤ usableBudgetGb`, bounding the *accounted set*.
2. **Live VRAM** — `requestedGb + headroomGb ≤ liveFreeGb`, bounding the *incoming load*.

These must never be collapsed. `liveFreeGb` already excludes current residents, so summing the
resident set against it double-counts them — that conflation was the v1.6.0 defect corrected by
v1.6.1, and reproducing it in the console would misreport why a model was refused.

**`explanation` is composed server-side**, from the same values the decision used, so the interface
cannot drift from admission's real reasoning. Templates:

- refused/`job-exclusive` → *"Refused: job `{tenant}` holds the GPU exclusively. A running job is
  never preempted."*
- refused/`budget` → *"Refused: admitting {requestedGb} GB would take the resident set to {sum} GB,
  over the {usableBudgetGb} GB usable budget."*
- refused/`live-vram` → *"Refused: needs {requestedGb} GB plus {headroomGb} GB headroom; live free
  VRAM is {liveFreeGb} GB on device {deviceIndex}."*
- refused/`cannot-fit-alone` → *"Refused: {requestedGb} GB exceeds the {usableBudgetGb} GB usable
  budget even with the GPU empty. Evicting other tenants cannot help."*
- `admitted-after-eviction` → *"Admitted to device {deviceIndex} after evicting {tenants}
  ({policy}) to free {freedGb} GB."*
- `admitted` → *"Admitted to device {deviceIndex}: fits the usable budget and live free VRAM
  alongside {n} resident tenant(s)."*

`cannot-fit-alone` is deliberately distinguished from `budget`: the former is **structural** — no
amount of eviction or waiting helps — while the latter is **transient**. That distinction is the
same one the compatibility verdict makes (§5), and the two must agree.

The wording deliberately teaches the platform's rules at the moment they bite: that a job takes the
whole GPU and is never preempted, and that serving tenants share a bounded budget.

```typescript
interface JournalEntry {
  sequence: number; timestamp: string; eventType: string;
  jobId?: string; engineId?: string; pid?: number; deviceIndex?: number;
  fromState?: string; toState?: string; detail?: string;
  checksumState: "ok" | "torn" | "unverified";
}
```

---

## 7. Endpoint (synthesized — no table, R7)

```typescript
interface Endpoint {
  id: string; modality: string;
  desired: { modelName?: string; version?: string; alias?: string; activationState?: string };
  resident: { modelIdentity?: string; registryVersion?: string; engineId?: string; host?: string };
  // FR-415: the complete endpoint status vocabulary.
  status: "unconfigured" | "pending" | "starting" | "healthy" | "degraded"
        | "draining" | "stopped" | "failed" | "unknown";
  traffic?: { requestCount?: number; errorRate?: number; latencyP95Ms?: number; windowSeconds: number };
  conflict?: StateConflict;
  lastUpdated: string;
}
```

**Status rule (FR-416/417)**: `healthy` requires **resident** confirmation. Desired-only ⇒ `pending`.
A CPU modality is `healthy` whenever its child answers. A GPU modality that is not resident because
another tenant holds the GPU is **`stopped`, not `failed`** — on-demand loading is the design, and
labelling it a failure would misrepresent Principle II as a fault.

---

## 8. Inference entities

```typescript
interface PredictionRecord {
  id: string; timestamp: string; endpointId?: string;
  modelName?: string; registryVersion?: string; modality: string;
  status: "ok" | "error"; latencyMs?: number;
  captureState: "not-captured" | "captured" | "expired" | "sampled-out";
  labelState: "unlabeled" | "pending-review" | "labeled" | "disputed" | "excluded";
  traceId?: string; policyResult?: string; error?: string;
}

interface PayloadPreview {
  available: boolean; revealed: false;   // server default — reveal is an explicit second call
  truncated: boolean; totalBytes: number; redactedFields: string[];
  preview?: string;                      // ONLY present on an explicit reveal request
}
```

**Payload rule (FR-409)**: the list and detail projections carry `PayloadPreview` **without**
`preview`. Content requires a separate, explicit request. This makes the default-hidden guarantee
structural — a component cannot accidentally render a payload it was never sent — rather than a
styling choice. Payload identifiers travel in the request body, never the path or query (SC-192).

```typescript
interface TraceSpan {
  spanId: string; parentSpanId?: string; name: string;
  startMs: number; durationMs: number;
  attributes: Record<string, unknown>; events: { name: string; timeMs: number }[];
  status: "ok" | "error"; error?: string;
}
interface TraceDetail {
  traceId: string; predictionId?: string; modelVersion?: string;
  totalDurationMs: number; spans: TraceSpan[];   // generic tree — no token assumptions (FR-413)
}
```

---

## 9. Evaluation, gate, drift

```typescript
interface GateRule {
  metric: string; operator: "gt" | "gte" | "lt" | "lte" | "between";
  threshold?: number; minimum?: number; maximum?: number; scope?: string;
}
interface EvaluationResult {
  id: string; modelName: string; version: string; modality: string;
  datasetRef?: string; benchmarkName?: string; benchmarkDigest?: string;
  metrics: { name: string; value: number; direction: "higher-better" | "lower-better" }[];
  // FR-400 / SC-191: a failure MUST carry the rule that produced it, the observed value,
  // and the incumbent it was compared against — all reachable without leaving the
  // evaluation view. FR-401: an override travels with its recorded reason.
  gate: {
    outcome: "passed" | "failed" | "warning" | "not-evaluated" | "incomplete";
    failedRule?: GateRule; observedValue?: number;
    comparedAgainst?: { version: string; value: number };
    override?: { applied: boolean; reason?: string };
  };
  sourceJobId?: string; createdAt: string;
}
interface DriftReport {
  modelName: string; endpointId?: string;
  referenceWindow: { from: string; to: string }; currentWindow: { from: string; to: string };
  featureCount: number; maxStatistic: number;
  features: { name: string; statistic: number; state: "stable" | "warning" | "significant" }[];
  thresholds: { warning: number; significant: number; configurable: true };
  calculatedAt: string;
}
```

`thresholds` is **returned with the report** (FR-405) so the interface never hard-codes the
0.10/0.25 convention; the limitations text (FR-406) is a static property of the surface, not data.

---

## 10. Redirect map (FR-364 / SC-186)

Every retired path resolves. Each row is one test case in `tests/test_ui_redirects.py`.

| Retired path (021 and pre-021) | → 027 area |
|---|---|
| `/` | `/overview` (was: `serving`) |
| `/serving` | `/deployments` |
| `/data` , `/datasets` | `/datasets` |
| `/training` , `/runs` | `/training` |
| `/models` | `/models` (kept) |
| `/monitoring` , `/monitor` | `/observability` |
| `/retraining` | `/evaluations/drift` |
| `/infer` | `/inference` |
| `/health` | `/observability/health` |
| `/healthz` , `/readyz` | unchanged — probes, not navigation |

`/retraining` → `/evaluations/drift` is the one judgement call: the retraining stage's real content
was policies, the cycle board, and suggestions, which in the ten-area IA split between Evaluations
(drift, gates) and MVP 3's suggestion review. Drift is the closest live destination in 027.

---

## 11. Degradation matrix (FR-428)

Machine-readable, driving both `PlatformHealth.services[].required` and the resilience tests.

| Service down | Overall | Preserved | Degraded |
|---|---|---|---|
| tracking | degraded | deployments, runtime, inference, datasets | experiments, runs, registry, traces |
| gateway | critical | nothing (read-limited shell + cached values only) | everything |
| database | critical | shell only | predictions, labels, jobs, policies, suggestions |
| agent | degraded | catalog, evaluations, datasets, CPU endpoints | runtime → `unknown`; **jobs never inferred stopped** |
| object store | degraded | everything else | artifact previews, downloads, dataset bytes |
| metrics | degraded | current direct state everywhere | historical charts, alert state |
| dashboard embed | healthy | everything | embedded panel → external link |

---

## 12. Datasets and artifacts (US8)

```typescript
interface DatasetVersion {
  name: string; version: string;
  contentDigest: string;                 // content-addressed identity — the platform's own scheme
  sizeBytes: number; objectCount: number; format?: string;
  schemaStatus: "known" | "unknown" | "mismatch";
  validation: {
    status: "passed" | "failed" | "warning" | "not-validated";
    checks?: { name: string; outcome: "pass" | "fail" | "warn"; detail?: string }[];
    validatedAt?: string;
  };
  createdAt: string;
  referencedBy: { runIds: string[]; modelVersions: string[] };   // FR-419
}

interface Artifact {
  uri: string;                           // logical reference — NEVER a presigned or credentialed URL
  kind: "model" | "dataset" | "eval-result" | "capture" | "other";
  sizeBytes?: number;
  digest?: string;
  integrity: "verified" | "verification-failed" | "not-verified" | "verification-unavailable";
  present: boolean;                      // actual existence check, not inferred from the URI
  observedAt: string;
}
```

**Integrity rule (FR-420)** — the four states are genuinely distinct and must not collapse:

- `verified` — a recorded checksum exists and was recomputed to match.
- `verification-failed` — a recorded checksum exists and did **not** match. This is a data-integrity
  incident and must surface in the attention panel (FR-373), not sit quietly in a detail view.
- `not-verified` — a checksum exists but was not recomputed for this response (verification is
  opt-in per request, because rehashing a multi-gigabyte object on every page render is not viable).
- `verification-unavailable` — no checksum was ever recorded for this object.

Collapsing the last two into a single "unverified" would conflate *"we did not check"* with *"there
is nothing to check against"* — materially different facts when an operator is deciding whether to
trust an artifact.

**Access rule (FR-421/422)**: `uri` is a **logical** reference. Byte access always goes through the
gateway's existing proxied download route, which validates the path against an allowlist of
permitted prefixes **before** any upstream request. No presigned URL is ever generated — 025 US3
removed presigned URLs precisely because they were signed against the internal store endpoint
(unresolvable from a browser) and constituted a leaked object-store capability. No credential
reaches the browser under any circumstance.

---

## 13. Observability and administration (US9)

```typescript
interface MetricPanel {
  key: "request-rate" | "error-rate" | "latency-percentiles" | "active-jobs" | "queue-depth"
     | "gpu-utilization" | "gpu-vram" | "engine-restarts"
     | "tracking-health" | "objectstore-health" | "database-health";        // FR-423
  series: { label: string; points: [number, number][] }[];
  unit?: string;
  windowSeconds: number;
  observedAt: string;
  degraded?: boolean;                    // metrics unreachable → no points, NOT zero points
}

interface AlertRule {
  name: string; severity?: string; expression?: string;
  state: "inactive" | "pending" | "firing" | "unknown";                     // FR-424
  activeSince?: string;
  runbookUrl?: string;                   // the 023 US7 runbooks in monitoring/README.md
  // NOTE: there is deliberately NO delivery/notification field. See rule below.
}

interface DashboardEmbed {
  id: string; title: string;
  embedUrl?: string;                     // omitted when embedding is unavailable
  externalUrl: string;                   // ALWAYS present — the fallback target
  embeddable: boolean;                   // resolved server-side from frame policy
  reason?: string;                       // why embedding is unavailable, when it is
}

interface AdminInfo {                                                       // FR-426
  storage: { bucket: string; objectCount?: number; sizeBytes?: number; reachable: boolean }[];
  database: {
    schemaVersion: string;
    migrations: { id: string; appliedAt?: string;
                  checksumState: "ok" | "mismatch" | "unapplied" }[];
    reachable: boolean;
  };
  integrations: { name: string; endpoint?: string; reachable: boolean; version?: string }[];
  apiAccess: { keyConfigured: boolean; failClosed: boolean };  // never the key itself
  system: { platformVersion?: string; constitutionVersion?: string; host?: string;
            uptimeSeconds?: number };
}
```

**Alert honesty rule (FR-424)**: `AlertRule` carries **no** delivery, notification, recipient, or
acknowledgement field, and none may be added. The platform has no Alertmanager and no notification
channel (023 US7 shipped rules deliberately without one). A delivery field would invite the
interface to imply someone was told — the same fake-semantics failure class as an admission queue
(research R1) or a persistent orchestration control plane (FR-397). The surface states rule state
only, and says plainly that no notification was sent.

**Embed rule (FR-425)**: `embeddable` is resolved **server-side** from the configured frame policy,
not discovered by the browser failing to render a frame. `externalUrl` is always populated so the
fallback is structural rather than an error path. The embed carries no administrative controls and
is explicitly labelled as an external dashboard — the platform's dashboard tool runs anonymous and
read-only with a CSP scoped to the console origin (004 US1), and the console must not present it as
a native surface.

**Credential rule (FR-426)**: `apiAccess` reports *whether* a key is configured and whether the
gateway is fail-closed. It never returns key material, and `integrations[].endpoint` is a host
identity, never a credentialed URL.

---

## 14. Console shell (US1)

```typescript
interface NavArea {
  id: "overview" | "models" | "training" | "evaluations" | "deployments"
    | "inference" | "datasets" | "runtime" | "observability" | "administration";
  label: string;
  secondary: { id: string; label: string; href: string }[];
  badge?: { kind: "count" | "state"; value: number | string; severity?: "info" | "warn" | "error" };
}

interface HeaderModel {                                                     // FR-367
  sidebarCollapsed: boolean;
  productName: string;
  mode: "offline" | "live" | "hardware";       // FR-429 — from reachability (R14)
  search: { enabled: boolean };                // FR-368
  health: PlatformHealth;                      // FR-369
  activeJobCount: number | null;               // null = unknown, never 0 on degradation
  notifications: { unread: number; items: AttentionItem[] };
  operator: { authenticated: boolean };        // single operator — no account model (R13)
}

interface AttentionItem {                                                   // FR-373
  id: string;
  kind: "engine-crash" | "gpu-memory-pressure" | "failed-training-run"
      | "evaluation-gate-failure" | "drift-significant" | "unlabeled-backlog"
      | "version-unsigned" | "missing-artifact" | "stale-agent-heartbeat";
  severity: "critical" | "warning" | "info";
  subject: string; detail: string; href: string; observedAt: string;
}
```

**Naming rule (FR-365)**: exactly ten `NavArea.id` values, and every one is a **workflow noun**, not
a backend. No navigation item — primary or secondary — may be named after the tracking server, the
database, the object store, the metrics store, the dashboard tool, the gateway, or the host agent.
`Runtime` and `Administration` are the two that could drift toward vendor naming and must not:
`Runtime` is where GPU *work* is inspected, and `Administration` groups platform-level settings
regardless of which service backs each one.

**Degradation rule**: `activeJobCount` is `number | null`. On gateway or agent loss it is `null`,
rendered as `unknown` — **never** `0`, which would read as "nothing is running" at exactly the
moment the console cannot tell (FR-430, and the reason SC-195 exists).
