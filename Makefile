SHELL := /bin/zsh
.DEFAULT_GOAL := help
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help venv test test-docker build bootstrap status state backup dedup dedup-apply update teardown export preflight swagger clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

venv: ## Create the local Python environment
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(VENV)/bin/pip install -q --upgrade pip
	@$(VENV)/bin/pip install -q -r requirements-dev.txt

test: venv ## Run the full offline test suite locally
	@$(PY) -m pytest

test-docker: ## Run the full offline test suite in the container (what CI runs)
	@docker-compose build --quiet tests && docker-compose run --rm tests

preflight: ## Check the DLP same-group gate and connector availability
	@./scripts/preflight.sh

swagger: ## Fetch live connector swagger to enable connector contract tests
	@./scripts/fetch-connector-swagger.sh

build: ## Pack managed + unmanaged solution zips into dist/
	@./scripts/build.sh

bootstrap: ## Guided first-time install (what you hand to a new user)
	@./scripts/bootstrap.sh

status: ## Show what is installed and whether it is healthy
	@./scripts/status.sh

state: ## Show whether the state lists exist and their row counts
	@./scripts/show-state.sh

backup: ## Back up the state lists to OneDrive (flow 6) and config locally
	@./scripts/run-flow.sh 6
	@./scripts/backup.sh

dedup: ## Report duplicate mirrored events (dry run; nothing is deleted)
	@./scripts/run-flow.sh 7

dedup-apply: ## Delete duplicate mirrored events and rebuild the sync map
	@./scripts/run-flow.sh --apply 7

update: ## Graceful in-place upgrade, preserving settings and state
	@./scripts/update.sh

teardown: ## Staged removal, confirming each destructive step separately
	@./scripts/teardown.sh

export: ## Pull portal edits back into solution/src as source of truth
	@./scripts/export.sh

clean: ## Remove build artefacts and caches
	@rm -rf solution/out dist/*.zip .pytest_cache **/__pycache__
