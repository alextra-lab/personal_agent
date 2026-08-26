# FRE-1303 — `recall_personal_history` returns the agent's own prior replies at `EXTERNAL`

**Ticket:** FRE-1303 (Approved, `stream:build1`, `Tier-1:Opus`, Bug)
**ADRs:** ADR-0138 D2 (admissible sources, independence), D1 (attributed-restatement exemption)
**Siblings:** FRE-1302 / PR #963 (the pull-path fix whose premise this corrects) · FRE-1299 (push path) ·
FRE-1282 (where the entitlement gate originates)

---

## 1 — The defect, restated

`register_tool_result` hardcodes `Entitlement.EXTERNAL` for every `TYPED_RETRIEVAL_TOOLS` member
except `search_memory` (`source_registry.py:722-726`). `recall_personal_history` returns
`assistant_response` — the model's own prior output, verbatim (`tools/personal_history.py:192`) — so
the model can cite something it said last week at the contract's most-trusted tier.

`verify_turn` rejects only `AGENT_DERIVED` (`verification.py:329`), so `EXTERNAL` is an *admitted*
tier: this is not an under-classification, it is a bypass of the gate FRE-1282 built.

## 2 — The design decision (AC-4 — recorded in the module docstring)

### D2 independence has two axes, and only one was implemented

FRE-1280 implemented **invocation independence**: does the model's *argument* compose the output?
The boundary is the parameter schema, and the module docstring already argues it at length.

This ticket adds **authorship independence**: was the returned *content* authored by the agent?
That is not a property of the parameter schema at all — `recall_personal_history(days_ago=7)` is as
typed as a call gets, and returns the model's own prose. The boundary here is the **store being
read**: a typed retrieval that reads back a store the agent itself writes into does not earn a
blanket `EXTERNAL`.

That boundary is finite because the agent's *write* tools are enumerable (`tools/__init__.py`):
`write`, `bash`, `run_python`, `notes_write`, `artifact_write`, `artifact_draft`,
`create_linear_issue`, `create_linear_project`, plus the turn/KG writers (episodic capture, entity
extraction).

### Option chosen for `recall_personal_history`: **A — most-restrictive, applied content-aware**

Not tool-keyed. Keyed on the fields actually present in the result, exactly as
`_search_memory_entitlement` is keyed on the Claim rows actually present — so "aggregate to the
least-entitled item" has one shape in this module, not two.

- **Value-sensitive, not presence-sensitive.** The executor always emits `assistant_response`,
  `summary` and `entities`, falling back to `""` / `[]` when the Turn has none
  (`personal_history.py:191-194`). A presence check would therefore deny every production
  result and make the promised positive branch unreachable — the rule tests each field for
  *non-empty content*. (Caught in plan review; the tests were written value-sensitive and pin
  the executor's real serialized shape, empty keys included.)
- Any returned turn carrying non-empty `assistant_response`, `summary` or `entities` →
  `AGENT_DERIVED`. All three are agent-authored: the response by definition, the summary by
  generation, the entity names by extraction (ADR-0098).
- Every returned turn carrying only `user_message` → `USER_STATED`. This is AC-2's positive
  control, and it is the honest classification: `Turn.user_message` *is* the owner's words.
  The per-turn metadata (`turn_id`, `timestamp`, `session_id`) and the envelope
  (`total`, `window_days`, `user_id`) are addresses, not prose, and carry no authorship.
- An empty `turns` list → `AGENT_DERIVED`. `turns` is the tool's only payload, so an empty
  window has no user-stated content to be entitled to. (This is where the rule diverges from
  `_search_memory_entitlement`, whose empty-Claims case keeps `EXTERNAL` because that tool's
  *other* payload — matched turns, entities — is still there.)
- Any shape this parse does not fully understand → `AGENT_DERIVED`, never `EXTERNAL`, matching
  `_search_memory_entitlement`'s documented fail direction.

**Rejected — Option B, split the source.** Registering the user-message half and the
assistant-response half under separate identifiers is the correct end state, but
`register_tool_result` returns one `ToolRegistration` with a single `source`, and the executor
consumes a single identifier (`executor.py:1424-1450`). Making that a tuple is the per-item
entitlement architecture FRE-1302 explicitly deferred, and it changes every consumer. Out of scope
for one PR; the content-aware rule above is the correct behaviour *until* it lands, and is not
thrown away by it.

**Rejected — Option C, drop `assistant_response` from the tool's output.** The tool exists to answer
"what did we discuss" (`recall_personal_history_tool.description`); the agent's half of the
exchange is most of that answer. Narrowing the tool to protect a citation rule trades a real
capability for one the model barely needs — it can *read* its prior reply without *citing* it, which
is exactly what Option A leaves it able to do.

### AC-2 has two independent proofs, and the second is the one that matters in practice

Real turns almost always carry `assistant_response`, so the registry-level positive control is the
narrow half. The load-bearing half is D1: `ExemptRegion.ATTRIBUTED_RESTATEMENT` means a span that
attributedly restates the user's words is `CLAIM_EXEMPT`, and `verify_turn` iterates
`extraction.non_exempt()` — such a span never reaches the entitlement gate at all. The owner's own
history stays usable regardless of what this rule decides. Both get a test.

## 3 — The audit (AC-3) — one verdict per `TYPED_RETRIEVAL_TOOLS` member

Question, per the ticket: *can this return text the model itself authored?* The corrected premise
replaces FRE-1302's false "every other member is model-independent".

**Content-aware — the result carries a field that settles authorship:**

| Member | Verdict |
|---|---|
| `search_memory` | **Yes** — already handled by `_search_memory_entitlement` (FRE-1302), keyed on `asserted_by`. |
| `recall_personal_history` | **Yes** — this ticket, keyed on which turn fields hold content. |
| `get_location` | **Yes, and my first audit pass got this wrong.** `session_notes` is a free-text *model-authored* parameter; `ExplicitLocationProvider` extracts a city from it and returns it as `location.city` (`location.py:179, 237`). This is D2's `printf` shape in a typed parameter, and `_strip_argument_echo` cannot see it — the returned city is a *substring* of the argument and nested one level down, while that function compares whole top-level values. `LocationResolution.source` is `Literal["explicit", "client"]` and reaches the output through `asdict`, so it splits exactly: `"explicit"` → `AGENT_DERIVED`, `"client"` (the device-provided location) → `EXTERNAL`. |

**Denied flat — the store is agent-writable and the tool exposes no author field to split on:**

| Member | Verdict |
|---|---|
| `notes_search` | **Yes.** `notes_write` is the store's only writer — "durable scratch space" for the agent (`notes_tools.py:1-8`). No external-author half at all. |
| `artifact_list` | **Yes.** `type = 'artifact'` only (`artifact_tools.py:560`), so it lists exactly what `artifact_write`/`artifact_draft` produced. No external-author half. |
| `expand_tool_result` | **Yes, and the denial is load-bearing rather than precautionary.** It replays any digested result verbatim from R2, and the digest pipeline explicitly handles `tool_name == "bash"` (`tool_result_digest.py:266`) — so `ARBITRARY_CODE_TOOLS`' own excluded stdout is replayable through a typed tool one hop later. |
| `artifact_read` | **Yes.** Returns inline content of artifacts written by `artifact_write`/`artifact_draft`. **Accepted regression:** its query is `type IN ('artifact', 'upload')` (`artifact_tools.py:649`) and uploads carry `created_by = 'user'` (`uploads_router.py:191`), but `created_by` is in neither the `SELECT` nor the output dict, so a genuine user upload is denied too. |
| `find_linear_issues` | **Yes.** Returns `title`, set verbatim from `create_linear_issue`'s model-authored `title` argument (`linear.py:478, 601`). **Accepted regression:** the owner files most tickets in this project, and the result carries no author field. |
| `list_linear_projects` | **Yes.** Returns `description`, set from `create_linear_project`'s model-authored argument. Same accepted regression. |
| `mcp_get_issue`, `mcp_list_comments`, `mcp_get_project`, `mcp_get_milestone`, `mcp_get_attachment`, `mcp_get_issue_status`, `mcp_get_team`, `mcp_get_user`, `mcp_list_cycles`, `mcp_list_documents`, `mcp_list_issue_labels`, `mcp_list_issue_statuses`, `mcp_list_issues`, `mcp_list_milestones`, `mcp_list_project_labels`, `mcp_list_projects`, `mcp_list_teams`, `mcp_list_users` | **Yes — on the store, which is verifiable here; not on their schemas, which are not.** `create_linear_issue` is a registered native tool writing to the same Linear workspace these read, so the store is agent-writable regardless of what any connector returns. This repo holds only their registry membership and auto-discovered governance descriptions (`config/governance/tools.yaml:876`), not their executors, so whether any of them exposes an author field is **unverified and recorded as such** — under most-restrictive that changes the remedy, not the verdict. |

### The accepted regression is this module's existing precedent, not a new tradeoff

`_entitlement_of` already documents it for Claims lacking `asserted_by`: *"an owner-stated fact
that is merely unlabelled loses its citation, where the alternative is the system certifying its
own errors."* A mixed-authorship store whose tool output carries no author field is the same
situation one layer out, and most-restrictive is the same answer. The remedy is also the same and
is FRE-1299's shape — **thread the author field** — which is separately sequenceable per store and
is ticketed rather than folded in, because each one needs a tool-output change of its own.

Two mitigations keep the cost bounded in practice: D1's `ATTRIBUTED_RESTATEMENT` exemption means
restating the owner's words never needed one of these citations, and for `expand_tool_result` the
originating call's own source is still registered in the turn that made it.

**Keeps `EXTERNAL` — no agent-authorship channel:**

| Member | Verdict |
|---|---|
| `web_search`, `mcp_search`, `fetch_url` | **No.** External web; the agent has no write path. |
| `read_skill` | **No.** Skill bodies are repo files, human-authored, not agent-writable at runtime. |
| `mcp_get_mappings`, `mcp_get_shards`, `mcp_list_indices` | **No.** Cluster structure, not document content. |
| `mcp_browser_snapshot`, `mcp_browser_console_messages`, `mcp_browser_network_requests` | **No.** Observation of a third-party page. |

**Keeps `EXTERNAL` with a recorded residual, flagged for master:**

| Member | Verdict |
|---|---|
| `read` | **Yes, in principle** — the agent has a `write` tool. Kept `EXTERNAL` deliberately: `read` is D2's designated channel for local state ("`read` for `cat`", module docstring), the filesystem is overwhelmingly authored outside the agent, and the intra-turn `write`→`read` pair is already closed by `_taint`. The residual is a *cross-session* write→read, which needs a durable record of agent-written paths — turn-scoped `_taint` cannot see it. **Ticketed**, not folded. |
| `mcp_esql` | **Yes** — ES\|QL's `ROW a = "…"` emits a model-authored literal with no index involved, the same escape hatch `find -printf` / `psql -c "SELECT '…'"` represent for `bash`. But this is the *invocation* axis, and ADR-0138 D2 explicitly blesses it ("a database query — the returned **rows** are a source"). Reclassifying contradicts the ADR's own illustration → ADR-requiring. **Ticketed**, not folded. |

## 4 — Steps

| # | Step | Verify |
|---|---|---|
| 1 | ✅ `tests/personal_agent/grounding/test_recall_history_entitlement_e2e.py` — AC-1 seeded negatives, both AC-2 positive controls, fail-direction cases, through the real chain on the executor's real serialized shape. | Ran: **9 failed** on `EXTERNAL`, genuine assertions not import errors. |
| 2 | ✅ `tests/personal_agent/grounding/test_agent_writable_store_entitlement.py` — one seeded negative per flat-denied member + set-coverage guard + `EXTERNAL` negative control. | — |
| 3 | Add `test_get_location_entitlement.py` — `"explicit"` denies, `"client"` stays `EXTERNAL`. | Fails before the fix. |
| 4 | Extend step-2 file with the accepted-regression pins: a user upload through `artifact_read` and an expanded `web_search` result both deny, each naming its follow-up ticket. | Fails before the fix. |
| 5 | `source_registry.py`: `_recall_personal_history_entitlement` (value-sensitive) and `_get_location_entitlement`. | — |
| 6 | `source_registry.py`: `AGENT_WRITABLE_STORE_TOOLS` frozenset carrying the audit table as its docstring. | — |
| 7 | `source_registry.py`: replace the `search_memory`-only ternary at :722 with a `_CONTENT_AWARE_ENTITLEMENT` dispatch + the `AGENT_WRITABLE_STORE_TOOLS` branch; rewrite the misleading FRE-1302 comment (AC-3). | Steps 1–4 tests pass. |
| 8 | `source_registry.py`: module docstring gains the two-axes framing, the Option A/B/C record, and the accepted-regression precedent (AC-4). | — |
| 9 | File three follow-up tickets: thread author provenance (`artifact_read` uploads / Linear / `expand_tool_result` originating tool); cross-session `write`→`read` taint; `mcp_esql` ADR question. | Ticket IDs in handoff. |
| 10 | Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. | All clean. |
| 11 | Self-review: `feature-dev:code-reviewer` **and** `security-review`, both scoped to `git diff origin/main...HEAD`. | Findings fixed on-branch. |

**Security review is required, and the first draft of this plan was wrong to skip it.** The new
code parses tool-result `content` to make a *trust* decision, and the executor already treats that
content as attacker-influenced. Malformed and adversarial result shapes are the security surface:
every parse failure must fall to `AGENT_DERIVED`, because falling to `EXTERNAL` would let a crafted
result readmit itself at the most-trusted tier. That direction is pinned by test, not asserted.

## 5 — Acceptance criteria → evidence

| AC | Evidence |
|---|---|
| AC-1 — model cannot cite its own prior reply at a trusted tier | `test_recall_history_entitlement_e2e.py::test_assistant_response_recall_is_refused` — seeded negative through `recall_personal_history`-shaped result → `register_tool_result` → `verify_turn`; asserts `AGENT_DERIVED` + `SOURCE_NOT_ENTITLED`. Fails before the fix. |
| AC-2 — the user's own words stay usable | `…::test_user_message_only_recall_is_citable` (registry-level, `USER_STATED` + `PASSED`) and `…::test_attributed_restatement_never_reaches_the_gate` (D1 exemption, the practical half). |
| AC-3 — comment corrected, premise re-applied | The FRE-1302 comment at :717-721 rewritten; audit table above transcribed into `AGENT_WRITABLE_STORE_TOOLS`' docstring, one verdict per member; `test_agent_writable_store_entitlement.py` proves each flat-denied member, and its set-coverage guard fails if a member is ever added without one. |
| AC-3b — the broadened scope does not silently cost capability | Every accepted regression is pinned by its own test naming what is sacrificed (`artifact_read` uploads, expanded external results, owner-filed Linear issues), and the `EXTERNAL` negative control proves the fix is not a blanket denial. |
| AC-4 — choice recorded where the next reader finds it | Module docstring of `source_registry.py`: two-axes framing + Option A chosen, B and C rejected with reasons. |

**Diff class: escalated** — `register_tool_result` sits in the executor's turn path, which writes
(Captain's Log, memory). Flagged for owner `/code-review ultra` before merge.
