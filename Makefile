.PHONY: infra-up infra-down dev stop logs test test-integration test-all help

help:
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

test:
	@uv run pytest -m "not integration" -q

test-integration:
	@uv run pytest -m "integration" -q

test-all:
	@uv run pytest -q
