# ADR-0158: Tool Output Size at Birth — Search Returns Pointers, `fetch_url` Returns Focused Passages

**Status:** Proposed
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

`fetch_url` cuts every page at 10,000 characters from the top (`tools/fetch.py:103`). On trace `00ee8f34`, 26 of 85 helper fetches hit that cap. The cut is positional: a needed passage in the middle of a long page is lost before the model sees it.

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

The exact mechanism of finding 2 does not apply to web results: web pages have no ranged-read contract, and `fetch_url` has no `offset` argument. The lesson does apply. `fetch_url` already cuts every page at 10,000 characters. A digest after that cut is a second truncation layer again. The change must go into each tool's own output contract.

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
2. No component compresses, digests or rewrites a tool result after the tool returns it. No earlier message changes between rounds.
3. This applies to every caller of `web_search` and `fetch_url`: the primary and every helper (owner decision 2).
4. This extends ADR-0156 D6.4. Today D6.4 sets only a ceiling of 25,000 tokens. D1 adds a **default size** to `web_search` and `fetch_url`: the size that the D5 rule picks. The ceiling does not change.
5. ADR-0085 stays parked. This ADR does not use its digest.
6. **The size is logged for every caller.** Today only helper captures record `result_chars`, so the primary's result sizes are not visible. Tool dispatch logs `result_chars` (the length of the rendered tool message) and `worker` (absent for the primary) for each call. AC-4 reads these fields.

### D2 — `web_search` returns pointers

1. **Exa highlight length.** `content_max_characters` for the `exa` engine drops from 2,000 to the value that the D5 rule picks from the grid {500, 800, 1,200}. The grid crosses each length with a result count of 5 and of 10 (`searxng_max_results`).
2. **Fields removed from each result:** `engine` and `score`. The model does not need them to choose or cite a result. The FRE-1339 health fields (`unresponsive_engines`, `engines_contributed`, `degraded_retrieval`, `retrieval_warning`) stay.
3. **Field added to each result:** `published_date`, from the SearXNG result's `publishedDate` when the engine supplies it. SearXNG's `exaapi` engine already parses it, and `tools/web.py` drops it today. Helper findings carry a `date_or_period` field, so the date is a fact the model needs.
4. **Description.** The `web_search` description states that a snippet is a pointer. To cite a page or to read more of it, the model calls `fetch_url` with `focus`.
5. **Vendor cost does not change.** Exa does not bill highlights on top of the search ($0.007 per search, measured 2026-10-11).

### D3 — `fetch_url` gets an optional `focus` argument

1. **The parameter.** `focus` is an optional string: the question that the page must answer.
2. **With `focus`:**
   - The tool reads the whole extracted page text, not only its first `max_chars` characters.
   - It splits the text into passages, ranks them with the arm that D4 picks, and returns the best passages **verbatim**, in page order, within a character budget that the D5 rule picks.
   - The result holds `passages: [{index, text}]`, `passages_total`, `chars_total` (the size of the whole extracted page), `focus` and `focus_matched`.
   - `next_action` tells the model how to see more: call again with a different `focus`, or with no `focus`.
3. **The model is never worse off than today.** If no passage matches, the tool returns today's cut from the top and sets `focus_matched: false`.
4. **Without `focus`,** the behavior does not change. `max_chars` keeps its range of up to 50,000.
5. **Telemetry.** The tool logs `focus_present` and `focus_matched` on `fetch_url_completed`. The live adoption criterion (AC-5) reads them.
6. **Verbatim only.** The tool never rewrites, joins or paraphrases a passage. The text the model reads is the page's own text, so the source stays the author (FRE-1303).
7. **Description.** The `fetch_url` description explains `focus` with one example call. The researcher prompt does not change. Its rule "Record a claim only when a fetched result states it" (`worker_types.py:279`) stays, and `focus` makes that fetch cheap.
8. **Out of scope:** PDF and other non-HTML bodies. Today they come back as raw text (`tools/fetch.py:458`). Passage ranking works on any text, but extraction of PDFs is a separate problem.

### D4 — The passage ranker is chosen by results, from fixed arms

The ADR fixes the arms and the rule. The harness picks the arm (owner decision 7).

| Arm | Description |
| -- | -- |
| R1 | BM25 over the passages, local |
| R2 | Voyage `rerank-2.5` over all passages |
| R3 | BM25 picks the top candidates, then `rerank-2.5` orders them |
| R4 | A condition, for example R1 for pages below a size and R3 above it |

For each arm, the harness reports anchor retention (D5), added latency per fetch, cost per fetch, and whether page text leaves the host. **The cheapest arm that passes the D5 bar ships.** "Cheapest" orders by cost per fetch, then added latency, then a local arm before a vendor arm. If no arm passes, the harness table goes to the owner before any `fetch_url` code ships.

### D5 — Quality guard: anchor retention on recorded fixtures, with a preregistered bar

1. **Facts.** The facts are stored helper findings (`full_output.findings[]`) that have a `source_url`. Each finding's **anchors** are the tokens of its `claim` that are numbers (with decimals, percents and thousands separators normalized), dates, or capitalized names. A finding with no anchor is excluded.
2. **Record step.** The harness records the raw material once, stores it outside git, and scores offline:
   - **Fetch fixtures:** the whole extracted text of each finding's `source_url`, with the extraction that `fetch_url` uses.
   - **Search fixtures:** the logged `query` values of the same helpers (`web_search_started`), run again against Exa at each grid length with the request body that SearXNG's `exaapi` engine sends, and against SearXNG for braveapi.
3. **Support.** A finding is **supported** by a full fixture if at least two of its anchors appear in it, or its only anchor appears in it. The support set is the anchors that appear. A finding that the full fixture does not support is dropped as **drifted**, and the harness reports the drop count. Drift is expected, because pages and search rankings change after the helper ran.
4. **Retention.** A supported finding is **kept** by a smaller form if every anchor of its support set appears in that form.
5. **The bar: at least 95% of supported findings kept,** scored separately for search and for fetch (owner decision 8). The bar is fixed by this ADR, before any run.
6. **Seeded negative.** A 200-character cut from the top is scored with every run. It must fail the bar. If it passes, the instrument is broken and no result of that run is used.
7. **The focus proxy.** Offline, the harness has no model to write a `focus`. It uses the helper's stored task text (`spec_task`) as the `focus`. This is a proxy. AC-5 checks live use.

### D6 — Selection rule, rollout and revert

1. **The smallest value that passes ships.** This picks the Exa highlight length, the result count and the passage budget. Smaller means fewer characters in the median result on the fixtures.
2. **No feature flag** (owner decision 9). The measured values ship as defaults. The revert path is config: `content_max_characters`, `searxng_max_results` and the passage budget are settings, and a `fetch_url` call without `focus` keeps today's behavior.
3. **Option B trigger.** Option B (direct Brave and Exa clients for `general`, with SearXNG kept for the specialist categories) becomes a decision for the owner if **no Exa length below 2,000 passes the bar for both task classes**, where the class is the helper's `thoroughness` (`quick` against `standard` and `deep`). A per-call length needs the direct client, because SearXNG sets Exa's length per engine, not per request.

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
- Its only lossy primitive for unstructured text is a head/tail cut that deletes the middle. A page is already cut from the top at 10,000 characters, so this is a second positional cut.
- It is a layer after the tool, which is the pattern ADR-0085's park decision warns against.

**Why Rejected:** A positional cut cannot keep a passage it does not know is needed. D3 ranks the whole page instead.

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

- The primary and every helper absorb less text per web tool call. The AC-3 target is half of today's 11,833 characters.
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
| The model does not pass `focus`, so pages stay at 10,000 characters | Medium | AC-5 measures the share of calls with `focus`. If it is low, the fix is the description, not more machinery. |
| The `spec_task` proxy ranks differently from the model's own `focus` | Medium | D5.7 names the proxy. AC-5 and AC-6 read the live behavior. |
| The fixtures drift from what the helper saw | Medium | D5.3 drops drifted findings and reports the count, so a small sample is visible. |
| The anchor check misses a paraphrased fact | Low | Anchors are numbers, dates and names, which a verbatim form keeps or loses as tokens. The seeded negative (D5.6) proves that the check can fail. |
| A shorter search snippet hides the result that holds the answer | Medium | The result count is in the grid. The bar is scored on search alone. |

---

## Implementation Notes

**Ticket sequence** (filed under FRE-1636):

1. **T1 — Fixtures and the anchor-retention harness** (`scripts/eval/`). Reads ES (read only) and the web. Writes fixtures outside git. Implements D5, the seeded negative, and the D4 and D6 grids. Gives a preflight cost and time projection before the full run. Blocks T2 and T3.
2. **T2 — `web_search` shrinks** (`tools/web.py`, `docker/searxng/settings.yml.example` and the live `settings.yml` value, which master applies). Implements D2 with the value that the T1 harness picks, and the D1.6 size fields.
3. **T3 — `fetch_url` `focus` mode** (`tools/fetch.py`). Implements D3 with the arm and budget that T1 picks. Also projects cost and latency on stored traces `00ee8f34` and `57198efc`. Blocked by T1 and T2, because the projection needs both ratios.

**Files affected:** `src/personal_agent/tools/web.py`, `src/personal_agent/tools/fetch.py`, `docker/searxng/settings.yml.example`, `src/personal_agent/config/settings.py` (passage budget), and a new folder under `scripts/eval/`.

**Constraints:**
- The harness never writes to production substrate (FRE-375).
- Exa calls in the harness read the key from the secrets store and never print it.
- The live `docker/searxng/settings.yml` is untracked and holds keys. Master changes the value there at deploy, by name only.

**Deploy class:** T2 and T3 are `seshat-gateway` rebuilds. T2 also restarts SearXNG.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria. They are checked on the umbrella ticket FRE-1636, after T1 to T3 are deployed. No single implementation ticket closes them.

**Baselines,** measured 2026-10-21 over the 71 helpers since 2026-09-20 that called `web_search` or `fetch_url`: 11,833 characters per web tool call. 61 of 71 reports (86%) hold at least one finding.

- **AC-1 — Every shipped size passes the bar, and the instrument can fail.** **Check:** the T1 harness report for the shipped values: search retention and fetch retention are each at least 95% of supported findings, and the 200-character seeded negative is below 95% in the same run. *Fails if* a shipped value scores below 95%, or the seeded negative passes, or the shipped value is not the smallest value that passed.
- **AC-2 — No earlier message changes between rounds.** **Check:** a unit test drives a three-round helper loop with `web_search` and `fetch_url` results and asserts that the serialized messages of round N are a byte-identical prefix of round N+1. *Fails if* any byte of an earlier message differs.
- **AC-3 — Web tool calls are half their size.** **Check:** ES helper captures, the first 10 helpers after deploy that called `web_search` or `fetch_url`: the sum of `result_chars` for those two tools divided by their call count is at most 5,900. *Fails if* it is above 5,900 (half of 11,833).
- **AC-4 — The primary gets the same smaller results.** **Check:** ES, the D1.6 fields, over the primary's own `web_search` calls (`worker` absent) in the first 3 owner turns after the T2 deploy that make such calls: the p50 `result_chars` is at most 6,850. *Fails if* it is above 6,850 (half of today's 13,683 p50), which shows that the primary reaches a path or a default that D2 did not change.
- **AC-5 — The model uses `focus`.** **Check:** ES `fetch_url_completed` over the first 10 helpers after the T3 deploy: at least 50% of calls have `focus_present: true`. *Fails if* the share is below 50%.
- **AC-6 — Helpers still find facts.** **Check:** ES helper captures, the first 20 web-using helpers after deploy: at least 76% of reports hold at least one finding (the 86% baseline minus 10 points). *Fails if* the share is below 76%.

**Limitation, stated rather than hidden.** No automatic oracle exists for live answer quality. AC-1 is the quality guard, offline. AC-6 detects a large loss of facts but not a small one.

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
- ADR-0081, ADR-0061 — the primary's context compaction, unchanged
- FRE-1303 — the authorship-independence rule (Option 1 rejected)
- FRE-1331, FRE-1339 — Exa highlight mode and SearXNG engine health fields
- `src/personal_agent/orchestrator/sub_agent.py:2261` — `_absorb`, every result verbatim
- `src/personal_agent/tools/fetch.py:103`, `src/personal_agent/tools/web.py:325` — today's sizes and result shape
- `docker/searxng/settings.yml.example:270–306` — the Exa engine entry
- Evidence: ES `agent-captains-captures-subagents-*` (`rounds[]`, `full_output`), ES `agent-logs-*` (`web_search_started.query`), Postgres `api_costs` for traces `00ee8f34` and `57198efc`

---

## Status Updates

### 2026-10-21 - Proposed
**Changed By:** adr session
**Reason:** The owner decided the frame, the scope, the rejected alternative, the ranker rule, the bar and the rollout across two rounds, and said "go".
