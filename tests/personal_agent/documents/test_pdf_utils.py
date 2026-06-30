"""Tests for the PDF utility primitives (FRE-681 / ADR-0102 T1).

AC-3: license-clean deps (pypdfium2 + Pillow present; no AGPL/GPL forbidden packages).
AC-5: salience-based selector skips near-blank pages over dense ones.
"""

from __future__ import annotations

import importlib.metadata
import io
from pathlib import Path

import pytest
from PIL import Image

from personal_agent.documents.pdf_utils import (
    PageSalienceScore,
    build_page_subset,
    get_outline_page_indices,
    is_text_dense,
    pixel_coverage_ratio,
    rasterize_page,
    score_page_salience,
    select_pages_by_salience,
)

# ---------------------------------------------------------------------------
# AC-3: License-clean dependency check
# ---------------------------------------------------------------------------


class TestAC3LicenseCleanDeps:
    """AC-3: Python dep set is license-clean (no AGPL/GPL PDF libraries)."""

    def test_pypdfium2_is_installed(self) -> None:
        importlib.metadata.version("pypdfium2")  # raises PackageNotFoundError if absent

    def test_pillow_is_installed(self) -> None:
        importlib.metadata.version("Pillow")

    def test_pymupdf_not_installed(self) -> None:
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("PyMuPDF")

    def test_fitz_not_importable(self) -> None:
        with pytest.raises(ImportError):
            __import__("fitz")

    def test_pdf2image_not_installed(self) -> None:
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("pdf2image")

    def test_pyproject_contains_pypdfium2(self) -> None:
        root = Path(__file__).parents[3]
        pyproject = (root / "pyproject.toml").read_text()
        assert "pypdfium2" in pyproject

    def test_pyproject_contains_pillow(self) -> None:
        root = Path(__file__).parents[3]
        pyproject = (root / "pyproject.toml").read_text()
        assert "pillow" in pyproject.lower()

    def test_pyproject_excludes_pymupdf(self) -> None:
        root = Path(__file__).parents[3]
        pyproject = (root / "pyproject.toml").read_text().lower()
        assert "pymupdf" not in pyproject
        assert '"fitz"' not in pyproject

    def test_pyproject_excludes_pdf2image(self) -> None:
        root = Path(__file__).parents[3]
        pyproject = (root / "pyproject.toml").read_text().lower()
        assert "pdf2image" not in pyproject


# ---------------------------------------------------------------------------
# is_text_dense
# ---------------------------------------------------------------------------


class TestIsTextDense:
    def test_empty_string_not_dense(self) -> None:
        assert is_text_dense("", floor=20) is False

    def test_below_floor_not_dense(self) -> None:
        assert is_text_dense("abc", floor=20) is False

    def test_at_floor_is_dense(self) -> None:
        text = "x" * 20
        assert is_text_dense(text, floor=20) is True

    def test_above_floor_is_dense(self) -> None:
        text = "word " * 50
        assert is_text_dense(text, floor=20) is True

    def test_whitespace_only_not_dense(self) -> None:
        # A page with only spaces/newlines should NOT count as dense
        text = "   \n\t\n   "
        assert is_text_dense(text, floor=5) is False

    def test_mixed_whitespace_counts_stripped(self) -> None:
        # Stripped content must meet the floor
        text = " " * 100 + "ab"  # stripped = "ab", len 2
        assert is_text_dense(text, floor=5) is False
        assert is_text_dense(text, floor=2) is True


# ---------------------------------------------------------------------------
# pixel_coverage_ratio
# ---------------------------------------------------------------------------


class TestPixelCoverageRatio:
    def test_all_white_image_is_zero(self) -> None:
        img = Image.new("RGB", (100, 100), color=(255, 255, 255))
        assert pixel_coverage_ratio(img) == pytest.approx(0.0)

    def test_all_black_image_is_one(self) -> None:
        img = Image.new("RGB", (100, 100), color=(0, 0, 0))
        assert pixel_coverage_ratio(img) == pytest.approx(1.0)

    def test_half_black_is_half(self) -> None:
        img = Image.new("RGB", (100, 100), color=(255, 255, 255))
        # Fill left half black
        for x in range(50):
            for y in range(100):
                img.putpixel((x, y), (0, 0, 0))
        ratio = pixel_coverage_ratio(img)
        assert 0.48 <= ratio <= 0.52

    def test_grayscale_image_accepted(self) -> None:
        img = Image.new("L", (50, 50), color=128)
        ratio = pixel_coverage_ratio(img)
        assert 0.0 <= ratio <= 1.0


# ---------------------------------------------------------------------------
# score_page_salience
# ---------------------------------------------------------------------------


class TestScorePageSalience:
    def test_blank_page_scores_near_zero(self) -> None:
        blank = PageSalienceScore(page_index=0, char_count=0, pixel_coverage=0.0)
        assert score_page_salience(blank) == pytest.approx(0.0)

    def test_dense_page_scores_higher_than_blank(self) -> None:
        blank = PageSalienceScore(page_index=0, char_count=0, pixel_coverage=0.0)
        dense = PageSalienceScore(page_index=1, char_count=800, pixel_coverage=0.70)
        assert score_page_salience(dense) > score_page_salience(blank)

    def test_score_increases_with_char_count(self) -> None:
        low = PageSalienceScore(page_index=0, char_count=100, pixel_coverage=0.0)
        high = PageSalienceScore(page_index=1, char_count=1000, pixel_coverage=0.0)
        assert score_page_salience(high) > score_page_salience(low)

    def test_score_increases_with_coverage(self) -> None:
        low = PageSalienceScore(page_index=0, char_count=0, pixel_coverage=0.1)
        high = PageSalienceScore(page_index=1, char_count=0, pixel_coverage=0.9)
        assert score_page_salience(high) > score_page_salience(low)


# ---------------------------------------------------------------------------
# AC-5: select_pages_by_salience
# ---------------------------------------------------------------------------


class TestAC5SelectPagesBySalience:
    """AC-5: Salience-based selector skips near-blank pages over dense ones."""

    def _blank(self, idx: int) -> PageSalienceScore:
        return PageSalienceScore(page_index=idx, char_count=5, pixel_coverage=0.01)

    def _dense(self, idx: int) -> PageSalienceScore:
        return PageSalienceScore(page_index=idx, char_count=800, pixel_coverage=0.70)

    def test_budget1_selects_dense_not_blank(self) -> None:
        """Budget=1 from [blank@0, dense@1] must select the dense page (index 1)."""
        result = select_pages_by_salience([self._blank(0), self._dense(1)], budget=1)
        assert result == [1], f"Expected dense page [1], got {result}"

    def test_naive_first_n_would_fail(self) -> None:
        """Prove a naive first-N selector returns the wrong answer for the AC-5 case."""
        naive_first_n = [0]
        result = select_pages_by_salience([self._blank(0), self._dense(1)], budget=1)
        assert naive_first_n != result, (
            "naive selector would pick the blank page — salience selector must differ"
        )

    def test_budget1_selects_dense_when_dense_is_first(self) -> None:
        """Dense page at index 0, blank at index 1 — selector must still pick dense."""
        result = select_pages_by_salience([self._dense(0), self._blank(1)], budget=1)
        assert result == [0], f"Expected dense page [0], got {result}"

    def test_budget2_includes_dense_excludes_blank(self) -> None:
        pages = [self._blank(0), self._dense(1), self._dense(2)]
        result = select_pages_by_salience(pages, budget=2)
        assert 1 in result
        assert 2 in result
        assert 0 not in result

    def test_budget_gte_page_count_returns_all(self) -> None:
        pages = [self._blank(0), self._dense(1)]
        result = select_pages_by_salience(pages, budget=5)
        assert sorted(result) == [0, 1]

    def test_empty_pages_returns_empty(self) -> None:
        assert select_pages_by_salience([], budget=3) == []

    def test_budget_zero_returns_empty(self) -> None:
        pages = [self._dense(0), self._blank(1)]
        assert select_pages_by_salience(pages, budget=0) == []

    def test_three_pages_ranked_correctly(self) -> None:
        pages = [
            PageSalienceScore(page_index=0, char_count=5, pixel_coverage=0.01),  # blank
            PageSalienceScore(page_index=1, char_count=800, pixel_coverage=0.70),  # dense
            PageSalienceScore(page_index=2, char_count=300, pixel_coverage=0.35),  # medium
        ]
        result = select_pages_by_salience(pages, budget=1)
        assert result == [1], "dense page must win budget=1"

        result2 = select_pages_by_salience(pages, budget=2)
        assert 1 in result2
        assert 2 in result2
        assert 0 not in result2


# ---------------------------------------------------------------------------
# get_outline_page_indices
# ---------------------------------------------------------------------------


class TestGetOutlinePageIndices:
    def test_returns_frozenset(self) -> None:
        import pypdfium2 as pdfium

        # Minimal blank PDF via pypdfium2 (no outline)
        doc = pdfium.PdfDocument.new()
        doc.new_page(612, 792)
        result = get_outline_page_indices(doc)
        assert isinstance(result, frozenset)

    def test_empty_outline_returns_empty_set(self) -> None:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument.new()
        doc.new_page(612, 792)
        result = get_outline_page_indices(doc)
        assert result == frozenset()


# ---------------------------------------------------------------------------
# rasterize_page
# ---------------------------------------------------------------------------


class TestRasterizePage:
    def test_rasterize_returns_pil_image(self) -> None:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument.new()
        page = doc.new_page(612, 792)
        img = rasterize_page(page, dpi=72)
        assert isinstance(img, Image.Image)

    def test_rasterize_dimensions_scale_with_dpi(self) -> None:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument.new()
        page = doc.new_page(612, 792)
        img_72 = rasterize_page(page, dpi=72)
        img_144 = rasterize_page(page, dpi=144)
        # Higher DPI → larger pixel dimensions
        assert img_144.width > img_72.width
        assert img_144.height > img_72.height


# ---------------------------------------------------------------------------
# build_page_subset
# ---------------------------------------------------------------------------


class TestBuildPageSubset:
    def _make_two_page_pdf(self) -> bytes:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument.new()
        doc.new_page(612, 792)
        doc.new_page(612, 792)
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def test_returns_bytes(self) -> None:
        src = self._make_two_page_pdf()
        result = build_page_subset(src, [0])
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_subset_contains_one_page(self) -> None:
        import pypdfium2 as pdfium

        src = self._make_two_page_pdf()
        result = build_page_subset(src, [0])
        doc = pdfium.PdfDocument(io.BytesIO(result))
        assert len(doc) == 1

    def test_subset_of_both_pages(self) -> None:
        import pypdfium2 as pdfium

        src = self._make_two_page_pdf()
        result = build_page_subset(src, [0, 1])
        doc = pdfium.PdfDocument(io.BytesIO(result))
        assert len(doc) == 2
