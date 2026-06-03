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
    COMPOSE      := docker compose -f docker-compose.cloud.yml
    SERVICE_PORT ?= 9001
else
    COMPOSE      := docker compose
    SERVICE_PORT ?= 9000
endif

# ─── VPS execution context ────────────────────────────────────────────────────
# Detects whether make is running ON the VPS (deploy path exists locally) or
# on a dev machine that needs to SSH in. All credentials stay in env vars —
# nothing is hardcoded here.
#   VPS_DEPLOY_PATH  path to repo on VPS (default: /opt/seshat)
#   VPS_SSH_HOST     SSH alias/host for the VPS (required on dev machines)
VPS_DEPLOY_PATH ?= /opt/seshat
SSH_HOST        := $(VPS_SSH_HOST)

_ON_VPS := $(shell test -d $(VPS_DEPLOY_PATH) && echo 1 || echo 0)

ifeq ($(_ON_VPS),1)
    CLOUD_EXEC = docker compose -f $(VPS_DEPLOY_PATH)/docker-compose.cloud.yml
else
    CLOUD_EXEC = ssh $(SSH_HOST) "cd $(VPS_DEPLOY_PATH) && docker compose -f docker-compose.cloud.yml"
endif

.PHONY: help services up down stop restart ps logs rebuild shell health \
        infra-up infra-down dev \
        deploy build build-full vps-bootstrap \
        tunnel-up tunnel-down tunnel-status _tunnel-guard \
        sandbox-build \
        test test-integration test-all test-file test-verbose test-k test-cov eval \
        test-pwa test-pwa-e2e \
        mypy ruff-check ruff-format \
        eval-recovery-survey eval-recovery \
        test-infra-up test-infra-down test-infra-reset test-infra-ps \
        eval-infra-up eval-infra-down

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
	@echo "  make health               Ping /health (port 9000 local, 9001 cloud)"
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
	@# Always (re)apply ES templates after a full-stack up. Idempotent; the
	@# script has its own readiness wait. Cloud env uses 127.0.0.1:9200 too —
	@# ES is bound to the loopback per docker-compose.cloud.yml.
	@[ -n "$(SERVICE)" ] || ES_URL="http://localhost:9200" bash scripts/setup-elasticsearch.sh

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
	@curl -sf http://localhost:$(SERVICE_PORT)/health | python3 -m json.tool

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
# Works from the VPS (runs docker compose directly) or from a dev machine
# (SSHes in). Set VPS_SSH_HOST in your environment for the latter.

_tunnel-guard:
	@[ "$(_ON_VPS)" = "1" ] || [ -n "$(SSH_HOST)" ] || \
	  { echo "ERROR: VPS_SSH_HOST is not set — required to reach the VPS from this machine"; exit 1; }

tunnel-up: _tunnel-guard
	@$(CLOUD_EXEC) start cloudflared

tunnel-down: _tunnel-guard
	@$(CLOUD_EXEC) stop cloudflared

tunnel-status: _tunnel-guard
	@$(CLOUD_EXEC) ps cloudflared

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

# PWA unit tests via Vitest (FRE-400 WS2 — added in PR2).
# Requires Node.js and seshat-pwa/package-lock.json to be present.
test-pwa:
	@cd seshat-pwa && npm ci --prefer-offline && npx vitest run

# PWA browser e2e tests via Playwright (FRE-400 WS3 — added in PR3).
# Runs headless Chromium; builds + serves the PWA automatically.
# One-time browser install: cd seshat-pwa && npx playwright install --with-deps chromium
test-pwa-e2e:
	@cd seshat-pwa && npm ci --prefer-offline && npx playwright test

# Runs the unit tests only (same as 'test') — eval harness excluded.
test-all:
	@uv run python -m pytest -m "not integration" -q

# Eval harness script tests — pure unit tests of analysis/reporting scripts.
# No live infrastructure required. Always green, run on demand.
test-eval:
	@uv run python -m pytest tests/evaluation/ -m "not integration" -q

# REQUIRES PERSONAL_AGENT_EVAL=1 — fires 100+ LLM calls (~60-90 min, GPU-intensive).
# Do NOT run from an AI agent session without explicit user instruction.
eval:
	@PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run $(EVAL_ARGS)

# ─── Agent self-diagnosis recovery ───────────────────────────────────────────
# Wave 1.1 — survey ES + Neo4j; reuses ConsolidationQualityMonitor.
eval-recovery-survey:  ## Run the recovery survey (no /chat traffic). DAYS=7 default.
	@uv run python scripts/eval/recovery_survey.py --days $(or $(DAYS),7)

# Wave 1.2 — drive prompts through /chat and capture per-trace artifacts.
# Args: RUN=<id> required. PROFILE=baseline|recovery (metadata only). PROMPT=<id> for single-prompt.
eval-recovery:  ## Run the recovery harness. RUN=<id> required.
	@if [ -z "$(RUN)" ]; then echo "RUN=<id> is required"; exit 2; fi
	@uv run python scripts/eval/recovery_harness.py \
		--run-id $(RUN) \
		--profile $(or $(PROFILE),baseline) \
		$(if $(PROMPT),--prompt $(PROMPT))

# ─── Phase D — Skill Routing Eval ────────────────────────────────────────────
# Run a single cell: make eval-skill-routing CELL=cloud-keyword RUN=run1
# Analyse: make eval-skill-routing-analyse CELL=cloud-keyword RUN=run1
# Run all available cloud cells: make eval-skill-routing-cloud RUN=<id>

SKILL_EVAL_DIR := telemetry/evaluation/EVAL-skill-routing-2026-05
SKILL_EVAL_PROMPTS := $(SKILL_EVAL_DIR)/prompts.yaml

eval-skill-routing:  ## Run one skill routing eval cell. CELL=<id> RUN=<id> required.
	@if [ -z "$(CELL)" ] || [ -z "$(RUN)" ]; then echo "CELL=<id> RUN=<id> required"; exit 2; fi
	@CELL=$(CELL); \
	 PROFILE=$$(uv run python -c "import yaml; m=yaml.safe_load(open('$(SKILL_EVAL_DIR)/matrix.yaml')); c=next(x for x in m['cells'] if x['id']=='$$CELL'); print(c['profile'])"); \
	 SRM=$$(uv run python -c "import yaml; m=yaml.safe_load(open('$(SKILL_EVAL_DIR)/matrix.yaml')); c=next(x for x in m['cells'] if x['id']=='$$CELL'); print(c.get('env',{}).get('AGENT_SKILL_ROUTING_MODE',''))"); \
	 echo "Running cell $$CELL (profile=$$PROFILE, skill_routing_mode=$$SRM, run=$(RUN))"; \
	 uv run python scripts/eval/recovery_harness.py \
		--run-id $(CELL)-$(RUN) \
		--profile $$PROFILE \
		--prompts $(SKILL_EVAL_PROMPTS) \
		--out $(SKILL_EVAL_DIR)/$(CELL)-$(RUN) \
		--chat-url http://localhost:$(SERVICE_PORT)/chat \
		$(if $$SRM,--skill-routing-mode $$SRM) \
		$(if $(PROMPT),--prompt $(PROMPT))

eval-skill-routing-analyse:  ## Analyse a skill routing eval run. CELL=<id> RUN=<id> required.
	@if [ -z "$(CELL)" ] || [ -z "$(RUN)" ]; then echo "CELL=<id> RUN=<id> required"; exit 2; fi
	@uv run python scripts/eval/skill_routing_analysis.py \
		--run-dir $(SKILL_EVAL_DIR)/$(CELL)-$(RUN)

eval-skill-routing-cloud:  ## Run all 3 cloud cells sequentially. RUN=<id> required.
	@if [ -z "$(RUN)" ]; then echo "RUN=<id> is required"; exit 2; fi
	@for CELL in cloud-keyword cloud-hybrid cloud-model-decided; do \
		echo "=== Cell: $$CELL ==="; \
		$(MAKE) eval-skill-routing CELL=$$CELL RUN=$(RUN); \
		$(MAKE) eval-skill-routing-analyse CELL=$$CELL RUN=$(RUN); \
	done

# Flexible test targets (run from project root so uv uses project env)
test-file:
	@uv run pytest $(or $(FILE),tests/test_tools/test_self_telemetry.py)

test-verbose:
	@uv run pytest -m "not integration" -v

test-k:
	@uv run pytest -m "not integration" -k "$(K)"

test-cov:
	@uv run pytest -m "not integration" --cov=src/personal_agent --cov-report=term-missing

# ─── Sandbox image ────────────────────────────────────────────────────────────

sandbox-build:  ## Build the Python sandbox image for run_python
	docker build -t seshat-sandbox-python:0.1 docker/sandbox/ -f docker/sandbox/Dockerfile.python

# ─── One-shot maintenance ─────────────────────────────────────────────────────

backfill-participated-in:  ## one-shot backfill of (:Person)-[:PARTICIPATED_IN]->(:Turn) edges (FRE-343)
	uv run python -m scripts.backfill_participated_in

# ─── Joinability probe (ADR-0074 Phase 5 / FRE-376) ──────────────────────────

joinability-probe:  ## Run one joinability probe walk against the current substrates
	uv run python -m scripts.monitors.joinability_probe $(ARGS)

joinability-status: ## 7-day green-gate verdict from agent-monitors-joinability-* (exit 0 on green)
	uv run python -m personal_agent.observability.joinability.status_cli $(ARGS)

cache-erosion-status: ## Jaccard prefix-hash stability for monitored callsites (exit 0 = stable)
	uv run python -m scripts.monitors.cache_erosion_monitor $(ARGS)

# ─── Prompt corpus ────────────────────────────────────────────────────────────

render-prompt-corpus:
	@uv run python scripts/render_prompt_corpus.py

# ─── Code quality ─────────────────────────────────────────────────────────────

mypy:
	@uv run mypy src/

ruff-check:
	@uv run ruff check src/

ruff-format:
	@uv run ruff format src/

# ─── Test infrastructure (FRE-375) ───────────────────────────────────────────
# Isolated substrate for the test suite (separate ports and volumes from prod).
# See docker-compose.test.yml for port mapping.

TEST_COMPOSE := docker compose -f docker-compose.test.yml

test-infra-up:          ## Start isolated test infra (Postgres:5433, ES:9201, Neo4j:7688)
	@$(TEST_COMPOSE) up -d

test-infra-down:        ## Stop and remove test infra containers
	@$(TEST_COMPOSE) down

test-infra-reset:       ## Stop, remove containers AND volumes (full reset)
	@$(TEST_COMPOSE) down -v

test-infra-ps:          ## Show test infra container status
	@$(TEST_COMPOSE) ps

# ─── Eval infrastructure (FRE-375) ───────────────────────────────────────────
# The eval stack uses docker-compose.eval.yml on top of docker-compose.cloud.yml.
# After FRE-375, eval services have their own isolated substrate.

eval-infra-up:          ## Start eval infra (requires eval.yml rewire)
	@docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml up -d

eval-infra-down:        ## Stop eval infra
	@docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml down seshat-gateway-control seshat-gateway-treatment
