.PHONY: check lint typecheck test fmt db-up db-down accuracy

check: lint typecheck test

# The Phase 2 gate: end-to-end accuracy against the real mapping API.
# Needs ANTHROPIC_API_KEY (or SCHEMA_MAPPER_*) in .env; skips without it.
accuracy:
	uv run --env-file .env pytest tests/accuracy/test_harness.py::test_end_to_end_accuracy -s -v

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
