# Quickstart: Validating Platform Architecture Hardening & Delivery Integrity

This is the implementation validation runbook for 023. Commands are illustrative until the
corresponding task lands; implementation must update them to exact copy/paste-ready commands. `[HW]`
means the target Windows/WSL + RTX 5070 Ti machine, not hosted CI.

## 0. Safety and baseline

```powershell
git status --short
git rev-parse --short HEAD
docker compose config --quiet
```

Record current gateway DB version, current serving identity, agent holder, and relevant image/model
disk use. Do not run destructive migration or activation drills against irreplaceable state without
the backup/restore step in US4.

## 1. Clean offline development environment (US3)

From a clean checkout and new Python environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest -q
```

Expected:

- collection succeeds without manually installing missing libraries;
- live/GPU tests skip with reasons;
- no torch/CUDA/model download occurs;
- offline tests pass.

UI from a clean dependency tree:

```powershell
Remove-Item -Recurse -Force ui\node_modules -ErrorAction SilentlyContinue
npm --prefix ui ci
npm --prefix ui run lint
npm --prefix ui run build
```

Expected: lockfile install and production build/type-check pass in the active OS environment.

## 2. Routing integrity (US1)

```powershell
.\.venv\Scripts\python -m pytest -q tests/test_evaluation_topology.py
.\.venv\Scripts\python scripts/check_specs.py --routing
```

Expected:

- LLM predictor default is `${AGENT_URL}/engines/llm/infer`;
- vision predictor default is `${AGENT_URL}/engines/vision/classify`;
- no executable routing default uses retired ports 8090–8095;
- explicit test endpoint injection still works when `evaluation.py` is loaded standalone.

## 3. Agent trust boundary (US2)

Generate/record secrets through the repository's standard secret command, then start the agent.

```powershell
.\.venv\Scripts\python -m pytest -q tests/test_agent_auth.py tests/test_agent_http.py tests/test_agent_jobs_http.py
```

Manual contract probes (replace `$AGENT_KEY` with a shell variable, never a command-line literal in
saved history):

```powershell
curl.exe -i http://localhost:8100/healthz
curl.exe -i http://localhost:8100/metrics
curl.exe -i http://localhost:8100/health
curl.exe -i -H "X-Agent-Key: $env:AGENT_KEY" http://localhost:8100/health
```

Expected:

- public healthz/metrics succeed;
- protected health without key is 401;
- correct key succeeds;
- wrong key is 403;
- missing key configuration prevents normal startup;
- open-development override works only when explicitly set and logs a warning;
- logs/metrics contain no key.

Also verify the gateway can infer, submit/poll/cancel a fake job, and unload using server-side key
injection while browser/BFF traffic contains only the gateway API key.

## 4. Required CI parity (US3)

Run the same commands named by `.github/workflows/quality.yml` locally:

```powershell
.\.venv\Scripts\python scripts/check_specs.py
docker compose --env-file .env.ci config --quiet
npm --prefix ui ci
npm --prefix ui run lint
npm --prefix ui run build
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest -q
```

Expected GitHub checks: `backend`, `ui`, `compose`, `specs`, each separately reported and required.

## 5. Migration adoption and upgrade (US4)

### Backup/restore gate

Before applying migrations to populated state:

```powershell
docker compose exec -T postgres pg_dump -U mlops -Fc gateway > gateway-before-023.dump
docker compose exec -T postgres createdb -U mlops gateway_restore_check
Get-Content -AsByteStream gateway-before-023.dump | docker compose exec -T postgres pg_restore -U mlops -d gateway_restore_check
docker compose exec -T postgres psql -U mlops -d gateway_restore_check -c "select count(*) from predictions;"
docker compose exec -T postgres dropdb -U mlops gateway_restore_check
```

Use implementation-provided wrappers if they avoid shell binary piping on the target platform; the
required outcome is a verified restore, not a particular shell expression.

### Migration tests

```powershell
.\.venv\Scripts\python -m pytest -q tests/test_migrations.py
.\.venv\Scripts\python scripts/migrate_db.py status
.\.venv\Scripts\python scripts/migrate_db.py apply
.\.venv\Scripts\python scripts/migrate_db.py apply
```

Expected:

- recognized legacy schema adopts baseline without data loss;
- fresh and upgraded schemas match;
- second apply changes nothing;
- two concurrent applies record each version once;
- edited checksum/newer unsupported schema fails closed;
- `init.sql` no longer duplicates application tables.

## 6. Recoverable LLM activation (US5, after 022 foundations)

```powershell
.\.venv\Scripts\python -m pytest -q tests/test_activation.py tests/test_activation_recovery.py
```

Failure-injection matrix:

| Inject after | Restart | Expected reconciliation |
|---|---|---|
| operation prepare | gateway | continue commit or safe rollback |
| alias mutation | gateway | pointer/reload resumes idempotently or alias restores |
| pointer mutation | gateway | desired/resident shown separately; reload or rollback |
| reload accepted | agent/gateway | read resident identity; never duplicate reload |
| target resident before `active` write | gateway | mark active from verified evidence |
| rollback pointer/alias partial | gateway | retry rollback or mark degraded with exact mismatch |

For every row, verify prediction logging uses agent-reported resident identity.

### `[HW]` activation drill

1. Serve LLM A; record `nvidia-smi`, agent holder, desired and resident identity.
2. Promote/switch to LLM B with operator confirmation.
3. Observe evict → free → load and exact B identity.
4. Repeat rapid alternating operations for at least 100 accepted cycles.
5. Start a GPU job and request a switch; verify refusal/defer and uninterrupted job.
6. Retry one operation ID after a forced client timeout; verify one reload.

Expected: at most one GPU tenant at every observation, last accepted target wins, no false identity.

## 7. Bounded retained transport (US6)

```powershell
.\.venv\Scripts\python -m pytest -q tests/test_agent_limits.py tests/test_agent_engines_http.py tests/test_agent_stream_drill.py
```

Validate:

- oversized JSON and multipart receive 413 before domain effects;
- chunked/unknown-length bodies are counted;
- concurrency above the bound queues finitely or receives 503;
- slow/disconnected clients release locks/workers;
- graceful shutdown drains then cleans children;
- REST/SSE golden behavior is unchanged;
- uvicorn/ASGI runtime switch is absent after parity.

### `[HW]` stream drill

Run `scripts/agent_stream_drill.py` against the retained transport for ordinary completion,
pre-header failure, mid-stream disconnect, and repeated streams. Confirm zero admission leak and one
resident tenant.

## 8. Metrics, alerts, and docs (US7)

```powershell
docker compose config --quiet
.\.venv\Scripts\python -m pytest -q tests/test_alert_rules.py tests/test_metrics_contract.py
```

Inject or replay metrics for every rule. In Prometheus/Grafana confirm alerts for wedged engine,
prolonged holder, repeated scheduler/activation/migration failure, low disk, and unavailable stores.
Check that labels never contain model/job/prediction/operation IDs or error strings.

Run the documentation checklist against README, this architecture review, Compose, topology, and git
status. Historical specs remain unchanged and clearly linked as history.

## 9. Full completion gate

```powershell
git diff --check
.\.venv\Scripts\python scripts/check_specs.py
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest -q
npm --prefix ui ci
npm --prefix ui run lint
npm --prefix ui run build
docker compose --env-file .env.ci config --quiet
```

Then complete all `[HW]` tasks and attach results with hardware profile, commit, commands, timestamps,
and observed invariants. Only after both offline and target-hardware gates pass may 023 be marked
implemented.

## Evidence — offline slice (2026-07-12, implementation PR)

Recorded from the implementation environment (Linux sandbox, Python 3.11, Node 22, local
Postgres 16 for the migration suite; the pinned CI environment is Python 3.12 + postgres:17).

- **SC-152 (US1 routing)**: `tests/test_evaluation_topology.py` — 7/7 pass (standalone load,
  URL derivation `<agent>/engines/llm|vision`, override precedence, fake-HTTP path assertions);
  `python scripts/check_specs.py` retired-port guard: **OK** over the whole executable tree.
- **SC-153 (US2 boundary)**: `tests/test_agent_auth.py` — 22/22 pass (fail-closed startup incl.
  the SWAP_CONTROL_SECRET non-enable rule, exact public allow-list, 401/403 payloads,
  auth-before-side-effects, constant-time seam, /metrics redaction, open-mode warning);
  `tests/test_serving_client.py` key-injection + no-redirect pins pass.
- **SC-154/155 (US3 gates)**: `python -m ruff check .` → All checks passed; full offline
  `pytest` → **green** (594+ passed at US2 checkpoint, grown since; 0 failed; live/hw skips are
  reasoned); `npm ci && npm run lint && npm run build` → clean;
  `docker compose config --quiet` (both files, `.env.ci.example` values) → exit 0;
  `scripts/check_specs.py` → OK. Branch protection (T509's external half) awaits the repo admin.
- **SC-156/157 (US4 migrations)**: `tests/test_migrations.py` — 10/10 pass against real Postgres:
  fresh apply + exact shape, legacy adoption with row preservation, no-op repeat, 4-way
  concurrent apply-once, mid-file rollback, checksum refusal, newer-schema refusal,
  bootstrap-never-creates, activation-repository CAS. Populated-copy backup/restore drill (T517)
  is the [HW/store] tail.
- **SC-158 (US5 recovery)**: `tests/test_activation.py` 13/13 + `tests/test_activation_recovery.py`
  8/8 — failure after every step + restart reconciliation converge with no duplicate reload;
  prediction identity stays agent-resident throughout. The 100-switch drill (T528) is [HW].
- **SC-160/161 (US6 bounds)**: `tests/test_agent_limits.py` — 8/8 socket-level pins
  (413-before-read, counted chunked abort, multipart bound, auth-before-buffer, saturation 503,
  bounded queueing, shutdown drain, applied IO timeout). On-host thread/memory measurement (T536)
  is [HW].
- **SC-162..164 (US7)**: metrics/alert contracts pass (`tests/test_metrics_contract.py` +
  `tests/test_alert_rules.py`, 17 tests — synthetic evaluation = every rule's expression resolves
  against exported metrics, runbook anchors verified); README/current-architecture/constitution
  v1.5.2 updated. The extraction-parity and resource-comparison halves ride the deferred
  T539/T543–T545 + T549.
- **T554 security review (offline scope)**: `scripts/check_secrets.sh` → no committed
  credentials; no `follow_redirects=True` anywhere agent-directed (also pinned by test); key
  values never printed by gen_secrets (written to .env only) and never serialized in
  health/metrics/errors (pinned by tests); unauthorized paths proven side-effect-free (T497).
