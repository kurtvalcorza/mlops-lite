// Renderer map for the task-driven Infer tab (009 US1, FR-077). The tab discovers the registry's
// serving `task`s and renders one panel per task by looking it up here; a task with no entry falls
// to NoRenderer (a read-only placeholder, never a broken tab). Adding a modality = register a model
// with a `task` tag (backend) + add one line to this map (frontend) — no tab re-plumbing.
//
// Lease-governed renderers (stream/classify/transcribe) gate on the GPU lease state; the always-on
// CPU renderers (embed/predict) ignore it (FR-082). Each renderer takes the same PanelProps.
import type { ComponentType } from 'react';
import { ClassifyPanel } from './ClassifyPanel';
import { EmbedPanel } from './EmbedPanel';
import { StreamPanel } from './StreamPanel';
import type { PanelProps } from './types';

export { NoRenderer } from './NoRenderer';
export type { PanelProps, ServingState, TaskEntry } from './types';

export const RENDERERS: Record<string, ComponentType<PanelProps>> = {
  'text-generation': StreamPanel,
  'image-classification': ClassifyPanel,
  embedding: EmbedPanel, // 009 Phase 2 (US2) — CPU, off-lease, always-on
  // 'asr': TranscribePanel,       // added in 009 Phase 3 (US3)
  // 'tabular': TabularPanel,      // added in 009 Phase 4 (US4)
};
