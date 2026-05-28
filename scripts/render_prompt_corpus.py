#!/usr/bin/env python3
"""Prompt corpus renderer — ADR-0078 D3 / FRE-404.

Produces docs/reference/PROMPT_CORPUS.md: a human-readable, token-annotated,
source-referenced document of every prompt component in the harness.

Run:  uv run python scripts/render_prompt_corpus.py
Make: make render-prompt-corpus
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "personal_agent"
SKILLS_DIR = REPO_ROOT / "docs" / "skills"
OUTPUT_PATH = REPO_ROOT / "docs" / "reference" / "PROMPT_CORPUS.md"

# ---------------------------------------------------------------------------
# Token counting — delegate to the unified counter
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Count tokens via tiktoken cl100k_base (the unified counter)."""
    try:
        from personal_agent.llm_client.token_counter import estimate_tokens

        return estimate_tokens(text)
    except ImportError:
        # Fallback if the module is not yet importable (bootstrapping)
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)) if text and text.strip() else 0


# ---------------------------------------------------------------------------
# AST extraction helpers
# ---------------------------------------------------------------------------


def _extract_string_constant(filepath: Path, name: str) -> str:
    """Extract a module-level string constant by name using AST parsing.

    Args:
        filepath: Absolute path to the Python source file.
        name: Variable name (e.g. ``_EXTRACTION_PROMPT_TEMPLATE``).

    Returns:
        The string value of the constant.

    Raises:
        ValueError: If the constant is not found or is not a string literal.
    """
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != name:
                continue
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                return val.value
            if isinstance(val, ast.JoinedStr):
                # f-string: return the raw source template (strips surrounding f""" markers)
                segment = ast.get_source_segment(source, val) or ""
                # Strip leading f / b / r prefix and surrounding quotes
                segment = segment.lstrip("fFbBrRuU")
                if segment.startswith('"""') or segment.startswith("'''"):
                    segment = segment[3:]
                    if segment.endswith('"""') or segment.endswith("'''"):
                        segment = segment[:-3]
                elif segment.startswith(('"', "'")):
                    segment = segment[1:]
                    if segment.endswith(('"', "'")):
                        segment = segment[:-1]
                return f"[f-string template]\n{segment}"
    raise ValueError(f"String constant {name!r} not found in {filepath}")


def _find_definition_line(filepath: Path, name: str) -> int:
    """Return the 1-based line number of a module-level assignment."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.lineno
    return 0


def _find_function_line(filepath: Path, name: str) -> int:
    """Return the 1-based line number of a module-level function definition."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node.lineno
    return 0


def _relative(path: Path) -> str:
    """Return repo-relative path string."""
    return str(path.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# Taxonomy entry types
# ---------------------------------------------------------------------------


class ConstantEntry(NamedTuple):
    component_id: str
    filepath: Path
    varname: str
    cache_tier: str


class FunctionEntry(NamedTuple):
    component_id: str
    filepath: Path
    funcname: str
    cache_tier: str
    description: str  # human summary shown instead of extracted text


class ClassDocEntry(NamedTuple):
    """DSPy signature — docstring is the prompt contract."""

    component_id: str
    filepath: Path
    classname: str
    cache_tier: str


# ---------------------------------------------------------------------------
# Component taxonomy (spec §1.1)
# ---------------------------------------------------------------------------

LEAF_PROMPTS: list[ConstantEntry | FunctionEntry | ClassDocEntry] = [
    ConstantEntry(
        "entity_extraction_system",
        SRC_ROOT / "second_brain" / "entity_extraction.py",
        "_EXTRACTION_SYSTEM_PROMPT",
        "STATIC",
    ),
    ConstantEntry(
        "entity_extraction_template",
        SRC_ROOT / "second_brain" / "entity_extraction.py",
        "_EXTRACTION_PROMPT_TEMPLATE",
        "SEMI_STATIC",
    ),
    ConstantEntry(
        "context_compressor_system",
        SRC_ROOT / "orchestrator" / "context_compressor.py",
        "_COMPRESSOR_SYSTEM_PROMPT",
        "STATIC",
    ),
    ConstantEntry(
        "router_system_prompt",
        SRC_ROOT / "orchestrator" / "prompts.py",
        "ROUTER_SYSTEM_PROMPT",
        "STATIC",
    ),
    ConstantEntry(
        "tool_rules",
        SRC_ROOT / "orchestrator" / "prompts.py",
        "_TOOL_RULES",
        "STATIC",
    ),
    ConstantEntry(
        "tool_use_native_prompt",
        SRC_ROOT / "orchestrator" / "prompts.py",
        "TOOL_USE_NATIVE_PROMPT",
        "STATIC",
    ),
    ConstantEntry(
        "tool_use_injected_prompt",
        SRC_ROOT / "orchestrator" / "prompts.py",
        "TOOL_USE_PROMPT_INJECTED",
        "STATIC",
    ),
    FunctionEntry(
        "tool_awareness_prompt",
        SRC_ROOT / "orchestrator" / "prompts.py",
        "get_tool_awareness_prompt",
        "SEMI_STATIC",
        (
            "Dynamically generated listing of available tools grouped by category. "
            "Cached for 60 s. Includes tool counts per category and key capability "
            "declarations (web search, file access, etc.). Content varies with the "
            "active tool registry — changes when tools are added or governance config "
            "is reloaded. Cannot be extracted statically; see function source."
        ),
    ),
    FunctionEntry(
        "operator_stanza",
        SRC_ROOT / "orchestrator" / "prompts.py",
        "get_owner_stanza",
        "SEMI_STATIC",
        (
            "Async function. Renders a compact Markdown stanza with the owner's "
            "known profile fields (name, location, pronouns, role, languages) from "
            "Neo4j. Content varies per connected user — queried every turn via a "
            "sub-millisecond Neo4j MERGE. Cannot be extracted statically."
        ),
    ),
    ClassDocEntry(
        "reflection_dspy_signature",
        SRC_ROOT / "captains_log" / "reflection_dspy.py",
        "GenerateReflection",
        "STATIC",
    ),
    ConstantEntry(
        "reflection_manual_fallback",
        SRC_ROOT / "captains_log" / "reflection.py",
        "REFLECTION_PROMPT",
        "STATIC",
    ),
    ConstantEntry(
        "html_generation_system",
        SRC_ROOT / "tools" / "artifact_tools.py",
        "_HTML_GENERATION_SYSTEM_PROMPT",
        "STATIC",
    ),
    ConstantEntry(
        "gateway_persona",
        SRC_ROOT / "gateway" / "chat_api.py",
        "_SYSTEM_PROMPT",
        "STATIC",
    ),
]

# Orchestrator composed prompt component taxonomy (spec §1.2)
ORCHESTRATOR_COMPONENTS = [
    (
        "deployment_context",
        "SEMI_STATIC",
        "executor.py",
        "1840–1850",
        "VPS/cloud deployment environment variables injected into the system prompt.",
    ),
    (
        "operator_stanza",
        "SEMI_STATIC",
        "executor.py",
        "1852–1858",
        "Owner identity + instructions from Neo4j profile (see operator_stanza leaf prompt).",
    ),
    (
        "skill_index",
        "SEMI_STATIC",
        "executor.py",
        "1860–1993",
        "Active skill metadata + matched skill bodies from docs/skills/*.md. "
        "Capped at 2,048 tokens. Populated by orchestrator/skills.py.",
    ),
    (
        "memory_section",
        "DYNAMIC",
        "executor.py",
        "2126–2149",
        "Recalled memory nodes for this turn. Varies per turn. ⚠ DYNAMIC — "
        "marks the end of the cacheable prefix.",
    ),
    (
        "tool_awareness",
        "SEMI_STATIC",
        "executor.py",
        "2151–2171",
        "Tool list + capabilities (see tool_awareness_prompt leaf prompt). "
        "⚠ PREPENDED late at line 2171 — inserted after memory_section, "
        "violating the stable-prefix ordering invariant.",
    ),
    (
        "tool_use_rules",
        "STATIC",
        "executor.py",
        "2171–2194",
        "_TOOL_RULES + TOOL_USE_PROMPT_INJECTED (see leaf prompts).",
    ),
    (
        "decomposition_instructions",
        "STATIC",
        "executor.py",
        "2176–2194",
        "Task decomposition guidance for SINGLE/HYBRID/DECOMPOSE/DELEGATE paths.",
    ),
]


# ---------------------------------------------------------------------------
# DSPy signature extraction
# ---------------------------------------------------------------------------


def _extract_dspy_signature_doc(filepath: Path, classname: str) -> str:
    """Extract the docstring + field descriptors from a DSPy Signature class."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != classname:
            continue
        parts: list[str] = []
        # Class docstring
        docstring = ast.get_docstring(node)
        if docstring:
            parts.append(f"[Class docstring]\n{docstring}")
        # Field descriptors (dspy.InputField / dspy.OutputField with desc=)
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            field_name = stmt.target.id if isinstance(stmt.target, ast.Name) else "?"
            if not isinstance(stmt.value, ast.Call):
                continue
            for kw in stmt.value.keywords:
                if kw.arg == "desc" and isinstance(kw.value, ast.Constant):
                    parts.append(f"[{field_name}] {kw.value.value}")
        return "\n\n".join(parts)
    raise ValueError(f"Class {classname!r} not found in {filepath}")


# ---------------------------------------------------------------------------
# Skill document parsing
# ---------------------------------------------------------------------------


def _read_skill_doc(path: Path) -> tuple[str, str]:
    """Return (title, body_without_frontmatter) for a skill .md file."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    # Strip YAML frontmatter if present
    if lines and lines[0].strip() == "---":
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
        if end:
            lines = lines[end + 1 :]
    body = "\n".join(lines).strip()
    # Title from first heading
    title = path.stem
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return title, body


# ---------------------------------------------------------------------------
# Git revision
# ---------------------------------------------------------------------------


def _git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _git_commit_timestamp() -> str:
    """Return the ISO-8601 timestamp of the current HEAD commit (stable per revision)."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

TIER_SYMBOL = {"STATIC": "🟢", "SEMI_STATIC": "🟡", "DYNAMIC": "🔴"}
TIER_NOTE = {
    "STATIC": "never varies at runtime",
    "SEMI_STATIC": "varies at session boundary",
    "DYNAMIC": "varies per turn",
}


def _render_corpus() -> str:
    warnings: list[str] = []
    sections: list[str] = []
    summary_rows: list[tuple[str, str, str, str]] = []  # id, tier, tokens, source

    # -----------------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------------
    sha = _git_short_sha()
    commit_ts = _git_commit_timestamp()
    sections.append(
        f"# Prompt Corpus — Seshat Personal Agent\n\n"
        f"> Source revision: `{sha}` · Committed: {commit_ts}  \n"
        f"> ADR: ADR-0078 · Spec: `docs/specs/PROMPT_MANAGEMENT_SPEC.md`  \n"
        f">\n"
        f"> **Cache tier key**  \n"
        f"> 🟢 STATIC — never varies at runtime  \n"
        f"> 🟡 SEMI_STATIC — varies at session boundary  \n"
        f"> 🔴 DYNAMIC — varies per turn (end of cacheable prefix)\n"
    )

    # -----------------------------------------------------------------------
    # Leaf prompts
    # -----------------------------------------------------------------------
    leaf_sections: list[str] = []

    for entry in LEAF_PROMPTS:
        component_id = entry.component_id
        filepath = entry.filepath
        cache_tier = entry.cache_tier
        tier_sym = TIER_SYMBOL[cache_tier]

        if not filepath.exists():
            warnings.append(f"⚠ Source file not found for {component_id!r}: {filepath}")
            summary_rows.append((component_id, f"{tier_sym} {cache_tier}", "N/A", "FILE NOT FOUND"))
            continue

        if isinstance(entry, ConstantEntry):
            try:
                text = _extract_string_constant(filepath, entry.varname)
                line = _find_definition_line(filepath, entry.varname)
            except ValueError as exc:
                warnings.append(f"⚠ {exc}")
                text = "(extraction failed)"
                line = 0
            token_count = _estimate_tokens(text)
            source_ref = f"`{_relative(filepath)}:{line}`"
            summary_rows.append(
                (component_id, f"{tier_sym} {cache_tier}", f"{token_count:,}", source_ref)
            )
            leaf_sections.append(
                f"### `{component_id}`\n\n"
                f"| | |\n"
                f"|---|---|\n"
                f"| **Cache tier** | {tier_sym} {cache_tier} — {TIER_NOTE[cache_tier]} |\n"
                f"| **Token count** | {token_count:,} |\n"
                f"| **Source** | {source_ref} |\n\n"
                f"```\n{text}\n```\n"
            )

        elif isinstance(entry, FunctionEntry):
            line = _find_function_line(filepath, entry.funcname)
            source_ref = f"`{_relative(filepath)}:{line}`"
            summary_rows.append((component_id, f"{tier_sym} {cache_tier}", "runtime", source_ref))
            leaf_sections.append(
                f"### `{component_id}`\n\n"
                f"| | |\n"
                f"|---|---|\n"
                f"| **Cache tier** | {tier_sym} {cache_tier} — {TIER_NOTE[cache_tier]} |\n"
                f"| **Token count** | runtime (see description) |\n"
                f"| **Source** | {source_ref} |\n\n"
                f"_{entry.description}_\n"
            )

        elif isinstance(entry, ClassDocEntry):
            line = _find_function_line(filepath, entry.classname)  # reuse — finds class too
            # Search for class def specifically
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == entry.classname:
                    line = node.lineno
                    break
            try:
                text = _extract_dspy_signature_doc(filepath, entry.classname)
            except ValueError as exc:
                warnings.append(f"⚠ {exc}")
                text = "(extraction failed)"
            token_count = _estimate_tokens(text)
            source_ref = f"`{_relative(filepath)}:{line}`"
            summary_rows.append(
                (component_id, f"{tier_sym} {cache_tier}", f"{token_count:,}", source_ref)
            )
            leaf_sections.append(
                f"### `{component_id}`\n\n"
                f"| | |\n"
                f"|---|---|\n"
                f"| **Cache tier** | {tier_sym} {cache_tier} — {TIER_NOTE[cache_tier]} |\n"
                f"| **Token count** | {token_count:,} (docstring + field descriptors) |\n"
                f"| **Source** | {source_ref} |\n\n"
                f"```\n{text}\n```\n"
            )

    # -----------------------------------------------------------------------
    # Skill documents
    # -----------------------------------------------------------------------
    skill_files = sorted(SKILLS_DIR.glob("*.md"))
    skill_rows: list[tuple[str, int, str]] = []
    skill_sections: list[str] = []

    for skill_path in skill_files:
        if skill_path.name in ("SKILL_TEMPLATE.md", "EMPIRICAL_TEST_RESULTS.md"):
            continue
        title, body = _read_skill_doc(skill_path)
        tok = _estimate_tokens(body)
        rel = _relative(skill_path)
        skill_rows.append((title, tok, rel))
        skill_sections.append(
            f"### {title}\n\n"
            f"| | |\n"
            f"|---|---|\n"
            f"| **Cache tier** | 🟡 SEMI_STATIC — included when skill is matched |\n"
            f"| **Token count** | {tok:,} |\n"
            f"| **Source** | `{rel}` |\n\n"
            f"```markdown\n{body}\n```\n"
        )

    # -----------------------------------------------------------------------
    # Summary table
    # -----------------------------------------------------------------------
    skill_total = sum(t for _, t, _ in skill_rows)
    summary_lines = [
        "## Summary\n",
        "### Leaf Prompts\n",
        "| component_id | cache tier | tokens | source |",
        "|---|---|---|---|",
    ]
    for cid, tier, tok, src in summary_rows:
        summary_lines.append(f"| `{cid}` | {tier} | {tok} | {src} |")

    summary_lines += [
        "",
        "### Skill Documents\n",
        f"Total skill docs: {len(skill_rows)} · Total tokens (all skills): {skill_total:,}  ",
        "*(Only matched skills are injected per turn; max 2,048 tokens via `skill_index` budget)*\n",
        "| skill | tokens | source |",
        "|---|---|---|",
    ]
    for title, tok, rel in skill_rows:
        summary_lines.append(f"| {title} | {tok:,} | `{rel}` |")

    sections.append("\n".join(summary_lines))

    # -----------------------------------------------------------------------
    # Orchestrator composition skeleton
    # -----------------------------------------------------------------------
    orch_lines = [
        "## Orchestrator Composition Skeleton\n",
        "**Callsite:** `orchestrator.primary`  \n"
        "**Source:** `src/personal_agent/orchestrator/executor.py:1835–2244`  \n"
        "**Note:** This prompt is assembled imperatively — it is not a string constant. "
        "The skeleton below shows the ordered components and their cache tiers. "
        "Components must be in STATIC/SEMI_STATIC order before DYNAMIC for KV-cache "
        "prefix stability (ADR-0038).  \n",
        "```",
    ]
    for cid, tier, src_file, lines, desc in ORCHESTRATOR_COMPONENTS:
        sym = TIER_SYMBOL[tier]
        warn = "  ⚠ OUT OF ORDER" if cid == "tool_awareness" else ""
        orch_lines.append(f"[{sym} {tier:<12}] {cid:<28} executor.py:{lines}{warn}")
    orch_lines.append("```\n")

    orch_lines.append("### Component descriptions\n")
    for cid, tier, src_file, lines, desc in ORCHESTRATOR_COMPONENTS:
        sym = TIER_SYMBOL[tier]
        orch_lines.append(f"**`{cid}`** ({sym} {tier})  \n{desc}\n")

    orch_lines.append(
        "> ⚠ **Cache-erosion risk**: `tool_awareness` (SEMI_STATIC) is prepended at line 2171, "
        "**after** `memory_section` (DYNAMIC). This means the DYNAMIC content precedes "
        "a SEMI_STATIC block, breaking the stable-prefix invariant that ADR-0038 assumed. "
        "See P2 (FRE-406) for the cache-erosion measurement dashboard."
    )
    sections.append("\n".join(orch_lines))

    # -----------------------------------------------------------------------
    # Leaf prompt sections
    # -----------------------------------------------------------------------
    sections.append("## Leaf Prompts\n\n" + "\n---\n\n".join(leaf_sections))

    # -----------------------------------------------------------------------
    # Skill document sections
    # -----------------------------------------------------------------------
    sections.append(
        "## Skill Documents (`docs/skills/*.md`)\n\n"
        "Skill bodies are injected into the `skill_index` component when the skill "
        "router matches them for a given turn. The index is capped at 2,048 tokens.  \n\n"
        + "\n---\n\n".join(skill_sections)
    )

    # -----------------------------------------------------------------------
    # Warnings
    # -----------------------------------------------------------------------
    if warnings:
        warn_section = "## Warnings\n\n" + "\n".join(f"- {w}" for w in warnings)
        sections.append(warn_section)

    return "\n\n---\n\n".join(sections) + "\n"


def main() -> None:
    """Render the prompt corpus and write to docs/reference/PROMPT_CORPUS.md."""
    print("Rendering prompt corpus…", file=sys.stderr)
    output = _render_corpus()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    tok_total = sum(
        _estimate_tokens(line)
        for line in output.splitlines()
        if line and not line.startswith("|") and not line.startswith("#")
    )
    print(f"Written: {OUTPUT_PATH.relative_to(REPO_ROOT)}", file=sys.stderr)
    print(f"Output size: {len(output):,} bytes", file=sys.stderr)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
