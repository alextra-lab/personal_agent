"""FRE-475 offline deterministic mechanism test (ADR-0085).

Answers the **one deterministic question** the owner's instructive note (Linear
``c8ac1fee``, 2026-06-04) asked for: *for a fixed accumulation of tool outputs, does
intra-turn digestion reduce the tokens re-sent to the model each round, and by how
much?* — with **no model, no gateway, no deploy** in the loop.

The two live A/Bs (``5f2d1277``, ``950386d6``) were uninterpretable for the mechanism
(≥4 confounds: build drift, trajectory nondeterminism, the FRE-478 output-cap spiral,
the FRE-484 forced-synthesis failure), so ``a0a07227`` is retired as a control. This
harness measures the **mechanical ceiling** — the best case if the model never
re-expands. It proves the floor of the effect, not the behavioural outcome (expand
clawback, answer quality), which still needs a scale run.

Design (faithful to the real path):
- A fixed, curated **tape** of ~20 realistic tool results (:func:`build_tape`).
- Each tape entry is one round. The harness drives the **real**
  :func:`personal_agent.orchestrator.tool_result_digest.apply_intra_turn_digest`
  against an in-memory fake R2 store, in the real birth-time call order (digest the
  fresh batch, *then* ``extend``), so every production gate runs (pin update, key
  validation, ``should_digest``, ``digest_saves_enough``, expand-exemption).
- **Re-bill model** (``HEAD_ONLY_BREAKPOINTS``): the a0a07227 forensics showed the
  accreted tool tail sits past the last cache breakpoint (FRE-468), so the whole
  growing tail is re-billed at full price every round::

      fresh_in(r) = Σ_{i ≤ r} tokens(effective_content_i)   # tail re-billed each round
      total_fresh = Σ_r fresh_in(r)                          # quadratic accumulation

  We also emit a **new-tail** lower-bound curve (``Σ_r tokens(effective_content_r)``,
  each result counted once) to separate "digest shrinks content" from "quadratic
  re-bill amplification".

Run::

    uv run python scripts/eval/fre475_compression_ab/offline_mechanism.py
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from personal_agent.config import settings
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.orchestrator.tool_result_digest import (
    _KEY_SEGMENT_RE,
    _is_existing_digest,
    _safe_session_uuid,
    apply_intra_turn_digest,
)

# Production-representative digest config the harness measures under. Pinned
# explicitly (not read from live ``settings``) so the result is reproducible and
# independent of future default drift.
DIGEST_CONFIG: dict[str, Any] = {
    "tool_result_digest_threshold_tokens": 1500,
    "tool_result_digest_min_savings_tokens": 500,
    "tool_result_digest_keep": 3,
    "tool_result_digest_pin_ttl_turns": 4,
    "tool_result_digest_put_timeout_ms": 2000,
    "tool_result_digest_exclude_tools": [],
    "tool_result_digest_head_lines": 40,
    "tool_result_digest_tail_lines": 20,
}


# ---------------------------------------------------------------------------
# Expected per-entry outcomes (the per-entry structural facts the CI test asserts)
# ---------------------------------------------------------------------------

DIGESTED = "digested"
VERBATIM_BELOW_THRESHOLD = "verbatim:below_threshold"
VERBATIM_PINNED = "verbatim:pinned"
VERBATIM_EXPAND_EXEMPT = "verbatim:expand_exempt"
VERBATIM_MIN_SAVINGS = "verbatim:min_savings"


@dataclass(frozen=True)
class TapeEntry:
    """One fixed tool result in the deterministic tape.

    Attributes:
        tool_call_id: Stable, key-safe call id.
        tool_name: Originating tool.
        arguments: Tool arguments (carries ``path``/``command`` for the sidecar).
        content: Verbatim result content string (JSON for bash/read/json tools).
        success: Whether the tool call succeeded (drives D4 write-release).
        expected: The declared outcome (one of the module constants above).
    """

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    content: str
    success: bool
    expected: str


# ---------------------------------------------------------------------------
# D-1 — the curated, deterministic tape
# ---------------------------------------------------------------------------


def _bash(command: str, *, lines: int, width: int, seed: str) -> str:
    """A realistic large ``bash`` stdout payload (deterministic)."""
    body = "\n".join(f"{seed}/{i:04d}: " + ("tok " * width).strip() for i in range(lines))
    return json.dumps({"command": command, "exit_code": 0, "success": True, "stdout": body})


def _bash_with_traceback(command: str) -> str:
    """A large ``bash`` result with a mid-stream traceback (structured-middle extractor).

    Sized above ``threshold_tokens`` so it digests; the deep stderr exercises the
    frame-preserving ``_structured_digest`` path rather than plain head/tail.
    """
    pre = "\n".join(f"collecting test case number {i} from the suite" for i in range(200))
    tb = (
        'Traceback (most recent call last):\n  File "x.py", line 42, in f\n'
        "    raise ValueError('boom')\nValueError: boom"
    )
    post = "\n".join(f"teardown fixture {i} releasing resources and handles" for i in range(200))
    return json.dumps(
        {"command": command, "exit_code": 1, "success": False, "stdout": pre, "stderr": tb + post}
    )


def _read(path: str, *, lines: int) -> str:
    """A large ranged ``read`` result (deterministic)."""
    body = "\n".join(f"{i:04d}    def func_{i}(arg): return arg * {i}" for i in range(lines))
    return json.dumps(
        {"path": path, "offset": 0, "limit": lines, "total_lines": lines, "content": body}
    )


def _generic_json(n: int) -> str:
    """A large generic-JSON tool result routed to the JSON extractor."""
    return json.dumps({"items": [{"id": i, "name": f"entity-{i}"} for i in range(n)]})


def _near_incompressible(lines: int, width: int) -> str:
    """Build the adverse case proving ``digest_saves_enough`` is genuinely in the loop.

    A ``bash`` result that clears the size threshold but whose head/tail digest barely
    shrinks it. With ``head+tail = 60`` kept lines, a payload of just over 60 wide
    lines elides only a handful, so the saving falls under ``min_savings`` and the
    result stays verbatim despite clearing the size threshold.
    """
    rows = [
        f"{i:03d} " + " ".join(f"uniqueword{i * 131 + j}" for j in range(width))
        for i in range(lines)
    ]
    body = "\n".join(rows)
    return json.dumps(
        {"command": "git log --oneline", "exit_code": 0, "success": True, "stdout": body}
    )


def build_tape() -> list[TapeEntry]:
    """Return the fixed, curated tool-result tape (ADR-0085, FRE-475 D-1).

    Shapes mirror the ``a0a07227`` discovery turn (bash-heavy: ~20 bash vs ~9 read);
    values are synthetic (no raw trace captures committed to git). Each entry carries
    its expected outcome under :data:`DIGEST_CONFIG`, consumed by the CI test.

    Note on reads (ADR-0085 §D4 birth-time design): **every** ``read`` with a path is
    pinned on arrival and stays verbatim within the turn — the model may edit against
    it — so all ``read`` entries are :data:`VERBATIM_PINNED`, not digested. Deferred
    digestion of a released pin is out of scope here (FRE-485). The measured bulk the
    digest targets is therefore the ``bash``/JSON discovery output.
    """
    tape: list[TapeEntry] = [
        TapeEntry(
            "c01",
            "bash",
            {"command": "ls -R src"},
            _bash("ls -R src", lines=400, width=6, seed="src"),
            True,
            DIGESTED,
        ),
        TapeEntry(
            "c02",
            "bash",
            {"command": "grep -rn TODO"},
            _bash("grep -rn TODO", lines=350, width=8, seed="hit"),
            True,
            DIGESTED,
        ),
        TapeEntry(
            "c03",
            "read",
            {"path": "/repo/a.py"},
            _read("/repo/a.py", lines=300),
            True,
            VERBATIM_PINNED,
        ),
        TapeEntry(
            "c04",
            "bash",
            {"command": "find . -name '*.py'"},
            _bash("find . -name '*.py'", lines=500, width=4, seed="file"),
            True,
            DIGESTED,
        ),
        TapeEntry(
            "c05",
            "bash",
            {"command": "cat pyproject.toml"},
            "small output, nothing to see",
            True,
            VERBATIM_BELOW_THRESHOLD,
        ),
        TapeEntry(
            "c06",
            "bash",
            {"command": "pytest -x"},
            _bash_with_traceback("pytest -x"),
            False,
            DIGESTED,
        ),
        TapeEntry(
            "c07",
            "read",
            {"path": "/repo/edit_me.py"},
            _read("/repo/edit_me.py", lines=260),
            True,
            VERBATIM_PINNED,
        ),
        TapeEntry(
            "c08",
            "bash",
            {"command": "wc -l **/*.py"},
            _bash("wc -l", lines=300, width=5, seed="wc"),
            True,
            DIGESTED,
        ),
        TapeEntry(
            "c09",
            "write",
            {"path": "/repo/edit_me.py"},
            json.dumps({"success": True, "path": "/repo/edit_me.py"}),
            True,
            VERBATIM_BELOW_THRESHOLD,
        ),
        TapeEntry(
            "c10",
            "bash",
            {"command": "git log --oneline"},
            _near_incompressible(lines=66, width=8),
            True,
            VERBATIM_MIN_SAVINGS,
        ),
        TapeEntry(
            "c11",
            "bash",
            {"command": "ls node_modules"},
            _bash("ls node_modules", lines=600, width=3, seed="dep"),
            True,
            DIGESTED,
        ),
        TapeEntry("c12", "search_knowledge", {"q": "entities"}, _generic_json(400), True, DIGESTED),
        TapeEntry(
            "c13",
            "read",
            {"path": "/repo/b.py"},
            _read("/repo/b.py", lines=320),
            True,
            VERBATIM_PINNED,
        ),
        TapeEntry(
            "c14",
            "bash",
            {"command": "grep -rn import"},
            _bash("grep -rn import", lines=450, width=6, seed="imp"),
            True,
            DIGESTED,
        ),
        TapeEntry("c15", "bash", {"command": "echo ok"}, "ok", True, VERBATIM_BELOW_THRESHOLD),
        TapeEntry(
            "c16",
            "expand_tool_result",
            {"key": "tool-results/s/t/c08"},
            "VERBATIM EXPANSION\n" + "\n".join(f"recovered line {i}" for i in range(400)),
            True,
            VERBATIM_EXPAND_EXEMPT,
        ),
        TapeEntry(
            "c17",
            "bash",
            {"command": "ls -la docs"},
            _bash("ls -la docs", lines=380, width=7, seed="doc"),
            True,
            DIGESTED,
        ),
        TapeEntry(
            "c18",
            "read",
            {"path": "/repo/c.py"},
            _read("/repo/c.py", lines=280),
            True,
            VERBATIM_PINNED,
        ),
        TapeEntry(
            "c19",
            "bash",
            {"command": "find docs -type f"},
            _bash("find docs", lines=420, width=5, seed="df"),
            True,
            DIGESTED,
        ),
        TapeEntry(
            "c20",
            "bash",
            {"command": "git status"},
            _bash("git status", lines=320, width=6, seed="st"),
            True,
            DIGESTED,
        ),
    ]
    return tape


# ---------------------------------------------------------------------------
# In-memory fake R2 store + fake execution context
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory stand-in for ``R2ArtifactStore`` — records puts, serves gets."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    async def put(
        self,
        *,
        r2_key: str,
        content: bytes,
        content_type: str,
        metadata: Any = None,
        trace_id: Any = None,
    ) -> None:
        self.puts.append(r2_key)
        self.objects[r2_key] = content

    async def get(self, r2_key: str, *, trace_id: Any = None) -> bytes:
        return self.objects[r2_key]


def _make_ctx(session_id: str, trace_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        messages=[],
        tool_iteration_count=0,
        tool_result_pins={},
        session_id=session_id,
        trace_id=trace_id,
    )


def _assert_tape_ids_are_key_safe(
    tape: Sequence[TapeEntry], session_id: str, trace_id: str
) -> None:
    """Guard against fake-ctx ids that would silently suppress all digestion (codex Q2).

    A non-UUID session or an unsafe ``trace_id``/``tool_call_id`` makes the digest pass
    skip every candidate, which would make the harness report a false 0%% reduction.
    """
    if _safe_session_uuid(session_id) is None:
        raise ValueError(
            f"harness session_id {session_id!r} is not a UUID — would skip all digestion"
        )
    if not _KEY_SEGMENT_RE.match(trace_id):
        raise ValueError(f"harness trace_id {trace_id!r} fails key grammar — would drop candidates")
    for entry in tape:
        if not _KEY_SEGMENT_RE.match(entry.tool_call_id):
            raise ValueError(f"tape tool_call_id {entry.tool_call_id!r} fails key grammar")


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------


@dataclass
class RoundRow:
    """Per-round measurement for one arm."""

    round: int
    tool: str
    verbatim_tokens: int
    effective_tokens: int  # what actually entered the transcript this round
    cumulative_fresh: int  # Σ_{i≤r} effective (head-only re-bill: per-round full-price tail)
    outcome: str  # "digested" | "verbatim"
    final_content: str  # the exact bytes that entered the transcript (byte-stability check)


@dataclass
class ArmResult:
    """One arm (ON or OFF) of the offline simulation."""

    label: str
    rows: list[RoundRow] = field(default_factory=list)

    @property
    def new_tail_total(self) -> int:
        """Σ_r effective_r — each result counted once (lower-bound curve)."""
        return sum(r.effective_tokens for r in self.rows)

    @property
    def cumulative_fresh_total(self) -> int:
        """Σ_r Σ_{i≤r} effective_i — the quadratic re-bill total (HEAD_ONLY_BREAKPOINTS)."""
        return sum(r.cumulative_fresh for r in self.rows)


async def _run_arm(tape: Sequence[TapeEntry], *, digest_on: bool) -> ArmResult:
    """Run one arm of the simulation, returning its per-round rows."""
    session_id = "11111111-1111-1111-1111-111111111111"
    trace_id = "offlinetrace"
    _assert_tape_ids_are_key_safe(tape, session_id, trace_id)

    ctx = _make_ctx(session_id, trace_id)
    store = _FakeStore()
    arm = ArmResult(label="ON" if digest_on else "OFF")
    cumulative = 0

    for entry in tape:
        ctx.tool_iteration_count += 1
        batch: list[dict[str, Any]] = [
            {
                "tool_call_id": entry.tool_call_id,
                "role": "tool",
                "name": entry.tool_name,
                "content": entry.content,
            }
        ]
        sidecar = {
            entry.tool_call_id: {
                "tool_name": entry.tool_name,
                "success": entry.success,
                "arguments": entry.arguments,
            }
        }
        if digest_on:
            await apply_intra_turn_digest(ctx, batch, sidecar, trace_ctx=None, store=store)
        ctx.messages.extend(batch)

        final_content = str(batch[0]["content"])
        digested = _is_existing_digest(final_content)
        effective = estimate_tokens(final_content)
        cumulative += effective
        arm.rows.append(
            RoundRow(
                round=ctx.tool_iteration_count,
                tool=entry.tool_name,
                verbatim_tokens=estimate_tokens(entry.content),
                effective_tokens=effective,
                cumulative_fresh=cumulative,
                outcome="digested" if digested else "verbatim",
                final_content=final_content,
            )
        )
    return arm


@dataclass
class SimulationResult:
    """Both arms of the offline mechanism simulation."""

    off: ArmResult
    on: ArmResult

    @property
    def cumulative_reduction(self) -> float:
        """Fractional reduction in the quadratic re-bill total (ON vs OFF)."""
        off = self.off.cumulative_fresh_total
        return 0.0 if off == 0 else 1.0 - self.on.cumulative_fresh_total / off

    @property
    def new_tail_reduction(self) -> float:
        """Fractional reduction in the new-tail lower-bound total (ON vs OFF)."""
        off = self.off.new_tail_total
        return 0.0 if off == 0 else 1.0 - self.on.new_tail_total / off


# ---------------------------------------------------------------------------
# Clawback break-even model — how much re-expansion before digestion stops paying
# ---------------------------------------------------------------------------


@dataclass
class BreakevenAnalysis:
    """Break-even expand rates: at what re-expansion does the win turn into a loss.

    A clawback is net-negative because the expansion re-adds the full verbatim bytes
    *on top of* the digest already in the tail (the digest is exempt from
    re-digestion — fix-a), so each expanded result costs an extra ``digest`` (new-tail
    basis) or ``verbatim × remaining_rounds`` (cumulative re-bill basis) versus never
    compressing. The ceiling reduction assumes zero expansion; these numbers say how
    fast it erodes.

    Attributes:
        digested_count: Number of results digested in the ON arm.
        new_tail_breakeven: Mass-based break-even on the new-tail basis — equals the
            digest compression ratio (forgiving; ignores re-bill amplification).
        cumulative_breakeven_best: Highest count-fraction of digested results that can
            be expanded before the cumulative re-bill win is erased, when the model
            expands the *cheapest-to-re-bill* (latest/smallest) digests first.
        cumulative_breakeven_worst: Same, when the model expands the *most expensive*
            (earliest/largest) digests first. ``> 1.0`` means digestion still wins even
            if every digested result is re-expanded.
    """

    digested_count: int
    new_tail_breakeven: float
    cumulative_breakeven_best: float
    cumulative_breakeven_worst: float


def breakeven_analysis(result: SimulationResult) -> BreakevenAnalysis:
    """Compute clawback break-even expand rates from a completed simulation.

    Args:
        result: A finished two-arm :class:`SimulationResult`.

    Returns:
        The :class:`BreakevenAnalysis`. The cumulative basis is the one that matters
        for the real cost driver (the quadratic re-bill); the new-tail basis is a
        forgiving lower bound shown for contrast.
    """
    n = len(result.on.rows)
    # Per-digested-result facts: (round, verbatim_tokens, digest_tokens).
    digested = [
        (row.round, row.verbatim_tokens, row.effective_tokens)
        for row in result.on.rows
        if row.outcome == "digested"
    ]
    if not digested:
        return BreakevenAnalysis(0, 0.0, 0.0, 0.0)

    # New-tail basis: expanding re-adds verbatim; break-even mass fraction = saving /
    # digested-verbatim = the average compression ratio.
    dig_verbatim = sum(v for _, v, _ in digested)
    dig_saving = sum(v - d for _, v, d in digested)
    new_tail_breakeven = dig_saving / dig_verbatim if dig_verbatim else 0.0

    # Cumulative basis: a result digested at round r, expanded one round later, re-adds
    # its verbatim mass riding rounds (r+1)..n → extra = verbatim × (n - r). Break-even
    # when accumulated extra reaches the cumulative saving (gap).
    gap = result.off.cumulative_fresh_total - result.on.cumulative_fresh_total
    extras = sorted(v * (n - r) for r, v, _ in digested)  # ascending = cheapest first

    def fraction_until_gap(ordered: list[int]) -> float:
        acc = 0
        for i, extra in enumerate(ordered, start=1):
            acc += extra
            if acc >= gap:
                return i / len(digested)
        return 1.0 + (gap - acc) / max(gap, 1)  # >1.0: wins even if all are expanded

    best = fraction_until_gap(extras)  # expand cheapest-to-re-bill first → highest f
    worst = fraction_until_gap(list(reversed(extras)))  # expand most expensive first
    return BreakevenAnalysis(
        digested_count=len(digested),
        new_tail_breakeven=new_tail_breakeven,
        cumulative_breakeven_best=best,
        cumulative_breakeven_worst=worst,
    )


async def simulate(tape: Sequence[TapeEntry] | None = None) -> SimulationResult:
    """Run both arms over the tape under :data:`DIGEST_CONFIG` and return the result.

    Pins the digest config onto ``settings`` for the duration of the run and restores
    the prior values afterward, so the call leaves global ``settings`` untouched (the
    CI test relies on this hermeticity).

    Args:
        tape: Optional tape override; defaults to :func:`build_tape`.

    Returns:
        The :class:`SimulationResult` carrying the OFF and ON arms.
    """
    if tape is None:
        tape = build_tape()
    saved = {key: getattr(settings, key) for key in DIGEST_CONFIG}
    try:
        for key, value in DIGEST_CONFIG.items():
            setattr(settings, key, value)
        off = await _run_arm(tape, digest_on=False)
        on = await _run_arm(tape, digest_on=True)
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)
    return SimulationResult(off=off, on=on)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(result: SimulationResult) -> str:
    """Render the per-round table + totals as a markdown string for the ticket."""
    lines: list[str] = []
    lines.append("## FRE-475 offline mechanism result (deterministic, no model)\n")
    lines.append(f"Config: {json.dumps(DIGEST_CONFIG, sort_keys=True)}\n")
    lines.append(
        "| round | tool | verbatim_tok | OFF eff | ON eff | OFF cum_fresh | ON cum_fresh | ON outcome |"
    )
    lines.append("|--:|--|--:|--:|--:|--:|--:|--|")
    for off_row, on_row in zip(result.off.rows, result.on.rows, strict=True):
        lines.append(
            f"| {on_row.round} | {on_row.tool} | {on_row.verbatim_tokens} | "
            f"{off_row.effective_tokens} | {on_row.effective_tokens} | "
            f"{off_row.cumulative_fresh} | {on_row.cumulative_fresh} | {on_row.outcome} |"
        )
    lines.append("")
    lines.append("### Totals\n")
    lines.append(
        f"- **Cumulative fresh (HEAD_ONLY_BREAKPOINTS re-bill):** "
        f"OFF {result.off.cumulative_fresh_total:,} → ON {result.on.cumulative_fresh_total:,} "
        f"= **{result.cumulative_reduction:.1%} reduction**"
    )
    lines.append(
        f"- **New-tail (each result once, lower bound):** "
        f"OFF {result.off.new_tail_total:,} → ON {result.on.new_tail_total:,} "
        f"= **{result.new_tail_reduction:.1%} reduction**"
    )
    digested = sum(1 for r in result.on.rows if r.outcome == "digested")
    lines.append(
        f"- ON digested {digested}/{len(result.on.rows)} rounds; the rest stayed verbatim "
        "(below-threshold / pinned read / expand-exempt / min-savings)."
    )
    be = breakeven_analysis(result)
    lines.append("")
    lines.append("### Clawback break-even (how much re-expansion before the win is erased)\n")
    lines.append(
        f"- **New-tail basis** (forgiving, ignores re-bill): break-even expand mass "
        f"**{be.new_tail_breakeven:.0%}** — i.e. the model would have to re-expand "
        f"{be.new_tail_breakeven:.0%} of digested bytes before new-tail savings vanish."
    )

    def _fmt(f: float) -> str:
        return ">100% (wins even if all re-expand)" if f > 1.0 else f"{f:.0%}"

    lines.append(
        f"- **Cumulative basis** (the real cost driver): break-even expand rate "
        f"**{_fmt(be.cumulative_breakeven_worst)}** (worst ordering) – "
        f"**{_fmt(be.cumulative_breakeven_best)}** (best ordering) of "
        f"{be.digested_count} digested results."
    )
    lines.append(
        "- Pre-fix trace 950386d6 expanded ~4/9 digests (~44%, count-based, pre fix-a/fix-b). "
        "If the post-fix expand rate stays near that, compare it against the cumulative "
        "break-even band above to judge whether the feature pays."
    )
    return "\n".join(lines)


def main() -> None:
    """Run the offline mechanism simulation and print the markdown report."""
    result = asyncio.run(simulate())
    print(format_report(result))  # noqa: T201 — eval script stdout is the artifact


if __name__ == "__main__":
    main()
