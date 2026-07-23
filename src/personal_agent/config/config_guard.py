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
* **Undocumented field** (policy) — an ``AppConfig`` field with no (or
  whitespace-only) ``description`` (ADR-0099 D4).
* **Secret field plaintext default** (policy) — a secret-marked field whose
  Python default is a real (non-empty, unexempted) value, or a field carrying
  the exemption key without the ``secret`` marker itself (metadata misuse).

Severity classes follow the ADR-0099 D4 table: safety findings hard-fail both
CI and (via the startup hook) process boot; policy findings hard-fail CI/
pre-commit but only warn-loud at startup.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

    from personal_agent.config.settings import AppConfig

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

# A per-profile role-assignment header (ADR-0099 D1 stage 2, FRE-650, retired these from
# config/*.yaml — role assignment lives only in config/model_roles.yaml).
_ROLE_HEADER_RE = re.compile(
    r"^\s*(entity_extraction|captains_log|session_summary|insights|compressor|embedding"
    r"|reranker)_role\s*:"
)

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


def _is_secret_marked(field: FieldInfo) -> bool:
    """A field is "secret" iff its ``json_schema_extra`` carries ``secret: True``.

    ADR-0099 D2 — derived from AppConfig metadata, no separately-edited list.
    The single predicate every secret-aware check (env-var mapping, plaintext-
    default detection) shares, so the marking convention can never drift
    out of sync between call sites.
    """
    extra = field.json_schema_extra
    return isinstance(extra, dict) and bool(extra.get("secret"))


def _secret_field_env_vars() -> dict[str, str]:
    """Map every accepted env-var spelling of a secret-marked field to its field name.

    Both the always-valid ``AGENT_<FIELD>`` spelling and a declared ``alias``
    (if any) are included, since either binds the field at runtime.
    """
    from personal_agent.config.settings import AppConfig  # noqa: PLC0415 — avoid import cycle

    mapping: dict[str, str] = {}
    for name, field in AppConfig.model_fields.items():
        if not _is_secret_marked(field):
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


def _resolved_model_definition(profile_yaml: JSONDict, model_name: str) -> JSONDict | None:
    models = profile_yaml.get("models")
    if not isinstance(models, dict):
        return None
    definition = models.get(model_name)
    return definition if isinstance(definition, dict) else None


def load_matrix(root: Path) -> JSONDict:
    """Load ``config/model_roles.yaml`` under *root*, or ``{}`` if absent/empty."""
    return _load_yaml(root / "config" / "model_roles.yaml")


def load_deployment_manifest(root: Path) -> JSONDict:
    """Load ``config/deployment.yaml`` under *root*, or ``{}`` if absent/empty."""
    return _load_yaml(root / "config" / "deployment.yaml")


# ── Substrate backend-selection seam (ADR-0112 D3 / AC-2, FRE-816) ────────────

#: The D3 canonical substrate components every profile in ``config/substrate.yaml``
#: must declare. A profile omitting any of these is exactly ADR-0112 AC-2's
#: "fails if any D3-listed component is hardcoded/omitted" — an embedder-only
#: seam, or one omitting the search/vector index, must fail the guard.
REQUIRED_SUBSTRATE_COMPONENTS: frozenset[str] = frozenset(
    {"postgres", "neo4j", "elasticsearch", "embedder", "reranker", "slm", "vector_index"}
)

#: The source-grammar prefixes a substrate row's ``source`` may use (see
#: config/substrate.yaml + substrate.py). Kept here so the guard and the
#: resolver agree on one vocabulary.
_SUBSTRATE_SOURCE_KINDS: frozenset[str] = frozenset({"setting", "model_endpoint", "backed_by"})
_SUBSTRATE_BACKEND_KINDS: frozenset[str] = frozenset({"local", "managed"})


def load_substrate_manifest(root: Path) -> JSONDict:
    """Load ``config/substrate.yaml`` under *root*, or ``{}`` if absent/empty."""
    return _load_yaml(root / "config" / "substrate.yaml")


def _appconfig_field_names() -> frozenset[str]:
    """Every declared ``AppConfig`` field name (for ``setting:<field>`` validation)."""
    from personal_agent.config.settings import AppConfig  # noqa: PLC0415 — avoid import cycle

    return frozenset(AppConfig.model_fields.keys())


def _check_substrate_row(
    profile: str,
    component: str,
    row: object,
    app_fields: frozenset[str],
    matrix_roles: JSONDict,
    declared_components: frozenset[str],
) -> list[Finding]:
    """Validate one ``(profile, component)`` row's ``kind`` + ``source`` reference."""
    findings: list[Finding] = []
    where = f"substrate profile '{profile}' component '{component}'"
    if not isinstance(row, dict):
        return [Finding("substrate_manifest_shape", "policy", f"{where}: not a mapping")]

    kind = row.get("kind")
    if kind not in _SUBSTRATE_BACKEND_KINDS:
        findings.append(
            Finding(
                "substrate_manifest_shape",
                "policy",
                f"{where}: invalid kind {kind!r} (expected one of {sorted(_SUBSTRATE_BACKEND_KINDS)})",
            )
        )

    source = row.get("source")
    if not isinstance(source, str) or ":" not in source:
        findings.append(
            Finding(
                "substrate_manifest_shape",
                "policy",
                f"{where}: source {source!r} must be '<kind>:<ref>' "
                f"(kinds: {sorted(_SUBSTRATE_SOURCE_KINDS)})",
            )
        )
        return findings

    source_kind, ref = source.split(":", 1)
    if source_kind not in _SUBSTRATE_SOURCE_KINDS:
        findings.append(
            Finding(
                "substrate_manifest_shape",
                "policy",
                f"{where}: unknown source kind {source_kind!r} in {source!r}",
            )
        )
    elif source_kind == "setting" and ref not in app_fields:
        findings.append(
            Finding(
                "substrate_source_dangling",
                "policy",
                f"{where}: source {source!r} names AppConfig field {ref!r} which does not exist",
            )
        )
    elif source_kind == "model_endpoint" and ref not in matrix_roles:
        findings.append(
            Finding(
                "substrate_source_dangling",
                "policy",
                f"{where}: source {source!r} names model role {ref!r} which is not declared "
                "in config/model_roles.yaml roles:",
            )
        )
    elif source_kind == "backed_by" and ref not in declared_components:
        findings.append(
            Finding(
                "substrate_source_dangling",
                "policy",
                f"{where}: source {source!r} references component {ref!r} which this "
                "profile does not declare",
            )
        )
    return findings


def check_substrate_manifest(root: Path) -> list[Finding]:
    """ADR-0112 AC-2 — every profile declares all D3 components; every source is well-formed.

    A component **omitted** from a profile is exactly AC-2's failure mode ("fails
    if any D3-listed component is hardcoded/omitted — an embedder-only seam, or
    one omitting the search/vector index, must fail this"). Also validates each
    row's ``kind`` and ``source`` reference so a malformed manifest is caught in
    CI/pre-commit, not at boot. All findings are ``policy`` (block CI/pre-commit;
    never wedge startup), consistent with the deployment-manifest checks.

    A **missing** ``config/substrate.yaml`` yields no findings (a fixture/test
    root legitimately has none) — mirroring the deployment-manifest checks.
    """
    manifest = load_substrate_manifest(root)
    if not manifest:
        return []

    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return [
            Finding(
                "substrate_manifest_shape",
                "policy",
                "config/substrate.yaml declares no non-empty 'profiles:' mapping",
            )
        ]

    raw_roles = load_matrix(root).get("roles", {})
    matrix_roles: JSONDict = raw_roles if isinstance(raw_roles, dict) else {}
    app_fields = _appconfig_field_names()

    findings: list[Finding] = []
    for profile, rows in profiles.items():
        if not isinstance(rows, dict):
            findings.append(
                Finding(
                    "substrate_manifest_shape",
                    "policy",
                    f"substrate profile '{profile}' is not a mapping of components",
                )
            )
            continue
        declared = frozenset(rows.keys())
        for component in sorted(REQUIRED_SUBSTRATE_COMPONENTS - declared):
            findings.append(
                Finding(
                    "substrate_component_missing",
                    "policy",
                    f"substrate profile '{profile}' omits required component '{component}' "
                    "(ADR-0112 AC-2 — every D3 component must be selectable by profile)",
                )
            )
        for component, row in rows.items():
            findings.extend(
                _check_substrate_row(profile, component, row, app_fields, matrix_roles, declared)
            )
    return findings


def check_dev_test_profile_isolation(root: Path) -> list[Finding]:
    """ADR-0112 AC-9 (FRE-820) — dev/test substrate profiles never resolve managed/paid backends.

    The machine-checkable half of AC-9's "no live paid endpoint call": every
    component of the ``dev``/``test`` profiles in ``config/substrate.yaml``
    must declare ``kind: local`` AND must not source a ``managed_*``-prefixed
    AppConfig field — a row could otherwise "lie" (claim ``kind: local``
    while a ``setting:managed_*`` source still points at a paid endpoint)
    and pass a kind-only check.

    A missing manifest, or one declaring neither a ``dev`` nor ``test``
    profile, yields no findings (a fixture/test root legitimately has
    neither) — mirroring :func:`check_substrate_manifest`. Malformed rows
    (non-mapping, unparsable ``source``) are that function's job to report;
    this check simply skips them rather than duplicating shape validation.
    """
    manifest = load_substrate_manifest(root)
    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict):
        return []

    findings: list[Finding] = []
    for profile_name in ("dev", "test"):
        rows = profiles.get(profile_name)
        if not isinstance(rows, dict):
            continue
        for component, row in rows.items():
            if not isinstance(row, dict):
                continue
            where = f"substrate profile '{profile_name}' component '{component}'"
            kind = row.get("kind")
            if kind != "local":
                findings.append(
                    Finding(
                        "dev_test_profile_not_local",
                        "policy",
                        f"{where} has kind={kind!r} — dev/test profiles must be "
                        "local-only (ADR-0112 AC-9: no live paid endpoint call)",
                    )
                )
            source = row.get("source")
            if isinstance(source, str) and ":" in source:
                source_kind, ref = source.split(":", 1)
                if source_kind == "setting" and ref.startswith("managed_"):
                    findings.append(
                        Finding(
                            "dev_test_profile_managed_source",
                            "policy",
                            f"{where} sources AppConfig field {ref!r} — a managed_*-"
                            "prefixed field under dev/test would still resolve a paid "
                            "endpoint even with kind: local (ADR-0112 AC-9)",
                        )
                    )
    return findings


def check_dangling_model_references(root: Path, matrix: JSONDict) -> list[Finding]:
    """AC-9 — every role must resolve to a model key the catalog actually defines.

    **Narrowed by FRE-916 phase 2 (ADR-0121).** This was
    ``check_forbidden_role_divergence_and_dangling_refs``, and its first half
    compared each ``divergence: forbidden`` role's resolved ``ModelDefinition``
    across the two active catalogs to catch *definition* drift — the same key
    backed by a different model in each file. Collapsing to one catalog makes
    that comparison impossible to fail, because there is nothing left to compare
    against: definition drift became unrepresentable rather than merely policed.

    The dangling-reference half is retained unchanged in substance. A role
    pointing at a key the catalog does not define is still a live failure, and it
    is *safety* class — it wedges role resolution at runtime, so it must fail
    loudly rather than merely block CI.
    """
    findings: list[Finding] = []
    roles: dict[str, JSONDict] = matrix.get("roles", {})  # type: ignore[assignment]

    catalog_rel = "config/models.yaml"
    catalog_path = root / catalog_rel
    if not catalog_path.is_file():
        return findings

    catalog = _load_yaml(catalog_path)
    for role, role_cfg in roles.items():
        model_name = role_cfg.get("all")
        if not isinstance(model_name, str):
            # Shape is check_matrix_shape's job; nothing to dereference here.
            continue
        if _resolved_model_definition(catalog, model_name) is None:
            findings.append(
                Finding(
                    check="dangling_model_reference",
                    severity="safety",
                    message=(
                        f"role '{role}' resolves to model '{model_name}', which has no "
                        f"matching entry under models: in {catalog_rel}"
                    ),
                )
            )
    return findings


def check_no_role_headers(root: Path) -> list[Finding]:
    """AC-2(a) — no config/*.yaml re-declares a role-assignment header (ADR-0099 D1 stage 4).

    Role assignment has lived ONLY in ``config/model_roles.yaml`` since stage 2 (FRE-650);
    the loader already ignores a ``<role>_role:`` header wherever it appears, so this can
    never wedge a boot (policy, not safety — ADR-0099 D4 defaults an unclassified finding
    to policy). But a reintroduced header is a maintainability regression that silently
    reopens the assignment-drift surface stage 2 closed, so stage 4 (FRE-652, the
    assembled-seam gate) makes the ADR's manual "grep returns zero" check a permanent
    CI/pre-commit gate instead of a one-time check.
    """
    findings: list[Finding] = []
    for path in sorted(root.glob("config/*.yaml")):
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _ROLE_HEADER_RE.match(line)
            if match is None:
                continue
            findings.append(
                Finding(
                    check="role_header_reintroduced",
                    severity="policy",
                    message=(
                        f"{path.relative_to(root)}:{lineno}: declares '{match.group(1)}_role:' — "
                        "role assignment lives only in config/model_roles.yaml "
                        "(ADR-0099 D1 stage 2, FRE-650)"
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


def check_field_descriptions(fields: Mapping[str, FieldInfo] | None = None) -> list[Finding]:
    """ADR-0099 D4 — every AppConfig field must carry a non-empty description.

    A regression ratchet, not a cleanup: every real field already complies.
    Accepts an injected *fields* mapping so it is unit-testable against a
    throwaway model without a filesystem fixture root (this check has no
    YAML/root surface, unlike the matrix/manifest checks).
    """
    if fields is None:
        from personal_agent.config.settings import AppConfig  # noqa: PLC0415 — avoid import cycle

        fields = AppConfig.model_fields

    findings: list[Finding] = []
    for name, field in fields.items():
        description = field.description
        if not isinstance(description, str) or not description.strip():
            findings.append(
                Finding(
                    "undocumented_field",
                    "policy",
                    f"AppConfig field '{name}' has no description (ADR-0099 D4)",
                )
            )
    return findings


def check_secret_field_plaintext_defaults(
    fields: Mapping[str, FieldInfo] | None = None,
) -> list[Finding]:
    """FRE-876 — no secret-marked field defaults to a real plaintext value.

    :func:`check_committed_secrets` (AC-8) only scans YAML/``.env`` text for a
    *committed* secret value; it never looks at a secret field's own Python
    default in ``settings.py``. A field may declare a documented, non-sensitive
    default (e.g. a local-only dev-convenience value already public elsewhere,
    such as a compose file's own hardcoded fallback) by adding
    ``"secret_default_allow": "<reason>"`` to its ``json_schema_extra`` alongside
    ``"secret": True`` — a considered exception, not a rubber stamp, mirroring
    the ``# fre-649-allow: <reason>`` convention :func:`check_committed_secrets`
    already uses. The exemption key on a field that is not itself secret-marked
    is flagged as metadata misuse (dead/contradictory declaration).

    A field declaring ``default_factory`` instead of a plain ``default`` is
    flagged unexempted too: ``field.default`` is ``PydanticUndefined`` in that
    case, so a hardcoded secret returned by the factory would otherwise pass
    silently — this static check has no safe way to invoke an arbitrary
    factory to inspect its return value.
    """
    if fields is None:
        from personal_agent.config.settings import AppConfig  # noqa: PLC0415 — avoid import cycle

        fields = AppConfig.model_fields

    findings: list[Finding] = []
    for name, field in fields.items():
        extra = field.json_schema_extra
        is_secret = _is_secret_marked(field)
        allow_reason = extra.get("secret_default_allow") if isinstance(extra, dict) else None
        is_exempted = isinstance(allow_reason, str) and bool(allow_reason.strip())

        if not is_secret:
            if allow_reason:
                findings.append(
                    Finding(
                        "secret_default_allow_without_secret_marker",
                        "policy",
                        f"AppConfig field '{name}' declares 'secret_default_allow' but is not "
                        "'secret'-marked — remove the unused exemption or add 'secret': True",
                    )
                )
            continue

        if is_exempted:
            continue

        default = field.default
        has_plaintext_default = isinstance(default, str) and bool(default.strip())
        has_factory = field.default_factory is not None
        if has_plaintext_default or has_factory:
            reason = (
                "declares a non-empty plaintext default"
                if has_plaintext_default
                else "declares a default_factory, whose return value this static check cannot "
                "introspect"
            )
            findings.append(
                Finding(
                    "secret_field_plaintext_default",
                    "policy",
                    f"AppConfig field '{name}' is secret-marked but {reason} in settings.py; "
                    "secrets must default to None and be supplied via environment (or add a "
                    "considered 'secret_default_allow' exemption)",
                )
            )
    return findings


def check_embedding_fallback_identity(settings: AppConfig | None = None) -> list[Finding]:
    """ADR-0112 AC-6 (FRE-821) — managed + local-fallback embedder configs pin the same revision.

    Strips an optional provider prefix (e.g. ``"Qwen/"``) from either side, then
    requires an **exact, case-sensitive** match of what remains — a fuzzy or
    substring comparison would not actually prove "same weights revision" (AC-6's
    bar). Output dimension and normalization/pooling are not checked here: both
    sides share the single ``embedding_dimensions`` request parameter by
    construction (no second field to drift), and pooling is a container command-
    line flag, not an AppConfig field — attested manually in the deploy runbook
    and cross-checked live by the probe script's rank-order sanity check.

    Args:
        settings: The ``AppConfig`` to compare. ``None`` constructs a fresh
            default instance (mirrors the other checks reading from the
            committed repo state, not a live singleton).

    Returns:
        A single-element list with one ``policy`` finding if the two model ids
        name different revisions, else an empty list.
    """
    from personal_agent.config.settings import AppConfig  # noqa: PLC0415 — avoid import cycle

    if settings is None:
        settings = AppConfig()

    managed = settings.managed_embedding_model
    local = settings.local_fallback_embedding_model
    if _strip_provider_prefix(managed) == _strip_provider_prefix(local):
        return []
    return [
        Finding(
            "embedding_fallback_identity_mismatch",
            "policy",
            f"managed_embedding_model {managed!r} and local_fallback_embedding_model "
            f"{local!r} do not name the same weights revision (ADR-0112 AC-6 requires "
            "an identical model on both sides for a same-space failover)",
        )
    ]


def _strip_provider_prefix(model_id: str) -> str:
    """Strip a leading ``'<org>/'`` provider prefix (e.g. ``'Qwen/'``), if present."""
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def check_matrix_shape(matrix: JSONDict) -> list[Finding]:
    """Every role must declare exactly one ``all:`` model key (FRE-650; FRE-916 phase 2).

    **Re-scoped by FRE-916 phase 2 (ADR-0121).** This check used to enforce the
    ``divergence: allowed | forbidden`` contract — that a ``forbidden`` role
    declare ``all`` and no per-profile value, and an ``allowed`` role the
    reverse. Collapsing to a single catalog removed the per-profile axis
    entirely, so the only remaining shape rule is the one that always carried the
    weight: a role resolves to exactly one declared key.

    Per-profile keys are now rejected outright rather than being one valid shape
    — a re-introduced ``local:``/``cloud:`` value would be silently ignored by
    :func:`~personal_agent.config.model_loader.resolve_role_model_key`, which is
    precisely the silent-assignment-drift failure this check exists to prevent.
    """
    findings: list[Finding] = []
    roles: dict[str, JSONDict] = matrix.get("roles", {})  # type: ignore[assignment]
    for role, role_cfg in roles.items():
        if not isinstance(role_cfg.get("all"), str):
            findings.append(
                Finding(
                    check="matrix_shape",
                    severity="policy",
                    message=f"role '{role}' declares no 'all' model key",
                )
            )
        stale = sorted(k for k in ("local", "cloud", "eval", "divergence") if k in role_cfg)
        if stale:
            findings.append(
                Finding(
                    check="matrix_shape",
                    severity="policy",
                    message=(
                        f"role '{role}' declares {stale}, which FRE-916 phase 2 retired "
                        "along with the second catalog. Only 'all' is read now, so these "
                        "would be silently ignored — the exact drift this check prevents."
                    ),
                )
            )
    return findings


def _compose_deployment_profiles(compose_yaml: JSONDict) -> set[str]:
    """Every distinct ``AGENT_DEPLOYMENT_PROFILE`` value set across a compose file's services.

    Handles both the mapping (``KEY: value``) and list (``- KEY=value``) forms
    docker-compose's ``environment:`` block allows. Merge keys (``<<: *anchor``)
    are already resolved by the time PyYAML hands back *compose_yaml*.
    """
    services = compose_yaml.get("services")
    if not isinstance(services, dict):
        return set()

    values: set[str] = set()
    for service in services.values():
        if not isinstance(service, dict):
            continue
        environment = service.get("environment")
        if isinstance(environment, dict):
            value = environment.get("AGENT_DEPLOYMENT_PROFILE")
            if isinstance(value, str):
                values.add(value)
        elif isinstance(environment, list):
            for item in environment:
                if isinstance(item, str) and item.startswith("AGENT_DEPLOYMENT_PROFILE="):
                    values.add(item.split("=", 1)[1])
    return values


def check_deployment_manifest_matches_compose(root: Path, manifest: JSONDict) -> list[Finding]:
    """AC-5 — a profile's compose file must declare that same profile (ADR-0099 D2.2, FRE-651).

    **Re-pointed in FRE-916 phase 2 (ADR-0121).** This guard used to cross-check
    each profile's ``AGENT_MODEL_CONFIG_PATH`` override against its compose file.
    That variable is gone — there is one catalog now — so the guard follows the
    provenance question onto the field that replaced it as the per-deployment
    discriminator: ``AGENT_DEPLOYMENT_PROFILE``, which keys the required-secret
    set. Getting it wrong is the same class of failure the old check caught: a
    deployment silently running under another profile's configuration.

    ``local`` is exempt from the must-declare half because it is the field's
    default; a compose file that declares nothing is correctly ``local``. Any
    profile that IS declared must match its manifest row.

    ADR-0099 D4 lists "provenance-manifest ≠ actual compose" explicitly under
    the *policy* severity class — a mismatch here must block CI/pre-commit but
    never wedge startup.
    """
    findings: list[Finding] = []
    profiles: dict[str, JSONDict] = manifest.get("profiles", {})  # type: ignore[assignment]

    for profile, row in profiles.items():
        compose_rel = row.get("compose_file")
        if not isinstance(compose_rel, str):
            continue

        compose_path = root / compose_rel
        if not compose_path.is_file():
            findings.append(
                Finding(
                    check="deployment_manifest_mismatch",
                    severity="policy",
                    message=(
                        f"profile '{profile}' declares compose_file={compose_rel!r} in "
                        "config/deployment.yaml, but that file does not exist"
                    ),
                )
            )
            continue

        declared = _compose_deployment_profiles(_load_yaml(compose_path))

        if not declared:
            if profile != "local":
                findings.append(
                    Finding(
                        check="deployment_manifest_mismatch",
                        severity="policy",
                        message=(
                            f"profile '{profile}' sets no AGENT_DEPLOYMENT_PROFILE in "
                            f"{compose_rel}, so it would boot as 'local' and enforce local's "
                            "required-secret set (config/model_roles.yaml)"
                        ),
                    )
                )
            continue

        if declared != {profile}:
            findings.append(
                Finding(
                    check="deployment_manifest_mismatch",
                    severity="policy",
                    message=(
                        f"profile '{profile}' is declared in config/deployment.yaml but "
                        f"{compose_rel} sets AGENT_DEPLOYMENT_PROFILE={sorted(declared)}"
                    ),
                )
            )
    return findings


def run_all_checks(root: Path) -> list[Finding]:
    """Run every check against *root* and return all findings."""
    matrix = load_matrix(root)
    manifest = load_deployment_manifest(root)

    findings: list[Finding] = []
    findings.extend(check_matrix_shape(matrix))
    findings.extend(check_dangling_model_references(root, matrix))
    findings.extend(check_no_role_headers(root))
    findings.extend(check_orphan_env_keys(root))
    findings.extend(check_committed_secrets(root))
    findings.extend(check_field_descriptions())
    findings.extend(check_secret_field_plaintext_defaults())
    findings.extend(check_deployment_manifest_matches_compose(root, manifest))
    findings.extend(check_substrate_manifest(root))
    findings.extend(check_dev_test_profile_isolation(root))
    findings.extend(check_embedding_fallback_identity())
    return findings
