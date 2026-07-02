# Contract: Policy API + Scheduler + Promotion modes (US3)

All routes key-protected like every lifecycle route; BFF allowlist gains exactly these.

## CRUD (gateway)

- `GET /policies` → list; `GET /policies/{model}` → one.
- `PUT /policies/{model}` — create/update whole-document (single-operator; no PATCH). Validates
  per data-model.md §ModelPolicy; invalid → 400 `{errors:[{field, reason}]}` and nothing stored
  (FR-179). `promotion_mode` defaults `manual`.
- `DELETE /policies/{model}` → removes policy (never touches the model).
- `GET /policies/{model}/status` → last check at/result, next check due, pending retrain,
  open suggestion — the Monitor page's data source.

## Scheduler behavior (gateway lifespan task, R5)

Tick loop: for each enabled policy whose `check_interval_s` elapsed → run its monitors through
the existing check paths (`monitoring.compute_drift` / `quality.compute_quality`) → on breach:
`try_reserve_retrain` (shared cooldown, reserve-before-launch — FR-163 groundwork) → launch
retrain via the jobs surface with the policy's `modality` + resolved dataset (`latest` →
newest registered version at launch time, FR-181) → on 409/busy: park/refresh the
queue-of-one `PendingRetrain` with backoff (FR-182). Every tick and outcome increments
metrics (`gateway_policy_checks_total{result}`, `gateway_policy_retrains_total{signal}`) and
writes a check report — no silent ticks (FR-180).

## Post-run promotion evaluation

On a policy-launched run reaching `succeeded` with a registered version: read its
score-at-registration + `evaluation.gate` verdict; read the latest shadow verdict when one
exists (FR-183):

- `manual` → nothing (today's behavior, byte-for-byte).
- `suggest` → create PromotionSuggestion (`open`); surfaces on Models page; operator
  accept → existing gated `promote()` path (still runs the gate — suggestion never bypasses
  it); dismiss → recorded.
- `auto-on-green` → gate pass AND (shadow window absent OR challenger wins) → call the same
  gated `promote()`; write AuditRecord `actor=policy:<model>`. Gate warn/blocked → falls back
  to `suggest` behavior (auto never overrides a gate).

## UI

- Monitor page: policy editor (list + form per model: monitors, interval, breach params,
  promotion mode, enable toggle) + policy status strip. 409/structured-error display uses
  structured `{status, body}` from `gw.ts` (fixing the string-matching brittleness while the
  panel is touched).
- Models page: open suggestions with gate + shadow verdicts and accept/dismiss; audit rows
  visible per model.
