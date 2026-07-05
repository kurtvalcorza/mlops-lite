'use client';

// 021 T426 (FR-210/213): per-stage live status glyph for the loop bar. Reads the shared LiveState
// (mounted once in LoopNav) — never fetches on its own. Signals per nav-and-routes.md:
//   training   = GPU-resident training (serving/state holder — the only derivable live run signal)
//   models     = candidate-awaiting-promotion
//   serving    = resident engine name
//   monitoring = latest-breach dot
//   retraining = open-suggestion count
// Platform unreachable → every glyph degrades to `?` (unknown), never blocks the nav (FR-213).

import type { LiveState } from '@/lib/useLiveState';

export type StageKey = 'data' | 'training' | 'models' | 'serving' | 'monitoring' | 'retraining';

export function StageBadge({ stage, live }: { stage: StageKey; live: LiveState }) {
  // Before the first signal or while unreachable: unknown/at-rest for every stage (FR-213).
  if (live.reachable === false || live.reachable === null) {
    if (stage === 'data') return null; // data carries no badge (contract: none required)
    return (
      <span className="text-ash" title="platform unreachable — state unknown">
        ?
      </span>
    );
  }

  switch (stage) {
    case 'data':
      return null;
    case 'training': {
      const active = live.lease?.holder === 'training';
      return active ? (
        <span className="st-accent" title="a training run holds the GPU">
          ~
        </span>
      ) : (
        <Rest title="no GPU-resident training" />
      );
    }
    case 'models': {
      if (live.candidate === null) return <Unknown />;
      return live.candidate ? (
        <span className="st-accent" title="a candidate version awaits promotion">
          +
        </span>
      ) : (
        <Rest title="nothing awaiting promotion" />
      );
    }
    case 'serving': {
      if (live.lease === null) return <Unknown />;
      return live.lease.resident ? (
        <span className="st-accent" title={`resident: ${live.lease.holder ?? 'serving model'}`}>
          ●
        </span>
      ) : (
        <Rest title="no model resident (lease idle)" />
      );
    }
    case 'monitoring': {
      if (live.breach === null) return <Unknown />;
      return live.breach ? (
        <span className="st-danger" title="latest check breached">
          !
        </span>
      ) : (
        <Rest title="latest checks clean" />
      );
    }
    case 'retraining': {
      if (live.openSuggestions === null) return <Unknown />;
      return live.openSuggestions > 0 ? (
        <span className="st-accent" title={`${live.openSuggestions} open promotion suggestion(s)`}>
          {live.openSuggestions}
        </span>
      ) : (
        <Rest title="no open suggestions" />
      );
    }
  }
}

function Unknown() {
  return (
    <span className="text-ash" title="state unknown">
      ?
    </span>
  );
}

function Rest({ title }: { title: string }) {
  return (
    <span className="st-mute" title={title}>
      ·
    </span>
  );
}
