# Phase 1 Data Model: 026 Unified ML Lifecycle Console

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

### Normalization table (FR-392)

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
banner. `reconcile` is surfaced but inert in 026 (MVP 3 owns automated reconciliation).

---

## 5. PlatformModel and compatibility

```typescript
interface PlatformModel {
  id: string; name: string;
  modality: "text-generation" | "image-classification" | "embedding" | "asr" | "tabular" | "unknown";
  loggedModelId?: string; registeredModelName?: string; version?: string;
  aliases: string[]; sourceRunId?: string;
  artifactUri: string; artifactDigest?: string; artifactSizeBytes?: number;
  artifactPresent: boolean;                     // object-store existence, not assumed (FR-384)
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
  largestFreeVramGb?: number;
  verdict: "eligible" | "not-currently-eligible" | "incompatible" | "unknown";
  reasons: string[];
}
```

**Verdict rule (FR-388)** — the distinction that matters:

- `incompatible` — **structural**: engine unavailable, compute capability mismatch, artifact missing,
  adapter base unresolvable. Waiting will not help.
- `not-currently-eligible` — **transient**: estimated VRAM exceeds the largest free block, or another
  tenant holds the GPU. Waiting will help.
- `unknown` — the agent is unreachable. **Never** collapse this into either of the above; an
  unreachable agent is not a compatibility fact.

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
  decision: "admitted" | "refused";
  reason?: "held" | "vram";
  explanation: string;         // rendered server-side, human-readable (FR-378)
  holder?: string; holderKind?: "serving" | "job";
  deviceIndex?: number; largestFreeGb?: number;
  decidedAt: string;           // a DECISION time, not a queue age (R1)
}
```

**`explanation` is composed server-side**, from the same values the decision used, so the interface
cannot drift from admission's real reasoning. Templates:

- refused/`held` by a job → *"Refused: job `{holder}` holds the GPU. A running job is never
  preempted."*
- refused/`held` by a serving model → *"Refused: serving model `{holder}` is resident. An
  operator-confirmed swap can displace it."*
- refused/`vram` → *"Refused: needs {requestedGb} GB; largest free block is {largestFreeGb} GB on
  device {deviceIndex}."*
- admitted → *"Admitted to device {deviceIndex}: capability and available-memory checks passed."*

The wording deliberately teaches the platform's non-negotiable rule at the moment it bites.

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

| Retired path (021 and pre-021) | → 026 area |
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
(drift, gates) and MVP 3's suggestion review. Drift is the closest live destination in 026.

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
