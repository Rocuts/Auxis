"""What an adjudicator may close unattended, and on what evidence.

Both gates here were earned by one incident. On document 01 the verifier
raised a dispute whose asserted "actual" value was simply wrong — it claimed a
record held 257300 when the record held 257250 — and the adjudicator then
auto-resolved it at 0.98 confidence, in a rationale that simultaneously said
the record was "correct as persisted" and that it "must be corrected to 257250
(or removed)". A human following that literally would have deleted a correct
row, inside a closed audit trail.

Every model response involved was perfectly contract-conformant. The
conformance ledger saw nothing, because it measures whether a model can emit
the contract and never whether what it emitted is true.
"""

from __future__ import annotations

from tax_tables.extraction.model import (
    Cell,
    ExtractedDocument,
    ExtractedTable,
    ExtractionCost,
    ExtractionMethod,
    GridSource,
    PageExtraction,
    ProseBlock,
    ProseKind,
)
from tax_tables.ports.adjudicator import cited_evidence, resolution_is_supported
from tax_tables.validation.validators import (
    AUTO_RESOLVABLE_RULES,
    FLAG_RULES,
    RULE_CONFIDENCE_FLOOR,
    RULE_VERIFIER_DISPUTE,
    RULE_VERIFIER_UNAVAILABLE,
)


def _document() -> ExtractedDocument:
    table = ExtractedTable(
        page_number=1,
        table_id="p1_t0",
        bbox=(0.0, 0.0, 100.0, 50.0),
        grid_source=GridSource.RULED_LINES,
        rows=[
            [Cell(text="Rate"), Cell(text="Head of Household")],
            [Cell(text="20 percent"), Cell(text="Over $566,700")],
        ],
        column_count=2,
    )
    prose = ProseBlock(
        page_number=1,
        kind=ProseKind.FOOTNOTE,
        text="NOTE. The first-bracket rate is 10 percent.",
        bbox=(0.0, 60.0, 100.0, 70.0),
    )
    return ExtractedDocument(
        filename="05_capital_gains_preferential_rates_TY2025.pdf",
        sha256="ef" * 32,
        pages=[
            PageExtraction(
                page_number=1,
                width=612.0,
                height=792.0,
                method=ExtractionMethod.OCR,
                tables=[table],
                prose=[prose],
            )
        ],
        cost=ExtractionCost(engine="tesseract", wall_seconds=2.6),
    )


CELL = {"kind": "cell", "table_id": "p1_t0", "row": 1, "col": 1, "page": 1, "prose_index": None}
NOTE = {"kind": "prose", "page": 1, "prose_index": 0, "table_id": None, "row": None, "col": None}


class TestDefaultDeny:
    def test_a_dispute_born_item_can_never_auto_close(self) -> None:
        """A dispute is a SECOND opinion that something is wrong. A THIRD
        model agreeing with it is correlation, not corroboration."""
        assert RULE_VERIFIER_DISPUTE not in AUTO_RESOLVABLE_RULES

    def test_an_unverified_item_can_never_auto_close(self) -> None:
        assert RULE_VERIFIER_UNAVAILABLE not in AUTO_RESOLVABLE_RULES

    def test_the_pipeline_s_own_flags_still_can(self) -> None:
        assert RULE_CONFIDENCE_FLOOR in AUTO_RESOLVABLE_RULES

    def test_auto_resolvable_is_strictly_narrower_than_flag(self) -> None:
        assert AUTO_RESOLVABLE_RULES < FLAG_RULES


class TestCitationsMustCarryTheFigures:
    def test_a_figure_present_in_a_cited_cell_is_supported(self) -> None:
        assert resolution_is_supported("the bound is $566,700 as printed", [CELL], _document())

    def test_a_figure_in_no_cited_cell_blocks_the_close(self) -> None:
        """The hallucination case: a number with no basis in the evidence the
        model itself chose to cite."""
        assert not resolution_is_supported("the bound is $566,750", [CELL], _document())

    def test_a_documented_percent_transform_is_reachable(self) -> None:
        """ "10 percent" in the footnote supports a mapped 0.10 — the schema's
        own rate convention, not a licence to invent."""
        assert resolution_is_supported(
            "footnote states 10 percent; 0.10 is right", [NOTE], _document()
        )

    def test_a_documented_bracket_derivation_is_reachable(self) -> None:
        assert resolution_is_supported(
            "lower_bound 566701 follows the printed bound", [CELL], _document()
        )

    def test_two_steps_away_is_not_reachable(self) -> None:
        assert not resolution_is_supported("lower_bound 566703", [CELL], _document())

    def test_citing_nothing_supports_nothing(self) -> None:
        assert not resolution_is_supported("the bound is $566,700", [], _document())

    def test_a_resolution_with_no_figures_is_not_blocked_here(self) -> None:
        """This gate judges figures. A prose-only disposition is left to the
        confidence threshold and the default-deny rule."""
        assert resolution_is_supported("the dash means no tax is imposed", [CELL], _document())

    def test_grid_coordinates_are_addresses_not_claims(self) -> None:
        """A resolution that says where it looked must not be refused for
        saying it — "row 1 col 1" is not an assertion about a tax value."""
        assert resolution_is_supported(
            "cell p1_t0 row 1 col 1 on page 1 reads $566,700", [CELL], _document()
        )

    def test_a_dangling_citation_contributes_no_evidence(self) -> None:
        bad = {**CELL, "row": 99}
        assert cited_evidence([bad], _document()) == ""
        assert not resolution_is_supported("the bound is $566,700", [bad], _document())
