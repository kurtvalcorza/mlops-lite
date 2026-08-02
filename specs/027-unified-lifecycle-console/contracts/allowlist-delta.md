# Contract: BFF Proxy-Surface Delta (027)

**File**: `ui/lib/gw-allowlist.ts` · **Guard**: `ui/app/api/gw/[...path]/route.ts`

The allowlist is the console's **complete** browser-reachable proxy surface: the operator key is
injected server-side for these entries only, and any other path or method is refused *before* the key
is attached (004 US1, FR-032). 021 established the discipline that every added proxy route is a
deliberate, itemized, reviewable entry rather than a wildcard — 027 keeps it.

**Shape of the change**: 021's delta was 13 additions to endpoints that already existed. 027's is
larger because the increment is full-stack — most additions are routes this increment also creates.

---

## 1. Sectioning

Comment sections change from the 021 loop vocabulary (`data → training → models → serving →
monitoring → retraining ⟲ + health`) to the ten areas. **Re-sectioning only** — it moves no entry and
grants no new access.

---

## 2. Additions

All are `GET` unless noted. Each maps to [console-read-api.md](./console-read-api.md).

**Runtime** (US2)

```
runtime/hosts
runtime/hosts/:host/devices
runtime/engines
runtime/admission
runtime/journal
```

**Models** (US3)

```
console/catalog
console/catalog/:name/:version
console/catalog/:name/:version/compatibility
```

**Training** (US4)

```
console/jobs
console/jobs/:id
console/runs
console/experiments
console/studies/:id/trials
```

**Evaluations** (US5)

```
console/evaluations
console/evaluations/:id
console/gates
console/compare
console/drift
```

**Inference** (US6)

```
console/predictions
console/predictions/:id
console/captures
console/review-queue
console/traces
console/traces/:id
POST console/predictions/:id/payload      ← explicit payload reveal
```

**Deployments** (US7)

```
console/endpoints
console/endpoints/:id
```

**Datasets and artifacts** (US8)

```
console/datasets
console/datasets/:name/:version
console/artifacts
```

**Observability / Administration** (US9)

```
console/metrics/summary
console/metrics/series
console/alerts
console/dashboards
console/admin/storage
console/admin/database
console/admin/integrations
console/admin/system
```

**Shell** (US1/US10)

```
console/health
console/capabilities
console/summary
console/search
console/activity
console/attention
```

`console/summary` was added during implementation: the eight Overview cards need the
null-is-not-zero rule enforced in one place, and that place is the server (see
[console-read-api.md](./console-read-api.md)).

---

## 3. Retained

Every existing entry stays. The 021 allowlist covers datasets (including 025's proxied byte
download), models, promotion, evaluate/compare, serving state and activation, all five inference
verbs, batch, monitor read and write, policies, suggestions, and the health probes — all still used
by the ten-area console, just from different pages.

**Nothing is removed in 027.** MVP 2 (US11) is where the write surface is reconsidered; pruning the
existing write entries now would break the inference and monitoring pages this increment ships.

---

## 4. Rules the delta must satisfy

1. **`POST console/predictions/:id/payload` is the only new write-shaped entry**, and it mutates
   nothing — it is a `POST` specifically so the payload reference travels in the body rather than a
   URL (SC-192). The allowlist matcher keys on method, so this does not widen `GET` access.
2. **No agent path appears here.** The console never reaches `:8100`; runtime data arrives through
   the gateway's `runtime/*` projections (research R5). An entry pointing at the agent would either
   leak `X-Agent-Key` toward the browser or need a second key-injecting proxy.
3. **No wildcards.** `:param` matches exactly one segment; the matcher requires equal segment counts.
   A `console/*` catch-all would silently grant every future route, defeating the allowlist's purpose.
4. **Artifact and dataset bytes stay on the existing proxied download route.** No new object-store
   path is added and no credential reaches the browser (FR-421/422).
5. **Every addition is exercised by a test.** `tests/test_ui_security.py` asserts that a
   non-allowlisted path is refused *without* the key being attached; the additions extend the same
   suite rather than relaxing it.
