---
name: turn-forensics
description: Reconstruct and diagnose a single agent turn across all substrates from its trace_id — per-call timeline, context-token attribution (what bloated the window), latency, retries, and (when available) the inference-server request log. Use for "why was this turn slow / why did context blow up / replay trace X".
when_to_use: When the user asks why a turn was slow, why context grew, what a turn did step by step, or wants to replay/diagnose a specific trace_id. Always query live logs — never reconstruct from memory.
tools: [bash]
nudge: "Reconstruct the turn from live logs (gateway + Postgres + R2 + inference-server). Never infer the timeline or token counts from priors — pull them."
keywords:
  - why was that slow
  - why so slow
  - context jump
  - context blew up
  - token jump
  - replay trace
  - replay the turn
  - what did the turn do
  - turn forensics
  - diagnose the turn
  - where did the time go
  - trace_id
---

# SKILL: turn-forensics

> **Tier:** 2 — log/SQL analysis via `bash`
> **Inputs:** a `trace_id` (preferred) or `session_id`. If only "the last/slowest turn" is given, find the trace first (step 0).
> **Related:** ADR-0074 (identity threading — the join key); FRE-401 (context bloat prevention).

---

## What This Skill Does

Takes one turn and reconstructs it end-to-end by joining the substrates on the shared identity tuple (`trace_id` / `session_id`, ADR-0074):

- **Gateway logs** — per-call timeline (`model_call_started/completed`, `input_tokens`, `latency_ms`, `role`), tool calls, state transitions, errors.
- **Postgres `session_events`** — what the PWA received (`RUN_ERROR`, `STATE_DELTA`, replay buffer).
- **R2 artifact store** — bytes actually produced (when the turn wrote an artifact).
- **Inference-server log** — the SLM's view of each request (optional; see §Caveats).

The headline output is a **context-token attribution**: track `input_tokens` per primary call, find the jumps, and blame the preceding tool result (a whole-file `read` is the usual culprit — file bytes ÷ ~4 ≈ tokens).

---

## When to Use

- "Why was that turn slow?" / "Where did the time go?"
- "The context counter jumped to 47K — what caused it?"
- "Replay trace `<id>` step by step."
- **Prefer `query-elasticsearch`** for fleet-wide log questions; this skill is for *one* turn in depth.

---

## Commands

All gateway commands strip ANSI colour first: `sed -r 's/\x1b\[[0-9;]*m//g'`. Container names come from `docker-compose.cloud.yml` (`cloud-sim-seshat-gateway`, `cloud-sim-postgres`).

### 0. Find the trace (if not given)

```bash
docker logs cloud-sim-seshat-gateway --since=30m 2>&1 | sed -r 's/\x1b\[[0-9;]*m//g' \
  | grep -E "task_failed|model_call_completed" | grep -oE "trace_id=\S+" | sort | uniq -c | sort -rn | head
```

### 1. Per-call timeline + context attribution (the core)

```bash
TRACE=<trace_id>
docker logs cloud-sim-seshat-gateway --since=40m 2>&1 | sed -r 's/\x1b\[[0-9;]*m//g' \
  | grep "$TRACE" | grep -E "model_call_completed|tool_call_started|artifact_draft_sub_agent_complete" \
  | python3 -c "
import sys,re
for l in sys.stdin:
    ts=re.search(r'(\d\d:\d\d:\d\d)\.\d+Z', l); t=ts.group(1) if ts else '?'
    if 'tool_call_started' in l:
        a=re.search(r\"'path': '([^']+)'|'command': '([^']{0,60})\", l); n=re.search(r'tool_name=(\S+)', l)
        print(t,'TOOL', n.group(1) if n else '?', (a.group(1) or a.group(2)) if a else '')
    elif 'sub_agent_complete' in l:
        print(t,'SUBAGENT done')
    else:
        it=re.search(r'input_tokens=(\d+)',l); lat=re.search(r'latency_ms=(\d+)',l); role=re.search(r'role=(\w+)',l)
        print(t,'CALL in=%s lat=%ss %s'%(it.group(1) if it else '?', int(int(lat.group(1))/1000) if lat else '?', role.group(1) if role else ''))
"
```

Read the `in=` column down the primary calls. A big step up = context bloat; the `TOOL read <path>` immediately before it is the cause. Cross-check file size: `wc -c <path>` (÷4 ≈ tokens).

### 2. What the PWA received (Postgres `session_events`)

```bash
docker exec cloud-sim-postgres psql -U agent -d personal_agent -c \
 "select seq, event_type, payload->'data'->>'category' as cat, created_at \
  from session_events where session_id='<session_id>' order by seq desc limit 20;"
```

### 3. Artifact bytes (if the turn wrote one)

```bash
docker exec cloud-sim-postgres psql -U agent -d personal_agent -t -c \
 "select r2_key from artifacts where id='<artifact_id>';"
docker exec cloud-sim-seshat-gateway sh -c 'cd /app && /app/.venv/bin/python -c "
import asyncio; from personal_agent.storage.artifact_store import get_artifact_store
print(len(asyncio.run(get_artifact_store().get(\"<r2_key>\"))))"'
```

### 4. Align the inference-server log (optional)

The SLM server logs `routing_request backend=… model_id=… port=…` per request. To map them to gateway calls:

- **Normalise the clock.** The SLM box runs **UTC+2 (CEST)**; the gateway logs **UTC**. Subtract 2h from SLM timestamps before matching. (Fix at source: log UTC, and propagate `trace_id` as a request header so no time-matching is needed — see the SLM-log-ingestion follow-up.)
- **Map by model/port:** primary → `…-A3B` on port `8502`; sub-agent → `…-A3B-subagent` on port `8503`.
- **An extra SLM request inside one gateway call window = a client retry.**

---

## Interpreting Results

- **Context jump after a `read`** → whole-file slurp. `read` defaults to a 1 MiB cap, so any source file under ~1 MB returns in full (e.g. `executor.py` ≈ 134 KB ≈ 33K tokens). Mitigation lives in the tool (ranged reads / lower token cap) — FRE-401.
- **Slow primary call with no tool before it** → large prefill (the bloated context is re-sent every step) or a degraded SLM origin (multi-minute calls, 524s). Restarting the SLM server fixes the latter.
- **Gap between `sub_agent_complete` and the final reply** → the post-tool *synthesis* primary call running on the now-bloated context (not the sub-agent being slow).
- **Terminal tool failure** → look for `tool_terminal_short_circuit` (FRE-402); the turn should end in ~ms with a `tool_failure` `RUN_ERROR`, no recovery LLM call.

---

## Caveats

- The inference-server log lives on the **Mac SLM host, not in `/opt/seshat`** — §4 only works when that log is reachable or pasted in. Steps 1–3 are fully self-contained on the gateway VPS.
- `input_tokens` (model tokenizer) and the status bar's `turn_status.context_tokens` (`estimate_messages_tokens` heuristic) differ by ~10–20%; same trend, different absolute.
