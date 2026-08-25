# FRE-1280 — Per-turn source registry and per-span citation format (ADR-0138 D1/D2/D3a)

**Ticket:** [FRE-1280](https://linear.app/frenchforest/issue/FRE-1280) · Approved · Tier-1:Opus · stream:build2
**ADR:** [ADR-0138](../../architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md) D1 (span binding, atomicity), D2 (source set, independence), D3(a)
**Substrate:** [ADR-0125](../../architecture_decisions/ADR-0125-two-quality-dimensions-and-turn-evidence-contract.md) — the turn-evidence contract, which records the *input* side

First ticket of the ADR-0138 chain. FRE-1282 (verification + retry) and FRE-1283 (prompts) bind to
the identifier this establishes; FRE-1281 (span extractor) binds to the citation format.

---

## What this ticket is, and what it deliberately is not

**Is:** the identifier layer. Every item retrieved during a turn gets a stable, turn-scoped
identifier; tool results are admitted only to the extent their content is not the model's own
arguments returning; and an output format binds one citation marker to one span.

**Is not:** D3(b) reachability, D3(c) containment, the D4 block-retry-refuse loop (FRE-1282); span
classification into assertion vs generation (FRE-1281); prompt changes that would make the model
emit markers (FRE-1283). Nothing in this PR blocks a turn or changes what the model is told. The
registry is populated and observable; nothing yet consumes it to reject anything.

That boundary is deliberate and is the reason this ships first: an identifier scheme changed after
three consumers bind to it is three rewrites.

---

## Design

### 1. Identifier scheme — content-bound, not turn-bound

The ADR's illustrative markers are `[S1]`, `[S2]`. Bare ordinals cannot satisfy this ticket's own
criteria: AC-1 fails if "an identifier is reused across turns for different content", and AC-5 fails
if a previous turn's identifier resolves. `S1` in turn A and `S1` in turn B are different content
under the same name, and a stale `[S1]` carried into turn B resolves against turn B's first source —
silently binding a claim to a source that has nothing to do with it.

A **turn nonce** (`S1@<hash of turn id>`) was the first draft and does not survive review either.
Codex found a concrete collision, verified in this session:
`sha256("turn-1884")[:6] == sha256("turn-2537")[:6] == "86cc08"`. Two turns then both mint
`S1@86cc08` over different content — exactly AC-1's failure condition. Widening the nonce only
moves the probability; it does not change what the identifier is *bound to*.

So the digest covers the **content**, not just the turn:

```
identifier = f"S{ordinal}@{sha256(turn_id \0 ordinal \0 kind \0 content)[:16]}"   e.g. [S1@a3f91c2b7d40e5f6]
```

An identifier that recurs across turns then recurs only where the content is byte-identical,
where resolving it is correct rather than a defect. It also fixes the same-turn case: a registry
rebuilt on the D4 retry with a different first item mints a different `S1@…`, so a stale marker
cannot silently re-point at new content.

**Sixteen hex chars, not ten, and the claim is a bound rather than a structure.** Round 2 of the
review called the 40-bit version "structural" — the plan's word — and broke it by brute force:
`turn-726429`/`content-726429` and `turn-1435878`/`content-1435878` share the prefix `dc0757a42e`.
Reproduced in this session. At 64 bits no accidental process reaches a collision, but the honest
statement is a probabilistic bound over non-adversarial input (the model does not choose `turn_id`),
carrying one inherited invariant: **`trace_id` must be unique per turn**, or a reused turn id with a
recurring ordinal and content legitimately re-mints the same identifier.

Cost: ~12 tokens per citation for the model to copy from a list it can see (rendering that list is
FRE-1283). Against a 6k-token prompt that is not the constraint worth optimising here; the marker
width can shrink later without changing a consumer.

Identifiers are **stable within a turn**: registering the same `(kind, origin, content)` twice
returns the existing entry rather than minting a second ordinal.

### 2. Source kinds

`SourceKind` — one member per D2 admissible source, no more:

| Member | D2 item | Registered from |
|---|---|---|
| `MEMORY` | memory graph (entities, episodes, claims) | admitted memory-context items at the ADR-0125 admission point |
| `TOOL` | tool and web results retrieved this turn | `step_tool_execution` phase 3 |
| `DOCUMENTATION` | context7 and equivalents | the same path, for the documentation tools |
| `USER` | the user's own words in this conversation | turn start |

### 3. D2 independence — admissibility attaches to the tool's parameter schema

This is the load-bearing part, and the direction matters. Enumerating laundering shapes is
unbounded (`printf`, `echo`, heredoc, `python -c`, `base64 -d`, `awk 'BEGIN{print …}'`, …), so a
blocklist is defeated by the next shape.

**The first draft's answer — an allowlist of shell command heads — is also wrong, and Codex's
counterexample is decisive.** `find` reads the filesystem, so it looked like external state; but

```sh
find . -maxdepth 0 -printf 'Paris has 9 million residents\n'
```

emits a model-authored argument verbatim, and `find` was on the allowlist. Verified in this session.
The same hole exists on nearly every allowlisted head — `git log --pretty=format:…`,
`stat --printf`, `ps -o comm=…`, `rg --replace`, `curl --write-out`, `psql -c "SELECT '…'"`. A
format-string escape hatch is the norm for Unix tools, not the exception, so "allowlist the head,
denylist its dangerous flags" is the same unbounded chase one level down. The plan's claim that the
enumeration was finite was simply false.

**The finite boundary is the parameter schema, not the command.** A tool whose arguments are
*typed, enumerated parameters* — `read(path, offset, limit)`, `web_search(query, categories, …)`,
`get_library_docs(library, topic, tokens)` — has no surface through which the model can inject
content into the result: the parameters select or address, they do not compose output. A tool that
takes **arbitrary model-authored code or a command line** — `bash`, `run_python`,
`mcp_browser_evaluate`, `mcp_browser_run_code` — is by construction a channel for the model's own
words to return wearing a tool's identifier, and no static analysis of its argument bounds that.

```
tool → policy:
  typed retrieval tool       → admissible; argument-derived content excluded
  typed documentation tool   → admissible as DOCUMENTATION
  arbitrary-code tool        → NO admissible source (model_authored_invocation)
  model-backed search tool   → NO admissible source (model_authored_invocation)
  anything else / unknown    → NO admissible source (unclassified_tool)
  ...and, across calls:
  a read of state an inadmissible call wrote this turn
                             → NO admissible source (derived_from_turn_write)
```

**Model-backed search tools are inadmissible too.** `perplexity_query`, `mcp_research` and
`mcp_sequentialthinking` were on the admissible list in the previous draft, on the reading that D2's
independence rule concerns the *caller's* arguments returning — which a model-backed search does not
do. Round 2 pointed out that a typed `query` parameter carrying a prompt to another model is not
distinguishable by schema shape from a proxy for generation, and D2's decision is that parametric
knowledge is never a source. Another model's parameters are still parameters, so default-deny keeps
them out until that question is decided on its own evidence.

**The two-call shape needs turn taint after all.** The previous draft removed the taint machinery on
the reasoning that "with no shell source admissible, there is nothing to taint" — which round 2
showed was wrong in the half that matters:

```
write(path="/tmp/x", content="Paris has 9 million residents")   # inadmissible — registers nothing
read(path="/tmp/x")                                             # ← typed, admissible, and laundered
```

Each call is innocent in isolation; the *read* is the admissible half. So an inadmissible call's
string argument values become turn-tainted, and an otherwise-admissible call naming a tainted value
registers nothing. Exact value match, deliberately — a looser rule denies unrelated reads, and a
false denial costs a legitimate citation. Residual, stated: a write and a read naming the same
target differently (absolute vs relative path) needs the tool layer to report its writes rather than
the registry to infer them.

**Consequence, stated plainly: a `curl` run through `bash` cannot be cited in v1.** Grounding is
channelled through the typed retrieval tools instead (`mcp_fetch_content` is the typed equivalent of
`curl`; `read` of `cat`). This is stricter than the ADR's illustration — D2 says "`curl <url>` — the
fetched page is a source; the URL the model chose is not" — but it *preserves the principle the
illustration teaches* while dropping an instrument that is not mechanically decidable. The principle
holds exactly for the typed fetch tool: the page is admissible, the `url` argument is not. **This is
a deviation from a literal reading of the ADR and is called out for master rather than buried.**

**Argument exclusion is generic, not per-field (AC-3).** Admissible content is the tool's *output*;
the model-authored arguments are recorded separately as `excluded_arguments` and never enter citable
content. Where a structured result echoes an argument back, the echoing field is stripped — driven by
comparison against the call's own `arguments` mapping, never by a hardcoded field name, so a tool
the table has never seen is handled by the same rule. `web_search` really does echo (`query`,
`categories_used`, `engines_used` — `tools/web.py:301`), which is what makes this arm non-vacuous.

Comparison is on **exact value**, bounded to text of ≥3 characters. Both bounds are deliberate:
`max_results=10` and a returned `result_count=10` are equal values sharing no provenance, and
stripping the count would corrupt retrieved content on every search. **Residual, stated rather than
implied:** an argument echoed back *transformed* — reworded, embedded mid-sentence, translated — is
not detected. That is the same open problem as D3(c)'s normalization contract, carries the same
false-rejection cost, and is bounded by AC-8 under FRE-1282 rather than guessed at here.

### 4. Citation format (D1)

Marker: `[S<n>@<digest>]`, matched by `\[(S\d+@[0-9a-f]{10})\]`.

Binding rule, stated once so it is not inferred: **a marker binds the contiguous text from the end
of the previous marker (or the start of the text) up to its own opening bracket**, whitespace
trimmed. Nothing is inferred from clause or sentence boundaries.

What the parser emits, and the scope line it does not cross — sharpened after Codex's finding that
the first draft's `UNBOUND_SPAN` was doing FRE-1281's job:

| Emission | Condition | Whose call |
|---|---|---|
| `spans` | one `(text, identifier)` per marker, per the binding rule | this ticket (format) |
| `uncited_regions` | text bound to no marker — a **neutral observation**, not a verdict | this ticket reports; FRE-1281 decides whether it needed a citation |
| `MULTIPLY_BOUND` violation | a marker whose bound text is empty or punctuation-only — `[S1][S2]` | this ticket: decidable from format alone |
| resolution outcome | whether an identifier is present in this turn's registry (D3(a)) | this ticket: the ticket names D3(a) explicitly and AC-5 tests it |

The rename matters. Calling unmarked text an `UNBOUND_SPAN` *violation* asserts it was an assertion
requiring a citation — which is precisely the classification D1 assigns to a measured span extractor
and which no regex performs. `uncited_regions` states only what is true from the format: this text
carries no marker.

Resolution stays here and D4 does not: this ticket *reports* whether an identifier resolves;
FRE-1282 decides to block, retry or refuse. That split is why `resolve()` returns the source or
`None` rather than raising or emitting a blocking verdict.

**Boundary this parser does not cross.** It reports the binding the *format* expresses. It cannot
know that `Paris is France's capital and has 2.1 million residents [S1@…]` contains two atomic
claims — the parse is deterministic (the marker binds the whole region) but *under-segmented*, and
segmentation into atomic propositions is the span extractor (FRE-1281), which by D1 is "a named
component, not a regex". A test pins this limitation explicitly rather than leaving it to be
discovered.

### 5. Wiring

| Where | What |
|---|---|
| `execute_task`, after `TASK_STARTED` | construct the registry from `ctx.trace_id`; register the user message |
| `_record_turn_evidence` | register the memory items the evidence record resolved as **admitted** — the same admission the ADR-0125 record names, never a second opinion |
| `step_tool_execution` phase 3 | register each dispatched result, with `plan["arguments"]` for the independence rule |
| end of `execute_task` | one `source_registry_snapshot` log line — identifiers, kinds, labels, admissibility, no content |

`ctx.source_registry` is `None` on paths that never enter `execute_task` (sub-agents); every
registration helper no-ops on `None` rather than raising.

---

## Files

| File | Change |
|---|---|
| `src/personal_agent/grounding/__init__.py` | new package — the ADR-0138 chain's home (span extractor, verification, retry and the compliance metric all land here) |
| `src/personal_agent/grounding/source_registry.py` | new — kinds, admissibility, registry, tool policy table, generic argument-echo exclusion |
| `src/personal_agent/grounding/citations.py` | new — marker format, parse, violations, turn-scoped resolution |
| `src/personal_agent/orchestrator/types.py` | `ExecutionContext.source_registry` field |
| `src/personal_agent/orchestrator/executor.py` | the four wiring points above + `_register_tool_source` helper |
| `CLAUDE.md` | module-map row for `grounding/` |
| `tests/personal_agent/grounding/test_source_registry.py` | new |
| `tests/personal_agent/grounding/test_citations.py` | new |
| `tests/personal_agent/orchestrator/test_source_registry_wiring.py` | new |

---

## Steps (TDD — failing test first, confirmed failing, then implement)

1. `tests/personal_agent/grounding/test_source_registry.py` — kinds, stable ids, cross-turn
   distinctness. → `uv run pytest tests/personal_agent/grounding/test_source_registry.py` fails on
   import.
2. `grounding/source_registry.py` — `SourceKind`, `Admissibility`, `RegisteredSource`,
   `ToolRegistration`, `SourceRegistry`. → step 1 passes.
3. Independence tests: every arbitrary-code shape (`bash` incl. `find -printf` and `curl`,
   `run_python`, `mcp_browser_evaluate`), write tools and an unknown tool → no source;
   `web_search`/`mcp_fetch_content`/`read`/`get_library_docs` → source with arguments excluded and
   echo fields stripped generically. → fails.
4. Policy table + generic argument-echo exclusion. → step 3 passes.
5. `tests/personal_agent/grounding/test_citations.py` — two-assertion binding, unbound,
   multiply-bound, cross-turn non-resolution. → fails.
6. `grounding/citations.py`. → step 5 passes.
7. `tests/personal_agent/orchestrator/test_source_registry_wiring.py` — a turn exercising all four
   kinds through the executor helpers; registry enumerated. → fails.
8. Wiring in `types.py` + `executor.py`; `CLAUDE.md` module-map row. → step 7 passes.
9. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.

---

## Acceptance criteria — how each is proved

Codex's round-1 finding was that **every** criterion in the first draft had a degenerate
implementation that passed its stated test. Each proof below now names the specific degenerate
implementation it kills, because a test that only a correct implementation passes is the only kind
worth writing.

| AC | Proof | Degenerate implementation it kills |
|---|---|---|
| **AC-1** — every retrieved item registered, all four kinds, no cross-turn reuse | `test_all_four_kinds_registered_in_one_turn` registers **three** memory items, **two** tool results, a documentation result and the user message, then asserts `len(registry.sources()) == 8` and that every input item's content is findable — not merely that four kinds appear. `test_identifiers_differ_across_turns_for_identical_content` uses byte-identical content under two turn ids. `test_same_content_same_turn_reuses_identifier` pins within-turn stability. **`test_identifier_changes_when_content_changes`** holds turn and ordinal fixed and varies only the content. | "register one item per kind and drop the rest" — caught by the count assertion. **The turn-nonce design itself** — caught by the content-varying test, under which `S1@hash(turn_id)` is identical for both and must not be |
| **AC-2** — wholly model-derived tool result registers no admissible source | `test_arbitrary_code_tools_register_no_source` parametrised over `bash` (`printf`, `echo`, heredoc, `python -c`, `awk BEGIN`, `find -printf`, `git --pretty=format:`, and a plain `curl`), `run_python`, `mcp_browser_evaluate`. **Three** positive controls — `web_search`, `mcp_fetch_content`, `read` — plus `test_unknown_tool_registers_no_source`, `test_model_backed_search_tools_register_no_source`, and the cross-call pair **`test_write_then_read_registers_no_source`** / `test_read_of_an_untouched_path_still_registers`. | "reject every shell command except a `curl` head" — the decision is tool-level and three admissible tools must still register. **A pure tool-name classifier** — killed by the write→read pair, where both tools' names are individually correct and only the sequence is laundering |
| **AC-3** — partly model-supplied result registers only the non-derived portion | `test_fetch_registers_page_not_url` (`mcp_fetch_content`): page body in `source.content`, `url` in `excluded_arguments`, absent from content. `test_argument_echo_stripped_generically` runs the **same assertion over two tools with different argument names** — `web_search`'s `query` and a synthetic tool's `topic` — proving the rule reads the `arguments` mapping rather than a hardcoded field. | "special-case `curl` URLs and `web_search.query`" — the second tool's differently-named echo field fails it |
| **AC-4** — one marker binds one span, unambiguously | `test_adjacent_assertions_bind_separately`: `Ortiz [S1@…] is better than Nardin [S2@…]` → two spans, each resolving to its own identifier, asserted on **span text** as well as identifier. `test_multiply_bound_flagged`, `test_uncited_region_reported`. `test_multi_claim_region_binds_as_one_region` pins the stated FRE-1281 boundary. | "treat every marker-delimited chunk as one span" survives only for the under-segmented case, which is now an explicitly-pinned boundary rather than an unstated pass |
| **AC-5** — resolution is turn-scoped | `test_previous_turn_identifier_does_not_resolve` (turn A's id, resolved in turn B) **plus `test_fabricated_current_turn_identifier_does_not_resolve`** (`S99@<turn B's own digest shape>`), plus the positive that turn B's own identifier resolves. | "accept any syntactically valid identifier carrying the current nonce" — killed by the fabricated-current-turn negative, which the first draft omitted entirely |

Every criterion carries a seeded negative *and* a positive control, per the ADR's testing strategy —
a guard never shown to reject anything has not been shown to work, and a guard that rejects
everything is not a guard either.

**AC-3's instrument changed and master should know.** The criterion says "**Check:** `curl` a page".
Under the redesign a `curl` through `bash` is inadmissible, so the check runs against
`mcp_fetch_content` — the typed fetch tool, where the criterion's actual question (is the page
admissible and the model-chosen URL not?) is answerable. The criterion's intent is met; its literal
instrument is not, and substituting it is master's call to ratify, not mine to make silently.

---

## Risks

| Risk | Handling |
|---|---|
| Shell inadmissibility over-rejects: a legitimate `curl`/`cat` in `bash` cannot be cited | Fail-safe direction by design (D7's accepted cost); typed equivalents exist for both; **no turn is blocked by this PR**, so the cost is not yet user-visible and FRE-1282 can revisit with evidence |
| The typed-tool table itself is wrong for some tool | One line per tool, and the default is deny — a mistake under-admits rather than launders |
| Registered content bloats the context object | Bounded with `mark_truncated` at 50k chars — explicit marker, never silent (ADR-0125 D5) |
| Digest collision mints one identifier for two different sources | 40 bits over `(turn_id, ordinal, kind, content)`; a recurrence across turns implies byte-identical content, where resolving is correct. Widening changes no consumer |
| Under-segmented citation (`claim and claim [S1]`) reads as compliant | Out of this ticket's reach by D1's own construction (extraction is a measured component, FRE-1281); pinned by a test and named in the handoff rather than left implicit |
| A transformed argument echo survives exclusion | Stated residual; the same open problem as D3(c) normalization, bounded by AC-8 under FRE-1282 |
| A write and a read naming the same target differently defeat turn taint | Stated residual; needs the tool layer to report its writes rather than the registry to infer them |
| **`prompts.py:58` still tells the model to fetch pages with `bash` + curl** | Real conflict with the v1 deviation, found in review and **carried to FRE-1283** (prompts) with a ticket comment — not fixed here, because prompts are that ticket's scope and this PR changes no prompt |
