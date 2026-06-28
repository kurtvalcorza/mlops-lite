# Implementation Plan: Model Evaluation & Validation Gates

**Branch**: `011-evaluation-gates` | **Date**: 2026-06-28 | **Spec**: [spec.md](./spec.md)

**Status**: **DRAFT — GRILLED (2026-06-28), build-ready**

> **Grilled decisions (2026-06-28):** (1) **Per-modality metrics = sensible defaults + small bundled
> held-out fixtures, configurable** — vision = top-1 accuracy (higher-better), ASR = WER (lower-better,
> `jiwer`), embeddings = recall@k (higher-better), tabular = AUC (higher-better), LLM = task-accuracy on a
> small QA held-out set (higher-better) + perplexity universal fallback (lower-better); LLM + vision
> committed, ASR/embeddings/tabular guidance stubs until served (009). (2) **Default gate = hard-gate with a
> small regression tolerance + explicit override**, configurable down to warn-only. (3)
> **Champion-challenger = held-out set (default); shadow-replay DEFERRED** to a 013-dependent follow-on
> (needs 013's prediction + label logging). Firm: single `registry.promote` choke-point (FR-105),
> offline-only (Principle II), light libs (FR-102), advances VI / hardens IV, **no amendment**.

**Input**: Feature specification from `specs/011-evaluation-gates/spec.md` (the keystone of the
MLOps-maturity layer — eval harness + gated promotion + offline champion-challenger)

## Summary

Add the **evaluation + validation gate** that makes promotion responsible: (US1) an **offline eval
harness** that scores a model version on a held-out, modality-specific benchmark and **logs the primary
metric to MLflow** with benchmark provenance; (US2) a **promotion gate** wired into the existing
`registry.promote` alias path that compares a candidate's metric against the serving incumbent's and **by
default hard-gates with a small tolerance** (refuse a regression, explicit override to bypass; configurable
down to warn), surfacing the verdict in the Models/Runs UI; (US3) an **offline champion-challenger**
comparison on the **held-out benchmark** (shadow-replay deferred to a 013-dependent follow-on) that honours
one-model-in-VRAM by scoring sequentially. Uses the **existing MLflow + gateway** —
new code + a few **light** metric libraries, no new service or runtime. Phase-gated like
002/004/005/006/007, validated against the full 001–007 suite, never regressing serving or the VRAM mutex.

## Technical Context

**Language/Version**: Python 3.12 (gateway, post-007), unchanged. No new language or runtime. The UI
surfacing (US2/US3) is Next 15.x (post-007), unchanged.

**Primary Dependencies (light metric libs — Principle III)**: candidate adds — `jiwer` (WER, ASR);
`sacrebleu` and/or `rouge-score` (text generation); `scikit-learn` metrics (accuracy / top-1 / AUC /
recall@k — sklearn is small and likely already transitively present). **Committed defaults (grilled):**
LLM = task-accuracy on a small QA held-out set (higher-better) + perplexity universal fallback
(lower-better); vision = top-1 accuracy (higher-better); ASR = WER (lower-better); embeddings = recall@k
(higher-better); tabular = AUC (higher-better) — all configurable; LLM + vision committed, the rest
guidance stubs until served (009). No heavy eval framework (no full lm-eval-harness), no new
datastore/broker, no always-on worker. MLflow (existing, 3.x post-007) is the metric/registry home.

**Storage**: eval metrics + benchmark identifiers log to **MLflow** (tracking, against the model version /
its run) — the existing backend, no new store. Held-out benchmark datasets reuse the existing dataset
registry surface (content-addressed on MinIO) where practical, so a benchmark carries a name + version/hash
for provenance (FR-101). Drift reports / results stay where 005 put them.

**Target Platform**: Win11 + WSL2 + Rancher Desktop. The gateway + MLflow run in Docker; GPU model loads
(serving LLM, vision) happen on the native WSL GPU host under the existing one-model-in-VRAM lease. The
harness invokes the same serving path, so champion-challenger inherits the VRAM mutex for free.

**Project Type**: a new lifecycle-side capability (evaluation + gate) over the hardened, refreshed platform
(002/004/005/006/007) — touches `gateway/app/registry.py` (gate in the promote path), a new
`gateway/app/evaluation.py` (harness + metrics + champion-challenger), the promote API route + its
response, `gateway/requirements.txt` (light metric libs), the Models/Runs UI (surface metric + verdict),
and tests. The eval harness may also expose a one-shot `scripts/` entry for batch runs.

**Performance Goals**: none targeted on the request path — the gate is a fast MLflow metric lookup +
comparison (no model load) at promote time; the harness/champion-challenger are explicit batch operations,
not inline with `/infer`. The gate MUST NOT add a model load to the promotion call.

**Constraints**: one model in VRAM (NON-NEGOTIABLE) → offline-only champion-challenger, sequential loads,
no online A/B; light footprint (small metric libs, no resident worker); every promotion gated (no
back-door); metric direction honoured; like-for-like comparison; benchmark provenance recorded.

## Constitution Check

*GATE: Must pass before design. Re-check after.*

| Principle | Gate | Status |
|---|---|---|
| I. Local-First, Single-Machine | Eval + gate run on-host against local MLflow; nothing leaves the machine | ✅ |
| II. Single-GPU On-Demand (NON-NEGOTIABLE) | **No online A/B** — champion-challenger is offline, sequential loads; the gate itself loads no model | ✅ honoured by design |
| III. Lightweight Footprint | Small metric libs (`jiwer`/`sacrebleu`/`rouge`/sklearn), no heavy framework, no always-on worker | ✅ |
| IV. Full Lifecycle Coverage | **Hardens** the registry→serving boundary with a validation gate; no stage dropped or added wholesale | ✅ strengthened |
| V. OSS & Swappable | Light OSS metric libs behind a harness interface; MLflow stays the metric/registry home; libs swappable per modality | ✅ |
| VI. Reproducibility & Observability | **Advanced** — every model version gains a tracked, benchmark-attributed eval metric ("if it isn't tracked, it didn't happen") | ✅ advanced |
| VII. Phase-Gated Delivery | Three independently-runnable stories (US1 harness → US2 gate → US3 champion-challenger), each verifiable on hardware | ✅ |
| Workflow: "no new runtime without amendment" | None introduced (Python/MLflow/Node all pre-existing; metric libs are pip deps) | ✅ no amendment |

**No amendment required.** 011 advances Principle VI (tracked eval per version) and hardens Principle IV
(gated promotion) using existing components; the offline-only champion-challenger keeps Principle II
untouched, and the light metric libs respect Principle III. Clean gate-check, mirroring 005/006/007.

## Project Structure

### Source Code (delta over 007)

```text
mlops-lite/
├── gateway/
│   ├── app/
│   │   ├── evaluation.py          # NEW: offline eval harness (per-modality primary metric), MLflow
│   │   │                          #      metric logging w/ benchmark provenance, gate-verdict compute,
│   │   │                          #      champion-challenger (held-out; shadow-replay deferred → 013) — mirrors
│   │   │                          #      monitoring.py style (dep-light, MinIO/MLflow reuse)
│   │   ├── registry.py            # MODIFIED: promote() consults the gate before set_..._alias; returns
│   │   │                          #           the verdict (pass/warn/blocked + metrics + delta)
│   │   └── main.py (routes)       # MODIFIED: promote route returns the verdict; new eval / compare routes
│   ├── requirements.txt           # MODIFIED: add light metric libs (jiwer / sacrebleu|rouge / sklearn)
│   └── ...
├── ui/                            # MODIFIED: Models/Runs tab shows eval metric + gate verdict (US2/US3)
│   └── (Models/Runs surfaces)     #           champion-challenger comparison surfaced
├── scripts/
│   └── eval_model.py              # NEW (optional): one-shot harness entry for batch eval / seeding
├── benchmarks/                    # NEW: small held-out benchmark fixtures per modality (name + version)
│   └── (llm / vision / ...)       #      content-addressed / hashed for provenance (FR-101)
└── tests/                         # NEW: test_eval_harness, test_promotion_gate, test_champion_challenger
                                   #      + a no-regression sweep over 001–007
```

**Structure Decision**: concentrate the new logic in `gateway/app/evaluation.py` (harness + metrics + gate
math + champion-challenger) and keep `registry.py`'s change **minimal** — `promote()` calls into the gate
and returns its verdict, so the alias move stays the single choke-point (FR-105, no back-door). Mirror
`monitoring.py`'s dependency-light, MinIO/MLflow-reusing style. Benchmarks are small versioned fixtures so a
score is reproducible (FR-101).

## Phasing (maps to constitution VII)

- **Phase 0 — Pre-flight**: confirm the light metric libs (`jiwer`, `sacrebleu`/`rouge`, sklearn) resolve
  clean in the gateway image and stay light (no heavy transitive pull); commit the **grilled per-modality
  primary metric + benchmark fixture** for the two *served* modalities (**LLM = task-accuracy/perplexity
  fallback, vision = top-1 accuracy**) — the others (ASR=WER / embeddings=recall@k / tabular=AUC) get
  guidance-stub defaults now, implementation when those serving paths exist (009).
- **Phase 1 — Offline eval harness (US1)**: build `evaluation.py` harness — per-modality primary metric,
  MLflow metric logging with benchmark provenance, reproducible re-run. Exit: SC-064.
- **Phase 2 — Gated promotion (US2)**: wire the gate into `registry.promote` (compare candidate vs
  incumbent, honour metric direction + like-for-like; **default hard-gate with tolerance + explicit
  override**, configurable to warn; missing-metric + no-incumbent policies); return the verdict (candidate
  vs incumbent metric + delta + tolerance + mode) from the promote API; surface metric + verdict in the
  Models/Runs UI. Exit: SC-065 + SC-066 + SC-067.
- **Phase 3 — Champion-challenger (US3)**: offline comparison — held-out scoring of champion vs challenger
  (sequential VRAM loads); per-metric winner; surface it. (Shadow-replay deferred → 013.) Exit: SC-068.
- **Phase 4 — Cross-cutting regression**: full 001–007 no-regression sweep; confirm the gateway image stays
  light, no always-on service added, and the VRAM mutex is never violated by champion-challenger. Exit:
  SC-069.

Cross-cutting: the gate adds **no model load** to the promote call (metric lookup only); champion-challenger
loads models **sequentially** and never holds two in VRAM.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Gate in the **single** `registry.promote` choke-point | Guarantees *every* alias move is gated — no ungated back-door (the DIMER-by-omission gap) | A separate "gated-promote" endpoint alongside the raw one leaves the raw alias move as an ungated bypass — defeats the purpose |
| **Offline** champion-challenger (held-out, sequential; shadow-replay deferred → 013) | Principle II forbids two models in VRAM, so online A/B is impossible by construction; held-out is reproducible with no traffic/label dependency | Online A/B / live traffic-split violates one-model-in-VRAM; shadow-replay needs 013's prediction+label logging to be meaningful, so it is deferred |
| **Light** metric libs (jiwer/sacrebleu/rouge/sklearn), no framework | Principle III (the gateway image lives on the scarce C: drive); a comparable signal needs only small libs | A full eval framework (lm-eval-harness, big suites) is heavyweight + disk-hungry for no 011 benefit |
| Metric **direction** + **like-for-like** in the gate | "Regression" is undefined without knowing higher/lower-is-better and comparing the same metric/modality | Treating every metric as higher-is-better silently inverts WER/perplexity verdicts — a correctness bug |
| **Default hard-gate + tolerance + override** (configurable to warn; missing-metric, no-incumbent policies) | Safe-by-default closes the DIMER no-gate gap without blocking on noise; an operator can still choose strictness and bypass deliberately | A default warn-only gate doesn't actually protect serving (silently promotes regressions); a fixed always-block gate can't bootstrap (no incumbent) and frustrates iteration — hence tolerance + override |
| Log eval metrics to **existing MLflow** | Principle VI home for tracked metrics; reuses the stack, keeps benchmark provenance with the version | A new eval datastore adds a service against Principle III for data MLflow already holds the right way |
| Benchmark **provenance** (name + version/hash) per EvalResult | A score is meaningless without its benchmark; reproducibility is Principle VI | Logging a bare number with no benchmark id makes scores non-comparable and non-reproducible |
