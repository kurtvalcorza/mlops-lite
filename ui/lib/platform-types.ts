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

/**
 * What KIND of deployment this is — not how well it is working. Resolved from reachability, never
 * from a configured string. A fixture-backed console and a GPU-backed one are both legitimately
 * `healthy`; an operator needs to know which they are looking at before trusting any number.
 */
export type PlatformMode = 'offline' | 'live' | 'hardware';

/** How well the platform is working. Deliberately separate from `PlatformMode`. */
export type HealthState = 'healthy' | 'degraded' | 'critical' | 'unknown';

/** `required: false` means impairment degrades and never criticals (FR-370). */
export type ServiceHealth = {
  service: 'gateway' | 'tracking' | 'database' | 'objectstore' | 'agent' | 'metrics' | 'gpu';
  state: HealthState;
  required: boolean;
  detail: string | null;
  observedAt: string;
};

export type PlatformHealth = {
  overall: HealthState;
  mode: PlatformMode;
  services: ServiceHealth[];
  /** The same reachability as a map, for one-line summaries. Derived once, server-side. */
  reachable: Record<string, boolean>;
  gpu_free_gb: number | null;
  jobs_active: number | null;
  wedged: boolean | null;
  observedAt: string;
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

// -- evaluations, gates, drift ------------------------------------------------------------------

/** Metrics are modality-native. There is no shared "score" type here, deliberately (FR-399). */
export type EvaluationMetric = {
  name: string;
  value: number;
  /** Without this, a number cannot be read as good or bad — and a surface assuming higher-better
   * would rank every WER backwards. */
  direction: 'higher-better' | 'lower-better';
};

export type GateRule = {
  metric: string;
  operator: 'gt' | 'gte' | 'lt' | 'lte' | 'between';
  threshold: number | null;
  scope: string | null;
};

/** A failure carries its evidence: the rule, the observed value, and the incumbent (SC-191). */
export type GateView = {
  outcome: 'passed' | 'failed' | 'warning' | 'not-evaluated' | 'incomplete';
  reason?: string | null;
  mode?: string | null;
  tolerance?: number | null;
  failedRule: GateRule | null;
  observedValue: number | null;
  comparedAgainst: { version: string; value: number } | null;
  delta?: number | null;
  /** An override with no reason is indistinguishable from a gate that was never enforced. */
  override: { applied: boolean; reason: string | null };
};

export type EvaluationResult = {
  id: string;
  modelName: string;
  version: string;
  modality: string | null;
  datasetRef: string | null;
  benchmarkName: string | null;
  benchmarkDigest: string | null;
  metrics: EvaluationMetric[];
  gate: GateView;
  sourceJobId: string | null;
  createdAt: string | null;
};

export type GateConfig = {
  mode: string;
  tolerance: number;
  missingMetricPolicy: string;
  rules: { metric: string; operator: string; scope?: string }[];
};

/** Six dimensions, kept apart. There is no combined verdict field, and that is the design. */
export type ComparisonView = {
  challenger: { name: string; version: string };
  champion: { name: string; version: string } | null;
  quality: { challenger: EvaluationMetric | null; champion: EvaluationMetric | null };
  latency: { challenger: number | null; champion: number | null };
  resources: { challenger: number | null; champion: number | null };
  artifacts: { challenger: string | null; champion: string | null };
  datasets: { challenger: string | null; champion: string | null };
  policy: { gate: GateView };
};

export type DriftReportView = {
  modelName: string | null;
  endpointId: string | null;
  referenceWindow: { from: string | null; to: string | null };
  currentWindow: { from: string | null; to: string | null };
  featureCount: number;
  /** `null`, not `0`, when nothing was measurable — a max of zero reads as "no drift". */
  maxStatistic: number | null;
  features: { name: string; statistic: number | null; state: string | null }[];
  /** Shipped with the report so the interface never restates the 0.10/0.25 convention (FR-405). */
  thresholds: { warning: number; significant: number; configurable: true };
  calculatedAt: string | null;
};

export type DriftView = { reports: DriftReportView[]; limitations: string[] };

// -- inference ------------------------------------------------------------------------------------

export type PredictionRecord = {
  id: string;
  timestamp: string | number | null;
  endpointId: string | null;
  modelName: string | null;
  registryVersion: string | null;
  modality: string;
  status: 'ok' | 'error';
  latencyMs: number | null;
  captureState: 'not-captured' | 'captured' | 'expired' | 'sampled-out';
  labelState: 'unlabeled' | 'pending-review' | 'labeled' | 'disputed' | 'excluded';
  traceId: string | null;
  policyResult: string | null;
  error: string | null;
};

/**
 * `preview` is **optional and absent by default** — not an empty string, not null. The content is
 * never sent unless explicitly requested, so a component cannot render a payload it was never
 * given. That is what makes hidden-by-default hold under refactoring (FR-408).
 */
export type PayloadPreview = {
  available: boolean;
  revealed: boolean;
  truncated: boolean;
  /** The TRUE stored size, even when `preview` is truncated. */
  totalBytes: number | null;
  redactedFields: string[];
  preview?: string;
};

export type PredictionDetail = PredictionRecord & { payload: PayloadPreview };

export type CaptureRow = {
  predictionId: string;
  modality: string | null;
  modelName: string | null;
  capturedAt: string | number | null;
  labelState: string;
  /** Whether a payload exists — NOT a link to it. Bytes move only through the explicit reveal. */
  hasPayload: boolean;
};

export type ReviewItem = {
  predictionId: string;
  modality: string | null;
  modelName: string | null;
  labelState: string;
  /** Every item states which signals put it here; a queue that ranks silently is taken on faith. */
  signals: string[];
  reason: string;
  capturedAt: string | number | null;
};

/** A generic span tree. No token-oriented fields: three of five modalities have no tokens. */
export type TraceSpan = {
  spanId: string;
  parentSpanId: string | null;
  name: string;
  startMs: number;
  durationMs: number;
  attributes: Record<string, unknown>;
  events: { name: string; timeMs: number }[];
  status: 'ok' | 'error';
  error: string | null;
  depth?: number;
};

export type TraceDetail = {
  traceId: string | null;
  predictionId: string | null;
  modelVersion: string | null;
  totalDurationMs: number;
  spans: TraceSpan[];
};

// -- deployments ----------------------------------------------------------------------------------

/**
 * `healthy` requires resident confirmation; desired-only is `pending`. A GPU modality that is not
 * resident because a job holds the GPU is `stopped`, **not** `failed` — on-demand loading is the
 * design, and calling it a failure misrepresents Principle II as a fault.
 */
export type EndpointStatus =
  | 'unconfigured'
  | 'pending'
  | 'starting'
  | 'healthy'
  | 'degraded'
  | 'draining'
  | 'stopped'
  | 'failed'
  | 'unknown';

export type PlatformEndpoint = {
  id: string;
  modality: string;
  desired: {
    modelName: string | null;
    version: string | null;
    alias: string | null;
    activationState: string | null;
  };
  resident: {
    /** The agent-reported LOADED identity, never the registry's desired pointer. */
    modelIdentity: string | null;
    registryVersion: string | null;
    engineId: string | null;
    host: string | null;
  };
  status: EndpointStatus;
  /** `null` rather than zeros: per-endpoint traffic is not measured here, and zeros read as "none". */
  traffic: null;
  conflict: JobConflict | null;
  lastUpdated: string | null;
};

// -- datasets and artifacts -----------------------------------------------------------------------

/**
 * Four states, none of which may collapse into another. `not-verified` ("we did not check") and
 * `verification-unavailable` ("no checksum was ever recorded") are materially different facts when
 * an operator is deciding whether to trust an artifact: the first is a button away from an answer,
 * the second means no answer exists.
 */
export type IntegrityState =
  | 'verified'
  | 'verification-failed'
  | 'not-verified'
  | 'verification-unavailable';

export type DatasetVersionView = {
  name: string;
  version: string;
  contentDigest: string | null;
  sizeBytes: number | null;
  objectCount: number | null;
  format: string | null;
  schemaStatus: 'known' | 'unknown' | 'mismatch';
  validation: {
    status: 'passed' | 'failed' | 'warning' | 'not-validated';
    checks: { name: string; outcome: string; detail?: string }[] | null;
    validatedAt: string | null;
  };
  createdAt: string | null;
  referencedBy: { runIds: string[]; modelVersions: string[] };
};

export type ArtifactView = {
  /** A LOGICAL reference. Never presigned, never credentialed — bytes move through the proxy. */
  uri: string | null;
  kind: 'model' | 'dataset' | 'eval-result' | 'capture' | 'other';
  sizeBytes: number | null;
  digest: string | null;
  integrity: IntegrityState;
  /** An actual existence check; `null` means unchecked, which is not missing. */
  present: boolean | null;
  observedAt: string | null;
};

// -- observability and administration ---------------------------------------------------------------

export type MetricPanelView = {
  key: string;
  series: { label: string; points: [number, number][] }[];
  unit: string | null;
  windowSeconds: number;
  observedAt: string | null;
  /** A degraded panel carries NO points, never zero points — a flat line at zero is a claim. */
  degraded: boolean;
};

export type MetricsSummary = { panels: MetricPanelView[]; windowSeconds: number };

/**
 * There is deliberately NO delivery, notification, recipient, or acknowledgement field, and none
 * may be added. This platform has no Alertmanager; such a field would invite the console to imply
 * someone was told, and an operator who believes a page went out will not send one.
 */
export type AlertRuleView = {
  name: string;
  severity: string | null;
  expression: string | null;
  state: 'inactive' | 'pending' | 'firing' | 'unknown';
  activeSince: string | null;
  runbookUrl: string | null;
};

export type AlertsView = { rules: AlertRuleView[]; noDeliveryNotice: string };

export type DashboardEmbed = {
  id: string;
  title: string;
  /** ALWAYS present — the fallback is structural, not an error path. */
  externalUrl: string;
  /** Resolved server-side from the frame policy, not by the browser failing to render a frame. */
  embeddable: boolean;
  reason: string | null;
  /** Omitted entirely when embedding is unavailable, rather than present-and-empty. */
  embedUrl?: string;
};

export type AdminStorage = {
  bucket: string;
  /** `null` on an unreachable bucket. `0` would read as an empty bucket. */
  objectCount: number | null;
  sizeBytes: number | null;
  reachable: boolean;
};

export type AdminDatabase = {
  schemaVersion: string | null;
  migrations: { id: string; appliedAt: string | null; checksumState: string }[] | null;
  reachable: boolean;
};

export type AdminIntegration = {
  name: string;
  /** A host identity, never a credentialed URL — credentials are stripped server-side. */
  endpoint: string | null;
  reachable: boolean | null;
  version: string | null;
};

export type AdminSystem = {
  platformVersion: string | null;
  constitutionVersion: string | null;
  host: string | null;
  uptimeSeconds: number | null;
  /** Whether a key is configured and whether the gateway is fail-closed. Never the key. */
  apiAccess: { keyConfigured: boolean; failClosed: boolean };
};

// -- tracking -----------------------------------------------------------------------------------

/** Tracking vocabulary preserved verbatim (FR-366): run, experiment, metric, param. */
export type TrackingRun = {
  run_id: string;
  name: string;
  experiment_id: string;
  experiment_name: string;
  status: string;
  start_time: number | null;
  end_time: number | null;
  job_id: string | null;
  metrics: Record<string, unknown>;
  params: Record<string, unknown>;
};

export type Experiment = { experiment_id: string; name: string; lifecycle_stage: string };

/**
 * Recorded executions of a completed sequence of trainings. Every field is past tense on purpose:
 * there is no persistent search service, and a present-tense view would invite an operator to wait
 * for a next trial nobody scheduled (FR-397).
 */
export type StudyTrials = {
  study_id: string;
  status: string;
  completed: number;
  recorded: number;
  metric: string | null;
  direction: string | null;
  best: { version: string; value: number; metric: string | null } | null;
  trials: {
    number: number;
    value: number | null;
    state: string;
    params: Record<string, unknown>;
    version: string | null;
    /** A trial that produced no model FAILED. It is not scored worst — that would let a crash
     * masquerade as a bad hyperparameter choice. */
    failed: boolean;
  }[];
  history: { number: number; value: number }[];
  axes: string[];
  /** The trial count travels with every correlation: four trials and four hundred are not the
   * same claim, and printing them identically would be the misleading part. */
  importance: Record<string, { correlation: number; trials: number }>;
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
