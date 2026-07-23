"""Author the pre-registered session-digest fixture sets (ADR-0124, FRE-947).

The ground truth for every case is hand-written **here** and emitted to JSON
alongside. Kept as a script rather than as raw JSON so the labelling stays
reviewable: a reviewer checking whether AC-12's negatives really are Tier-C needs to
read intent, not 900 lines of escaped capture records.

Run once, commit the output, never re-run to "improve" a set after seeing results —
see REGISTRY.md for the fixture discipline this serves.

    uv run python tests/fixtures/session_digest/build_fixtures.py
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

_OUT = pathlib.Path(__file__).parent
_USER_ID = "00000000-0000-0000-0000-000000000001"
_SESSION_START = "2026-07-20T10:00:00+00:00"


def _ts(minute: int) -> str:
    return f"2026-07-20T10:{minute:02d}:00+00:00"


def _tool(
    name: str,
    *,
    output: Any = None,
    error: str | None = None,
    success: bool = True,
    arguments: dict[str, Any] | str | None = None,
    include_arguments: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tool_name": name,
        "success": success,
        "output": output,
        "error": error,
        "latency_ms": 14.0,
    }
    if include_arguments:
        result["arguments"] = arguments if arguments is not None else {}
    return result


def _capture(
    session_id: str,
    idx: int,
    *,
    user: str,
    assistant: str | None,
    tools: list[dict[str, Any]] | None = None,
    tools_used: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": f"{session_id}-t{idx}",
        "session_id": session_id,
        "timestamp": _ts(idx * 5),
        "user_message": user,
        "assistant_response": assistant,
        "steps": [],
        "tools_used": tools_used or [t["tool_name"] for t in (tools or [])],
        "outcome": "completed",
        "user_id": _USER_ID,
        "tool_results": tools or [],
    }


def _write(name: str, payload: dict[str, Any]) -> None:
    path = _OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    cases = payload.get("cases", payload.get("sessions", []))
    print(f"wrote {path.name}: {len(cases)} cases")


# ==========================================================================
# AC-8 — input completeness
# ==========================================================================


def ac8() -> dict[str, Any]:
    """Three sessions covering the input dimensions AC-8 names.

    Deliberately includes a gate-blocked and a malformed-argument invocation: those
    reach a capture only after this ticket's capture-completeness fix, so their
    presence in the prompt is itself the proof that fix works end to end.
    """
    long_answer = (
        "The reindex completed. Every shard reallocated cleanly and the cluster "
        "returned to green. " * 25
    ).strip()

    multi_result = [
        _capture(
            "ac8-multi",
            1,
            user="check the cluster and the config",
            assistant="The cluster is green and the config parsed.",
            tools=[
                _tool(
                    "query_elasticsearch",
                    output='{"status": "red", "unassigned_shards": 4}',
                    arguments={"index": "agent-logs-*", "size": 10},
                ),
                _tool(
                    "read_file",
                    output="retention_days: 30\nshards: 3",
                    arguments={"path": "/etc/seshat/retention.yaml"},
                ),
            ],
        ),
        _capture("ac8-multi", 2, user="and the shard count?", assistant="Four are unassigned."),
    ]

    failed_calls = [
        _capture(
            "ac8-failed",
            1,
            user="read the missing config",
            assistant="I could not read that file.",
            tools=[
                _tool(
                    "read_file",
                    success=False,
                    output=None,
                    error="ENOENT: /etc/seshat/missing.yaml",
                    arguments={"path": "/etc/seshat/missing.yaml"},
                ),
                # Never dispatched — malformed argument JSON.
                _tool(
                    "query_elasticsearch",
                    success=False,
                    output=None,
                    error="malformed argument JSON: Expecting ',' delimiter",
                    arguments='{"index": "agent-logs-*"',
                ),
                # Never dispatched — loop gate.
                _tool(
                    "read_file",
                    success=False,
                    output=None,
                    error="blocked by loop gate: block_identity",
                    arguments={"path": "/etc/seshat/missing.yaml"},
                ),
            ],
        ),
        _capture("ac8-failed", 2, user="try another path", assistant="That one does not exist."),
    ]

    long_response = [
        _capture(
            "ac8-long",
            1,
            user="run the reindex and report",
            assistant=long_answer,
            tools=[
                _tool(
                    "query_elasticsearch",
                    output={"acknowledged": True, "shards_moved": 12},
                    arguments={"action": "reindex", "index": "agent-logs-2026.07"},
                )
            ],
        ),
        _capture("ac8-long", 2, user="how long did it take?", assistant="About four minutes."),
    ]

    return {
        "set": "ac8_input_completeness",
        "criterion": "AC-8",
        "synthetic": True,
        "cases": [
            {"case_id": "multi_result_turn", "captures": multi_result},
            {"case_id": "failed_and_undispatched_calls", "captures": failed_calls},
            {"case_id": "long_assistant_response", "captures": long_response},
        ],
    }


# ==========================================================================
# AC-9 — tool-only facts survive into the digest
# ==========================================================================


def ac9() -> dict[str, Any]:
    """Five sessions, one per tool, each with one decision-relevant tool-only fact.

    The discriminating property: the fact appears ONLY in tool output and is never
    restated in the assistant text, so a narration-only producer — which is what the
    pre-FRE-947 producer structurally was — cannot reproduce any of them.
    """
    cases = []

    # 1. query_elasticsearch — the retention window that decides the migration.
    cases.append(
        {
            "case_id": "es_retention_window",
            "tool": "query_elasticsearch",
            "expected_fact": "retention is 7 days, not the assumed 30",
            "expected_locator": {"capture_id": "ac9-es-t1", "field": "tool_result[0].output"},
            "captures": [
                _capture(
                    "ac9-es",
                    1,
                    user="can we migrate the log index next month?",
                    # Note: the assistant never states the 7-day number.
                    assistant="I checked the current ILM policy. There is a constraint we "
                    "should work around before scheduling the migration.",
                    tools=[
                        _tool(
                            "query_elasticsearch",
                            output='{"policy": "agent-logs", "delete": {"min_age": "7d"}}',
                            arguments={"index": "agent-logs-*", "query": "ilm/policy"},
                        )
                    ],
                ),
                _capture(
                    "ac9-es",
                    2,
                    user="so should we schedule it?",
                    assistant="Let's defer until the constraint is resolved.",
                ),
            ],
        }
    )

    # 2. read_file — the pinned version that blocks the upgrade.
    cases.append(
        {
            "case_id": "pinned_dependency_version",
            "tool": "read_file",
            "expected_fact": "neo4j driver is pinned to 5.26",
            "expected_locator": {"capture_id": "ac9-file-t1", "field": "tool_result[0].output"},
            "captures": [
                _capture(
                    "ac9-file",
                    1,
                    user="can we upgrade to the new driver?",
                    assistant="I read the lockfile. There is a pin that decides this.",
                    tools=[
                        _tool(
                            "read_file",
                            output='[[package]]\nname = "neo4j"\nversion = "5.26.0"\n',
                            arguments={"path": "uv.lock"},
                        )
                    ],
                ),
                _capture(
                    "ac9-file",
                    2,
                    user="what should we do?",
                    assistant="Unpin it first, then upgrade in a separate change.",
                ),
            ],
        }
    )

    # 3. web_search — the deprecation date that sets the deadline.
    cases.append(
        {
            "case_id": "api_deprecation_date",
            "tool": "web_search",
            "expected_fact": "the v1 endpoint is removed on 2026-11-01",
            "expected_locator": {"capture_id": "ac9-web-t1", "field": "tool_result[0].output"},
            "captures": [
                _capture(
                    "ac9-web",
                    1,
                    user="is the v1 API going away?",
                    assistant="Yes, there is a published removal date we need to plan around.",
                    tools=[
                        _tool(
                            "web_search",
                            output="Changelog: the v1 completions endpoint will be removed "
                            "on 2026-11-01. Migrate to v2 before that date.",
                            arguments={"query": "v1 completions endpoint removal date"},
                        )
                    ],
                ),
                _capture(
                    "ac9-web",
                    2,
                    user="do we have time?",
                    assistant="Enough, if we start the migration this quarter.",
                ),
            ],
        }
    )

    # 4. search_memory — the prior decision that makes this a re-litigation.
    cases.append(
        {
            "case_id": "prior_rejected_approach",
            "tool": "search_memory",
            "expected_fact": "sharding by date was already rejected for skew",
            "expected_locator": {"capture_id": "ac9-mem-t1", "field": "tool_result[0].output"},
            "captures": [
                _capture(
                    "ac9-mem",
                    1,
                    user="should we shard the index by date?",
                    assistant="We have discussed this before. Let me check what was decided.",
                    tools=[
                        _tool(
                            "search_memory",
                            output="Session 2026-06-14: sharding by date was rejected because "
                            "write skew concentrated on the newest shard.",
                            arguments={"query": "index sharding strategy"},
                        )
                    ],
                ),
                _capture(
                    "ac9-mem",
                    2,
                    user="ok, what instead?",
                    assistant="Shard by hash of the trace id.",
                ),
            ],
        }
    )

    # 5. system_metrics_snapshot — the headroom that decides the batch size.
    cases.append(
        {
            "case_id": "memory_headroom",
            "tool": "system_metrics_snapshot",
            "expected_fact": "only 1.2 GiB of RAM is free",
            "expected_locator": {"capture_id": "ac9-metrics-t1", "field": "tool_result[0].output"},
            "captures": [
                _capture(
                    "ac9-metrics",
                    1,
                    user="can we run the backfill with a large batch?",
                    assistant="I checked the host. Available memory is the binding constraint.",
                    tools=[
                        _tool(
                            "system_metrics_snapshot",
                            output='{"mem_total_gib": 10.0, "mem_available_gib": 1.2, '
                            '"cpu_count": 8}',
                            arguments={},
                        )
                    ],
                ),
                _capture(
                    "ac9-metrics",
                    2,
                    user="so what batch size?",
                    assistant="Keep it small and stream the results.",
                ),
            ],
        }
    )

    return {
        "set": "ac9_tool_only_facts",
        "criterion": "AC-9",
        "synthetic": True,
        "threshold": "the digest reproduces the expected fact in ALL cases",
        "cases": cases,
    }


# ==========================================================================
# AC-10 — basis tagging discriminates
# ==========================================================================


def ac10() -> dict[str, Any]:
    """40 labelled items across 8 sessions, balanced 10 per basis value.

    The balance is what makes the anti-collapse threshold meaningful: with a flat
    ground truth, a producer that tags everything `mixed` cannot claim it was merely
    matching a skewed truth.
    """
    cases = []
    specs = [
        # (basis, user text, assistant text, tool output or None, the item's content)
        (
            "tool_evidence",
            "check {thing}",
            "I looked it up and reported below.",
            '{{"{key}": "{value}"}}',
            "{key} is {value}",
        ),
        (
            "user_statement",
            "note that {thing}",
            "Understood, I will keep that in mind.",
            None,
            "the user stated {thing}",
        ),
        (
            "assistant_reasoning",
            "what do you think about {thing}?",
            "Reasoning from the structure alone, {thing} implies a trade-off.",
            None,
            "the assistant inferred a trade-off in {thing}",
        ),
        (
            "mixed",
            "given {thing}, what should we do?",
            "Combining what you said with the lookup, the answer follows.",
            '{{"{key}": "{value}"}}',
            "{thing} combined with the measurement",
        ),
    ]
    topics = [
        ("shard allocation", "unassigned_shards", "4"),
        ("retention policy", "min_age", "7d"),
        ("driver version", "version", "5.26.0"),
        ("memory headroom", "mem_available_gib", "1.2"),
        ("index size", "docs_count", "2276"),
        ("reranker latency", "p95_ms", "310"),
        ("embedding width", "dimensions", "1024"),
        ("budget cap", "daily_cap_usd", "2.50"),
        ("cache hit rate", "hit_ratio", "0.62"),
        ("consolidation lag", "lag_seconds", "45"),
    ]

    items = []
    for i, (topic, key, value) in enumerate(topics):
        for basis, u, a, out, content in specs:
            items.append(
                {
                    "basis": basis,
                    "user": u.format(thing=topic),
                    "assistant": a.format(thing=topic),
                    "output": out.format(key=key, value=value) if out else None,
                    "content": content.format(thing=topic, key=key, value=value),
                }
            )

    # 8 sessions x 5 items, so every session mixes basis values and no session can be
    # answered by tagging uniformly.
    for s in range(8):
        chunk = items[s * 5 : (s + 1) * 5]
        session_id = f"ac10-s{s}"
        captures = []
        labelled = []
        for j, item in enumerate(chunk, start=1):
            tools = (
                [_tool("query_elasticsearch", output=item["output"], arguments={"q": item["user"]})]
                if item["output"]
                else []
            )
            captures.append(
                _capture(
                    session_id,
                    j,
                    user=item["user"],
                    assistant=item["assistant"],
                    tools=tools,
                )
            )
            labelled.append({"content": item["content"], "true_basis": item["basis"]})
        cases.append({"case_id": session_id, "captures": captures, "labelled_items": labelled})

    return {
        "set": "ac10_basis_labelling",
        "criterion": "AC-10",
        "synthetic": True,
        "threshold": "agreement >= 85%; no single emitted tag > 60% of emissions",
        "label_distribution": {b: 10 for b, *_ in specs},
        "cases": cases,
    }


# ==========================================================================
# AC-12 — corrections fire when they should, stay silent when they should not
# ==========================================================================


def _tier_a(case_id: str, claim: str, evidence: str, topic: str) -> dict[str, Any]:
    """A direct contradiction: evidence contradicts the SAME proposition asserted."""
    return {
        "case_id": case_id,
        "expected": "correction",
        "tier": "A",
        "captures": [
            _capture(
                f"ac12-{case_id}",
                1,
                user=f"check {topic}",
                assistant=claim,
                tools=[_tool("query_elasticsearch", output=evidence, arguments={"q": topic})],
            ),
            _capture(f"ac12-{case_id}", 2, user="anything else?", assistant="That is all for now."),
        ],
    }


def _tier_b(case_id: str, wrong: str, correction: str, evidence: str, topic: str) -> dict[str, Any]:
    """An evidenced self-correction: the agent fixed the record, evidence supports it."""
    return {
        "case_id": case_id,
        "expected": "correction",
        "tier": "B",
        "captures": [
            _capture(f"ac12-{case_id}", 1, user=f"what is {topic}?", assistant=wrong),
            _capture(
                f"ac12-{case_id}",
                2,
                user="are you sure?",
                assistant=correction,
                tools=[_tool("read_file", output=evidence, arguments={"path": f"/etc/{topic}"})],
            ),
        ],
    }


def _tier_c(case_id: str, kind: str, captures: list[dict[str, Any]]) -> dict[str, Any]:
    """Not an error. Belongs in `unresolved`, or is omitted — never a correction."""
    return {
        "case_id": case_id,
        "expected": "no_correction",
        "tier_c_kind": kind,
        "captures": captures,
    }


def ac12() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    # --- 6 Tier-A: direct contradictions -----------------------------------
    cases += [
        _tier_a(
            "a1_cluster_status",
            "The cluster is green and every shard is assigned.",
            '{"status": "red", "unassigned_shards": 4}',
            "cluster health",
        ),
        _tier_a(
            "a2_doc_count",
            "The index holds roughly 10,000 documents.",
            '{"docs_count": 2276}',
            "index size",
        ),
        _tier_a(
            "a3_budget_state",
            "We are well under the daily cap with plenty of headroom.",
            '{"daily_cap_usd": 2.50, "spend_usd": 2.49, "denied": true}',
            "budget state",
        ),
        _tier_a(
            "a4_embedding_dims",
            "The embedder returns 768-dimensional vectors.",
            '{"model": "managed-8b", "dimensions": 1024}',
            "embedder config",
        ),
        _tier_a(
            "a5_command_success",
            "The migration command completed successfully.",
            '{"exit_code": 1, "stderr": "relation already exists"}',
            "migration run",
        ),
        _tier_a(
            "a6_retention",
            "Logs are retained for thirty days.",
            '{"policy": "agent-logs", "delete": {"min_age": "7d"}}',
            "retention policy",
        ),
    ]

    # --- 4 Tier-B: evidenced self-corrections ------------------------------
    cases += [
        _tier_b(
            "b1_port",
            "The gateway listens on port 9000.",
            "Correction: I was wrong, it listens on 9001 in this deployment.",
            "AGENT_SERVICE_PORT=9001",
            "gateway port",
        ),
        _tier_b(
            "b2_driver",
            "The Neo4j driver is on 5.28.",
            "I need to correct that — the lockfile pins 5.26.0.",
            'name = "neo4j"\nversion = "5.26.0"',
            "driver version",
        ),
        _tier_b(
            "b3_role",
            "Entity extraction runs on Sonnet.",
            "That was wrong. The role matrix binds it to gpt-5.4-mini.",
            "entity_extraction:  { all: gpt-5.4-mini }",
            "role binding",
        ),
        _tier_b(
            "b4_threshold",
            "The similarity floor is 0.5.",
            "Correcting myself: the configured floor is 0.35.",
            "AGENT_RECALL_SIMILARITY_FLOOR=0.35",
            "similarity floor",
        ),
    ]

    # --- 12 Tier-C negatives -----------------------------------------------
    # Each is something a careless producer would plausibly flag as an error.

    # 3 x weak / partial conflict
    cases.append(
        _tier_c(
            "c1_approximate_number",
            "weak_partial_conflict",
            [
                _capture(
                    "ac12-c1",
                    1,
                    user="how many turns?",
                    assistant="A bit over two thousand turns.",
                    tools=[_tool("query_elasticsearch", output='{"turns": 2276}', arguments={})],
                ),
                _capture("ac12-c1", 2, user="thanks", assistant="Anytime."),
            ],
        )
    )
    cases.append(
        _tier_c(
            "c2_rounded_latency",
            "weak_partial_conflict",
            [
                _capture(
                    "ac12-c2",
                    1,
                    user="reranker latency?",
                    assistant="Around 300 milliseconds at p95.",
                    tools=[_tool("query_elasticsearch", output='{"p95_ms": 310}', arguments={})],
                ),
                _capture("ac12-c2", 2, user="acceptable?", assistant="Yes, within budget."),
            ],
        )
    )
    cases.append(
        _tier_c(
            "c3_partial_scope",
            "weak_partial_conflict",
            [
                _capture(
                    "ac12-c3",
                    1,
                    user="are all indices healthy?",
                    assistant="The ones I checked are healthy.",
                    tools=[
                        _tool(
                            "query_elasticsearch",
                            output='{"checked": ["agent-logs"], "status": "green"}',
                            arguments={},
                        )
                    ],
                ),
                _capture("ac12-c3", 2, user="check the rest later", assistant="Will do."),
            ],
        )
    )

    # 3 x failed or incomplete tool calls
    cases.append(
        _tier_c(
            "c4_failed_call",
            "failed_or_incomplete_call",
            [
                _capture(
                    "ac12-c4",
                    1,
                    user="read the config",
                    assistant="I could not read it; the file is missing.",
                    tools=[
                        _tool(
                            "read_file",
                            success=False,
                            output=None,
                            error="ENOENT: /etc/missing.yaml",
                            arguments={"path": "/etc/missing.yaml"},
                        )
                    ],
                ),
                _capture("ac12-c4", 2, user="ok", assistant="Let me know the right path."),
            ],
        )
    )
    cases.append(
        _tier_c(
            "c5_timeout",
            "failed_or_incomplete_call",
            [
                _capture(
                    "ac12-c5",
                    1,
                    user="search the web",
                    assistant="The search timed out, so I have nothing to report.",
                    tools=[
                        _tool(
                            "web_search",
                            success=False,
                            output=None,
                            error="upstream timeout after 30s",
                            arguments={"query": "neo4j sharding"},
                        )
                    ],
                ),
                _capture("ac12-c5", 2, user="retry?", assistant="Worth one retry."),
            ],
        )
    )
    cases.append(
        _tier_c(
            "c6_empty_result",
            "failed_or_incomplete_call",
            [
                _capture(
                    "ac12-c6",
                    1,
                    user="find prior decisions on sharding",
                    assistant="Nothing came back, so I cannot say whether it was discussed.",
                    tools=[_tool("search_memory", output="[]", arguments={"query": "sharding"})],
                ),
                _capture("ac12-c6", 2, user="ok", assistant="We can decide fresh."),
            ],
        )
    )

    # 2 x ambiguous readings
    cases.append(
        _tier_c(
            "c7_ambiguous_unit",
            "ambiguous_reading",
            [
                _capture(
                    "ac12-c7",
                    1,
                    user="how big is the index?",
                    assistant="About two gigabytes.",
                    tools=[_tool("query_elasticsearch", output='{"size": 2048}', arguments={})],
                ),
                _capture("ac12-c7", 2, user="ok", assistant="Let me know if you need exact bytes."),
            ],
        )
    )
    cases.append(
        _tier_c(
            "c8_ambiguous_referent",
            "ambiguous_reading",
            [
                _capture(
                    "ac12-c8",
                    1,
                    user="is it enabled?",
                    assistant="Yes, that setting is on.",
                    tools=[
                        _tool(
                            "read_file",
                            output="reranker_enabled: true\nfusion_enabled: false",
                            arguments={"path": "config.yaml"},
                        )
                    ],
                ),
                _capture("ac12-c8", 2, user="good", assistant="Anything else?"),
            ],
        )
    )

    # 2 x legitimately changed state
    cases.append(
        _tier_c(
            "c9_state_changed",
            "legitimately_changed_state",
            [
                _capture(
                    "ac12-c9",
                    1,
                    user="cluster status?",
                    assistant="It is red with unassigned shards.",
                    tools=[_tool("query_elasticsearch", output='{"status": "red"}', arguments={})],
                ),
                _capture(
                    "ac12-c9",
                    2,
                    user="and now, after the reallocation?",
                    assistant="It has recovered to green.",
                    tools=[
                        _tool("query_elasticsearch", output='{"status": "green"}', arguments={})
                    ],
                ),
            ],
        )
    )
    cases.append(
        _tier_c(
            "c10_spend_grew",
            "legitimately_changed_state",
            [
                _capture(
                    "ac12-c10",
                    1,
                    user="spend so far?",
                    assistant="About one dollar.",
                    tools=[
                        _tool("query_elasticsearch", output='{"spend_usd": 1.02}', arguments={})
                    ],
                ),
                _capture(
                    "ac12-c10",
                    2,
                    user="and after the batch?",
                    assistant="It has risen to two dollars.",
                    tools=[
                        _tool("query_elasticsearch", output='{"spend_usd": 2.05}', arguments={})
                    ],
                ),
            ],
        )
    )

    # 2 x disagreement with a subjective judgment
    cases.append(
        _tier_c(
            "c11_subjective_recommendation",
            "subjective_disagreement",
            [
                _capture(
                    "ac12-c11",
                    1,
                    user="should we shard by date?",
                    assistant="I would not recommend it; hash sharding is cleaner here.",
                    tools=[
                        _tool(
                            "search_memory",
                            output="Date sharding is a common and widely used strategy.",
                            arguments={"query": "sharding"},
                        )
                    ],
                ),
                _capture("ac12-c11", 2, user="I disagree", assistant="That is a fair position."),
            ],
        )
    )
    cases.append(
        _tier_c(
            "c12_subjective_priority",
            "subjective_disagreement",
            [
                _capture(
                    "ac12-c12",
                    1,
                    user="is this urgent?",
                    assistant="I would treat it as low priority.",
                    tools=[
                        _tool(
                            "query_elasticsearch",
                            output='{"error_rate": 0.001, "severity_label": "high"}',
                            arguments={},
                        )
                    ],
                ),
                _capture("ac12-c12", 2, user="I think it is urgent", assistant="Then we escalate."),
            ],
        )
    )

    return {
        "set": "ac12_corrections",
        "criterion": "AC-12",
        "synthetic": True,
        "threshold": (
            "ZERO negatives yield a correction (precision absolute); >=80% of positives do; "
            "every Tier-B correction carries the located span of its supporting evidence"
        ),
        "composition": {"tier_a": 6, "tier_b": 4, "tier_c": 12},
        "cases": cases,
    }


# ==========================================================================
# AC-13 — missing evidence produces silence, not invention
# ==========================================================================


def ac13() -> dict[str, Any]:
    """The fixture triple. Both directions matter.

    A producer that invents contradictions from gaps fails case 1; one that goes mute
    whenever any evidence is missing fails cases 2 and 3.
    """
    return {
        "set": "ac13_missing_evidence",
        "criterion": "AC-13",
        "synthetic": True,
        "threshold": (
            "payload_absent yields NO correction; status_visible and self_correction each yield ONE"
        ),
        "cases": [
            {
                "case_id": "payload_absent",
                "expected": "no_correction",
                "why": (
                    "the only possible contradiction lives in a payload the capture does "
                    "not hold; absence of evidence is not evidence of absence"
                ),
                "captures": [
                    _capture(
                        "ac13-absent",
                        1,
                        user="check the cluster",
                        assistant="The cluster is green and all shards are assigned.",
                        tools=[
                            # Succeeded, but the payload was never stored.
                            _tool(
                                "query_elasticsearch",
                                success=True,
                                output=None,
                                arguments={"index": "agent-logs-*"},
                            )
                        ],
                    ),
                    _capture(
                        "ac13-absent",
                        2,
                        user="anything to worry about?",
                        assistant="Not that I saw.",
                    ),
                ],
            },
            {
                "case_id": "status_visible",
                "expected": "correction",
                "why": (
                    "a contradiction between 'the command succeeded' and a recorded error "
                    "status is Tier A on status alone, and must not be suppressed merely "
                    "because a payload is missing"
                ),
                "captures": [
                    _capture(
                        "ac13-status",
                        1,
                        user="run the migration",
                        assistant="The migration ran successfully with no errors.",
                        tools=[
                            _tool(
                                "read_file",
                                success=False,
                                output=None,
                                error="exit code 1: relation already exists",
                                arguments={"path": "migrations/003.sql"},
                            )
                        ],
                    ),
                    _capture("ac13-status", 2, user="so we are done?", assistant="We should be."),
                ],
            },
            {
                "case_id": "self_correction",
                "expected": "correction",
                "why": "Tier B needs only the session's own text plus supporting evidence",
                "captures": [
                    _capture(
                        "ac13-self",
                        1,
                        user="what port does the gateway use?",
                        assistant="It listens on port 9000.",
                    ),
                    _capture(
                        "ac13-self",
                        2,
                        user="are you certain?",
                        assistant="I was wrong — the deployment config sets it to 9001.",
                        tools=[
                            _tool(
                                "read_file",
                                output="AGENT_SERVICE_PORT=9001",
                                arguments={"path": ".env"},
                            )
                        ],
                    ),
                ],
            },
        ],
    }


if __name__ == "__main__":
    _write("ac8_input_completeness", ac8())
    _write("ac9_tool_only_facts", ac9())
    _write("ac10_basis_labelling", ac10())
    _write("ac12_corrections", ac12())
    _write("ac13_missing_evidence", ac13())
