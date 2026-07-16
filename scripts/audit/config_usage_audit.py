"""Config-parameter usage audit (FRE-893, ADR-0099 hygiene).

Categorizes every ``AppConfig`` field into one of four buckets, backed by evidence:

* ``load-bearing`` — read somewhere in production code AND overridden away from its
  default somewhere this audit can see (compose, the test substrate, or the deployed
  environment).
* ``read-but-never-overridden`` — read, but never seen overridden — a hardcode candidate.
* ``never-read`` — no read evidence anywhere — a dead-config candidate.
* ``writer-pinned-guardrail`` — a secret or safety-critical field; never a removal
  candidate regardless of read/override evidence.

Read evidence combines three sources: an **AST alias-aware scan** (FRE-896,
``scripts/audit/settings_reads.py``) over ``src``, ``scripts``, ``tests`` (tagged by root
so a field touched only by tests/tooling is not mistaken for production load-bearing) that
resolves reads reaching a field through an alias — ``cfg = settings; cfg.<field>``,
``get_settings().<field>``, a multi-line ``getattr(settings, "<field>")``, an
``AppConfig``-typed param, or a ``self._settings`` attribute alias — which FRE-893's
line-oriented ``git grep`` for ``settings.<field>`` systematically missed; whether
``settings.py`` itself consults the field via ``self.<field>`` inside a cross-field
validator; and whether ``config/substrate.yaml`` names the field via a
``source: "setting:<field>"`` reference (the one dynamic-``getattr`` resolution path in
the codebase, resolved by reading the manifest directly rather than tracing the runtime
call — a codex plan-review pass found that a literal-string grep alone cannot see this
path).

Override evidence combines three sources: the 5 ``docker-compose*.yml`` files' parsed
``services.*.environment`` blocks (a real deployment-config surface);
``tests/conftest.py``'s ``os.environ.setdefault("AGENT_...")`` test-substrate defaults
(FRE-375); and the deployed environment file(s) at the VPS root (``/opt/seshat`` by
default, overridable via ``AUDIT_DEPLOYED_ENV_ROOT`` for tests) — gitignored, not
tracked in this repo, but readable on disk. Scoped to the *currently active*
environment (``get_environment()``, matching ``env_loader.py``'s own priority order)
rather than every ``Environment`` value, so a stray ``.env.test`` alongside the real
deployed ``.env`` isn't misread as a live production override. Tagged separately
(``"compose"`` / ``"test-substrate"`` / ``"deployed-env"``) so a reader can tell a real
deployment override from a test-isolation default from the live production file. Only
env-var **key names** are ever read from the deployed file(s) — parsed with
``python-dotenv`` (the same parser ``env_loader.py`` trusts for these files), taking
only ``.keys()``; the value half is never bound to a variable, so no secret value can
reach this audit's output.

FRE-893 was reopened after its first pass shipped without this source: the override
analysis conflated "not in the git repo" with "cannot see it," so 64 of the deployed
``.env``'s 73 real ``AGENT_`` overrides were false-flagged as hardcode candidates. This
version fixes that by reading the deployed file(s) directly.

Run from the repo root::

    uv run python -m scripts.audit.config_usage_audit generate
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from dotenv import dotenv_values
from pydantic.fields import FieldInfo

from personal_agent.config.env_loader import get_environment
from personal_agent.config.settings import AppConfig
from scripts.audit.config_inventory import (
    INVENTORY_DOC,
    REPO_ROOT,
    _accepted_env,
    _is_secret,
)
from scripts.audit.settings_reads import collect_field_reads

SETTINGS_FILE = REPO_ROOT / "src" / "personal_agent" / "config" / "settings.py"
SUBSTRATE_MANIFEST = REPO_ROOT / "config" / "substrate.yaml"
CONFTEST_FILE = REPO_ROOT / "tests" / "conftest.py"
REPORT_DOC = REPO_ROOT / "docs" / "research" / "2026-07-16-fre-893-config-parameter-usage-audit.md"

# The VPS deployment root — where the real, gitignored `.env` lives (see
# `env_loader.py::load_env_files`'s `project_root`). Overridable via
# `AUDIT_DEPLOYED_ENV_ROOT` so tests never touch the real production file.
_DEPLOYED_ENV_ROOT_VAR = "AUDIT_DEPLOYED_ENV_ROOT"
_DEFAULT_DEPLOYED_ENV_ROOT = Path("/opt/seshat")

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


@lru_cache(maxsize=1)
def _ast_reads_by_field() -> dict[str, dict[str, list[str]]]:
    """`field -> {root -> [file:line, ...]}` for every alias-aware read across the tree.

    One AST pass over every `*.py` under `src`, `scripts`, `tests` (excluding
    `settings.py` itself — the field's own definition module), delegating alias
    resolution to `scripts.audit.settings_reads.collect_field_reads` (FRE-896). Replaces
    FRE-893's line-oriented `git grep`, which could not see reads through an alias
    (`cfg = settings`, `get_settings().<field>`, a multi-line `getattr`, an
    `AppConfig`-typed param, or a `self._settings` attribute alias). Cached — repo state
    is fixed within a process. A file that fails to parse (e.g. a Python 2 fixture) is
    skipped rather than aborting the whole audit.
    """
    field_names = frozenset(AppConfig.model_fields)
    settings_rel = str(SETTINGS_FILE.relative_to(REPO_ROOT))
    by_field: dict[str, dict[str, list[str]]] = {}
    for root in SEARCH_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            rel = str(path.relative_to(REPO_ROOT))
            if rel == settings_rel:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, ValueError, UnicodeDecodeError):
                continue
            for field_name, lineno in collect_field_reads(tree, field_names):
                by_field.setdefault(field_name, {}).setdefault(root, []).append(f"{rel}:{lineno}")
    return by_field


def external_reads(name: str) -> dict[str, list[str]]:
    """Alias-aware reads of `settings.<name>`, keyed by top-level root (`src`/`scripts`/`tests`).

    Excludes hits inside `config/settings.py` itself (the field's own definition).
    """
    return _ast_reads_by_field().get(name, {})


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


def deployed_env_root() -> Path:
    """The deployed-env root directory (`AUDIT_DEPLOYED_ENV_ROOT` override, else the VPS)."""
    override = os.environ.get(_DEPLOYED_ENV_ROOT_VAR)
    return Path(override) if override else _DEFAULT_DEPLOYED_ENV_ROOT


def _deployed_env_candidates(root: Path) -> tuple[Path, ...]:
    """Candidate deployed-env file paths at `root`, in `env_loader.py`'s priority order.

    Scoped to the *currently active* environment (`get_environment()`, reading `APP_ENV`
    — the same signal `load_env_files()` itself uses) rather than all four `Environment`
    values: a stray `.env.test` or `.env.development` alongside the real deployed `.env`
    would otherwise be misread as a live production override when the running service
    (under a different `APP_ENV`) never actually loads that file — a code-review pass on
    this ticket found this false-positive risk in an earlier draft that scanned every
    environment unconditionally.
    """
    env_name = get_environment().value
    return (
        root / f".env.{env_name}.local",
        root / f".env.{env_name}",
        root / ".env.local",
        root / ".env",
    )


def deployed_env_files_present(root: Path | None = None) -> tuple[str, ...]:
    """Which candidate deployed-env paths actually exist at `root` (report transparency)."""
    resolved = root if root is not None else deployed_env_root()
    return tuple(str(p) for p in _deployed_env_candidates(resolved) if p.exists())


@lru_cache(maxsize=None)
def _deployed_env_key_sources(root: Path) -> dict[str, str]:
    """KEY -> first deployed-env file path (at `root`) where the key is set.

    Parses each candidate file with `python-dotenv` (already a project dependency —
    `env_loader.py` itself uses it to load these same files), taking only `.keys()` —
    the value half of `dotenv_values(path)`'s returned mapping is never bound to a
    variable, so no secret value can reach this audit's output. A missing file is
    silently skipped (expected on CI/dev machines without a deployed `.env`). Values
    are looked up in `_deployed_env_candidates`'s priority order, so a key's recorded
    source is always the highest-priority file it appears in.

    A hand-rolled line regex previously did this instead; a code-review pass on this
    ticket found it mis-parses multi-line quoted values (e.g. a PEM cert), splitting a
    continuation line that happens to contain `=` into a spurious extra key. `dotenv_values`
    is the same parser `env_loader.py` trusts for these exact files, so it doesn't share
    that gap.
    """
    sources: dict[str, str] = {}
    for path in _deployed_env_candidates(root):
        if not path.exists():
            continue
        try:
            keys = tuple(dotenv_values(path).keys())
        except OSError:
            continue
        for key in keys:
            sources.setdefault(key, str(path))
    return sources


def override_locations(name: str, field: FieldInfo) -> list[tuple[str, str]]:
    """`(source, kind)` pairs where this field's env var is set away from its default.

    `kind="compose"` for a docker-compose*.yml `environment:` block; `kind="test-substrate"`
    for a `tests/conftest.py` `os.environ.setdefault("AGENT_...")` default; `kind="deployed-env"`
    for a key present in the VPS's real, gitignored `.env` (or a higher-priority sibling) —
    key presence only, the value is never read.
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

    root = deployed_env_root()
    deployed_sources = _deployed_env_key_sources(root)
    deployed_hits = accepted & deployed_sources.keys()
    if deployed_hits:
        # Attribute to whichever hit's file ranks highest in priority order — not
        # whichever alias spelling sorts first alphabetically (a code-review-confirmed
        # defect: an aliased field's lower-priority spelling could otherwise "win").
        priority = {str(p): i for i, p in enumerate(_deployed_env_candidates(root))}
        best_key = min(
            deployed_hits, key=lambda k: (priority.get(deployed_sources[k], len(priority)), k)
        )
        found.append((deployed_sources[best_key], "deployed-env"))

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
        "three ways for reads and three ways for overrides:"
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
        '`os.environ.setdefault("AGENT_...")` test-substrate defaults (FRE-375); (3) the '
        "deployed environment file(s) at the VPS root (`/opt/seshat` by default) — key "
        "**names** only, checked in `env_loader.py`'s priority order "
        "(`.env.<environment>.local`, `.env.<environment>`, `.env.local`, `.env`). Tagged "
        "`compose` / `test-substrate` / `deployed-env` respectively, so a reader can tell "
        "a real deployment override from a test-isolation default from the live "
        "production file."
    )
    lines.append("")
    found_deployed = deployed_env_files_present()
    if found_deployed:
        lines.append(
            f"**This run read the deployed environment from:** "
            f"{', '.join(f'`{p}`' for p in found_deployed)}. Key names only — no value "
            "was parsed, held, or printed, so no secret can leak into this report."
        )
    else:
        lines.append(
            "**This run found no deployed environment file at "
            f"`{deployed_env_root()}`** (expected off the VPS, e.g. in CI) — override "
            "evidence for this run reflects only `compose`/`test-substrate` sources. "
            "**Regenerate on the VPS to include the deployed `.env`** before treating "
            "the hardcode-candidate list below as authoritative."
        )
    lines.append("")
    lines.append(
        "**Limitation (measure-don't-assert), still applies even when the deployed `.env` "
        "was read:** the three sources above are not the only way a field could be "
        "overridden in practice — a `docker-compose.override.yml` outside the tracked 5 "
        "files, a `docker run -e` / `--env-file` flag, a `systemd Environment=` unit "
        "directive, a host-shell `export`, or an orchestrator/secrets-manager injection "
        "are all invisible to this audit. **Zero override evidence across all three "
        "sources is not proof a field is never overridden — it is proof there is no "
        "evidence in the specific places this audit looks.** A field the deployed `.env` "
        "sets but that has zero read evidence is still correctly `never-read` (a "
        "genuinely dead env override, not a contradiction) — override and read evidence "
        "are independent axes. (Why a deployed-env source exists at all: an earlier "
        "version of this audit had none, so it silently missed the one channel that in "
        "practice carries most real production overrides — see FRE-893's ticket history.)"
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
        "substrate manifest). Removal **candidates**, not a delete list — FRE-896's "
        "origin-ADR provenance map "
        "([2026-07-16-fre-896-config-provenance-map.md](2026-07-16-fre-896-config-provenance-map.md)) "
        "classifies each as outgrown (deleted) vs forward-declaration / wiring-gap / "
        "wiring-bug / cost-gov (kept): most never-read fields back a live or planned "
        "feature whose knob is merely unwired, so deleting them would amputate a stream."
    )
    lines.append("")
    for result in dead:
        lines.append(f"- `{result.name}`")
    lines.append("")

    hardcode = [r for r in results if r.category == "read-but-never-overridden"]
    lines.append(f"## Hardcode candidates — read-but-never-overridden ({len(hardcode)})")
    lines.append("")
    lines.append(
        "Read in production code, with no override evidence in compose, the test "
        "substrate, or the deployed environment (when this run had access to it — see "
        "the note above). Candidates to hardcode and remove from the configurable "
        "surface — each is a candidate for owner review, not an automatic removal."
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


_TRAILING_RULE = re.compile(r"\n{0,2}---\s*\Z")


def splice_inventory_section(doc: str, section: str) -> str:
    """Idempotently (re)place the §10 section + its leading separator in `doc`.

    Regenerating must not accumulate a fresh `---` separator on every run. Two distinct
    ways that can happen, both handled by stripping AT MOST ONE trailing bare `---` rule
    — not just the one immediately behind a still-present marker, but not an unbounded
    loop either (an earlier version of this looped, which could silently eat through a
    doc's own unrelated trailing horizontal rules — a code-review pass on this ticket
    flagged that as a content-destroying regression):

    1. The marker is present (a previous `generate` run's §10) — strip its leading
       separator before re-appending, or repeated runs stack `---` lines above the
       section (a code-review pass on this ticket caught this by re-running the
       generator and observing the file grow).
    2. The marker is ABSENT but a trailing separator still is — e.g. a §10 section was
       manually removed (leaving its leading `---` behind) and the tool re-run before
       a fresh insertion; naively appending a new separator here doubles it to `---`
       `---` (this exact case happened during the FRE-893 redo: master's removal PR
       deleted the §10 heading+body but left the separator dangling at EOF). Only one
       dangling separator is ever left behind by either scenario, so stripping a single
       occurrence is sufficient and doesn't risk consuming legitimate content.
    """
    idx = doc.find(_SECTION_MARKER)
    prefix = doc[:idx] if idx != -1 else doc
    stripped = prefix.rstrip()
    match = _TRAILING_RULE.search(stripped)
    if match:
        stripped = stripped[: match.start()].rstrip()
    return stripped + _SECTION_SEPARATOR + section


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
