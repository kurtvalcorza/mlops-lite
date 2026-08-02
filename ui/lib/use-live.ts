// 027 T695 — the shared live-fetch hook.
//
// Every live panel in the console goes through this, because the truthfulness properties are
// **infrastructure written once**, not per-page polish. Retrofitting data-age, degradation, and
// backoff into finished pages is far more expensive than building them into the fetch layer, and a
// page that forgot one of them displays a confident falsehood rather than an obvious gap.
//
// It carries all of:
//
//   * **Visibility-gated polling** — a hidden tab polls nothing. Ten live panels against the agent's
//     bounded transport is a self-inflicted control-plane DoS, and most of those panels are usually
//     behind a background tab.
//   * **Exponential backoff, `Retry-After`-aware** — a failing backend is not helped by the console
//     hammering it at the steady-state cadence.
//   * **Last-known-good with data age** — a value that is 40 seconds old is shown as 40 seconds old,
//     never as current. Stale-but-labelled beats blank; stale-and-unlabelled is a lie.
//   * **Bounded retention** — exactly one previous value is kept. An unbounded history here is the
//     most likely cause of a console footprint regression, and nothing in the interface needs more
//     than the last good reading.

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { Envelope } from './platform-types';
import { gwGet } from './gw';

/** Per-resource cadences. Runtime moves fastest; catalog and admin barely move at all. */
export const CADENCE = {
  runtime: 3000,
  jobs: 4000,
  health: 5000,
  catalog: 15000,
  admin: 30000,
} as const;

const BACKOFF_BASE_MS = 1000;
const BACKOFF_MAX_MS = 30000;

export type LiveState<T> = {
  /** The most recent successful value, or null if we have never had one. Never a fabricated zero. */
  data: T | null;
  /** Milliseconds since `data` was observed. null when we have never had a value. */
  ageMs: number | null;
  /** Sources that could not be reached for the latest reading. */
  degraded: string[];
  conflict: Envelope<T>['conflict'];
  /** True only before the first settled attempt — a refresh is not a loading state. */
  loading: boolean;
  /** Set when the LATEST attempt failed. `data` may still hold the last known good value. */
  error: string | null;
  /** True while showing a value the latest attempt did not refresh. */
  stale: boolean;
  refresh: () => void;
};

function isVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState === 'visible';
}

/**
 * Poll a console read route, returning its envelope plus data age and staleness.
 *
 * `path` is the gateway path after `/api/gw/` — the BFF injects the operator key server-side, so no
 * credential is ever in the browser.
 */
export function useLive<T>(path: string, intervalMs: number = CADENCE.runtime): LiveState<T> {
  const [data, setData] = useState<T | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [conflict, setConflict] = useState<Envelope<T>['conflict']>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [observedAt, setObservedAt] = useState<number | null>(null);
  const [, forceTick] = useState(0);

  // Bounded retention: refs hold exactly the scheduling state, never a history.
  const failures = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const alive = useRef(true);

  const tick = useCallback(async () => {
    if (!alive.current) return;
    if (!isVisible()) {
      // Hidden: reschedule without fetching. Checking on the timer rather than only on
      // `visibilitychange` means a tab hidden before mount also polls nothing.
      schedule(intervalMs);
      return;
    }
    try {
      const envelope = await gwGet<Envelope<T>>(path);
      if (!alive.current) return;
      failures.current = 0;
      setError(null);
      setDegraded(envelope.degraded ?? []);
      setConflict(envelope.conflict ?? null);
      if (envelope.data !== null && envelope.data !== undefined) {
        setData(envelope.data);
        setObservedAt(Date.now());
        setStale(false);
      } else {
        // A degraded read. Keep the last known good value and SAY it is stale — replacing it with
        // null would blank a panel that still has something true to show, and replacing it silently
        // would present old data as current.
        setStale(true);
      }
      schedule(intervalMs);
    } catch (e) {
      if (!alive.current) return;
      failures.current += 1;
      setError(e instanceof Error ? e.message : String(e));
      setStale(true);
      schedule(backoffMs(failures.current, e));
    } finally {
      if (alive.current) setLoading(false);
    }

    function schedule(delay: number) {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(tick, delay);
    }
  }, [path, intervalMs]);

  useEffect(() => {
    alive.current = true;
    tick();
    const onVisible = () => {
      // Coming back to a tab should refresh immediately rather than waiting out the interval — the
      // first thing an operator does on return is read the numbers.
      if (isVisible()) tick();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      alive.current = false;
      document.removeEventListener('visibilitychange', onVisible);
      if (timer.current) clearTimeout(timer.current);
    };
  }, [tick]);

  // Re-render once a second while holding a value, so the displayed age advances rather than
  // freezing at whatever it was when the last fetch landed.
  useEffect(() => {
    if (observedAt === null) return;
    const id = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [observedAt]);

  return {
    data,
    ageMs: observedAt === null ? null : Date.now() - observedAt,
    degraded,
    conflict,
    loading,
    error,
    stale,
    refresh: tick,
  };
}

/** Exponential, capped, and `Retry-After`-aware when the error carries one. */
export function backoffMs(failures: number, error?: unknown): number {
  const retryAfter = retryAfterSeconds(error);
  if (retryAfter !== null) return Math.min(retryAfter * 1000, BACKOFF_MAX_MS);
  return Math.min(BACKOFF_BASE_MS * 2 ** Math.max(0, failures - 1), BACKOFF_MAX_MS);
}

function retryAfterSeconds(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null;
  const value = (error as { retryAfter?: unknown }).retryAfter;
  const parsed = typeof value === 'string' ? Number(value) : value;
  return typeof parsed === 'number' && Number.isFinite(parsed) ? parsed : null;
}

/** "just now" / "12s ago" / "4m ago". `null` renders as "unknown", never as a time. */
export function formatAge(ageMs: number | null): string {
  if (ageMs === null) return 'unknown';
  const seconds = Math.floor(ageMs / 1000);
  if (seconds < 2) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}
