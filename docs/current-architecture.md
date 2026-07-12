# Current architecture — ground truth + reusable checklist (023 US7, T547 — FR-328)

Two things in one file: the **current-state snapshot** (the entry point the README links) and the
**checklist** every future spec/review must walk before claiming what the platform "currently"
does. Historical specs are immutable decision history; THIS file is the one place that must stay
current. When an increment changes any row below, updating this file is part of that increment.

## Snapshot (as of 023)

### Process topology

| Process | Where | Port | Notes |
|---|---|---|---|
| FastAPI gateway | Compose | :8080 | the only operator-facing API; applies DB migrations at startup |
| MLflow | Compose | :5500 | tracking + registry + traces; Postgres backend, Garage artifacts |
| Garage | Compose | :3900 | THE S3 object store (datasets · models · results) |
| Postgres | Compose | :55432 | `mlflow` DB + `gateway` DB (relational store, ledgered schema) |
| Prometheus | Compose | :9090 | scrapes gateway + agent directly; local alert rules, no Alertmanager |
| Grafana | Compose | :3001 | dashboards over Prometheus |
| gpu-host-agent | native WSL | :8100 | ONE bounded stdlib HTTP process: every engine child + every job |
| operator console | native WSL | :3000 | Next.js + key-injecting BFF, 127.0.0.1 only |
| supervisor | native WSL | :8099 | starts/restarts {agent, ui}; probes the agent's public /readyz |

Retired (do not resurrect in code or docs-as-current): per-daemon serving ports 8090–8095, the
cross-process GPU lockfile, the BentoML children, the MinIO store, `hostagent/asgi.py` +
`AGENT_RUNTIME`.

### Data authority (who owns what — no substitutions mid-operation)

| Fact | Authority |
|---|---|
| experiment/model versions, gate metrics, `@serving` aliases | MLflow registry |
| dataset versions | content-addressed registry on Garage (constitution v1.5.2) |
| desired platform-wide LLM | `serving_llm` pointer row (Postgres) |
| activation/recovery state | `activation_operations` (Postgres, 023 US5) |
| actually-resident model | the host agent ONLY (health-reported identity) |
| prediction identity | copied from the agent-reported resident at request time |
| monitoring/job state | `gateway` Postgres DB via `platformlib.store` |
| relational schema | `platformlib/migrations/*.sql` + the `schema_migrations` ledger (023 US4) |

### Trust boundaries

| Boundary | Credential | Fail posture |
|---|---|---|
| operator → gateway | `X-API-Key` (`GATEWAY_API_KEYS`) | fail-closed (005); `GATEWAY_ALLOW_OPEN` dev-only |
| gateway/tools → agent | `X-Agent-Key` (`AGENT_API_KEY`, 023 US2) | agent refuses to START keyless; `AGENT_ALLOW_OPEN` dev-only |
| browser → BFF → gateway | BFF injects the key; allowlist + CSRF/CSP (004) | keys never reach the browser |
| public agent probes | none — exactly `GET /healthz` `/readyz` `/metrics` | everything else 401/403 before side effects |

### Runtime invariants

- ONE GPU tenant at any instant — in-process admission (`hostagent/admission.py`); jobs are
  structurally non-preemptable; serving displacement is operator-confirmed (`preempt=true`).
- Promote = go-live for text-generation, as a durable ActivationOperation (one non-terminal
  platform-wide; reconciled at gateway startup; `active` requires agent-verified residency).
- Agent transport bounds: `AGENT_MAX_WORKERS`+`AGENT_QUEUE_SIZE` admission, 1 MiB JSON / 32 MiB
  multipart body caps before buffering, per-socket IO timeouts, drain-on-SIGTERM.
- Gateway startup order: migrations (fail readiness on error) → reconcile activations → serve.

### Commands (local = CI, contracts/delivery-gates.md)

```
make lint test spec-check      # backend + specs gates (= the required PR checks)
make ui-check compose-check    # UI build/lint + Compose render
python scripts/migrate_db.py status|apply
python scripts/check_specs.py  # spec consistency + retired-port guard
```

## The checklist (walk it before writing "currently…" in any spec/review)

- [ ] **Topology**: does the doc's process/port list match the Snapshot table (and
      `docker-compose.yml` + `platformlib/topology.py`)? No retired port/daemon named as current?
- [ ] **Data authority**: does every stated fact-owner match the authority table? (Desired vs
      resident especially — the pointer is never evidence of residency.)
- [ ] **Trust boundaries**: are both credentials (`X-API-Key`, `X-Agent-Key`) and both fail-closed
      postures stated where auth is discussed? Public agent surface = exactly three GET probes?
- [ ] **Runtime status**: is anything described as "in progress" that has landed (or vice versa)?
      Check the increment table in README against merged `specs/*/tasks.md`.
- [ ] **Ports/commands**: are quoted commands runnable today (`make …`, `scripts/…`)? Do quoted
      URLs use `AGENT_URL`/derived engine paths, never literal retired ports?
- [ ] **Schema**: does any proposed relational change come as a NEW numbered migration file (never
      an edit to an applied one, never ad-hoc DDL in code)?
- [ ] **This file**: if the increment changes any answer above, update the Snapshot in the same PR.
