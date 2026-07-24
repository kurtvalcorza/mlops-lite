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
| [0005](./0005-serving-llm-pointer-not-mlflow-alias.md) | Accepted | The platform serving-LLM selection is a Postgres pointer, not an MLflow alias (documents spec 022). |

_Reserved for feature 024 (added with the refactors they document — see `specs/024-deepen-modules-seams/tasks.md`):_
`0001` store decomposition · `0002` go-live paths NOT merged (rejected) · `0003` agent stays framework-free · `0004` behavior-preserving test-parity gate.
