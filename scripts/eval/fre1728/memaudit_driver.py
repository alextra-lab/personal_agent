"""FRE-1728: the pilot loop. It runs in MemAudit's environment.

    python -I scripts/eval/fre1728/memaudit_driver.py --memaudit-dir <clone> --bridge-url <url> --run-dir <dir> ...

``run.py`` starts this after the bridge. This file imports no project module, and no MemAudit file changes.
``RealMemAudit`` replaces names at runtime: the completion function, the usage hook and the attribution root.

Scoring a target fact, with MemAudit's threshold of 0.75:

    graph score   MemAudit's slot fill over the graph's facts, then its judge.
    answer score  MemAudit's judge over the real agent's answer, one question in a fresh session.
    outcome       graph low and answer low: write_failure.
                  graph high and answer low: retrieval_failure.
                  graph low and answer high: answer_without_graph, reported and not ranked.
                  both high: ok.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

OK_THRESHOLD = 0.75
_CATEGORIES = (
    "skill_memory",
    "knowledge_memory",
    "episodic_memory",
    "self_model",
    "assistance_preference",
)
_ATTEMPTS = 3
EXIT_OK = 0
EXIT_SPEND_CAP = 3
EXIT_HARNESS_FAILURE = 4


@dataclass(frozen=True)
class PilotConfig:
    """One pilot's inputs.

    Attributes:
        runs: Run labels. The second run is the noise arm.
        users: MemAudit user ids.
        tasks_per_user: Tasks per user.
        max_turns: The turn cap per task.
        run_dir: The output directory.
        severity: Severity per MemAudit label, set by the owner.
    """

    runs: tuple[str, ...]
    users: tuple[str, ...]
    tasks_per_user: int
    max_turns: int
    run_dir: Path
    severity: dict[str, float]


@dataclass(frozen=True)
class Target:
    """One hidden fact, and the task that can bring it out.

    Attributes:
        user_id: The user.
        episode: The task's 1-based position in the user's selected tasks.
        task: The task record.
        category: The bank category.
        dimension: The bank dimension.
        short: The reference value.
        explanation: The user-specific explanation.
    """

    user_id: str
    episode: int
    task: dict[str, str]
    category: str
    dimension: str
    short: str
    explanation: str


class MemAudit(Protocol):
    """The parts of MemAudit that the pilot uses."""

    def load_user(self, user_id: str) -> dict[str, dict]:
        """Return a user's record."""
        ...

    def load_tasks(self, user_id: str) -> list[dict[str, str]]:
        """Return a user's tasks."""
        ...

    def run_episode(self, user_id: str, task: str, agent: object, max_turns: int) -> dict:
        """Run one task and return the transcript record."""
        ...

    def slot_fill(self, memories_json: str, category: str, dimension: str) -> tuple[str, str]:
        """Return a label and a reason."""
        ...

    def judge(
        self, dimension: str, explanation: str, predicted: str, ground_truth: str
    ) -> tuple[float, str]:
        """Return a score and a reason. An empty reason means that the reply did not parse."""
        ...

    def attribute(
        self, record: dict, run: str, user_id: str, tasks: list[dict[str, str]], oracle: dict
    ) -> dict:
        """Return the attribution of one low score."""
        ...


def select_tasks(tasks: Sequence[dict[str, str]], count: int, used: set) -> list[dict[str, str]]:
    """Take tasks round robin over the bank's categories.

    A dimension that an earlier user took is skipped, so no question repeats in a run.

    Args:
        tasks: The user's tasks, in MemAudit's order.
        count: How many to take.
        used: Dimensions already taken. This function adds to it.

    Returns:
        The selected tasks.
    """
    queues = {c: [t for t in tasks if t["target_category"] == c] for c in _CATEGORIES}
    selected: list[dict[str, str]] = []
    while len(selected) < count and any(queues.values()):
        for category in _CATEGORIES:
            queue = queues[category]
            while queue and queue[0]["target_dimension"] in used:
                queue.pop(0)
            if queue and len(selected) < count:
                task = queue.pop(0)
                used.add(task["target_dimension"])
                selected.append(task)
    return selected


def question_for(dimension: str) -> str:
    """Build the fresh-session question. It holds no reference value.

    Args:
        dimension: The bank dimension.

    Returns:
        The question.
    """
    words = dimension.replace("_", " ")
    return (
        f"From what you remember about me: what best describes my {words}? "
        "Answer in one or two sentences. If you do not know, say so."
    )


def classify(graph_score: float, answer_score: float) -> str:
    """Map the two scores to an outcome.

    Args:
        graph_score: The score from the graph's facts.
        answer_score: The score from the agent's answer.

    Returns:
        The outcome.
    """
    in_graph = graph_score >= OK_THRESHOLD
    in_answer = answer_score >= OK_THRESHOLD
    if in_answer:
        return "ok" if in_graph else "answer_without_graph"
    return "retrieval_failure" if in_graph else "write_failure"


def attribution_copy(transcript: dict) -> dict:
    """Copy a transcript without the last simulator message.

    The episode ends there, so the agent never received that message. A fact disclosed only in it is not a
    memory failure, and ``attribute_dim`` reads every simulator message that it finds.

    Args:
        transcript: The full transcript.

    Returns:
        The copy.
    """
    copied = json.loads(json.dumps(transcript))
    if copied["turns"]:
        copied["turns"][-1]["user_simulator"]["text"] = None
    return copied


def make_guarded_completion(complete: Callable, check_spend: Callable[[], None]) -> Callable:
    """Wrap MemAudit's completion: one paid attempt per spend check, and failure stops the run.

    ``get_completion`` makes three attempts and then returns ``None``. A caller then scores 0.0, which looks
    like a Seshat defect.

    Args:
        complete: ``llm_client.get_completion``.
        check_spend: Raises at the cap.

    Returns:
        The wrapper.
    """
    from memaudit_adapter import HarnessFailure

    def guarded(messages: list[dict[str, str]], **kwargs: object) -> str:
        kwargs["max_retries"] = 1
        for _ in range(_ATTEMPTS):
            check_spend()
            reply = complete(messages, **kwargs)
            if reply:
                return reply
        raise HarnessFailure(f"MemAudit's model call failed {_ATTEMPTS} times")

    return guarded


class RealMemAudit:
    """MemAudit itself, at a pinned commit."""

    def __init__(self, repo: Path, run_dir: Path, bridge: object) -> None:
        """Import MemAudit and redirect its cost and output paths.

        Args:
            repo: The clone.
            run_dir: The run's output directory.
            bridge: The bridge client.
        """
        sys.path.insert(0, str(repo))
        import failure_attribution
        import llm_client
        import scorer
        import simulation
        import token_tracker

        self._repo = repo
        self._run_dir = run_dir
        self._simulation = simulation
        self._evaluator = scorer.ReconstructionEvaluator()
        self._attribution = failure_attribution

        llm_client._client = llm_client._client.with_options(max_retries=0)
        token_tracker.log_usage = lambda _model, prompt, completion, _total: bridge.report_usage(
            prompt, completion
        )
        guarded = make_guarded_completion(llm_client.get_completion, bridge.check_spend)
        simulation.get_completion = guarded
        scorer.get_completion = guarded
        failure_attribution.get_completion = guarded
        failure_attribution._HERE = str(run_dir)

    def load_user(self, user_id: str) -> dict[str, dict]:
        """Return a user's record.

        Args:
            user_id: The user.

        Returns:
            The record.
        """
        return self._simulation.load_user(user_id)

    def load_tasks(self, user_id: str) -> list[dict[str, str]]:
        """Return a user's tasks.

        Args:
            user_id: The user.

        Returns:
            The task records.
        """
        path = self._repo / "benchmark_data" / "CustomTasksPooledFinal" / f"{user_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))["tasks"]

    def run_episode(self, user_id: str, task: str, agent: object, max_turns: int) -> dict:
        """Run one task. The preference judge is off, which saves one paid call per turn.

        Args:
            user_id: The user.
            task: The task text.
            agent: The agent.
            max_turns: The turn cap.

        Returns:
            The transcript record.
        """
        simulation = self._simulation

        class _NoPreference:
            def check(self, _user_id: str, _task: str, _response: str) -> object:
                return simulation.PrefJudgment(score=None, per_pref=[], reason="off")

        episode = simulation.Episode(user_id, task, None, "llm", max_turns=max_turns)
        episode.pref_checker = _NoPreference()
        result = episode.run(agent)
        path = self._run_dir / "tmp_episode.json"
        result.save_history(str(path))
        return json.loads(path.read_text(encoding="utf-8"))

    def slot_fill(self, memories_json: str, category: str, dimension: str) -> tuple[str, str]:
        """Fill one slot from the memories.

        Args:
            memories_json: The facts.
            category: The bank category.
            dimension: The bank dimension.

        Returns:
            A label and a reason.
        """
        return self._evaluator._slot_fill(memories_json, category, dimension)

    def judge(
        self, dimension: str, explanation: str, predicted: str, ground_truth: str
    ) -> tuple[float, str]:
        """Score a prediction.

        Args:
            dimension: The bank dimension.
            explanation: The user-specific explanation.
            predicted: The prediction.
            ground_truth: The reference value.

        Returns:
            A score and a reason.
        """
        return self._evaluator._judge(dimension, explanation, predicted, ground_truth)

    def attribute(
        self, record: dict, run: str, user_id: str, tasks: list[dict[str, str]], oracle: dict
    ) -> dict:
        """Attribute one low score.

        Args:
            record: The fact.
            run: The run label.
            user_id: The user.
            tasks: The user's selected tasks.
            oracle: The cache of task-design judgments for this user.

        Returns:
            The attribution.
        """
        return self._attribution.attribute_dim(record, run, user_id, tasks, oracle)


def build_targets(
    memaudit: MemAudit, users: Sequence[str], tasks_per_user: int
) -> dict[str, list[Target]]:
    """Fix each user's tasks and target facts. Every run uses the same ones.

    Args:
        memaudit: MemAudit.
        users: The users.
        tasks_per_user: Tasks per user.

    Returns:
        The targets per user.
    """
    used: set = set()
    targets: dict[str, list[Target]] = {}
    for user_id in users:
        bank = memaudit.load_user(user_id)["memory_bank"]
        selected = select_tasks(memaudit.load_tasks(user_id), tasks_per_user, used)
        entries: list[Target] = []
        for episode, task in enumerate(selected, 1):
            category = task["target_category"]
            entry = next(e for e in bank[category] if e["dimension"] == task["target_dimension"])
            entries.append(
                Target(
                    user_id=user_id,
                    episode=episode,
                    task=task,
                    category=category,
                    dimension=entry["dimension"],
                    short=entry["short"],
                    explanation=entry["explanation"],
                )
            )
        targets[user_id] = entries
    return targets


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_user(
    run: str,
    targets: list[Target],
    config: PilotConfig,
    memaudit: MemAudit,
    agent: object,
    bridge: object,
) -> list[dict[str, object]]:
    user_id = targets[0].user_id
    wiped = bridge.wipe()
    _write_json(config.run_dir / "results" / run / f"{user_id}.wipe.json", wiped)
    # Startup creates an owner entity with no content, so only a fact with content is a leak.
    residual = [m for m in wiped["residual_memories"] if m.get("content")]
    agent.trace_ids = []
    tasks = [t.task for t in targets]
    for target in targets:
        transcript = memaudit.run_episode(user_id, target.task["task"], agent, config.max_turns)
        name = f"{user_id}/episode_{target.episode}.json"
        _write_json(config.run_dir / "transcripts" / run / name, transcript)
        _write_json(config.run_dir / "history" / run / name, attribution_copy(transcript))

    settled = bridge.settle(list(agent.trace_ids))
    user_invalid: list[str] = []
    if residual:
        user_invalid.append(f"facts survived the wipe: {len(residual)}")
    if settled["missing_turns"]:
        user_invalid.append(f"turns missing from the graph: {len(settled['missing_turns'])}")
    if settled["budget_denied"]:
        user_invalid.append(f"budget denials: {settled['budget_denied']}")

    # Read the graph before any question, so that a question's own extraction cannot feed the read.
    memories = agent.get_all_memories()
    _write_json(config.run_dir / "memories" / run / f"{user_id}.json", memories)
    memories_json = json.dumps(memories, ensure_ascii=False, indent=2)

    oracle: dict = {}
    facts: list[dict[str, object]] = []
    for target in targets:
        label_text, fill_reason = memaudit.slot_fill(
            memories_json, target.category, target.dimension
        )
        graph_score, graph_reason = memaudit.judge(
            target.dimension, target.explanation, label_text, target.short
        )
        question = question_for(target.dimension)
        answer = agent.ask(question)
        answer_score, answer_reason = memaudit.judge(
            target.dimension, target.explanation, answer, target.short
        )
        invalid = list(user_invalid)
        if not fill_reason:
            invalid.append("the slot fill reply did not parse")
        if not graph_reason or not answer_reason:
            invalid.append("a judge reply did not parse")
        outcome = classify(graph_score, answer_score)
        label = "ok"
        if outcome != "ok":
            record = {
                "dimension": target.dimension,
                "ground_truth": target.short,
                "explanation": target.explanation,
                "score": min(graph_score, answer_score),
            }
            attribution = memaudit.attribute(record, run, user_id, tasks, oracle)
            label = attribution["category"]
        facts.append(
            {
                "run": run,
                "user_id": user_id,
                "category": target.category,
                "dimension": target.dimension,
                "reference": target.short,
                "graph_label": label_text,
                "graph_score": graph_score,
                "question": question,
                "answer": answer,
                "answer_score": answer_score,
                "outcome": outcome,
                "label": label,
                "invalid_reasons": invalid,
                "transcript": f"transcripts/{run}/{user_id}/episode_{target.episode}.json",
            }
        )
    _write_json(config.run_dir / "results" / run / f"{user_id}.json", facts)
    return facts


def run_pilot(config: PilotConfig, memaudit: MemAudit, bridge: object, agent: object) -> int:
    """Run the pilot. A stop writes the partial results and the reason.

    Args:
        config: The inputs.
        memaudit: MemAudit.
        bridge: The bridge client.
        agent: The ``SeshatAgent``.

    Returns:
        The exit code.
    """
    from defect_rank import rank_defects, render_report
    from memaudit_adapter import SpendCapReached

    targets = build_targets(memaudit, config.users, config.tasks_per_user)
    facts_by_run: dict[str, list[dict[str, object]]] = {}
    status, code, detail = "complete", EXIT_OK, ""
    try:
        for run in config.runs:
            facts_by_run[run] = []
            for user_id in config.users:
                facts_by_run[run] += _run_user(
                    run, targets[user_id], config, memaudit, agent, bridge
                )
    except SpendCapReached as exc:
        status, code, detail = "stopped: spend_cap", EXIT_SPEND_CAP, str(exc)
    except Exception as exc:  # noqa: BLE001 - any other failure must still leave the partial results and the reason
        status, code, detail = (
            "stopped: harness_failure",
            EXIT_HARNESS_FAILURE,
            f"{type(exc).__name__}: {exc}",
        )

    # The stopped gateway no longer answers, and the spend comes from the bridge's Postgres read.
    try:
        spent = bridge.spend()["spent_usd"]
    except Exception as exc:  # noqa: BLE001 - this read must not cost the report
        spent, detail = None, f"{detail} The spend read failed: {exc}".strip()
    manifest = {
        "status": status,
        "detail": detail,
        "spent_usd": spent,
        "invalid_facts": [
            {
                "run": fact["run"],
                "user_id": fact["user_id"],
                "dimension": fact["dimension"],
                "reasons": fact["invalid_reasons"],
            }
            for facts in facts_by_run.values()
            for fact in facts
            if fact["invalid_reasons"]
        ],
        "runs": list(config.runs),
        "users": list(config.users),
        "targets": {u: [asdict(t) for t in ts] for u, ts in targets.items()},
    }
    defects = rank_defects(facts_by_run, config.severity) if status == "complete" else []
    _write_json(config.run_dir / "manifest.json", manifest)
    _write_json(config.run_dir / "defects.json", defects)
    report = render_report(defects, manifest)
    (config.run_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pilot with MemAudit itself.

    Args:
        argv: Command-line arguments.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memaudit-dir", type=Path, required=True)
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--users", nargs="+", required=True)
    parser.add_argument("--tasks-per-user", type=int, required=True)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--severity", type=Path, required=True)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.memaudit_dir))
    from memaudit_adapter import BridgeClient, SeshatAgent

    bridge = BridgeClient(args.bridge_url)
    config = PilotConfig(
        runs=tuple(args.runs),
        users=tuple(args.users),
        tasks_per_user=args.tasks_per_user,
        max_turns=args.max_turns,
        run_dir=args.run_dir,
        severity=json.loads(args.severity.read_text(encoding="utf-8")),
    )
    args.run_dir.mkdir(parents=True, exist_ok=True)
    memaudit = RealMemAudit(args.memaudit_dir, args.run_dir, bridge)
    return run_pilot(config, memaudit, bridge, SeshatAgent(bridge))


if __name__ == "__main__":
    sys.exit(main())
