// 004 US1 (FR-032): the BFF's COMPLETE proxy surface. The browser's API key is injected server-side
// for these routes ONLY — any other gateway path/method is refused before the key is attached, so a
// foreign page can't ride the operator key to an arbitrary gateway route.
//
// Each entry is a method + a path pattern; `:param` matches exactly one path segment. This list is
// the single source of truth — adding a stage/view means adding its gateway call here, on purpose.
//
// 021 (FR-251/252): sections follow the loop vocabulary — data → training → models → serving →
// monitoring → retraining ⟲ (+ off-axis health). The 021 delta is 13 additions, all to endpoints
// that already exist (contracts/allowlist-delta.md); everything else is comment re-sectioning only.

export type AllowEntry = { method: string; pattern: string };

export const ALLOWLIST: AllowEntry[] = [
  // data stage
  { method: 'GET', pattern: 'datasets' },
  { method: 'POST', pattern: 'datasets' }, // upload/register
  { method: 'GET', pattern: 'datasets/:name' }, // idempotency pre-check
  { method: 'GET', pattern: 'datasets/:name/:version' }, // 021: manifest inspect (FR-215 — no byte download; the download_url presigns the internal store)
  { method: 'POST', pattern: 'datasets/:name/:version/validate' }, // 014 US2: readiness report (gate vs warn)
  // training stage
  { method: 'POST', pattern: 'runs' }, // launch
  { method: 'GET', pattern: 'runs/:id' }, // 021: polled run detail/metrics (FR-221)
  { method: 'GET', pattern: 'runs/:id/events' }, // live run (SSE)
  { method: 'POST', pattern: 'studies' }, // 012: launch an HPO study
  { method: 'GET', pattern: 'studies/:id' }, // 012: poll study status + best trial
  // models stage
  { method: 'GET', pattern: 'models' }, // list models + serving version
  { method: 'GET', pattern: 'models/:name' }, // versions
  { method: 'POST', pattern: 'models/:name/promote' }, // promote (returns the 011 gate verdict)
  { method: 'POST', pattern: 'models/:name/evaluate' }, // 011 US1: score a version → log eval metric
  { method: 'POST', pattern: 'models/:name/compare' }, // 011 US3: offline champion-challenger
  // serving stage
  { method: 'GET', pattern: 'serving/state' }, // GPU/lease status (pill + LeaseView + panel gating, 008 US3)
  { method: 'GET', pattern: 'serving/tasks' }, // task discovery → one panel per task (009 US1)
  { method: 'POST', pattern: 'infer' }, // 021: LLM trace mode — returns registry_version + prediction_id + load_ms (FR-232/233)
  { method: 'POST', pattern: 'infer/stream' }, // streaming inference (SSE)
  { method: 'POST', pattern: 'vision/classify' }, // image classify
  { method: 'POST', pattern: 'embed' }, // embeddings (CPU, off-lease — 009 US2)
  { method: 'POST', pattern: 'transcribe' }, // ASR transcript (whisper.cpp lease tenant — 009 US3)
  { method: 'POST', pattern: 'predict' }, // tabular predict (CPU, off-lease — 009 US4)
  { method: 'POST', pattern: 'batch' }, // 014 US1: launch an offline batch-inference job (021: lives in serving)
  { method: 'GET', pattern: 'batch/:id' }, // 014 US1: poll batch status + result link
  // monitoring stage
  { method: 'POST', pattern: 'monitor/check' }, // drift check
  { method: 'GET', pattern: 'monitor' }, // 021: drift-report history (FR-238)
  { method: 'POST', pattern: 'monitor/quality/check' }, // 021: output-quality check (FR-238)
  { method: 'GET', pattern: 'monitor/quality' }, // 021: quality-report history (FR-238)
  { method: 'POST', pattern: 'monitor/labels' }, // 021: attach ground-truth label by prediction id (FR-239)
  // retraining stage — per-model policies (018 US3, FR-179/180)
  { method: 'GET', pattern: 'policies' },
  { method: 'GET', pattern: 'policies/:model' },
  { method: 'PUT', pattern: 'policies/:model' }, // declare/update (validated write, structured 400)
  { method: 'DELETE', pattern: 'policies/:model' },
  { method: 'GET', pattern: 'policies/:model/status' }, // last check / next due / pending retrain
  // retraining stage — promotion suggestions (018 US3, FR-183)
  { method: 'GET', pattern: 'suggestions' },
  { method: 'POST', pattern: 'suggestions/:id/accept' }, // routes through the gated promote
  { method: 'POST', pattern: 'suggestions/:id/dismiss' },
  // health (+ smoke probe)
  { method: 'GET', pattern: 'platform/health' },
  { method: 'GET', pattern: 'platform/events' }, // live state (SSE — also feeds the loop-nav badges)
  { method: 'GET', pattern: 'serving/health' }, // 021: per-engine probe dots (FR-249)
  { method: 'GET', pattern: 'predict/health' },
  { method: 'GET', pattern: 'vision/health' },
  { method: 'GET', pattern: 'embed/health' },
  { method: 'GET', pattern: 'transcribe/health' },
  { method: 'GET', pattern: 'training/health' },
];

/** True if `method` + `segments` (the path after /api/gw/) match an allowlist entry. */
export function isAllowed(method: string, segments: string[]): boolean {
  const m = method.toUpperCase();
  return ALLOWLIST.some((e) => {
    if (e.method !== m) return false;
    const pat = e.pattern.split('/');
    if (pat.length !== segments.length) return false;
    return pat.every((p, i) => p.startsWith(':') || p === segments[i]);
  });
}
