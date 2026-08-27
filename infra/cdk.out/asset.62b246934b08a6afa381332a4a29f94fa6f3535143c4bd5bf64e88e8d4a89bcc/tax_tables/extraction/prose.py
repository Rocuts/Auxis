"""Prose-block kind heuristic, shared by every extractor adapter.

The classification is advisory — reporting convenience only. The full text
of every block always travels with the grid regardless of ``kind``, so a
misclassified block loses nothing (anti-goal #8 is about content, and the
content is never filtered by kind).
"""

from __future__ import annotations

import re

from tax_tables.extraction.model import ProseKind

#: Footnote openers seen across the corpus: "(a) ...", "* ...", "NOTE. ...",
#: "Notes: ...", "Source: ...".
_FOOTNOTE_RE = re.compile(r"^(\(\w{1,2}\)|\*|NOTE\b|Notes?\s*[:.]|Source:)", re.IGNORECASE)

_MAX_HEADING_LENGTH = 90


def classify_block(text: str) -> ProseKind:
    first_line = text.split("\n", 1)[0].strip()
    if _FOOTNOTE_RE.match(first_line):
        return ProseKind.FOOTNOTE
    if (
        "\n" not in text.strip()
        and len(first_line) <= _MAX_HEADING_LENGTH
        and not first_line.endswith(".")
    ):
        return ProseKind.HEADING
    return ProseKind.BODY
