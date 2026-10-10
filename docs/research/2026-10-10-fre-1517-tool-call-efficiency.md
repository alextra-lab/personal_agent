# Are 144 tool calls necessary? A call-by-call audit of the FRE-1517 sessions, and our skills and tools against the authoring guidance

**Date:** 2026-10-10 · **Seat:** cc-explore · **Ticket:** FRE-1517 · **Commission:** the owner's question,
relayed by master: "Are 144 calls necessary? It seems we are not giving the models the tools or skills they
need." The owner then asked: "Do our skills and tools follow this guidance?" (Agent Skills authoring
guidance, Anthropic tool-design guidance, MCP).

## Answer

**No, most are not necessary, and the main cause is our tools and skills, not the models.**
- On Flash-Next's tool-building session, 89 of 151 attempts were useful (59%). On its log-and-learning
  session, 68 of 144 (47%).
- Of the calls that were not useful, the model's own choice explains few. The rest have a cause a fix can
  remove: 55 calls (36% of all calls) in s2 and 65 (45%) in s3.
- **The largest causes:**
  - the telemetry path: 70 calls over all sessions;
  - workers that cannot open the file their task names: 21;
  - a degraded search backend: 24;
  - overlapping worker searches and re-reads: 24;
  - workers that fetch the web through `run_python`: 18;
  - no edit tool: 13.
- **Our skills and tools follow the guidance only in part:**
  - Of 23 skills, 5 state how to check that a result is correct, and 9 say what to do on a failure.
  - Skill bodies are inlined whole by substring keyword match.
  - Every tool error that the model sees is cut to 150 characters.
  - There are good examples: `read`, `query_telemetry`, `fetch_url` and the mermaid skill.
- **Fewer calls is not the goal.** Gemma made 38 calls on s2 and scored lower (stage-3 F5).

Two security findings came out of this audit. Both went to master ahead of this report:
- the bash allowlist bypass, now **FRE-1572**;
- `run_python` with network on joins the network of the production stores (G7). The explore seat first
  reported this as a worker hole. That was wrong: FRE-1564 already forces `network: false` for workers. What
  remains concerns the primary.

## Scope and data

| Session | Model, stage | Turns | Attempts |
|---|---|---|---|
| `860d7435` | Flash-Next, stage 3, s2_tool | 20 | 151 |
| `514a039b` | Flash-Next, stage 3, s3_logs_learning | 20 | 144 |
| `c70518ad` | Gemma, stage 3, s2_tool | 20 | 38 |
| `114cd7dd` | Gemma, stage 3, s3_logs_learning | 20 | 48 |
| `755dc023` | Gemma, stage 4, s2_tool | 1–13 | 49 |
| `f7458125` | Gemma, stage 4, s3_logs_learning | 1–4 | 17 |

- **Stored data only, no model call and no live turn:**
  - ES `agent-logs-*`, every event of the 97 study traces (17,791 events);
  - the 97 archived captures (`captains_log/captures_archive_fre1517/`), which hold the primary's tool
    results as the model saw them;
  - the runner rows, which hold the sub-agent captures, with rounds and sizes per call;
  - Tempo, for one span check.
- **An attempt** is a `tool_call_started` event. Added to these:
  - 7 bash commands refused by the allowlist, which emit no `tool_call_started`;
  - 2 attempts refused before the start event (a relative-path `write` and a `search_memory` call
    without its required parameter), found in the captures.
- The stage-3 Flash-Next s2 count is 145 `tool_call_started` events, master's "144 calls" ± 1, plus the 6
  refused commands.

---

## Findings

### G1 — Master's measured facts, checked

**Verdict: POSITIVE** (each figure is a count).

**Query:** ES `agent-logs-*`, all events of the 20 traces of session `860d7435`, by `event_type` and `span_id`.

**Output:**

| Master's figure | Measured | Note |
|---|---|---|
| 145 `tool_call_started` | **145** | Correct |
| 6 bash `approval_denied` with `bash_allowlist_miss` | **6** | Correct. Those 6 have no `tool_call_started` |
| `run_python` 27 started, 16 finished | **27 started, 27 finished** | 7 exit codes other than 0, 1 timeout (`sandbox_timed_out`, 60 s) |
| 44 `web_search_completed`, 0 with zero results | **48 completed**, 0 with zero results | All 48 have `degraded_retrieval: True` |
| "The logs do not hold query text" | **They do** | `web_search_started.query` holds it, joined by `span_id` |

**A trap for anyone counting:** `tool_call_completed success: True` means only that the tool ran. A `bash`
result with `exit_code: 1` still logs `success: True`. Fact from the capture of trace `2fed491a`.

### G2 — Classes per session

**Verdict: POSITIVE.**

**Query:**
- Each attempt was joined by `span_id` to its outcome events: exit codes, result counts, errors, refusals and
  loop-gate decisions.
- For the primary, the attempt was also joined to its tool result from the capture.
- Three Claude subagents classified every attempt under `scripts/eval/fre1517/tool_audit/DEFINITIONS.md`,
  which was written before the first label. The classes, in order of precedence:
  - **failed:** an error, or no usable result;
  - **repeated:** the same intent again, with no new input;
  - **workaround:** a detour around a missing or refused tool, skill or permission;
  - **useful.**
- The explore seat cross-checked 39 rows by hand (G2a).

**Output:**

| Session | Attempts | Useful | Failed | Workaround | Repeated | Model's own choice | Study design | **Fixable** |
|---|---|---|---|---|---|---|---|---|
| Flash-Next s2 | 151 | 89 (59%) | 25 | 21 | 16 | 4 | 3 | **55 (36%)** |
| Flash-Next s3 | 144 | 68 (47%) | 37 | 30 | 9 | 6 | 5 | **65 (45%)** |
| Gemma s2 | 38 | 25 (66%) | 12 | 1 | 0 | 1 | 2 | **10 (26%)** |
| Gemma s3 | 48 | 7 (15%) | 25 | 14 | 2 | 2 | 1 | **38 (79%)** |
| Gemma s2, stage 4 | 49 | 35 (71%) | 6 | 1 | 7 | 0 | 0 | **14 (29%)** |
| Gemma s3, stage 4 (t1–4) | 17 | 2 (12%) | 4 | 10 | 1 | 0 | 0 | **15 (88%)** |

- **Model's own choice:** calls where a better call was available and documented, plus a guessed URL.
- **Study design:** memory recalls of content that the consolidation pause kept out of memory (stage-3 F6a).
  These calls are not counted as fixable.
- **Fixable:** every other non-useful call. Each has a cause in a tool, skill, instruction, policy or the
  environment.

#### G2a — The hand cross-check

- 32 rows were drawn at random (seed 20261010, 8 per Flash-Next session and 4 per Gemma session). Added to
  them were the 7 rows touched by two join defects, described below.
- **The class agreed on 39 of 39 rows. The cause agreed on 38.** The explore seat changed `gm_s2#35`
  (`ls nfl_helper.py` on a relative path) from `model` to `instruction`, because nothing tells the model that
  bash does not start in the workspace.

**Join defects found and handled:**
1. Parallel web searches in the same second got another call's result URLs. This affected 2 rows. The query
   text was correct.
2. On Flash-Next s3 turn 3, one refused bash row shifted the capture results by one. The reviewer read the
   capture and labelled from it.

The reviewers also corrected two labels by reading the captures (`gm_s3#46`, `#47`).

### G3 — Where the fixable calls go, ranked

**Verdict: POSITIVE.**

**Query:** every non-useful call, assigned to one fix by rules over its tool, role, arguments and label reason
(`buckets.py`). The 38 rows that no rule matched were assigned by hand, by reading each reason. All rows are
assigned.

**Output** (calls; FN = Flash-Next stage 3, GM3 = Gemma stage 3, GM4 = Gemma stage 4):

| # | Cause, and the fix that removes it | FN | GM3 | GM4 | Total | In hand |
|---|---|---|---|---|---|---|
| 1 | **Telemetry path.** (a) The primary queries Elasticsearch with `bash` + `curl`, guided by a stale 23k-char skill: 42. (b) The planner sends the internal task to a web researcher: 22. (c) `query_telemetry` refusals and dead ends: 6 | 17 | 38 | 15 | **70** | (b) FRE-1564, deployed: GM4 shows 0. (c) FRE-1567, merged as `199f137c`. Skills: FRE-1568. **(a) is not in hand:** `query_telemetry` is `worker_only=True` (`telemetry_query.py:311`), so the primary still curls (GM4: 9) |
| 2 | **Workers cannot open the file the task names.** A `general` worker was asked to change `/app/agent_workspace/nfl_picks.py`, which its sandbox does not mount. It searched the filesystem and `/proc`, then rebuilt the file from memory | 19 | 2 | 0 | **21** | Related: FRE-1565 (each worker its own workspace). File reads are not in its scope |
| 3 | **Degraded search backend.** All stage-3 web searches ran with `degraded_retrieval` ("google cse: too many requests" or "Suspended"). Flash-Next s3 also had 14 SearXNG timeouts at 12 s, plus 3 page fetches blocked by 429, JS gating or empty extraction | 21 | 2 | 1 | **24** | Not in hand. Stage 4 shows 0 degraded, so it may be intermittent |
| 4 | **Overlapping searches and re-reads.** Sequential and parallel researchers search the same topic in one turn (for example, one paper searched 5 times: `fn_s3#124–132`). The primary re-reads unchanged file regions | 22 | 0 | 2 | **24** | Not in hand |
| 5 | **Workers fetch the web through `run_python` + `requests`.** In stage 3 the researcher had only `web_search` | 18 | 0 | 0 | **18** | FRE-1564: researchers got `fetch_url`, and worker `run_python` is forced to `network: false` (`tools.yaml:25-29`) |
| 6 | **No edit tool.** Patch scripts in `/tmp`, heredocs, and `sed`/`cat` through bash in place of `read` | 11 | 1 | 1 | **13** | Not in hand. FRE-487 covers governance of bash file reads |
| 7 | **Tool descriptions:** bash cwd and relative paths (4), `web_search` `categories='it'` returns MDN pages (4), `run_python` keeps no state between calls (3), a brief that depends on a sibling task's output (2), workers lack the current date (1) | 4 | 5 | 5 | **14** | Not in hand |
| 8 | **Allowlist refusals** on a harness turn, which has no approval transport, so the refusal stands. One refused command is the infrastructure-health skill's own `psql` probe (`fn_s3#144`) | 8 | 0 | 0 | **8** | Overstated: a PWA turn asks the owner. The refusal text: G5 |
| 9 | **Large page fetches** | 0 | 0 | 5 | **5** | FRE-1569, deployed |
| — | Model's own choice (12) and guessed URL (1) | 10 | 3 | 0 | 13 | — |
| — | Study design: memory recall during the consolidation pause | 8 | 3 | 0 | 11 | Excluded (stage-3 proposal 8) |

**Guessed URLs** (master's live trace `ea5154b5`: 14 of 49 `fetch_url` calls failed on composed
documentation URLs):
- In this data the pattern is rare: one composed Wikipedia URL returned 404 (`fn_s3#111`).
- Three more composed Wikipedia URLs worked (`fn_s3#53`, `#112`, `#113`). The model composed them because
  search timed out.
- In stage 3 the researcher had no `fetch_url`, which may explain the low count. The stage-4 sample is small.

### G4 — Our skills against the guidance

**Verdict: POSITIVE** (counts over the 23 skills).

**Query:** a read-only audit of `docs/skills/*.md` against the guidance checklist. A Claude subagent wrote it,
with a file:line citation for each claim. The explore seat re-checked the claims below, in the code or by
running the routing function offline.

**Output:**

| Check | Skills that pass, of 23 |
|---|---|
| Precise trigger (keywords that do not fire on unrelated text) | 6 |
| Preconditions stated | 21 |
| Canonical commands given | 21 |
| Tested script for fragile mechanics | 6 (5 not applicable) |
| Decision points with criteria | 16 |
| **Success criteria (how to check the result)** | **5** |
| **Recovery policy (what to do on a failure)** | **9** |
| Size kept to what the agent would not otherwise know | 18 |
| Free of stale or contradicting content | 9 |

**Re-checked by the explore seat:**
1. **Substring keyword routing** (`skills.py:389`, `kw.lower() in msg_lower`). It was run offline on two
   plain messages:
   - "Which model is best for logic puzzles?" loads `mermaid-diagrams` ("model"), `query-elasticsearch`
     (" log", inside " logic") and `self-telemetry`.
   - "Please draw up a paragraph on the drawbacks of Spanish trade." loads `mermaid-diagrams` ("draw",
     "graph") and `query-tempo` ("span", inside "Spanish").
2. **Skill commands that the allowlist refuses.** The infrastructure-health skill's `psql "$(…)" -c` probe
   (`infrastructure-health.md:37`) is the command that was refused live on `fn_s3#144`.
3. **A blocked-command rule applies to all bash commands.** self-telemetry's `known_bad_pattern`
   `"from personal_agent"` (`self-telemetry.md:61`) carries a reason about `run_python` only.
4. **The skill template has no success or recovery section** (`docs/skills/SKILL_TEMPLATE.md`).

**Not re-checked (the subagent's reading, cited):**
- the mermaid validation command misses the allowlist;
- `scripts/es_index_granularity.py` is absent from the gateway image (INFERRED from
  `Dockerfile.gateway:66-72`);
- stale statements in read-write, turn-forensics and bash.md.

**Good examples:**
- mermaid-diagrams has the best success-and-recovery section: "VALID" means that the output file exists, the
  model must not rely on `$?`, and there are 3 repair attempts, then a fallback.
- run-python and system-diagnostics carry stop rules.
- web-search and personal-history-recall give clear "use this, not that" guidance.

### G5 — Our tools against the guidance

**Verdict: POSITIVE.**

**Query:** the same audit over `src/personal_agent/tools/`. The explore seat re-checked the items marked ✓.

**Output:**

| Tool | Abstraction | What the model sees on a failure |
|---|---|---|
| `bash` | A shell wrapper: the model builds every command | `success:false` with `exit_code`, `stdout` and `stderr`. Clear |
| `read` | A useful abstraction: head, range and tail paging | The truncation text gives the exact next calls. The best example |
| `write` | Whole-file overwrite or append only | ✓ **No edit tool exists.** A partial change needs a full rewrite or bash |
| `run_python` | A sandbox. Reasonable | `exit_code`, `timed_out`, `oom` |
| `web_search`, `fetch_url` | Useful abstractions | ✓ Raised errors are cut to 150 chars (`tool_dispatch.py:304`) |
| `query_telemetry` | A narrow, validated tool: the guidance's ideal | ✓ Refusals are cut to 150 chars, which drops the list of allowed fields. ✓ Workers only |

- **No shared error format.** No tool returns `retryable` or `next_action`, except the artifact tools'
  `TerminalToolError`.
- **An allowlist refusal tells the model nothing about the cause.** The capture of `fn_s2` turn 5 shows the
  model's view: `"Permission denied: approval_connection_lost"`. The refused segment goes only to a log
  event.

### G6 — Context that the skills and tools spend without any call

**Verdict: POSITIVE.**

**Query:** ES `tool_result_skill_hint_appended` and `read_skill_invoked` on the 97 traces. Code at
`tool_dispatch.py:280-297`.

**Output:**
- 366 of 438 tool results carried "[hint: skill X is available — call read_skill…]". The model called
  `read_skill` 2 times.
- The hint tracks only skills loaded by `read_skill`. It ignores the skill bodies already inlined in the
  prompt. So every `bash` result names the `bash` skill, whose body the prompt carries on every turn.
- Workers get the hint on `web_search` results too, although they have no `read_skill` tool in stage 3.
  - Flash-Next s2: 48 of 48 `web_search` hints went to researchers.
- Whole bodies are inlined, capped at 8,192 tokens. `skill_bodies_truncated` fired 3 times; one of them
  dropped `query-elasticsearch` on a turn where its keywords matched. `read_skill` also returns a whole
  body. So no real progressive disclosure exists.

### G7 — Security: `run_python` with network on joins the network of the production stores

**Verdict: POSITIVE.** Sent to master at about 16:20 UTC. **Corrected the same hour:** the first report named
the `general` worker, and that was wrong.

**Query:** `config/governance/tools.yaml:25-29` and `run_python.py:71-79` at `origin/main`. ES
`sandbox_starting.network` and `sub_agent_start` on the study traces. `docker network inspect seshat_cloud-sim`.

**Output:**
- **Workers: closed by FRE-1564.** `sub_agent_tools.run_python.param_forced.network: false`
  (`tools.yaml:25-29`). Master reports that `sub_agent_tool_param_clamped` fired twice for `run_python`
  since the 11:33 UTC deploy.
  - On stage-4 trace `883b6190` (s3 t4), the `general` worker's sandbox started with `network: False`
    (13:47:05).
- **The stage-4 sandbox with network on belonged to the primary.** Trace `ca5ddc77`, s3 t3, 13:45:54. That
  trace has no `sub_agent_start`.
  - The explore seat's first count was per session, not per role, and so named the worker by mistake.
- **The stage-3 counts predate FRE-1564:** 14 of 17 sandboxes in Flash-Next s3 and 6 of 27 in s2 ran with
  network on, primary and workers together.
- **What remains, for the primary:**
  - `run_python` takes a model-set `network: bool` and needs approval in ALERT and DEGRADED only.
  - A network sandbox joins `seshat_cloud-sim`. That network is `internal=false`, and it holds production
    Elasticsearch, Postgres, Neo4j, Redis, Tempo and the gateway.
  - So code that the model writes can reach Elasticsearch without credentials, and the other stores
    directly. Master takes this to the owner as a defense-in-depth item: an egress-only sandbox network.

The bash allowlist bypass (newline, single `&`, `$(…)`, backticks) is **FRE-1572**, and is not repeated here.

---

## Proposals

At most ten. Each is for master's disposition and the owner's decision. None was executed by this seat.

1. **Give the primary the telemetry tool.** `query_telemetry` with Tempo (FRE-1567) is worker-only, so the
   primary still writes `curl` against a stale skill. 42 calls are in this bucket (G3 #1a). The cost is the
   tool's description in every primary prompt. The owner weighs that cost.
2. **Let a worker read the file its task names**, read-only, or keep file-editing tasks on the primary. Then
   no worker rebuilds a file from memory (21 calls, G3 #2).
3. **Add an edit tool** (an exact-string replace, with a clear error when the old text is missing or not
   unique). This removes patch scripts and full rewrites (13 calls, G3 #6).
4. **Watch the search backend.** Alert on the share of `degraded_retrieval` and on SearXNG timeouts.
   Reconsider the 12-second timeout (24 calls, G3 #3).
5. **Stop overlapping research.** Give each researcher the queries that earlier researchers in the turn
   already ran, and let the planner avoid briefs that overlap (24 calls, G3 #4).
6. **One error format for every tool:** `{ok, error: {code, message, retryable, next_action}}`. Remove the
   150-char cut for refusals that carry the fix (`query_telemetry`'s allowed fields). An allowlist refusal
   names the refused segment and a permitted alternative (G5).
7. **Skill hygiene, in this order:**
   1. Fix the hint loop (G6).
   2. Match whole words, and prune generic keywords (G4).
   3. Add success and recovery sections to the template.
   4. Add a test that every skill command passes the allowlist, which matters more once FRE-1572
      tightens parsing.
   5. Fix the stale and contradicting content.
   6. Split the largest skills into a core and parts loaded on demand.
8. **Fix the tool descriptions in G3 #7:**
   - bash: say where commands start, and use absolute paths;
   - `web_search`: describe what `categories` returns;
   - `run_python`: state that no state is kept between calls;
   - give workers the current date;
   - the planner must not write a brief that depends on a sibling task's output (14 calls).
9. **Give network sandboxes an egress-only network** without the production stores (G7, the primary's
   residual). Master takes this to the owner. The worker path is already closed by FRE-1564.

## Filed tickets

None. Master filed FRE-1572 from this study's side finding. Master takes G7's residual to the owner.

Related tickets that this report counts as in hand: FRE-1564, FRE-1565, FRE-1567, FRE-1568, FRE-1569 and
FRE-487. FRE-1570 concerns grounding output, not tool calls, so it overlaps nothing here.

## Caveats

- **Harness turns have no approval transport,** so allowlist refusals overstate production, where a PWA turn
  asks the owner.
- **Consolidation was paused,** so memory recalls of session content could not succeed. Those calls are
  counted apart (G2).
- **Fewer calls is not the goal.** A fix can add a call where a call is the right step.
- **The labels are Claude judgments under fixed definitions.** One person cross-checked 39 rows. For sub-agent
  rows no result text exists: those labels rest on arguments, sizes, exit codes and result URLs.
- **ES counts are provisional** (FRE-1051). The per-session counts match the runner rows and the captures
  where both exist.
- **Gemma stage 4 covers only 17 turns,** and it ran after FRE-1564 and FRE-1569.

## Method appendix

- **Scripts** (committed for provenance in `scripts/eval/fre1517/tool_audit/`; run them from a scratch copy,
  because they write their outputs next to themselves):
  - `extract.py`: every event of each study trace;
  - `calls.py`: one row per attempt, joined by `span_id`, plus the refused bash attempts;
  - `enrich.py`: the primary's tool results from the captures, and `web_search` result URLs;
  - `buckets.py`: the fix assignment, with the hand assignments by id.
- **Definitions** are in `DEFINITIONS.md`, written before the first label.
- **Labels:**
  - three subagents, each reading its sessions in call order, so that "repeated" sees the earlier calls of
    the turn;
  - the files `labels_*.jsonl`, outputs not committed;
  - one subagent for the skill and tool audit, `skill_tool_audit.md`, kept by the explore seat, with
    file:line citations.
- **Hand checks by the explore seat:**
  - the 39-row cross-check;
  - the allowlist splitter, run offline on 5 commands;
  - the skill router, run offline on 2 messages;
  - `query_telemetry` `worker_only`, `tool_dispatch.py:280-304`, `worker_types.py` and `run_python.py` at
    `origin/main`;
  - the sandbox network inspect.
- **Archive paths:** `captures_archive_fre1517/2026-10-09/`, `/2026-10-10/` and `/2026-10-10-stage4/`.
