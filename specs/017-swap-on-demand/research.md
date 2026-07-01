# Phase 0 Research: Swap-on-Demand (017)

Design resolved in a grilling session (2026-06-30). Decision / Rationale / Alternatives. One item needs an
operator confirmation (the constitution-wording question, D6).

## D1 — Preemptable = serving tenants only

- **Decision**: Only resident **serving** models (LLM/vision/ASR) are preemptable. A running
  **training/HPO/batch** job is **never** preempted — it keeps cooperative refuse-if-held.
- **Rationale**: Evicting an in-progress fine-tune destroys GPU work (and would need checkpoint/restart
  machinery). Served models are cheap to reload (~2.5s). This is the safety boundary that makes preemption
  acceptable.
- **Alternatives**: Preempt everything incl. training (rejected — wastes GPU time, large+risky);
  serving + queued-but-not-started (rejected — needs a job-state notion the lease doesn't track).

## D2 — Gateway-brokered orchestration via an `unload-now` endpoint

- **Decision**: The gateway orchestrates: resolve the holder (`/serving/state`) → call the holder's
  **`unload-now`** endpoint → wait for the lease to free → forward the request to the target daemon.
- **Rationale**: The cost-stating confirm is a UI action → the gateway is the natural entry point; it
  already knows the holder and is a control-plane proxy. `unload-now` is a *control* command (not
  lease-holding), so the gateway can issue it without violating 008's "gateway never holds the lease."
  Daemons stay simple (one endpoint each).
- **Alternatives**: Daemon-to-daemon preemption (rejected — couples every serving daemon to every other);
  lease-poll flag (rejected — polling latency + a new lease state machine on the 008 primitive).

## D3 — In-flight handling = drain with bounded timeout

- **Decision**: On `unload-now` the holder finishes its current in-flight request(s), then unloads +
  releases; a request exceeding a bounded timeout is hard-cut.
- **Rationale**: Graceful is what makes preemption safe to expose — the common case drops no work; the
  timeout prevents a stuck request from hanging the swap.
- **Alternatives**: Hard unload immediately (rejected — drops in-flight work, surfaces errors to mid-request
  callers).

## D4 — Per-request `preempt` opt-in, default OFF

- **Decision**: A serving request MAY set **`preempt=true`** (default false). Default preserves 008
  refuse-if-held **everywhere** (API/CLI/UI). The capability is uniform; the UI sets the flag after confirm.
- **Rationale**: Preemption must never be implicit or surprising — no caller should suddenly start evicting
  models. Explicit opt-in keeps the safe default and makes the feature API-uniform.
- **Alternatives**: UI-only swap (rejected — narrower; special-cases the UI; API/CLI can't opt in).

## D5 — UX = cost-stating "Swap & classify" confirm (consequence of D4)

- **Decision**: The Infer tab's 008/A1 "classify disabled — GPU busy" becomes a **"Swap & classify"** action
  that states the cost ("evicts the resident LLM, ~2.5s reload") and, on confirm, sends `preempt=true`.
- **Rationale**: Turns the A1 dead-end into a one-click, cost-aware swap; the confirm is also the thrash
  guard (no automatic preemption).
- **Alternatives**: Auto-swap with no confirm (rejected — surprising + thrash-prone).

## D6 — Constitution wording (NEEDS OPERATOR CONFIRMATION)

- **Question**: 008's v1.4.0 amendment text describes the lease as cooperative with **"no swap/evict"**. 017
  adds operator-confirmed *preemptive* swap for serving. **Principle II's one-tenant rule is unchanged**
  (still one model in VRAM; the swap is sequential), so this is **not a rule change** — but the descriptive
  line is now stale.
- **Recommendation**: A **one-line v1.4.x wording refresh** noting that operator-confirmed preemption is
  allowed for *serving* tenants (training never preempted), the one-tenant rule unchanged. **Confirm with
  the operator** whether to refresh the text or leave 008's line as historical. Either way, no principle
  changes.

## Open items (plan/tasks, non-blocking)

- Drain-timeout default; supervisor "in-flight" detection (an active-request counter vs a llama-server
  health probe).
- Whether `unload-now` is authenticated like other control routes (it should be — it's destructive).
- The gateway wait-for-free mechanism (poll `current_holder()` until None/changed vs a short bounded wait).
