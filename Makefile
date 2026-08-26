.PHONY: check lint typecheck test fmt db-up db-down

check: lint typecheck test

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy

test:
	uv run pytest

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

db-up:
	docker compose up -d --wait db

db-down:
	docker compose down -v
