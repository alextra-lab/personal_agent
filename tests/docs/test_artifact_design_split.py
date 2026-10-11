"""ADR-0156 D5.3 (FRE-1579 AC-3): the split of artifact-design lost no library recipe.

``docs/skills/artifact-design/`` holds ``SKILL.md`` and one file per curated library under
``references/``. Each file keeps its library's boot recipe, which is the version-pinned URLs of
``config/artifact_lib_manifest.json``, and its gotchas. paged.js is the exception: its asset is
``eval_gated``, so its file keeps the experimental prose and must not hold its URL.

The wider drift guard (``test_artifact_design_doc_matches_manifest``) reads the whole folder.
"""

from __future__ import annotations

import pytest

from personal_agent.observability.artifact_envelope.spec import (
    DEFAULT_LIB_MANIFEST_PATH,
    LibAsset,
    load_lib_manifest,
)

_SKILL = DEFAULT_LIB_MANIFEST_PATH.resolve().parents[1] / "docs" / "skills" / "artifact-design"
_REFERENCES = _SKILL / "references"

# Manifest asset name -> the reference file that holds its boot recipe.
_LIBRARY_FILE = {
    "katex": "katex.md",
    "chartjs": "chartjs.md",
    "three": "threejs.md",
    "highlightjs": "highlightjs.md",
    "source-serif-4": "ofl-fonts.md",
    "playfair-display": "ofl-fonts.md",
    "jetbrains-mono": "ofl-fonts.md",
    "pagedjs": "pagedjs.md",
}
# The libraries whose section had a "Gotchas" list before the split (paged.js had none).
_WITH_GOTCHAS = ("katex.md", "chartjs.md", "threejs.md", "highlightjs.md", "ofl-fonts.md")

_ORIGIN, _ASSETS = load_lib_manifest()


def _text(name: str) -> str:
    return (_REFERENCES / name).read_text(encoding="utf-8")


def test_every_manifest_asset_has_a_reference_file() -> None:
    """A new asset in the manifest forces a decision about where its recipe lives."""
    assert {a.name for a in _ASSETS} <= set(_LIBRARY_FILE)


@pytest.mark.parametrize("asset", _ASSETS, ids=[a.path for a in _ASSETS])
def test_a_pinned_url_is_in_its_own_library_file(asset: LibAsset) -> None:
    url = f"{_ORIGIN}/lib/{asset.path}"
    text = _text(_LIBRARY_FILE[asset.name])
    if asset.eval_gated:
        assert url not in text, f"an eval-gated asset must not be first-class: {url}"
    else:
        assert url in text, f"{_LIBRARY_FILE[asset.name]} lacks the boot recipe URL {url}"


@pytest.mark.parametrize("name", _WITH_GOTCHAS)
def test_a_library_file_keeps_its_gotchas(name: str) -> None:
    assert "**Gotchas:**" in _text(name)


def test_pagedjs_stays_experimental_and_gated() -> None:
    text = _text("pagedjs.md").lower()
    assert "experimental" in text
    assert "eval_gated" in text


def test_the_body_names_every_reference_file() -> None:
    body = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    files = sorted(p.name for p in _REFERENCES.glob("*.md"))
    assert files, "the split left no references/ files"
    missing = [name for name in files if f"references/{name}" not in body]
    assert missing == []


def test_every_mapped_file_exists() -> None:
    missing = [n for n in sorted(set(_LIBRARY_FILE.values())) if not (_REFERENCES / n).is_file()]
    assert missing == []


def test_the_references_hold_no_bash_recipe() -> None:
    """The D8.7 test reads ``SKILL.md`` only, so a bash fence here would escape its check."""
    for path in _REFERENCES.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert not any(f"```{lang}" in text for lang in ("bash", "sh", "shell")), path.name
