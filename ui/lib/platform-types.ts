// 027 T694 — the normalized type layer.
//
// This is the Principle V swappability seam: **no component may import a backend payload shape
// directly**. The interface depends on these names, not on what MLflow, the agent, Postgres, or the
// registry happen to return, so replacing any one of them is a change to the projection layer rather
// than to every screen that displays its data.
//
// Two conventions run through the whole file and are the reason it is worth having:
//
//   * **`null` means unknown; it is never zero or empty.** Optional fields are `| null` rather than
//     absent-or-defaulted, because a component that renders `?? 0` turns "the agent is unreachable"
//     into "there are no devices", which is a false statement an operator would act on.
//   * **Conflicts are surfaced, not resolved.** `StateConflict` carries every source's answer, and
//     no field silently prefers one system of record over another.

/** The envelope every console read route returns. */
export type Envelope<T> = {
  data: T | null;
  /** Per source, because a projection over five backends has five data ages. */
  observed: Record<string, string>;
  /** Sources that could not be reached for THIS projection. */
  degraded: string[];
  conflict: StateConflict[] | null;
};

/** Two systems of record disagreeing about one field. Reported, never resolved by precedence. */
export type StateConflict = {
  field: string;
  values: Record<string, unknown>;
  note?: string;
  observed_at: string;
};

// -- health -------------------------------------------------------------------------------------

/** Resolved from reachability, never from a configured string. */
export type PlatformMode = 'full' | 'degraded' | 'minimal';

export type PlatformHealth = {
  mode: PlatformMode;
  services: Record<string, boolean>;
  gpu_free_gb: number | null;
  jobs_active: number | null;
  wedged: boolean | null;
};

/**
 * What this deployment can do. The interface OMITS an unsupported control rather than rendering one
 * that fails — a console that shows every control and lets the backend reject the unsupported ones
 * teaches an operator that the interface is unreliable.
 */
export type Capabilities = {
  runtime_reads: boolean;
  broker: boolean;
  co_residency: boolean;
  jobs_lane: boolean;
  tenant_jobs: boolean;
  sessions: boolean;
};

// -- runtime ------------------------------------------------------------------------------------

/** Provenance of a device reading. On `static`, every field but `index` may legitimately be null. */
export type ReadingSource = 'nvml' | 'smi' | 'static';

export type DeviceProcess = {
  pid: number;
  vram_gb: number | null;
  engine_id: string | null;
};

export type RuntimeDevice = {
  index: number;
  name: string | null;
  uuid: string | null;
  compute_capability: string | null;
  total_vram_gb: number | null;
  free_vram_gb: number | null;
  used_vram_gb: number | null;
  utilization_pct: number | null;
  temperature_c: number | null;
  processes: DeviceProcess[];
  source: ReadingSource;
};

export type RuntimeHost = {
  host: string;
  reachable: boolean;
  device_count: number | null;
  active_engines: string[];
  jobs_active: number | null;
  interrupted_since_start: number | null;
  wedged: boolean | null;
  gpu_free_gb: number | null;
  last_heartbeat: string;
};

/** The coordinator's view of a model's place in the resident set. */
export type ResidencyState = 'loading' | 'resident' | 'draining' | 'evicting' | 'rolling-back';

export type EngineProcess = {
  engine_id: string;
  /** The engine PROCESS's health. Distinct from `residency_state` — see below. */
  state: string;
  gpu: boolean;
  optional: boolean;
  reason: string | null;
  pid: number | null;
  device_index: number | null;
  vram_gb: number | null;
  /**
   * The agent-reported LOADED identity (022) — what is actually resident. Never the registry's
   * desired pointer: the two legitimately diverge during an in-flight activation, which is exactly
   * when an operator is looking, and sourcing it from the pointer would manufacture the falsehood
   * conflict detection exists to catch.
   */
  model_identity: string | null;
  registry_version: string | null;
  started_at: number | null;
  active_requests: number | null;
  /**
   * Deliberately NOT merged with `state`. Collapsing them loses the difference between "the child is
   * fine but its model is being evicted" and "the child is sick".
   */
  residency_state: ResidencyState | null;
};

/** One resident model. Keyed by MODEL INSTANCE, not tenant: many tenants share one resident child. */
export type Resident = {
  model_key: string;
  kind: 'serving' | 'job';
  vram_gb: number;
  state: ResidencyState;
  /** The claim count. 0 in `resident` means genuinely idle and evictable. */
  active_requests: number;
};

export type AdmissionDecision = 'admitted' | 'refused' | 'evicted-retry';

/**
 * `cannot-fit-alone` is deliberately distinct from `budget`: the former is structural — no amount of
 * eviction or waiting helps — and the latter transient. They have opposite remedies.
 */
export type AdmissionReason =
  | 'job-exclusive'
  | 'budget'
  | 'live-vram'
  | 'cannot-fit-alone'
  | 'load-failed';

export type AdmissionRecord = {
  id: string;
  op_id: string | null;
  tenant: string | null;
  kind: 'serving' | 'job';
  model_key: string | null;
  requested_gb: number | null;
  decision: AdmissionDecision;
  reason: AdmissionReason | null;
  attempt: number | null;
  residents: Resident[];
  evicted: { model_key: string; policy: 'idle-first' | 'lru'; freed_gb: number }[];

  // The TWO checks, each with its OWN reservation term. Never collapsed: `live_free_gb` already
  // excludes current residents, so summing the resident set against it double-counts them.
  usable_budget_gb: number | null;
  accounted_resident_gb: number | null;
  /** Σ ALL outstanding reservations → the budget check. */
  reserved_gb: number | null;
  /** Σ not-yet-reconciled reservations → the live-VRAM check only. */
  unmaterialized_gb: number | null;
  live_free_gb: number | null;
  headroom_gb: number | null;

  device_index: number;
  /** Composed SERVER-side. The interface renders it verbatim and never writes its own wording. */
  explanation: string;
  /** A DECISION time, not a queue age — admission decides immediately and has no queue. */
  decided_at: string;
};

export type AdmissionView = {
  observed_at: string;
  residents: Resident[];
  usable_budget_gb: number | null;
  accounted_resident_gb: number | null;
  reserved_gb: number | null;
  unmaterialized_gb: number | null;
  live_free_gb: number | null;
  headroom_gb: number | null;
  job_barrier: boolean;
  active_job: { job_id: string; started_at: number } | null;
  capacity: number;
  records: AdmissionRecord[];
};

/** `torn` is shown as torn, never dropped — a missing final transition is what a crash looks like. */
export type ChecksumState = 'ok' | 'torn';

export type JournalEntry = {
  sequence: number;
  timestamp: string | null;
  event_type: string;
  job_id: string | null;
  engine_id: string | null;
  pid: number | null;
  device_index: number;
  from_state: string | null;
  to_state: string | null;
  detail: string | null;
  checksum_state: ChecksumState;
};

export type JournalPage = {
  entries: JournalEntry[];
  next_cursor: string | null;
  has_more: boolean;
};

// -- catalog ------------------------------------------------------------------------------------

/** `unknown` is a member so an unrecognized task tag renders rather than being filtered out. */
export type Modality =
  | 'text-generation'
  | 'image-classification'
  | 'embedding'
  | 'asr'
  | 'tabular'
  | 'unknown';

export type EvaluationState = 'passed' | 'failed' | 'warning' | 'not-evaluated' | 'incomplete';

export type PlatformModel = {
  id: string;
  name: string;
  version: string;
  modality: Modality;
  registeredModelName: string | null;
  aliases: string[];
  sourceRunId: string | null;
  artifactUri: string | null;
  artifactDigest: string | null;
  artifactSizeBytes: number | null;
  /** Tri-state. `null` is "we did not check", which is NOT "the object is missing". */
  artifactPresent: boolean | null;
  evaluationState: EvaluationState;
  deploymentIds: string[];
  lineage: { baseModel: string | null; baseResolvable: boolean; parentRunId: string | null } | null;
  tags: Record<string, string>;
  serving: boolean;
};

export type CheckResult = 'pass' | 'fail' | 'unknown';

/**
 * `unknown` is never collapsed into `incompatible`: an unreachable agent is not a compatibility
 * fact, and reporting it as one would send an operator to rebuild a model that was fine.
 */
export type CompatibilityVerdict =
  | 'eligible'
  | 'not-currently-eligible'
  | 'incompatible'
  | 'unknown';

export type RuntimeCompatibility = {
  requiredEngine: string | null;
  acceleratorRequired: boolean;
  requiredComputeCapability: string | null;
  artifactAvailable: boolean | null;
  hostCompatible: boolean | null;
  estimatedVramGb: number | null;
  usableBudgetGb: number | null;
  accountedResidentGb: number | null;
  /** Σ ALL outstanding reservations → the budget check only. */
  reservedGb: number | null;
  /** Σ not-yet-reconciled reservations → the live-VRAM check only. */
  unmaterializedGb: number | null;
  liveFreeVramGb: number | null;
  headroomGb: number | null;
  budgetCheck: CheckResult;
  liveVramCheck: CheckResult;
  fitsAlone: boolean | null;
  jobExclusive: boolean;
  verdict: CompatibilityVerdict;
  reasons: string[];
};

// -- jobs ---------------------------------------------------------------------------------------

export type JobState =
  | 'Draft'
  | 'Queued'
  | 'AdmissionCheck'
  | 'Admitted'
  | 'Starting'
  | 'Running'
  | 'Completing'
  | 'Succeeded'
  | 'Failed'
  | 'Cancelled'
  | 'Rejected'
  | 'Orphaned'
  | 'Unknown';

/**
 * One unit of work across its three identifiers. The three native states are kept alongside the
 * normalized one (FR-392): the normalization is for scanning a list, the natives are for debugging,
 * and dropping them would send the operator back to the three systems this join exists to replace.
 */
export type PlatformJob = {
  id: string;
  type: 'training' | 'hpo' | 'batch' | 'shadow' | 'evaluation' | 'inference';
  normalizedState: JobState;
  gatewayState: string | null;
  agentState: string | null;
  trackingRunState: string | null;
  runId: string | null;
  studyId: string | null;
  modelId: string | null;
  assignedHost: string | null;
  assignedDevice: number | null;
  admissionReason: string | null;
  createdAt: number | null;
  startedAt: number | null;
  completedAt: number | null;
  observed: Record<string, string | null>;
  conflict: JobConflict | null;
  timeline?: { at: number; event: string }[];
  resources?: { device_index: number | null; vram_gb: number | null; host: string | null };
};

/**
 * `skewExceeded` suppresses the claim rather than reporting it. A stale reading disagreeing with a
 * fresh one is not evidence of inconsistency, and a banner that cries wolf costs the real conflicts
 * their audience.
 */
export type JobConflict = {
  entity: 'job' | 'model' | 'endpoint' | 'engine';
  entityId: string;
  sources: { source: string; state: string | null; observedAt: string | null }[];
  skewExceeded: boolean;
  conflict: boolean;
  suggestedAction: 'refresh' | 'inspect-journal' | 'reconcile';
  lastConsistentAt?: string | null;
};

// -- overview -----------------------------------------------------------------------------------

/**
 * The eight Overview cards. Every one is `| null` — that is the type system carrying the
 * null-is-not-zero rule, so a component cannot quietly write `?? 0` without the compiler having
 * shown it the alternative.
 *
 * `admissionDecisions` occupies the slot FR-371 calls "pending admissions". There is no pending
 * count: admission decides synchronously and has no queue, so that card would read `0` forever —
 * and a permanent zero teaches an operator that requests never wait, which is the opposite of what
 * a refusal means.
 */
export type SummaryCards = {
  activeEndpoints: number | null;
  runningJobs: number | null;
  gpuUtilization: number | null;
  admissionDecisions: { admitted: number; refused: number } | null;
  failedJobs: number | null;
  modelsRequiringReview: number | null;
  unlabeledCaptures: number | null;
  driftWarnings: number | null;
};

export type AttentionKind =
  | 'engine-crash'
  | 'gpu-memory-pressure'
  | 'failed-training-run'
  | 'evaluation-gate-failure'
  | 'drift-significant'
  | 'unlabeled-backlog'
  | 'version-unsigned'
  | 'missing-artifact'
  | 'stale-agent-heartbeat';

export type AttentionItem = {
  id: string;
  kind: AttentionKind;
  severity: 'critical' | 'warning' | 'info';
  subject: string;
  detail: string;
  href: string;
  observedAt: string;
};

/** The 021 loop, kept as a visualization now that it is no longer navigation. */
export type ActivityStage = 'data' | 'train' | 'evaluate' | 'deploy' | 'infer' | 'monitor';

export type ActivityEvent = {
  at: string;
  stage: ActivityStage;
  kind: string;
  subject: string;
  detail: string;
  href: string;
};

export type SearchResult = {
  kind: 'model' | 'run' | 'dataset' | 'job' | 'endpoint' | 'prediction';
  id: string;
  label: string;
  href: string;
  exact: boolean;
};

// -- navigation ---------------------------------------------------------------------------------

/**
 * A console area. No area may be named after a backing service (FR-365) — an operator should not
 * have to know which process answers a question in order to find where to ask it.
 */
export type NavArea = {
  slug: string;
  label: string;
  description: string;
  children?: { slug: string; label: string }[];
};
