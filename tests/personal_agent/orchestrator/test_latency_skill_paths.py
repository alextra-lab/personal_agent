"""The latency skills send a latency question to a path that holds data (FRE-1568).

AC-1: ``query-elasticsearch.md`` recommends no retired latency field.
AC-2 (offline half): the ``query-tempo.md`` recipe passes the bash allowlist, and its ``jq``
    step reduces a Tempo reply to exact percentiles and says when the reply is capped.
    The live half (a real Tempo query) is run by master, not here.

No test talks to Tempo or Elasticsearch (tests/CLAUDE.md, FRE-375).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.tools.primitives.bash_allowlist import check_segment_allowlist

_SKILLS = Path(__file__).resolve().parents[3] / "docs" / "skills"
_ELASTICSEARCH = (_SKILLS / "query-elasticsearch.md").read_text(encoding="utf-8")
_TEMPO = (_SKILLS / "query-tempo.md").read_text(encoding="utf-8")
_NORMAL = load_governance_config().tools["bash"].auto_approve_prefixes["NORMAL"]

_RETIRED_FIELDS = ("duration_ms", "latency_ms", "elapsed_ms", "elapsed_s", "response_time_ms")
_RECIPE_HEADING = "### Pattern 5: Model-call latency by role and model"
_FENCE = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def _retired_field_lines(text: str) -> list[str]:
    """Return the lines that name a retired latency field without a 'retired' note."""
    return [
        line
        for line in text.splitlines()
        if any(field in line for field in _RETIRED_FIELDS) and "retired" not in line.lower()
    ]


def _recipe_block() -> str:
    """Return the bash block under the model-call latency heading of ``query-tempo.md``."""
    start = _TEMPO.index(_RECIPE_HEADING)
    ends = [i for i in (_TEMPO.find("\n### ", start + 1), _TEMPO.find("\n## ", start + 1)) if i != -1]
    section = _TEMPO[start : min(ends) if ends else len(_TEMPO)]
    blocks = _FENCE.findall(section)
    assert len(blocks) == 1, "the recipe section holds exactly one bash block"
    return blocks[0]


def _recipe_jq_program(block: str) -> str:
    match = re.search(r"\|\s*jq\s+--argjson limit \d+\s+'(.*)'\s*$", block, re.DOTALL)
    assert match is not None, "the recipe ends with: | jq --argjson limit N '<program>'"
    return match.group(1)


def _frontmatter_keywords(text: str) -> list[str]:
    block = re.search(r"^keywords:\n((?:  .*\n)+)", text, re.MULTILINE)
    assert block is not None
    return [
        line.strip()[2:].strip().strip('"')
        for line in block.group(1).splitlines()
        if line.strip().startswith("- ")
    ]


# --------------------------------------------------------------------------- AC-1


def test_elasticsearch_skill_names_no_retired_latency_field() -> None:
    assert _retired_field_lines(_ELASTICSEARCH) == []


def test_checker_flags_a_retired_field_line() -> None:
    """Seeded negative: the line that the skill held before FRE-1568 is caught."""
    old_line = "| `elapsed_ms` / `duration_ms` | long | Elapsed time in milliseconds |"
    assert _retired_field_lines(old_line) == [old_line]
    assert _retired_field_lines("`duration_ms` is retired (ADR-0129).") == []


def test_elasticsearch_skill_sends_latency_to_tempo() -> None:
    assert "query-tempo" in _ELASTICSEARCH
    words = _frontmatter_keywords(_ELASTICSEARCH)
    assert "latency" not in words
    assert "p95" not in words


def test_tempo_skill_keeps_the_latency_keyword() -> None:
    assert re.search(r"^  - latency$", _TEMPO, re.MULTILINE)


# --------------------------------------------------------------------------- scope 4


def test_tempo_recipe_is_auto_approved() -> None:
    """No ``bash_allowlist_miss``: the real NORMAL allowlist accepts the whole recipe."""
    assert check_segment_allowlist(_recipe_block(), _NORMAL) is None


def test_allowlist_check_rejects_the_old_tempo_forms() -> None:
    """Seeded negative: the two forms the skill held before FRE-1568 need approval."""
    docker_exec = "docker exec cloud-sim-seshat-gateway curl -s 'http://tempo:3200/status'"
    substitution = "start=$(date -d '1 hour ago' +%s)"
    assert check_segment_allowlist(docker_exec, _NORMAL) is not None
    assert check_segment_allowlist(substitution, _NORMAL) is not None


def test_every_tempo_skill_bash_block_is_auto_approved() -> None:
    """Every command the skill shows passes the real NORMAL allowlist (no approval prompt)."""
    blocks = _FENCE.findall(_TEMPO)
    assert blocks
    for block in blocks:
        assert check_segment_allowlist(block, _NORMAL) is None, block


def test_tempo_recipe_names_both_caps_and_they_agree() -> None:
    block = _recipe_block()
    curl_limit = re.search(r"--data-urlencode 'limit=(\d+)'", block)
    jq_limit = re.search(r"--argjson limit (\d+)", block)
    assert curl_limit is not None and jq_limit is not None
    assert curl_limit.group(1) == jq_limit.group(1)
    assert re.search(r"--data-urlencode 'spss=\d+'", block)
    assert "gen_ai.operation.name" in block
    assert "gen_ai.request.model" in block


def test_latency_question_routes_to_tempo_before_self_telemetry() -> None:
    """The AC-4 sentence and a bare "p95 latency" rank query-tempo first (skills.py routing)."""
    docs = {
        name: _frontmatter_keywords((_SKILLS / f"{name}.md").read_text(encoding="utf-8"))
        for name in ("query-tempo", "query-elasticsearch", "self-telemetry")
    }
    for message in (
        "Analyze the model call latencies over the last 24 hours. "
        "Compare p50 and p90 by model role.",
        "show p95 latency",
    ):
        lowered = message.lower()
        hits = {n: sum(1 for kw in kws if kw.lower() in lowered) for n, kws in docs.items()}
        # Ties keep file order, and "query-tempo" sorts before "self-telemetry".
        assert hits["query-tempo"] >= hits["self-telemetry"], (message, hits)
        assert hits["query-tempo"] > hits["query-elasticsearch"], (message, hits)


# --------------------------------------------------------------------------- AC-2


def _span(role: str, model: str, millis: int, span_id: str) -> dict[str, Any]:
    return {
        "spanID": span_id,
        "durationNanos": str(millis * 1_000_000),
        "attributes": [
            {"key": "gen_ai.operation.name", "value": {"stringValue": role}},
            {"key": "gen_ai.request.model", "value": {"stringValue": model}},
        ],
    }


def _require_jq() -> None:
    """Skip on a workstation without jq. Fail on CI, so a missing jq is never a silent pass."""
    if shutil.which("jq") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail("jq is not installed on this CI runner: the recipe tests cannot run")
    pytest.skip("jq is not installed")


def _run_recipe_jq(reply: dict[str, Any]) -> dict[str, Any]:
    _require_jq()
    block = _recipe_block()
    limit = re.search(r"--argjson limit (\d+)", block)
    assert limit is not None
    done = subprocess.run(  # noqa: S603 - fixed argv, test input only
        ["jq", "--argjson", "limit", limit.group(1), _recipe_jq_program(block)],  # noqa: S607
        input=json.dumps(reply),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def _reply() -> dict[str, Any]:
    primary = [
        _span("primary", "m1", ms, f"p{i}") for i, ms in enumerate((100, 200, 300, 400, 1000))
    ]
    sub = [_span("sub_agent", "m2", ms, f"s{i}") for i, ms in enumerate((50, 150))]
    return {
        "traces": [
            # a one-set trace uses "spanSet" ...
            {"traceID": "a", "spanSet": {"matched": 5, "spans": primary}},
            # ... a multi-set trace uses "spanSets"
            {"traceID": "b", "spanSets": [{"matched": 2, "spans": sub}]},
        ],
        "metrics": {"completedJobs": 3, "totalJobs": 3},
    }


def test_tempo_recipe_jq_percentiles_per_role_and_model() -> None:
    out = _run_recipe_jq(_reply())
    groups = {(g["role"], g["model"]): g for g in out["groups"]}
    assert set(groups) == {("primary", "m1"), ("sub_agent", "m2")}
    primary = groups[("primary", "m1")]
    assert (primary["spans"], primary["p50_ms"], primary["p90_ms"], primary["max_ms"]) == (
        5,
        300,
        1000,
        1000,
    )
    sub = groups[("sub_agent", "m2")]
    assert (sub["spans"], sub["p50_ms"], sub["p90_ms"], sub["max_ms"]) == (2, 50, 150, 150)
    assert out["complete"] is True
    assert out["traces_returned"] == 2
    assert out["spans_cut"] == 0


def test_tempo_recipe_jq_says_incomplete_when_spans_were_cut() -> None:
    reply = _reply()
    reply["traces"][0]["spanSet"]["matched"] = 120  # Tempo matched 120, returned 5
    out = _run_recipe_jq(reply)
    assert out["complete"] is False
    assert out["spans_cut"] == 115


def test_tempo_recipe_jq_says_incomplete_when_the_trace_limit_is_reached() -> None:
    block = _recipe_block()
    limit = int(re.search(r"--argjson limit (\d+)", block).group(1))  # type: ignore[union-attr]
    traces = [
        {
            "traceID": str(i),
            "spanSet": {"matched": 1, "spans": [_span("primary", "m1", 10, str(i))]},
        }
        for i in range(limit)
    ]
    out = _run_recipe_jq({"traces": traces})
    assert out["traces_returned"] == limit
    assert out["complete"] is False


def test_tempo_recipe_jq_handles_an_empty_reply() -> None:
    out = _run_recipe_jq({"traces": []})
    assert out["groups"] == []
    assert out["traces_returned"] == 0
    # An empty reply is complete but holds no spans: the recipe's prose says to read it as "no
    # matching spans in this window", never as zero latency.
    assert out["complete"] is True


def test_tempo_recipe_jq_says_incomplete_when_blocks_were_not_read() -> None:
    reply = _reply()
    reply["metrics"] = {"completedJobs": 2, "totalJobs": 3}  # Tempo stopped early
    out = _run_recipe_jq(reply)
    assert out["complete"] is False
    assert out["blocks_unread"] == 1


def test_tempo_recipe_jq_reads_job_counts_written_as_strings() -> None:
    reply = _reply()
    reply["metrics"] = {"completedJobs": "3", "totalJobs": "3"}
    assert _run_recipe_jq(reply)["complete"] is True


def test_tempo_recipe_jq_says_incomplete_when_a_set_has_no_match_count() -> None:
    reply = _reply()
    del reply["traces"][0]["spanSet"]["matched"]
    out = _run_recipe_jq(reply)
    assert out["complete"] is False
    assert out["sets_without_match"] == 1


def test_tempo_recipe_jq_skips_a_span_with_no_duration_and_says_so() -> None:
    reply = _reply()
    del reply["traces"][0]["spanSet"]["spans"][0]["durationNanos"]
    out = _run_recipe_jq(reply)
    assert out["complete"] is False
    assert out["spans_without_duration"] == 1
    primary = {(g["role"], g["model"]): g for g in out["groups"]}[("primary", "m1")]
    assert primary["spans"] == 4  # the span with no duration is not counted


def test_tempo_recipe_jq_says_incomplete_when_a_trace_has_no_span_set() -> None:
    reply = _reply()
    reply["traces"].append({"traceID": "c"})  # neither "spanSet" nor "spanSets"
    out = _run_recipe_jq(reply)
    assert out["complete"] is False
    assert out["traces_without_span_set"] == 1


def test_tempo_recipe_jq_reads_an_omitted_completed_jobs_as_zero() -> None:
    """Proto JSON leaves out a zero: totalJobs alone means no block was read."""
    reply = _reply()
    reply["metrics"] = {"totalJobs": 3}
    out = _run_recipe_jq(reply)
    assert out["complete"] is False
    assert out["blocks_unread"] == 3


def test_every_jq_program_in_the_tempo_skill_compiles() -> None:
    """jq exits 3 on a compile error. A runtime error on null input is not one."""
    _require_jq()
    programs = 0
    for block in _FENCE.findall(_TEMPO):
        for program in re.findall(r"\bjq\s+(?:--argjson limit \d+\s+)?'([^']*)'", block):
            done = subprocess.run(  # noqa: S603 - fixed argv, test input only
                ["jq", "-n", "--argjson", "limit", "1", program],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            assert done.returncode != 3, f"jq compile error: {done.stderr}\n{program}"
            programs += 1
    assert programs >= 5  # patterns 1, 2, 3, 4 and 5 each hold at least one program
