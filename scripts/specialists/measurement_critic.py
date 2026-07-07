#!/usr/bin/env python3
"""Measurement/decision critic + mechanical trigger (ADR-0113 §3, FRE-833).

The second judgment specialist: adversarial scrutiny of an experiment or decision
for **confounds, missing controls, and unstated assumptions**. It targets master's
demonstrated weakness — failing to recognize a one-way door as one — so its
trigger is **mechanical, not a master judgment call**: :func:`classify_action`
decides, from an action's **structured fields only** (``kind`` / ``paths`` /
``params`` — never its free-text ``description``), whether the action falls in a
danger class (bulk substrate mutation, re-embed, schema/data migration,
always-ask-class deploy, control-plane/config mutation). On a class-matching
action the critic runs under the independence harness (raw artifact, fixed
template, injection-quarantined) and can return a blocking verdict.

**Advisory-only in FRE-833 (precise claim).** This delivers the mechanical trigger
+ the critic specialist + the guard *decision* (:func:`guard_action`), designed to
be called **before** an action actuates. It does **not** wire the deny into
master's real re-embed/deploy actuation — "real actuation cannot proceed without
passing the guard" is FRE-835 / the assembled-loop seam. Per ADR §5 this is
Phase A. No ``src/`` behavior — dev-process tooling under ``scripts/``.

Callable by hand::

    python -m scripts.specialists.measurement_critic --action action.json           # dry-run: print the sealed invocation
    python -m scripts.specialists.measurement_critic --action action.json --execute  # run the critic live + print the verdict
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import structlog

from scripts.specialists.harness import (
    DENY_ALL_CLEARANCE,
    ClearanceVerifier,
    OwnerClearance,
    PrimaryArtifact,
    SpecialistInvocation,
    SpecialistRunner,
    Verdict,
    assemble_invocation,
    blocks_merge,
    claude_headless_runner,
    load_template,
    merge_allowed,
    run_specialist,
)

# The fixed adversarial template this specialist runs from (harness-validated to
# live under .claude/agents/, content-versioned into every verdict).
MEASUREMENT_CRITIC_TEMPLATE_PATH = Path(".claude/agents/measurement-critic.md")

# --- the mechanical trigger classes (ADR-0113 §3) --------------------------

ActionClass = Literal[
    "bulk_substrate_mutation",
    "re_embed",
    "schema_migration",
    "data_migration",
    "always_ask_deploy",
    "control_plane_config",
]

# Deploy classes that keep a human stamp (ADR §2) — an always-ask deploy triggers
# the critic. The reversible standing-approval classes explicitly do NOT.
ALWAYS_ASK_DEPLOY_CLASSES: frozenset[str] = frozenset(
    {
        "gateway_rebuild",
        "es_type_change",
        "es_reindex",
        "postgres_migration",
        "cost",
        "budget",
        "governance",
    }
)
REVERSIBLE_DEPLOY_CLASSES: frozenset[str] = frozenset(
    {"pwa", "es_template_additive", "kibana_import"}
)

# Control-plane / config path families (ADR §2/§3), by real repo path. A diff or
# action touching any of these is a control-plane mutation — rulesets/workflows,
# governance + cost/budget config, model-routing config, and permission
# allowlists. Kept tight (not all of `.claude/` or `config/`) so an ordinary
# skill/doc edit does not fire the critic.
CONTROL_PLANE_PATH_PREFIXES: tuple[str, ...] = (
    ".github/",
    "config/governance/",
    "config/model_roles.yaml",
    "config/models",
    ".claude/MODEL_ROUTING_POLICY.md",
    ".claude/settings",
)

# Schema / data one-way-door paths.
SCHEMA_PATH_PREFIXES: tuple[str, ...] = ("docker/postgres/migrations/", "docker/postgres/init.sql")

# Action kinds. `re_embed` is its own class; the broader bulk-capable
# substrate-mutation kinds (a re-embed expressed as a raw cypher/graph bulk is
# caught here) drive the bulk class fail-closed.
RE_EMBED_KINDS: frozenset[str] = frozenset({"re_embed", "reembed", "re-embed"})
SUBSTRATE_MUTATION_KINDS: frozenset[str] = frozenset(
    {
        "re_embed",
        "reembed",
        "re-embed",
        "cypher_bulk",
        "cypher_update",
        "cypher_mutation",
        "graph_bulk_mutation",
        "graph_mutation",
        "sql_bulk",
        "sql_update",
        "bulk_mutation",
        "bulk_substrate_mutation",
    }
)
MIGRATION_KINDS: dict[str, ActionClass] = {
    "schema_migration": "schema_migration",
    "data_migration": "data_migration",
}
CONTROL_PLANE_KINDS: frozenset[str] = frozenset(
    {
        "config_mutation",
        "ruleset_change",
        "permission_change",
        "routing_change",
        "integration_mapping_change",
    }
)

# Affected-row/node count at/above which a substrate mutation is "bulk". For a
# bulk-capable kind, a MISSING or unparseable count is treated as bulk
# (fail-closed — a one-way door must not slip through on an omitted count).
BULK_COUNT_THRESHOLD = 100
_COUNT_PARAM_KEYS: tuple[str, ...] = ("affected_count", "node_count", "row_count", "count")

# The critic's standing reference frame — a GENERAL adversarial rubric first, then
# known confounds as NON-EXHAUSTIVE examples, so the critic generalizes to novel
# confounds instead of overfitting the known ones.
_STANDING_GUARDRAILS = """\
Adversarial scrutiny rubric (apply to ANY experiment or decision, not just the examples below):
- Confounds: is an observed effect attributable to an uncontrolled variable?
- Missing controls: is there a baseline / control group / A-B comparison? If absent, the claim is unproven.
- Unstated assumptions: what must be true for the conclusion to hold, and is it stated and checked?
- Reversibility / one-way door: can this action be cleanly undone? An irreversible action demands proof, not a hunch.
- Provenance of every cited number: under what configuration was it measured (model size, quantization,
  precision, dimension, environment)? A number measured under one config does not transfer to another.

Known confound examples (illustrative, NOT exhaustive — reason from the rubric, do not pattern-match these):
- Embedding-dimension ceiling: the separation sweet spot is ~1024 dims (FRE-694); re-embedding at native
  4096 is a costly, near-irreversible action for no measured gain.
- Local-vs-cloud precision: FRE-694's 8B numbers were local/Q4, not cloud full-precision — reading them as
  full-precision is a provenance confound.
- One-way doors hiding in process/config (a ruleset change, a permission/routing edit), not just in data.
"""

CriticGate = ClearanceVerifier  # alias for readability at the action altitude


@dataclasses.dataclass(frozen=True)
class ProposedAction:
    """An action master may be about to actuate, described for mechanical triage.

    Only ``kind``, ``paths``, and ``params`` drive triggering — ``description`` is
    read by the critic (as untrusted data) but is NEVER a trigger input, so
    triggering cannot depend on master noticing risk in prose.

    Attributes:
        kind: The structured action kind (e.g. ``re_embed``, ``deploy``,
            ``config_mutation``, ``cypher_bulk``).
        description: The experiment/decision text the critic scrutinizes.
        paths: Substrate/repo paths the action touches (path-based classes).
        params: Structured parameters (e.g. ``dimension``, ``deploy_class``,
            ``affected_count``).
    """

    kind: str
    description: str
    paths: tuple[str, ...] = ()
    params: Mapping[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class GuardOutcome:
    """The result of guarding an action with the critic.

    Attributes:
        triggered: Whether the mechanical trigger fired (the action is in a
            danger class).
        matched_classes: The danger classes the action matched.
        verdict: The critic's verdict when triggered, else ``None``.
        actuation_permitted: Whether the action may actuate — ``True`` when not
            triggered; when triggered, ``False`` unless the verdict permits it.
    """

    triggered: bool
    matched_classes: frozenset[ActionClass]
    verdict: Verdict | None
    actuation_permitted: bool


# --- the mechanical trigger (pure; structured-fields-only) ------------------


def _path_matches(paths: Sequence[str], prefixes: Sequence[str]) -> bool:
    """Return whether any path starts with any prefix (normalized separators)."""
    normalized = [p.strip().replace("\\", "/").removeprefix("./") for p in paths]
    return any(p.startswith(prefix) for p in normalized for prefix in prefixes)


def _parse_count(params: Mapping[str, str]) -> int | None:
    """Return the first parseable affected-count param, or ``None`` if absent/bad."""
    for key in _COUNT_PARAM_KEYS:
        if key in params:
            try:
                return int(str(params[key]).strip())
            except (ValueError, TypeError):
                return None
    return None


def classify_action(action: ProposedAction) -> frozenset[ActionClass]:
    """Return the danger classes an action matches — from structured fields ONLY.

    This is the load-bearing "not master's discretion" property (AC-6): triggering
    is a pure function of ``kind`` / ``paths`` / ``params`` and never of the
    free-text ``description``, so a scary description with benign fields does not
    fire and a bland description with a re-embed kind does.

    Args:
        action: The proposed action.

    Returns:
        The set of matched :data:`ActionClass` values (empty if none).
    """
    classes: set[ActionClass] = set()
    kind = action.kind.strip().lower()

    if kind in RE_EMBED_KINDS:
        classes.add("re_embed")

    if kind in SUBSTRATE_MUTATION_KINDS:
        count = _parse_count(action.params)
        # Fail-closed: a bulk-capable kind with a missing/unparseable count is bulk.
        if count is None or count >= BULK_COUNT_THRESHOLD:
            classes.add("bulk_substrate_mutation")

    if kind in MIGRATION_KINDS:
        classes.add(MIGRATION_KINDS[kind])
    if _path_matches(action.paths, SCHEMA_PATH_PREFIXES):
        classes.add("schema_migration")

    if kind in CONTROL_PLANE_KINDS or _path_matches(action.paths, CONTROL_PLANE_PATH_PREFIXES):
        classes.add("control_plane_config")

    if kind == "deploy":
        deploy_class = str(action.params.get("deploy_class", "")).strip().lower()
        if deploy_class in ALWAYS_ASK_DEPLOY_CLASSES:
            classes.add("always_ask_deploy")

    return frozenset(classes)


def triggers_critic(action: ProposedAction) -> bool:
    """Return whether an action is in a danger class (the critic must run)."""
    return bool(classify_action(action))


# --- the critic specialist (runs under the harness) -------------------------


def build_critic_artifact(
    action: ProposedAction, *, repo_root: Path = Path(".")
) -> PrimaryArtifact:
    """Build the raw primary artifact for an action — the action is untrusted data.

    Args:
        action: The proposed action under scrutiny.
        repo_root: Unused placeholder for signature symmetry with other
            specialists (kept for future repo-checked reference material).

    Returns:
        The assembled :class:`PrimaryArtifact`: the action's structured fields +
        description as untrusted data, the standing guardrails as trusted
        reference.
    """
    del repo_root  # no repo read needed; the standing guardrails are the reference
    params_text = "\n".join(f"  {key}: {value}" for key, value in sorted(action.params.items()))
    untrusted = (
        f"PROPOSED ACTION\n"
        f"kind: {action.kind}\n"
        f"paths: {', '.join(action.paths) if action.paths else '(none)'}\n"
        f"params:\n{params_text or '  (none)'}\n\n"
        f"DESCRIPTION / EXPERIMENT:\n{action.description}"
    )
    return PrimaryArtifact(
        kind="proposed_action",
        source=f"proposed-action:{action.kind}",
        trusted_reference=_STANDING_GUARDRAILS,
        untrusted=untrusted,
    )


def build_invocation(
    action: ProposedAction,
    *,
    template_path: Path = MEASUREMENT_CRITIC_TEMPLATE_PATH,
    repo_root: Path = Path("."),
) -> SpecialistInvocation:
    """Assemble the critic invocation from the fixed template + the action.

    Args:
        action: The proposed action under scrutiny.
        template_path: The fixed critic template.
        repo_root: Repository root for template resolution.

    Returns:
        The assembled :class:`SpecialistInvocation`.
    """
    template = load_template(repo_root / template_path)
    artifact = build_critic_artifact(action, repo_root=repo_root)
    return assemble_invocation(template, artifact)


def critique_action(
    action: ProposedAction,
    *,
    specialist_runner: SpecialistRunner,
    template_path: Path = MEASUREMENT_CRITIC_TEMPLATE_PATH,
    repo_root: Path = Path("."),
) -> Verdict:
    """Run the critic over an action and return its verdict.

    The only content input is ``action``; ``specialist_runner`` is an IO seam and
    there is no framing/summary parameter, so master cannot prose the critic.

    Args:
        action: The proposed action under scrutiny.
        specialist_runner: The specialist runner seam (fake in tests; ``claude
            -p`` in production).
        template_path: The fixed critic template.
        repo_root: Repository root.

    Returns:
        The critic :class:`Verdict`.
    """
    inv = build_invocation(action, template_path=template_path, repo_root=repo_root)
    return run_specialist(inv, specialist_runner)


def actuation_permitted(
    verdict: Verdict,
    clearance: OwnerClearance | None = None,
    *,
    verifier: CriticGate = DENY_ALL_CLEARANCE,
) -> bool:
    """Return whether an action may actuate given the critic verdict.

    Same gate semantics as the merge gate (ADR-0113 §3): an APPROVE permits the
    action; a blocking verdict is terminal under the default deny-all verifier —
    there is no master-supplied parameter that lifts it. FRE-835 injects the
    owner-signal verifier for the owner-only escape hatch.

    Args:
        verdict: The critic verdict.
        clearance: An optional owner clearance for a blocking verdict.
        verifier: The clearance verifier seam (default denies all).

    Returns:
        ``True`` iff the action may actuate.
    """
    return merge_allowed(verdict, clearance, verifier=verifier)


def guard_action(
    action: ProposedAction,
    *,
    specialist_runner: SpecialistRunner,
    template_path: Path = MEASUREMENT_CRITIC_TEMPLATE_PATH,
    repo_root: Path = Path("."),
    clearance: OwnerClearance | None = None,
    verifier: CriticGate = DENY_ALL_CLEARANCE,
) -> GuardOutcome:
    """Guard an action: fire the critic on a class match, and gate on its verdict.

    A pre-actuation decision primitive (AC-6). Not in a danger class → the critic
    does not run and the action is permitted (the critic gates only its class).
    In a class → the critic runs and a blocking verdict makes ``actuation_permitted``
    False (terminal under the default deny-all verifier).

    Args:
        action: The proposed action.
        specialist_runner: The specialist runner seam.
        template_path: The fixed critic template.
        repo_root: Repository root.
        clearance: An optional owner clearance for a blocking verdict.
        verifier: The clearance verifier seam (default denies all).

    Returns:
        The :class:`GuardOutcome`.
    """
    matched = classify_action(action)
    if not matched:
        return GuardOutcome(
            triggered=False, matched_classes=matched, verdict=None, actuation_permitted=True
        )
    verdict = critique_action(
        action,
        specialist_runner=specialist_runner,
        template_path=template_path,
        repo_root=repo_root,
    )
    return GuardOutcome(
        triggered=True,
        matched_classes=matched,
        verdict=verdict,
        actuation_permitted=actuation_permitted(verdict, clearance, verifier=verifier),
    )


# --- CLI -------------------------------------------------------------------


def _load_action(path: Path) -> ProposedAction:
    """Load a :class:`ProposedAction` from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"action file {path} must be a JSON object")
    raw_params = data.get("params") or {}
    params = {str(k): str(v) for k, v in raw_params.items()} if isinstance(raw_params, dict) else {}
    raw_paths = data.get("paths") or []
    paths = tuple(str(p) for p in raw_paths) if isinstance(raw_paths, list) else ()
    return ProposedAction(
        kind=str(data.get("kind") or ""),
        description=str(data.get("description") or ""),
        paths=paths,
        params=params,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Classifies an action, then dry-runs or runs the critic."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--action", required=True, help="Path to a JSON ProposedAction file.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the critic live via claude -p (default: dry-run, no LLM call).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args(argv)

    logger = structlog.get_logger(__name__)
    trace_id = str(uuid.uuid4())

    action = _load_action(Path(args.action))
    matched = classify_action(action)
    logger.info(
        "measurement_critic_triage",
        trace_id=trace_id,
        kind=action.kind,
        matched_classes=sorted(matched),
        triggered=bool(matched),
    )

    if not matched:
        print("not in a critic-triggering class — no scrutiny required")
        return 0

    inv = build_invocation(action)
    if not args.execute:
        print(inv.prompt)
        return 0

    verdict = run_specialist(inv, claude_headless_runner())
    permitted = actuation_permitted(verdict)
    logger.info(
        "measurement_critic_verdict",
        trace_id=trace_id,
        kind=action.kind,
        decision=verdict.decision,
        blocks=blocks_merge(verdict),
        actuation_permitted=permitted,
        findings=len(verdict.findings),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "decision": verdict.decision,
                    "actuation_permitted": permitted,
                    "matched_classes": sorted(matched),
                    "findings": [
                        {"severity": f.severity, "category": f.category, "summary": f.summary}
                        for f in verdict.findings
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"VERDICT: {verdict.decision} (actuation_permitted={permitted})")
        for finding in verdict.findings:
            print(f"  [{finding.severity}] {finding.category}: {finding.summary}")
    return 0 if permitted else 2


if __name__ == "__main__":
    sys.exit(main())
