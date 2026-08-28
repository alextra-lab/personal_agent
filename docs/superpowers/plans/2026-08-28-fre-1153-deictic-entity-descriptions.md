# FRE-1153 — Resolve deictic ("the user") entity descriptions at extraction write-time

Ticket: https://linear.app/frenchforest/issue/FRE-1153
Related: FRE-1150 (render-time mitigation, already shipped — commits 54cf6252/f919de94/65ed17d3)

## Scope

Entity extraction (`second_brain/entity_extraction.py` → `second_brain/consolidator.py` →
`memory/service.py::create_entity`) writes `Entity.description` into Neo4j as a
group-visibility field rendered to every authenticated reader. When the LLM writes a
description containing "the user" (e.g. `"Susan: The user's stated name in the
conversation."`), it becomes a claim about whoever reads it next, not the person the
description was actually extracted from.

FRE-1150 mitigated this at **render** time (append a clarifier when "the user" appears in
a recalled description). This ticket fixes it at the **source**: entity extraction never
writes a description containing an unresolved "the user" reference in the first place.
FRE-1150's render-time clarifier stays as-is — it remains the safety net for the 176
pre-existing unrepairable rows already in the graph; this ticket does not touch it.

Scoped to the entity-extraction pipeline only (as the ticket title states). `gateway/
app.py`'s `store_fact` is a *different* write path — its own docstring already says
"user-provided facts, not extraction" (`gateway/app.py:181`) — a human typing a fact
through the API is not the class of bug this ticket describes (an LLM inferring a
description it never grounds to a concrete subject). Out of scope; not touched.

Only `knowledge_entities` (written to the Neo4j `:Entity` graph — the "6,220 described
entities" the ticket's census counts) are affected in practice. `finding`-kind entities go
to the physically separate sysgraph store (ADR-0105); `ephemeral` entities are never
written. Stances/claims are already anchored per-user via
`:Person{user_id}-[:HAS_FACT]->` edges, not globally rendered — out of scope. The fix
itself lives in `_finalize_extraction`, which runs over *all* entities regardless of
`output_kind` — harmless on finding/ephemeral rows since they never reach the KG, but
keeps the fix at the single point already responsible for Python-owned entity
normalization rather than re-deriving the output_kind partition a second time.

## Design revision — dropped identity substitution after Codex plan review

**Original plan** (superseded): resolve "the user" against the session's actual
authenticated user, substituting their real display name into the stored description.
**Codex review caught a real problem**: this is a multi-tenant, group-visibility graph
(cross-user entity scoping is explicitly deferred to FRE-674). Writing a specific other
person's real name into a field every authenticated user can read is *more* identifying
than the ambiguous "the user" it replaces — before the fix, a reader could misattribute
the claim to themselves (the FRE-1150 bug); after the naive fix, every reader would
correctly and explicitly learn who the described person actually is, which is a new
disclosure this ticket has no mandate to introduce (FRE-674's boundary, not this ticket's).
It also depended on `MemoryService.get_or_provision_user_person`'s `display_name`, which
falls back to an email local-part when the user never set one — not a value already
authorized for group-wide disclosure.

**Revised design**: deterministic, identity-free substitution. Replace "the user" (word-
bounded, case-insensitive — matches inside "the user's" because the possessive suffix
sits outside the match span and survives untouched) with a **fixed neutral phrase**
that:
- Contains no literal "the user" (satisfies AC-1 by construction, not by prompt luck).
- Asserts no identity — mirrors FRE-1150's own render-time clarifier, which also never
  named anyone ("whoever that earlier conversation was with").
- Reads naturally as both a subject and a possessive, so mid-sentence substitution
  ("the user's employer" → "the other party's employer") does not produce broken prose.

This is strictly simpler than the superseded design: no `MemoryService` changes, no
Neo4j lookup, no `consolidator.py` changes, no async wiring or lazy-cache concurrency
question — the whole fix is a pure function inside `entity_extraction.py`, applied in
`_finalize_extraction` (which already owns Python-side entity normalization: `class`,
`output_kind`, `description_update_kind`).

## Implementation

In `second_brain/entity_extraction.py`, near the existing module constants:

```python
_DEICTIC_USER_RE = re.compile(r"\bthe user\b", re.IGNORECASE)
"""Matches a stored description referring to "the user" (FRE-1153). Mirrors the
render-time pattern in orchestrator/executor.py (FRE-1150) — word-bounded so "the
username field" does not match; matches inside "the user's" because the apostrophe is
a \\b boundary, and the match span excludes the "'s" suffix, which survives
substitution unchanged.
"""

_DEICTIC_REPLACEMENT = "the other party"
"""Identity-free stand-in for "the user" in a stored description (FRE-1153). Unlike
FRE-1150's render-time clarifier (an appended note), this REPLACES the phrase so the
stored text itself never asserts a claim about "the user" — but, like that clarifier,
it names no one: writing a specific person's real name here would disclose their
identity to every other authenticated reader of this group-visible field, which is a
new privacy exposure this ticket has no mandate to introduce (cross-user entity scoping
is FRE-674's, not this ticket's).
"""


def resolve_deictic_description(description: str) -> str:
    """Rewrite a description's "the user" reference to a non-identifying phrase (FRE-1153).

    A description is stored globally and rendered to every authenticated reader
    (visibility="group"), so a "the user" reference in it is a claim about whoever
    reads it next, not about the person the description was actually extracted from.
    Replacing it here means a newly extracted description can never carry that claim,
    regardless of what the extractor prompt does or does not manage to instruct the
    model to avoid (a deterministic guarantee, not a prompt-compliance hope).

    Args:
        description: The entity's description as returned by the extractor.

    Returns:
        ``description`` unchanged if it contains no "the user" reference; otherwise
        every occurrence replaced with :data:`_DEICTIC_REPLACEMENT`, capitalized when
        the match starts the string (the common case: "The user's ..." at the start of
        a one-sentence description).
    """
    if not description or not _DEICTIC_USER_RE.search(description):
        return description

    def _replace(match: "re.Match[str]") -> str:
        if match.start() == 0:
            return _DEICTIC_REPLACEMENT[0].upper() + _DEICTIC_REPLACEMENT[1:]
        return _DEICTIC_REPLACEMENT

    return _DEICTIC_USER_RE.sub(_replace, description)
```

Wire into `_finalize_extraction` (entity_extraction.py:838), inside the existing
`for entity in result.get("entities", []):` loop, alongside the class/output_kind/
description_update_kind normalization already there:

```python
    for entity in result.get("entities", []):
        entity["class"] = _normalize_entity_class(...)
        entity["output_kind"] = _normalize_output_kind(...)
        entity["description_update_kind"] = _normalize_description_update_kind(...)
        entity["description"] = resolve_deictic_description(entity.get("description") or "")
```

`resolve_deictic_description` is exported without a leading underscore (mirrors
`default_extraction_summary`) so the contract test can call it directly without
exercising the full async extraction path.

No changes needed to `consolidator.py` or `memory/service.py` — the description is
already resolved by the time `extraction_result["entities"]` reaches the consolidator's
entity-creation loop.

No change to the extraction prompt. Same rationale as before: FRE-1150's own commit found
a blanket prompt-side tag "worse than doing nothing extra" for the equivalent render-side
problem, and a deterministic transform is guaranteed regardless of model compliance,
which is what AC-2 (count does not grow, "over a period of real traffic") needs.

## Atomic steps

1. Failing tests first, in `tests/test_second_brain/test_entity_extraction_contract.py`
   (new class `TestDeicticDescriptionResolution`), calling `resolve_deictic_description`
   directly:
   - `"The user's stated name in the conversation."` → `"The other party's stated name
     in the conversation."` (sentence-initial capitalization; possessive preserved; no
     "the user" anywhere in the output — assert via `not _DEICTIC_USER_RE.search(...)`,
     not just a substring check, so the test itself is the AC-1 postcondition).
   - Mid-sentence: `"Employer of the user, mentioned in passing."` → contains
     `"the other party"`, no "the user".
   - Case-insensitive: `"THE USER prefers dark mode."` → resolved, no match remains.
   - Multiple occurrences in one description → every occurrence replaced.
   - `"the username field"` is NOT touched (word-boundary regression guard — byte-
     identical output).
   - A description with no "the user" at all is returned unchanged (byte-identical —
     guards against an over-eager rewrite, not just "no match").
   - Empty string / `None`-coerced-to-`""` input returns unchanged without raising.
2. Implement `_DEICTIC_USER_RE`, `_DEICTIC_REPLACEMENT`, `resolve_deictic_description`
   in `entity_extraction.py`; wire into `_finalize_extraction`. Confirm step-1 tests pass.
3. Add one `_finalize_extraction`-level test (existing test class in the same file, or a
   small addition near `TestDescriptionUpdateKind`) confirming the wiring: an entity dict
   with a deictic description, run through `_finalize_extraction`, ends with a resolved
   `description` — this is what pins the fix to the actual code path
   `extract_entities_and_relationships` returns to the consolidator, not just the pure
   helper in isolation.
4. Grep `tests/` for existing fixtures containing literal `"the user"` /
   `"The user"` inside an entity `description` value that flow through
   `_finalize_extraction` or a full `extract_entities_and_relationships` call (e.g.
   mocked-LLM-response fixtures) — update any that would now assert on stale text.
5. Quality gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.
6. Self-review: `feature-dev:code-reviewer` on `git diff origin/main...HEAD`;
   `security-review` (touches a field rendered to other authenticated users — the exact
   class of issue this revision exists to avoid re-introducing, worth the scan even
   though the revised design removes the identity lookup).

## Acceptance criteria mapping

- **AC-1** ("a newly extracted entity description contains no unresolved reference to
  'the user'"): the contract tests assert this as a postcondition (regex no longer
  matches), not just eyeballed example text — covers the exact incident shape (`"Susan:
  The user's stated name in the conversation."`) plus case/multi-occurrence/boundary
  variants.
- **AC-2** ("the count of deictic descriptions in the graph does not grow"): the
  substitution runs unconditionally inside `_finalize_extraction`, which every entity
  passes through before `extract_entities_and_relationships` returns — no code path in
  the entity-extraction pipeline can hand the consolidator a description still matching
  `_DEICTIC_USER_RE`. Holds regardless of what the extractor prompt produces, which is
  what "over a period of real traffic" needs (robust to prompt drift, not just true for
  one probe conversation).

## Diff class

Touches the entity-extraction pipeline's normalization step (default-on, background
job) and a field rendered to other authenticated users. Per the build skill's diff-class
rule this escalates — will note in the PR body + handoff comment: "diff class:
escalated — flagged for owner `/code-review ultra` before merge."
