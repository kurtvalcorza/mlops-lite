# Data Model: 020 Stack Remediation

020 adds **no new runtime data shapes** — the platform's entities (datasets, model versions,
predictions, labels, policies, jobs…) are untouched. The entities below are the increment's own
*evidence artifacts*: they gate transitions and then persist as records.

## MigrationReport (`scripts/migrate_store.py` output, JSON)

Gates cutover (FR-199) and decommission (FR-201); one per run, kept under `docs/` after the
final pre-cutover pass.

| Field | Type | Notes |
|---|---|---|
| `direction` | `"forward" \| "reverse"` | forward = incumbent → replacement |
| `started_at` / `finished_at` | float (epoch) | wall-clock bounds |
| `buckets[]` | list | one entry per bucket |
| `buckets[].name` | str | `datasets` / `models` / `results` / `mlflow` |
| `buckets[].source` | `{objects: int, bytes: int}` | counted after the copy pass |
| `buckets[].dest` | `{objects: int, bytes: int}` | must equal `source` for parity |
| `buckets[].copied` | int | objects copied THIS run (0 on an idempotent re-run — SC-127) |
| `buckets[].skipped` | int | already present with equal size |
| `parity` | bool | true iff every bucket's source == dest |

Validation: cutover requires `parity == true` on a fresh forward run; decommission additionally
requires a final forward run with `copied == 0` on every bucket (no stranded delta).
ETags are deliberately NOT compared (multipart ETags are not portable across stores — R3).

## RuntimeBaselineRecord (runbook entry, `docs/on-hardware-validation-018.md`)

The FR-205 evidence; one per drill run, appended.

| Field | Notes |
|---|---|
| `runtime` | `stdlib` or `uvicorn` (`AGENT_RUNTIME`) |
| `ttft_ms` | stream time-to-first-token, median of N |
| `stalls` | inter-token gaps > threshold under concurrent health polling |
| `multipart_ms` | vision/ASR multipart round-trip, median |
| `disconnect_ok` | mid-stream client disconnect: child unaffected, next request clean |
| `swap_contention` | behavior of a preempt arriving mid-stream (409-vs-drain per lease semantics) |
| `baselines` | the runbook numbers compared against |
| `verdict` | `keep-stdlib` or `upgrade-uvicorn` — with the misses named if any |

State transition: `AGENT_RUNTIME` default flips only on a recorded `upgrade-uvicorn` verdict; the
losing runtime's switch is deleted the following increment (R7).

## GoldenSet (`tests/goldens/<engine>/`, captured — not committed as truth)

Per child (vision/embed/tabular): the FR-203 byte-parity gate.

| Field | Notes |
|---|---|
| `request` | verb/path, content type, body bytes (fixture image / JSON payload) |
| `response.status` | int |
| `response.content_type` | str |
| `response.body` | bytes — diffed exactly at the agent boundary |
| `probe` | the `/readyz` exchange |

Captured against the live pre-swap child, replayed against the post-swap child on the same
machine/session (model-output floats are hardware-sensitive, so goldens are per-environment
artifacts, not repo fixtures — R6).

## StoreEndpoint (configuration, not a table)

The single seam the cutover flips: `MLFLOW_S3_ENDPOINT_URL` + the credential pair, consumed by
gateway, MLflow server, and the host venv. Invariant: exactly **one** store is authoritative at
any instant; both exist only inside the migration/rollback window (FR-200).
