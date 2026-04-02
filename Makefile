# All targets assume the current working directory is the project root.
# Run from project root (e.g. `cd /path/to/personal_agent`) so uv finds pyproject.toml and .venv.

.PHONY: infra-up infra-down dev stop logs test test-integration test-all test-file test-verbose test-k test-cov eval mypy ruff-check ruff-format help

help:
	@echo "Run from project root so uv uses .venv (e.g. cd /path/to/personal_agent)."
	@echo ""
	@echo "Usage:"
	@echo "  make infra-up         Start Docker infrastructure (PostgreSQL, Elasticsearch, Neo4j, Kibana, SearXNG)"
	@echo "  make infra-down       Stop and remove Docker containers"
	@echo "  make dev              Start the agent service with hot-reload (run 'make infra-up' first)"
	@echo "  make stop             Stop Docker containers (preserves data volumes)"
	@echo "  make logs             Tail Docker service logs"
	@echo ""
	@echo "  make test             Run fast unit/mock tests only — SAFE, no LLM server needed"
	@echo "  make test-integration Run integration tests (requires PERSONAL_AGENT_INTEGRATION=1 + live LLM)"
	@echo "  make test-all         Run the complete test suite (safe unit tests only)"
	@echo "  make eval             Run eval harness (requires PERSONAL_AGENT_EVAL=1 + live agent + LLM)"
	@echo "  make test-file        Run tests for a file (FILE=tests/path/to/test_file.py)"
	@echo "  make test-verbose     Run unit tests with verbose output"
	@echo "  make test-k           Run tests matching pattern (K=pattern)"
	@echo "  make test-cov         Run unit tests with coverage report"
	@echo ""
	@echo "  make mypy             Type-check with mypy"
	@echo "  make ruff-check       Lint with ruff check"
	@echo "  make ruff-format      Format with ruff format"
	@echo ""
	@echo "  ⚠  AI AGENTS: Use 'make test' only. Do NOT run test-integration or eval without"
	@echo "     explicit user instruction — they fire live LLM calls and will overload the GPU."

infra-up:
	@docker compose up -d
	@bash scripts/init-services.sh

infra-down:
	@docker compose down

dev:
	@uv run uvicorn personal_agent.service.app:app --reload --port 9000

stop:
	@docker compose stop

logs:
	@docker compose logs -f

# Unit/integration test targets (python -m pytest ensures venv's pytest is used)
#
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

mypy:
	@uv run mypy src/

ruff-check:
	@uv run ruff check src/

ruff-format:
	@uv run ruff format src/
