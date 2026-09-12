# FRE-1359 — the ordering signal, the preregistered threshold, the measurement machinery

**Ticket:** FRE-1359 (Approved, `stream:build1`, Tier-1:Opus)
**ADRs:** ADR-0139 D8 · ADR-0140 T4, AC-3, AC-5 · ADR-0138 D2
**Scope of this PR:** AC-1, AC-2, and AC-3's **machinery**. **No wrapper.** Master's dispatch
comment (2026-09-11 21:11 UTC) states the no-wrapper rule, and ADR-0140 AC-3 fails if a wrapper
merges before the measurement is posted.

> **Revision 2 (2026-09-12), after codex plan review.** Five Major findings, all verified against
> the code and all accepted. Two changed the design: an origin-only field cannot carry the
> ordering signal (§3 D-a), and the measurement's unit of analysis is the turn, not the document
> (§3 D-e). The rest corrected false claims in revision 1 and the step order.

---

## 1. What this PR can and cannot deliver

| Deliverable | AC | This PR |
|---|---|---|
| The ordering signal | AC-1 | **Ships** — two fields on `grounding_verification_completed` |
| The preregistered threshold | AC-2 | **Ships** — a Linear comment posted before any window is read |
| The measurement | AC-3 | **Machinery only** — the committed, re-runnable query |
| Wrappers | AC-4 | **Out of scope** — separate tickets, after the measurement ranks them |

**AC-3 cannot complete on this merge, and the ticket must not close on it.** No document written
before deploy carries the new fields, so window 1 cannot open until the gateway is deployed.
ADR-0140 adjudicates AC-3 over **two** consecutive windows. This is stated in the PR body and in
the handoff comment so FRE-1359 is not flipped to `Done` by the merge.

## 2. Why the fields are needed, stated from the code

An `uncitable` turn admitted nothing. `source_registry_snapshot` therefore holds no refused
origin (`orchestrator/executor.py:2244-2258`), and the only record carrying `tool_name` on a
refusal is the DEBUG event `source_registry_tool_inadmissible`
(`orchestrator/executor.py:1651-1659`) whose join ADR-0139 D1 exists to abolish.

## 3. Design decisions

**D-a — two fields, because one cannot answer the question.** `register_tool_result` refuses in
four shapes, and the branch order decides what each one means
(`grounding/source_registry.py:1048-1094`):

| Admissibility | Reached when | Wrapper demand? |
|---|---|---|
| `MODEL_AUTHORED_INVOCATION` | the tool is in `ARBITRARY_CODE_TOOLS` | **Yes** |
| `UNCLASSIFIED_TOOL` | the tool is on no table (default-deny) | **Yes** |
| `DERIVED_FROM_TURN_WRITE` | a **typed** tool addressed state an inadmissible call wrote | No |
| `NO_CONTENT` | a **typed** tool returned nothing to cite, or failed | No |

The last two are reachable only *after* the tool passed `DOCUMENTATION_TOOLS` or
`TYPED_RETRIEVAL_TOOLS`, so their origin already has a typed tool and can never be a
provisioning candidate. A field carrying only tool names cannot separate them, and revision 1
promised an exclusion it had no way to implement. So:

- **`refused_tool_origins`** — the distinct tool names whose offer registered no source. This is
  AC-1's field, in AC-1's words, covering every refusal shape, so "empty on a turn with
  refusals" cannot happen.
- **`refused_origin_admissibility`** — the distinct `"<tool_name>:<admissibility>"` pairs. This
  is the ordering key AC-3 actually needs.

A flat list of joined strings, not an object keyed by tool name: a dynamic object would mint one
ES leaf field per tool name and eat the `index.mapping.total_fields.limit` budget.

**The residual ADR-0140 already declares.** Even with the reason, `bash` mixes transitional
retrieval with permanently uncitable command execution, and `ARBITRARY_CODE_TOOLS` holds
generative tools a wrapper cannot replace. ADR-0140 says AC-3 cannot detect that split
(`ADR-0140:465-480`). This plan does not claim to fix it; the measurement reports origin and
reason and names the residual.

**D-b — the fields are not gated on `verification.available`.** They sit with
`tool_results_offered` and `tool_results_admitted`, outside the `if verification.available`
block, because they are properties of the registry and not of the span list.

*The scope this does not extend.* `_record_grounding` runs only after `_verify_grounding`
(`orchestrator/executor.py:7453-7457`), so a mode-off or stopped-early turn emits no event at
all. AC-1 is therefore scoped to **every emitted `grounding_verification_completed` event**, not
to every turn in the system. `verification.available == False` means verification was attempted
and unavailable — never "verification did not run".

**D-c — distinct, ordered by first refusal.** A `dict[str, None]` keyed by the recorded string.
Two refused `bash` calls yield one entry; registration order is preserved.

**D-d — no complement claim.** Revision 1 called the field "the honest complement of
`tool_results_admitted`". That is false: `tool_results_offered` counts calls including a D4
retry's duplicate, while `tool_results_admitted` counts unique registered sources
(`grounding/source_registry.py:920-940`), so `offered - admitted` already counts deduplicated
admissions. The field is defined narrowly instead: **the distinct tool names that produced a
non-`ADMISSIBLE` outcome so far this turn.** One origin can appear in both the admitted sources
and this list, and a test asserts exactly that.

**D-e — the unit of analysis is the delivered turn, and it is preregistered.**
`_record_grounding` is called on **every** synthesis attempt before D4's retry returns to
`LLM_CALL` (`orchestrator/executor.py:7453-7481`), and one registry serves the whole turn
(`:4007-4014`). So a retried trace emits two documents, and attempt 2 still carries attempt 1's
refused origins. Counting documents would double-count.

**The measurement therefore groups by `trace_id` and keeps the document with the highest
`attempts`** — the turn as delivered. This is preregistered with the threshold, because a unit
of analysis chosen after reading a window is as fitted as a threshold chosen after reading one.

## 4. Steps — preregistration first, and that order is load-bearing

### Step 1 — preregister the threshold and the unit of analysis (AC-2)

**Before any query touches `grounding_verification_completed`.** A Linear comment on FRE-1359
recording: the window definition, the unit of analysis (D-e), the qualifying threshold, the
denominator, the zero-population behaviour, and the four-window re-derivation deadline
ADR-0140 AC-3 imposes.

Recorded in the same comment for auditability: the only ES calls made before it were
`_cat/indices` and a `_mapping/field` read, which resolve names and touch no window data.

**Verify:** the comment exists, with its timestamp, before Step 5 runs the script against ES.

### Step 2 — the registry records refused origins and their reason (TDD)

New file `tests/personal_agent/grounding/test_source_registry_refused_origins.py`:

- a refused `bash` call records `"bash"` and `"bash:model_authored_invocation"`;
- AC-1's seeded shape — two refused `bash` calls and one refused unclassified tool — yields
  exactly two distinct origins, in refusal order;
- an admitted `fetch_url` call leaves both tuples empty;
- a `NO_CONTENT` refusal from a typed tool is recorded, and its pair names `no_content`, so the
  measurement can exclude it (D-a);
- a `DERIVED_FROM_TURN_WRITE` refusal is recorded with its own reason;
- **one origin admitted once and refused once** appears in the sources *and* in the refused list
  — the explicit disproof of the complement claim D-d retracts;
- a duplicate refused call does not duplicate the entry.

Then `src/personal_agent/grounding/source_registry.py`:

- `self._refused_tool_origins: dict[str, None]` and `self._refused_origin_admissibility:
  dict[str, None]` in `__init__`;
- a private `_record_refusal(tool_name, admissibility)` called from each of the four refusal
  branches, so a fifth branch added later cannot silently skip the record;
- two properties with Google docstrings naming ADR-0139 D8 and ADR-0140 AC-3.

**Verify:** `make test-file FILE=tests/personal_agent/grounding/test_source_registry_refused_origins.py`
— fails before the source change, passes after.

### Step 3 — the executor emits both fields (TDD)

New tests in `tests/personal_agent/orchestrator/test_executor_grounding.py`, beside the D1 field
tests. The existing `_grounding_verification_completed` helper returns the **first** matching
event, which cannot see a retry's second document, so the retry test reads the full event list.

- AC-1's literal check: two refused `bash` calls plus one refused unclassified call, one
  document, `refused_tool_origins == ["bash", "<unclassified>"]`, the pairs naming both reasons,
  `turn_evidence_class == "uncitable"`;
- a fully admitted turn logs `[]` for both;
- a turn whose verification was attempted and unavailable still logs the refused origins, while
  `turn_evidence_class` stays `None` (D-b).

Then `_record_grounding` in `src/personal_agent/orchestrator/executor.py`: read both tuples
beside the two counters, emit them as lists, with a comment naming ADR-0140 AC-3, this ticket,
and the per-attempt emission D-e depends on.

**Verify:** `make test-file FILE=tests/personal_agent/orchestrator/test_executor_grounding.py`.

### Step 4 — the ES mapping

`docker/elasticsearch/index-template.json`: explicit `keyword` for both fields, beside
`tool_name`.

**Stated plainly, because it changes what window 1 measures:** `agent-logs-*` uses monthly
physical indices and `agent-logs-2026-09` already exists, so the template governs October
onward. Until then both fields map dynamically through `default_string_keyword` — `keyword` with
`ignore_above: 1024`, verified live against `outcomes`, the same event's existing string array.
Tool names and admissibility values are far under that limit, so aggregation is correct in both
months. No live mapping is patched by this PR.

**Verify:** `python scripts/audit/telemetry_surface_check.py --gate --baseline scripts/audit/telemetry_surface_baseline.json`
exits 0. No baseline entry is expected: the trap lint fires on numeric, join-key and long-text
names, and neither field matches.

### Step 5 — the measurement query, committed and re-runnable

`scripts/audit/fre1359_uncitable_population.py`:

- `--since`/`--until` as UTC dates, **validated to be exactly the preregistered 14-day half-open
  interval** — an arbitrary window is refused, not silently measured;
- reduces documents to turns by `trace_id`, keeping the highest `attempts` (D-e), and reports
  how many traces carried more than one attempt;
- reports the count per `turn_evidence_class`; the `uncitable` share of asserting turns
  (`citable + uncitable`); and, within `uncitable`, the per-origin split **with its reason**,
  marking each origin as wrapper-candidate or not;
- publishes **both** shares — over all uncitable turns and over wrapper-candidate uncitable
  turns — so the denominator choice is visible rather than assumed;
- fails loudly if the document cap is reached, because a truncated aggregate reads as complete;
- exits non-zero when Elasticsearch is unreachable, so an empty window is never read as a
  measured zero.

AC-3 is adjudicated over two windows, so this is re-run, not single-use.

**Verify:** run it for the pre-deploy baseline window and read the output. It is run **after**
Step 1, never before.

### Step 6 — post what is measurable now

A ticket comment carrying the pre-deploy baseline the existing corpus can answer — the class
split and the uncitable share of asserting turns — and stating plainly that the origin split
opens with window 1 after deploy. This is **not** AC-3 evidence and is not presented as such.

## 5. Quality gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` ·
`feature-dev:code-reviewer` scoped to `git diff origin/main...HEAD`.

`security-review` is not indicated: no input parsing, no subprocess, no auth, no secrets. The
query script is read-only against the local stack.

## 6. Diff class

**Self-serve, stated with its reasoning so master can overrule.** The change is an *additive
production telemetry schema and write*: two new fields on an existing INFO event in the turn
path's call chain, plus two additive ES template properties with no type change. It mutates no
application state, deletes nothing, changes no cost or governance code, and runs no migration.
The ES template half is the "additive ES template (no type change)" reversible deploy class.

## 7. Acceptance criteria table

| AC | What it demands | Evidence this PR produces |
|---|---|---|
| AC-1 | The field exists and is populated on every turn with a refusal | The seeded two-`bash`-plus-one-unclassified executor test reading the logged document; registry tests covering all four refusal shapes, the duplicate call, and the admitted-and-refused origin |
| AC-2 | The threshold was preregistered, not fitted | The Linear comment, timestamped before the first window read; Step 1 precedes every live query, and the pre-comment ES calls are disclosed in it |
| AC-3 | The measurement is reported before the first wrapper ships | **Machinery only.** `scripts/audit/fre1359_uncitable_population.py` plus the pre-deploy baseline. The measurement itself is post-deploy, over two windows; no wrapper is in this PR |
| AC-4 | Each wrapper moves its own population | **Out of scope** — later tickets |
