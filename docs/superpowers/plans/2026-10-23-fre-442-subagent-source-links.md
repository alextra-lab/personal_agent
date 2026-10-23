# FRE-442 — sub-agent sources become citable, and a cited real source becomes a link

**Ticket:** FRE-442 (absorbs FRE-1468). **Design intent:** ADR-0138 D2 (admissibility), D3(a)
(resolution), D1 (default-deny); ADR-0150 D1 (`worker_report_v1`, one `source_url` per finding).

**Owner decision, 2026-10-23 (recorded on the ticket):** a marker becomes a link when it resolves
in this turn's registry to a source with a web URL. The link shows provenance. It does not certify
the claim. An invented, stale or URL-less marker is stripped as today.

## Measured before planning

- `grounding_verification_completed`, 90 days, all `mode=observe`: 1513 cited spans, 104 passed
  (7%). This is why the owner chose "resolves" over "passed".
- `source_registry_snapshot`, 30 days, 214 turns: `web_search` registered in 31 turns, `fetch_url`
  in fewer than 12. A primary `web_search` source has `referent=None` (one source per call, a set
  of results). So a primary `web_search` citation gets no link in this ticket. Recorded as a
  follow-up candidate, not folded in (it changes FRE-1280's one-source-per-call design).
- The reply reaches the PWA whole (`service/app.py`, one `TextDeltaEvent` after the turn), so a
  marker is never visible mid-stream. The PWA already renders Markdown links as
  `<a target="_blank" rel="noopener noreferrer">` (`MarkdownContent.tsx`), and react-markdown's
  default URL transform drops `javascript:`.

## Design

1. **Worker records what it retrieved.** `sub_agent.py` keeps one `RetrievedToolResult`
   `(tool_name, arguments, content)` per successful dispatch on `_ToolLoopState.retrieved`. The
   result constructor that carries a report passes `retrieved=tuple(state.retrieved)` to the new
   `SubAgentResult.retrieved` field. Refused, malformed and failed calls are not recorded.
2. **Registry admits a finding only from a retrieval that returned its URL.** New
   `SourceRegistry.register_worker_finding(*, source_url, retrieved) -> ToolRegistration`:
   - Counts one offer (`tool_results_offered += 1`), so `admitted <= offered` still holds.
   - Scans the worker's results in order. A result matches when (a) its tool declares a
     `referent_parameter` and that argument equals the URL (`fetch_url`): the source content is
     the whole result; or (b) the parsed JSON holds an object whose `"url"` equals the URL (a
     `web_search` result entry): the source content is the smallest such object. URL equality is
     exact after `strip()`. Only a bare site-root `/` is folded (codex review: `/path` and
     `/path/` can be two resources).
   - The matched tool passes the same D2 policy as `register_tool_result` (arbitrary-code and
     unclassified tools refused, taint check, argument-echo strip, `NO_CONTENT`, entitlement).
     The policy and entitlement code is shared through two small private helpers, not copied.
   - No match → new `Admissibility.NOT_RETRIEVED`, refusal recorded as
     `sub_agent_finding:not_retrieved`, nothing registered (AC-2).
   - Registered source: `kind` from the policy table, `origin=tool_name`,
     `label="sub_agent:<tool_name>"` (visible in `source_registry_snapshot`),
     `referent=source_url`.
   - A truncated result (`"truncated": true`, `partial` string) matches only through (a).
3. **The primary's digest carries the marker.** `ExpansionController.execute(...,
   source_registry=None)` threads the registry to `_build_synthesis_context`, which registers each
   finding of each schema-backed report and passes `{source_url: identifier}` to
   `render_worker_report_body` / `render_worker_report_findings`. A finding line becomes
   `claim — why [url] (date) [S3@…]`. Registration is best-effort: an exception is logged and the
   line renders without a marker. One INFO line `sub_agent_findings_registered` (trace_id,
   offered, registered).
4. **A resolved marker with a web URL becomes a link.** New
   `citations.link_citation_markers(text, registry)`: each well-formed marker (with its leading
   space) that resolves to a source whose `referent` is a safe `http`/`https` URL becomes
   ` ([host](<url>))`. Every other marker is removed exactly as `strip_citation_markers` removes
   it. Safe URL: scheme `http`/`https`, a host matching `[a-z0-9.-]+`, no whitespace, control
   characters, `<` or `>`. `|` and `\` are percent-encoded (a `|` would split a GFM table cell).
   Link text is the host without a leading `www.`.
5. **One call site.** `executor._strip_markers_from_turn` calls `link_citation_markers` when
   `ctx.source_registry` is set, else `strip_citation_markers`. It still runs on the reply and on
   every assistant message, so the persisted history equals the delivered reply.
6. **Docs in code.** Update `classify_turn_shape`'s docstring (a worker's findings now enter the
   registry, so a sub-agent turn with registered findings is shape B) and the
   `_strip_markers_from_turn` docstring.

No prompt change: `GROUNDING_CONTRACT_PROMPT` already tells the model to copy a marker that a
tool result carries, and the synthesis reports reach the model as a tool result (FRE-1360).

## Codex plan review (2026-10-23) — dispositions

- Trailing-slash match too loose → fixed: only the site root is folded.
- `_register` dedupe ignored the referent, so two pages with one body shared the first URL →
  fixed: the referent is part of the dedupe key (test with two identical bodies).
- `tool_results_offered` gains a second unit → kept, and re-documented on the property: one
  offer per primary tool result plus one per worker finding. An all-refused fan-out is
  `uncitable`, which is ADR-0139 D1's meaning ("offered nothing citable").
- URLs with credentials → never linked. IPv6 literals and Unicode hosts → stripped, documented.
- Not folded in, recorded in the handoff: hard context compression can drop the synthesis
  exchange (true of the whole report before this change); `stop_and_show` delivers worker
  reports verbatim with no markers (no generated claim to cite); a contract-truncated
  `web_search` result does not match (its JSON is a string prefix); `Finding.source_url` caps
  URLs at 200 characters.

## Steps (TDD: each test first, confirm it fails, then implement)

1. `tests/personal_agent/grounding/test_worker_finding_sources.py` — AC-1, AC-2, policy and
   dedupe cases for `register_worker_finding`. Implement in `grounding/source_registry.py`.
   Run: `make test-file FILE=tests/personal_agent/grounding/test_worker_finding_sources.py`.
2. `tests/personal_agent/grounding/test_citation_links.py` — linking and stripping cases.
   Implement `link_citation_markers` in `grounding/citations.py`.
3. `tests/personal_agent/orchestrator/test_sub_agent.py` — a successful dispatch lands on
   `SubAgentResult.retrieved`. A refused or failed one does not. Implement in `sub_agent.py`,
   `sub_agent_types.py`.
4. `tests/personal_agent/orchestrator/test_subagent_source_links.py` — AC-1 digest half (two
   markers in the context, two sources) and AC-5 (a tool-free worker: no source, no marker).
   Implement in `expansion_controller.py`, `worker_types.py`, executor call site.
5. `tests/personal_agent/orchestrator/test_executor_grounding.py` — AC-3 backend half: a resolved
   web source becomes a link in the reply and history, an unresolved marker is stripped. The
   existing strip test moves to a `web_search` source (no referent), since a `fetch_url` source
   now links by the owner's decision.
6. The same file — the path end to end without
   a model: worker result → synthesis context → a reply that copies the marker →
   `_strip_markers_from_turn` → a clickable link.
7. `seshat-pwa/src/__tests__/MarkdownContent.test.tsx` — AC-3 PWA half: the backend's link form
   renders an anchor with the URL, `target="_blank"`, `rel` containing `noopener`, in prose and
   in a table cell. No PWA source change is needed.
   Run: `cd seshat-pwa && npx vitest run src/__tests__/MarkdownContent.test.tsx`.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`, `cd seshat-pwa && npm run lint`.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 | Step 1 (two sources registered, referents set) + step 4 (both markers in the digest) |
| AC-2 | Step 1 seeded negative: a URL absent from the worker's results registers nothing |
| AC-3 | Step 5 + step 6 (backend) + step 7 (PWA renders the anchor; no raw marker remains) |
| AC-4 | Owner's live turn after deploy (needs the owner) |
| AC-5 | Step 4: a tool-free worker turn adds no source |

## Out of scope

- Per-result registration of the primary's own `web_search` call (no link for those citations
  today). Candidate follow-up, decided by the owner.
- Any change to verification outcomes, enforcement or the grounding prompt.
