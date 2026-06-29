// Shared types for the task-driven Infer tab (009 US1, FR-077). The tab queries the registry for the
// distinct serving `task`s and renders one panel per task via a renderer map keyed by task. Adding a
// modality = register a model with a `task` tag + drop a renderer into the map (components/infer).

// 008 US3 (FR-068) + 009 US3: the gateway's lease/GPU state. `holder` ∈ {llm, vision, training, asr,
// null} — ASR (whisper.cpp) joined the single lease as a tenant in 009.
export type ServingState = {
  holder: 'llm' | 'vision' | 'training' | 'asr' | null;
  resident: boolean;
  serving_model: string;
  serving_version: string | null;
};

// 009 US1 (FR-077): one registry @serving version → one panel. `task`/`serving_engine` are null for
// a legacy version registered before 009 (→ the "no renderer" placeholder).
export type TaskEntry = {
  model: string;
  version: string;
  task: string | null;
  serving_engine: string | null;
};

// Every per-task renderer receives the discovered registry entry + the shared lease state. The
// lease-governed renderers (stream/classify/transcribe) gate on `serving`; the always-on CPU
// renderers (embed/predict) ignore it (FR-082).
export type PanelProps = { entry: TaskEntry; serving: ServingState | null };
