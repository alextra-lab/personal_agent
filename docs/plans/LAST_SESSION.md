# Last session — 2026-08-29 → 30

## Doing / discussing  (≤5 sentences)
The Flash-Next swap was adjudicated and **reverted** on measured turn wall-clock, not benchmark
tok/s. Chasing why the model emitted an Alibaba URL led, by accident, to discovering `web_search`
had been returning nothing for two days — and then that it also returns *ten junk results* while
reporting success, which is worse. The owner ran one identical research question across three
primaries; the comparison produced a three-way model verdict **and** exposed that sequential runs
contaminate each other through the knowledge graph. Five tickets filed and approved out of that;
the owner's intent-classification test is now buildable as FRE-1337.

## What was decided and why

**The Alibaba URL was not memorised, and the probe that "proved" it was mine failing.** A held-out
suffix probe gave 2/6 hits on the real bucket prefix vs 0/6 on a fabricated one, and I called that
"the discriminator firing." At n=25 it reversed: **2/25 real vs 4/25 control** — the fabricated
bucket scored *higher*. The model knows the Alibaba OSS *convention* (`<x>-sg.oss-` → `ap-southeast-1.aliyuncs.com`),
not this host. A separate arm asked it to invent a temp-file URL on a clean prompt: it **refused 4/4**
and correctly named the false premise. Also: an n=15 run returned "0/14" that was **29 backend
errors** my parser scored as non-hits — I nearly reported an outage I had caused as a failed
replication. The local primary is `max_concurrency: 1`; unpaced probes take it down.

**The real cause was search starvation.** `web_search` returned `result_count: 0` on 13/13 calls in
that turn — with **no search results, the model constructed URLs from priors**, and the Alibaba one
was simply the exotic-looking member of three invented URLs (the others were plausible-but-absent
Wikipedia articles). Root cause external: DuckDuckGo/Startpage/Qwant/karmasearch all refuse this
VPS's datacenter IP. `general` was running on **brave alone**; PR #995 added `bing` (which worked all
along but was only configured under `categories: news`) and disabled the three that will never
answer. **Free scraped search is a depreciating asset from a datacenter IP** — the durable fix is
keyed APIs, which puts **FRE-1331 on the critical path**, not the medium-hygiene ticket I filed it as.

**Flash-Next reverted.** On an ordinary research turn context grew **12k → 83k over six calls**; at its
285 t/s prefill (vs 923) an 83k call needs ~291s *before* the first token. It hit the 900s
`orchestrator_task_timeout_seconds` and returned the degraded stub. `sub_agent` returned to the local
instruct companion — verified live, it spends **2 completion tokens** on "Say OK" where the thinking
primary spends 122, so non-thinking is expressed by the deployment and FRE-1007's guard needs no cloud
reasoning lever.

**Assumptions now known false.** (1) The "8 pre-existing test failures" figure repeated across three
PR handoffs — I contradicted it claiming 41, then found **I measured bare `pytest` while CI and
`make test` run `-m "not integration"`**. CI reports **0**; local `make test` 23; bare pytest 41. The
seats' 8 may be right in their condition. Retracted on PR #996 before merge. (2) I read six seat
permission-stalls as a machinery defect; the config deliberately allows `docker exec` **only** for
`*-test-1` containers and asks for everything else — the guard was correct, the seat was wrongly
reaching into production. (3) ADR-0034's privacy rationale for SearXNG is stale — it forwards to 35
engines, query text already leaves.

**Fable's round-4 on ADR-0139 found three blocking defects**, all verified independently: `NOT_CONTAINED`
is unreachable for FRE-1327's trace because `_verify_span` short-circuits to `UNCITED` before
containment; `observed_span_outcomes` keys on the `OBSERVED` entitlement but `mcp_esql` is `EXTERNAL`,
so the FRE-1306 detection it headlines is invisible to its own monitoring; and the negative check fails
open on partial composition (`echo "found: $(ls|wc -l)"`). Do not accept ADR-0139 without a round 5.

## Worktrees — anything special
`docker/searxng/` reverts to uid **977** ownership on every container restart — `sudo chown debian:977`
before editing or git operations fail confusingly. `settings.yml` is gitignored as of PR #990; it and
the tracked `.example` must change together.

## Sequence position + drift
Heavy drift, owner-directed and justified: none of yesterday's queue advanced. The session went
Dependabot → SearXNG outage → model adjudication → three-way comparison. The Observability Foundation
directive is untouched.

## Answers for the fresh start
- **Is the Alibaba URL a security incident?** No. Not memorised, not exfiltration; two GETs, both 403.
  It was search starvation. The bucket is blocked and FRE-1330 is Done.
- **Why is `web_search` suspect even though it "works"?** It reported `result_count: 10` while returning
  Hotmail login pages and Slovak bus timetables. **A bad ten is worse than a zero.** FRE-1339.
- **Can I compare models by running them in sequence?** **No.** Entity extraction writes one arm's
  sources into Neo4j and the next arm's `search_memory` cites them without fetching — verified.
  FRE-1338. Any eval must disable `search_memory` or snapshot the KG per arm.
- **Is the intent classifier mis-classifying?** It is worse: `conversational` is the *fallback* when
  the regex cascade matches nothing, there is no `RESEARCH` type, and `complexity=moderate` was already
  correct. 3 units of expansion budget granted, 0 used, 7/7. FRE-1337 measures before FRE-1288 decides.
- **Captain's Log?** Both tables have **zero rows ever** while billing $0.058–0.073/turn. FRE-1340.
- **Unfiled, owner's call:** FRE-1330's novelty signal both checks *and* records, so the first blocked
  attempt marks `aliyuncs.com` seen and every sibling bucket is then unblocked *and* unremarkable.
  Fix: record only when not blocked. Also unfiled: local inference invisible in `api_costs`;
  `span_extraction` is a 16–28s serial post-answer tax.
