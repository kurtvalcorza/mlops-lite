# Contract: Reproducible Delivery Gates

## Required pull-request jobs

| Stable job | Required commands/outcomes | Prohibited |
|---|---|---|
| `backend` | clean Python 3.12 setup; Ruff; complete offline pytest collection and execution | GPU, model download, live stack, repository secrets |
| `ui` | pinned Node setup; `npm ci`; supported lint; production build/type-check | reuse of host/WSL `node_modules` |
| `compose` | create non-secret CI substitutions; `docker compose config --quiet` | starting or pulling the platform |
| `specs` | artifact/placeholder/ID/task/traceability consistency | marking implementation tasks complete in a spec-only PR |

Jobs report independently and use stable names suitable for branch protection. Workflow permissions
are read-only unless a future reporting requirement explicitly needs more.

## Backend setup

One repository command installs the dependency-light developer/test environment from a clean
checkout. It includes pytest, Ruff, and every library required to collect offline tests. It excludes
torch/CUDA/model weights and does not mutate runtime lock files.

Offline tests must not pass merely because broad modules fail to import. Optional heavy dependencies
are imported lazily or replaced with fakes for contract tests. Live/hardware tests use the existing
explicit guard fixtures and state the skip reason.

## UI setup

- Node version is pinned in workflow/project metadata.
- `npm ci` is authoritative and uses `ui/package-lock.json`.
- The production build is the type-check gate.
- Lint uses a command supported by the pinned Next.js version.
- CI never trusts a checked-out or cached `node_modules` directory from another OS/shell.

## Compose validation

CI supplies syntactically valid dummy values for required interpolation variables through a
temporary environment file. Values are not usable production secrets. Validation renders the model
only; it does not start services, pull images, or require Docker GPU support.

## Specification consistency

For every full feature folder, the checker verifies:

- `spec.md`, `plan.md`, `research.md`, `data-model.md`, `quickstart.md`, and `tasks.md` exist.
- referenced contracts/checklists exist.
- no template placeholders or unresolved clarification markers remain in an implementation-ready
  artifact.
- requirement, success-criterion, and task IDs are unique and monotonically valid for the increment.
- task format includes path and story where required.
- every user story has independent-test and task coverage.
- implementation tasks remain unchecked until implementation evidence exists.

Historical folders that predate a full artifact set may be allow-listed explicitly; the checker
must not silently ignore new incomplete folders.

## Hardware gate

The target-hardware quickstart remains manual/recorded and covers GPU invariants, actual model load,
streaming, rapid activation, and resource measurements. A `[HW]` task cannot be checked off from
hosted CI evidence.

## Local equivalence

Every required CI command is documented in `quickstart.md` and runnable locally. CI-specific wrappers
may orchestrate commands but cannot hide a materially different test path.
