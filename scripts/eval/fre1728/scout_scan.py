"""FRE-1728: scan the pilot's task transcripts with Inspect Scout, then rank every defect together.

    python -I scripts/eval/fre1728/scout_scan.py --run-dir <dir> --runs A B --remaining-usd <usd>

``run.py`` starts this in Scout's own environment, after the driver. It makes no gateway turn: it reads the saved
transcripts and sends them to the scanning model. MemAudit's labels cover memory only, so this adds the conversation
defects. The both-runs rule and the severities are the same for both kinds.

Scout documents no cost cap. This file estimates the cost from the transcripts' size before it starts, and it refuses
if the estimate does not fit the pilot's remaining budget. An interrupted scan is not resumed here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from defect_rank import rank_defects as rank  # noqa: E402
from defect_rank import render_report  # noqa: E402

DEFAULT_MODEL = "openai/gpt-5.4-mini"
#: USD per token, input and output, from ``litellm.model_cost`` on 2026-11-02.
PRICES: Mapping[str, tuple[float, float]] = {
    "openai/gpt-5.4-mini": (7.5e-07, 4.5e-06),
    "openai/gpt-5.6-luna": (2e-07, 1.2e-06),
    "openai/gpt-6-luna": (1e-07, 5e-07),
}
_CHARS_PER_TOKEN = 4
_PROMPT_OVERHEAD_TOKENS = 300
_ANSWER_TOKENS = 300
_ESTIMATE_MARGIN = 1.5
OUTCOME = "conversation_failure"
CATEGORY = "conversation"
EXIT_OK = 0
EXIT_OVER_BUDGET = 3


def load_episodes(run_dir: Path, runs: Sequence[str]) -> list[dict[str, object]]:
    """Read the saved task transcripts.

    Args:
        run_dir: The run directory.
        runs: The run labels.

    Returns:
        One record per episode: ``id``, ``run``, ``user_id``, ``episode``, ``path``, ``transcript``.
    """
    episodes: list[dict[str, object]] = []
    for run in runs:
        for path in sorted((run_dir / "transcripts" / run).glob("*/episode_*.json")):
            user_id = path.parent.name
            episode = path.stem.split("_")[1]
            episodes.append(
                {
                    "id": f"{run}|{user_id}|{episode}",
                    "run": run,
                    "user_id": user_id,
                    "episode": episode,
                    "path": str(path.relative_to(run_dir)),
                    "transcript": json.loads(path.read_text(encoding="utf-8")),
                }
            )
    return episodes


def transcript_turns(transcript: Mapping[str, object]) -> list[tuple[str, str]]:
    """Flatten one transcript into ``(role, text)`` pairs, starting with the task.

    Args:
        transcript: The saved transcript.

    Returns:
        The messages in order. An empty simulator message is left out.
    """
    messages = [("user", str(transcript["task"]))]
    for turn in transcript["turns"]:  # type: ignore[attr-defined]
        messages.append(("assistant", str(turn["agent_response"])))
        text = (turn.get("user_simulator") or {}).get("text")
        if text:
            messages.append(("user", str(text)))
    return messages


def estimate_usd(
    episodes: Sequence[Mapping[str, object]], question_count: int, model: str
) -> tuple[int, float]:
    """Estimate the scan's calls and cost, with a margin.

    Args:
        episodes: The episodes to scan.
        question_count: The questions per transcript.
        model: A key of :data:`PRICES`.

    Returns:
        The call count and the cost in USD.
    """
    input_price, output_price = PRICES[model]
    calls = len(episodes) * question_count
    chars = sum(
        len(text)
        for episode in episodes
        for _, text in transcript_turns(episode["transcript"])  # type: ignore[arg-type]
    )
    input_tokens = chars / _CHARS_PER_TOKEN * question_count + calls * _PROMPT_OVERHEAD_TOKENS
    output_tokens = calls * _ANSWER_TOKENS
    cost = (input_tokens * input_price + output_tokens * output_price) * _ESTIMATE_MARGIN
    return calls, cost


def findings_to_facts(
    findings: Sequence[Mapping[str, object]], episodes: Sequence[Mapping[str, object]]
) -> dict[str, list[dict[str, object]]]:
    """Convert positive scanner answers into the records that the ranking reads.

    Args:
        findings: One record per answer: ``transcript_id``, ``question``, ``value``, ``explanation``.
        episodes: The scanned episodes.

    Returns:
        The records per run.
    """
    by_id = {str(e["id"]): e for e in episodes}
    facts: dict[str, list[dict[str, object]]] = {}
    for finding in findings:
        if finding["value"] is not True:
            continue
        episode = by_id[str(finding["transcript_id"])]
        facts.setdefault(str(episode["run"]), []).append(
            {
                "run": episode["run"],
                "user_id": episode["user_id"],
                "category": CATEGORY,
                "dimension": f"episode_{episode['episode']}",
                "outcome": OUTCOME,
                "label": finding["question"],
                "graph_score": None,
                "answer_score": None,
                "invalid_reasons": [],
                "transcript": episode["path"],
                "explanation": finding["explanation"],
            }
        )
    return facts


def rank_everything(
    run_dir: Path,
    runs: Sequence[str],
    conversation_facts: Mapping[str, Sequence[Mapping[str, object]]],
    severity: Mapping[str, float],
) -> str:
    """Merge the memory facts and the conversation facts, rank them and rewrite the report.

    Args:
        run_dir: The run directory.
        runs: The run labels.
        conversation_facts: The conversation records per run.
        severity: Severity per label.

    Returns:
        The report text.
    """
    merged: dict[str, list[Mapping[str, object]]] = {}
    for run in runs:
        merged[run] = list(conversation_facts.get(run, []))
        for path in sorted((run_dir / "results" / run).glob("*.json")):
            if not path.name.endswith(".wipe.json"):
                merged[run] += json.loads(path.read_text(encoding="utf-8"))
    defects = rank(merged, severity)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    (run_dir / "defects.json").write_text(
        json.dumps(defects, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = render_report(defects, manifest)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    return report


async def _insert_transcripts(database: Path, episodes: Sequence[Mapping[str, object]]) -> None:
    from inspect_ai.model import ChatMessageAssistant, ChatMessageUser
    from inspect_scout import Transcript, transcripts_db

    classes = {"user": ChatMessageUser, "assistant": ChatMessageAssistant}
    transcripts = [
        Transcript(
            transcript_id=str(episode["id"]),
            source_type="fre1728-memaudit",
            source_id=str(episode["run"]),
            metadata={"user_id": episode["user_id"], "episode": episode["episode"]},
            messages=[
                classes[role](content=text)
                for role, text in transcript_turns(episode["transcript"])  # type: ignore[arg-type]
            ],
        )
        for episode in episodes
    ]
    async with transcripts_db(str(database)) as db:
        await db.insert(transcripts)


def run_scan(
    run_dir: Path, questions: Sequence[Mapping[str, str]], model: str, base_url: str | None
) -> list[dict[str, object]]:
    """Run Scout over the transcript database.

    Args:
        run_dir: The run directory.
        questions: The questions, each with ``name`` and ``question``.
        model: The scanning model.
        base_url: An OpenAI-compatible URL for a stub run, or None.

    Returns:
        One record per answer.

    Raises:
        RuntimeError: If the scan did not complete.
    """
    from inspect_ai.model import GenerateConfig
    from inspect_scout import llm_scanner, scan, scan_results_df, transcripts_from

    status = scan(
        scanners={
            q["name"]: llm_scanner(question=q["question"], answer="boolean") for q in questions
        },
        transcripts=transcripts_from(str(run_dir / "scout" / "db")),
        scans=str(run_dir / "scout" / "scans"),
        model=model,
        model_base_url=base_url,
        model_args={"responses_api": False},
        model_config=GenerateConfig(max_retries=2, max_connections=2),
        max_transcripts=2,
        display="plain",
        fail_on_error=True,
    )
    if not status.complete:
        raise RuntimeError(f"the Scout scan did not complete: {status.location}")
    results = scan_results_df(status.location)
    findings: list[dict[str, object]] = []
    for question in questions:
        frame = results.scanners[question["name"]]
        for row in frame.to_dict("records"):
            findings.append(
                {
                    "transcript_id": row["transcript_id"],
                    "question": question["name"],
                    "value": str(row["value"]).strip().lower() == "true",
                    "explanation": str(row.get("explanation") or ""),
                }
            )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Estimate, scan and rank.

    Args:
        argv: Command-line arguments.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--remaining-usd", type=float, required=True)
    parser.add_argument("--model", choices=sorted(PRICES), default=DEFAULT_MODEL)
    parser.add_argument("--model-base-url", default=None)
    parser.add_argument("--questions", type=Path, default=_HERE / "scout_questions.json")
    parser.add_argument("--severity", type=Path, default=_HERE / "severity.json")
    args = parser.parse_args(argv)

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    episodes = load_episodes(args.run_dir, args.runs)
    calls, cost = estimate_usd(episodes, len(questions), args.model)
    print(f"[scout] {len(episodes)} transcripts, {calls} calls, estimate {cost:.2f} USD")
    if cost > args.remaining_usd:
        print(f"[scout] refused: the remaining budget is {args.remaining_usd:.2f} USD")
        return EXIT_OVER_BUDGET

    asyncio.run(_insert_transcripts(args.run_dir / "scout" / "db", episodes))
    findings = run_scan(args.run_dir, questions, args.model, args.model_base_url)
    (args.run_dir / "scout" / "findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    severity = json.loads(args.severity.read_text(encoding="utf-8"))
    print(rank_everything(args.run_dir, args.runs, findings_to_facts(findings, episodes), severity))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
