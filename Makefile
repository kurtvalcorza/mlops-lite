COMPOSE = docker compose
PWSH = pwsh -NoProfile -File

.PHONY: up down up-all down-all logs ps smoke gpu-check

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
