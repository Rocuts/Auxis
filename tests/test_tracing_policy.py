"""Anti-goal #6, enforced against the dependency graph rather than trusted.

The anti-goal forbids the AWS X-Ray SDK, which entered maintenance mode in
February 2026. Its earlier wording recommended Powertools Tracer as the
replacement, which was self-defeating: ``aws-lambda-powertools``'s published
metadata gates ``aws-xray-sdk<3.0.0,>=2.8.0`` on ``extra == "tracer" or
extra == "all"``, so choosing Tracer *installs* the forbidden SDK. ADR 013
resolves it; these tests are what make the resolution checkable, so a
lockfile grep and the written policy can never drift apart.

Keyless and dependency-free on purpose: this must run in every environment,
including the lean ones that skip the infra suite.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "uv.lock"

#: The extras whose metadata pulls aws-xray-sdk.
FORBIDDEN_POWERTOOLS_EXTRAS = ("tracer", "all")


def _declared_requirements() -> list[str]:
    """Every requirement string pyproject declares, across the project
    dependencies, all optional-dependency extras, and all dependency groups.

    Parsed rather than grepped: an earlier text-search version of this check
    failed on its own explanatory comment, which is a false positive worth
    not shipping — a package NAMED in prose is not a package DEPENDED ON.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    requirements: list[str] = list(data.get("project", {}).get("dependencies", []))
    for extra in data.get("project", {}).get("optional-dependencies", {}).values():
        requirements.extend(extra)
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(group)
    return requirements


def _sources() -> list[Path]:
    return [
        path
        for path in (REPO_ROOT / "src").rglob("*.py")
        if "cdk.out" not in path.parts and "__pycache__" not in path.parts
    ]


class TestXRaySdkIsAbsent:
    def test_the_sdk_is_not_in_the_lockfile(self) -> None:
        """The resolved graph, not the declared one: a transitive pull is
        exactly the failure mode the old wording invited. The lockfile is
        generated and carries no prose, so a text search is sound here."""
        assert "aws-xray-sdk" not in LOCKFILE.read_text().lower()

    def test_the_sdk_is_not_declared(self) -> None:
        offenders = [r for r in _declared_requirements() if "aws-xray-sdk" in r.lower()]
        assert not offenders, offenders

    def test_nothing_imports_it(self) -> None:
        offenders = [
            path.relative_to(REPO_ROOT)
            for path in _sources()
            if re.search(r"^\s*(import|from)\s+aws_xray_sdk", path.read_text(), re.MULTILINE)
        ]
        assert not offenders, f"aws_xray_sdk imported by {offenders}"


class TestPowertoolsMayNotCarryTracer:
    """Powertools itself stays permitted — it is the CLAUDE.md Lambda toolkit,
    narrowed to Logger (ADR 013 addendum), and its base distribution requires
    only ``jmespath`` and ``typing-extensions``. What is forbidden is the pair
    of extras that drag the SDK in with it."""

    def test_no_forbidden_extra_is_requested(self) -> None:
        declared = "\n".join(_declared_requirements())
        for match in re.finditer(r"aws-lambda-powertools\s*\[([^\]]*)\]", declared):
            requested = {part.strip().lower() for part in match.group(1).split(",")}
            forbidden = requested.intersection(FORBIDDEN_POWERTOOLS_EXTRAS)
            assert not forbidden, (
                f"aws-lambda-powertools{sorted(forbidden)} pulls aws-xray-sdk "
                "(anti-goal #6, ADR 013)"
            )


class TestPowertoolsIsActuallyUsed:
    """The other half of the claim-vs-lockfile check that produced ADR 013's
    addendum: a toolkit named in the settled-constraints table but imported
    nowhere is the same defect as a docstring that contradicts the lockfile,
    only pointing the other way."""

    def test_the_declared_toolkit_is_imported_by_the_handlers(self) -> None:
        declared = "\n".join(_declared_requirements())
        assert "aws-lambda-powertools" in declared
        handlers = (REPO_ROOT / "src" / "tax_tables" / "aws" / "handlers.py").read_text()
        assert "from aws_lambda_powertools import Logger" in handlers

    def test_the_toolkit_never_reaches_the_shared_dependency_list(self) -> None:
        """It belongs to the `aws` extra (and its dev mirror), never to the
        project dependencies that every target installs — the same rule that
        keeps boto3, mangum, and pytesseract out of the Vercel bundle."""
        data = tomllib.loads(PYPROJECT.read_text())
        shared = data["project"]["dependencies"]
        assert not [r for r in shared if "powertools" in r.lower()]
