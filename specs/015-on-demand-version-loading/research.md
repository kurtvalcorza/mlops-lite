# Phase 0 Research: Score-at-Registration (015)

The design was resolved in a grilling session (2026-06-30). Each decision below is Decision / Rationale /
Alternatives considered. No blocking `NEEDS CLARIFICATION` remain.

## D1 — Trainer-side scoring runs in-process (not via a serving daemon)

- **Decision**: The HPO objective and any trainer-side eval score the just-trained model **in the trainer
  subprocess** through the eval harness's injectable `predict_fn` seam, not by asking a serving daemon to
  load the version.
- **Rationale**: The trainer already holds the model in memory; the eval harness already supports
  `predict_fn` injection (offline tests use it). In-process scoring sidesteps SC-068 **and** finding #4
  (the native trainer can't resolve Docker hostnames like `host.docker.internal:8092`) for the hot path.
- **Alternatives**: Daemon per-version loading (rejected — heavy multi-daemon machinery to re-load a model
  that's already in memory, and it still needs the trainer-URL fix).

## D2 — Score-at-registration for ALL fine-tunes (not just HPO)

- **Decision**: Every fine-tune (LLM/vision/embeddings/ASR) scores its model on its modality's held-out
  benchmark and **logs the metric against the new registry version at registration**.
- **Rationale**: Makes every downstream consumer correct by construction — gate, `compare`, quality, and
  the HPO objective just read logged metrics. The "unevaluated incumbent" case (fixed in PR #20) becomes
  rare. One coherent pipeline: every version born with its metric.
- **Alternatives**: HPO-only scoring (rejected — leaves regular fine-tunes unscored, so the gateway eval
  path would still need per-version loading or promote-first).

## D3 — Gateway standalone `/evaluate` + `/compare`: guard, don't load

- **Decision**: Build **no** per-version daemon-loading. `compare`/gate/quality read logged metrics. The
  gateway `/evaluate` scores the `@serving` model; for a *different* version with no logged metric it
  returns a **clear error** (serve/promote it, or it was scored at registration).
- **Rationale**: With D2, versions carry metrics, so the heavy machinery is redundant; the guard closes the
  SC-068 mislabel (silently scoring the resident model) with minimal surface.
- **Alternatives**: Full per-version daemon loading (rejected — largely redundant after D2; large blast
  radius across llama/whisper/bento).

## D4 — Modality scope: all four trainable (LLM, vision, embeddings, ASR)

- **Decision**: All four score at registration. 015 therefore **ships the embeddings (recall@k) and ASR
  (WER) held-out benchmark fixtures and finalizes those metrics** (today 011 "guidance stubs"). Tabular has
  no fine-tune flow → out of scope.
- **Rationale**: Operator chose comprehensive coverage; the fine-tune flows for all four already exist
  (010), so the marginal work is the scorer + fixture per modality.
- **Alternatives**: LLM + vision only (the committed 011 modalities) — rejected in favor of full coverage.

## D5 — LLM scores the SERVED GGUF (transient llama.cpp)

- **Decision**: Convert the adapter→GGUF (already done at registration), then load base+adapter in a
  **transient llama.cpp** and score over the QA benchmark — the exact quantized artifact that gets served.
- **Rationale**: The gate should measure what's actually served (Q4_K_M GGUF), not the unquantized HF
  weights. The GGUF already exists at registration, so it's load+score+free.
- **Alternatives**: In-memory HF `transformers.generate` (rejected — measures unquantized weights; small
  but real fidelity gap vs the served artifact).
- **Open (non-blocking, for tasks)**: `llama-cli` one-shot per prompt vs a short-lived `llama-server`;
  loading base GGUF + the LoRA-GGUF adapter via `--lora` for scoring.

## D6 — ASR mirrors the LLM (served ggml via whisper.cpp); vision + embeddings in-memory

- **Decision**: ASR scores the served **ggml via a transient whisper.cpp** (same served-artifact fidelity
  as D5). Vision and embeddings score the **in-memory** torch / sentence-transformers model.
- **Rationale**: ASR is the LLM's twin (HF-trained, served as a converted ggml) → same quantization gap →
  same served-artifact choice. Vision (checkpoint) and embeddings (ST model) are served as the in-memory
  artifact — no gap, so in-memory scoring is both faithful and simplest.
- **Alternatives**: In-memory HF Whisper for ASR (rejected — inconsistent with D5; measures unquantized
  weights).

## D7 — Scoring runs within the fine-tune's existing lease hold; no constitution amendment

- **Decision**: train → **free training model** → load served artifact (LLM/ASR) or reuse in-memory model
  (vision/embeddings) → score → release lease, **once**. One model in VRAM at any instant. **No
  constitution amendment.** Batch (014) is **out of SC-068 scope** (scoring `@serving` is correct for it).
- **Rationale**: Keeping scoring inside the existing hold preserves Principle II (sequential, one resident)
  and avoids a window where another tenant grabs the GPU mid-pipeline and stalls scoring. It adds no
  always-on service and changes no top-level stage, so v1.4.0 needs no change.
- **Alternatives**: Release + re-acquire for scoring (rejected — opens a GPU-contention window and
  complicates the train→score handoff).
- **Edge (for tasks)**: a fine-tune whose training succeeds but scoring fails should **register the version
  and warn** (the gate's missing-metric policy, PR #20, then applies) rather than fail the whole run.
