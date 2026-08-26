"""Page rasterization, shared by every pixel-licensed extractor.

Two adapters need a PDF page as an image — Textract on the AWS target and
the vision-OCR adapter on Vercel — and they must render it *identically*, or
their outputs stop being comparable and the accuracy harness starts measuring
the renderer instead of the engine. One implementation, imported by both.

Resolution is 300 DPI, measured rather than assumed on this corpus: 150 DPI
breaks the strokes of a roughly 200 DPI scan, and 400+ magnifies its JPEG
artifacts. Textract documents a 150 DPI floor, so 300 clears it with margin.
The render is RGB, not grayscale: the local OCR adapter measured grayscale
actively misreading "15 percent" as "|S percent", and there is no reason to
hand a hosted engine a worse image than a local one.
"""

from __future__ import annotations

import io
from typing import Any

#: See the module docstring — this number is a measurement, not a default.
RENDER_DPI = 300
PDF_POINTS_PER_INCH = 72


def render_page_png(
    pdf: Any,
    number: int,
    *,
    max_bytes: int | None = None,
    limit_label: str = "engine",
) -> tuple[bytes, int, int]:
    """Render one 1-based page to PNG bytes, with the image's pixel size.

    ``max_bytes`` is the caller's transport limit. Exceeding it raises here,
    naming the page and the limit, rather than surfacing later as an opaque
    error from someone else's SDK — and never by silently downscaling the
    evidence, which would make an unreadable page look like a readable one.
    """
    page = pdf[number - 1]
    image = page.render(scale=RENDER_DPI / PDF_POINTS_PER_INCH).to_pil().convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    if max_bytes is not None and len(payload) > max_bytes:
        raise ValueError(
            f"page {number} renders to {len(payload)} bytes at {RENDER_DPI} DPI, over the "
            f"{max_bytes}-byte {limit_label} limit"
        )
    return payload, int(image.width), int(image.height)
