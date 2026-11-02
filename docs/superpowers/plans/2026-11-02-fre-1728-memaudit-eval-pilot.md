# FRE-1728: MemAudit eval pilot, implementation plan

**Ticket:** FRE-1728. **Design intent:** the ticket body, and ADR-0087 D5 for the write and retrieval gates.
**Tier:** Standard (eval code, cost stop, several files). This build makes no paid call and no call to a real model.

## Measured findings that shape the design

1. **The wipe.** `IsolatedArmRunner.run_turn` wipes on every call with `session_id=None`
   (`scripts/eval/eval_isolation.py:361-365`). This confirms the search agent's report.
2. **A graph wipe alone does not isolate.** `consolidate_recent_captures` reads the last 7 days of capture
   files and skips a capture only if its `Turn` exists in the graph (`second_brain/consolidator.py:324`).
   After a wipe, it extracts every earlier capture again. An earlier user's facts return, and extraction is
   paid again. The wipe must also delete the capture files.
3. **Control and treatment share state.** Both use the same captures volume, Postgres and Neo4j, and both hold
   real keys. Treatment must be stopped for a run on control. A stub session must not reach their database,
   because their idle sweep can summarize it on real Sonnet.
4. **The identity.** A non-owner identity changes the code path (Operator stanza, stances and claims attach
   to a `Person`). The owner identity is the code path that the owner sees, so the pilot uses it.
   The overlay sets a neutral `AGENT_OWNER_NAME`, so the real name does not contradict the persona.
5. **Identity nodes.** A wipe removes the owner's `Person` and `Agent` nodes, which only startup creates.
   The wipe restarts the gateway, which creates them again and clears process caches.
6. **Eval budget policies.** Eval `_total` is $30 per week, and each background role has a daily cap of $5.
   A denied extraction looks like a write failure. The run reports `budget_denied` rows, and a user with a
   denial is marked invalid.
7. **Default primary.** Production binds `primary` to the local `qwen3.8-flash-next`. No settings override
   exists. Main inference on it costs nothing, but it uses the owner's Mac. The $0.27 basis mixes cloud primaries.
8. **Scout.** The owner chose to add it on 2026-11-02. It runs after the driver on saved transcripts, in its own
   environment. Scout documents no cost cap, so `scout_scan.py` refuses to start if its estimate exceeds the remaining budget.
9. **Python.** MemAudit pins 3.10.20, which is past end of life. The parts that this pilot imports work on 3.14, so its
   own environment uses 3.14.

## Architecture

Two processes, in two environments, joined by HTTP on loopback:

- **Bridge (project environment, `scripts/eval/fre1728/bridge.py`).** A FastAPI app. It owns `IsolatedArmRunner`,
  the wipe, the graph read, the settle wait and the spend guard.
- **Driver (MemAudit environment, Python 3.14, `memaudit_driver.py`, `memaudit_adapter.py`).** It imports MemAudit's `Episode`,
  `UserSimulator`, `ReconstructionEvaluator` and `attribute_dim`. It imports no project module.
  `SeshatAgent(MemoryAgent)` talks only to the bridge.

MemAudit stays outside the repo at a pinned commit (`0c20cf72`), in `~/.cache/seshat-fre1728/`. No MemAudit file
changes. `requirements.txt` is not installed. The environment holds `openai==2.30.0` and its dependencies.

## Files

| File | Purpose |
|---|---|
| `scripts/eval/eval_isolation.py` | Add `wipe_on_new_session` (default `True`, so other callers do not change) and a public `wipe(es)` |
| `scripts/eval/fre1337_intent_probe/substrate.py` | Add the `stub` arm, `http://localhost:9014` |
| `scripts/eval/fre1728/spend.py` | `SpendGuard`, `SpendCapReached` |
| `scripts/eval/fre1728/bridge.py` | Bridge app and the wipe sequence |
| `scripts/eval/fre1728/memaudit_adapter.py` | `SeshatAgent(MemoryAgent)` |
| `scripts/eval/fre1728/memaudit_driver.py` | Pilot loop, scoring, attribution |
| `scripts/eval/fre1728/defect_rank.py` | Ranking and the both-runs rule, standard library only |
| `scripts/eval/fre1728/stub_model.py` | Stub for the gateway and for MemAudit's OpenAI calls |
| `scripts/eval/fre1728/stub_stack.py` | `docker run` launcher for the stub gateway, never compose |
| `scripts/eval/fre1728/isolation_probe.py` | AC-2 live probe |
| `scripts/eval/fre1728/run.py` | Starts the bridge, the driver, then the Scout scan |
| `scripts/eval/fre1728/scout_scan.py`, `scout_questions.json` | Conversation defects, merged into one ranking |
| `scripts/eval/fre1728/stack.py` | Wires the bridge to one gateway and its stores |
| `scripts/eval/fre1728/setup_memaudit.sh` | Pinned clone and environment |
| `scripts/eval/fre1728/docker-compose.fre1728.yml` | Overlay for the paid pilot, used by master from `/opt/seshat` |
| `scripts/eval/fre1728/severity.yaml` | Severity per defect class, set by the owner |
| `scripts/eval/fre1728/README.md` | Commands, and the preflight (AC-5) |
| `tests/evaluation/test_fre1728_*.py` | Unit tests with fakes |

## The pilot loop

```
for run in (A, B):
  for user in users:
    bridge.wipe()                      # idle, delete captures, wipe graph, restart gateway, verify 0 nodes
    for task in tasks:                 # 8 tasks, round robin over the bank's 5 categories
      Episode(max_turns=6).run(agent)  # new session per task. The preference judge is off (not asked for).
    bridge.settle()                    # gateway idle; count Turn nodes against the trace ids
    memories = agent.get_all_memories()    # before any probe, so a probe cannot feed the read
    for fact in target_facts:
      graph_score  = slot_fill(memories) + judge
      answer       = agent.ask(question)   # one fresh session, one turn
      answer_score = judge(answer)
      label        = attribute_dim(min(graph_score, answer_score))
rank(defects present in both runs)
```

- **Outcome per fact** (threshold 0.75, MemAudit's own): `graph < 0.75` and `answer < 0.75` is `write_failure`.
  `graph >= 0.75` and `answer < 0.75` is `retrieval_failure`. Both at or above is `ok`.
  `graph < 0.75` and `answer >= 0.75` is `answer_without_graph`, which is reported and not ranked as a defect.
- **The graph read** holds `Entity`, `Claim` and `HAS_STANCE`. Raw `Turn.user_message` stays out, because it holds
  every disclosed sentence and a write failure could not then appear.
- **The last simulator message.** The agent never receives it, because the episode ends. The attribution
  transcript leaves it out, so a fact disclosed only there is not labeled `memory_failure`.
- **Defect key:** `(outcome, label, category)`. Count: facts that fail with that key in both runs.
  Score: count times severity. Each entry lists its user, dimension and transcript paths for the owner to read.

## Spend stop (AC-4)

`SpendGuard.spent = sum(eval api_costs since run start) + MemAudit tokens times gpt-5.4-mini price`. The bridge checks it
before every turn and answers 402 at or above the cap. The driver wraps the `get_completion` name in `simulation`,
`scorer` and `failure_attribution`, and checks before each attempt. It reports usage through `token_tracker.log_usage`,
replaced with a function that cannot raise (`get_completion` swallows exceptions and retries, so a raise there
pays again). The run exits with code 3 and writes `stopped: spend_cap`.

Codex plan review, accepted:

- One gateway turn can start consolidation over up to 50 captures. At the cap, the bridge **stops the gateway
  container**, so background work ends. The overshoot is the work in flight at that moment, not a fixed number of calls.
- One `get_completion` call can make three attempts, and the OpenAI SDK retries too. The wrapper passes
  `max_retries=1`, sets the SDK client to `max_retries=0`, and loops up to three times with a check before each attempt.
  A failed attempt that the vendor billed is not visible. The report states the count of failed attempts.

## Harness artifacts that must not rank as defects

- **A failed MemAudit call.** `get_completion` returns `None`, and `_judge` then returns score 0.0 with an empty reason.
  The wrapper raises after three failed attempts, so the run stops with `stopped: harness_failure`.
  A judge reply that does not parse also returns an empty reason. The fact is then marked invalid and is not ranked.
- **Replay.** `Episode` passes the task plus the dialogue on later turns. The gateway holds the session already, so
  the adapter sends only `history[-1]["user"]`. A test asserts the exact message.
- **The last simulator message.** `attribute_dim` reloads the episode and includes every simulator message. The driver
  therefore writes two files per episode: the full transcript, and an attribution copy with the last message removed.
  `failure_attribution._HERE` points at the run directory, so `attribute_dim` reads the copy.

## The stub run cannot reach a real model

- `model_roles.yaml` is mounted with every model role bound to the local deployment, and `AGENT_SLM_BASE_URL` names the stub.
- The embedder uses the managed path, with the endpoint at the stub and a placeholder token.
- Every cloud key is a placeholder.
- `--add-host` maps `caddy`, `searxng`, `tempo`, the production store names and each host in `config/models.yaml` to
  `127.0.0.1`. The reranker fallback is `caddy:8600`, which reaches the owner's Mac, so this mapping is required.
- The stub uses its own Postgres database and Redis index, so the sweep of the real-keyed eval gateways cannot see it.
- The stub captures volume is its own. The run asserts `api_costs` is empty at the end.

## Steps

1. Failing tests, then `eval_isolation.py` and `substrate.py`. Test: `uv run pytest tests/evaluation/test_fre1728_isolation.py tests/evaluation/test_fre1372_eval_isolation.py tests/evaluation/test_fre1337_substrate_guard.py`.
2. Failing tests, then `spend.py` and `bridge.py`. Test: `make test-file FILE=tests/evaluation/test_fre1728_bridge.py`.
3. Failing tests, then `adapter.py`, `defects.py`, `driver.py`, with a fake `base_agent` and fake MemAudit parts.
4. `stub_model.py`, `stub_stack.py`, `isolation_probe.py`. Live AC-2: real production image, stub model, eval Neo4j and Elasticsearch, its own Postgres database and Redis index.
5. `run.py`, `setup_memaudit.sh`, overlay, severity file, README.
6. Live AC-3 dry run. **This step runs downloaded MemAudit code, and it needs the owner's permission.**
7. Gates, self-review, PR, handoff.

## Acceptance criteria

| AC | Proof |
|---|---|
| AC-1 | `test_fre1728_adapter.py` with the stub, and the live dry run with the real `MemoryAgent` |
| AC-2 | `test_fre1728_isolation.py`, plus `isolation_probe.py` output: fact readable in the next session; removed by the wipe; seeded negatives: a per-session wipe loses the fact, and a wipe that keeps captures brings it back; Postgres and Elasticsearch counts across the wipe |
| AC-3 | `run.py --arm stub --users user_019 --tasks-per-user 1 --runs A B` output |
| AC-4 | `test_fre1728_spend.py`: a run stops at the cap and makes no further turn |
| AC-5 | README preflight, linked from the PR |
| AC-6 | Master and owner, after merge |
