// 004 US1 (FR-032): the BFF's COMPLETE proxy surface. The browser's API key is injected server-side
// for these routes ONLY — any other gateway path/method is refused before the key is attached, so a
// foreign page can't ride the operator key to an arbitrary gateway route.
//
// Each entry is a method + a path pattern; `:param` matches exactly one path segment. This list is
// the single source of truth — adding a tab/route means adding its gateway call here, on purpose.

export type AllowEntry = { method: string; pattern: string };

export const ALLOWLIST: AllowEntry[] = [
  // Infer tab
  { method: 'GET', pattern: 'serving/state' }, // GPU/lease status line + classify gating (008 US3)
  { method: 'GET', pattern: 'serving/tasks' }, // task discovery → one panel per task (009 US1)
  { method: 'POST', pattern: 'infer/stream' }, // streaming inference (SSE)
  { method: 'POST', pattern: 'vision/classify' }, // image classify
  { method: 'POST', pattern: 'embed' }, // embeddings (CPU, off-lease — 009 US2)
  { method: 'POST', pattern: 'transcribe' }, // ASR transcript (whisper.cpp lease tenant — 009 US3)
  { method: 'POST', pattern: 'predict' }, // tabular predict (CPU, off-lease — 009 US4)
  // Models tab
  { method: 'GET', pattern: 'models' }, // list models + serving version
  { method: 'GET', pattern: 'models/:name' }, // versions
  { method: 'POST', pattern: 'models/:name/promote' }, // promote (returns the 011 gate verdict)
  { method: 'POST', pattern: 'models/:name/evaluate' }, // 011 US1: score a version → log eval metric
  { method: 'POST', pattern: 'models/:name/compare' }, // 011 US3: offline champion-challenger
  // Datasets tab
  { method: 'GET', pattern: 'datasets' },
  { method: 'POST', pattern: 'datasets' }, // upload
  { method: 'GET', pattern: 'datasets/:name' }, // idempotency pre-check
  { method: 'POST', pattern: 'datasets/:name/:version/validate' }, // 014 US2: dataset validation report
  // Runs tab
  { method: 'POST', pattern: 'runs' }, // launch
  { method: 'GET', pattern: 'runs/:id/events' }, // live run (SSE)
  { method: 'POST', pattern: 'studies' }, // 012: launch an HPO study
  { method: 'GET', pattern: 'studies/:id' }, // 012: poll study status + best trial
  { method: 'POST', pattern: 'batch' }, // 014 US1: launch an offline batch-inference job
  { method: 'GET', pattern: 'batch/:id' }, // 014 US1: poll batch status + result link
  // Monitor tab
  { method: 'POST', pattern: 'monitor/check' }, // drift check
  // Monitor tab — per-model policies (018 US3, FR-179/180)
  { method: 'GET', pattern: 'policies' },
  { method: 'GET', pattern: 'policies/:model' },
  { method: 'PUT', pattern: 'policies/:model' }, // declare/update (validated write, structured 400)
  { method: 'DELETE', pattern: 'policies/:model' },
  { method: 'GET', pattern: 'policies/:model/status' }, // last check / next due / pending retrain
  // Models tab — promotion suggestions (018 US3, FR-183)
  { method: 'GET', pattern: 'suggestions' },
  { method: 'POST', pattern: 'suggestions/:id/accept' }, // routes through the gated promote
  { method: 'POST', pattern: 'suggestions/:id/dismiss' },
  // Health tab (+ smoke probe)
  { method: 'GET', pattern: 'platform/health' },
  { method: 'GET', pattern: 'platform/events' }, // live state (SSE)
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
