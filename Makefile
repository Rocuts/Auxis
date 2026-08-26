.PHONY: check lint typecheck test fmt db-up db-down accuracy api openapi diagrams

check: lint typecheck test

# The Phase 2 gate: end-to-end accuracy against the real mapping API.
# Needs ANTHROPIC_API_KEY (or SCHEMA_MAPPER_*) in .env; skips without it.
accuracy:
	uv run --env-file .env pytest tests/accuracy/test_harness.py::test_end_to_end_accuracy -s -v

lint:
	uv run ruff check src tests infra
	uv run ruff format --check src tests infra

typecheck:
	uv run mypy

test:
	uv run pytest

fmt:
	uv run ruff format src tests infra
	uv run ruff check --fix src tests infra

# Phase 4 gate sequence, offline by construction: cdk synth (cdk-nag runs
# as an aspect — an unsuppressed error fails the synth) + cfn-lint over the
# synthesized template. Runs with no AWS credentials.
synth:
	cd infra && npx --yes aws-cdk@2 synth --quiet

synth-check: synth
	uv run cfn-lint --config-file infra/.cfnlintrc infra/cdk.out/TaxTables.template.json

db-up:
	docker compose up -d --wait db

db-down:
	docker compose down -v

# Serve the API locally against .env (DATABASE_URL, API_KEY, CRON_SECRET).
api:
	uv run --env-file .env uvicorn tax_tables.api.main:app --port 8000

# Regenerate docs/openapi.yaml; a stale export fails the contract tests.
openapi:
	uv run python -m tax_tables.tools.export_openapi

# Phase 5 gate: every mermaid block in the README must parse under two
# Mermaid majors, which brackets whatever version GitHub bundles. Needs
# Node (mermaid-cli pulls a headless Chromium on first run).
diagrams:
	uv run python scripts/check_diagrams.py README.md
