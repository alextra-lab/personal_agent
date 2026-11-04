"""FRE-1730: the owner's table. A difference counts only if it is larger than the gap between repeat runs."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.eval.fre1730.footprint import plugin_changed_content, plugin_only
from scripts.eval.fre1730.matrix import BUDGET_SUBTYPE, is_valid

#: Name, label, and digits to show.
METRICS = (
    ("lines_code", "Lines changed outside tests", 1),
    ("lines_test", "Lines changed in tests", 1),
    ("pr_tests_fraction", "PR's own tests passed (share)", 2),
    ("tests_added", "Test functions added", 1),
    ("cost_usd", "Cost (USD)", 2),
    ("duration_s", "Time (s)", 0),
    ("turns", "Turns", 1),
)
NOT_COVERED = (
    "- Each session is one ticket with no history. The test does not show the plugin's effect on a long session.",
    "- Neither arm has the seats' other plugins, memory, output style, `/build` steps, Codex review or master's gate.",
    "- The model reads its session budget in a system message, in both arms. A seat session has no such message.",
    "- The score cannot see design quality. A sample of the diffs is for the owner to read.",
    "- Five small tickets do not stand for an ADR, a migration or a PWA change.",
)
Record = Mapping[str, object]


@dataclass(frozen=True)
class TaskComparison:
    """One metric on one task.

    Attributes:
        ticket: The ticket's id.
        plugin_median: The plugin arm's median.
        control_median: The control arm's median.
        noise_gap: The larger of the two arms' ranges across repeat runs.
        counts: The difference is larger than the noise gap. One run per arm measures no noise, so it never counts.
    """

    ticket: str
    plugin_median: float
    control_median: float
    noise_gap: float
    counts: bool

    @property
    def difference(self) -> float:
        """Return plugin minus control.

        Returns:
            The difference.
        """
        return self.plugin_median - self.control_median


def _value(record: Record, metric: str) -> float:
    return float(str(record[metric]))


def compare_metric(records: Sequence[Record], metric: str) -> list[TaskComparison]:
    """Compare the arms on one metric, per task.

    Args:
        records: The finished sessions.
        metric: The metric's name.

    Returns:
        One row per task that has both arms.
    """
    values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        values[str(record["ticket"])][str(record["arm"])].append(_value(record, metric))
    rows = []
    for ticket, arms in sorted(values.items()):
        plugin, control = arms.get("plugin"), arms.get("control")
        if not plugin or not control:
            continue
        gap = max(max(plugin) - min(plugin), max(control) - min(control))
        difference = statistics.median(plugin) - statistics.median(control)
        measured_noise = len(plugin) > 1 and len(control) > 1
        rows.append(
            TaskComparison(
                ticket,
                statistics.median(plugin),
                statistics.median(control),
                gap,
                measured_noise and abs(difference) > gap,
            )
        )
    return rows


def risky_test_rate(records: Sequence[Record], arm: str) -> tuple[int, int]:
    """Count the sessions on a risky task that added a test.

    Args:
        records: The finished sessions.
        arm: The arm.

    Returns:
        Sessions that added a test, and all sessions on a risky task in this arm.
    """
    risky = [r for r in records if r["arm"] == arm and r["risky"]]
    return sum(1 for r in risky if _value(r, "tests_added") > 0), len(risky)


def render_report(records: Sequence[Record], hook_events: Sequence[str], go: str) -> str:
    """Render the report.

    Args:
        records: The finished sessions.
        hook_events: The hook events that the plugin declares.
        go: The owner's recorded go.

    Returns:
        The report as Markdown.
    """
    spent = sum(_value(r, "cost_usd") for r in records)
    excluded = [r for r in records if not is_valid(r)]
    records = [r for r in records if is_valid(r)]
    lines = [
        "# FRE-1730 replay report",
        "",
        f"Valid sessions: {len(records)}. Sessions left out: {len(excluded)}. "
        f"Owner's go: {go or 'none (stub run)'}.",
        "",
    ]
    lines += [f"- Left out: {r['key']}: {r['subtype']}, {r['problems']}" for r in excluded]
    lines += [
        "## Per arm",
        "",
        "A value is the mean, over the tasks, of the median of the repeat runs. The noise gap is the mean, over the "
        "tasks, of the larger range between repeat runs of one arm. A task counts if its difference is larger than "
        "its own noise gap.",
        "",
        "| Metric | Plugin | Control | Noise gap | Tasks where the difference counts |",
        "| -- | -- | -- | -- | -- |",
    ]
    details = ["## Per task", ""]
    for metric, label, digits in METRICS:
        rows = compare_metric(records, metric)
        if not rows:
            continue
        plugin = statistics.mean(r.plugin_median for r in rows)
        control = statistics.mean(r.control_median for r in rows)
        gap = statistics.mean(r.noise_gap for r in rows)
        counted = [r for r in rows if r.counts]
        lines.append(
            f"| {label} | {plugin:.{digits}f} | {control:.{digits}f} | {gap:.{digits}f} "
            f"| {len(counted)} of {len(rows)} |"
        )
        details += [
            f"### {label}",
            "",
            "| Task | Plugin | Control | Difference | Noise gap | Counts |",
            "| -- | -- | -- | -- | -- | -- |",
        ]
        details += [
            f"| {r.ticket} | {r.plugin_median:.{digits}f} | {r.control_median:.{digits}f} | "
            f"{r.difference:+.{digits}f} | {r.noise_gap:.{digits}f} | {'yes' if r.counts else 'no'} |"
            for r in rows
        ]
        details.append("")
    plugin_added, plugin_risky = risky_test_rate(records, "plugin")
    control_added, control_risky = risky_test_rate(records, "control")
    at_budget = {
        arm: sum(1 for r in records if r["arm"] == arm and r["subtype"] == BUDGET_SUBTYPE)
        for arm in ("plugin", "control")
    }
    lines += [
        "",
        f"Sessions that reached their budget and did not finish: plugin {at_budget['plugin']}, "
        f"control {at_budget['control']}. They count in the tables at the cost and diff that they reached.",
        f"Sessions on a risky task that added a test: plugin {plugin_added} of {plugin_risky}, "
        f"control {control_added} of {control_risky}.",
        f"Total spend, with the sessions left out: ${spent:.2f}.",
        "",
        *details,
        "## Files that only the plugin arm wrote in `~`",
        "",
    ]
    plugin_paths = [p for r in records if r["arm"] == "plugin" for p in _paths(r)]
    control_paths = [p for r in records if r["arm"] == "control" for p in _paths(r)]
    only = sorted(set(plugin_only(plugin_paths, control_paths)))
    lines += [f"- `{path}`" for path in only] or ["- none"]
    lines += ["", "## Files that both arms wrote, where the plugin arm wrote other content", ""]
    content = plugin_changed_content(_digests(records, "plugin"), _digests(records, "control"))
    lines += [f"- `{path}`" for path in content] or ["- none"]
    refused = sorted({str(t) for r in records for t in _sequence(r, "refused")})
    lines += ["", "## Network targets that the gate refused", ""]
    lines += [f"- `{target}`" for target in refused] or ["- none"]
    lines += ["", "## Ignored files that the diffs do not show", ""]
    hidden = [f"- {r['key']}: `{path}`" for r in records for path in _sequence(r, "ignored_paths")]
    lines += hidden or ["- none"]
    lines += ["", "## Diffs for the owner to read", ""]
    sample = {(str(r["ticket"]), str(r["arm"])): str(r["diff_path"]) for r in records}
    lines += [f"- {ticket}, {arm}: `{path}`" for (ticket, arm), path in sorted(sample.items())]
    lines += ["", "## What the plugin and this test do not cover", ""]
    lines.append(
        f"- The plugin declares hooks on: {', '.join(hook_events) or 'nothing'}. It has no other gate on a session."
    )
    lines += NOT_COVERED
    lines += [
        "",
        "## Recommendation",
        "",
        "Master writes this after reading the sample. The owner decides on the install.",
    ]
    return "\n".join(lines) + "\n"


def _digests(records: Sequence[Record], arm: str) -> list[Mapping[str, str]]:
    found: list[Mapping[str, str]] = []
    for record in records:
        digests = record.get("footprint_digests")
        if record["arm"] == arm and isinstance(digests, dict):
            found.append(digests)
    return found


def _sequence(record: Record, key: str) -> Sequence[object]:
    value = record.get(key, ())
    return value if isinstance(value, list | tuple) else ()


def _paths(record: Record) -> list[str]:
    return [str(path) for path in _sequence(record, "footprint")]


def plugin_hook_events(plugin: Path) -> list[str]:
    """Read the hook events that a plugin declares. This reads files as text and runs nothing.

    Args:
        plugin: The plugin directory.

    Returns:
        The events.
    """
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    files = manifest.get("hooks", [])
    events: set[str] = set()
    for name in [files] if isinstance(files, str) else files:
        events |= set(json.loads((plugin / name).read_text()).get("hooks", {}))
    return sorted(events)
