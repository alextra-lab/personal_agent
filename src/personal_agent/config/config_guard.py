"""Cross-config guard checks (ADR-0099 D1/D4, FRE-649 — stage 1).

Shared by ``scripts/check_config.py`` (the CI/pre-commit CLI) and the tiered
startup hook in ``settings.py``, so there is exactly one implementation of
each check — never a second copy that can drift from the first.

Checks the intent declared in ``config/model_roles.yaml`` against the real
per-profile model-definition YAMLs and the documented environment template
(``.env.example``):

* **Forbidden-role divergence** (policy) — a role marked ``divergence: forbidden``
  resolves to different (but individually valid) models across active profiles.
* **Dangling model reference** (safety) — a role's resolved model name does not
  exist as a key under its active profile's ``models:`` mapping.
* **Committed secret** (safety) — a live (uncommented) assignment of an
  ``AppConfig`` secret-marked field's env var to a non-placeholder value in a
  tracked YAML or ``.env.example``.
* **Orphan ``.env`` key** (policy) — a documented ``AGENT_*`` key in
  ``.env.example`` that binds no ``AppConfig`` field.

Severity classes follow the ADR-0099 D4 table: safety findings hard-fail both
CI and (via the startup hook) process boot; policy findings hard-fail CI/
pre-commit but only warn-loud at startup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]

# model_loader.py imports from this module (resolve_role_model_key, ADR-0099
# D1 stage 2) — this module must never import model_loader.py back, or that
# becomes a cycle.

Severity = Literal["safety", "policy"]
#: A parsed YAML mapping (matrix, model-definition file, or role sub-mapping).
JSONDict = dict[str, object]

_EXEMPTION_RE = re.compile(r"#\s*fre-649-allow")

# A live (uncommented) KEY = value / KEY: value assignment line.
_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.+?)\s*$")

# Values that never count as a committed secret: shell interpolation, or the
# repo's angle-bracket placeholder convention (see .env.example).
_SAFE_VALUE_RE = re.compile(r"^(\$\{.*\}|<.*>)$")

# Files scanned for committed secrets, relative to a fixture/repo root.
_SECRET_SCAN_GLOBS: tuple[str, ...] = (
    "config/*.yaml",
    "config/*.yml",
    "config/governance/*.yaml",
    ".env.example",
    "docker-compose*.yml",
)

# AGENT_-namespace .env.example keys legitimately consumed outside AppConfig
# (model-loader endpoints, infra scripts) — kept in sync with the equivalent
# allow-list in scripts/audit/config_inventory.py.
_KNOWN_ENV_ONLY: frozenset[str] = frozenset(
    {
        "AGENT_EMBEDDING_ENDPOINT",
        "AGENT_RERANKER_ENDPOINT",
        "AGENT_MCP_SECRETS_FILE",
        "AGENT_GATEWAY_TOKEN_PWA",
        "AGENT_GATEWAY_TOKEN_EXTERNAL_AGENT",
        "AGENT_CLOUDFLARE_TUNNEL_TOKEN",
    }
)


@dataclass(frozen=True)
class Finding:
    """A single guard violation."""

    check: str
    severity: Severity
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        """Render as ``[severity] check: message`` for CLI/log output."""
        return f"[{self.severity}] {self.check}: {self.message}"


def repo_root() -> Path:
    """Resolve the repository root (four levels up from this file).

    Mirrors the same convention already used by ``env_loader.load_env_files``
    and ``validators.resolve_path``, so all three agree on what "the repo" is
    regardless of the caller's current working directory.
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _strip_inline_comment(value: str) -> str:
    """Strip a trailing quote/whitespace so 'sk-ant-...' and "sk-ant-..." compare equal."""
    return value.strip().strip("'\"")


def _secret_field_env_vars() -> dict[str, str]:
    """Map every accepted env-var spelling of a secret-marked field to its field name.

    A field is "secret" iff its ``json_schema_extra`` carries ``secret: True``
    (ADR-0099 D2 — derived from AppConfig metadata, no separately-edited list).
    Both the always-valid ``AGENT_<FIELD>`` spelling and a declared ``alias``
    (if any) are included, since either binds the field at runtime.
    """
    from personal_agent.config.settings import AppConfig  # noqa: PLC0415 — avoid import cycle

    mapping: dict[str, str] = {}
    for name, field in AppConfig.model_fields.items():
        extra = field.json_schema_extra
        if not (isinstance(extra, dict) and extra.get("secret")):
            continue
        mapping[f"AGENT_{name.upper()}"] = name
        if isinstance(field.alias, str) and field.alias:
            mapping[field.alias] = name
    return mapping


def _load_yaml(path: Path) -> JSONDict:
    if not path.is_file():
        return {}
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    return content if isinstance(content, dict) else {}


def _resolved_role_model_key(role_cfg: JSONDict, profile: str) -> str | None:
    """Resolve a role's model key for *profile* from the matrix entry itself.

    ADR-0099 D1 stage 2 (FRE-650): role assignment lives ONLY in the matrix
    now — a ``forbidden`` role's key is its ``all:`` value (same for every
    profile); an ``allowed`` role's key is its ``local:``/``cloud:`` value for
    *profile*. Returns ``None`` if the entry declares no value for this case
    (e.g. an ``allowed`` role with no value for this particular profile) —
    the caller treats that as nothing to check, not a finding.
    """
    divergence = role_cfg.get("divergence")
    value = role_cfg.get("all") if divergence == "forbidden" else role_cfg.get(profile)
    return value if isinstance(value, str) else None


def _resolved_model_definition(profile_yaml: JSONDict, model_name: str) -> JSONDict | None:
    models = profile_yaml.get("models")
    if not isinstance(models, dict):
        return None
    definition = models.get(model_name)
    return definition if isinstance(definition, dict) else None


def load_matrix(root: Path) -> JSONDict:
    """Load ``config/model_roles.yaml`` under *root*, or ``{}`` if absent/empty."""
    return _load_yaml(root / "config" / "model_roles.yaml")


def resolve_active_profile(model_config_path: Path, matrix: JSONDict, root: Path) -> str | None:
    """Return the ``active_profiles`` key whose file resolves to *model_config_path*.

    Both sides are resolved absolute before comparing, so callers may pass
    either an already-resolved ``settings.model_config_path`` or a bare
    relative path. Returns ``None`` if no entry matches (e.g. a fixture or
    test root with no ``active_profiles`` declared).
    """
    target = model_config_path.resolve()
    active_profiles = matrix.get("active_profiles", {})
    if not isinstance(active_profiles, dict):
        return None
    for profile, rel_path in active_profiles.items():
        if (root / rel_path).resolve() == target:
            return str(profile)
    return None


def check_forbidden_role_divergence_and_dangling_refs(
    root: Path, matrix: JSONDict
) -> list[Finding]:
    """AC-3 (forbidden-role divergence) + AC-9 (dangling model reference)."""
    findings: list[Finding] = []
    active_profiles: dict[str, str] = matrix.get("active_profiles", {})  # type: ignore[assignment]
    roles: dict[str, JSONDict] = matrix.get("roles", {})  # type: ignore[assignment]

    profile_yamls: dict[str, JSONDict] = {}
    for profile, rel_path in active_profiles.items():
        path = root / rel_path
        if path.is_file():
            profile_yamls[profile] = _load_yaml(path)

    for role, role_cfg in roles.items():
        resolved: dict[str, tuple[str, JSONDict | None]] = {}
        for profile, profile_yaml in profile_yamls.items():
            model_name = _resolved_role_model_key(role_cfg, profile)
            if model_name is None:
                # No value declared for this profile (e.g. an `allowed` role
                # with only a `local:`/`cloud:` value, not both) — nothing to
                # check for this profile.
                continue
            definition = _resolved_model_definition(profile_yaml, model_name)
            resolved[profile] = (model_name, definition)
            if definition is None:
                findings.append(
                    Finding(
                        check="dangling_model_reference",
                        severity="safety",
                        message=(
                            f"role '{role}' resolves to model '{model_name}' under profile "
                            f"'{profile}', which has no matching entry under models: "
                            f"in {active_profiles[profile]}"
                        ),
                    )
                )

        if role_cfg.get("divergence") != "forbidden" or len(resolved) < 2:
            continue

        definitions = {
            profile: definition
            for profile, (_, definition) in resolved.items()
            if definition is not None
        }
        distinct = {
            (d.get("id"), d.get("provider"), d.get("max_tokens"), d.get("temperature"))
            for d in definitions.values()
        }
        if len(distinct) > 1:
            model_name = next(name for name, _ in resolved.values())
            profile_summary = ", ".join(
                f"{profile}={d.get('id')}" for profile, d in sorted(definitions.items())
            )
            findings.append(
                Finding(
                    check="forbidden_role_divergence",
                    severity="policy",
                    message=(
                        f"role '{role}' is divergence:forbidden and resolves to the same "
                        f"model key '{model_name}' in every active profile, but the "
                        f"underlying ModelDefinition differs (definition drift): {profile_summary}"
                    ),
                )
            )
    return findings


def check_orphan_env_keys(root: Path) -> list[Finding]:
    """AC-4 — a documented AGENT_* key in .env.example binding no AppConfig field."""
    from personal_agent.config.settings import AppConfig  # noqa: PLC0415 — avoid import cycle

    env_example = root / ".env.example"
    if not env_example.is_file():
        return []

    bound: set[str] = set()
    for name, field in AppConfig.model_fields.items():
        bound.add(f"AGENT_{name.upper()}")
        if isinstance(field.alias, str) and field.alias:
            bound.add(field.alias)

    findings: list[Finding] = []
    for raw_line in env_example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#") or not line:
            continue
        match = _ASSIGNMENT_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if not key.startswith("AGENT_"):
            continue
        if key in bound or key in _KNOWN_ENV_ONLY:
            continue
        findings.append(
            Finding(
                check="orphan_env_key",
                severity="policy",
                message=f".env.example documents '{key}' which binds no AppConfig field",
            )
        )
    return findings


def check_committed_secrets(root: Path) -> list[Finding]:
    """AC-8 — no live assignment of a secret-marked field's env var to a real value."""
    secret_env_vars = _secret_field_env_vars()
    findings: list[Finding] = []

    scanned: set[Path] = set()
    for pattern in _SECRET_SCAN_GLOBS:
        scanned.update(root.glob(pattern))

    for path in sorted(scanned):
        if not path.is_file():
            continue
        for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if line.startswith("#") or not line:
                continue
            if _EXEMPTION_RE.search(raw_line):
                continue
            match = _ASSIGNMENT_RE.match(line)
            if not match:
                continue
            key, value = match.group(1), _strip_inline_comment(match.group(2))
            field_name = secret_env_vars.get(key)
            if field_name is None:
                continue
            if not value or _SAFE_VALUE_RE.match(value):
                continue
            findings.append(
                Finding(
                    check="committed_secret",
                    severity="safety",
                    message=(
                        f"{path.relative_to(root)}:{lineno}: committed value for secret field "
                        f"'{field_name}' (env var {key})"
                    ),
                )
            )
    return findings


def check_matrix_shape(matrix: JSONDict) -> list[Finding]:
    """A role's declared keys must match its own ``divergence`` value (FRE-650).

    A ``forbidden`` role must declare ``all`` and must NOT declare
    ``local``/``cloud`` (there is no per-profile value to diverge — that is
    the entire point of ``forbidden``). An ``allowed`` role must declare at
    least one of ``local``/``cloud`` and must NOT declare ``all`` (an `all`
    value on an `allowed` role is a stale/contradictory declaration). Without
    this check, a malformed matrix silently relocates the old
    assignment-drift failure mode into ``config/model_roles.yaml`` itself,
    uncaught by :func:`check_forbidden_role_divergence_and_dangling_refs`.
    """
    findings: list[Finding] = []
    roles: dict[str, JSONDict] = matrix.get("roles", {})  # type: ignore[assignment]
    for role, role_cfg in roles.items():
        divergence = role_cfg.get("divergence")
        has_all = "all" in role_cfg
        has_per_profile = "local" in role_cfg or "cloud" in role_cfg
        if divergence == "forbidden":
            if not has_all:
                findings.append(
                    Finding(
                        check="matrix_shape",
                        severity="policy",
                        message=f"role '{role}' is divergence:forbidden but declares no 'all' value",
                    )
                )
            if has_per_profile:
                findings.append(
                    Finding(
                        check="matrix_shape",
                        severity="policy",
                        message=(
                            f"role '{role}' is divergence:forbidden but declares "
                            "'local'/'cloud' — a forbidden role has no per-profile value"
                        ),
                    )
                )
        elif divergence == "allowed":
            if not has_per_profile:
                findings.append(
                    Finding(
                        check="matrix_shape",
                        severity="policy",
                        message=(
                            f"role '{role}' is divergence:allowed but declares neither "
                            "'local' nor 'cloud'"
                        ),
                    )
                )
            if has_all:
                findings.append(
                    Finding(
                        check="matrix_shape",
                        severity="policy",
                        message=(
                            f"role '{role}' is divergence:allowed but declares 'all' "
                            "— 'all' is only valid for a forbidden role"
                        ),
                    )
                )
        else:
            findings.append(
                Finding(
                    check="matrix_shape",
                    severity="policy",
                    message=(
                        f"role '{role}' has invalid divergence {divergence!r} "
                        "(expected 'forbidden' or 'allowed')"
                    ),
                )
            )
    return findings


def run_all_checks(root: Path) -> list[Finding]:
    """Run every check against *root* and return all findings."""
    matrix = load_matrix(root)

    findings: list[Finding] = []
    findings.extend(check_matrix_shape(matrix))
    findings.extend(check_forbidden_role_divergence_and_dangling_refs(root, matrix))
    findings.extend(check_orphan_env_keys(root))
    findings.extend(check_committed_secrets(root))
    return findings
