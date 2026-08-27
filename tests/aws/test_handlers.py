"""Unit tests for the AWS deploy-time contract: every handler the CDK stack
addresses is real, and the split-step pipeline preserves the shared
pipeline's invariants. The DB-backed cases run against the docker Postgres
like the pipeline suite; nothing here touches the network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from tax_tables.adapters.postgres import PostgresRecordRepository
from tax_tables.aws import handlers
from tax_tables.domain.records import CanonicalRecord, FilingStatus, RecordType, ReviewStatus
from tax_tables.extraction.model import ExtractedDocument
from tax_tables.ports.adjudicator import Adjudication, ReviewItem
from tax_tables.ports.mapper import MappingCost
from tests.api.conftest import tiny_pdf
from tests.conftest import TEST_DSN, reset_database

# Deliberately not imported from tests.infra: the template cross-check needs
# only the committed JSON, never the aws_cdk import that module skips on.
INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"

#: Long enough to clear the router's text-layer threshold: this document
#: must route deterministic, never to OCR.
_PDF_TEXT = "Synthetic tax table for the AWS handler contract test, tax year 2026"


def _record(lower: int, upper: int | None, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source_page": 1,
        "table_id": "p1_t0",
        "record_type": RecordType.ORDINARY_INCOME_BRACKET,
        "jurisdiction": "ZZ-AWS",
        "filing_status": FilingStatus.SINGLE,
        "tax_year": 2026,
        "lower_bound": lower,
        "upper_bound": upper,
        "rate": Decimal("0.1"),
        "currency": "USD",
        "attrs": {"source_table_label": "table_aws"},
        "confidence": Decimal("0.95"),
    }
    values.update(overrides)
    return CanonicalRecord(**values).model_dump(mode="json")


def _seed_document_and_job() -> tuple[UUID, UUID, PostgresRecordRepository]:
    reset_database()
    repository = PostgresRecordRepository(TEST_DSN)
    handle = repository.register_document(sha256="ab" * 32, filename="aws_case.pdf", byte_size=9)
    repository.store_blob(handle.id, tiny_pdf(text=_PDF_TEXT))
    row = repository.connection.execute(
        "INSERT INTO jobs (document_id) VALUES (%s) RETURNING id", (handle.id,)
    ).fetchone()
    assert row is not None
    repository.connection.commit()
    return handle.id, row[0], repository


def _extracted_payload() -> dict[str, Any]:
    from tests.mapping.test_pipeline import _extracted

    return _extracted().model_dump(mode="json")


class TestTemplateContract:
    def test_every_template_handler_resolves_to_a_callable(self) -> None:
        """The audit's 'handlers do not exist' critical, foreclosed: each
        AWS::Lambda::Function handler string in the committed template must
        resolve to a real callable in this package."""
        template = json.loads((INFRA_DIR / "cdk.out" / "TaxTables.template.json").read_text())
        handler_strings = [
            resource["Properties"]["Handler"]
            for resource in template["Resources"].values()
            if resource["Type"] == "AWS::Lambda::Function"
            and str(resource["Properties"].get("Handler", "")).startswith("tax_tables.")
        ]
        assert len(handler_strings) == 6
        for spec in handler_strings:
            module_path, _, attr = spec.rpartition(".")
            assert module_path == "tax_tables.aws.handlers"
            assert callable(getattr(handlers, attr)), spec


class TestProxyDsn:
    def test_dsn_uses_the_app_role_and_requires_tls(self) -> None:
        env = {
            "DB_PROXY_ENDPOINT": "proxy.example.internal",
            "DB_PORT": "5432",
            "DB_NAME": "tax",
            "DB_USER": "app_ingest",
        }
        dsn = handlers.proxy_dsn(env, token="tok/with=chars")
        assert dsn.startswith("postgresql://app_ingest:tok%2Fwith%3Dchars@")
        assert dsn.endswith("proxy.example.internal:5432/tax?sslmode=require")


class _FakeS3:
    def __init__(self, body: bytes | None = None) -> None:
        self._body = body
        self.put_calls: list[dict[str, Any]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        assert self._body is not None
        return {"Body": _Body(self._body)}

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)


class _ExplodingTextract:
    """A digital document must never reach the paid OCR engine — even on
    AWS (the router's core economics)."""

    def analyze_document(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Textract must not be called for a text-layer document")


class TestExtractDocument:
    def test_digital_document_costs_zero_textract_calls(self) -> None:
        document_id, job_id, repository = _seed_document_and_job()
        with repository:
            result = handlers.extract_document(
                {
                    "job_id": str(job_id),
                    "document_id": str(document_id),
                    "bucket": "b",
                    "key": "k",
                    "filename": "aws_case.pdf",
                },
                s3=_FakeS3(tiny_pdf(text=_PDF_TEXT)),
                textract=_ExplodingTextract(),
                repository=repository,
            )
            status = repository.connection.execute(
                "SELECT status, attempt FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
        extracted = ExtractedDocument.model_validate(result["extracted"])
        assert extracted.cost.usd == Decimal(0)
        assert [page.method.value for page in extracted.pages] == ["deterministic_text"]
        assert result["job_id"] == str(job_id)  # payload rides through
        assert status == ("running", 1)


class _FakeSfn:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.executions: list[dict[str, Any]] = []

    def start_execution(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("simulated Step Functions outage")
        self.executions.append(kwargs)


class TestStepFunctionsJobRunner:
    def test_notify_stages_blob_and_starts_one_execution(self) -> None:
        document_id, job_id, repository = _seed_document_and_job()
        s3, sfn = _FakeS3(), _FakeSfn()
        env = {"DOCUMENTS_BUCKET": "docs-bucket", "PIPELINE_STATE_MACHINE_ARN": "arn:sm"}
        with repository:
            handlers.StepFunctionsJobRunner(env=env, s3=s3, sfn=sfn, repository=repository).notify(
                job_id
            )
        (put,) = s3.put_calls
        assert put["Bucket"] == "docs-bucket"
        assert put["Key"] == f"documents/{document_id}.pdf"
        assert put["Body"].startswith(b"%PDF")
        (execution,) = sfn.executions
        payload = json.loads(execution["input"])
        (item,) = payload["documents"]
        assert item == {
            "job_id": str(job_id),
            "document_id": str(document_id),
            "bucket": "docs-bucket",
            "key": f"documents/{document_id}.pdf",
            "filename": "aws_case.pdf",
        }

    def test_notify_never_raises_past_the_caller(self) -> None:
        _document_id, job_id, repository = _seed_document_and_job()
        env = {"DOCUMENTS_BUCKET": "docs-bucket", "PIPELINE_STATE_MACHINE_ARN": "arn:sm"}
        with repository:
            handlers.StepFunctionsJobRunner(
                env=env, s3=_FakeS3(), sfn=_FakeSfn(fail=True), repository=repository
            ).notify(job_id)
            status = repository.connection.execute(
                "SELECT status FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
        assert status == ("queued",)  # still recoverable, never lost


class TestPersistRecords:
    def test_persist_half_preserves_the_accounting_invariant(self) -> None:
        document_id, job_id, repository = _seed_document_and_job()
        event = {
            "job_id": str(job_id),
            "document_id": str(document_id),
            "extracted": _extracted_payload(),
            "mapping": {
                "records": [
                    _record(0, 9000),
                    _record(9001, None),
                    _record(500, 8000, rate=Decimal("0.2")),  # overlap -> REJECT
                ],
                "issues": [
                    {
                        "source_page": 1,
                        "table_id": "p1_t0",
                        "row_index": 0,
                        "col_index": 1,
                        "raw_value": "??",
                        "reason": "unreadable cell",
                    }
                ],
                "cost": None,
            },
            "verification": {
                "verdicts": [
                    {"record_index": 0, "verdict": "disputed", "reason": "cell disagrees"},
                    {"record_index": 1, "verdict": "confirmed", "reason": None},
                    {"record_index": 2, "verdict": "confirmed", "reason": None},
                ],
                "notes": [],
                "cost": None,
            },
        }
        with repository:
            result = handlers.persist_records(event, repository=repository)

        assert result["persisted"] == 2
        with psycopg.connect(TEST_DSN) as conn:
            statuses: dict[int, str] = dict(
                conn.execute("SELECT lower(bracket), review_status FROM records").fetchall()
            )
            reasons = sorted(
                row[0] for row in conn.execute("SELECT reason FROM review_queue").fetchall()
            )
            job = conn.execute(
                "SELECT status, records_persisted, review_count FROM jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
        # The disputed record persisted flagged; the overlap was rejected
        # into the queue; the mapper issue rode along; the job closed with
        # honest counts.
        assert statuses[0] == ReviewStatus.NEEDS_REVIEW.value
        assert any(reason.startswith("verifier_dispute:") for reason in reasons)
        assert any(reason.startswith("bracket_overlap:") for reason in reasons)
        assert "mapping: unreadable cell" in reasons
        assert job is not None and job[0] == "succeeded"
        assert job[1] == 2 and job[2] == len(reasons)


class TestAdjudicateQueue:
    def test_pass_runs_with_the_shared_containment(self) -> None:
        document_id, job_id, repository = _seed_document_and_job()

        class _Confident:
            def adjudicate(self, item: ReviewItem, extracted: ExtractedDocument) -> Adjudication:
                return Adjudication(
                    item_id=item.id,
                    resolution="the grid settles it",
                    citations=[
                        {
                            "kind": "cell",
                            "page": 1,
                            "table_id": "p1_t0",
                            "row": 0,
                            "col": 0,
                            "prose_index": None,
                        }
                    ],
                    confidence=Decimal("0.97"),
                    citations_valid=True,
                    cost=MappingCost(engine="bedrock-test", api_calls=1),
                )

        from tests.mapping.test_pipeline import _record

        # Two records, only ONE of which reaches the fact table — the queue
        # rows must carry the records they stand for, exactly as
        # review_queue_entry writes them, or eligibility has nothing to look
        # up. The absent one is what proves the AWS path inherits the
        # presence gate rather than re-deriving it from the reason prefix.
        present = _record(0, 9000, confidence=Decimal("0.5"))
        absent = _record(500, 8000)
        with repository:
            repository.ingest(document_id, [present])
            repository.queue_review(
                document_id,
                [
                    {
                        "reason": "confidence_floor: 0.5 below 0.7",
                        "source_page": 1,
                        "raw_value": present.model_dump_json(),
                    },
                    {
                        "reason": "bracket_overlap: [500,8000] overlaps",
                        "source_page": 1,
                        "raw_value": absent.model_dump_json(),
                    },
                ],
            )
            result = handlers.adjudicate_queue(
                {
                    "job_id": str(job_id),
                    "document_id": str(document_id),
                    "extracted": _extracted_payload(),
                },
                repository=repository,
                adjudicator=_Confident(),
            )
        dispositions = sorted(o["disposition"] for o in result["adjudications"])
        # FLAG-born item auto-resolves; the REJECT-born item only ever gets
        # a stored proposal — the shared eligibility rule, unchanged on AWS.
        assert dispositions == ["auto_resolved", "proposal_stored"]


class TestApiHandler:
    def test_openapi_served_through_mangum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_PROXY_ENDPOINT", "proxy.example.internal")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_NAME", "tax")
        monkeypatch.setenv("DB_USER", "app_ingest")
        monkeypatch.setenv("API_KEY", "aws-test-key")
        monkeypatch.setenv("CRON_SECRET", "aws-test-cron")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(handlers, "auth_token", lambda env=None: "iam-token")

        event = {
            "httpMethod": "GET",
            "path": "/openapi.json",
            "headers": {"host": "api.example"},
            "multiValueHeaders": {},
            "queryStringParameters": None,
            "multiValueQueryStringParameters": None,
            "body": None,
            "isBase64Encoded": False,
            "requestContext": {
                "resourcePath": "/{proxy+}",
                "httpMethod": "GET",
                "path": "/openapi.json",
                "identity": {"sourceIp": "127.0.0.1"},
            },
            "resource": "/{proxy+}",
            "pathParameters": {"proxy": "openapi.json"},
            "stageVariables": None,
        }

        class _Context:
            function_name = "IngestApi"
            memory_limit_in_mb = 1024
            invoked_function_arn = "arn:aws:lambda:eu:1:function:IngestApi"
            aws_request_id = "req-1"

        response = handlers.api(event, _Context())
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["openapi"].startswith("3.1")


class TestMarkJobFailed:
    """The failure twin of ``_finish_job``.

    ``tolerated_failure_percentage=100`` makes the Distributed Map absorb a
    failed document so its siblings finish — AWS: "the workflow won't fail
    even if all child workflow executions fail." That is a deliberate
    transport choice, and it is only safe because the job row records the
    failure. This handler is what writes it.
    """

    def test_a_failed_step_closes_the_job_with_its_reason(self) -> None:
        _document_id, job_id, repository = _seed_document_and_job()
        repository.connection.execute(
            "UPDATE jobs SET status = 'running', started_at = now() WHERE id = %s", (job_id,)
        )
        repository.connection.commit()
        event = {
            "job_id": str(job_id),
            "error": {
                "Error": "Lambda.Unknown",
                "Cause": '{"errorMessage": "Textract throttled", "errorType": "RuntimeError"}',
            },
        }
        with repository:
            handlers.mark_job_failed(event, repository=repository)

        with psycopg.connect(TEST_DSN) as conn:
            row = conn.execute(
                "SELECT status, error, finished_at FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
        assert row is not None
        status, error, finished_at = row
        assert status == "failed"
        assert finished_at is not None
        assert error["type"] == "pipeline_step_failed"
        assert error["error_class"] == "Lambda.Unknown"
        assert "Textract throttled" in error["message"]

    def test_a_job_that_already_succeeded_is_never_reopened(self) -> None:
        """AdjudicateStep runs *after* PersistStep. If adjudication fails,
        the records are already in the fact table and the job genuinely
        succeeded — the open queue items are the honest signal, and
        rewriting the job to 'failed' would misreport persisted data as
        lost."""
        _document_id, job_id, repository = _seed_document_and_job()
        repository.connection.execute(
            "UPDATE jobs SET status = 'succeeded', records_persisted = 7 WHERE id = %s", (job_id,)
        )
        repository.connection.commit()
        with repository:
            handlers.mark_job_failed(
                {"job_id": str(job_id), "error": {"Error": "States.TaskFailed", "Cause": "{}"}},
                repository=repository,
            )

        with psycopg.connect(TEST_DSN) as conn:
            row = conn.execute(
                "SELECT status, records_persisted, error FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
        assert row == ("succeeded", 7, None)

    def test_the_stored_cause_is_bounded(self) -> None:
        """A Step Functions Cause carries a Lambda stack trace. It is
        error text, not a value store — cap it rather than letting an
        unbounded string into a row the API serves."""
        _document_id, job_id, repository = _seed_document_and_job()
        repository.connection.execute("UPDATE jobs SET status = 'running' WHERE id = %s", (job_id,))
        repository.connection.commit()
        with repository:
            handlers.mark_job_failed(
                {"job_id": str(job_id), "error": {"Error": "E", "Cause": "x" * 10_000}},
                repository=repository,
            )
        with psycopg.connect(TEST_DSN) as conn:
            row = conn.execute("SELECT error FROM jobs WHERE id = %s", (job_id,)).fetchone()
        assert row is not None
        assert len(row[0]["message"]) <= handlers.MAX_STORED_CAUSE_CHARS
