"""ADR-0156 D5 (FRE-1578 AC-1, AC-2, AC-3): the 8 telemetry and host skills are converted.

The T5 validator (``test_skill_validator.py``) checks the S1 rules. This file checks what the
conversion adds on top of them:

- every ``evals.yaml`` names its skill and gives scenarios that each hold a prompt and the
  expected result (D5.2), and no prompt is shared by two skills;
- no skill names a retired field or index outside a line that says "retired" (AC-3);
- the four telemetry skills show a ``query_telemetry`` call before their first curl recipe
  (D5.5), and every call that they show is one the tool's own validator accepts (AC-2).
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path

import pytest
import yaml

from personal_agent.tools.telemetry_query import _build_request, _build_tempo_request

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_CONVERTED = (
    "infrastructure-health",
    "neo4j-direct",
    "query-elasticsearch",
    "query-tempo",
    "self-telemetry",
    "system-diagnostics",
    "system-metrics",
    "turn-forensics",
)
_LEAD_WITH_THE_TOOL = ("query-elasticsearch", "query-tempo", "self-telemetry", "turn-forensics")

# --- AC-1: evals.yaml -----------------------------------------------------------------------


def _scenarios(name: str) -> list[dict[str, object]]:
    loaded = yaml.safe_load((_SKILLS / name / "evals.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{name}: evals.yaml is not a mapping"
    assert loaded.get("skill") == name, f"{name}: 'skill' must name the folder"
    found = loaded.get("scenarios")
    assert isinstance(found, list), f"{name}: 'scenarios' is not a list"
    return found


@pytest.mark.parametrize("name", _CONVERTED)
def test_each_scenario_holds_a_prompt_and_the_expected_result(name: str) -> None:
    scenarios = _scenarios(name)
    assert len(scenarios) >= 3
    for index, scenario in enumerate(scenarios):
        for key in ("prompt", "expected_skill", "expect"):
            value = scenario.get(key) if isinstance(scenario, dict) else None
            assert isinstance(value, str) and value.strip(), f"{name}[{index}] has no {key}"
        assert scenario["expected_skill"] == name, f"{name}[{index}] expects another skill"


def test_no_prompt_is_shared_by_two_skills() -> None:
    owners: dict[str, str] = {}
    for name in _CONVERTED:
        for scenario in _scenarios(name):
            prompt = str(scenario["prompt"]).strip().lower()
            assert prompt not in owners, f"{name} repeats a prompt of {owners.get(prompt)}"
            owners[prompt] = name


# --- AC-3: no retired name outside a "retired" line -----------------------------------------

# FRE-1568: the model-call timing fields that no event writes since ADR-0129, and the index
# names that never existed. A name inside a longer field (probe_latency_ms) is not retired.
_RETIRED = re.compile(
    r"(?<![\w.])(?:duration_ms|latency_ms|elapsed_ms|elapsed_s|response_time_ms)(?!\w)"
    r"|(?<![\w-])(?:agent-events|agent-traces|agent-telemetry)-\*"
    r"|(?<![\w-])logs-\*"
)


def _retired_lines(text: str) -> list[str]:
    """Return the sentences that name a retired field or index and do not say "retired".

    A line holds one sentence or more. The check is per sentence, so a retired note does not
    cover a second sentence on the same line that recommends a retired name.
    """
    return [
        sentence
        for line in text.splitlines()
        for sentence in re.split(r"(?<=[.;])\s+", line)
        if _RETIRED.search(sentence) and "retired" not in sentence.lower()
    ]


def _skill_texts() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name in _CONVERTED:
        for path in sorted((_SKILLS / name).rglob("*")):
            if path.is_file() and path.suffix in {".md", ".py", ".yaml"}:
                found.append((f"{name}/{path.relative_to(_SKILLS / name)}", path.read_text()))
    return found


@pytest.mark.parametrize(("where", "text"), _skill_texts(), ids=[w for w, _ in _skill_texts()])
def test_no_retired_name_without_a_retired_note(where: str, text: str) -> None:
    assert _retired_lines(text) == [], where


@pytest.mark.parametrize(
    "line",
    [
        "| `elapsed_ms` / `duration_ms` | long | Elapsed time |",
        "Group by `latency_ms` to see the spread.",
        "FROM agent-events-* | STATS count=COUNT(*)",
        "curl 'http://elasticsearch:9200/logs-*/_search'",
    ],
)
def test_the_checker_flags_a_retired_name(line: str) -> None:
    """Seeded negatives: each form that an old skill held is caught."""
    assert _retired_lines(line) == [line]


@pytest.mark.parametrize(
    "line",
    [
        "`duration_ms` is retired from agent-logs (ADR-0129).",
        "`probe_latency_ms` and `max_latency_ms` are allowed fields.",
        "Read `agent-logs-*` for events.",
        "`latency_total_ms` is the whole-turn time in agent-topology.",
    ],
)
def test_the_checker_passes_a_retired_note_and_a_longer_name(line: str) -> None:
    assert _retired_lines(line) == []


def test_a_retired_note_does_not_cover_the_next_sentence() -> None:
    """Seeded negative: a note on one name must not excuse another sentence on the line."""
    line = "`duration_ms` is retired. Group by `latency_ms` instead."
    assert _retired_lines(line) == ["Group by `latency_ms` instead."]


# --- AC-4: distinct "Use when" clauses ------------------------------------------------------


def _use_when(name: str) -> str:
    text = (_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    description = yaml.safe_load(text.split("---", 2)[1])["description"]
    sentences = re.split(r"(?<=[.])\s+", " ".join(str(description).split()))
    clauses = [s for s in sentences if s.startswith("Use when")]
    assert len(clauses) == 1, f"{name}: the description needs exactly one 'Use when' sentence"
    return clauses[0].lower()


def test_no_two_descriptions_share_a_use_when_clause() -> None:
    clauses = {name: _use_when(name) for name in _CONVERTED}
    assert len(set(clauses.values())) == len(_CONVERTED), clauses


# --- paths that the validator does not read -------------------------------------------------

_SCRIPT_PATH = re.compile(r"skills/([a-z0-9-]+)/((?:scripts|references)/[\w.-]*\w)")
_OTHER_SKILL_PATH = re.compile(r"(?<![\w/-])([a-z0-9-]+)/(references/[\w.-]*\w)")


def test_every_path_to_a_skill_file_exists() -> None:
    """The validator reads only ``scripts/x`` in the skill's own folder (skills.py:363).

    A repository path (``skills/<name>/scripts/x``) or a file of another skill
    (``<name>/references/x``) escapes it, so a typo would pass. This test reads them.
    """
    missing: list[str] = []
    for where, text in _skill_texts():
        if not where.endswith(".md"):
            continue
        # A path that starts with skills/ names its folder outright: a missing folder is a
        # typo. The other-skill form (<name>/references/x) can match ordinary prose, so it is
        # read only when its first part is a real skill folder.
        for skill, relative in _SCRIPT_PATH.findall(text):
            if not (_SKILLS / skill / relative).is_file():
                missing.append(f"{where}: {skill}/{relative}")
        for skill, relative in _OTHER_SKILL_PATH.findall(text):
            if (_SKILLS / skill).is_dir() and not (_SKILLS / skill / relative).is_file():
                missing.append(f"{where}: {skill}/{relative}")
    assert missing == []


def test_the_path_check_reads_both_spellings() -> None:
    text = "Run skills/neo4j-direct/scripts/x.py or read self-telemetry/references/y.md."
    assert _SCRIPT_PATH.findall(text) == [("neo4j-direct", "scripts/x.py")]
    assert _OTHER_SKILL_PATH.findall(text) == [("self-telemetry", "references/y.md")]


def test_a_mistyped_skill_folder_in_a_repository_path_is_caught(tmp_path: Path) -> None:
    """Seeded negative: a typo in the folder of a skills/ path must not pass."""
    text = "Run skills/neo4j-dirct/scripts/cypher.py now."
    found = _SCRIPT_PATH.findall(text)
    assert found == [("neo4j-dirct", "scripts/cypher.py")]
    assert not (_SKILLS / found[0][0] / found[0][1]).is_file()


# --- D5.5 and AC-2: the telemetry skills lead with query_telemetry --------------------------

_CALL_START = re.compile(r"query_telemetry\(")
_FIRST_CURL_FENCE = re.compile(r"```(?:bash|sh|shell)\n(?:(?!```).)*?\bcurl\b", re.DOTALL)


def _calls(text: str) -> list[str]:
    """Return each ``query_telemetry(...)`` call in ``text``, with balanced brackets."""
    found: list[str] = []
    for start in _CALL_START.finditer(text):
        depth, quote = 0, ""
        for end in range(start.end() - 1, len(text)):
            char = text[end]
            if quote:
                quote = "" if char == quote and text[end - 1] != "\\" else quote
            elif char in "\"'":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    found.append(text[start.start() : end + 1])
                    break
    return found


def _check_call(call: str) -> None:
    """Raise if the ``query_telemetry`` tool would refuse ``call`` before it sends anything."""
    node = ast.parse(call, mode="eval").body
    assert isinstance(node, ast.Call)
    args = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
    if args["action"] == "latency":
        _build_tempo_request(
            args.get("since"),
            args.get("span"),
            args.get("group_by"),
            args.get("role"),
            args.get("model"),
            args.get("limit"),
            now=time.time(),
        )
    else:
        _build_request(
            args["action"],
            args["index"],
            args.get("query"),
            args.get("aggs"),
            args.get("fields"),
            args.get("size"),
            args.get("since"),
        )


def _all_calls() -> list[tuple[str, str]]:
    return [(where, call) for where, text in _skill_texts() for call in _calls(text)]


@pytest.mark.parametrize("name", _LEAD_WITH_THE_TOOL)
def test_the_skill_shows_the_tool_before_its_first_curl(name: str) -> None:
    body = (_SKILLS / name / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[2]
    first_call = _CALL_START.search(body)
    first_curl = _FIRST_CURL_FENCE.search(body)
    assert first_call is not None, f"{name} shows no query_telemetry call"
    assert first_curl is None or first_call.start() < first_curl.start()


def test_the_skills_show_query_telemetry_calls() -> None:
    assert len(_all_calls()) >= 15


@pytest.mark.parametrize(
    ("where", "call"), _all_calls(), ids=[f"{w}:{i}" for i, (w, _) in enumerate(_all_calls())]
)
def test_a_shown_call_is_one_the_tool_accepts(where: str, call: str) -> None:
    _check_call(call)


def test_the_tool_refuses_a_field_that_does_not_exist() -> None:
    """Seeded negative: the call that a primary once made through curl, where it passed."""
    call = (
        'query_telemetry(action="search", index="agent-logs", since="1h", '
        'aggs={"by": {"terms": {"field": "fake_field"}}})'
    )
    with pytest.raises(Exception, match="fake_field"):
        _check_call(call)


def test_the_call_extractor_reads_a_call_that_spans_lines() -> None:
    text = 'Run:\n\nquery_telemetry(\n    action="count",\n    index="agent-logs",\n)\n'
    assert _calls(text) == ['query_telemetry(\n    action="count",\n    index="agent-logs",\n)']
