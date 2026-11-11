# ADR-0160: `web_search` Calls Exa and Brave LLM Context Directly — SearXNG Leaves the Stack

**Status:** Accepted
**Date:** 2026-11-11
**Deciders:** Owner (all design decisions, 2026-11-11, in the adr session, and the build order relayed by master at 16:10 UTC), adr session (analysis and authoring)
**Tags:** web_search, tools, search, cost, structure, ADR-0028 Tier 1
**Supersedes:** ADR-0034 (SearXNG as the search backend). The ADR-0034 status changes in FRE-1767 (T2), when SearXNG leaves the stack.
**Amends:** ADR-0158 (its search fixtures and its "Option B" trigger)

---

## Context

### What SearXNG does for us today

`web_search` (`src/personal_agent/tools/web.py`) sends every query to a self-hosted SearXNG. ADR-0034 chose SearXNG in 2026-03 for three reasons: privacy, engine routing by category, and zero cost per query. In 2026-11, SearXNG does three jobs:

1. **It merges two paid engines in `general`.** FRE-1584 removed the scraped engines, because the VPS IP is refused. Since then, `general` is only `braveapi` and `exa`, both keyed and paid.
2. **It routes other categories to 20 free engines** (arXiv, PubMed, GitHub, Stack Overflow, Open-Meteo and others).
3. **It returns plugin answers** (time zone, currency, calculator) and infoboxes.

### What the logs show (Elasticsearch, `web_search_started`, 2026-09-01 to 2026-11-11)

- 2,103 calls. 1,999 (95%) use only `general`. About 104 (5%) use another category: `news`, `it`, `science`, `exa`, `weather`, `recipes`, `social_media`, `health`, `files`, alone or mixed with `general`. Each category had 1 to 17 calls. No call named an engine.
- The rate fell: 1,292 calls in September, 724 in October, 87 in the first 11 days of November.
- Plugin answers carry almost no load. 2 queries asked for a time ("time Berlin" and one other). No query asked for a unit conversion or a sum.
- 23 queries name the weather, and only 5 of them used the `weather` category. The other 18 went to the paid engines.
- In the 30 days to 2026-11-11, 310 of 310 `web_search_completed` events returned at least one result, and none was degraded.

### Why SearXNG now does harm

- **It removes the structure of the text.** Exa `/search` highlights arrive as markdown with line breaks: 10 of 10 highlights, 311 line breaks in one test call (FRE-1760, 2026-11-11). SearXNG 2026.11.11 removes all line breaks (`html_to_text`, `_normalize_text_fields`) and cuts content to 1,200 characters (FRE-1762). A flattened table makes the model misread rows: in FRE-1759, 9 of 18 "structure lost" claims fall on a flattened `web_search` table.
- **It cannot reach the better Brave endpoint.** The `braveapi` engine calls only `/res/v1/web/search`, which sends flat text: 0 of 90 fields with a line break. Brave `/res/v1/llm/context` works with our existing key and sends markdown: 38 of 42 snippets with line breaks, 16 with table rows, 237 to 2,295 characters per URL (FRE-1760, 2026-11-11). No SearXNG engine calls it, in the deployed tag or upstream.
- **It costs maintenance.** Tag updates (FRE-1762), a gitignored settings file that holds the keys, files owned by uid 977 that broke `git pull` on 2026-11-11, and past incidents where an eval-stack command recreated the production container.

### Privacy does not change

ADR-0034's first reason was privacy: upstream engines see only the server IP. Today every `general` query already reaches Exa and Brave in plain text, from the same VPS IP, through SearXNG. A direct call sends the same query from the same IP. So this ADR loses no privacy.

### ADR-0158

ADR-0158 kept SearXNG (its Option A) and named direct Brave and Exa clients as a fallback (its Option B), triggered by a size harness. The owner stopped ADR-0158 after T1 on 2026-10-21. So no size rule from ADR-0158 is in force. This ADR takes Option B for a different reason: structure, not size.

### Vendor facts

- **Brave LLM Context** is in the Brave Search plan at $5 per 1,000 requests, the same price as web search. The plan includes $5 of free credit each month (Brave launch post and API page, References). Parameters: `count` and `maximum_number_of_urls` (1 to 50, default 20), `maximum_number_of_tokens` (1,024 to 32,768, default 8,192), `freshness` (`pd`, `pw`, `pm`, `py`, or a date range), `search_lang`, `safesearch`. The response holds `grounding.generic[]` (`url`, `title`, `snippets[]`) and `sources{url: {title, hostname, age}}`.
- **Exa `/search`** in `auto` mode costs $7 per 1,000 requests, up to 10 results. Highlights cost nothing extra. `contents.highlights.maxCharacters` sets the highlight length per URL. `category` takes `company`, `publication`, `news`, `personal site`, `financial report`, `people`. Exa has no IT or science category. `includeDomains` limits the domains. The response holds `costDollars.total`. Errors: 402 (`NO_MORE_CREDITS` and budget tags), 429 (`RATE_LIMIT_EXCEEDED`), 503 (not billed).
- The owner confirmed room on the Brave account on 2026-11-11. No balance or limit is recorded here (lifecycle rules, Paid runs).

### The owner's decisions (2026-11-11)

In the adr session, as replies to numbered items: "A, brief/full, C, drop _for now_, c, soft $10", then "1 yes 2 yes but seems small 3 yes 4 tool 5 yes". Master relayed the build order at 16:10 UTC: "All the issues we found this morning, all the decisions we made build them now", with the revised sizes of D2.

---

## Decision

### D1 — Exa and Brave LLM Context in parallel, merged in our code

1. `web_search` stays a native tool (Tier 1 under ADR-0028) with the same name. It calls two providers for each search, in parallel:
   - Exa `POST https://api.exa.ai/search`, `type: auto`, with `contents.highlights`.
   - Brave `GET https://api.search.brave.com/res/v1/llm/context`.
2. **Merge.** The result list alternates the two providers by their own rank (Exa 1, Brave 1, Exa 2, Brave 2, and so on). A URL that both providers return appears once, at its first position. URL comparison ignores the scheme, a `www.` prefix, a trailing slash and the fragment.
3. **One provider fails.** The other provider's results return. The result carries `degraded_retrieval: true` and a `retrieval_warning` that names the failed provider and its reason.
4. **Both providers fail.** The executor sorts the failure of each provider:
   - A bad query: HTTP 400 or 422. The query itself is the cause.
   - A refusal: every other failure. That is HTTP 401, 402, 403, 404, 405, 429, any other 4xx, 5xx, a timeout, a connection error or a response that does not parse. A 404 or 405 means that the endpoint or our integration is wrong, so the model cannot repair it with another query.
   - **Both refused:** the tool returns the error `search_unavailable`, retryable. D5 adds the owner alert for this case only.
   - **At least one bad query:** the tool returns the error `search_failed`, not retryable, with a next action to change the query. The error text names the failure of each provider. No alert is sent. If the other provider refused, the next search with a good query shows that refusal.
5. **Text is not changed.** Provider text reaches the result unchanged, with its line breaks and markdown table rows. The tool cuts no snippet. The size comes only from the request parameters of D2. For Brave, the snippets of one URL join with a blank line into one `snippet`.
6. **Rendering.** The shared contract renders a tool result with `json.dumps` (`tools/contract.py:246`). So a line break reaches the model as the escape `\n` inside a JSON string. The row boundary stays visible to the model, which SearXNG replaced with a space. This ADR does not change the render format. This ADR takes in the `web_search` part of FRE-1760. The two `fetch_url` parts of FRE-1760 stay separate.
7. **Keys.** Two new secret settings, `exa_api_key` and `brave_api_key` (`AGENT_EXA_API_KEY`, `AGENT_BRAVE_API_KEY`, `SecretStr`), read through `settings`. No log, event or error text holds a key. Master moves the values into `.env` by name at deploy.
8. **Timeouts.** One setting for each provider. The tool timeout (15 s) stays above both.

### D2 — A `size` argument: `brief` (default) and `full`

| Size | Exa | Brave LLM Context | Expected total |
|------|-----|-------------------|----------------|
| `brief` (default) | 10 results, `highlights.maxCharacters` 800 | `maximum_number_of_tokens` 2,048, `maximum_number_of_urls` 10 | about 14,000 to 16,000 characters |
| `full` | 10 results, `highlights.maxCharacters` 2,000 | `maximum_number_of_tokens` 8,192, `maximum_number_of_urls` 10 | about 35,000 characters, about 10,000 tokens |

1. The four size values are settings. A change needs no code change.
2. `brief` keeps today's size range. ADR-0158 measured today's searches at 10,000 to 24,000 characters.
3. `full` stays under the `web_search` ceiling of 20,000 tokens (`max_result_tokens`, ADR-0156 D6.4). That ceiling does not change.
4. `max_results` stays as an optional cap on the merged list (1 to 50).
5. The `engines` argument goes. No provider is addressed by name.

### D3 — SearXNG leaves the stack, and categories map onto provider filters

| Category | Exa | Brave LLM Context |
|----------|-----|-------------------|
| `general` (default) | no filter | no filter |
| `news` | `category: news` | `freshness: pw`, unless `time_range` sets it |
| `it` | `includeDomains`: github.com, stackoverflow.com, developer.mozilla.org, huggingface.co, hub.docker.com | the same domains as `site:` operators |
| `science` | `includeDomains`: arxiv.org, pubmed.ncbi.nlm.nih.gov, semanticscholar.org, crossref.org, openalex.org | the same domains as `site:` operators |
| any other value | as `general` | as `general` |

1. The domain lists are constants in the tool module, tested against the description text.
2. `time_range` maps to Brave `freshness` (`day` → `pd`, `week` → `pw`, `month` → `pm`, `year` → `py`) and to Exa `startPublishedDate` (now minus the range, ISO 8601).
3. `language` maps to Brave `search_lang`. Exa has no language parameter, so Exa gets the query only.
4. After T1 is live, T2 removes the SearXNG service from every compose file, its settings, its monitor, its tests, its egress permission and every prompt text that names it.

### D4 — Plugin answers are dropped for now

The time zone, currency and calculator answers go, together with `answers`, `infoboxes` and `suggestions`. The model computes these itself. The usage data (2 of 2,103 queries) does not justify a provider for them. The words "for now" are the owner's: a later ADR can bring one back if a need appears.

### D5 — No rescue provider. One owner alert for each outage

1. When both providers refuse (D1.4), the owner gets one alert through the existing owner alert channel (`service/owner_ntfy.py`).
2. Further refusals in the same outage send no alert. The outage ends at the next search that succeeds. The next outage can alert again.
3. A bad query sends no alert.
4. No third source serves the search. This answers FRE-1670 with its option C.

### D6 — A soft $10 monthly budget for Exa and Brave together

1. A setting, `web_search_monthly_budget_usd`, default 10.
2. When the `api_costs` total for providers `exa` and `brave` in the calendar month (UTC) reaches the setting, the owner gets one alert for that month.
3. Search continues. The budget never blocks a search.
4. Projection at the current rate: each search costs about $0.012 ($0.007 Exa plus $0.005 Brave). At 240 to 724 searches each month, that is $3 to $9.

### D7 — A native `weather` tool on Open-Meteo

1. A new Tier 1 tool, `weather`, calls the Open-Meteo forecast API and its geocoding API. Both are free and need no key.
2. It takes a place name or coordinates, and a number of forecast days.
3. It returns current conditions, plus hourly and daily forecasts with one line for each hour or day. Each line holds its time and its values with units.
4. A forecast is structured data, not a search result, so it is a separate tool, not a `web_search` category (owner: "4 tool").

### D8 — Four PRs, in order

| Ticket | Phase | Blocked by |
|--------|-------|------------|
| FRE-1766 | T1: the two direct clients, the keys in `settings`, the new `web_search` (D1, D2, D3 mapping, D4), and D9.1 to D9.8, except the weather pointer of D9.7 | — |
| FRE-1767 | T2: remove SearXNG from compose, config, eval stacks, monitors, tests and prompts (D3.4, D9.5). ADR-0034 becomes Superseded | FRE-1766 |
| FRE-1768 | T3: the outage alert (D5) and the budget alert (D6) | FRE-1766 |
| FRE-1769 | T4: the `weather` tool (D7), and the weather pointer in the `web_search` description (D9.7) | FRE-1766, for the description edit only |

T1 owns the telemetry of D9.8, because AC-2 to AC-4 read it. The ticket text of FRE-1766 does not list D9.8 yet. The PR of this ADR names that gap for master.

Master filed the four tickets on 2026-11-11 and builds them. Each ticket carries its own acceptance criteria.

### D9 — What the change touches

1. **Result shape.** Each result holds `title`, `url`, `snippet`, `engine` (now the provider: `exa` or `brave`) and `published_date` (Exa `publishedDate`, Brave `sources[url].age`), when the provider gives one. `score` goes, because neither provider sends one. The health fields stay with the same names: `unresponsive_engines`, `engines_contributed`, `degraded_retrieval`, `retrieval_warning`. Their values now name providers. Same names keep the eval readers working (`scripts/eval/fre1517/tool_audit/calls.py` reads `unresponsive_engine_reasons`). `result_count` stays `len(results)`.
2. **Size limits.** D2. The ADR-0156 ceiling stays. ADR-0158 D2 (the Exa length grid through SearXNG) is not implemented and does not apply.
3. **Cost recording.** `paid_engines_used` and its guess from the SearXNG response go. Each provider call that returns HTTP 2xx writes one `api_costs` row with `purpose: web_search`, also when it returns no result: Exa with `costDollars.total` from its response, Brave with `brave_search_cost_per_call_usd`. A call that fails writes no row. Today a `general` search whose paid engine returned no result was not counted (`web.py:350`). That gap closes.
4. **Egress guard.** Both clients use `create_guarded_http_client`. The gateway runs the guard in `blocklist` mode (`url_guard_mode` default), and neither `api.exa.ai`, `api.search.brave.com`, `api.open-meteo.com` nor `geocoding-api.open-meteo.com` is on the blocklist. A deployment that runs `web_search` or `weather` with the guard in `allowlist` mode must list those hosts. `tests/test_security/test_egress_seams.py` moves from the SearXNG URL to the two provider URLs.
5. **Recorded fixtures and the eval stack.** The FRE-1641 harness recorded raw SearXNG JSON through its own SearXNG instances. Its search part has no backend after T2. Its `run.py` and `report.py` also pass the `engines` argument, which D2.5 removes. T2 keeps it importable or marks it retired with a note (FRE-1767 AC-4). A new search fixture records the raw provider responses and shapes them through the production shaping functions, as ADR-0158 D1.8 requires. An eval stack that runs `web_search` now calls the paid providers directly with the production keys, as `general` did through SearXNG before. The paid-run preflight rule applies unchanged.
6. **Grounding.** Every result entry with a `url` stays a citable source (`grounding/source_registry.py:1017`). `web_search` stays in `TYPED_RETRIEVAL_TOOLS`. The call-level source tuple `("answers", "infoboxes")` (`source_registry.py:434`) becomes empty, because D4 removes both fields. The grounding tests that read those two fields change with it (`tests/personal_agent/orchestrator/test_source_registry_wiring.py`). "web_search results are directly citable" (`orchestrator/prompts.py:148`) stays true: a snippet is still the provider's own text.
7. **Tool description.** The text that the model reads names the two providers and says that both see the plain-text query. It states that snippets keep line breaks and table rows, explains `size`, lists the four categories, and points page reading to `fetch_url`. The plugin examples and the engine list go. The pointer from weather questions to `weather` comes with T4, when that tool is registered. A description never names a tool that is not deployed.
8. **Telemetry.** `web_search_started` keeps its fields, drops `engines` and adds `size`. `web_search_completed` keeps `result_urls`, `unresponsive_engine_reasons` and `degraded_retrieval`, and adds:
   - `size`;
   - `result_chars` and `result_tokens`: the length and the contract token count (`tools/contract.py:252`) of the shaped result rendered as JSON, before the contract bound;
   - `provider_outcomes`: one entry for each provider, with `provider`, `http_status` (absent on a timeout or a connection error), `outcome` (`ok`, `refused` or `bad_query`), `result_count`, `latency_ms` and, for Exa, `cost_usd` from `costDollars.total`.
   
   No Grafana dashboard reads these events today.

---

## Alternatives Considered

### Option 1: Patch SearXNG to keep line breaks, and keep it

**Description:** A local patch to the SearXNG `exaapi` engine and to its core text steps, so that Exa highlights keep their line breaks.

**Pros:**
- No change to the tool, the fixtures or the eval stack.
- The free category engines stay.

**Cons:**
- Brave still comes through `/res/v1/web/search`, which sends flat text. No SearXNG patch brings back rows that this endpoint does not send.
- We would own a fork of upstream core code, and every tag update (FRE-1762) would need the patch again.
- The maintenance cost stays: the settings file, the uid 977 files, the eval-stack incidents.

**Why Rejected:** It fixes one of the two providers, and it raises the maintenance cost that the owner asked to remove.

### Option 2: Direct clients for `general`, and a small SearXNG for the other categories (ADR-0158 Option B as written)

**Description:** `general` calls Exa and Brave directly. SearXNG stays for `it`, `science`, `news`, `weather` and the rest.

**Pros:**
- The 20 free engines stay available.

**Cons:**
- All of the maintenance cost stays, for 5% of the calls (1 to 17 calls in 72 days for each category).
- Two backends for one tool, with two result shapes and two failure paths.
- Exa `includeDomains` and Brave `site:` reach the same sources for `it` and `science`.

**Why Rejected:** The owner chose to remove SearXNG (decision 3C). The usage data does not pay for a second backend.

### Option 3: One provider first, the other only on failure

**Description:** Each search calls Exa. Brave serves only when Exa fails (or the other order).

**Pros:**
- About half the cost per search.

**Cons:**
- Each search loses the second view. Brave and Exa return different sources for the same query.
- A failover adds latency on each failure.

**Why Rejected:** The owner chose parallel calls (decision 1A). The cost difference is a few dollars each month (D6.4).

### Option 4: A rescue source when both providers refuse (FRE-1670 options A and B)

**Description:** Exa's keyless public endpoint (A), or a third paid vendor (B), serves a search when both keyed providers refuse.

**Pros:**
- A user gets results during an outage.

**Cons:**
- A: the same vendor as one of the failed providers, rate limited, with no contract.
- B: a third key, a third bill and a third result shape.
- The one known outage (FRE-1641 run r1, 2026-10-21) came from an eval run, and the paid-run rule now prevents that cause.

**Why Rejected:** The owner chose no rescue and one alert for each outage (decision 5C).

---

## Consequences

### Positive Consequences

- Table rows and line breaks from both providers reach the model, so the cause of 9 of 18 FRE-1759 "structure lost" claims goes.
- One backend, two HTTP calls, and no container, settings file, image pin or monitor to maintain.
- The size of a search is set per call, as ADR-0158 Option B wanted.
- Each provider call records its real cost, including Exa's own `costDollars`.
- Weather gets a forecast tool instead of 18 paid searches that return pages about the weather.

### Negative Consequences

- The free engines go. A query that only arXiv or PubMed would answer now depends on the Exa and Brave indexes of those domains.
- Time zone, currency and calculator answers go (D4).
- `brief` is the default, so a search that needs depth must ask for `full` or call `fetch_url`. The model must learn when to do that from the description.
- The FRE-1641 search fixtures lose their backend.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Both providers refuse together (credit or quota) | Medium | D5 alert for each outage. The paid-run rule protects the shared credit. The D6 budget alert warns before the month's spend grows. |
| A key appears in a log or an error | High | `SecretStr` settings. A test asserts that no event or error text holds the key value. |
| The domain lists for `it` and `science` miss a good source | Low | The lists are constants in one place. A query can use `general`. |
| JSON escapes still hide structure from the model | Medium | D1.6 states it. The FRE-1759 measure, run again after T1, shows whether the structure claims fall. A render change is a separate decision. |
| An eval stack spends the production credit | Medium | The paid-run preflight rule, unchanged. |

---

## Implementation Notes

- **Files affected:** `src/personal_agent/tools/web.py`, a new `src/personal_agent/tools/weather.py`, `src/personal_agent/tools/__init__.py`, `src/personal_agent/config/settings.py` and `setting_classes.py`, `src/personal_agent/grounding/source_registry.py`, `src/personal_agent/orchestrator/prompts.py`, `src/personal_agent/tools/fetch.py` (risk text), `config/governance/tools.yaml` and `permissions.yaml`, `.env.example`, every compose file with a `searxng` service, `docker/searxng/`, `scripts/monitors/searxng_engine_parity.py`, the FRE-1641 harness, and their tests.
- **Migration:** T1 ships the direct path while SearXNG still runs. T2 removes SearXNG only after T1 is live. Master moves the two keys from the SearXNG settings file (or `pass`) into `.env` by name, before the T1 deploy.
- **Deploy class:** T1, T3 and T4 are `seshat-gateway` rebuilds. T2 is a gateway rebuild plus the removal of the `searxng` container.
- **Testing:** saved Exa and Brave responses (no live call in CI) through the executor and through `render_content`.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria. They are judged on FRE-1763 after T1 to T4 are deployed, not at the merge of this ADR, and not by one implementation ticket.

- **AC-1 — Table rows reach the model.** · **Check:** one live `web_search` run by the executor inside the gateway container (no model turn), query "average monthly temperature Paris table", `size=brief`. Parse the rendered tool message with `json.loads`. At least one snippet holds a line break, at least one snippet holds a markdown table row (a line that starts and ends with `|`), and both providers appear in `engines_contributed`. A committed test runs saved Exa and Brave responses through the executor and `render_content` and asserts the same. · *Fails if* any step still replaces line breaks with spaces, or one provider is not called.
- **AC-2 — Search works without SearXNG.** · **Check:** Elasticsearch, `web_search_completed`, the first 7 days after the T2 deploy (at least 20 events). At least 97% hold `result_count > 0`, and at most 5% hold `degraded_retrieval: true`. Baseline, 30 days to 2026-11-11: 310 of 310 with results, 0 degraded. `docker ps` on the VPS shows no `searxng` container. · *Fails if* the direct path returns empty or degraded results more often than the bar, or SearXNG still runs.
- **AC-3 — `brief` keeps today's size, and `full` fits the ceiling.** · **Check:** `web_search_completed` (D9.8), same 7 days. For events with `size: brief` (at least 20), the median `result_chars` is between 8,000 and 24,000. For `full`, one live `web_search` with `size=full` by the executor in the gateway container, same query as AC-1, plus every `size: full` event of the 7 days: each `result_tokens` is at most 20,000, and the rendered tool message of the live call holds no `"truncated": true`. · *Fails if* `brief` falls outside today's range, or a `full` result is over the ceiling and the contract cuts it.
- **AC-4 — Every successful provider call is costed, and no failed call is.** · **Check:** for the same 7 days, join `web_search_completed` to `api_costs` (`purpose = 'web_search'`) by trace id. For each `provider_outcomes` entry with `outcome: ok`, the trace holds exactly one row of that provider, also when its `result_count` is 0. For each entry with another outcome, the trace holds no row of that provider. Each Exa row's `cost_usd` equals the entry's `cost_usd` and is above 0. · *Fails if* a 2xx call has no row, a failed call has a row, or the Exa cost is a fixed value instead of the vendor's.
- **AC-5 — Outage and budget alerts fire once, and the outage alert resets.** · **Check:** committed tests with the alert sender mocked and both provider URLs on a stub:
  - HTTP 402 from both: two searches give two `search_unavailable` errors and one alert. Then one search succeeds. Then HTTP 402 again gives a second alert.
  - HTTP 400 from one provider and 402 from the other: `search_failed` and no alert.
  - The month's `api_costs` total above `web_search_monthly_budget_usd`: two searches give one alert for the month, and both searches return results.
  
  *Fails if* the alert fires for each search, never fires again after a success, fires on a bad query, or the budget blocks a search.
- **AC-6 — Weather gives a forecast in lines.** · **Check:** two live `weather` calls by the executor in the gateway container: "Paris" with 2 days, and the coordinates 48.86, 2.35 with 3 days. Each result holds current conditions, at least 24 hourly lines and exactly as many daily lines as the days asked. Each line holds its time and every value with its unit. An unknown place name returns an error result. · *Fails if* the tool returns pages or prose, ignores the days asked, rejects coordinates, or gives a value without a unit.

---

## References

- ADR-0034 — SearXNG self-hosted web search (superseded by this ADR when FRE-1767 lands)
- ADR-0028 — tool integration tiers (both clients and `weather` are Tier 1)
- ADR-0156 — tool result contract, D6.4 result ceiling
- ADR-0158 — tool output size at birth (stopped after T1, its Option B taken here for structure)
- FRE-1763 — this ADR's umbrella ticket
- FRE-1766, FRE-1767, FRE-1768, FRE-1769 — implementation tickets T1 to T4
- FRE-1760 — structure fix; its comments of 2026-11-11 hold the direct Exa and Brave checks
- FRE-1762 — SearXNG update and its AC-5 line-break probe
- FRE-1759 — `docs/research/2026-11-11-fre-1759-tool-result-structure-loss.md`
- FRE-1670 — owner decision on a search rescue (answered by D5)
- FRE-1641 — ADR-0158 T1 harness and run r1, the 2026-10-21 credit outage
- FRE-1584 — scraped engines removed from `general`
- Brave launch post: https://brave.com/blog/most-powerful-search-api-for-ai/
- Brave Search API: https://brave.com/search/api/
- Brave LLM Context reference: https://api-dashboard.search.brave.com/documentation/services/llm-context
- Exa search reference: https://docs.exa.ai/reference/search
- Exa pricing: https://exa.ai/pricing
- Exa error codes: https://exa.ai/docs/admin/error-codes.md

---

## Status Updates

### 2026-11-11 - Accepted
**Changed By:** adr session, on the owner's decisions
**Reason:** The owner decided every item in the adr session on 2026-11-11, and master relayed the build order at 16:10 UTC. Master filed FRE-1766 to FRE-1769 and builds them. AC-1 to AC-6 stay open until the four tickets deploy.
