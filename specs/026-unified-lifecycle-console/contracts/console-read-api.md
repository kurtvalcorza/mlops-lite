# Contract: Gateway Console Read API (026)

**Plane**: FastAPI gateway (`:8080`) · **Callers**: the console BFF · **Auth**: `X-API-Key`
(fail-closed, 005 US2) · **Home**: `gateway/app/routers/console.py` over `gateway/app/console/`.

These routes are **read-only projections** (see [data-model.md](../data-model.md)). They add no
persisted entity and require **no migration** (research R7). Every response carries observation
timestamps so the console can render data age (FR-430) and detect conflicts (FR-427).

**Envelope** — every response in this contract:

```json
{ "data": {}, "observed": { "<source>": "2026-07-31T09:14:02Z" }, "degraded": ["agent"], "conflict": null }
```

`degraded` names sources that could not be reached for **this** projection. A partially-degraded
projection returns `200` with the reachable parts populated and the unreachable parts `null` — it
does **not** fail whole (FR-428). A `null` under `degraded` means *unknown*, never *zero*.

---

## Runtime proxy

| Route | Projects |
|---|---|
| `GET /runtime/hosts` | agent reachability, device count, active engines/jobs, journal state, last heartbeat (FR-374) |
| `GET /runtime/hosts/{host}/devices` | agent `GET /runtime/devices` |
| `GET /runtime/engines` | enriched `EngineState` list (FR-376) |
| `GET /runtime/admission` | agent `GET /runtime/admission` (FR-377/378) |
| `GET /runtime/journal` | agent `GET /journal`, cursor and filters passed through (FR-380) |

The gateway is the **only** holder of `X-Agent-Key` (research R5). When the agent is unreachable
these return `200` with `data: null` and `degraded: ["agent"]` — **never** an empty list, which the
console would legitimately render as "no devices" (FR-428).

`GET /runtime/hosts` returns a list even though the reference deployment has one host (FR-382).

---

## Catalog and models

| Route | Notes |
|---|---|
| `GET /console/catalog` | Joined `PlatformModel[]` (FR-383/384). Filter by modality, alias, evaluation state, deployment state. Paged. |
| `GET /console/catalog/{name}/{version}` | Detail + lineage (FR-386/390). |
| `GET /console/catalog/{name}/{version}/compatibility` | `RuntimeCompatibility` (FR-387/388). |

Compatibility is computed **at request time** against live topology — it is a statement about *now*,
so it must not be cached beyond the device snapshot's own TTL. With the agent unreachable the verdict
is `unknown`, never `incompatible`: an unreachable agent is not a compatibility fact.

`artifactPresent` requires an actual object-store existence check (FR-384). Assuming presence from a
registry URI is how a console shows a download that 404s.

---

## Jobs, runs, studies

| Route | Notes |
|---|---|
| `GET /console/jobs` | Unified active-work list joining gateway jobs, agent jobs, tracking runs (FR-372). |
| `GET /console/jobs/{id}` | `PlatformJob` + timeline + resources + `StateConflict` (FR-391/393/394). |
| `GET /console/runs` | Run listing — **net-new**; only `GET /runs/{id}` existed. |
| `GET /console/experiments` | Experiment listing. |
| `GET /console/studies/{id}/trials` | Trial table + history + parameter importance (FR-396). |

Job logs reuse the existing `GET /runs/{id}/events` SSE stream. No new streaming surface is
introduced (research R10).

Study responses MUST NOT imply a persistent search service; trials are reported as recorded
executions (FR-397).

---

## Evaluations, gates, drift

| Route | Notes |
|---|---|
| `GET /console/evaluations` | FR-398/399 — modality-native metrics, not coerced. |
| `GET /console/evaluations/{id}` | Failing rule, threshold, observed value, comparison basis, override (FR-400/401). |
| `GET /console/gates` | Configured gates and rules. |
| `GET /console/compare` | Quality / latency / resources / artifacts / datasets / policy, separated (FR-403). |
| `GET /console/drift` | Reports **with `thresholds` inline** (FR-404/405). |

Drift thresholds ship in the payload so the interface never hard-codes the 0.10/0.25 convention. The
limitations text (FR-406) is a static property of the surface, not data.

---

## Inference

| Route | Notes |
|---|---|
| `GET /console/predictions` | Paged, from the gateway record — **not** reconstructed from traces (FR-407). |
| `GET /console/predictions/{id}` | Detail with `PayloadPreview` **and no payload content** (FR-408). |
| `POST /console/predictions/{id}/payload` | Explicit reveal. Id in the **body**, never the path. |
| `GET /console/captures` | Capture index with label state. |
| `GET /console/review-queue` | Prioritized (FR-411). |
| `GET /console/traces` · `GET /console/traces/{id}` | Normalized generic span tree (FR-412/413). |

**Why reveal is a `POST` with the id in the body**: `GET /…/{id}/payload` would place a payload
reference in a URL, and URLs reach logs, history, and referrers. SC-192 requires no payload value in
any address; making reveal a body-carrying call makes that structural rather than a convention
someone must remember.

Trace normalization happens server-side so FR-413's generic presentation is enforced in one place and
the client stays free of tracking-vendor payload shapes.

---

## Endpoints, health, search

| Route | Notes |
|---|---|
| `GET /console/endpoints` | Synthesized (research R7) — desired vs resident separated (FR-414/416). |
| `GET /console/endpoints/{id}` | Detail + traffic + `StateConflict`. |
| `GET /console/health` | `PlatformHealth` incl. resolved `mode` (FR-369/429). |
| `GET /console/capabilities` | Backend capability → feature flags (FR-433). |
| `GET /console/search?q=` | Composed resolver across models, runs, datasets, jobs, endpoints, predictions (FR-368). |
| `GET /console/activity` | Normalized lifecycle timeline (FR-363). |
| `GET /console/attention` | Severity-ranked issues (FR-373). |

`GET /console/capabilities` is what lets the interface **omit** an unsupported control rather than
render one that fails on use — it is the mechanism behind FR-418's ban on decorative rollout
controls. If the gateway does not implement traffic splitting, the capability is absent and the
control is never rendered.

`mode` is resolved from **reachability**, never from a configured string (research R14).

---

## Observability

| Route | Notes |
|---|---|
| `GET /console/metrics/summary` | Curated native panels (FR-423). |
| `GET /console/metrics/series` | Bounded range queries for charts. |
| `GET /console/alerts` | Rule state ∈ inactive, pending, firing, unknown (FR-424). |

`GET /console/alerts` MUST NOT include any notification/delivery field. There is no Alertmanager;
a delivery field would invite the console to claim something the platform cannot do (FR-424).

`GET /console/metrics/series` bounds the time range and step server-side, so a console panel cannot
issue an unbounded query against the metrics store.

---

## Cross-cutting rules

1. **Every projection names its sources and their observation times.** Data age (FR-430) and conflict
   detection (FR-427) are impossible without this, and retrofitting it is expensive.
2. **Degradation is per-projection, never global.** One unreachable source degrades the fields it
   owns; the rest of the response is still served.
3. **Paging is mandatory** on journal, predictions, captures, traces, and catalog — unbounded reads
   are how a local platform is knocked over by its own console.
4. **No route in this contract mutates anything.** MVP 2 (US11) owns writes and will add its own
   contract.
5. **Every route added here appears in [allowlist-delta.md](./allowlist-delta.md)** and in the
   exported OpenAPI (FR-438 / SC-203).
