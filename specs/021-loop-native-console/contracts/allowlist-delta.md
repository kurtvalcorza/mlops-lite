# Contract: BFF proxy allow-list delta (`ui/lib/gw-allowlist.ts`)

The allow-list is the console's security seam: the BFF (`ui/app/api/gw/[...path]/route.ts`) injects
the operator key **only** for `{method, pattern}` pairs present here, refusing any other gateway
path/method before the key is attached. Adding a stage/view therefore *requires* adding its gateway
call here, on purpose. This is the **only non-UI change** in 021 — the proxy route logic is
untouched.

`:param` matches exactly one path segment (as today).

## Additions (13 entries — all target endpoints ALREADY EXIST)

### data stage
| Method | Pattern | For |
|---|---|---|
| GET | `datasets/:name/:version` | version detail — **manifest inspect only**; the `download_url` is presigned against the internal store and is not browser-reachable, so byte download is deferred (FR-215) |

### serving stage
| Method | Pattern | For |
|---|---|---|
| POST | `infer` | LLM **trace mode** — single-shot inference returning `registry_version` + `prediction_id` + `load_ms`, and the path that logs the prediction + input capture feeding monitoring (FR-232/233) |

### training stage
| Method | Pattern | For |
|---|---|---|
| GET | `runs/:id` | polled run detail/metrics (only `runs/:id/events` SSE is allow-listed today) — FR-221 |

### monitoring stage (the biggest read-side gap)
| Method | Pattern | For |
|---|---|---|
| GET | `monitor` | drift-report history — FR-238 |
| POST | `monitor/quality/check` | output-quality check — FR-238 |
| GET | `monitor/quality` | quality-report history — FR-238 |
| POST | `monitor/labels` | attach ground-truth label by prediction id — FR-239 |

### health (per-engine probes)
| Method | Pattern | For |
|---|---|---|
| GET | `serving/health` | per-engine liveness dot — FR-249 |
| GET | `predict/health` | " |
| GET | `vision/health` | " |
| GET | `embed/health` | " |
| GET | `transcribe/health` | " |
| GET | `training/health` | " |

## Re-sectioning only (NO new entries)

The policy + suggestion routes are **already allow-listed** (currently commented under
"Monitor tab — per-model policies" and "Models tab — promotion suggestions"). 021 moves these under a
new `retraining` comment block. Behaviour identical; comments follow the loop vocabulary:

```
GET  policies            GET policies/:model      PUT policies/:model
DELETE policies/:model   GET policies/:model/status
GET  suggestions         POST suggestions/:id/accept   POST suggestions/:id/dismiss
```

Likewise the existing engine + serving-state entries are re-grouped under `serving`, and the
dataset/run/model entries under `data`/`training`/`models` — comment relabeling only.

## Explicitly NOT added (kept off the allow-list)

| Method | Pattern | Why |
|---|---|---|
| POST | `models` | interactive model register/upload is deferred to **feature 022** (BYOM) — FR-230 |
| GET | `runs` (list) | no such gateway endpoint exists — run history is a documented backend gap, out of scope |
| GET | `metrics`, `GET /` | machine/scrape endpoints, not UI surfaces |

## Invariant (must hold after the change)

- `isAllowed(method, segments)` returns **true** for every call any 021 view issues, and the set of
  entries is the exact union of {existing entries} ∪ {additions above}. No wildcard, no broadening
  beyond these patterns (FR-251). A view issuing a non-listed call must fail closed at the BFF, not
  reach the gateway.
