# FRE-1728: MemAudit eval pilot

This pilot drives Seshat with MemAudit's simulated users on the eval gateway and ranks the defects that it finds.
It finds defects. It does not report a score.

- **Ticket:** FRE-1728. **Gates:** ADR-0087 D5, write path and retrieval path.
- **MemAudit:** <https://github.com/sora1998/MEMAUDIT-bench>, commit `0c20cf72`. Paper: <https://arxiv.org/abs/2606.24595>.

## How it fits together

| Process | Environment | Files |
|---|---|---|
| Bridge on loopback `:9016`. It owns `IsolatedArmRunner`, the wipe, the graph read and the spend stop. | Project, Python 3.14 | `run.py`, `stack.py`, `bridge.py`, `spend.py` |
| Driver. It runs MemAudit's simulator, judges and attribution. | Its own, Python 3.14, `~/.cache/seshat-fre1728/venv` | `memaudit_driver.py`, `memaudit_adapter.py`, `defect_rank.py` |

| Scout scan. It runs after the driver, on the saved task transcripts, and makes no gateway turn. | Its own, Python 3.14, `~/.cache/seshat-fre1728/scout-venv` | `scout_scan.py`, `scout_questions.json` |

The bridge and the driver talk only over HTTP. MemAudit stays outside the repo at `~/.cache/seshat-fre1728/memaudit`.
No MemAudit file changes. The environment holds the OpenAI client only, not MemAudit's `requirements.txt`.

**Scoring.** The driver reads the graph before it asks any question. It then asks each target fact once, in a fresh session.

| Graph score | Answer score | Outcome | Ranked |
|---|---|---|---|
| Low | Low | `write_failure` | Yes |
| At least 0.75 | Low | `retrieval_failure` | Yes |
| Low | At least 0.75 | `answer_without_graph` | No, reported only |
| At least 0.75 | At least 0.75 | `ok` | No |

**Conversation defects.** MemAudit's labels cover memory only. Inspect Scout (<https://meridianlabs-ai.github.io/inspect_scout/>, MIT, 0.6.2) asks each question in `scout_questions.json` of each task transcript. A "yes" becomes `conversation_failure`, with the question's name as its label. Edit that file to change the questions, and give a new question a severity.

A defect is `(outcome, label, category)`. It counts only if it appears in both runs. Its score is the lower count
times the severity of its label in `severity.json`. **The owner sets those severities.** Memory and conversation defects share one ranking. The values in the file
are placeholders: 1 for a Seshat label and 0 for a label that blames the simulator or the task.

**The stop rule.** Continue past the pilot only if the owner confirms that at least 3 of the top 5 defects are real
and worth a ticket.

## Dry run, with no paid call

```bash
scripts/eval/fre1728/setup_memaudit.sh
set -a; . <(grep -E '^(POSTGRES_PASSWORD|NEO4J_PASSWORD|SESHAT_APP_EVAL_PASSWORD)=' /opt/seshat/.env); set +a
RUN=$HOME/.cache/seshat-fre1728/runs/dry
uv run python -m scripts.eval.fre1728.stub_stack up --run-dir $RUN
uv run python -m scripts.eval.fre1728.isolation_probe --run-dir $RUN    # AC-2, about 20 minutes
uv run python -m scripts.eval.fre1728.run --arm stub --users user_019 --tasks-per-user 1 --runs A B --run-dir $RUN
uv run python -m scripts.eval.fre1728.stub_stack down
```

## Preflight for the paid pilot (AC-5)

Master runs steps 1 to 4 from `/opt/seshat`. Never run the eval compose from a worktree: it recreates production's searxng.

### 1. Rebuild and repair the eval stack

```bash
make eval-infra-up
docker exec cloud-sim-seshat-gateway-control python -V     # expect Python 3.14
docker exec cloud-sim-seshat-gateway-treatment python -V   # expect Python 3.14
```

**Eval Postgres is stale.** On 2026-11-02 it lacked 26 production columns: 14 in `route_traces`, 7 in `settings_edit_audit`,
4 in `settings_overrides` and `budget_reservations.counter_ids`. `init.sql` runs only on a new volume. This command deletes
eval history only, including $26.45 of old eval `api_costs` rows:

```bash
F="-p seshat --env-file /opt/seshat/.env -f docker-compose.cloud.yml -f docker-compose.eval.yml"
docker compose $F rm -sf postgres-eval
docker volume rm seshat_seshat_postgres_eval
docker compose $F up -d --no-deps --wait postgres-eval
```

### 2. Apply the pilot overlay to control, and stop treatment

Treatment shares control's captures, Postgres and Neo4j, and it holds real keys. If both run, the same work is paid for twice. `run.py` refuses to start.

```bash
F="$F -f scripts/eval/fre1728/docker-compose.fre1728.yml"
export BUILD_FINGERPRINT=$(uv run python -m scripts.eval.gateway_freshness --print-fingerprint)
plan=$(docker compose $F up -d --no-deps --dry-run seshat-gateway-control 2>&1) \
  && printf '%s\n' "$plan" | uv run python -m scripts.eval.compose_plan_guard \
  && docker compose $F up -d --no-deps --build seshat-gateway-control
docker stop cloud-sim-seshat-gateway-treatment
docker inspect cloud-sim-searxng --format '{{.State.StartedAt}}'   # must not change
```

**Check the Voyage key.** The relevance gate trusts only the calibrated reranker. Without the key, every memory turn says "records could not be reached" (measured on the stub, 2026-11-02), and each one would rank as a false retrieval defect. The overlay passes the key. In the preflight task, read the primary's request and confirm a `reranker_applied` event with provider `voyage`.

### 3. Deployments per role

Production has no row in `settings_overrides` or `user_constraint_preferences` (read on 2026-11-02), so `config/model_roles.yaml` decides. The eval gateway uses the same file.

| Role | Deployment | Cost |
|---|---|---|
| `primary` | `qwen3.8-flash-next`, local llama.cpp on the owner's Mac | None, but it uses the Mac |
| `sub_agent` | `inherit`, worker mode | As primary |
| `entity_extraction`, `compressor` | `gpt-5.4-mini` | Paid |
| `captains_log`, `artifact_builder` | `gpt-6.1-sol` | Paid |
| `session_summary`, `span_extraction`, `entailment`, `insights`, `vision` | `claude_sonnet` | Paid |
| `embedding` | `embedding`, OVH | Paid, small |
| `reranker`, `reranker_fallback` | Voyage, then the Mac through `caddy:8600` | Paid, small |
| MemAudit simulator, judges, attribution | `gpt-5.4-mini`, called directly | Paid |
| Scout scanner | `gpt-5.4-mini`, called directly. Change it with `--scout-model`. | Paid |

### 4. Credit check per vendor

**No balance was read in this build.** No read-only balance call is available here, and this build makes no paid call. A rate-limit header is not a balance. The owner must read each balance before the first go, and the go must quote it.

| Vendor | Used by | Shares production's credit | Projected | Balance |
|---|---|---|---|---|
| OpenAI | Extraction, MemAudit, Scout | Yes | About $3 | Owner reads the dashboard |
| Anthropic | Span extraction, summaries | Yes | About $8 | Owner reads the dashboard |
| OVH | Embeddings | Yes | Under $1 | Owner reads the dashboard |
| Voyage | Reranker | Yes | Under $1 | Owner reads the dashboard |
| Exa, Brave | `web_search`, through production's searxng | **Yes** | About 330 calls, $2 | Owner reads the dashboards |

FRE-1641 used up the Exa and Brave credit that production shares, so read those two first. Stop the run on any vendor credit or quota error.

### 5. The run

```bash
set -a; . <(grep -E '^(POSTGRES_PASSWORD|NEO4J_PASSWORD|AGENT_OPENAI_API_KEY)=' /opt/seshat/.env); set +a
# One task, after the owner's first go is recorded on the ticket. Read the whole trace.
uv run python -m scripts.eval.fre1728.run --arm control --users user_019 --tasks-per-user 1 --runs A \
  --owner-go "<the owner's go, as quoted on FRE-1728>"
# The full pilot, after the owner's second go.
uv run python -m scripts.eval.fre1728.run --arm control \
  --owner-go "<the owner's go, as quoted on FRE-1728>"
```

Exit code 0 means complete, 3 means the spend cap stopped the run, and 4 means a harness failure. The result is `report.md` in the run directory.

### Cost basis

**The ticket's $33 leaves out the questions.** 2 users, 8 tasks and 2 runs give about 112 task turns, and the scoring adds 32 question turns, so the total is about 144 turns.

| Case | Basis | Projection |
|---|---|---|
| Local primary, which is production's binding | Below | **About $13** |
| Ticket's basis applied to 144 turns | $0.27 per turn, plus MemAudit | About $40, close to the $45 stop |

The local case, from production `api_costs` over 60 days:

| Item | Unit cost | Count | Total |
|---|---|---|---|
| Span extraction | $0.033 | 144 | $4.81 |
| Entity extraction | $0.0075 | 144 | $1.08 |
| Session summary, not separately measured | About $0.05 | 64 | About $3.20 |
| Web search | $0.006 | About 330 | About $2 |
| MemAudit, estimated from prompt sizes | | | About $1.20 |
| Scout, estimated: 32 transcripts, 5 questions, about 3,000 input tokens per call | $0.003 | 160 | About $0.45 |

The search count uses a production rate of 192 calls in 11 traces, so it may be far from the simulated tasks. The preflight task measures all of these, and master must correct this projection from it.

**Eval budget policies.** Eval `_total` is $30 per week, with a daily cap of $5 per background role. This is lower than the $45 stop. A denied extraction looks like a write failure, so the run reports `budget_denied` rows and marks that user's facts invalid. On a cloud primary, the budget gate acts first. A change to these caps is the owner's decision.

**The spend stop.** The bridge checks the spend before each turn, and the driver checks it before each MemAudit attempt. At $45, the bridge stops the gateway container, so background work ends. The overshoot is the work in flight at that moment. A failed MemAudit attempt that a vendor billed is not visible to this count.

**Scout's spend.** Scout documents no cost cap, and its calls are not in that count. `scout_scan.py` estimates its cost from the transcripts' size, with a margin of 1.5, and refuses to start if the estimate exceeds the budget that remains. Its real token use is in the scan's output. Exit code 6 from `run.py` means that the scan failed or was refused, and `report.md` then holds memory defects only. To scan again, run `scout_scan.py` with the saved transcripts. This makes no gateway turn.

### Time

| Item | Basis | Projection |
|---|---|---|
| Primary | Production local model time per trace, 30 days: p50 73 s, p90 219 s | 3 to 9 hours for 144 turns |
| Waits | 150 s of quiet at each wipe and settle, 10 s per turn | About 1 hour |

The owner's Mac must stay awake, and a pilot turn competes with the owner's own turns on llama.cpp. Hold other timing measurements during the run.

### Differences from production that remain

| Difference | Effect on this pilot |
|---|---|
| The owner's display name is `Sam` | Avoids a conflict with the persona |
| `channel=EVAL` | Blocks side-effecting tools, and suppresses Captain's Log promotion, so transcripts come from the runner |
| Session summary idle 60 s and sweep 30 s; production 900 s and 300 s | A digest arrives sooner than in production. Chosen by the ticket, to avoid a run of 8 hours or more |
| **Primitive tools off**; production has them on | The primary has no `bash`, `read`, `write` or `edit`. A task that needs them gets a different answer than in production. They stay off because this pilot tests memory and conversation. FRE-1729 fixed one defect in `read`, and that does not show that these tools are safe here. A run with them on needs its own security check and preflight |
| Captain's Log reflection interval 86400000 s; production 1800 s | Reflection effectively off. Saves about $0.05 per reflection |
| `AGENT_CLOUD_WEEKLY_BUDGET_USD` 50; production 30 | Eval's own budget |
| R2 values point nowhere | R2 tools register, and a call fails. An `artifact_draft` call can still pay for its builder |
| No Linear, Perplexity, Scaleway or abuse.ch key | Those tools fail. A Scaleway primary cannot be selected |
| Auth off, one identity for all simulated users | The wipe separates users |
| A gateway restart at each wipe | Process caches clear, which a real user's sessions do not do |
| Postgres and Elasticsearch keep rows across a wipe | See the AC-2 measurement on the ticket |

### Known limits

- It tests memory and conversation, not tool use.
- It tests volume, not the passing of weeks, because the gateway uses the real clock.
- Simulated users shift success rates (arXiv 2601.17087), so use this to find defects, not to report a score.
- Humans agreed with MemAudit's labels 75% of the time, so the owner must read the top defects.
- A question's answer can itself reach the graph, so a later question in that user's set can see it. The graph read comes first and is not affected.
- `user_001` and other users are children. The default users are working adults, so the tasks resemble the owner's use.
- Scout is an LLM reading a transcript, so a positive answer can be wrong. The owner must read the top defects.
- MemAudit pins Python 3.10.20, which is past end of life. This pilot runs the parts that it imports on Python 3.14, and it does not reproduce the paper's environment.

## Data terms

Read on 2026-11-02. This repo holds no MemAudit data or code, and nothing is redistributed.

| Source | Terms |
|---|---|
| MemAudit code and benchmark data | CC BY 4.0, so a report that uses them cites the paper |
| O\*NET | CC BY 4.0 |
| Inspect Scout | MIT, from its PyPI metadata |
| DeepPersona (`thzva/Deeppersona`) | Its README says MIT, but the repo has no LICENSE file and GitHub reports no license |
