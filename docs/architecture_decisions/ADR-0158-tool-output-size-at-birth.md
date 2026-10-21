# ADR-0158: Tool Output Size at Birth — Search Returns Pointers, `fetch_url` Returns Focused Passages

**Status:** Accepted
**Date:** 2026-10-21
**Deciders:** Owner (design decisions, 2026-10-20 and 2026-10-21), adr session (analysis and authoring)
**Tags:** context, tools, web_search, fetch_url, cost, prompt-cache, sub-agents

---

## Context

### The problem the owner saw

On the owner's `gpt-6.1-sol` turn of 2026-10-20 (trace `00ee8f34…`), each of three research helpers grew its prompt from 2,598 tokens in round 1 to 99,214 to 117,061 tokens in its landing call. The turn cost $2.09 (Postgres `api_costs`). The Flash-Next Deep run (trace `57198efc…`) shows the same growth: its three helpers absorbed 128K, 131K and 243K characters of tool output.

The cause is structural. Every tool result enters the helper's messages verbatim (`orchestrator/sub_agent.py:2261`, `_absorb`) and stays there for every later round. The primary does the same with its own tool results.

### What fills the context: search results, then pages

FRE-1636 named fetched pages as the cause. The helper captures say otherwise. Over the 107 helpers captured since 2026-09-20 (ES `agent-captains-captures-subagents-*`, field `rounds[]`):

| Tool | Share of absorbed characters | Calls | p50 result | p90 result |
| -- | -- | -- | -- | -- |
| `web_search` | **77.5%** | 508 | 13,683 | 19,419 |
| `fetch_url` | 15.4% | 173 | 6,279 | 10,847 |
| all other tools | 7.1% | — | — | — |

On the Flash-Next trace, `web_search` was 100%, 92% and 69% of each helper's intake.

The size of a search result comes from Exa. Exa joined the `general` category on 2026-09-10 with `content_mode: highlights` and `content_max_characters: 2000` (`docker/searxng/settings.yml.example`). That gives about 1,926 characters per Exa result, against braveapi's 217. The SearXNG engine's own default is 500. A search with 5 to 10 results returns 10K to 24K characters.

`fetch_url` cuts every page from the top at `max_chars`: 10,000 characters by default, at most 50,000 (`tools/fetch.py:103`, `:406`, `:522`). On trace `00ee8f34`, 26 of 85 helper fetches returned 10,000 characters or more. The cut is positional: a needed passage in the middle of a long page is lost before the model sees it.

### Where the cost is, with a working cache

The cost anatomy of trace `00ee8f34` decides where a fix can work:

| Part | Cost | Time |
| -- | -- | -- |
| 3 helper landing calls | $0.93 (45%) | 189 s |
| 33 helper round calls | $1.03 | about 7 s each |
| Primary (plan and final answer) | $0.12 | 94 s |

- **The landing calls lost the prompt cache.** Each sent 99K to 117K tokens with 0 or 2,319 tokens cached, while earlier rounds cached 85% to 95%. This is a separate defect. The owner chose to fix it separately, as FRE-1639. This ADR does not address it, but smaller results make every landing call smaller too.
- **In the round calls, a result costs most when it first arrives.** In helper 1, the tokens billed at full price total about 99K. The cached re-reads total about 487K tokens, at about one tenth of the price. So about two thirds of the round input cost is the first billing of each new result.

So a smaller result at birth saves the expensive part. A rewrite of older results saves only the cheap part (cached re-reads), and it breaks the cache, which bills the whole history at full price again.

### History: ADR-0085

ADR-0085 (FRE-475) added a digest that compressed tool results after the tool ran. It is parked behind `tool_result_compression_enabled` (default off) since 2026-06-05 (FRE-486). Two of its findings bind this ADR:

1. **Placement.** The "keep-deferred" placement rewrote older tool messages and added **+34.9%** fresh input from cache churn (PR #161, reverted). Only birth-time placement had no churn.
2. **A second lossy layer fights the tool's own contract.** The digest cut the middle out of file reads that came through `bash`. That destroyed the `grep -n` line maps the model used to read ranges (`docs/research/2026-06-05-tool-result-compression-park-decision.md`).

The exact mechanism of finding 2 does not apply to web results: web pages have no ranged-read contract, and `fetch_url` has no `offset` argument. The lesson does apply. `fetch_url` already cuts every page from the top at `max_chars` (10,000 by default). A digest after that cut is a second truncation layer again. The change must go into each tool's own output contract.

### What the quality guard can use

The stored traces hold result **sizes**, not result **content**. `rounds[]` records `result_chars` per call, and `tool_call_completed` has no output field. So no stored trace can replay a result through a smaller form.

The stored traces do hold **facts**. Each schema-backed helper report (`full_output`) holds `findings[]`, and each finding has a `claim` and a `source_url` (`orchestrator/worker_types.py:84`). Across all captures, 86 helper reports that used web tools hold 1,119 findings over 784 distinct URLs, and 788 of the claims contain a digit. The search queries are logged on `web_search_started` (field `query`, 2,169 events).

### The owner's decisions

Round 1, relayed by master on 2026-10-21 ("1 yes, 2 both, 3 separate, 4 reject"):

1. The ADR is about tool output size at birth: search first, pages second.
2. The change applies to the primary and to the helpers.
3. The landing-call cache miss is a separate ticket, FRE-1639.
4. A cheap-model summary of results is rejected on the FRE-1303 authorship ground, with no measurement.

Round 2, in the adr session on 2026-10-21:

5. SearXNG stays. Its config is tuned (Option A). Direct vendor clients are the named fallback (Option B), triggered by the harness result.
6. Search gives pointers, and `fetch_url` with `focus` gives depth.
7. The ranker is chosen by results, not fixed in advance: "judge the results. The decision does not need to be so binary."
8. The quality bar is 95% anchor retention.
9. No feature flag.

---

## Decision

### D1 — Shrink tool output at birth, inside each tool's result contract

1. The size of a result is set by the tool that makes it, before the result enters any message list.
2. This ADR adds no component that compresses, digests or rewrites a tool result after the tool returns it. In the normal round path, no earlier message changes between rounds. AC-3 tests that path.
3. **Overflow paths stay, and they are not size policy.** Three existing mechanisms change older tool content when a prompt does not fit:
   - the helper landing trim (`_trim_for_landing`, FRE-1522, `sub_agent.py:565`), which stubs old results in a copy of the messages for the landing call only;
   - the primary's context truncation and compaction (ADR-0061, ADR-0081, `executor.py:5967`, `:5983`);
   - the primary's recovery after a context-window rejection (`executor.py:7537`).
   This ADR does not change them. Smaller results make them fire less often. They remain recovery paths for a prompt that does not fit, not a way to make results small.
4. This applies to every caller of `web_search` and `fetch_url`: the primary and every helper (owner decision 2).
5. This extends ADR-0156 D6.4. Today D6.4 sets only a ceiling of 25,000 tokens. D1 adds a **default size** to `web_search` and `fetch_url`: the size that the D6 rule picks. The ceiling does not change.
6. ADR-0085 stays parked. This ADR does not use its digest.
7. **The size is logged for every caller.** Today only helper captures record `result_chars`, so the primary's result sizes are not visible. `tool_call_completed` is emitted before the contract renders the message (`tools/executor.py:946`), and the rendered content is made later in shared dispatch (`orchestrator/tool_dispatch.py:343`). So shared dispatch emits a new event, `tool_result_rendered`, once per call after rendering. It holds `tool_name`, `result_chars` (the length of the rendered tool message), `worker` (absent for the primary), `categories` for `web_search`, and `focus_present` for `fetch_url`. `tool_call_completed` does not change. AC-4 and AC-5 read the new event.
8. **One code path for the harness and the tools.** The D5 harness calls the production shaping functions of `web_search` and `fetch_url`. It does not copy them. So the form that the harness scores is the form that the deployed tool returns for the same raw input. The harness report records the git commit of the code it scored. AC-2 compares it with the deployed commit.

### D2 — `web_search` returns pointers

1. **Exa highlight length.** `content_max_characters` for the `exa` engine drops from 2,000 to the value that the D6 rule picks from the grid {500, 800, 1,200, 1,600}. The grid crosses each length with a default result count of 5 and of 10 (`searxng_max_results`). That gives 8 search cells.
2. **Fields removed from each result:** `engine` and `score`. The model does not need them to choose or cite a result. The FRE-1339 health fields (`unresponsive_engines`, `engines_contributed`, `degraded_retrieval`, `retrieval_warning`) stay.
3. **Field added to each result:** `published_date`, from the SearXNG result's `publishedDate` when the engine supplies it. SearXNG's `exaapi` engine already parses it, and `tools/web.py` drops it today. Helper findings carry a `date_or_period` field, so the date is a fact the model needs.
4. **Description.** The `web_search` description states that a snippet is a short excerpt. To read more of a page, the model calls `fetch_url` with `focus`.
5. **Citability does not change.** The primary's prompt says "web_search results are directly citable" (`orchestrator/prompts.py:148`). That stays true: a shorter snippet is still the vendor's own text. This ADR does not change the grounding contract, and it does not change `prompts.py`.
6. **Vendor cost does not change.** Exa does not bill highlights on top of the search ($0.007 per search, measured 2026-10-11).

### D3 — `fetch_url` gets an optional `focus` argument

1. **The parameter.** `focus` is an optional string of at most 200 characters: the question that the page must answer.
2. **With `focus`:**
   - The tool ranks the extracted page text up to a bound of 200,000 characters (`fetch_focus_max_chars`), not only its first `max_chars` characters. Text beyond the bound is not ranked, and the result says so.
   - It splits that text into passages. A passage is a block from the extractor's line breaks. Blocks shorter than 200 characters merge with the next block, and blocks longer than 800 characters split at a sentence end.
   - It ranks the passages with the arm that D4 picks and returns the best passages **verbatim**, in page order, within a character budget that the D6 rule picks from the grid {2,000, 3,000, 4,000}. Every focused result fits the budget plus a fixed allowance of 1,500 characters for its fields.
   - **`focus_matched` has one definition for every arm:** at least one returned passage holds a term of the `focus` that is not a stop word. The definition is lexical and does not depend on the ranker's score. T3 tests it with a seeded page that holds no `focus` term.
   - The result holds `passages: [{index, text}]`, `passages_total`, `chars_total` (the size of the text extracted from the downloaded body), `chars_ranked`, `focus` and `focus_matched`.
   - **The download gets a bound too.** Today the client reads the whole response into memory before any cap (`tools/fetch.py:433`, `:458`). D3 makes the tool read the body as a stream and stop at 5 MB (`fetch_max_body_bytes`). A body cut at that bound sets `body_truncated: true` and `body_bytes_read`, plus `content_length` when the server sent that header. The bound applies with and without `focus`.
   - `next_action` tells the model how to see more: call again with a different `focus`, or with no `focus`.
3. **Today's form stays available.** A call without `focus` returns today's cut from the top. If no passage matches the `focus`, the tool returns that cut too and sets `focus_matched: false`. A focused result can still omit text that today's cut holds. The D5 bar limits that loss on the fixtures. It does not remove it.
4. **Without `focus`,** the behavior does not change, except for the D3.2 download bound. `max_chars` keeps its range of up to 50,000.
5. **Telemetry.** The tool logs `focus_present` and `focus_matched` on `fetch_url_completed`. The live adoption criterion (AC-6) reads them.
6. **No silent truncation (ADR-0125 D5).** A tool result is assembled context, so every shortened form records that it is shortened and by how much. A focused result carries `chars_total`, `chars_ranked`, `passages_total` and the index of each returned passage. The fallback cut from the top carries `truncated` and `chars_total`. A `web_search` snippet is the vendor's own excerpt at the configured length, and the tool does not cut it further. When the tool keeps fewer results than SearXNG returned (`capped_max`, `web.py:333`), the result carries `results_available`, the count before the cut.
7. **Verbatim only.** The tool never rewrites, joins or paraphrases a passage. The text the model reads is the page's own text, so the source stays the author (FRE-1303).
8. **Description.** The `fetch_url` description explains `focus` with one example call. The researcher prompt does not change. Its rule "Record a claim only when a fetched result states it" (`worker_types.py:279`) stays, and `focus` makes that fetch cheap.
9. **Out of scope:** PDF and other non-HTML bodies. Today they come back as raw text (`tools/fetch.py:458`). Passage ranking works on any text, but extraction of PDFs is a separate problem.

### D4 — The passage ranker is chosen by results, from fixed arms

The ADR fixes the arms and the rule. The harness picks the arm and the budget (owner decision 7).

| Arm | Description |
| -- | -- |
| R1 | BM25 over all ranked passages, local |
| R2 | Voyage `rerank-2.5` over the first 100 passages |
| R3 | BM25 picks the top 20 passages, then `rerank-2.5` orders them |
| R4 | R1 for a page with at most 50 passages, R3 for a larger page |

Each arm runs at each of the 3 passage budgets. That gives 12 fetch cells.

For each cell, the harness reports anchor retention (D5), added latency per fetch, cost per fetch, and whether page text leaves the host. **The cheapest arm that passes the D5 bar at any budget ships, with its smallest passing budget.** "Cheapest" orders by cost per fetch, then added latency. A remaining tie follows the fixed order R1, R4, R3, R2. If no arm passes, the harness table goes to the owner before any `fetch_url` code ships.

### D5 — Quality guard: anchor retention on recorded fixtures, with a preregistered bar

1. **Facts.** The facts are stored helper findings (`full_output.findings[]`) that have a `source_url`. Each finding's **anchors** are the tokens of its `claim` that are numbers (with decimals, percents and thousands separators normalized), dates, or capitalized names. A finding with no anchor is excluded.
2. **Record step.** The harness records the raw material once, stores it outside git, and scores offline.
   - **Fetch fixtures:** the extracted text of each finding's `source_url`, up to the D3 bound, made by the production extraction (D1.8).
   - **Search fixtures:** the logged `query`, `categories`, `engines` and `time_range` values of the same helpers (`web_search_started`), run again through a **separate SearXNG instance** for each of the 4 Exa lengths. Each instance uses the production `settings.yml` with only `content_max_characters` changed. So the merge, the duplicate removal and the order are SearXNG's own, as in production. The result-count axis is applied afterward by the production shaping in `tools/web.py` (`capped_max`, `web.py:333`). The harness instances run under their own compose project name, never the production project, and stop after the run.
   - **Inputs that the logs do not hold.** `web_search_started` logs the query cut at 120 characters (`web.py:247`), and it does not log `language` or an explicit `max_results`. A search whose logged query is 120 characters long is excluded, because its full text is unknown. The replay uses the production defaults for `language` and the grid count for `max_results`. A historical call with a non-default `language` is therefore replayed with the default. This is a known limit of the replay, accepted rather than hidden. D1.8 still holds, because it is about the code, not the inputs. T2 adds `max_results` and `language` to `web_search_started`, so later runs can replay them.
3. **Support is tied to the source and to one place in it.**
   - **Fetch:** a finding is **supported** if one window of 600 characters in the fixture for its own `source_url` holds its support set. The support set is at least two of its anchors, or its only anchor. The harness records the character offsets of that window. If more than one window qualifies, it records all of them.
   - **Search:** a finding is **supported** if its `source_url` is among the results of a recorded search of the same helper at the full form (2,000 characters, 10 results), and that result's own snippet holds the support set.
   - Co-occurrence of anchors is a proxy for support. It does not test the relationship that the claim states. This is a known limit of the instrument, accepted rather than hidden. For fetch, D5.6 reduces it: a passage counts only if it covers a recorded support window, so other text with the same anchors does not count. For search, a shorter Exa highlight is a new excerpt, not a slice with offsets, so the search check stays at anchors in the same result's snippet.
4. **Drift.** A finding that the full form does not support is dropped as **drifted**. Drift is expected, because pages and rankings change after the helper ran. The harness reports the drop rate for each tool.
5. **Minimum sample.** A run counts only if it has at least 200 supported fetch findings and at least 150 supported search findings. Otherwise the run has no result, and the harness table goes to the owner. A task class (D6.3) is scored on its own only if it has at least 50 supported search findings.
6. **Retention.** A supported finding is **kept** by a smaller form if the form, for the same `source_url`, holds every anchor of the support set. For a fetch, the returned passages must cover the character offsets of one recorded support window. Passages are verbatim slices with known offsets, so this is exact.
7. **The bar: at least 95% of supported findings kept,** scored separately for search and for fetch (owner decision 8). The bar is fixed by this ADR, before any run.
8. **Seeded negative.** A 200-character cut from the top of each fetch fixture, and a 100-character snippet for each search result, are scored with every run. Each must fail the bar. If one passes, the instrument is broken and no result of that run is used.
9. **The focus proxy.** Offline, the harness has no model to write a `focus`. It uses the helper's stored task text (`spec_task`) as the `focus`. This is a proxy. AC-6 checks live use.

### D6 — Selection rule, rollout and revert

1. **For search, the smallest cell that passes ships.** A search cell passes if it meets the bar on the pooled search findings **and** on each task class that D5.5 scores on its own. "Smallest" means the fewest characters in the median result on the fixtures, over the 8 cells of D2.1. A tie goes to the shorter highlight, then to the lower result count. **For fetch, D4's rule applies** over its 12 cells: the cheapest passing arm, then its smallest passing budget.
2. **No feature flag** (owner decision 9). The measured values ship as defaults. The revert path for sizes is config: `content_max_characters`, `searxng_max_results`, `fetch_focus_max_chars`, `fetch_max_body_bytes` and the passage budget are settings. The revert path for the `focus` mode itself is a code revert of T3. The owner accepted that cost when choosing no flag.
3. **Option B trigger.** Option B (direct Brave and Exa clients for `general`, with SearXNG kept for the specialist categories) becomes a decision for the owner if **no search cell passes under D6.1**. Then no search change ships. The classes are the helper's `thoroughness`: `quick` against `standard` and `thorough`. A class below the D5.5 class minimum is reported and is not scored on its own. Since 2026-09-20, only 4 of 71 web-using helpers ran `quick`. A per-call length needs the direct client, because SearXNG sets Exa's length per engine, not per request.

### D7 — Scope boundaries

1. The landing-call cache miss is FRE-1639.
2. Other tools (`query_telemetry`, `search_memory`, `run_python` and the rest) are 7.1% of helper intake. They are not in scope.
3. The primary's own context management (ADR-0081 compaction, ADR-0061) does not change.

---

## Alternatives Considered

### Option 1: Cheap-model summary of each result (the `compressor` role)

**Description:** A small model summarizes each search result or page before it enters the messages.

**Pros:**
- Large reduction for both tools.
- Can follow the task's intent better than a lexical ranker.

**Cons:**
- The text the helper reads becomes another model's statement under a retrieval's URL. Under the FRE-1303 authorship rule, that is not external evidence.
- One extra model call per result: added latency and cost on every search and fetch.
- A summary can drop or change a number without any trace of the change.

**Why Rejected:** The owner rejected it on the authorship ground, with no measurement (owner decision 4).

### Option 2: Replace older rounds' results with short notes, keep the newest verbatim

**Description:** After each round, rewrite earlier tool messages into short notes.

**Pros:**
- Bounds the context at a fixed size, whatever the result sizes.

**Cons:**
- It saves the cheap part. Cached re-reads cost about one tenth of a fresh token, and they are about one third of round input cost.
- It breaks the cache from the first changed message. ADR-0085 measured +34.9% fresh input from this churn (PR #161).
- The landing report loses the source text that it cites.

**Why Rejected:** It attacks the cheap part of the cost and makes the expensive part larger.

### Option 3: Enable the parked ADR-0085 digest for web tools

**Description:** Turn on `tool_result_compression_enabled` with an allowlist for `web_search` and `fetch_url`.

**Pros:**
- The infrastructure exists in `orchestrator/tool_result_digest.py`.

**Cons:**
- Its only lossy primitive for unstructured text is a head/tail cut that deletes the middle. A page is already cut from the top at `max_chars`, so this is a second positional cut.
- It is a layer after the tool, which is the pattern ADR-0085's park decision warns against.

**Why Rejected:** A positional cut cannot keep a passage it does not know is needed. D3 ranks the page, up to its 200,000-character bound, instead.

### Option 4: Direct Brave and Exa clients now (Option B)

**Description:** `web_search` calls Brave and Exa directly for `general`, and keeps SearXNG for the specialist categories.

**Pros:**
- Exa's length, result count and highlight query can be set per call.
- Exa's per-call cost (`costDollars`) becomes visible.

**Cons:**
- Two native clients (Tier 1 under ADR-0028), the egress guard on both, and a merge step that SearXNG does today.
- It may not be needed. One fixed length can pass the bar.

**Why Rejected:** Rejected for now, not forever. D6.3 names the harness result that makes it a decision for the owner.

### Option 5: A per-call `brief`/`full` switch on `web_search`

**Description:** The model chooses a short or a long highlight on each search.

**Pros:**
- The model can ask for more text when one search must answer the question.

**Cons:**
- SearXNG sets Exa's length per engine. The switch needs two Exa engine entries, or Option B.
- `fetch_url` with `focus` already gives depth, from the page itself.

**Why Rejected:** D3 covers the same need with one mechanism.

---

## Consequences

### Positive Consequences

- The primary and every helper absorb less text per web tool call. The AC-4 target is half of today's 13,683-character search result.
- The prompt cache stays intact, because no earlier message changes.
- A page passage in the middle of a long page can now reach the model. Today the 10,000-character cut from the top drops it.
- Search results carry a publication date.
- Every landing call is smaller. FRE-1639 fixes its cache miss, and D1 reduces what it bills.

### Negative Consequences

- A shorter Exa highlight can hold fewer facts per search. The bar limits this, but a shorter highlight can still move work from search to fetch.
- `fetch_url` gets a second mode, more code and more tests.
- If D4 picks R2, R3 or R4, page text leaves the host to a vendor, and each fetch costs a vendor call.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The model does not pass `focus`, so pages stay at `max_chars` (10,000 by default) | Medium | AC-6 measures the share of calls with `focus`. If it is low, the fix is the description, not more machinery. |
| The `spec_task` proxy ranks differently from the model's own `focus` | Medium | D5.9 names the proxy. AC-6 and AC-7 read the live behavior. |
| The fixtures drift from what the helper saw | Medium | D5.4 drops drifted findings and reports the rate. D5.5 sets a minimum sample, so a small survivor set cannot pass. |
| The anchor check misses a paraphrased fact | Low | Anchors are numbers, dates and names, which a verbatim form keeps or loses as tokens. The seeded negatives (D5.8) prove that the check can fail. |
| A shorter search snippet hides the result that holds the answer | Medium | The result count is in the grid. The bar is scored on search alone. |

---

## Implementation Notes

**Ticket sequence** (filed under FRE-1636):

1. **T1 — Fixtures and the anchor-retention harness** (a new folder under `scripts/eval/`). Reads ES (read only) and the web. Runs the harness SearXNG instances of D5.2. Writes fixtures outside git. Implements D5, both seeded negatives, the 8 search cells of D2.1 and the 12 fetch cells of D4. To score the fetch cells, T1 needs the passage split and the rankers as functions. T1 builds them in the module that T3 later wires into `fetch_url`, so the code is shared (D1.8). Gives a preflight cost and time projection before the full run. Blocks T2 and T3.
2. **T2 — `web_search` shrinks, and sizes are logged for every caller** (`tools/web.py`, `orchestrator/tool_dispatch.py`, `docker/searxng/settings.yml.example`, and the live `settings.yml` value, which master applies). Implements D2 with the cell that T1 picks, the D1.7 event `tool_result_rendered`, `results_available` (D3.6), and `max_results` and `language` on `web_search_started` (D5.2).
3. **T3 — `fetch_url` `focus` mode** (`tools/fetch.py`, `config/settings.py`). Wires D3 with the arm and budget that T1 picks, the 5 MB streamed download bound, and the seeded page for `focus_matched`. Also projects cost and latency on stored traces `00ee8f34` and `57198efc`. Blocked by T1 and T2, because the projection needs both ratios.

**Files affected:** `src/personal_agent/tools/web.py`, `src/personal_agent/tools/fetch.py`, `src/personal_agent/orchestrator/tool_dispatch.py`, `docker/searxng/settings.yml.example`, `src/personal_agent/config/settings.py` (`fetch_focus_max_chars`, `fetch_max_body_bytes`, the passage budget), and a new folder under `scripts/eval/`.

**Constraints:**
- The harness never writes to production substrate (FRE-375).
- The harness SearXNG instances use their own compose project name. They never recreate the production `searxng` container.
- Vendor keys come from the secrets store and are never printed.
- The live `docker/searxng/settings.yml` is untracked and holds keys. Master changes the value there at deploy, by name only.

**Deploy class:** T2 and T3 are `seshat-gateway` rebuilds. T2 also restarts SearXNG.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria. They are checked on the umbrella ticket FRE-1636, after T1 to T3 are deployed. No single implementation ticket closes them.

**Baselines,** measured 2026-10-21 over the 71 helpers since 2026-09-20 that called `web_search` or `fetch_url`:
- `web_search` result p50: 13,683 characters (all categories; 93% of searches are `general`). Half is 6,841.
- `fetch_url` result p50: 6,279 characters, p90 10,847.
- Findings per report: mean 10.3, p50 9. 61 of 71 reports (86%) hold at least one finding.

- **AC-1 — Every shipped value passes the bar, and the instrument can fail.** **Check:** the T1 harness report. *Fails if* any of these is true:
  - the run has fewer supported findings than D5.5 requires;
  - the shipped search cell, or the shipped fetch cell, keeps less than 95% of supported findings;
  - a seeded negative of D5.8 reaches 95% in the same run;
  - the shipped cell is not the one that the D6.1 rule picks from the report.
- **AC-2 — The deployed tools return the form the harness scored.** **Check:**
  - `git diff <scored commit>..<deployed commit>` over the shaping code of `tools/web.py`, `tools/fetch.py` and the passage and ranker module is empty. The scored commit is in the T1 report (D1.8). The deployed commit is the gateway image's commit.
  - At deploy, master reads the live `content_max_characters` and the gateway's `searxng_max_results`, `fetch_focus_max_chars`, `fetch_max_body_bytes` and passage budget, by name, and compares them with the T1 report.
  - A committed test feeds a small synthetic page and a synthetic SearXNG response through the shipped tools and asserts the exact output, so CI catches a later change.
  *Fails if* the diff is not empty, any live value differs from the report, or the test fails.
- **AC-3 — No earlier message changes between normal rounds.** **Check:** a unit test drives a three-round helper loop with `web_search` and `fetch_url` results, with no overflow, and asserts that the serialized messages of round N are a byte-identical prefix of round N+1. *Fails if* any byte of an earlier message differs.
- **AC-4 — Search results are half their size, for helpers and for the primary.** **Check:** the D1.7 event `tool_result_rendered`, `tool_name: web_search` and `categories: general`, over the first 30 helper calls and, separately, the first 10 primary calls after the T2 deploy. *Fails if* the p50 `result_chars` of either group is above 6,841.
- **AC-5 — Focused fetches stay within their budget.** **Check:** the D1.7 event `tool_result_rendered` for `fetch_url` calls with `focus_present: true`, over the first 30 such calls after the T3 deploy. *Fails if* any one call's `result_chars` is above the shipped passage budget plus the 1,500-character allowance of D3.2.
- **AC-6 — The model uses `focus`, and it matches.** **Check:** `fetch_url_completed` over the first 30 helper `fetch_url` calls after the T3 deploy, however many turns that takes. `focus_matched` has the single lexical definition of D3.2, and T3's seeded page proves that it can be false. *Fails if* fewer than 50% have `focus_present: true`, or fewer than 80% of those have `focus_matched: true`.
- **AC-7 — Helpers still find facts.** **Check:** ES helper captures, the first 20 web-using helpers after the T3 deploy. *Fails if* the mean findings per report is below 8.3 (the 10.3 baseline minus 2), or fewer than 76% of reports hold at least one finding (the 86% baseline minus 10 points).

**Limitation, stated rather than hidden.** No automatic oracle exists for live answer quality. AC-1 is the quality guard, offline. AC-7 detects a large loss of facts but not a small one. AC-6 shows that `focus` matched, not that it picked the right passage.

---

## References

- FRE-1636 — umbrella ticket, owner decisions in its comments (2026-10-21)
- FRE-1639 — the landing-call cache miss, fixed separately
- ADR-0085 — intra-turn tool-result compression, parked; its keep-deferred churn (+34.9%, PR #161)
- `docs/research/2026-06-05-tool-result-compression-park-decision.md` — why ADR-0085 is parked
- ADR-0156 D6 — the tool contract and the 25,000-token ceiling that D1 extends
- ADR-0150 D1 — the worker report schema with `findings[].claim` and `source_url`
- ADR-0149 D6 — tools retained on the landing call to keep the cache
- ADR-0028 — tool integration tiers (Option B would be Tier 1)
- ADR-0125 D5 — no silent truncation on an evidence path (D3.6)
- ADR-0081, ADR-0061 — the primary's context compaction, unchanged
- FRE-1303 — the authorship-independence rule (Option 1 rejected)
- FRE-1331, FRE-1339 — Exa highlight mode and SearXNG engine health fields
- `src/personal_agent/orchestrator/sub_agent.py:2261` — `_absorb`, every result verbatim
- `src/personal_agent/tools/fetch.py:103`, `src/personal_agent/tools/web.py:325` — today's sizes and result shape
- `docker/searxng/settings.yml.example:270–306` — the Exa engine entry
- Evidence: ES `agent-captains-captures-subagents-*` (`rounds[]`, `full_output`), ES `agent-logs-*` (`web_search_started.query`), Postgres `api_costs` for traces `00ee8f34` and `57198efc`

---

## Status Updates

### 2026-10-21 - Implementation stopped after T1
**Changed By:** master, on the owner's decision
**Reason:** The owner: "cancel the rest of adr-0158. I don't think it is necessary." T1 (FRE-1641) shipped the harness and the passage module and found no search cell and no fetch cell at the 95% bar. The post-mortem on FRE-1641 found that today's fetch (the first 10,000 characters) keeps 97.5% of the supported facts, more than any focused cell (best: R2 × 4,000 at 92.2%), and that most of the search drift is real vendor variation. Meanwhile FRE-1639 (the landing-call cache, D7) and FRE-1635 (parallel cloud helpers) cut the owner's research turn from about 10 min and $2.08 to 3 min 53 s and $0.87, and the owner accepted that performance. T2 (FRE-1642) and T3 (FRE-1643) are canceled. D2, D3, D4 and D6 are not implemented. `src/personal_agent/tools/passages.py` stays only because the FRE-1641 harness imports it. AC-1 to AC-7 of this ADR are not adjudicated.

### 2026-10-21 - Accepted
**Changed By:** adr session, at master's gate request on PR #1324
**Reason:** The owner decided every item in two rounds on 2026-10-21, both recorded on FRE-1636. Round 1, relayed by master: "1 yes, 2 both, 3 separate, 4 reject" (frame, scope, FRE-1639 split, Option C rejected). Round 2, direct: SearXNG Option A with Option B as the fallback, search pointers with `fetch_url` `focus` for depth, the ranker chosen by results, the 95% bar, no feature flag, and "go". Codex review closed in 3 rounds with no blocking finding open. AC-1 to AC-7 stay open until FRE-1641 to FRE-1643 deploy.

### 2026-10-21 - Proposed
**Changed By:** adr session
**Reason:** The owner decided the frame, the scope, the rejected alternative, the ranker rule, the bar and the rollout across two rounds, and said "go".
