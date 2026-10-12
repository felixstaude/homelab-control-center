# Homelab Control Center — development task runner.
#
# One entry point for every routine task, so contributors do not have to
# remember three toolchains' invocations and CI runs exactly what a developer
# runs locally.
#
#   make help        list every target
#   make setup       install all dependencies
#   make check       run every quality gate (what CI runs)
#
# Requires: Python 3.12+, Node 20.19+/22.12+, Go 1.24+, and Docker or Podman.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# --- Paths ---
SERVER_DIR := apps/server
WEB_DIR    := apps/web
AGENT_DIR  := agents/linux
COMPOSE_FILE := deploy/compose.yaml

VENV   := $(SERVER_DIR)/.venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip

# --- Container runtime detection (ADR-010) ---
# Prefers `docker compose`, falls back to `podman-compose`, so the same
# targets work on a Docker host and on a Fedora box running Podman.
COMPOSE := $(shell \
	if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then \
		echo "docker compose"; \
	elif command -v podman-compose >/dev/null 2>&1; then \
		echo "podman-compose"; \
	else \
		echo ""; \
	fi)

# --- Local development database ---
# Matches deploy/compose.yaml. Local throwaway credentials, not a secret.
DEV_DATABASE_URL := postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc

# --- Agent build metadata, stamped into the binary at link time ---
AGENT_VERSION   ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
AGENT_COMMIT    ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
AGENT_BUILDDATE ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
AGENT_PKG       := github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo
AGENT_LDFLAGS   := -s -w \
	-X $(AGENT_PKG).version=$(AGENT_VERSION) \
	-X $(AGENT_PKG).commit=$(AGENT_COMMIT) \
	-X $(AGENT_PKG).buildDate=$(AGENT_BUILDDATE)

.PHONY: help
help: ## Show this help
	@echo "Homelab Control Center — development targets"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Container runtime: $(if $(COMPOSE),$(COMPOSE),NONE FOUND — install Docker or podman-compose)"

# ============================================================================
# Setup
# ============================================================================

.PHONY: setup
setup: install-server install-web ## Install all dependencies
	@echo "✓ Setup complete. Next: make db-up && make migrate"

.PHONY: install-server
install-server: ## Create the Python venv and install the backend
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -e "$(SERVER_DIR)[dev]"
	@echo "✓ Backend installed into $(VENV)"

.PHONY: install-web
install-web: ## Install frontend dependencies
	@cd $(WEB_DIR) && npm ci || npm install
	@echo "✓ Frontend dependencies installed"

.PHONY: env
env: ## Create .env files from the committed examples
	@test -f $(SERVER_DIR)/.env || cp $(SERVER_DIR)/.env.example $(SERVER_DIR)/.env
	@test -f $(WEB_DIR)/.env.local || cp $(WEB_DIR)/.env.example $(WEB_DIR)/.env.local
	@echo "✓ Environment files ready (edit them; they are gitignored)"

# ============================================================================
# Database
# ============================================================================

.PHONY: db-up
db-up: ## Start PostgreSQL and wait until it accepts queries
	@test -n "$(COMPOSE)" || { echo "No container runtime found. Install Docker or podman-compose."; exit 1; }
	$(COMPOSE) -f $(COMPOSE_FILE) up -d
	@echo "Waiting for PostgreSQL to accept connections..."
	@for i in $$(seq 1 60); do \
		if $(COMPOSE) -f $(COMPOSE_FILE) exec -T postgres pg_isready -U hcc -d hcc >/dev/null 2>&1; then \
			echo "✓ PostgreSQL ready"; exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "✗ PostgreSQL did not become ready within 60s"; exit 1

.PHONY: db-down
db-down: ## Stop PostgreSQL, preserving data
	@test -n "$(COMPOSE)" || { echo "No container runtime found."; exit 1; }
	$(COMPOSE) -f $(COMPOSE_FILE) down

.PHONY: db-reset
db-reset: ## Destroy the database volume and recreate it from migrations
	@test -n "$(COMPOSE)" || { echo "No container runtime found."; exit 1; }
	@echo "This deletes all local development data."
	$(COMPOSE) -f $(COMPOSE_FILE) down -v
	@$(MAKE) db-up
	@$(MAKE) migrate

.PHONY: db-shell
db-shell: ## Open a psql shell against the development database
	@test -n "$(COMPOSE)" || { echo "No container runtime found."; exit 1; }
	$(COMPOSE) -f $(COMPOSE_FILE) exec postgres psql -U hcc -d hcc

.PHONY: migrate
migrate: ## Apply all pending migrations
	@cd $(SERVER_DIR) && HCC_DATABASE_URL="$(DEV_DATABASE_URL)" .venv/bin/alembic upgrade head

.PHONY: migrate-down
migrate-down: ## Roll back the most recent migration
	@cd $(SERVER_DIR) && HCC_DATABASE_URL="$(DEV_DATABASE_URL)" .venv/bin/alembic downgrade -1

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add hosts table"
	@test -n "$(m)" || { echo 'Usage: make migration m="description"'; exit 1; }
	@cd $(SERVER_DIR) && HCC_DATABASE_URL="$(DEV_DATABASE_URL)" \
		.venv/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: bootstrap
bootstrap: ## Create the initial administrator (prompts for a password)
# Runs once per installation and does not reopen — see ADR-033. Use
# `make db-reset` to start over in development.
	@cd $(SERVER_DIR) && HCC_DATABASE_URL="$(DEV_DATABASE_URL)" \
		.venv/bin/hcc bootstrap-admin $(if $(u),--username $(u),)

# ============================================================================
# Development servers
# ============================================================================

.PHONY: dev-server
dev-server: ## Run the backend with hot reload on :8000
	@cd $(SERVER_DIR) && HCC_DATABASE_URL="$(DEV_DATABASE_URL)" \
		HCC_CORS_ORIGINS='["http://localhost:5173"]' \
		.venv/bin/uvicorn --factory hcc.app:create_app \
			--reload --host 127.0.0.1 --port 8000 --log-config /dev/null \
			--no-server-header

.PHONY: dev-web
dev-web: ## Run the frontend dev server on :5173
	@cd $(WEB_DIR) && npm run dev

.PHONY: agent-run
agent-run: ## Run the agent against the local control plane
	@cd $(AGENT_DIR) && go run ./cmd/hcc-agent

# ============================================================================
# Quality gates
# ============================================================================

.PHONY: check
check: check-docs check-contract check-server check-web check-agent ## Run the local quality gates
# Not everything CI runs: CI additionally executes the PostgreSQL integration
# tests, the migration apply/reverse/check cycle, the frontend production
# build, Go cross-compilation and the browser end-to-end suite. Use
# `make test-integration`, `make smoke` and `make e2e` for those locally.
	@echo "✓ All checks passed"

.PHONY: check-contract
check-contract: ## Fail if the API contract changed without regenerating the snapshot
	@./scripts/openapi-snapshot.sh --check

.PHONY: check-docs
check-docs: ## Validate documentation links, anchors, ADR numbering and roadmap
	@./scripts/check-docs.sh

# --- Backend ---

.PHONY: check-server
check-server: fmt-server-check lint-server typecheck-server test-server ## Backend gate

.PHONY: fmt-server
fmt-server: ## Format Python code
	@cd $(SERVER_DIR) && .venv/bin/ruff format .
	@cd $(SERVER_DIR) && .venv/bin/ruff check --fix .

.PHONY: fmt-server-check
fmt-server-check: ## Verify Python formatting
	@cd $(SERVER_DIR) && .venv/bin/ruff format --check .

.PHONY: lint-server
lint-server: ## Lint Python code
	@cd $(SERVER_DIR) && .venv/bin/ruff check .

.PHONY: typecheck-server
typecheck-server: ## Type-check Python code
	@cd $(SERVER_DIR) && .venv/bin/mypy

.PHONY: test-server
test-server: ## Run backend unit tests (no services required)
	@cd $(SERVER_DIR) && .venv/bin/python -m pytest

.PHONY: smoke
smoke: ## End-to-end smoke test against a running database (requires make db-up)
	@./scripts/smoke-test.sh

.PHONY: e2e
e2e: ## Browser end-to-end tests (requires make db-up)
# Creates its own disposable database and ports, so it never touches the
# development installation — see scripts/e2e.sh.
	@./scripts/e2e.sh

.PHONY: openapi
openapi: ## Regenerate the OpenAPI snapshot the web client's types are checked against
	@./scripts/openapi-snapshot.sh

.PHONY: test-integration
test-integration: ## Run backend integration tests (requires make db-up)
	@cd $(SERVER_DIR) && HCC_TEST_DATABASE_URL="$(DEV_DATABASE_URL)" \
		.venv/bin/python -m pytest -m integration

# --- Frontend ---

.PHONY: check-web
check-web: ## Frontend gate
	@cd $(WEB_DIR) && npm run typecheck && npm run lint && npm run test

.PHONY: fmt-web
fmt-web: ## Auto-fix frontend lint issues
	@cd $(WEB_DIR) && npm run lint:fix

.PHONY: test-web
test-web: ## Run frontend tests
	@cd $(WEB_DIR) && npm run test

.PHONY: build-web
build-web: ## Build the production frontend bundle
	@cd $(WEB_DIR) && npm run build

# --- Agent ---

.PHONY: check-agent
check-agent: fmt-agent-check vet-agent test-agent ## Agent gate

.PHONY: fmt-agent
fmt-agent: ## Format Go code
	@cd $(AGENT_DIR) && gofmt -w .

.PHONY: fmt-agent-check
fmt-agent-check: ## Verify Go formatting
	@cd $(AGENT_DIR) && \
		unformatted=$$(gofmt -l .); \
		if [ -n "$$unformatted" ]; then \
			echo "✗ Not gofmt-clean:"; echo "$$unformatted"; exit 1; \
		fi
	@echo "✓ gofmt clean"

.PHONY: vet-agent
vet-agent: ## Run go vet
	@cd $(AGENT_DIR) && go vet ./...

.PHONY: test-agent
test-agent: ## Run agent tests with the race detector
	@cd $(AGENT_DIR) && go test -race ./...

.PHONY: agent-build
agent-build: ## Build the agent binary with version stamping
	@cd $(AGENT_DIR) && go build -ldflags "$(AGENT_LDFLAGS)" -o bin/hcc-agent ./cmd/hcc-agent
	@echo "✓ Built $(AGENT_DIR)/bin/hcc-agent ($(AGENT_VERSION))"
	@$(AGENT_DIR)/bin/hcc-agent -version

# ============================================================================
# Housekeeping
# ============================================================================

.PHONY: clean
clean: ## Remove build artefacts and caches
	rm -rf $(AGENT_DIR)/bin $(WEB_DIR)/dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(SERVER_DIR)/.pytest_cache $(SERVER_DIR)/.mypy_cache $(SERVER_DIR)/.ruff_cache
	@echo "✓ Cleaned"

.PHONY: clean-all
clean-all: clean ## Also remove installed dependencies
	rm -rf $(VENV) $(WEB_DIR)/node_modules
	@echo "✓ Dependencies removed"
