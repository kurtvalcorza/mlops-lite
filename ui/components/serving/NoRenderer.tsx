'use client';

import { Panel } from '@/components/Panel';
import type { PanelProps } from './types';

// Fallback renderer (009 US1, FR-077): a registry @serving version whose `task` the UI has no
// renderer for — including a legacy version registered before 009 with no `task` tag at all — shows
// a read-only placeholder instead of breaking the tab. Adding a renderer to RENDERERS is the only
// change needed to support a new task.
export function NoRenderer({ entry }: PanelProps) {
  const label = entry.task ?? 'untagged';
  return (
    <Panel title={label} hint="no renderer">
      <div className="text-caption-md text-mute">
        <p>
          <span className="st-mute">[i]</span> serving{' '}
          <span className="text-ink">
            {entry.model}
            {entry.version ? `@v${entry.version}` : ''}
          </span>{' '}
          for task <span className="text-ink">{label}</span>
          {entry.serving_engine ? ` on ${entry.serving_engine}` : ''}.
        </p>
        <p className="mt-1 text-ash">
          This console has no renderer for this task yet — register a renderer in
          components/serving to add one. Routing still resolves via the gateway.
        </p>
      </div>
    </Panel>
  );
}
