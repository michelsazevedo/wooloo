
.PHONY: sync run lint format typecheck test test-unit test-integration \
        migration-create migration-up migration-down migration-current \
        db-up db-down db-logs clean

ifneq (,$(wildcard .env))
	include .env
	export
endif


sync:
	uv sync

run:
	uv run uvicorn wooloo.main:asgi_app --reload --app-dir src

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src

test:
	uv run pytest

test-unit:
	uv run pytest tests/unit

test-integration:
	uv run pytest tests/integration

db-up:
	docker compose up -d

db-down:
	docker compose down

db-logs:
	docker compose logs -f

migration-create:
	@if [ -z "$(name)" ]; then \
		echo "Usage: make migration-create name=create_repositories_table"; \
		exit 1; \
	fi
	uv run alembic revision -m "$(name)"

migration-up:
	uv run alembic upgrade head

migration-down:
	uv run alembic downgrade -1

migration-current:
	uv run alembic current

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type f -name ".coverage" -delete