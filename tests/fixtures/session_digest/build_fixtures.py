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


def _self_correction(
    case_id: str,
    *,
    t1_user: str,
    t1_assistant: str,
    t2_user: str,
    t2_assistant: str,
    span: str,
    evidence_span: str,
    evidence_field: tuple[str, str],
    tool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An evidenced self-correction (Amendment A).

    The agent fixed the record within the conversation, and the supporting evidence
    lives in a field the producer is GIVEN — a tool error or the conversation text —
    never a tool payload.

    ``reference_correction`` records a hand-authored citation that resolves; it is
    pre-validated offline (test_session_digest_validator.py) so an un-citable positive
    cannot silently become an ``errored`` case in the paid arm.
    """
    sid = f"ac12-{case_id}"
    return {
        "case_id": case_id,
        "expected": "correction",
        "tier": "self_correction",
        "reference_correction": {
            "span": span,
            "locator": {"capture_id": f"{sid}-t2", "field": "assistant_text"},
            "evidence_span": evidence_span,
            "evidence_locator": {"capture_id": evidence_field[0], "field": evidence_field[1]},
        },
        "captures": [
            _capture(sid, 1, user=t1_user, assistant=t1_assistant, tools=[tool] if tool else None),
            _capture(sid, 2, user=t2_user, assistant=t2_assistant),
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

    # --- 8 self-corrections (Amendment A) ----------------------------------
    # 4 backed by a visible tool error; the assistant asserted success, the tool's own
    # error line denies it, and the assistant self-corrects on the next turn.
    cases += [
        _self_correction(
            "b1_migration",
            t1_user="did the migration apply?",
            t1_assistant="Yes, the migration applied cleanly and the sessions table is ready.",
            t2_user="are you sure it succeeded?",
            t2_assistant="Correcting myself — it did not apply; the migration failed.",
            span="it did not apply; the migration failed",
            evidence_span="relation sessions already exists",
            evidence_field=("ac12-b1_migration-t1", "tool_result[0].error"),
            tool=_tool(
                "run_migration",
                success=False,
                error="exit code 1: relation sessions already exists",
                arguments={"path": "migrations/004.sql"},
            ),
        ),
        _self_correction(
            "b2_deploy",
            t1_user="did the deploy go out?",
            t1_assistant="The deploy went through and the new image is live.",
            t2_user="is it really live?",
            t2_assistant="I need to correct that — the deploy failed because the image was not found.",
            span="the deploy failed because the image was not found",
            evidence_span="manifest unknown: image not found",
            evidence_field=("ac12-b2_deploy-t1", "tool_result[0].error"),
            tool=_tool(
                "deploy_service",
                success=False,
                error="manifest unknown: image not found",
                arguments={"tag": "v2.3.1"},
            ),
        ),
        _self_correction(
            "b3_index",
            t1_user="create the index",
            t1_assistant="I created the index and it is ready to receive writes.",
            t2_user="did it create cleanly?",
            t2_assistant="That was wrong — the index already existed, so nothing was created.",
            span="the index already existed, so nothing was created",
            evidence_span="resource already exists exception: index agent-logs",
            evidence_field=("ac12-b3_index-t1", "tool_result[0].error"),
            tool=_tool(
                "create_index",
                success=False,
                error="resource already exists exception: index agent-logs",
                arguments={"index": "agent-logs"},
            ),
        ),
        _self_correction(
            "b4_write",
            t1_user="save the config",
            t1_assistant="The config file was written successfully.",
            t2_user="confirm it saved?",
            t2_assistant="Correcting myself: it did not save, permission was denied.",
            span="it did not save, permission was denied",
            evidence_span="EACCES permission denied opening /etc/app.conf",
            evidence_field=("ac12-b4_write-t1", "tool_result[0].error"),
            tool=_tool(
                "write_file",
                success=False,
                error="EACCES permission denied opening /etc/app.conf",
                arguments={"path": "/etc/app.conf"},
            ),
        ),
    ]

    # 4 backed by the conversation text; the user supplies the correcting fact and the
    # assistant self-corrects, evidence cited from user_text.
    cases += [
        _self_correction(
            "b5_port",
            t1_user="what port does the gateway use?",
            t1_assistant="The gateway listens on port 9000.",
            t2_user="The env file I am reading says AGENT_SERVICE_PORT=9001.",
            t2_assistant="You are right — I was wrong, it listens on 9001, not 9000.",
            span="I was wrong, it listens on 9001, not 9000",
            evidence_span="AGENT_SERVICE_PORT=9001",
            evidence_field=("ac12-b5_port-t2", "user_text"),
        ),
        _self_correction(
            "b6_driver",
            t1_user="what version is the neo4j driver?",
            t1_assistant="The Neo4j driver is pinned at 5.28.",
            t2_user="The lockfile shows neo4j version 5.26.0.",
            t2_assistant="Correcting myself — the driver is 5.26.0; I misremembered.",
            span="the driver is 5.26.0",
            evidence_span="neo4j version 5.26.0",
            evidence_field=("ac12-b6_driver-t2", "user_text"),
        ),
        _self_correction(
            "b7_floor",
            t1_user="what is the similarity floor?",
            t1_assistant="The similarity floor is 0.5.",
            t2_user="My notes say the configured floor is 0.35.",
            t2_assistant="You are correct — I was wrong, the floor is 0.35.",
            span="I was wrong, the floor is 0.35",
            evidence_span="the configured floor is 0.35",
            evidence_field=("ac12-b7_floor-t2", "user_text"),
        ),
        _self_correction(
            "b8_role",
            t1_user="what model does entity extraction use?",
            t1_assistant="Entity extraction runs on Sonnet.",
            t2_user="The role matrix binds entity_extraction to gpt-5.4-mini.",
            t2_assistant="That was my mistake — entity extraction runs on gpt-5.4-mini, not Sonnet.",
            span="entity extraction runs on gpt-5.4-mini, not Sonnet",
            evidence_span="binds entity_extraction to gpt-5.4-mini",
            evidence_field=("ac12-b8_role-t2", "user_text"),
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
        "amendment": "A — Tier-A payload contradictions removed; positives are self-corrections only",
        "threshold": (
            "ZERO negatives yield a correction (precision absolute); >=80% of positives do; "
            "every self_correction carries the located span of its supporting evidence"
        ),
        "composition": {"self_correction": 8, "tier_c_negative": 12},
        "note": (
            "Every positive's supporting evidence lives in a field the producer is actually given "
            "(a tool error or the conversation text) — never a tool payload, which Amendment A no "
            "longer feeds. reference_correction records a hand-authored citation that resolves; "
            "tests/personal_agent/memory/test_session_digest_validator.py asserts each resolves "
            "before the paid arm runs."
        ),
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
    _write("ac10_basis_labelling", ac10())
    _write("ac12_corrections", ac12())
    _write("ac13_missing_evidence", ac13())
