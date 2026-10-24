# FRE-1666 — Hermes Agent web search: what its setup and its search and fetch tools do that ours do not

> **Ticket:** [FRE-1666](https://linear.app/frenchforest/issue/FRE-1666) · **Date:** 2026-10-24 · **Seat:** explore · **Paid cost:** $0 · **Commission:** master, on the owner's request of 2026-10-24 · **Upstream:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) at commit `a547240007877c7fd99c4f92069e402c672b7928` (2026-10-24) · **License:** MIT

## Question

The owner, verbatim: "Web search is a real problem for ai harnesses. I could see that the Hermes harness has addressed this and includes a web search configuration section during the install/setup. We couls probably learn from the harness."

The study asks three things:

1. How does Hermes configure web search at setup?
2. How are its search and fetch tools built: schemas, validation, result shape, page extraction, errors, retries and provider fallback?
3. Which of those ideas fix a failure that our production tools show today?

The commission's hypothesis is that the setup section is the answer. This study tests that hypothesis. It does not assume it.

## Summary for the owner

- **The setup wizard is not what makes Hermes work.** The wizard writes three or four config keys. The robustness is in the runtime: a fallback to other vendors when one refuses, an honest note on a rescued result, and page extraction by a vendor, not by a local parser (F1).
- **Our `general` search has one failure mode and no second source.** It runs on two paid engines, Brave and Exa. On 2026-10-21 both ran out of credit at the same time (F2). No user saw it, because no production search ran in that window. Hermes would serve that call from a free tier and say so in the result. This is a decision for you (FRE-1670).
- **`fetch_url` failed on 41 of 229 calls last week (17.9%)** (F4). The biggest class is HTTP 403 (16), then 404 (13).
  - 28 of the 41 are the kinds a vendor extractor can address: bot protection, JavaScript pages, timeouts and disconnects. Hermes always extracts through a vendor. Whether a vendor rescues our pages is not measured, because that needs a paid call (F8, FRE-1671, your decision).
  - 11 of the 13 404s are URLs that no earlier search in the session returned. The model built them itself (F5). Hermes has no answer for this.
- **`fetch_url` returns a PDF's raw bytes as text** (F7, FRE-1669). Four PDF fetches last week each returned 10,000 characters. Hermes detects binary bodies and converts PDFs. This is the cheapest and clearest fix in the study.
- **Three Hermes features are not worth porting now:** the setup picker, the 20-minute search cache, and the head-and-tail page cut with the full text stored. The measured benefit of each is small (F9, F10).

| Proposal | Ticket | Who decides |
|---|---|---|
| P1 — `fetch_url` converts PDFs and refuses other binaries | FRE-1669 | master (bugfix) |
| P2 — a search rescue when both keyed engines refuse | FRE-1670 | **owner** |
| P3 — a vendor extract fallback for bot-protected and JavaScript pages | FRE-1671 | **owner**, then a paid preflight |
| P4 — do not retry a URL that already failed in the session | FRE-1672 | master |
| P5 — do not port the setup picker, the search cache or the head-and-tail cut | none | master |

## Findings

Every upstream claim is a code reading of Hermes at commit `a547240007877c7fd99c4f92069e402c672b7928`, cited as `path:line`. We do not run Hermes, so an upstream claim is about what the code does, not a measurement. Every claim about our system is a measurement against production, unless the finding says otherwise. Elasticsearch counts are provisional (FRE-1051).

### F1 — Hermes's web search robustness is in the runtime, not in the setup wizard

**Verdict:** POSITIVE (upstream code reading).

**What was read, and what it shows:**

| Hermes part | Path | What it does |
|---|---|---|
| Setup picker rows | `plugins/web/_common.py:232-241`, each `plugins/web/<vendor>/provider.py` `get_setup_schema` | One row per vendor, with a cost badge ("free · self-hosted", "paid", "free · no key"), a key prompt and a sign-up URL. Exa, Parallel and Keenable have a Free row and a Paid row. |
| What the picker writes | `hermes_cli/tools_config_providers.py:713-726` | `web.backend`, and `web.provider_tier.<vendor>` for a tiered row. Separate `web.search_backend` and `web.extract_backend` keys can split the two capabilities. |
| Backend choice when nothing is set | `tools/web_tools.py:100-138` | Walks the vendor keys in a fixed order (Tavily, Perplexity, Exa, Parallel, Keenable, Firecrawl, SearXNG, Brave free, DuckDuckGo), then the free keyless tier last. |
| A stored choice | `tools/web_tools.py:100-105` | Used as-is, with no availability probe, "so a broken selection surfaces the vendor's honest error rather than silently rerouting". |
| One-shot rescue | `tools/web_tools_rescue.py:93-118`, `plugins/web/keyless_mcp.py:398-413` | A failed call is served once from the free keyless tier. The result names the failure, and the next call uses the configured vendor again. |
| Failure classes | `plugins/web/keyless_mcp.py:36-69` | A rate limit, or HTTP 401, 402, 403 or 5xx, moves to the next vendor. Any other error stops, because "a malformed query fails everywhere". |
| Readiness check | `hermes_cli/doctor_tools.py:107-125` | `hermes doctor` reports one row for search and one for extract. It checks that a key or a keyless tier exists. It does not check credit. |

The picker is a menu over the same config keys that a user can also set by hand. The parts that keep search working after setup are the rescue, the failure classes and the strict selection. So the lesson for us is in the runtime (F2, F8). We are one deployment with one owner, and our engine config is already explicit (`docker/searxng/settings.yml.example`, `keep_only`). A setup wizard adds nothing that we lack.

### F2 — Our `general` search runs on two paid engines that fail together, with no second source

**Verdict:** POSITIVE.

**Query 1 — the live engine list.** Read from the running container, keys redacted:

```
docker exec cloud-sim-searxng sh -c 'grep -n -B2 -A3 "keep_only" /etc/searxng/settings.yml'
```

**Output (excerpt):**

```
50:    keep_only:
51-      # general
52-      - braveapi
53-      - exa
...
104-  # engines that used to sit beside them were refused by this datacenter IP (CAPTCHA,
105-  # "access denied", "too many requests"); each refusal marked the whole search
106:  # degraded_retrieval. They are gone from this file, not switched off — see keep_only.
```

The `exa` engine block has `categories: [general, exa]` (line 299). Both engines are keyed.

**Query 2 — the 2026-10-21 incident.** Source: the FRE-1641 build2 comment of 2026-10-21 06:51 UTC, verbatim: "At replay 384 of 798, on all 5 instances at the same time, Exa and Brave started to refuse access. … A direct check after the run returned 402 from both keys. Exa says 'You have exceeded your credits limit. Please top up…' (tag NO_MORE_CREDITS)." The final report of 07:28 UTC adds: "No production `web_search` ran during the outage window, so production SearXNG never suspended its engines."

**What it means.** Both engines share one failure mode: our credit. When it runs out, every `general` search has no engine. The next outage will reach a user. Hermes's answer is in F1: classify the refusal, serve the call from another source, label the result, and do not cache it. Our result already carries an honest `degraded_retrieval` flag (FRE-1339, `src/personal_agent/tools/web.py:265-335`). What we lack is the second source. The choice of that source is the owner's (P2). It also bears on ADR-0158 Option B (direct Brave and Exa clients), which is already an open owner decision after FRE-1641 (`docs/architecture_decisions/ADR-0158-tool-output-size-at-birth.md:165`).

### F3 — The paid engines served production search again on 2026-10-23 and 2026-10-24

**Verdict:** POSITIVE.

**Query** (Postgres, the `api_costs` ledger, which FRE-1656 began to fill for searches):

```sql
select date(timestamp) d, provider, count(*), round(sum(cost_usd)::numeric,3)
from api_costs where provider in ('brave','exa') and timestamp >= '2026-10-15'
group by 1,2 order by 1,2;
```

**Output:**

```
2026-10-23|brave|7|0.035
2026-10-23|exa|7|0.049
2026-10-24|brave|40|0.200
2026-10-24|exa|25|0.175
```

The credit is live today. Rows exist only from 2026-10-23, because FRE-1656 deployed then. Production search in this window costs about $0.38 a day on the busiest day.

### F4 — `fetch_url` failed on 41 of 229 production calls in the week of 2026-10-19

**Verdict:** POSITIVE.

**Query** (`agent-logs-*`, `tool_name` in `web_search` and `fetch_url`, by `event_type`, `tool_name` and `error_code`):

```json
{"size":0,"query":{"bool":{"filter":[{"range":{"@timestamp":{"gte":"2026-10-19","lt":"2026-10-26"}}},{"terms":{"tool_name":["web_search","fetch_url"]}}]}},
 "aggs":{"ev":{"terms":{"field":"event_type","size":20},"aggs":{"tool":{"terms":{"field":"tool_name","size":5},"aggs":{"code":{"terms":{"field":"error_code","size":10}}}}}}}}
```

**Output (excerpt):**

```
tool_call_started: 402      fetch_url: 229   web_search: 173
tool_call_failed: 47
  fetch_url: 41   http_error: 29  no_readable_text: 5  upstream_timeout: 5  fetch_failed: 2
  web_search: 6   missing_query: 6
```

The same query for 2026-10-05 to 2026-10-12 returns `tool_call_failed` 49: `web_search` 25 and `fetch_url` 24 (no `error_code` field on those events). Master's figures in the commission match.

**The 29 `http_error` failures by status** (same index and window, `error_code: http_error`, the cause parsed from the error text):

| Status | Count | Named cause |
|---|---:|---|
| 403 | 6 | Cloudflare challenge |
| 403 | 3 | DataDome |
| 403 | 7 | none named |
| 404 | 13 | — |

**Scope.** All 402 calls carry one `user_id`. The two eval gateways (`-treatment`, `-control`) send their logs to a different Elasticsearch: the SHA-256 of `AGENT_ELASTICSEARCH_URL` is `c35f0d987ab5` in both, against `255885964ef4` in the production gateway. So these counts cover the production gateway only.

**Deployed code.** The production gateway's `fetch.py` and `web.py` match `main` at `0ff3dd12` by file hash (`a4c2faea2b35`, `8c566ca9ccec`).

### F5 — 11 of the 13 HTTP 404s are URLs that no earlier search in the session returned

**Verdict:** POSITIVE.

**Query.** For each failed `fetch_url` call: was its URL in the `result_urls` of an earlier `web_search_completed` event in the same turn, or in the same session? Source events: `fetch_url_started` (`url`), `web_search_completed` (`result_urls`) and `tool_call_failed`, all in `agent-logs-*`, 2026-10-19 to 2026-10-26. Script: `provenance.py` in the scratchpad (method appendix).

**Output:**

```
all 229 fetch_url_started: same_turn_search 106, same_session_search 12, no_prior_search 111
('http_error','404','no_prior_search') 11      ('http_error','404','same_turn_search') 2
('http_error','403','no_prior_search') 6       ('http_error','403','same_turn_search') 6
('http_error','403','same_session_search') 4   ('no_readable_text','','same_turn_search') 5
('upstream_timeout','','same_session_search') 3 ('upstream_timeout','','same_turn_search') 2
('fetch_failed','','same_turn_search') 2
```

Examples of the 11: two flight-route pages on a travel site, two path forms of one airline-review page, and two `raw.githubusercontent.com` paths. (URLs from the owner's turns are described, not quoted, because this repo is public.)

**Limit.** "No prior search" also covers a URL that the user typed or a link from a fetched page. The events do not show which. So 11 is an upper bound on model-built URLs. Hermes has no defense against a guessed URL either. Our 404 error already tells the model to search instead (`_OTHER_SOURCE`, `src/personal_agent/tools/fetch.py:51`). This finding has no proposal.

### F6 — 5 failures were a repeat of a URL that had already failed in the same turn

**Verdict:** POSITIVE.

**Query.** Same events and window as F5. A failure counts as a repeat when the same trace already had a failure for the same URL.

**Output:**

```
failed calls on a (trace,url) that already failed earlier in the trace: 5
   x 2 <a travel-review blog post>
   x 3 <a Reddit thread>
   x 3 <the same travel-review blog post, in a later turn>
```

The travel-review blog post timed out 5 times on 2026-10-24 between 11:39 and 11:54, at 20 s each: 100 s of the owner's wait. Hermes caches only successful extracts (`tools/web_result_cache.py:1-8`). It does not remember a failure either, so this gap is ours alone (P4, FRE-1672).

### F7 — `fetch_url` returns a PDF's raw bytes as text

**Verdict:** POSITIVE for the code path. UNVERIFIABLE for the content of the four production results.

**Query 1 — offline probe.** `download_text` with a mocked HTTP transport that returns a 7,500-byte body with `content-type: application/pdf`. Run with `unshare -rn` (no network) on the deployed code.

**Output:**

```
status 200 chars 7500
'%PDF-1.7\n%����\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Length 44 /Filter /FlateDecode >>\nstream\nx�\x0b��,V\x00�D��Ԋ\x12\x00\x00endstream...'
```

The cause is `src/personal_agent/tools/fetch.py` in `download_text`: the HTML extractor runs only when the content type contains `html` or is empty. For any other type the tool returns `resp.text`.

**Query 2 — production.** `fetch_url_started` events with a URL that ends in `.pdf` (or a known PDF path), joined to `fetch_url_completed` in the same trace, 2026-10-19 to 2026-10-26.

**Output:**

```
fetch_url_started with .pdf URL: 4
  <a technical-report file on huggingface.co> -> [(10000, True)]
  <a book PDF on a research site> -> [(10000, True)]
  <a book PDF on a CDN> -> [(10000, True)]
  <a course-reading PDF on a teaching site> -> [(10000, True)]
```

All four completed at the 10,000-character default cut. The returned text is not stored anywhere that I found, so I cannot show that it was bytes. The code path and the size agree with that reading.

**Hermes.** `tools/web_tools_truncate.py:136-170` compares the body's first bytes with a table of file signatures and returns a typed error that names the type ("URL returned binary content (ZIP), not a page…"). Its extract vendors return PDF text, and its tool description says so (`tools/web_tools.py:539`). Our repo already holds a license-clean PDF text extractor (ADR-0102, FRE-681). P1 (FRE-1669) uses it.

### F8 — 28 of the 41 fetch failures are the classes that a vendor extractor addresses

**Verdict:** POSITIVE for the class counts. UNVERIFIABLE for the rescue rate.

**Counts** (from F4): 403 = 16, `no_readable_text` = 5, `upstream_timeout` = 5, `fetch_failed` = 2. Total 28. The 13 404s are excluded, because a missing page is missing for every fetcher.

**Second window.** FRE-1641's fixture run could not fetch 153 of 781 distinct URLs that production had cited, "mostly HTTP 403" (FRE-1641 build2 comment, 2026-10-21 06:51 UTC).

**Hermes.** Hermes never parses a page itself. `web_extract` always calls a vendor: Firecrawl, Tavily, Exa, Parallel or Keenable (`plugins/web/*/provider.py`, `tools/web_tools_extract.py:1-7`). A batch that fails gets one keyless rescue (`tools/web_tools_rescue.py:127-159`). The description tells the model to use a browser for pages that need JavaScript or a login (`tools/web_tools.py:539`).

**Why the rescue rate is UNVERIFIABLE.** Measuring it needs Exa contents calls, which are paid, and the commission forbids paid calls. FRE-1671 carries the preflight: a sample of the 153 stored failures, a cost projection, a credit check and the owner's go. Two constraints are the owner's: the URL leaves the host, and FRE-1554 recorded the owner's choice of an honest user agent. A vendor's crawler is a different path from our own fetch.

### F9 — The head-and-tail page cut would help little: support beyond character 10,000 is rare

**Verdict:** POSITIVE.

**Hermes.** `tools/web_tools_truncate.py:86-128` keeps the first 75% and the last 25% of the character budget (15,000 by default), stores the full page on disk, and adds a footer with the exact `read_file` call that pages through the omitted middle.

**Query 1 — how often we cut** (`fetch_url_completed`, 2026-10-19 to 2026-10-26):

```
fetch_url_completed 188 truncated=true 90
truncated fetches followed by a later fetch of the same URL in the session: 3
   (<a model card on huggingface.co>, 10000, [10000])
   (<another model card on huggingface.co>, 10000, [10000])
   (<a personal bookshelf page>, 10000, [10000])
```

**Query 2 — what the cut loses.** FRE-1641 check 2 (master, 2026-10-21, offline on stored fixtures): "Only 19 supported findings have their support beyond character 10,000", of 756. Today's cut keeps 97.5%.

**What it means.** We cut half the pages, but the cut loses 2.5% of the facts that answers used. In three cases the model tried again and got the same 10,000 characters, so a way to reach the rest was missing. That is the only cost seen. The owner closed the fetch-size question on 2026-10-21 ("Do not reopen the fetch-size question without a new reason"). This study finds no new reason (P5).

### F10 — A Hermes-style search cache would serve 1 search in 167

**Verdict:** POSITIVE.

**Hermes.** `tools/web_result_cache.py:1-8`, `:56-63` and `tools/web_tools.py:350-389`: an in-memory cache for 20 minutes, keyed on the case-folded query and a rounded result count. It merges concurrent identical calls into one.

**Query.** `web_search_started` events. A repeat is the same normalized `query`, `categories`, `engines` and `time_range` within 20 minutes of the previous one. The `query` field is cut at 120 characters in the log, so two long queries can collide. That error can only raise the count.

**Output:**

```
2026-10-05..2026-10-12: web_search_started=456 repeat within 20 min=15 (same turn 15, other session 0)
2026-10-19..2026-10-26: web_search_started=167 repeat within 20 min=1  (same turn 1,  other session 0)
```

At the F3 prices, one repeat in 167 saves about $0.01 a week. The cache does not earn its code (P5).

### F11 — All six empty-query searches came from the `researcher` worker, and Hermes has no defense either

**Verdict:** POSITIVE.

**Query.** `tool_call_started` for `web_search` with an empty `query`, 2026-10-19 to 2026-10-26, joined to the nearest earlier `permission_evaluated` event in the trace for its `worker` field.

**Output (all six):**

```
2026-10-20T14:28:14 {'query': '', 'max_results': 8, 'categories': 'general', 'time_range': '', 'engines': 'braveapi', 'language': 'en'} worker= researcher
2026-10-24T05:21:51 {'query': '', 'max_results': 10, 'categories': '', 'language': '', 'time_range': '', 'engines': ''} worker= researcher
2026-10-24T05:28:09 {'query': '', 'max_results': 8, 'engines': 'hackernews,stackoverflow', 'time_range': '', 'categories': '', 'language': ''} worker= researcher
2026-10-24T05:28:12 {'query': '', 'language': '', 'max_results': 8, 'time_range': 'year'} worker= researcher
2026-10-24T11:48:09 {'query': '', 'max_results': 15, 'time_range': '', 'categories': '', 'engines': '', 'language': ''} worker= researcher
2026-10-24T11:52:41 {'query': '', 'max_results': 20, 'time_range': 'month', 'categories': 'social_media', 'engines': 'lemmy posts', 'language': 'en'} worker= researcher
```

The worker fills the optional fields and leaves `query` empty. Our tool rejects it with `missing_query` and a retry hint (`src/personal_agent/tools/web.py:437-444`). Hermes turns empty arguments into `{}` (`agent/turn_tool_validation.py:157-169`) and sends an empty query to the vendor (`tools/web_tools.py:561`). So Hermes offers no lesson here. The cause is in the worker model or its prompt, which this study does not cover. No proposal.

## Method appendix

**Upstream.** `git clone --depth 1 https://github.com/NousResearch/hermes-agent.git` into the session scratchpad, outside the repo. Commit `a547240007877c7fd99c4f92069e402c672b7928`, dated 2026-10-24T05:08:52-07:00. No install script and no Hermes code ran. Files read: `tools/web_tools.py`, `tools/web_tools_rescue.py`, `tools/web_tools_truncate.py`, `tools/web_tools_extract.py`, `tools/web_result_cache.py`, `plugins/web/_common.py`, `plugins/web/keyless_mcp.py`, `plugins/web/searxng/provider.py`, the `get_setup_schema` of each `plugins/web/<vendor>/provider.py`, `hermes_cli/tools_config_providers.py`, `hermes_cli/doctor_tools.py`, `agent/turn_tool_validation.py`, `tools/arg_coercion.py`.

**License.** MIT (`LICENSE`, "Copyright (c) 2025 Nous Research"; `pyproject.toml:17`). Code or text from Hermes may be copied into this repo if the MIT notice comes with it. That applies to the failure classifier (`plugins/web/keyless_mcp.py:36-69`), the binary signature check (`tools/web_tools_truncate.py:136-150`), the truncation footer text (`:107-126`) and the tool descriptions (`tools/web_tools.py:515-557`). None of the proposals needs a copy. Each one can be written from the description in this document.

**Our side.** Code read at `main` `0ff3dd12`: `src/personal_agent/tools/web.py`, `src/personal_agent/tools/fetch.py`, `src/personal_agent/tools/tool_result_expand.py`. Deployed identity: the production gateway's `/app/src/personal_agent/tools/fetch.py` and `web.py` match by SHA-256. The SearXNG engine config was read from the running `cloud-sim-searxng` container, with key values redacted on output.

**Stores and windows.**
- Elasticsearch `agent-logs-*` on the production host, read with `_search` only. Windows: 2026-10-05 to 2026-10-12, 2026-10-12 to 2026-10-19, and 2026-10-19 to 2026-10-26 (the last one runs to "now", 2026-10-24 about 12:30 UTC). Event types: `tool_call_started`, `tool_call_failed`, `fetch_url_started`, `fetch_url_completed`, `web_search_started`, `web_search_completed`, `permission_evaluated`. All counts are provisional (FRE-1051).
- Postgres `api_costs` in `cloud-sim-postgres`, one read-only `select`.
- Linear: the FRE-1641 comments, for the 2026-10-21 incident and the fetch fixture data.

**Scripts** (session scratchpad, not committed): `es.py` (a `_search` and `_count` helper), `provenance.py` (F5 and F6), `pdf_probe.py` (F7).

**Identifier resolutions.**
- `error_code` first appears on a `tool_call_failed` event at 2026-10-11T07:49Z. The last event without it is at 2026-10-10T15:44Z. So F4 gives the 2026-10-05 week as totals only.
- `web_search_started` has `query` cut at 120 characters (`src/personal_agent/tools/web.py:455`).
- `web_search_completed` carries `result_urls` on all 167 events in the 2026-10-19 week. F5 relies on that field.
- The week of 2026-10-12 has only 7 web tool calls. That fits the owner's testing stop of 2026-10-10 (FRE-1573). I did not test whether events were lost that week.

**Rejected.**
- A per-day "degraded search" series for 2026-10-20 to 2026-10-24. It reads 0 degraded on every day. That zero agrees with FRE-1641's statement that no production search ran during the outage. It adds nothing to F2, so I report no verdict on it.
- Any live fetch of the failed pages. That would be a live probe, which the commission forbids.

**Not measured.** The model behind the `researcher` worker (F11). The vendor rescue rate (F8). The content of the four PDF results (F7).

## Proposals

At most ten. Five here.

1. **P1 — `fetch_url` converts a PDF to text and refuses any other binary body (FRE-1669).** Decide on the body as well as the header. Reuse the ADR-0102 PDF utility. Evidence: F7. Scope: bugfix, offline tests only.
2. **P2 — the owner chooses a second source for `general` when both keyed engines refuse (FRE-1670).** Options: Exa's anonymous keyless endpoint, a third keyed vendor, or none with an owner alert. Then build Hermes's failure classes, a labelled and uncached rescue, and one alert per outage. Evidence: F1, F2, F3. It bears on ADR-0158 Option B.
3. **P3 — the owner decides on a vendor extract fallback for `fetch_url` (FRE-1671).** Only for 403, `no_readable_text`, timeout and disconnect. A paid preflight on the stored FRE-1641 failures comes first, with a baseline arm, a repeat arm and a credit check. Evidence: F4, F8.
4. **P4 — `fetch_url` remembers a failed URL for the session (FRE-1672).** A non-retryable failure returns the stored error at once. A retryable one gets one more real attempt. Evidence: F6.
5. **P5 — do not port three Hermes features now.** No ticket.
   - The setup picker: our single deployment already has explicit config (F1).
   - The search cache: 1 repeat in 167 searches (F10).
   - The head-and-tail cut with the full page stored: the cut loses 2.5% of supported facts, and the owner closed the fetch-size question (F9).
   Reopen the third one only if a later measurement shows facts lost past the cut.

## Filed tickets

- [FRE-1666](https://linear.app/frenchforest/issue/FRE-1666) — this study's anchor (Backlog). Master commissioned the study without a ticket, so explore filed this one to give the study an id.
- [FRE-1667](https://linear.app/frenchforest/issue/FRE-1667) — the anchor of the second commissioned study, OpenCode (Backlog). It is not a proposal of this study.
- [FRE-1669](https://linear.app/frenchforest/issue/FRE-1669) — P1 (Backlog).
- [FRE-1670](https://linear.app/frenchforest/issue/FRE-1670) — P2 (Backlog).
- [FRE-1671](https://linear.app/frenchforest/issue/FRE-1671) — P3 (Backlog).
- [FRE-1672](https://linear.app/frenchforest/issue/FRE-1672) — P4 (Backlog).
