"""Anti-goal #1, enforced mechanically instead of promised.

    "Never read fixtures/ground_truth.json from anywhere except
    tests/accuracy/. It is the test oracle. If any module under src/ imports,
    opens, or embeds values from it, the work is invalid. Every extracted
    value must be derived from the PDF itself. Assume this will be verified
    with grep."

So here is the grep, as a test. Two independent properties:

1. **No module under ``src/`` names the oracle**, by any spelling. Reading
   the sample PDFs is fine and expected; reading the answers is not.
2. **The deployed bundle does not ship it.** ``vercel.json``'s
   ``excludeFiles`` and ``.vercelignore`` both drop ``fixtures/``, so the
   oracle cannot travel to a live target even by accident.

The conventions legitimately adopt target-schema *encodings* — ``US-FED``,
``estate_or_trust``, the ``attribute_key`` slugs, the attribute key names,
the inclusive-bounds reading of ``Over $X`` (ADR 014 §8, §8a, §8c). Those are
names, agreed in writing and printed in no PDF. This test guards the other
half of that boundary: no per-record VALUE, and no runtime path to the file
that holds them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

#: Any way a module could name the oracle or reach into its contents.
#:
#: Deliberately NOT a ban on the string "fixtures": the CLI report tools take
#: a PDF path that defaults there, the Textract adapter's docstring cites the
#: recorded response fixture, and "registers no fixtures" is about pytest.
#: Reading the sample PDFs is the whole point of the exercise. What must never
#: appear is the ORACLE — the file of expected answers.
_FORBIDDEN = re.compile(r"ground_truth|expected_records")


class TestOracleIsolation:
    def test_no_source_module_references_the_oracle(self) -> None:
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if _FORBIDDEN.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{number}: {line.strip()}")
        assert not offenders, (
            "src/ must never reference the test oracle or its directory "
            "(anti-goal #1):\n" + "\n".join(offenders)
        )

    def test_the_oracle_is_not_importable_as_package_data(self) -> None:
        """It must not have been copied under src/ under any name."""
        strays = [p.relative_to(REPO_ROOT) for p in SRC.rglob("*.json")]
        assert not strays, f"no JSON data belongs under src/: {strays}"

    def test_the_deployed_bundle_excludes_the_fixtures(self) -> None:
        config = json.loads((REPO_ROOT / "vercel.json").read_text())
        excluded = json.dumps(config)
        assert "fixtures/**" in excluded, "vercel.json must exclude fixtures/ from the bundle"
        ignored = (REPO_ROOT / ".vercelignore").read_text().splitlines()
        assert any(line.strip().rstrip("/") == "fixtures" for line in ignored), (
            ".vercelignore must drop fixtures/"
        )
