'use client';

// 021 T424 (FR-210/211/213, research R2): ONE shared live-state source for the loop shell.
// Subscribes to the `platform/events` SSE snapshot (already allow-listed) and adds light polling
// for the per-stage badge values the stream does not carry (lease detail, open suggestions,
// candidate-awaiting-promotion, latest breach). Everything degrades to `null` (= unknown/at-rest)
// when the platform is unreachable — the shell renders and navigates regardless.
//
// Mounted ONCE (in LoopNav); StageBadge/GpuPill consume the returned value as props so the console
// holds a single EventSource + one set of polls, not one per badge.

import { useEffect, useRef, useState } from 'react';
import { gwGet } from '@/lib/gw';

export type LeaseState = {
  // Since 018/T364 the holder is whichever admission tenant holds the GPU (ASR included); a
  // `kind="job"` holder (batch/retrain) surfaces as its job label — keep the type open for that.
  holder: 'llm' | 'vision' | 'asr' | 'training' | (string & {}) | null;
  resident: boolean;
  serving_model: string;
  serving_version: string | null;
};

export type PlatformSnap = {
  all_healthy?: boolean;
  daemons?: Record<string, { reachable: boolean; url: string }>;
  serving?: {
    resident?: boolean;
    est_vram_gb?: number;
    fits?: boolean;
    vram_budget_gb?: number;
  } | null;
  gpu_free?: number | null;
};

export type LiveState = {
  /** null until the first signal lands; false = platform unreachable (badges show unknown). */
  reachable: boolean | null;
  snap: PlatformSnap | null;
  lease: LeaseState | null;
  /** retraining badge: open promotion suggestions (null = unknown). */
  openSuggestions: number | null;
  /** models badge: some model has a registered version newer than its @serving pointer. */
  candidate: boolean | null;
  /** monitoring badge: the latest drift OR quality report breached. */
  breach: boolean | null;
};

type ModelRow = { name: string; serving_version: string | null };
type ModelDetail = { versions?: { version: string; serving: boolean }[] };
type DriftReport = { dataset_drift?: boolean };
type QualityReport = { breach?: boolean };

const LEASE_MS = 4000; // lease pill + training badge — snappy
const SLOW_MS = 15000; // counts/breach — light background reads

export function useLiveState(): LiveState {
  const [snap, setSnap] = useState<PlatformSnap | null>(null);
  const [lease, setLease] = useState<LeaseState | null>(null);
  const [openSuggestions, setOpenSuggestions] = useState<number | null>(null);
  const [candidate, setCandidate] = useState<boolean | null>(null);
  const [breach, setBreach] = useState<boolean | null>(null);
  const [sseUp, setSseUp] = useState<boolean | null>(null);
  const [pollUp, setPollUp] = useState<boolean | null>(null);
  const esRef = useRef<EventSource | null>(null);

  // Live snapshot channel (SSE via the BFF — EventSource is GET-only, key injected server-side).
  useEffect(() => {
    const es = new EventSource('/api/gw/platform/events');
    esRef.current = es;
    es.onopen = () => setSseUp(true);
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.event === 'state') {
          setSnap(data);
          setSseUp(true);
        }
      } catch {
        /* ignore keep-alives */
      }
    };
    es.onerror = () => setSseUp(false); // EventSource auto-reconnects; flag unknown meanwhile
    return () => es.close();
  }, []);

  // Lease detail — the GPU pill + the training badge (holder === 'training').
  useEffect(() => {
    let alive = true;
    const tick = () =>
      gwGet<LeaseState>('serving/state')
        .then((s) => {
          if (!alive) return;
          setLease(s);
          setPollUp(true);
        })
        .catch(() => {
          if (!alive) return;
          setLease(null); // unknown, not "idle" — FR-213
          setPollUp(false);
        });
    tick();
    const id = setInterval(tick, LEASE_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // Slow lane: suggestion count, candidate-awaiting-promotion (documented N+1), latest breach.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      // retraining badge
      try {
        const d = await gwGet<{ suggestions: unknown[] }>('suggestions?state=open');
        if (alive) setOpenSuggestions((d.suggestions ?? []).length);
      } catch {
        if (alive) setOpenSuggestions(null);
      }
      // models badge — list, then per-model version check (nav-and-routes.md: N+1, acceptable at
      // single-operator scale). A model counts when a registered version is newer than @serving
      // (or nothing is promoted yet but versions exist).
      try {
        const d = await gwGet<{ models: ModelRow[] }>('models');
        const rows = d.models ?? [];
        let found = false;
        for (const m of rows) {
          try {
            const det = await gwGet<ModelDetail>(`models/${encodeURIComponent(m.name)}`);
            const versions = det.versions ?? [];
            if (versions.length === 0) continue;
            if (m.serving_version == null) {
              found = true;
            } else {
              const cur = Number(m.serving_version);
              if (versions.some((v) => Number(v.version) > cur)) found = true;
            }
            if (found) break;
          } catch {
            /* skip this model — partial signal beats none */
          }
        }
        if (alive) setCandidate(found);
      } catch {
        if (alive) setCandidate(null);
      }
      // monitoring badge — newest drift + quality reports
      try {
        const [drift, qual] = await Promise.all([
          gwGet<{ reports: DriftReport[] }>('monitor?limit=1').catch(() => null),
          gwGet<{ reports: QualityReport[] }>('monitor/quality?limit=1').catch(() => null),
        ]);
        if (alive) {
          if (drift === null && qual === null) setBreach(null);
          else
            setBreach(
              Boolean(drift?.reports?.[0]?.dataset_drift) || Boolean(qual?.reports?.[0]?.breach),
            );
        }
      } catch {
        if (alive) setBreach(null);
      }
    };
    tick();
    const id = setInterval(tick, SLOW_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // Reachable when either live channel is delivering; null only before the first signal.
  const reachable = sseUp === null && pollUp === null ? null : Boolean(sseUp || pollUp);

  return { reachable, snap, lease, openSuggestions, candidate, breach };
}
