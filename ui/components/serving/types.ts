// Shared types for the task-driven Infer tab (009 US1, FR-077). The tab queries the registry for the
// distinct serving `task`s and renders one panel per task via a renderer map keyed by task. Adding a
// modality = register a model with a `task` tag + drop a renderer into the map (components/serving).

// 008 US3 (FR-068) + 009 US3: the gateway's lease/GPU state. `holder` ∈ {llm, vision, training, asr,
// null} — ASR (whisper.cpp) joined the single lease as a tenant in 009. 022 (FR-260/274): the
// model+version are the AGENT-reported served identity ("unknown" when the agent is unreachable),
// and a served fine-tune exposes its resolved base + adapter provenance.
export type ServingState = {
  holder: 'llm' | 'vision' | 'training' | 'asr' | null;
  resident: boolean;
  serving_model: string;
  serving_version: string | null;
  base?: string | null;
  adapter?: string | null;
};

// 009 US1 (FR-077): one registry @serving version → one panel. `task`/`serving_engine` are null for
// a legacy version registered before 009 (→ the "no renderer" placeholder). 022 (FR-268): a
// text-generation entry carries its base-vs-adapter kind + lineage (base/dataset/parent).
export type TaskEntry = {
  model: string;
  version: string;
  task: string | null;
  serving_engine: string | null;
  kind?: 'full-model' | 'lora-adapter' | null;
  lineage?: Record<string, string> | null;
};

// Every per-task renderer receives the discovered registry entry + the shared lease state. The
// lease-governed renderers (stream/classify/transcribe) gate on `serving`; the always-on CPU
// renderers (embed/predict) ignore it (FR-082).
export type PanelProps = { entry: TaskEntry; serving: ServingState | null };

// 023 US5 (T525 — contracts/promotion-activation.md §Read): the desired/resident/activation read
// model. `desired` is the ActiveServingLLM pointer, `resident` is AGENT-reported, `consistent`
// only when they agree in a terminal-success state — incomplete desired state is never shown as
// serving.
export type ActivationView = {
  desired: { model_name: string | null; version: string | null };
  resident: { model_name: string | null; version: string | null; resident: boolean };
  activation: {
    operation_id: string;
    state: string;
    target: string;
    attempts: number;
    last_error: string | null;
    last_error_code: string | null;
  } | null;
  consistent: boolean;
};
