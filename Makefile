.PHONY: check lint typecheck test fmt db-up db-down accuracy api openapi diagrams \
	fanout-lock fanout-unlock

check: lint typecheck test

# The Phase 2 gate: end-to-end accuracy against the real mapping API.
# Needs ANTHROPIC_API_KEY (or SCHEMA_MAPPER_*) in .env; skips without it.
# -p loads the conformance reporter, which prints the measured schema-failure
# and retry rates under the accuracy table. It is a print-only plugin and the
# harness itself is untouched.
accuracy:
	uv run --env-file .env pytest -p tax_tables.observability.pytest_plugin \
		tests/accuracy/test_harness.py::test_end_to_end_accuracy -s -v

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
	# The unit-test database is separate from the pipeline database, so a
	# `make check` can never drop a live pipeline run's data. See
	# tests/conftest.py — this separation exists because it happened.
	@docker compose exec -T db psql -U tax -d postgres -tc \
		"SELECT 1 FROM pg_database WHERE datname='tax_test'" | grep -q 1 || \
		docker compose exec -T db psql -U tax -d postgres -c \
		"CREATE DATABASE tax_test OWNER tax"

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

# Hold the pipeline database for a long-running run. While the sentinel
# exists, tests/conftest.py refuses to drop any schema, so a concurrent
# `make check` fails loudly instead of destroying the run's data.
fanout-lock:
	@touch .fanout-active && echo "pipeline database held (.fanout-active)"

fanout-unlock:
	@rm -f .fanout-active && echo "pipeline database released"
