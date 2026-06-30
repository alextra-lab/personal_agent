# FRE-681: ADR-0102 T1 — License-clean PDF Utility

**Date:** 2026-06-30
**Ticket:** FRE-681
**ADR:** ADR-0102 (Document PDF Ingestion)
**Branch:** `fre-681-pdf-utility`

---

## Scope

Add `pypdfium2` + `Pillow` as the license-clean PDF primitives layer. Create a pure helper module `src/personal_agent/documents/pdf_utils.py` with no integration into the orchestrator or turn assembly (that comes in later tickets FRE-682+). No changes to existing files except `pyproject.toml` and `uv.lock`.

**Acceptance criteria owned by this ticket:**
- **AC-3**: pypdfium2 + Pillow present in Python deps; no pymupdf/fitz/pdf2image/poppler; no AGPL/GPL in PDF stack; no GPL renderer binary in Docker image.
- **AC-5**: unit test — given a synthetic page set (one near-blank: char_count < 20, near-zero pixel coverage; one dense: char_count >> floor) and budget=1, the selector picks the dense page and drops the blank. Proven by the same test failing for naive first-N.

---

## Implementation Steps

### Step 1: Add dependencies
```bash
cd /opt/seshat/.claude/worktrees/build2
uv add pypdfium2 pillow
```
Verify: `grep -E "pypdfium2|pillow" pyproject.toml` shows both entries.

### Step 2: Create module skeleton
Files to create:
- `src/personal_agent/documents/__init__.py` — empty (package marker)
- `src/personal_agent/documents/pdf_utils.py` — the helper module

### Step 3: Implement `pdf_utils.py`

```
src/personal_agent/documents/pdf_utils.py
```

**Public API:**

```python
@dataclass(frozen=True)
class PageSalienceScore:
    page_index: int
    char_count: int
    pixel_coverage: float  # 0.0–1.0, fraction of non-white pixels

def extract_page_text(page: pdfium.PdfPage) -> str:
    """Extract all text from a single PDF page via pypdfium2 text page."""

def is_text_dense(text: str, floor: int = 100) -> bool:
    """Return True if len(text) >= floor (page has a usable text layer)."""

def rasterize_page(page: pdfium.PdfPage, dpi: int = 150) -> PIL.Image.Image:
    """Render a PDF page to a PIL Image using pypdfium2."""

def pixel_coverage_ratio(image: PIL.Image.Image) -> float:
    """Fraction of non-white pixels in a PIL Image (0.0 = blank, 1.0 = fully inked)."""

def score_page_salience(score: PageSalienceScore) -> float:
    """Combined salience score: normalised char_count + pixel_coverage.
    
    char_count contributes linearly (divided by a normalising constant).
    pixel_coverage contributes as a ratio.
    """

def select_pages_by_salience(
    scores: Sequence[PageSalienceScore], budget: int
) -> list[int]:
    """Return the page_index values of the top-`budget` pages by salience."""

def compute_page_scores(
    pdf: pdfium.PdfDocument,
    raster_dpi: int = 72,
) -> list[PageSalienceScore]:
    """Compute PageSalienceScore for every page in a PDF document."""

def build_page_subset(src_bytes: bytes, page_indices: Sequence[int]) -> bytes:
    """Build a sub-PDF from src_bytes containing only page_indices (0-based).
    
    Uses PdfDocument.import_pages. Falls back to rasterizing each selected page
    and embedding as images if import_pages raises (some encrypted/restricted inputs).
    Returns the sub-PDF as bytes.
    """
```

**Notes on scoring formula:**
- `score_page_salience` normalises char_count by dividing by 1000 (so 1000 chars = 1.0 unit) and adds pixel_coverage directly. This keeps the two signals roughly comparable without requiring calibration.
- The formula does NOT need tuning here — it just needs to score a blank page (0 chars, 0.0 coverage) below a dense page (>1000 chars, >0.5 coverage).

### Step 4: Create tests
Files:
- `tests/personal_agent/documents/__init__.py`
- `tests/personal_agent/documents/test_pdf_utils.py`

**Test AC-3: license_clean_deps**
```python
def test_ac3_license_clean_deps():
    # Required packages are importable
    import pypdfium2  # noqa: F401
    from PIL import Image  # noqa: F401
    # Forbidden packages raise ImportError
    for forbidden in ["fitz", "pdf2image"]:
        with pytest.raises(ImportError):
            __import__(forbidden)
```

Note: `pymupdf` is the wheel name (not directly importable as `pymupdf`); the importable module is `fitz`. Checking `fitz` + `pdf2image` covers the actual risk. Checking pyproject.toml for the package names guards the install-level.

**Test AC-5: salience selector**
```python
def test_ac5_salience_selector_skips_blank_page():
    blank = PageSalienceScore(page_index=0, char_count=5, pixel_coverage=0.01)
    dense = PageSalienceScore(page_index=1, char_count=800, pixel_coverage=0.70)

    result = select_pages_by_salience([blank, dense], budget=1)
    assert result == [1], "selector must pick dense page (index 1)"

    # Prove that naive first-N fails this assertion
    naive_first_n = [0]
    assert naive_first_n != result, "naive selector returns wrong page"

def test_ac5_selector_ordering():
    pages = [
        PageSalienceScore(page_index=0, char_count=5, pixel_coverage=0.01),   # blank
        PageSalienceScore(page_index=1, char_count=800, pixel_coverage=0.70), # dense
        PageSalienceScore(page_index=2, char_count=200, pixel_coverage=0.30), # medium
    ]
    result = select_pages_by_salience(pages, budget=2)
    assert 1 in result, "dense page must be in budget-2 selection"
    assert 0 not in result, "blank page must be excluded from budget-2 selection"
```

**Additional unit tests:**
- `test_is_text_dense_below_floor` — len < floor → False
- `test_is_text_dense_at_floor` — len == floor → True
- `test_pixel_coverage_ratio_blank_image` — all-white PIL Image → 0.0
- `test_pixel_coverage_ratio_black_image` — all-black PIL Image → 1.0
- `test_score_page_salience_blank_is_low` — blank page scores near zero
- `test_score_page_salience_dense_beats_blank` — dense page scores higher

### Step 5: Quality gates
```bash
make test-file FILE=tests/personal_agent/documents/test_pdf_utils.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

---

## Acceptance Criteria Proof Plan

| AC | How proven |
|----|-----------|
| AC-3 (license-clean) | `test_ac3_license_clean_deps` — fitz/pdf2image raise ImportError; pyproject.toml grep in test or CI |
| AC-5 (selection skips blank) | `test_ac5_salience_selector_skips_blank_page` — budget-1 returns dense index, naive would return blank index |

---

## Files Changed

| File | Action |
|------|--------|
| `pyproject.toml` | add pypdfium2, pillow |
| `uv.lock` | updated by uv |
| `src/personal_agent/documents/__init__.py` | create |
| `src/personal_agent/documents/pdf_utils.py` | create |
| `tests/personal_agent/documents/__init__.py` | create |
| `tests/personal_agent/documents/test_pdf_utils.py` | create |

No existing source files are modified.

---

## Follow-up Tickets

None expected from this ticket — it is purely additive and self-contained. FRE-682+ in the chain will import from this module.
