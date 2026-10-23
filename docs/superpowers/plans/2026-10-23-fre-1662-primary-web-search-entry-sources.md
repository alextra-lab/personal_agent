# FRE-1662 — a primary web_search gives each result entry its own source, so the reply links to it

**Ticket:** FRE-1662. **Design intent:** ADR-0138 D1 (one marker per claim), D2 (admissibility),
D3(a)/(b) (resolution, reachability). Follows FRE-442 (PR #1344), which made sub-agent
`web_search` entries citable and made a resolved web source a link.

## The defect

Owner turn 2026-10-23, trace `52d7391f19b1f4ce3be38b0ad3a6f0d4`: the primary ran 3 `web_search`
calls. `SourceRegistry.register_tool_result` registered each call as one source with
`referent=None`. `link_citation_markers` links only a source with a web `referent`, so it
stripped every marker, and the reply had no links.

## ADR reading (scope item 4)

ADR-0138 D3 states three checks per cited span. It has no rule of one marker per tool call.
D1 requires each claim to carry its own adjacent marker. "One source per tool call" is the
FRE-1280 implementation, stated in `source_registry.py` docstrings. Per-entry sources narrow
what one marker covers, which is the direction D1 asks for. No ADR change is needed.

## Design

1. **Keep the call-level source.** `register_tool_result` is unchanged. The call-level source
   keeps the `answers` (weather, calculator) and `infoboxes` citable. Its marker stays the
   top-level `_citation` field.
2. **New policy table** `ENTRY_SOURCE_TOOLS = {"web_search": ("answers", "infoboxes")}` in
   `source_registry.py`: the tools whose entries register, and the top-level keys whose objects
   the call-level marker covers. Only these tools register entries. A `url` field inside a `read` file, a Linear issue or a
   fetched JSON page is a link that the content carries, not a retrieval (the FRE-442 security
   finding, commit 83de1d6f).
3. **New public walker** `url_entries(node) -> list[dict[str, object]]`: every JSON object in a
   parsed result whose `url` field is a non-empty string, pre-order. An entry with `url: ""`
   is not returned, so it is not offered.
4. **New method** `SourceRegistry.register_result_entry(*, tool_name, arguments, entry) ->
   ToolRegistration`:
   - Counts one offer, so `tool_results_admitted <= tool_results_offered` still holds
     (ADR-0139 D1). The `tool_results_offered` docstring names the third unit.
   - Refuses `UNCLASSIFIED_TOOL` when the tool is not in `ENTRY_SOURCE_TOOLS` or not on the
     policy table, and `DERIVED_FROM_TURN_WRITE` when the arguments are tainted.
   - Content = `strip_argument_echo(json.dumps(entry), arguments)`, the same rule the sub-agent
     path applies to a matched entry. Empty content → `NO_CONTENT`. A url that is only in the
     arguments (the echoed `query` string) is never offered, because it is not an object (AC-2).
   - Registers `kind` from the policy table, `label=tool_name`, `origin=tool_name`,
     `entitlement=_entitlement_for(...)`, `referent=url.strip()`.
   - Every refusal goes through `_record_refusal`.
5. **Executor** `_with_entry_sources(ctx, *, tool_name, arguments, content, call_identifier) -> str`
   in `orchestrator/executor.py`, beside `_with_citation_marker`. It replaces the
   `_with_citation_marker` call in phase 3, which runs only when the call-level registration
   returned an identifier, so an inadmissible or failed call registers no entries.
   - Falls back to `_with_citation_marker` without a registry, for a tool outside
     `ENTRY_SOURCE_TOOLS`, for content that is not a JSON object (a truncated result holds its
     body as a string), or when no entry got a marker.
   - Registers each entry in pre-order, then writes `entry["_citation"] = "[S{n}@…]"` on it at
     once. A child entry registers after its parent, so no marker enters registered content.
   - When at least one entry has a marker, the call-level marker goes on each object under
     `answers` and `infoboxes`, and not at the top level. The model then sees one marker beside
     each result fact: its entry's.
   - Best-effort: an exception is logged (`source_registry_entry_registration_failed`,
     trace_id, session_id, tool_name). The entries marked before it keep their markers.
6. **Snapshot** `_log_source_registry_snapshot` adds `"referent": s.referent` to each source.
   `agent-logs-*` is `dynamic: true`; the new string maps to `keyword` (additive). This is what
   AC-5 reads. A URL is not retrieved content, and `web_search_completed` already logs
   `result_urls`.
7. **Docstrings** that say a `web_search` source has no referent are updated where they are
   general claims (`_referent_of`, `link_citation_markers`).

No prompt change: `GROUNDING_CONTRACT_PROMPT` tells the model to copy the marker that the
content it uses carries. The entry marker sits inside the entry beside its snippet.

## Known residuals (recorded, not fixed here)

- The call-level source content still holds the results (FRE-1280 unchanged). The model sees
  its marker only beside `answers`/`infoboxes`, so a result fact has one marker to copy.
- An entry `url` that is not a safe web URL registers, and its marker is stripped at delivery,
  like a call-level marker today.
- D3(b) reachability now applies to a cited entry: a short entry whose snippet holds a
  soft-failure phrase ("not found") reads as `UNREACHABLE`. The sub-agent entry path has the
  same rule today.
- Each search adds about 35 characters per entry to the content, after the result token bound
  was applied. The call-level marker already does the same.

## Codex plan review (2026-10-23) — dispositions

1. Call-level source keeps nested results that the query echoes → rejected: FRE-1280 behaviour,
   no referent, no link. A search engine that returns an entry for a URL query retrieved it.
2. URL-survival rule underspecified → rule dropped. AC-2 asks only that a url found *only* in the
   arguments registers nothing, and the walker never offers the echoed `query` string.
3. An exception after registration admits sources the model never sees → fixed: mark each entry
   at once, keep the marks made before a failure.
4. Byte-identical entries share one source → rejected: same content and url is one source, and
   both entries show its marker.
5. AC-5 probabilistic, top-level marker competes → fixed: the call-level marker moves to the
   `answers`/`infoboxes` objects when entries carry markers.
6. Non-linkable entry urls admitted → rejected: stripped at delivery like any marker without a
   web referent. Recorded as a residual.
7. Allowlist, taint, ordering, offer accounting → confirmed fine.

## Acceptance criteria → tests

| AC | Test (file) | Asserts |
|----|-------------|---------|
| AC-1 | `test_three_entries_register_three_sources_with_their_urls` (`tests/personal_agent/grounding/test_primary_search_entry_sources.py`) | 3 sources, referent = each url, offered/admitted counts |
| AC-2 | `test_an_entry_with_no_url_registers_nothing`, `test_a_url_only_in_the_arguments_registers_nothing`, `test_a_tainted_search_registers_no_entries`, `test_a_url_field_in_a_non_search_result_registers_nothing` (same file) | no source |
| AC-3 | `test_the_model_reads_one_marker_per_entry_in_valid_json` (`tests/personal_agent/orchestrator/test_source_registry_wiring.py`) | `json.loads` passes, each entry `_citation` resolves to its url |
| AC-4 | `test_a_cited_entry_marker_renders_a_link_and_an_unminted_one_is_stripped` (`tests/personal_agent/grounding/test_citation_links.py`) | link to the entry url, unminted marker stripped |
| AC-5 | live owner turn after deploy | links in the reply, snapshot shows `web_search` sources with `referent` urls |

Plus: snapshot carries `referent`; an inadmissible call registers no entries; an exception in
the entry path returns the original content.

## Steps

1. Write the failing tests above → `make test-file FILE=<each file>` fails.
2. Implement `ENTRY_SOURCE_TOOLS`, `url_entries`, `register_result_entry` → AC-1/AC-2 pass.
3. Implement `_with_entry_sources`, wire in phase 3, add `referent` to the snapshot → AC-3/AC-4 pass.
4. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
