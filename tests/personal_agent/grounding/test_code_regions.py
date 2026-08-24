"""FRE-1281 — layer 1: where, if anywhere, exemption can be proved (ADR-0138 D1).

Layer 1 is not a classifier and must never act like one. Its whole contract is: grant
exemption only where code-ness is *provable*, and hand everything else to the model pass.
A codex plan review found the first draft violating exactly this — it treated ``js``,
``sql`` and ``bash`` as code without a parser to prove it, which made an arbitrary-prose
``js`` fence exempt and opened a complete bypass of the contract.
"""

from __future__ import annotations

from personal_agent.grounding.code_regions import (
    RegionKind,
    partition_output,
)


def _kinds(text: str) -> list[RegionKind]:
    return [region.kind for region in partition_output(text)]


def _of_kind(text: str, kind: RegionKind) -> list[str]:
    return [r.text for r in partition_output(text) if r.kind is kind]


# ── plain prose ──────────────────────────────────────────────────────────────


def test_unfenced_text_is_a_single_classify_region() -> None:
    """Prose with no fence goes to the model pass, whole."""
    regions = partition_output("Paris is the capital of France.")
    assert len(regions) == 1
    assert regions[0].kind is RegionKind.CLASSIFY
    assert regions[0].text == "Paris is the capital of France."


def test_regions_tile_the_output_exactly() -> None:
    """Every character belongs to exactly one region, in order.

    A gap here would be text nothing ever looks at — the silent seam the coverage
    contract exists to close.
    """
    text = "Intro.\n\n```python\nx = 1\n```\n\nOutro claim about the world.\n"
    regions = partition_output(text)
    assert "".join(r.text for r in regions) == text
    for earlier, later in zip(regions, regions[1:], strict=False):
        assert earlier.end == later.start
    assert regions[0].start == 0
    assert regions[-1].end == len(text)


# ── parse-verified code ──────────────────────────────────────────────────────


def test_valid_python_fence_is_proved_code() -> None:
    """A python fence that parses is exempt-by-construction."""
    text = "```python\ndef f() -> int:\n    return 1\n```\n"
    assert RegionKind.PROVEN_CODE in _kinds(text)


def test_valid_json_toml_yaml_fences_are_proved_code() -> None:
    """The other three languages with a parser available here."""
    for language, body in (
        ("json", '{"a": 1}'),
        ("toml", 'name = "demo"'),
        ("yaml", "a: 1"),
    ):
        text = f"```{language}\n{body}\n```\n"
        assert RegionKind.PROVEN_CODE in _kinds(text), language


def test_python_fence_that_does_not_parse_is_handed_to_the_classifier() -> None:
    """D1: a fence "whose content does not parse as the declared language" is not exempt."""
    text = "```python\nThe parser was rewritten last year and now handles nested quotes.\n```\n"
    assert RegionKind.PROVEN_CODE not in _kinds(text)
    assert RegionKind.CLASSIFY in _kinds(text)


# ── the bypass codex found ───────────────────────────────────────────────────


def test_language_without_a_parser_is_not_exempted() -> None:
    """The blocking finding: no parser here means no proof, so no exemption.

    Under the rejected draft, arbitrary prose in a ``js`` fence was exempt because no JS
    parser exists in this repo. ADR-0138 AC-5 fails an implementation where "fencing or
    mere parseability buys exemption".
    """
    text = "```js\nThe library was rewritten in 2019 and is faster than its predecessor.\n```\n"
    assert RegionKind.PROVEN_CODE not in _kinds(text)
    assert RegionKind.CLASSIFY in _kinds(text)


def test_unknown_language_is_not_exempted() -> None:
    """An unrecognized fence type is explicitly not an exempt region under D1."""
    text = "```brainfuck\nThe specification was never formally standardised.\n```\n"
    assert RegionKind.PROVEN_CODE not in _kinds(text)


def test_text_fence_is_prose() -> None:
    """ "Prose placed inside a fence is prose."."""
    text = "```text\nThe library was released in 2019.\n```\n"
    assert RegionKind.PROVEN_CODE not in _kinds(text)


# ── natural language hiding inside valid code ────────────────────────────────


def test_string_literal_in_valid_python_is_extracted_for_classification() -> None:
    """ADR-0138 D1's own example.

    ``print("Paris has 9 million residents")`` parses cleanly as Python; a parse check
    alone would exempt it, "making a string literal a delivery channel for an uncited
    assertion".
    """
    text = '```python\ndef banner() -> None:\n    print("Paris has 9 million residents")\n```\n'
    classified = _of_kind(text, RegionKind.CLASSIFY)
    assert any("Paris has 9 million residents" in chunk for chunk in classified)


def test_comment_and_docstring_in_valid_python_are_extracted() -> None:
    """Comments and docstrings are prose channels too."""
    text = (
        "```python\n"
        "# ISO 8601 requires a leading zero\n"
        "def normalise(month: int) -> str:\n"
        '    """Pad a month. The standard was published in 1988."""\n'
        '    return f"{month:02d}"\n'
        "```\n"
    )
    classified = " ".join(_of_kind(text, RegionKind.CLASSIFY))
    assert "ISO 8601 requires a leading zero" in classified
    assert "The standard was published in 1988" in classified


def test_fstring_content_is_extracted_like_any_other_literal() -> None:
    """PEP 701 changed how f-strings tokenize, and the first draft missed it entirely.

    Since Python 3.12 ``tokenize`` emits no ``STRING`` token for an f-string at all — it
    emits ``FSTRING_START`` / ``FSTRING_MIDDLE`` / ``FSTRING_END``. Checking only for
    ``STRING`` therefore left every f-string invisible to prose extraction, so
    ``print(f"...")`` stayed inside PROVEN_CODE and was labelled exempt without the
    classifier ever seeing it.

    That is ADR-0138 D1's own named exemplar with the quote prefixed by one character,
    and f-strings are the idiomatic way to write such a literal — the common case, not an
    edge case. This project requires >=3.12, so the old behaviour was unreachable.
    """
    text = '```python\ndef banner() -> None:\n    print(f"Paris has 9 million residents")\n```\n'
    classified = " ".join(_of_kind(text, RegionKind.CLASSIFY))
    assert "Paris has 9 million residents" in classified
    assert "Paris has 9 million residents" not in " ".join(_of_kind(text, RegionKind.PROVEN_CODE))


def test_fstring_with_an_interpolation_still_yields_its_literal_text() -> None:
    """The claim and the interpolation live in one f-string; the claim must still surface."""
    text = '```python\nname = "x"\nprint(f"Basalt is denser than granite, {name}")\n```\n'
    classified = " ".join(_of_kind(text, RegionKind.CLASSIFY))
    assert "Basalt is denser than granite" in classified


def test_code_around_an_extracted_literal_stays_proved_code() -> None:
    """Pulling out the prose must not surrender the surrounding code to the classifier."""
    text = '```python\nx = 1\nprint("Paris has 9 million residents")\ny = 2\n```\n'
    proved = " ".join(_of_kind(text, RegionKind.PROVEN_CODE))
    assert "x = 1" in proved
    assert "y = 2" in proved


def test_python_string_literals_are_extracted_whatever_they_contain() -> None:
    """Every Python string literal is a prose *candidate*, including a URL.

    Layer 1 does not decide whether a literal says anything about the world — that is
    the classifier's call, and this literal will come back NOT_A_CLAIM. What matters
    here is that extracting it does not disturb the code around it: an earlier draft of
    this test asserted the URL was *not* extracted, which would have required layer 1 to
    judge content it is explicitly forbidden to judge.
    """
    text = '```python\nurl = "https://example.com/#anchor"\n```\n'
    assert "https://example.com/#anchor" in " ".join(_of_kind(text, RegionKind.CLASSIFY))
    assert "url =" in " ".join(_of_kind(text, RegionKind.PROVEN_CODE))


def test_comment_marker_inside_a_yaml_string_is_not_a_comment() -> None:
    """The scanner tracks quote state, where the scanner is actually used.

    YAML has a parser here, so a valid block is proven code and only its comments are
    carved out. A scanner that mistook the ``#`` inside this quoted value for a comment
    would carve the line in half — a precision bug that looks like a recall win.
    """
    text = '```yaml\nlabel: "release #4 candidate"\ncount: 2\n```\n'
    classified = " ".join(_of_kind(text, RegionKind.CLASSIFY))
    assert "candidate" not in classified
    assert "label:" in " ".join(_of_kind(text, RegionKind.PROVEN_CODE))


def test_yaml_comment_is_carved_out_for_classification() -> None:
    """The positive half: a real YAML comment is a prose channel and must be reachable."""
    text = "```yaml\n# the scheduler polls every thirty seconds by default\ninterval: 30\n```\n"
    classified = " ".join(_of_kind(text, RegionKind.CLASSIFY))
    assert "the scheduler polls every thirty seconds" in classified


def test_unparseable_language_block_reaches_the_classifier_whole() -> None:
    """A language with no parser cannot hide prose anywhere, comment or not.

    The whole block goes to the classifier, so there is nothing left to carve — which is
    why the unproved path does no comment scanning at all.
    """
    text = "```js\n// Node has shipped fetch by default since version 18\nconst a = 1;\n```\n"
    classified = " ".join(_of_kind(text, RegionKind.CLASSIFY))
    assert "Node has shipped fetch by default" in classified
    assert "const a = 1;" in classified


# ── dependency declarations ──────────────────────────────────────────────────


def test_python_imports_are_dependency_declarations() -> None:
    """D1's hole in the code exemption: imports are verified, not exempt."""
    text = "```python\nimport httpx\nfrom pydantic import BaseModel\n\nx = 1\n```\n"
    deps = _of_kind(text, RegionKind.DEPENDENCY)
    assert any("import httpx" in d for d in deps)
    assert any("from pydantic import BaseModel" in d for d in deps)


def test_install_commands_are_dependency_declarations() -> None:
    """Install commands, in a fence with no parser available."""
    text = "```bash\nuv add fastapi-turbo\npip install httpx\n```\n"
    deps = " ".join(_of_kind(text, RegionKind.DEPENDENCY))
    assert "uv add fastapi-turbo" in deps
    assert "pip install httpx" in deps


def test_manifest_dependency_entries_are_dependency_declarations() -> None:
    """A manifest entry is a dependency declaration wherever it is written."""
    text = '```toml\n[project]\nname = "demo"\ndependencies = ["httpx>=0.27"]\n```\n'
    deps = " ".join(_of_kind(text, RegionKind.DEPENDENCY))
    assert "httpx>=0.27" in deps


def test_ordinary_call_is_not_a_dependency_declaration() -> None:
    """Positive control — 'use' is not 'declare', so this stays exempt code."""
    text = "```python\nimport httpx\n\nclient = httpx.AsyncClient()\n```\n"
    proved = " ".join(_of_kind(text, RegionKind.PROVEN_CODE))
    assert "client = httpx.AsyncClient()" in proved
    assert "client = httpx.AsyncClient()" not in " ".join(_of_kind(text, RegionKind.DEPENDENCY))


def test_unterminated_fence_is_classified_not_exempted() -> None:
    """A malformed fence must not swallow the rest of the output as exempt code."""
    text = "Here you go:\n\n```python\nx = 1\nThe library was released in 2019.\n"
    assert RegionKind.PROVEN_CODE not in _kinds(text)
    assert "The library was released in 2019." in " ".join(_of_kind(text, RegionKind.CLASSIFY))
