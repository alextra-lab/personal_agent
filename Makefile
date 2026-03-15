# All targets assume the current working directory is the project root.
# Run from project root (e.g. `cd /path/to/personal_agent`) so uv finds pyproject.toml and .venv.

.PHONY: infra-up infra-down dev stop logs test test-integration test-all test-file test-verbose test-k test-cov mypy ruff-check ruff-format help

help:
	@echo "Run from project root so uv uses .venv (e.g. cd /path/to/personal_agent)."
	@echo ""
	@echo "Usage:"
	@echo "  make infra-up         Start Docker infrastructure (PostgreSQL, Elasticsearch, Neo4j, Kibana)"
	@echo "  make infra-down       Stop and remove Docker containers"
	@echo "  make dev              Start the agent service with hot-reload (run 'make infra-up' first)"
	@echo "  make stop             Stop Docker containers (preserves data volumes)"
	@echo "  make logs             Tail Docker service logs"
	@echo ""
	@echo "  make test             Run fast unit/mock tests only (no LLM server required)"
	@echo "  make test-integration Run integration tests that require a live LLM server"
	@echo "  make test-all         Run the complete test suite"
	@echo "  make test-file        Run tests for a file (FILE=tests/path/to/test_file.py)"
	@echo "  make test-verbose     Run unit tests with verbose output"
	@echo "  make test-k           Run tests matching pattern (K=pattern)"
	@echo "  make test-cov         Run unit tests with coverage report"
	@echo ""
	@echo "  make mypy             Type-check with mypy"
	@echo "  make ruff-check       Lint with ruff check"
	@echo "  make ruff-format      Format with ruff format"

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
test:
	@uv run python -m pytest -m "not integration" -q

test-integration:
	@uv run python -m pytest -m "integration" -q

test-all:
	@uv run python -m pytest -q

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
