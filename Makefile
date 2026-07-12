COMPOSE = docker compose
PWSH = pwsh -NoProfile -File
PY = python3

.PHONY: up down up-all down-all logs ps smoke gpu-check \
        test lint ui-check compose-check spec-check check

# --- 023 US3 (T508): the LOCAL equivalents of the required CI gates (delivery-gates.md) ------------
# Same commands CI runs (.github/workflows/quality.yml) — no CI-only test path.

test:          ## Full offline pytest (live/hw tests self-skip with reasons)
	$(PY) -m pytest

lint:          ## Ruff over the whole repository
	$(PY) -m ruff check .

ui-check:      ## UI lint + production build/type-check from a clean lockfile install
	cd ui && npm ci && npm run lint && npm run build

compose-check: ## Validate the Compose model with non-secret CI values (render only)
	@test -f .env || cp .env.ci.example .env
	$(COMPOSE) -f docker-compose.yml config --quiet
	$(COMPOSE) -f docker-compose.yml -f docker-compose.gpu.yml config --quiet

spec-check:    ## Spec artifact/ID/placeholder consistency + the retired-port guard
	$(PY) scripts/check_specs.py

check: lint test spec-check ## The backend + specs gates in one call

up:            ## Build + start the foundational stack (Compose only)
	$(COMPOSE) up -d --build

down:          ## Stop the stack (Compose only)
	$(COMPOSE) down

up-all:        ## 002/US3: one-command bring-up — infra + native daemons (supervised) + IP wiring
	$(PWSH) scripts/up_all.ps1

down-all:      ## 002/US3: one-command teardown — daemons (no GPU orphans) + infra
	$(PWSH) scripts/down_all.ps1

logs:          ## Tail logs
	$(COMPOSE) logs -f

ps:            ## Show service status
	$(COMPOSE) ps

smoke:         ## Run the Phase 1-2 foundation smoke test
	python tests/test_foundation.py

gpu-check:     ## Gate Zero: verify container GPU access
	bash scripts/gpu_check.sh
