"""License-clean PDF primitive utilities for ADR-0102 document ingestion (FRE-681).

Uses pypdfium2 (Apache-2.0/BSD-3) and Pillow (HPND) only.
PyMuPDF (AGPL) and pdf2image/Poppler (GPL) are explicitly excluded.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import pypdfium2 as pdfium
from PIL import Image


@dataclass(frozen=True)
class PageSalienceScore:
    """Per-page salience metrics used for budget-constrained page selection.

    Attributes:
        page_index: Zero-based page index within the source PDF.
        char_count: Number of non-whitespace characters extracted from the text layer.
        pixel_coverage: Fraction of non-white pixels in a low-DPI raster (0.0–1.0).
    """

    page_index: int
    char_count: int
    pixel_coverage: float


def extract_page_text(page: pdfium.PdfPage) -> str:
    """Extract all text content from a single PDF page.

    Args:
        page: An open pypdfium2 PdfPage.

    Returns:
        The full text of the page as a string, or an empty string if no text layer.
    """
    textpage = page.get_textpage()
    return cast(str, textpage.get_text_range())


def is_text_dense(text: str, floor: int = 100) -> bool:
    """Return True if the stripped text length meets or exceeds the density floor.

    Uses stripped length so that a page containing only whitespace or form-feeds
    is correctly classified as not dense.

    Args:
        text: Text extracted from a page (may contain whitespace).
        floor: Minimum character count (after stripping) to be considered dense.

    Returns:
        True when ``len(text.strip()) >= floor``.
    """
    return len(text.strip()) >= floor


def rasterize_page(page: pdfium.PdfPage, dpi: int = 150) -> Image.Image:
    """Render a PDF page to a PIL Image.

    Args:
        page: An open pypdfium2 PdfPage.
        dpi: Target resolution in dots per inch (default 150 for quality, 72 for scoring).

    Returns:
        A PIL Image in RGB mode.
    """
    # pypdfium2 scale parameter = pixels per PDF canvas unit (72 units/inch)
    bitmap = page.render(scale=dpi / 72)
    return cast(Image.Image, bitmap.to_pil())


def pixel_coverage_ratio(image: Image.Image) -> float:
    """Compute the fraction of non-white pixels in a PIL Image.

    A white pixel is one with value 255 in all channels (after grayscale conversion).
    This is the ink/content coverage proxy used by the salience scorer.

    Args:
        image: Any PIL Image (will be converted to grayscale internally).

    Returns:
        A float in [0.0, 1.0]; 0.0 = fully blank, 1.0 = fully inked.
    """
    gray = image.convert("L")
    hist = gray.histogram()  # 256-bucket count of pixel intensities
    total = sum(hist)
    if total == 0:
        return 0.0
    white_pixels = hist[255]
    return (total - white_pixels) / total


def score_page_salience(score: PageSalienceScore) -> float:
    """Compute a combined salience score from character count and pixel coverage.

    Both signals are normalised to a comparable scale:
    - ``char_count`` is divided by 1000 (so 1000 chars ≈ 1.0 unit).
    - ``pixel_coverage`` is used directly (already 0.0–1.0).

    A blank page (0 chars, 0.0 coverage) scores 0.0.
    A dense page (800 chars, 0.7 coverage) scores ~1.5.

    Args:
        score: A PageSalienceScore for a single page.

    Returns:
        A non-negative float; higher means more informative.
    """
    return score.char_count / 1000 + score.pixel_coverage


def select_pages_by_salience(
    scores: Sequence[PageSalienceScore],
    budget: int,
) -> list[int]:
    """Return the page indices of the highest-salience pages within the given budget.

    Pages are ranked by ``score_page_salience`` (descending). The returned list
    preserves the original document order (ascending page_index) so callers can
    build a sub-PDF without reordering.

    Args:
        scores: Per-page salience scores, one per page to consider.
        budget: Maximum number of pages to select. If >= len(scores), all are returned.

    Returns:
        A list of zero-based page indices in ascending document order.
    """
    if budget <= 0 or not scores:
        return []
    ranked = sorted(scores, key=score_page_salience, reverse=True)
    selected = ranked[:budget]
    # Restore document order
    return sorted(s.page_index for s in selected)


def get_outline_page_indices(pdf: pdfium.PdfDocument) -> frozenset[int]:
    """Extract the set of page indices referenced by the PDF's outline (TOC/bookmarks).

    Callers can use this as a bonus signal when ranking pages — pages explicitly
    named in the document's table of contents are presumed more substantive.

    Args:
        pdf: An open pypdfium2 PdfDocument.

    Returns:
        A frozenset of zero-based page indices that appear in the TOC;
        an empty frozenset if the document has no outline.
    """
    indices: set[int] = set()
    for bookmark in pdf.get_toc():
        dest = bookmark.get_dest()
        if dest is None:
            continue
        idx = dest.get_index()
        if idx is not None and idx >= 0:
            indices.add(idx)
    return frozenset(indices)


def compute_page_scores(
    pdf: pdfium.PdfDocument,
    raster_dpi: int = 72,
) -> list[PageSalienceScore]:
    """Compute a PageSalienceScore for every page in a PDF document.

    Extracts the text layer and rasterizes each page at ``raster_dpi`` to obtain
    the two salience proxies: stripped character count and pixel coverage ratio.

    Args:
        pdf: An open pypdfium2 PdfDocument.
        raster_dpi: Rasterization resolution for pixel-coverage scoring. Lower DPI
            is faster and sufficient for salience discrimination (default 72).

    Returns:
        One PageSalienceScore per page in document order.
    """
    scores: list[PageSalienceScore] = []
    for i, page in enumerate(pdf):
        text = extract_page_text(page)
        char_count = len(text.strip())
        img = rasterize_page(page, dpi=raster_dpi)
        coverage = pixel_coverage_ratio(img)
        scores.append(
            PageSalienceScore(
                page_index=i,
                char_count=char_count,
                pixel_coverage=coverage,
            )
        )
    return scores


def build_page_subset(src_bytes: bytes, page_indices: Sequence[int]) -> bytes:
    """Build a sub-PDF containing only the specified pages from a source PDF.

    Attempts to use ``PdfDocument.import_pages`` (vector-quality). If that raises
    (e.g. restricted/encrypted inputs), falls back to rasterizing the selected
    pages at 150 DPI and embedding them as images in a new PDF.

    Args:
        src_bytes: The full source PDF as raw bytes.
        page_indices: Zero-based page indices to include, in any order.

    Returns:
        The sub-PDF as raw bytes (empty bytes if page_indices is empty).
    """
    if not page_indices:
        return b""

    src = pdfium.PdfDocument(io.BytesIO(src_bytes))
    dst = pdfium.PdfDocument.new()
    try:
        dst.import_pages(src, pages=list(page_indices))
        buf = io.BytesIO()
        dst.save(buf)
        return buf.getvalue()
    except Exception:
        # Fallback: rasterize selected pages and save as image-backed PDF
        images = [rasterize_page(src[i], dpi=150) for i in page_indices]
        if not images:
            return b""
        buf = io.BytesIO()
        images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
        return buf.getvalue()
