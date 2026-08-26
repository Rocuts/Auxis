"""Adapters behind config: the same code picks a different target's
collaborators from the environment, and an unknown value is loud.

The failure this guards is specific. If an unset or misspelled engine
silently fell back to a local binary that is not installed on the target,
the scanned document would extract to nothing and the run would report an
empty document at high confidence — anti-goal #8's silent loss, wearing a
success badge.
"""

from __future__ import annotations

import pytest

from tax_tables.adapters.pdfplumber_extractor import PdfplumberExtractor
from tax_tables.api.runner import runner_from_env
from tax_tables.ports.jobs import NullJobRunner
from tax_tables.service.jobs import ocr_extractor


class TestOcrEngineSelection:
    def test_default_is_the_local_engine(self) -> None:
        assert type(ocr_extractor({})).__name__ == "TesseractExtractor"

    def test_vercel_selects_the_vision_adapter(self) -> None:
        extractor = ocr_extractor(
            {"EXTRACTION_OCR_ENGINE": "vision", "ANTHROPIC_API_KEY": "sk-test"}
        )
        assert type(extractor).__name__ == "AnthropicVisionExtractor"
        assert extractor.engine.startswith("vision:")

    def test_aws_selects_textract(self) -> None:
        assert type(ocr_extractor({"EXTRACTION_OCR_ENGINE": "textract"})).__name__ == (
            "TextractExtractor"
        )

    def test_case_and_whitespace_are_tolerated(self) -> None:
        assert type(
            ocr_extractor({"EXTRACTION_OCR_ENGINE": "  Vision  ", "ANTHROPIC_API_KEY": "k"})
        ).__name__ == ("AnthropicVisionExtractor")

    def test_an_unknown_engine_raises_rather_than_falling_back(self) -> None:
        with pytest.raises(ValueError, match="unknown EXTRACTION_OCR_ENGINE"):
            ocr_extractor({"EXTRACTION_OCR_ENGINE": "tesseractt"})

    def test_the_digital_adapter_is_never_the_configurable_one(self) -> None:
        """Only the OCR branch varies by target; the $0 deterministic path is
        the same pdfplumber everywhere, which is what makes the cost claim
        target-independent."""
        assert PdfplumberExtractor().engine == "pdfplumber"


class TestJobRunnerSelection:
    def test_default_enqueues_only(self) -> None:
        assert isinstance(runner_from_env({}), NullJobRunner)

    def test_vercel_selects_the_self_kicking_runner(self) -> None:
        runner = runner_from_env({"JOB_RUNNER": "vercel", "CRON_SECRET": "s", "VERCEL_URL": "h"})
        assert type(runner).__name__ == "VercelJobRunner"

    def test_an_unknown_runner_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown JOB_RUNNER"):
            runner_from_env({"JOB_RUNNER": "queues"})
