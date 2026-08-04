// 004 US1 (FR-032): the BFF's COMPLETE proxy surface. The browser's API key is injected server-side
// for these routes ONLY — any other gateway path/method is refused before the key is attached, so a
// foreign page can't ride the operator key to an arbitrary gateway route.
//
// Each entry is a method + a path pattern; `:param` matches exactly one path segment. This list is
// the single source of truth — adding a stage/view means adding its gateway call here, on purpose.
//
// 027 (T761, contracts/allowlist-delta.md): sections follow the TEN AREAS, replacing 021's loop
// vocabulary (data → training → models → serving → monitoring → retraining ⟲). Re-sectioning
// only — it moves no entry and grants no access. The loop is not gone from the console; it moved
// to /console/activity as a visualization, which is what it always was. Navigation is now by area
// of concern, and this file is grouped the way an operator would look for a route.

export type AllowEntry = { method: string; pattern: string };

export const ALLOWLIST: AllowEntry[] = [
  // datasets
  { method: 'GET', pattern: 'datasets' },
  { method: 'POST', pattern: 'datasets' }, // upload/register
  { method: 'GET', pattern: 'datasets/:name' }, // idempotency pre-check
  { method: 'GET', pattern: 'datasets/:name/:version' }, // 021: manifest inspect (025: the manifest no longer carries a presigned download_url)
  // 025 US3 (FR-355, closes 021 FR-215): byte download PROXIED by the gateway. Not a presigned URL —
  // that was signed against the internal store endpoint (garage:3900), unresolvable from a browser and
  // a leaked object-store capability. The BFF injects the key; no credential reaches the browser.
  { method: 'GET', pattern: 'datasets/:name/:version/download' },
  { method: 'POST', pattern: 'datasets/:name/:version/validate' }, // 014 US2: readiness report (gate vs warn)
  // training
  { method: 'POST', pattern: 'runs' }, // launch
  { method: 'GET', pattern: 'runs/:id' }, // 021: polled run detail/metrics (FR-221)
  { method: 'GET', pattern: 'runs/:id/events' }, // live run (SSE)
  { method: 'POST', pattern: 'studies' }, // 012: launch an HPO study
  { method: 'GET', pattern: 'studies/:id' }, // 012: poll study status + best trial
  // models
  { method: 'GET', pattern: 'models' }, // list models + serving version
  { method: 'GET', pattern: 'models/:name' }, // versions
  { method: 'POST', pattern: 'models/:name/promote' }, // promote (returns the 011 gate verdict)
  { method: 'POST', pattern: 'models/:name/evaluate' }, // 011 US1: score a version → log eval metric
  { method: 'POST', pattern: 'models/:name/compare' }, // 011 US3: offline champion-challenger
  // deployments + inference (the request verbs serve both areas)
  { method: 'GET', pattern: 'serving/state' }, // GPU/lease status (pill + LeaseView + panel gating, 008 US3)
  { method: 'GET', pattern: 'serving/llm/activation' }, // 023 US5: desired vs resident + activation state (T525)
  { method: 'GET', pattern: 'serving/tasks' }, // task discovery → one panel per task (009 US1)
  { method: 'POST', pattern: 'infer' }, // 021: LLM trace mode — returns registry_version + prediction_id + load_ms (FR-232/233)
  { method: 'POST', pattern: 'infer/stream' }, // streaming inference (SSE)
  { method: 'POST', pattern: 'vision/classify' }, // image classify
  { method: 'POST', pattern: 'embed' }, // embeddings (CPU, off-lease — 009 US2)
  { method: 'POST', pattern: 'transcribe' }, // ASR transcript (whisper.cpp lease tenant — 009 US3)
  { method: 'POST', pattern: 'predict' }, // tabular predict (CPU, off-lease — 009 US4)
  { method: 'POST', pattern: 'batch' }, // 014 US1: launch an offline batch-inference job (021: lives in serving)
  { method: 'GET', pattern: 'batch/:id' }, // 014 US1: poll batch status + result link
  // observability
  { method: 'POST', pattern: 'monitor/check' }, // drift check
  { method: 'GET', pattern: 'monitor' }, // 021: drift-report history (FR-238)
  { method: 'POST', pattern: 'monitor/quality/check' }, // 021: output-quality check (FR-238)
  { method: 'GET', pattern: 'monitor/quality' }, // 021: quality-report history (FR-238)
  { method: 'POST', pattern: 'monitor/labels' }, // 021: attach ground-truth label by prediction id (FR-239)
  // evaluations — per-model retraining policies (018 US3, FR-179/180)
  { method: 'GET', pattern: 'policies' },
  { method: 'GET', pattern: 'policies/:model' },
  { method: 'PUT', pattern: 'policies/:model' }, // declare/update (validated write, structured 400)
  { method: 'DELETE', pattern: 'policies/:model' },
  { method: 'GET', pattern: 'policies/:model/status' }, // last check / next due / pending retrain
  // evaluations — promotion suggestions (018 US3, FR-183)
  { method: 'GET', pattern: 'suggestions' },
  { method: 'POST', pattern: 'suggestions/:id/accept' }, // routes through the gated promote
  { method: 'POST', pattern: 'suggestions/:id/dismiss' },
  // observability — health probes (+ the smoke probe)
  { method: 'GET', pattern: 'platform/health' },
  { method: 'GET', pattern: 'platform/events' }, // live state (SSE — also feeds the loop-nav badges)
  { method: 'GET', pattern: 'serving/health' }, // 021: per-engine probe dots (FR-249)
  { method: 'GET', pattern: 'predict/health' },
  { method: 'GET', pattern: 'vision/health' },
  { method: 'GET', pattern: 'embed/health' },
  { method: 'GET', pattern: 'transcribe/health' },
  { method: 'GET', pattern: 'training/health' },
  // administration — the broker (026), owner-only surfaces. The BFF injects the OPERATOR key, which is what
  // `require_owner` accepts; a tenant key never reaches these routes and could not raise its own
  // quota through them. Tenant-facing /v1/* is deliberately ABSENT: tenants hold their own keys and
  // call the gateway directly over TLS, so proxying them through the console's operator credential
  // would let any browser session spend any tenant's quota.
  { method: 'GET', pattern: 'admin/queue' }, // resident set + both VRAM bounds + both lanes (T656)
  { method: 'GET', pattern: 'admin/usage' }, // per-tenant consumption + ledger + reconciliation
  { method: 'GET', pattern: 'admin/tenants' },
  { method: 'POST', pattern: 'admin/tenants' }, // create tenant + first key (raw key shown once)
  { method: 'POST', pattern: 'admin/tenants/:id/keys' }, // rotate/add
  { method: 'POST', pattern: 'admin/tenants/:id/revoke' },
  { method: 'POST', pattern: 'admin/tenants/:id/enable' },
  { method: 'PUT', pattern: 'admin/tenants/:id/quota' },
  { method: 'POST', pattern: 'admin/keys/:id/revoke' },
  { method: 'POST', pattern: 'admin/jobs/:id/:action' }, // owner override — never touches a running job
  // console read surface (027, contracts/allowlist-delta.md). Read-only by construction: every
  // entry below is a GET except `POST console/predictions/:id/payload`, which mutates nothing and
  // is a POST solely so the payload reference travels in a body rather than a URL (SC-192). A real
  // write here would be a control the console is not supposed to have.
  //
  // NO AGENT PATH APPEARS — the console never reaches :8100. The gateway is the only holder of
  // X-Agent-Key (023 US2, research R5), and the `runtime/*` entries below are the GATEWAY's proxy
  // routes, not the agent's own.
  { method: 'GET', pattern: 'console/health' }, // PlatformHealth incl. the resolved mode
  { method: 'GET', pattern: 'console/capabilities' }, // what to render vs omit (FR-433)
  { method: 'GET', pattern: 'console/summary' }, // the eight Overview cards (FR-371)
  { method: 'GET', pattern: 'console/attention' }, // severity-ranked issues (FR-373)
  { method: 'GET', pattern: 'console/activity' }, // normalized lifecycle timeline (FR-363)
  { method: 'GET', pattern: 'console/search' }, // composed resolver across six kinds (FR-368)
  { method: 'GET', pattern: 'console/catalog' }, // the five-system join (FR-383/384)
  { method: 'GET', pattern: 'console/catalog/:name/:version' }, // detail + lineage (FR-386/390)
  { method: 'GET', pattern: 'console/catalog/:name/:version/compatibility' }, // FR-387/388
  { method: 'GET', pattern: 'console/jobs' }, // gateway lane ⋈ agent table ⋈ tracking runs (FR-372)
  { method: 'GET', pattern: 'console/jobs/:id' }, // + timeline, resources, StateConflict (FR-391)
  { method: 'GET', pattern: 'console/runs' }, // run listing — net-new
  { method: 'GET', pattern: 'console/experiments' },
  { method: 'GET', pattern: 'console/studies/:id/trials' }, // recorded trials, not a live search (FR-396/397)
  // evaluations, gates, drift (US5)
  { method: 'GET', pattern: 'console/evaluations' }, // modality-native metrics, not coerced (FR-399)
  { method: 'GET', pattern: 'console/evaluations/:name/:version' }, // failure + its evidence (FR-400)
  { method: 'GET', pattern: 'console/gates' },
  { method: 'GET', pattern: 'console/compare' }, // six dimensions kept separate (FR-403)
  { method: 'GET', pattern: 'console/drift' }, // thresholds inline (FR-405)
  // inference (US6). The one write-SHAPED entry mutates nothing: it is a POST specifically so the
  // payload reference travels in the body rather than a URL, where it would reach logs, history,
  // and referrers (SC-192). The matcher keys on method, so this does not widen GET access.
  { method: 'GET', pattern: 'console/predictions' }, // from the gateway record, not traces (FR-407)
  { method: 'GET', pattern: 'console/predictions/:id' }, // PayloadPreview WITHOUT content (FR-408)
  { method: 'POST', pattern: 'console/predictions/:id/payload' }, // the explicit reveal (FR-409)
  { method: 'GET', pattern: 'console/captures' },
  { method: 'GET', pattern: 'console/review-queue' }, // prioritized, each item naming its signal
  { method: 'GET', pattern: 'console/traces' },
  { method: 'GET', pattern: 'console/traces/:id' }, // generic span tree, no token assumptions
  // deployments (US7)
  { method: 'GET', pattern: 'console/endpoints' }, // synthesized; desired vs resident separated
  { method: 'GET', pattern: 'console/endpoints/:id' },
  // datasets and artifacts (US8). Bytes still move ONLY through the existing proxied download
  // route above — these carry logical references, never a presigned or credentialed URL.
  { method: 'GET', pattern: 'console/datasets' },
  { method: 'GET', pattern: 'console/datasets/:name/:version' },
  { method: 'GET', pattern: 'console/artifacts' }, // four integrity states, never collapsed
  // observability and administration (US9)
  { method: 'GET', pattern: 'console/metrics/summary' },
  { method: 'GET', pattern: 'console/metrics/series' }, // range bounded server-side
  { method: 'GET', pattern: 'console/alerts' }, // rule state only — no delivery field exists
  { method: 'GET', pattern: 'console/dashboards' }, // embeddability resolved server-side
  { method: 'GET', pattern: 'console/admin/storage' },
  { method: 'GET', pattern: 'console/admin/database' }, // the ledger, read-only; never an apply
  { method: 'GET', pattern: 'console/admin/integrations' }, // host identities, not credentialed URLs
  { method: 'GET', pattern: 'console/admin/system' }, // whether a key is set, never the key
  { method: 'GET', pattern: 'runtime/hosts' }, // a list even with one host (FR-374/382)
  { method: 'GET', pattern: 'runtime/hosts/:host/devices' }, // per-device topology (FR-375)
  { method: 'GET', pattern: 'runtime/engines' }, // enriched EngineState (FR-376)
  { method: 'GET', pattern: 'runtime/admission' }, // decision history, not a queue (FR-377/378)
  { method: 'GET', pattern: 'runtime/journal' }, // paged/filtered journal (FR-380)
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
