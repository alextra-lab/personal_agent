"""Config-parameter usage audit (FRE-893, ADR-0099 hygiene).

Categorizes every ``AppConfig`` field into one of four buckets, backed by evidence:

* ``load-bearing`` — read somewhere in production code AND overridden away from its
  default somewhere in the repo.
* ``read-but-never-overridden`` — read, but never seen overridden — a hardcode candidate.
* ``never-read`` — no read evidence anywhere — a dead-config candidate.
* ``writer-pinned-guardrail`` — a secret or safety-critical field; never a removal
  candidate regardless of read/override evidence.

Read evidence combines three sources: a ``git grep`` over ``settings.<field>`` /
``getattr(settings, "<field>")`` usage (tagged by root — ``src``, ``scripts``, ``tests`` —
so a field touched only by tests/tooling is not mistaken for production load-bearing);
whether ``settings.py`` itself consults the field via ``self.<field>`` inside a
cross-field validator; and whether ``config/substrate.yaml`` names the field via a
``source: "setting:<field>"`` reference (the one dynamic-``getattr`` resolution path in
the codebase, resolved by reading the manifest directly rather than tracing the runtime
call — a codex plan-review pass on this ticket found that a literal-string grep alone
cannot see this path).

Override evidence combines two sources: the 5 ``docker-compose*.yml`` files' parsed
``services.*.environment`` blocks (a real deployment-config surface), and
``tests/conftest.py``'s ``os.environ.setdefault("AGENT_...")`` test-substrate defaults
(FRE-375) — tagged separately (``"compose"`` vs ``"test-substrate"``) so a reader can
tell a real deployment override from a test-isolation default. The actual deployed
``.env`` (gitignored, not in this repo) is NOT visible to this audit — a field with no
in-repo override evidence is not proof it is never overridden in production, only that
there is no repo-visible override (stated explicitly in the generated report).

Run from the repo root::

    uv run python -m scripts.audit.config_usage_audit generate
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic.fields import FieldInfo

from personal_agent.config.settings import AppConfig
from scripts.audit.config_inventory import (
    INVENTORY_DOC,
    REPO_ROOT,
    _accepted_env,
    _is_secret,
)

SETTINGS_FILE = REPO_ROOT / "src" / "personal_agent" / "config" / "settings.py"
SUBSTRATE_MANIFEST = REPO_ROOT / "config" / "substrate.yaml"
CONFTEST_FILE = REPO_ROOT / "tests" / "conftest.py"
REPORT_DOC = REPO_ROOT / "docs" / "research" / "2026-07-16-fre-893-config-parameter-usage-audit.md"

# The 5 compose files present in this repo (verified via `ls docker-compose*.yml` —
# a plan-review pass on this ticket found `docker-compose.study.yml` missing from an
# earlier draft that only checked the 4 better-known files).
COMPOSE_FILES: tuple[str, ...] = (
    "docker-compose.yml",
    "docker-compose.cloud.yml",
    "docker-compose.eval.yml",
    "docker-compose.test.yml",
    "docker-compose.study.yml",
)

SEARCH_ROOTS: tuple[str, ...] = ("src", "scripts", "tests")

# The one non-secret field forced into the guardrail bucket: consumed only via a
# self-referential validator (`_validate_owner_storage_allowlist`) that gates which
# hosts substrate URLs may point at. Read/override evidence alone would otherwise
# under-represent its importance.
_GUARDRAIL_EXTRA: frozenset[str] = frozenset({"owner_storage_allowlist"})

CATEGORIES: frozenset[str] = frozenset(
    {"load-bearing", "read-but-never-overridden", "never-read", "writer-pinned-guardrail"}
)

_MANIFEST_SETTING = re.compile(r'source:\s*"setting:(\w+)"')
_CONFTEST_SETDEFAULT = re.compile(r'os\.environ\.setdefault\(\s*["\'](AGENT_\w+)["\']')


@dataclass(frozen=True)
class FieldUsage:
    """Evidence-backed categorization for a single AppConfig field."""

    name: str
    reads: dict[str, list[str]] = dataclass_field(default_factory=dict)
    internal_self_read: bool = False
    manifest_read: bool = False
    overrides: list[tuple[str, str]] = dataclass_field(default_factory=list)
    category: str = "never-read"


def _git_grep(pattern: str, roots: tuple[str, ...]) -> list[str]:
    """`git grep -n -P <pattern> -- <roots>`; [] on no matches or missing git."""
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "-P", pattern, "--", *roots],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    if result.returncode not in (0, 1):  # 1 == no matches, not an error
        return []
    return [line for line in result.stdout.splitlines() if line]


@lru_cache(maxsize=1)
def _all_settings_usage_lines() -> tuple[str, ...]:
    """Every `settings.<x>` / `getattr(settings, "<x>")` line for ANY field name.

    One `git grep` over the whole codebase instead of one per field — called once,
    cached, then filtered per-field in Python. Replaces 311 subprocess spawns (one per
    `AppConfig` field) with 1; a code-review pass on this ticket found the per-field
    grep dominating `generate`'s runtime and flagged it as an efficiency defect.
    """
    pattern = r'settings\.\w+\b|getattr\(settings,\s*["\']\w+["\']'
    return tuple(_git_grep(pattern, SEARCH_ROOTS))


def external_reads(name: str) -> dict[str, list[str]]:
    """`settings.<name>` / `getattr(settings, "<name>")` hits, keyed by top-level root.

    Excludes hits inside `config/settings.py` itself (the field's own definition).
    """
    field_pattern = re.compile(
        rf'settings\.{re.escape(name)}\b|getattr\(settings,\s*["\']{re.escape(name)}["\']'
    )
    settings_rel = str(SETTINGS_FILE.relative_to(REPO_ROOT))
    by_root: dict[str, list[str]] = {}
    for line in _all_settings_usage_lines():
        if line.startswith(settings_rel + ":"):
            continue
        if not field_pattern.search(line):
            continue
        root = line.split("/", 1)[0]
        by_root.setdefault(root, []).append(line)
    return by_root


@lru_cache(maxsize=1)
def _settings_source() -> str:
    return SETTINGS_FILE.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _manifest_setting_fields() -> frozenset[str]:
    if not SUBSTRATE_MANIFEST.exists():
        return frozenset()
    return frozenset(_MANIFEST_SETTING.findall(SUBSTRATE_MANIFEST.read_text(encoding="utf-8")))


def internal_self_read(name: str) -> bool:
    """Whether `settings.py` consults the field via `self.<name>` (cross-field validators)."""
    return re.search(rf"self\.{re.escape(name)}\b", _settings_source()) is not None


def manifest_read(name: str) -> bool:
    """Whether `config/substrate.yaml` names the field via `source: "setting:<name>"`."""
    return name in _manifest_setting_fields()


@lru_cache(maxsize=None)
def _compose_env_keys(path: Path) -> frozenset[str]:
    """Every env-var key set in any service's `environment:` block of a compose file.

    Cached per path — called once per one of 311 fields per compose file, but the
    file's parsed key set is invariant across calls within a process.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return frozenset()
    if not isinstance(doc, dict):
        return frozenset()
    keys: set[str] = set()
    for service in (doc.get("services") or {}).values():
        if not isinstance(service, dict):
            continue
        env = service.get("environment")
        if isinstance(env, dict):
            keys.update(str(k) for k in env)
        elif isinstance(env, list):
            for entry in env:
                if isinstance(entry, str) and "=" in entry:
                    keys.add(entry.split("=", 1)[0].strip())
    return frozenset(keys)


@lru_cache(maxsize=1)
def _conftest_env_keys() -> frozenset[str]:
    """Every `AGENT_*` key set via `os.environ.setdefault(...)` in tests/conftest.py."""
    if not CONFTEST_FILE.exists():
        return frozenset()
    text = CONFTEST_FILE.read_text(encoding="utf-8")
    return frozenset(_CONFTEST_SETDEFAULT.findall(text))


def override_locations(name: str, field: FieldInfo) -> list[tuple[str, str]]:
    """`(source, kind)` pairs where this field's env var is set away from its default.

    `kind="compose"` for a docker-compose*.yml `environment:` block; `kind="test-substrate"`
    for a `tests/conftest.py` `os.environ.setdefault("AGENT_...")` default.
    """
    accepted = _accepted_env(name, field)
    found: list[tuple[str, str]] = []

    for compose in COMPOSE_FILES:
        path = REPO_ROOT / compose
        if not path.exists():
            continue
        if accepted & _compose_env_keys(path):
            found.append((compose, "compose"))

    if accepted & _conftest_env_keys():
        found.append((str(CONFTEST_FILE.relative_to(REPO_ROOT)), "test-substrate"))

    return found


def is_guardrail(name: str, field: FieldInfo) -> bool:
    """Secret (regex heuristic OR authoritative schema flag) or the storage-allowlist guardrail.

    The schema flag (`json_schema_extra={"secret": True}`) is authoritative and catches
    fields the regex heuristic in `config_inventory.py` misses (the `managed_*` endpoint/
    URI/token fields — a codex plan-review pass on this ticket found the mismatch).
    """
    schema_secret = isinstance(field.json_schema_extra, dict) and bool(
        field.json_schema_extra.get("secret")
    )
    return _is_secret(name) or schema_secret or name in _GUARDRAIL_EXTRA


def categorize(name: str, field: FieldInfo) -> FieldUsage:
    """Categorize a single field into one of the 4 buckets, with full evidence."""
    reads = external_reads(name)
    self_read = internal_self_read(name)
    manifest = manifest_read(name)
    overrides = override_locations(name, field)
    has_production_read = bool(reads.get("src")) or self_read or manifest

    if is_guardrail(name, field):
        category = "writer-pinned-guardrail"
    elif not has_production_read:
        category = "never-read"
    elif not overrides:
        category = "read-but-never-overridden"
    else:
        category = "load-bearing"

    return FieldUsage(
        name=name,
        reads=reads,
        internal_self_read=self_read,
        manifest_read=manifest,
        overrides=overrides,
        category=category,
    )


@lru_cache(maxsize=1)
def _audit_all_cached() -> tuple[FieldUsage, ...]:
    return tuple(categorize(name, field) for name, field in sorted(AppConfig.model_fields.items()))


def audit_all() -> list[FieldUsage]:
    """Categorize every AppConfig field. Cached — repo state is fixed within a process."""
    return list(_audit_all_cached())


def _evidence_cell(result: FieldUsage) -> str:
    parts: list[str] = []
    for root in SEARCH_ROOTS:
        hits = result.reads.get(root)
        if hits:
            parts.append(f"{root}:{len(hits)}")
    if result.internal_self_read:
        parts.append("self-read")
    if result.manifest_read:
        parts.append("manifest")
    return ", ".join(parts) if parts else "none"


def _override_cell(result: FieldUsage) -> str:
    if not result.overrides:
        return "none in-repo"
    return ", ".join(f"{source} ({kind})" for source, kind in result.overrides)


def generate_report(results: list[FieldUsage]) -> str:
    """Render the dated FRE-893 report — full categorized table + ranked candidate lists."""
    counts = {cat: sum(1 for r in results if r.category == cat) for cat in sorted(CATEGORIES)}
    lines: list[str] = []
    lines.append("# Config-parameter usage audit — FRE-893 (ADR-0099 hygiene)")
    lines.append("")
    lines.append(
        "> **Ticket:** [FRE-893](https://linear.app/frenchforest/issue/FRE-893) · "
        "**Backing ADR:** "
        "[ADR-0099](../architecture_decisions/ADR-0099-configuration-management-and-validation.md) "
        "· **Extends:** [CONFIG_INVENTORY.md](../reference/CONFIG_INVENTORY.md) §10 · "
        "**Generated:** 2026-07-16 · **Scope guard:** analysis and report only — "
        "zero configuration removed or changed."
    )
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        f"Every one of the {len(results)} typed `AppConfig` fields "
        "(`src/personal_agent/config/settings.py`) is categorized from evidence gathered "
        "three ways for reads and two ways for overrides:"
    )
    lines.append("")
    lines.append(
        "- **Reads** — (1) `git grep` for `settings.<field>` or "
        '`getattr(settings, "<field>")` across `src/`, `scripts/`, `tests/` (excluding '
        "`settings.py` itself), tagged by root; (2) whether `settings.py` consults the "
        "field via `self.<field>` inside one of its cross-field validators; (3) whether "
        '`config/substrate.yaml` names the field via `source: "setting:<field>"` — the '
        "one dynamic `getattr(settings, field)` resolution path in the codebase "
        "(`src/personal_agent/config/substrate.py::_resolve_setting`), which no "
        "literal-string grep can trace without reading the manifest directly. Only "
        "`src`-root hits, the self-read check, or the manifest check count as **production** "
        "read evidence — a field touched only under `tests/`/`scripts/` is not treated as "
        "production load-bearing."
    )
    lines.append(
        "- **Overrides** — (1) the 5 `docker-compose*.yml` files, parsed with `pyyaml` "
        "(not raw-text regex) and checked for the field's env var in any service's "
        "`environment:` block; (2) `tests/conftest.py`'s "
        '`os.environ.setdefault("AGENT_...")` test-substrate defaults (FRE-375), tagged '
        "`test-substrate` separately from a real `compose` deployment override."
    )
    lines.append("")
    lines.append(
        "**Limitation (measure-don't-assert): the real deployed `.env` is gitignored and "
        "not in this repo.** It is the one place a field could be overridden that this "
        "audit structurally cannot see. **A field with zero in-repo override evidence is "
        "not proof it is never overridden in production — it is proof there is no "
        "repo-visible override.** Secrets in particular are expected to be overridden "
        "*only* via the real `.env`; they are placed in `writer-pinned-guardrail` "
        "specifically so this gap never reads as a false hardcode-candidate."
    )
    lines.append("")
    lines.append(
        "**Discovered finding (not fixed here — out of scope for an audit-only ticket): "
        "`config_inventory.py`'s regex-based `_is_secret()` heuristic misses 7 fields "
        'that carry the authoritative `json_schema_extra={"secret": True}` marker** '
        "(`managed_database_url`, `managed_neo4j_uri`, `managed_elasticsearch_url`, "
        "`managed_embedding_endpoint`, `managed_embedding_token`, "
        "`managed_reranker_endpoint`, `managed_slm_endpoint` — the regex's "
        "`_api_key$|_password$|_secret$|secret_access_key$` suffixes don't match "
        "`_url`/`_endpoint`/`_token`). This audit's guardrail check ORs in the schema "
        "flag so it is not affected, but `config_inventory.py`'s own secret redaction "
        "relies on the regex alone — a candidate follow-up ticket."
    )
    lines.append("")
    lines.append("## Category counts")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|---|---|")
    for cat in sorted(CATEGORIES):
        lines.append(f"| `{cat}` | {counts[cat]} |")
    lines.append("")
    lines.append("## Full categorized table")
    lines.append("")
    lines.append("| Field | Category | Read evidence | Override evidence |")
    lines.append("|---|---|---|---|")
    for result in results:
        lines.append(
            f"| `{result.name}` | `{result.category}` | {_evidence_cell(result)} "
            f"| {_override_cell(result)} |"
        )
    lines.append("")

    dead = [r for r in results if r.category == "never-read"]
    lines.append(f"## Dead-config candidates — never-read ({len(dead)})")
    lines.append("")
    lines.append(
        "Zero read evidence anywhere (`src/`, self-referential validator, or the "
        "substrate manifest). Candidates for removal — a separate, owner-gated "
        "follow-up ticket, not this one."
    )
    lines.append("")
    for result in dead:
        lines.append(f"- `{result.name}`")
    lines.append("")

    hardcode = [r for r in results if r.category == "read-but-never-overridden"]
    lines.append(f"## Hardcode candidates — read-but-never-overridden ({len(hardcode)})")
    lines.append("")
    lines.append(
        "Read in production code, but with no repo-visible override anywhere — "
        "candidates to hardcode and remove from the configurable surface. Per the "
        "limitation above, this is not proof the real `.env` never overrides them; "
        "each is a candidate for owner review, not an automatic removal."
    )
    lines.append("")
    for result in hardcode:
        lines.append(f"- `{result.name}`")
    lines.append("")

    return "\n".join(lines)


_SECTION_MARKER = "## §10 — Parameter usage audit (FRE-893)"
_SECTION_SEPARATOR = "\n\n---\n\n"


def generate_inventory_section(results: list[FieldUsage]) -> str:
    """Render the short §10 extension section for CONFIG_INVENTORY.md."""
    counts = {cat: sum(1 for r in results if r.category == cat) for cat in sorted(CATEGORIES)}
    lines: list[str] = []
    lines.append(_SECTION_MARKER)
    lines.append("")
    lines.append(
        "A read-evidence + override-evidence categorization of every `AppConfig` field "
        "into load-bearing / read-but-never-overridden (hardcode candidate) / never-read "
        "(dead candidate) / writer-pinned-guardrail (secrets + `owner_storage_allowlist`, "
        "never a removal candidate). Full per-field evidence, methodology, and ranked "
        "candidate lists live in the dated report — this section is a summary only, "
        "extending this doc rather than duplicating that one."
    )
    lines.append("")
    lines.append(
        f"**Report:** [{REPORT_DOC.name}](../research/{REPORT_DOC.name}) · "
        f"**{len(results)} fields categorized**"
    )
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|---|---|")
    for cat in sorted(CATEGORIES):
        lines.append(f"| `{cat}` | {counts[cat]} |")
    lines.append("")
    return "\n".join(lines)


def splice_inventory_section(doc: str, section: str) -> str:
    """Idempotently (re)place the §10 section + its leading separator in `doc`.

    Regenerating must not accumulate a fresh `---` separator on every run: a prior
    version of this cut only `doc[:marker_index].rstrip()`, which strips whitespace
    but not the separator's literal `---` line, so re-running `generate` repeatedly
    left 2, 3, 4... stray horizontal rules stacked above the section (caught by a
    code-review pass that re-ran the generator and observed the file grow).
    """
    idx = doc.find(_SECTION_MARKER)
    if idx == -1:
        prefix = doc.rstrip()
    else:
        prefix = doc[:idx]
        if prefix.endswith(_SECTION_SEPARATOR):
            prefix = prefix[: -len(_SECTION_SEPARATOR)]
        prefix = prefix.rstrip()
    return prefix + _SECTION_SEPARATOR + section


def write_outputs() -> int:
    """Write the dated report and splice the §10 summary into CONFIG_INVENTORY.md."""
    results = audit_all()

    REPORT_DOC.parent.mkdir(parents=True, exist_ok=True)
    REPORT_DOC.write_text(generate_report(results), encoding="utf-8")

    section = generate_inventory_section(results)
    doc = INVENTORY_DOC.read_text(encoding="utf-8")
    INVENTORY_DOC.write_text(splice_inventory_section(doc, section), encoding="utf-8")

    print(f"Wrote {REPORT_DOC.relative_to(REPO_ROOT)}; extended {INVENTORY_DOC.name} with §10.")
    return 0


def main(argv: list[str]) -> int:
    """Dispatch to `generate` (default)."""
    mode = argv[1] if len(argv) > 1 else "generate"
    if mode == "generate":
        return write_outputs()
    print(f"usage: {argv[0]} [generate]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
