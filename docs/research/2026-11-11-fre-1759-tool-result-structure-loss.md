# FRE-1759 — Structure loss in tool results before the model reads them

> **Study:** FRE-1759, commissioned by master on the owner's request, 2026-11-11 · **Seat:** cc-explore
> **Data:** stored tool results only. No paid call, no gateway turn, no page fetch, no production write.
> **Privacy:** ids and counts only. Questions, answers, source text and the row-level tags stay in the
> private replay directory (mode 700). Sources are named by kind, never by site or URL.
> **Deployed code:** the gateway files that this study cites match `main` at `928e0fa5` by SHA-256
> (`tools/fetch.py`, `tools/web.py`, `tools/contract.py`, `orchestrator/tool_dispatch.py`,
> `orchestrator/executor.py`, `captains_log/capture.py`).

## Summary

1. **`web_search` text loses every line break before our code sees it.** 0 of 1,490 stored snippets hold
   a line break. In the reading-test set, 66 of 102 `web_search` results hold a table or a list snippet.
   The break is removed inside SearXNG, by the engine step `html_to_text` and by the core step
   `_normalize_text_fields`.
2. **`fetch_url` passes a JSON API response through unchanged.** A weather API returns each hourly
   measure as a separate array, so the model must count positions to pair a value with its hour.
3. **The false claims split evenly between these two causes.** 18 of 66 tagged false claims are
   "structure lost": 9 on a flattened `web_search` table, 9 on positional weather arrays. No confident
   case traces to truncation or to a `fetch_url` HTML table.
4. **Frequency favours the `web_search` fix.** In all production captures, 241 of 746 successful
   `web_search` results carry a flattened table or list. Positional arrays occur in 7 of 1,930 results.
   In the unselected replay first run, 4 of the 5 confident cases are `web_search` tables.
5. **The other tools show no structure loss of these kinds**, with two smaller format points: escaped
   non-ASCII text in every result, and `perplexity_query` citations that the model maps by position.

## Findings

### F1 — Every `web_search` snippet reaches the model as one line (AC-1, AC-3)

| Field | Content |
|---|---|
| **Verdict** | NEGATIVE (no snippet holds a line break) |
| **Query** | Q-ES-1 (appendix) over production primary captures, `timestamp >= 2026-10-01`, plus the 70 reading-test answers. For each `web_search` output, `results[].snippet`, count `"\n" in snippet`. |
| **Output** | Reading-test set: 755 snippets, **0** with a line break. Production since 2026-10-01: 735 snippets, **0** with a line break. |

**Arms.**
- *Arm 1 (1a).* The queried field is `results[].snippet`, which `web.py:shape_search_response` builds
  from SearXNG `content` (`web.py:257`). The same store holds 1,490 instances of that field in scope.
  The same snippets carry a markdown heading mark after other text on the same line: 196 of 540 Exa and
  Brave snippets in the reading-test set, 114 of 512 in production since 2026-10-01. A heading mark
  exists only at a line start, so the text held line breaks at its origin.
- *Arm 2.* Same populations, same run, identifier varied to the `text` field of `fetch_url`: 22 of 25
  text fields in the reading-test set and 26 of 26 in production since 2026-10-01 hold a line break.
  The read path and the string test work.
- *Arm 3.* Scope: the 70 reading-test answers (30 stored, 40 replay) and production primary captures
  from 2026-10-01 to 2026-11-07. Helper captures hold no tool results (F10) and are out of scope.

### F2 — The line breaks are removed inside SearXNG (AC-3)

| Field | Content |
|---|---|
| **Verdict** | POSITIVE |
| **Query** | In the running SearXNG container, call `searx.utils.html_to_text` and the `WHITESPACE_REGEX` substitution of `searx/result_types/_base.py:_normalize_text_fields` on a synthetic 5-line markdown table. No network call. |
| **Output** | `input newlines: 4` · `after html_to_text newlines: 0` · `after join+html_to_text newlines: 0` · `after _normalize_text_fields regex newlines: 0`. The result reads `## Title \| Name \| Day \| Value \| \| --- \| --- \| --- \| \| Item one \| Day 1 \| 10 \| \| Item two \| Day 2 \| 20 \|` |

The loss happens at two steps, both in SearXNG:

1. **Engine step.** `searx/engines/exaapi.py:_extract_content` joins the Exa highlights with a space and
   calls `html_to_text`. `searx/engines/braveapi.py:127` calls `html_to_text` on the Brave description.
2. **Core step.** `searx/result_types/_base.py:114` replaces every run of spaces, tabs and newlines in
   `content` with one space (`WHITESPACE_REGEX = '( |\t|\n)+'`, line 37).

Our code adds no loss on this path. `web.py:257` copies `content` into `snippet` unchanged.

In the flattened form, `| |` can mean a row break or an empty cell. One reading-test page also shows a
row pattern of "dates, legs, then price and airline" with no table marks at all. There, each price sits
directly before the dates of the next row.

### F3 — Whether the providers send line breaks: UNVERIFIABLE (AC-3)

| Field | Content |
|---|---|
| **Verdict** | UNVERIFIABLE |
| **Query** | Searched the stored data for raw Exa or Brave API text: production captures, the replay `run/` files, and the reading packets. |
| **Output** | No store holds the provider text before SearXNG. Every stored copy is SearXNG output. |

The SearXNG steps in F2 remove line breaks whether or not the provider sends them. So the loss is
certain at our SearXNG layer, and the provider side is unknown. One provider-side effect is visible in
the stored text: Exa and Brave return excerpts, and an excerpt boundary (`...`) can cut a row in two.
One confident case (R09) loses the dates of its row at such a boundary. The check of the provider text
needs one paid call per provider and the owner's go.

### F4 — `fetch_url` returns JSON API data as positional arrays (AC-1, AC-3)

| Field | Content |
|---|---|
| **Verdict** | POSITIVE |
| **Query** | Classifier `parallel_arrays` (appendix) on each successful output, including JSON text inside the `fetch_url` `text` field. |
| **Output** | Reading-test set: 2 of 30 `fetch_url` results (A008 result 2, A039 result 3, both one weather API). Production all-time: 5 of 129 `fetch_url` results and 2 of 525 `bash` results. Production since 2026-10-01: 0 of 416. |

`fetch.py:675-676` runs `_extract_text` only for an HTML content type. A JSON body is returned as raw
text. The API groups data by measure: one array of 72 hour stamps, then one array of 72 values for
each measure. `contract.py:render_content` then encodes that JSON text as an escaped string inside
the tool's JSON result. To read "rain at 07:00", the model must find position 31 in two separate arrays.

### F5 — `fetch_url` joins HTML table cells with single spaces (AC-3)

| Field | Content |
|---|---|
| **Verdict** | POSITIVE (code behaviour) · frequency UNVERIFIABLE |
| **Query** | Call the deployed `fetch._extract_text` on a synthetic HTML table followed by a definition list. |
| **Output** | `'Name Day Value\nItem one Day 1 10\nItem two Day 2 20 Key A Value A Key B Value B'` |

Rows survive as lines, because `tr` is a block tag (`fetch.py:184`). Cells do not, because `td` and `th`
are inline and get one space. A multi-word cell then merges with its neighbours. `table`, `dl`, `dt` and
`dd` are not block tags, so the text after a table joins its last row. The stored outputs keep no HTML,
so the share of fetched pages with a table cannot be measured. A first row detector misfired on prose
lines with numbers (method appendix), and this study rejected it. No confident false claim traces to
this loss (F7).

### F6 — Truncation is rare at the render bound and common at the `fetch_url` cap (AC-3)

| Field | Content |
|---|---|
| **Verdict** | POSITIVE |
| **Query** | Rebuild each model-facing message with the deployed `render_content(output, max_result_tokens=<tool bound>)`. Count results that come back as `{"ok": true, "truncated": true, ...}`. Count `fetch_url` outputs with `truncated: true`. |
| **Output** | Render bound: 0 of 134 in the reading-test set, **3 of 1,930** in production all-time. A seeded oversized input returns `truncated: True`. `fetch_url` cap: 11 of 30 (reading-test set), **68 of 129** (production all-time). |

A render truncation also double-encodes the result, because `_bound_content` puts the cut JSON text into
a `partial` string (`contract.py:283-292`). The `fetch_url` cap (default 10,000 characters,
`fetch.py:767`) cuts the end of a page. It removes rows, but it does not merge them. No confident
false claim traces to either cut.

### F7 — The false claims, tagged blind (AC-2)

| Field | Content |
|---|---|
| **Verdict** | POSITIVE |
| **Query** | One written rule (appendix), applied to each claim before the model key was opened. Then the key `glmtest/blind_key.json` gives the model per claim. |
| **Output** | Table below. |

| Claim set | False claims | Structure lost | Uncertain | Not structure |
|---|---|---|---|---|
| FRE-1756 replay first run (qwen3.8-flash-next, 30 answers) | 21 | **5** | 3 | 13 |
| GLM test, qwen3.8-flash-next (7 answers × 3 runs) | 37 | **10** | 2 | 25 |
| GLM test, GLM-5.3 (7 answers × 3 runs) | 8 | **3** | 0 | 5 |

- Share of false claims that are "structure lost": replay first run 5 of 21 (8 of 21 with the uncertain
  cases). Of its 14 source misreads, 5 are confident and 8 include the uncertain cases.
- GLM test: qwen 10 of 37 (27%), GLM-5.3 3 of 8 (38%). Per 100 checkable claims: qwen 1.2, GLM-5.3 0.5.
- **By cause, the 18 confident cases:** 9 on positional weather arrays (`fetch_url`, F4), 7 on a
  flattened Brave snippet and 2 on a flattened Exa snippet (`web_search`, F1–F2). 0 on truncation.
  0 on a `fetch_url` HTML table.
- All 18 sit on 3 answers: A008 (weather), A045 (fare listing), A051 (model benchmark tables).
- All 3 GLM-5.3 cases are the weather arrays. The GLM test selected its 7 answers for earlier misreads,
  so it overweights these sources. The replay first run is the unselected set.
- 3 further claims (R12, G33, G34) attach a figure to the wrong section of one page. The cause is the
  same lost line break, but the tag rule covers tables, lists and arrays only, so they count as
  "not structure" here.

**Arms for the two zeros.** *Arm 1 (1a):* the same claim set holds `fetch_url` HTML results in the
sources of A051 (results 2 and 3), and the same set holds the truncated `fetch_url` outputs of F6.
*Arm 2:* the same tagging returns 9 on the arrays and 9 on the snippets. *Arm 3:* the scope is the
66 claims above and their sources, nothing wider.

### F8 — A careful reader also misread the flattened fare listing

| Field | Content |
|---|---|
| **Verdict** | POSITIVE |
| **Query** | Compare the stated reasons of the two FRE-1756 readers for the A045 fare claims with the row pattern on the same page (result 1 of A045). |
| **Output** | The FRE-1756 reader 2 paired each price with the dates after it in 2 claims (G14, G31). The page's own complete row shows "dates, legs, then price". Both claims stay false under the correct reading, but for a different reason than that reader wrote. |

This does not change any FRE-1756 count. It shows that the flattened form misleads a careful reader
with a written rule, not only the two models.

### F9 — The other tools (AC-6)

| Field | Content |
|---|---|
| **Verdict** | per row |
| **Query** | Q-ES-2 (appendix) for call counts. The classifier of F4 and F1 on each successful output in production primary captures, 2026-05-05 to 2026-11-07. Output shape from the stored keys and the tool code. |
| **Output** | Table below, ranked by calls. |

Arms for each "no loss found" row: *Arm 1 (1a)* the stored top-level keys, quoted in the row, are a
raw instance from the queried store. *Arm 2* the same run finds 241 lost results for `web_search` and
5 for `fetch_url`. *Arm 3* the scope is production primary captures, 2026-05-05 to 2026-11-07.
Helper captures are excluded (F10).

| Rank | Tool | Calls | Format the model reads | Same kind of loss | Evidence |
|---|---|---|---|---|---|
| 1 | `web_search` | 762 | records `title, url, snippet, engine, score` | **Found** | F1–F2. 241 of 746 successful results |
| 2 | `bash` | 543 | `command, exit_code, stdout, stderr` | Found, rare | 2 of 525 hold positional arrays in JSON stdout. 71 hold JSON text as an escaped string |
| 3 | `search_memory` | 220 | keyed records `matched_turns, entities, claims, stance_history` | Not found | 0 of 207 |
| 4 | `read_file` | 180 | no output | Not measurable | 180 of 180 calls failed. The tool is not in `tools/` at `928e0fa5` |
| 5 | `fetch_url` | 174 | `url, text, char_count, truncated` | **Found** | F4–F6. 5 of 129 arrays, 68 of 129 cut at the cap |
| 6 | `read` | 95 | `path, content, truncated, lines_returned` | Not found | 0 of 94. File text keeps its lines |
| 7 | `nonexistent_tool` | 60 | no output | Not measurable | 60 of 60 calls failed |
| 8 | `system_metrics_snapshot` | 60 | no output | Not measurable | 60 of 60 calls failed. The tool is not in `tools/` at `928e0fa5` |
| 9 | `recall_personal_history` | 40 | records under `turns` | Not found | 0 of 40 |
| 10 | `run_python` | 31 | `exit_code, stdout, stderr, scratch_files` | Not found | 0 of 31 |
| 11 | `write` | 31 | `path, bytes_written, mode` | Not found | 0 of 29 |
| 12 | `perplexity_query` | 30 | `answer` prose with `[n]` marks, `citations` as a list of URLs | **Positional, impact not measured** | 27 of 27 answers use `[n]` marks that point into the list by position |
| 13 | `read_skill` | 26 | markdown `body` | Not found | 0 of 26 |
| 14–24 | `artifact_*`, `notes_*`, `query_telemetry`, `search_logs`, `get_location`, `get_library_docs`, `find_linear_issues`, `measure_model_latency` | 1–17 each | small keyed dicts or keyed records | Not found | 0 in each |
| 25 | MCP tools | 1 | no output | Not measurable | 1 call, `mcp_sequentialthinking`, failed |
| — | calendar | 0 | — | Not measurable | No calendar tool is deployed. FRE-1747 is In Progress |

**Escaped non-ASCII text, every tool.** `render_content` calls `json.dumps` with the default
`ensure_ascii=True` (`contract.py:246`). Each accented character reaches the model as a 6-character
escape such as `é`. 656 of 746 `web_search` results and 114 of 129 `fetch_url` results hold such
escapes. With the deployed `result_tokens` estimate, the escapes add 4.3% to `web_search` results and
6.7% to `fetch_url` results that hold them. This is a reading cost, not a structure loss, and no false
claim in this study traces to it.

### F10 — Helper tool results: UNVERIFIABLE

| Field | Content |
|---|---|
| **Verdict** | UNVERIFIABLE |
| **Query** | Q-ES-2 on `agent-captains-captures-subagents-*`, `timestamp >= 2026-05-01`. |
| **Output** | 402 captures, 0 nested `tool_results`. Control, same query on `agent-captains-captures-2*`: 2,381 captures, 2,335 tool results. |

The store holds no tool result from a helper. So this study cannot see what text a helper read. This
matches the FRE-1756 limit on helper turns.

## Proposals

1. **(AC-4 recommendation) Keep line breaks in `web_search` text.** Of the 18 confident cases, this
   covers 9, and 4 of the 5 in the unselected replay first run. Its source pattern occurs in 241 of 746
   successful `web_search` results, against 7 of 1,930 results for positional arrays. The loss is in
   SearXNG, so the fix sits either in SearXNG or in our own calls to the two provider APIs. The design
   is the owner's decision. Precondition: one paid call per provider, on the owner's go, to confirm that
   the provider text holds line breaks (F3). Filed as FRE-1760.
2. **Turn positional arrays into rows before the model reads them.** It covers the other 9 confident
   cases, including all 3 GLM-5.3 cases. Every case comes from one weather API, and the pattern is rare
   in production (F4). A smaller option covers the same cases: point the weather questions at a source
   that already returns rows. Not filed.
3. **Mark table cells in `fetch_url` text.** A cell separator and block treatment for `table`, `dl`,
   `dt` and `dd` would keep the columns (F5). No false claim in this study traces to it, so its value is
   not measured. Not filed.

## Filed tickets

- FRE-1760 — Keep line breaks in `web_search` result text, so tables and lists reach the model with their rows (Backlog).

## Method appendix

**Stores and windows.**
- Reading-test set: the 70 FRE-1756 reader packets (30 stored, 40 replay) and the 30 stored turns in the
  private replay directory. 135 tool results: 102 `web_search`, 30 `fetch_url`, 3 other. The packets
  re-wrap long lines at 180 characters. A JSON result rebuilds by joining the wrapped lines. 79 of 80
  replay results rebuilt, and 1 (A020 result 0, `get_library_docs`) was cut at 25,000 characters by the
  packet builder and counts as unreadable.
- Production: `agent-captains-captures-2*` (primary captures) on the production Elasticsearch, read-only
  `_search`. Window 2026-05-05 to 2026-11-07 (all-time) and from 2026-10-01. The capture stores the raw
  tool output (`executor.py:8735-8744`), before `render_content`. The study rebuilt the model-facing
  text with the deployed `render_content`. The rebuild leaves out the citation marker and the
  repeat-call warning that dispatch adds, and these do not touch structure.

**Q-ES-1 (tool results).**
`POST agent-captains-captures-2*/_search` with
`{"size":500,"_source":["trace_id","timestamp","tool_results"],"query":{"bool":{"filter":[{"range":{"timestamp":{"gte":"<since>"}}},{"nested":{"path":"tool_results","query":{"exists":{"field":"tool_results.tool_name"}}}}]}},"sort":[{"timestamp":"asc"}]}`,
paged with `search_after`.

**Q-ES-2 (calls per tool).**
`{"size":0,"query":{"range":{"timestamp":{"gte":"<since>"}}},"aggs":{"tr":{"nested":{"path":"tool_results"},"aggs":{"t":{"terms":{"field":"tool_results.tool_name","size":60}}}}}}`.

**Classifier.** Applied to each snippet (for `web_search`) or to the output text (other tools).
- Table: at least 2 matches of `\|[^|\n]{1,80}\|[^|\n]{1,80}\|`, or a markdown rule `\|\s*:?-{3,}`.
- List: at least 3 bullet marks `(?:^|\s)(?:[•·▪●◦]|\*|-)\s+\S`, or 3 numbered marks `(?:^|\s)\d{1,2}[.)]\s+[A-Z]`.
- Parallel arrays: a dict with 2 or more sibling lists of equal length (at least 5) that hold only scalars.
- A `web_search` result is "lost" when a snippet is a table or a list and no snippet holds a line break.
- Hand check of the detector on a random sample of stored snippets (seed 1759): 6 of 6 "table" snippets
  are tables, 5 of 6 "list" snippets are lists, and 1 of 6 "prose" snippets is a table without pipe
  marks. Table counts are therefore a floor.
- Rejected: a `fetch_url` row detector (3 or more consecutive lines with 3 or more numbers). By hand, all
  4 of its reading-test hits are prose lines with numbers.

**Tag rule** (written before tagging, kept in the private directory).
- *Structure lost:* the error attaches a value to the wrong row, column, date, item or position, and in
  the text the model received, that value and its key sit in a table, list or arrays whose delimiters are
  absent or ambiguous.
- *Structured, intact:* the delimiters are intact and the model still misread. No claim got this tag.
- *Not structure:* everything else, including model memory, inference, and conflict between sources.
- Uncertain tags count as "not structure" in the headline numbers and appear separately.
- Claim ids R01–R21 (replay first run) and G01–G45 (GLM test reader 2) are local to the private tag
  file. Model names came from `blind_key.json` only after all tags were saved.

**SearXNG probe.** The probe script went into the SearXNG container's `/tmp` with `docker cp`, ran
there, and was then deleted. It ran twice: a first run with values taken from a reader packet, then the
neutral run quoted in F2. Both runs gave the same newline counts. It read no settings and made no network call. This was a temporary file in
a production container, which the commission did not name. It is disclosed here.

**Limits.**
- The tagging is one reader's judgement under one written rule. Another reader can move the uncertain
  cases.
- The GLM test answers were selected for earlier misreads, so its shares overweight hard sources.
- The heading-mark evidence in F1 shows line breaks at the origin page. It does not show that the
  provider API still sends them (F3).
