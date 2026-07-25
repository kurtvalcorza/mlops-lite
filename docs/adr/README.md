# Architecture Decision Records

Lightweight ADRs for mlops-lite. Each record captures a decision (or a deliberately **rejected**
alternative) that is not obvious from the code alone — so a future contributor does not re-lit­igate
it or "helpfully" undo it.

**Format** — one file per decision, `NNNN-title.md`:

- **Status**: Accepted | Rejected | Superseded
- **Context**: the forces and constraints
- **Decision**: what was chosen
- **Consequences**: what follows (good and bad), and what NOT to do

## Index

| ADR | Status | Summary |
|---|---|---|
| [0001](./0001-store-decomposition.md) | Accepted | Decompose `platformlib/store.py` into per-aggregate `storeimpl/` repositories behind the test-pinned facade (024 US1). |
| [0002](./0002-go-live-paths-not-merged.md) | Rejected | Do NOT merge the three `registry.promote` callers — only the operator route may live-switch the LLM (FR-275/307/313; 024 US2). |
| [0003](./0003-agent-stays-framework-free.md) | Accepted | The host agent dispatches with a stdlib route table, not a web framework (Principle III / FR-339; 024 US3). |
| [0004](./0004-behavior-preserving-test-parity-gate.md) | Accepted | 024's refactors are gated by test parity — unchanged offline suite + web-free seam tests + live legs. |
| [0005](./0005-serving-llm-pointer-not-mlflow-alias.md) | Accepted | The platform serving-LLM selection is a Postgres pointer, not an MLflow alias (documents spec 022). |
