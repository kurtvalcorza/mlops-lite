# Contract: loop nav, routes, chrome & hand-offs

Defines the shell contract: the ordered loop, the route map, the off-axis chrome, per-stage live
badges, and the cross-stage deep-link hand-offs. This is the observable UI contract 021 must satisfy
(the quickstart validates it).

## The loop bar (FR-208/209/212)

Rendered order, left→right, with directional connectors and a loop-back marker after the last stage
returning to the first:

```
data → training → models → serving → monitoring → retraining ⟲        [ GPU ● <holder>/<resident> · <swap|idle> ]   health
```

- The six stages are the loop axis (in this exact order).
- `health` and the GPU pill are **off-axis** (right-aligned), not part of the ordered loop.
- Default landing route is `serving` (`app/page.tsx` redirects `/` → `/serving`).

## Route map (rename + add)

| Loop stage | Route | From (today) |
|---|---|---|
| data | `/data` | `/datasets` (rename) |
| training | `/training` | `/runs` (rename) |
| models | `/models` | `/models` (unchanged path) |
| serving | `/serving` | `/infer` (rename) |
| monitoring | `/monitoring` | `/monitor` (rename) |
| retraining | `/retraining` | — (new) |
| health | `/health` | `/health` (unchanged) |

Renamed routes SHOULD preserve deep-link stability where cheap (e.g. redirect the old path to the new
one) so bookmarks/telemetry do not hard-break; not a hard requirement (single operator).

## Per-stage live badge (FR-210) — signal per stage

| Stage | Badge signal | Source |
|---|---|---|
| data | (none required; optional latest-version tick) | `GET /datasets` |
| training | GPU-resident-training indicator | `GET /serving/state` (`holder == "training"`) |
| models | candidate-awaiting-promotion | `GET /models` + per-model `GET /models/:name` (N+1) |
| serving | resident engine name | `GET /serving/state` |
| monitoring | latest-breach dot | `GET /monitor` / `GET /monitor/quality` |
| retraining | open-suggestion count | `GET /suggestions?state=open` |
| (badge fallback) | `unknown`/at-rest when platform unreachable | — (FR-213) |

> **training badge limitation**: with no runs-list endpoint (the spec's documented backend gap,
> FR-223), the only derivable live training signal is the GPU lease holder — so the badge reflects
> *GPU-resident* training, not queued/CPU-side runs. `platform/events`/`runs/:id/events` can't feed it
> (the snapshot carries no run state; the run-SSE needs a run id the shell doesn't hold).
> **models badge** is an N+1 read (list, then per-model version check) — acceptable at single-operator
> scale, but not a single poll.

## GPU pill (FR-211)

- Always visible in the header.
- Shows: lease `holder` (`llm`/`vision`/`asr`/`training`/`null` — since 018/T364 the holder is the
  admission tenant, ASR included; a `kind="job"` holder such as batch/retrain surfaces as its job
  label), `resident` model name, swap/idle state.
- Click → opens `/serving` (the full LeaseView).
- Source: `GET /serving/state` + `platform/events`.

## Cross-stage hand-offs (deep-links; R7 — URL query params)

| Seam | From → To | Carries |
|---|---|---|
| data → training | "train on this version" | `dataset@version` prefill (FR-217) |
| training → models | "view registered version" on run completion | model name + version (FR-221) |
| serving → monitoring | "label this prediction" | `prediction_id` (FR-237/239) |
| monitoring → training | inline one-shot retrain launches a run | (retrain fires; run appears in training) |
| retraining → models | blocked-accept "review & override" | model name + candidate version (FR-247) |
| retraining → training | policy fires a retrain | (run appears in training) |
| GPU pill / badges → stage | click-through | the target stage route |

## High-trust friction (FR-250) — must interrupt with confirmation

| Action | Friction |
|---|---|
| promote override (models) | confirm dialog requiring a **typed reason** |
| preemptive swap (serving) | confirm dialog naming the **holder to evict** |
| enable auto-promote (retraining) | explicit **opt-in warning**; off by default |

## Invariants

- The nav renders exactly these six ordered stages + two off-axis surfaces; no stage step is
  unreachable (SC-135).
- The shell renders and navigates with the platform down (SC-142 / FR-213).
- No view issues a gateway call absent from [allowlist-delta.md](./allowlist-delta.md) (FR-251).
- The design language (monospace + `Panel`/`Badge`) is preserved; this is IA, not a reskin (FR-253).
