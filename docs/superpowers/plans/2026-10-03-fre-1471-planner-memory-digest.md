# FRE-1471 — The planner memory digest (ADR-0154 D5, carrying ADR-0147 D1/D4)

**Tier:** Standard (touches `src/` memory-render logic). Codex plan-review: done, findings folded in
below (revision 2).
**Scope:** build the digest and its bounds. Wire nothing. FRE-1472 threads it to the planner.
**Design source:** ADR-0147 D1 and D4, carried unchanged by ADR-0154 D5.

## Design

### Shared selection (AC-1)

`_select_renderable_memory(items) -> tuple[_SelectedItem, ...]` in `orchestrator/executor.py`.
`_SelectedItem` is a frozen dataclass: `kind: MemoryItemKind`, `item: dict`. The function moves the
bucket-by-kind step and the filter-then-cap step out of `_render_memory_section_with_ids`, with
their comments. It returns the eligible items **flat, in the upstream relevance order** that the
memory context carries (ADR-0147 D4). It applies the per-kind caps by position among eligible items
of the same kind.

`_render_memory_section_with_ids` calls the function, then groups the result by kind and emits the
sections in its existing order. Its output must stay byte-identical (see the golden test).

### Shared text (AC-2)

The digest builder calls `_entity_line(item)` and `_stance_line(item)` with no identifier. It calls
one new helper, `_episode_line(item, number, identifier=None)`, which returns `"{number}. {text}"`
built from `_episode_text`. The renderer calls the same helper. The number is the item's 1-based
position among the episodes, as the renderer counts it. The digest counts episodes in the same way.
The digest never calls the registry.

### Per-line bound

A digest line is the first physical line of the renderer's line (`splitlines()[0]`). If it is longer
than 120 characters, the cut falls at the last clause boundary (`, ` `; ` `. ` `: ` ` — ` ` (`) at
or after character 60. Failing that, it falls at the last space at or after character 60. Failing
that, it is a hard cut at 120. No marker is added: a marker would stop the line being a prefix of
the renderer's line.

**Deictic safety (FRE-1150).** An entity description that says "the user" carries a clarifier that
the renderer appends. If a cut line contains "the user" and does not contain the whole clarifier,
the line is cut back to before "the user". A fact about another person never reaches the planner
as a statement about the connected user. The renderer's own constants are used, not copies.

### Builder and bounds (AC-3, AC-4, AC-5)

`_build_planner_memory_digest(items) -> PlannerMemoryDigest`:

1. `eligible = _select_renderable_memory(items)`.
2. Take at most 20 items (`_DIGEST_MAX_ITEMS`), in order. Build one line each.
3. While the joined text is above 300 estimated tokens (`_DIGEST_MAX_TOKENS`, measured with
   `request_gateway.budget.estimate_tokens`), drop the last line. `dropped_count` counts these drops
   only.
4. `text = "\n".join(lines)`. No lines gives `""`, never a header or whitespace.

### Keys (AC-6)

`MemoryItemKey(kind, identity, ordinal)`, a frozen dataclass in `expansion_types.py`. The identity
comes from `memory_item_identity`. The ordinal is the 0-based position in the eligible sequence.
`rendered_item_keys` holds one key per eligible item, in that sequence. `item_keys` holds one key
per emitted line. The digest is a prefix of the sequence, so `item_keys` is an ordered sub-multiset
of `rendered_item_keys` by construction.

### Value object

`PlannerMemoryDigest` in `expansion_types.py`: frozen dataclass with `text`, `item_keys`,
`rendered_item_keys`, `item_count`, `eligible_count`, `dropped_count`, `kind_counts` (a plain
`dict`, so it serialises to JSON), `max_line_chars`, `estimated_tokens`.

## Open point for master (not blocking the build)

ADR-0147 D1 says each line carries "the item kind". AC-2 says each line is a prefix of the
renderer's own line. The two cannot both hold as text: a prepended `entity:` label breaks the
prefix. This build follows AC-2. An entity line carries its `[entity_type]`. The kind is also in
`item_keys` and `kind_counts`. Behavioural and topic stances share one text format, as they do in
the renderer. A label can be added later as one line in the builder if the owner wants it.

## Steps

1. **Golden first.** Record the renderer's current output (`golden/memory_render_golden.json`:
   all kinds, caps, blanks, legacy shapes, registry and no registry) from the unmodified code.
2. **Tests first** — `test_memory_digest.py`, one class per AC. Expected: collection fails.
3. Add `MemoryItemKey` and `PlannerMemoryDigest` to `expansion_types.py`.
4. Add `_SelectedItem`, `_select_renderable_memory`, `_episode_line`; refactor the renderer. Add
   `test_memory_render_golden.py`. Expected: golden test and the existing render tests pass.
5. Add `_build_planner_memory_digest` and the constants. Expected: the new tests pass.
6. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.

## Tests, by acceptance criterion

- **AC-1:** patch `executor._select_renderable_memory` to return a selection that the real filters
  would reject: 20 entities (above the cap of 15) and one blank-description entity. The renderer and
  the digest builder both carry every selected item. A second filter or cap in either one fails.
- **AC-2:** 47 eligible items in mixed relevance order, plus session items, blank items and
  over-cap items with leak tokens. Every digest line is a prefix of a renderer line for the same
  item, which proves the number format too. Episode numbering is also tested with episodes
  placed after other kinds, and with a digest that cuts the episode list.
- **AC-3:** 47 items with 600-character descriptions. At most 20 items, no line above 120
  characters, at most 300 estimated tokens, and `dropped_count > 0`.
- **AC-4:** the arithmetic over an empty input, a small input, 25 undropped items and the
  adversarial input.
- **AC-5:** an empty list, and a list whose every item filters out.
- **AC-6:** two entities with one name plus a stance with an empty target and a non-blank affect.
- **Line shape:** word boundary, clause boundary, hard cut, CR/LF and Unicode line separators,
  a newline in a name, the existing `...[truncated N chars]` marker, multi-byte text, the deictic cut.
- **Golden:** `test_memory_render_golden.py` compares the renderer to the recorded output.
