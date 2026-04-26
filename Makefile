# All targets assume the current working directory is the project root.
# Run from project root (e.g. `cd /path/to/personal_agent`) so uv finds pyproject.toml and .venv.

# ─────────────────────────────────────────────────────────────────────────────
# Environment selector
#   ENV=local  (default) → docker-compose.yml
#   ENV=cloud            → docker-compose.cloud.yml
#
# Service selector (optional — all targets that wrap docker compose accept it)
#   SERVICE=<name>  target a single service; omit to operate on all
#   Run `make services` to list available service names.
#
# Examples:
#   make up                              # start all local services
#   make up SERVICE=neo4j                # start one service
#   make logs SERVICE=seshat-gateway     # tail one service
#   make restart SERVICE=searxng         # restart one service
#   make shell SERVICE=neo4j             # exec into neo4j container
#   make rebuild SERVICE=seshat-gateway  # local build + restart
#   ENV=cloud make services              # list cloud services
#   ENV=cloud make ps                    # check cloud container status
# ─────────────────────────────────────────────────────────────────────────────

ENV     ?= local
SERVICE ?=

ifeq ($(ENV),cloud)
    COMPOSE := docker compose -f docker-compose.cloud.yml
else
    COMPOSE := docker compose
endif

# Cloud compose always available for VPS-specific targets regardless of ENV
COMPOSE_CLOUD := docker compose -f docker-compose.cloud.yml

.PHONY: help services up down stop restart ps logs rebuild shell health \
        infra-up infra-down dev \
        deploy build build-full vps-bootstrap \
        tunnel-up tunnel-down tunnel-status \
        test test-integration test-all test-file test-verbose test-k test-cov eval \
        mypy ruff-check ruff-format

help:
	@echo "Run from project root so uv uses .venv (e.g. cd /path/to/personal_agent)."
	@echo ""
	@echo "Usage:"
	@echo "  make services             List all services in current ENV compose file"
	@echo "  make up [SERVICE=x]       Start all services or one service"
	@echo "  make down [SERVICE=x]     Remove containers (use 'stop' to preserve them)"
	@echo "  make stop [SERVICE=x]     Stop containers (preserve data volumes)"
	@echo "  make restart [SERVICE=x]  Restart all or one service"
	@echo "  make ps [SERVICE=x]       Show container status"
	@echo "  make logs [SERVICE=x]     Tail service logs"
	@echo "  make rebuild SERVICE=x    Local: build + restart one service"
	@echo "  make shell SERVICE=x      Exec shell into a service (bash, sh fallback)"
	@echo "  make health               Ping /health on localhost:9000"
	@echo ""
	@echo "  make dev                  Start agent service with hot-reload (local only)"
	@echo ""
	@echo "  VPS / cloud (run from Mac or directly on VPS):"
	@echo "  make deploy               SSH → pull + restart (no rebuild)"
	@echo "  make build                SSH → pull + rebuild seshat-gateway + restart"
	@echo "  make build-full           SSH → pull + rebuild all images + restart"
	@echo "  make vps-bootstrap        First-time full deploy on VPS"
	@echo "  make tunnel-up            Start cloudflared tunnel service"
	@echo "  make tunnel-down          Stop cloudflared tunnel service"
	@echo "  make tunnel-status        Show cloudflared tunnel container status"
	@echo ""
	@echo "  Set ENV=cloud to target docker-compose.cloud.yml"
	@echo "  e.g. ENV=cloud make ps  or  ENV=cloud make logs SERVICE=seshat-gateway"
	@echo ""
	@echo "  make test                 Run fast unit/mock tests only — SAFE, no LLM needed"
	@echo "  make test-integration     Run integration tests (PERSONAL_AGENT_INTEGRATION=1 required)"
	@echo "  make test-all             Run full test suite (unit tests only)"
	@echo "  make test-file FILE=...   Run tests for a specific file"
	@echo "  make test-verbose         Run unit tests with verbose output"
	@echo "  make test-k K=pattern     Run tests matching a name pattern"
	@echo "  make test-cov             Run unit tests with coverage report"
	@echo ""
	@echo "  make mypy                 Type-check with mypy"
	@echo "  make ruff-check           Lint with ruff check"
	@echo "  make ruff-format          Format with ruff format"
	@echo ""
	@echo "  ⚠  AI AGENTS: Use 'make test' only. Do NOT run test-integration or eval without"
	@echo "     explicit user instruction — they fire live LLM calls and will overload the GPU."

# ─── Infrastructure targets ──────────────────────────────────────────────────

services:
	@$(COMPOSE) config --services

up:
	@$(COMPOSE) up -d $(SERVICE)
	@[ -n "$(SERVICE)" ] || [ "$(ENV)" != "local" ] || bash scripts/init-services.sh

down:
	@$(COMPOSE) down $(SERVICE)

stop:
	@$(COMPOSE) stop $(SERVICE)

restart:
	@$(COMPOSE) restart $(SERVICE)

ps:
	@$(COMPOSE) ps $(SERVICE)

logs:
	@$(COMPOSE) logs -f $(SERVICE)

rebuild:
	@[ -n "$(SERVICE)" ] || { echo "Usage: make rebuild SERVICE=<name>  (run 'make services' to list)"; exit 1; }
	@$(COMPOSE) build $(SERVICE)
	@$(COMPOSE) up -d $(SERVICE)

shell:
	@[ -n "$(SERVICE)" ] || { echo "Usage: make shell SERVICE=<name>  (run 'make services' to list)"; exit 1; }
	@$(COMPOSE) exec $(SERVICE) bash 2>/dev/null || $(COMPOSE) exec $(SERVICE) sh

health:
	@curl -sf http://localhost:9000/health | python3 -m json.tool

# ─── Backward-compat aliases ─────────────────────────────────────────────────

infra-up:
	@$(MAKE) up

infra-down:
	@$(MAKE) down

# ─── Local dev ───────────────────────────────────────────────────────────────

dev:
	@uv run uvicorn personal_agent.service.app:app --reload --port 9000

# ─── VPS deploy (run from Mac — SSH to VPS) ──────────────────────────────────

deploy:
	@bash infrastructure/scripts/deploy.sh

build:
	@bash infrastructure/scripts/deploy.sh --build

build-full:
	@bash infrastructure/scripts/deploy.sh --full

vps-bootstrap:
	@$(MAKE) build-full

# ─── Cloudflare tunnel (VPS cloudflared container) ───────────────────────────

tunnel-up:
	@$(COMPOSE_CLOUD) start cloudflared

tunnel-down:
	@$(COMPOSE_CLOUD) stop cloudflared

tunnel-status:
	@$(COMPOSE_CLOUD) ps cloudflared

# ─── Tests ───────────────────────────────────────────────────────────────────
# SAFE for agents and CI:
test:
	@uv run python -m pytest -m "not integration" -q

# REQUIRES PERSONAL_AGENT_INTEGRATION=1 — fires real LLM calls (3-10 min, GPU-intensive).
# Do NOT run from an AI agent session without explicit user instruction.
test-integration:
	@if [ "$$PERSONAL_AGENT_INTEGRATION" != "1" ]; then \
		echo "ERROR: Set PERSONAL_AGENT_INTEGRATION=1 to run integration tests."; \
		echo "       These tests fire real LLM calls and require a live inference server."; \
		echo "       Example: PERSONAL_AGENT_INTEGRATION=1 make test-integration"; \
		exit 1; \
	fi
	@uv run python -m pytest -m "integration" -q

# Runs the unit tests only (same as 'test') — eval harness excluded.
test-all:
	@uv run python -m pytest -m "not integration" -q

# REQUIRES PERSONAL_AGENT_EVAL=1 — fires 100+ LLM calls (~60-90 min, GPU-intensive).
# Do NOT run from an AI agent session without explicit user instruction.
eval:
	@PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run $(EVAL_ARGS)

# Flexible test targets (run from project root so uv uses project env)
test-file:
	@uv run pytest $(or $(FILE),tests/test_tools/test_self_telemetry.py)

test-verbose:
	@uv run pytest -m "not integration" -v

test-k:
	@uv run pytest -m "not integration" -k "$(K)"

test-cov:
	@uv run pytest -m "not integration" --cov=src/personal_agent --cov-report=term-missing

# ─── Code quality ─────────────────────────────────────────────────────────────

mypy:
	@uv run mypy src/

ruff-check:
	@uv run ruff check src/

ruff-format:
	@uv run ruff format src/
