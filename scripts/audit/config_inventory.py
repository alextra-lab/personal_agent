"""Config-inventory generator + verifier (FRE-648, ADR-0099 stage 0).

The canonical configuration inventory (`docs/reference/CONFIG_INVENTORY.md`) has one
machine-generated section — the ``AppConfig`` scalar table — and several hand-curated
sections (model-role matrix, profiles, governance, compose, findings). This module owns
the generated section and the completeness check that proves the whole document covers
every configuration parameter (FRE-648 acceptance criterion #2).

Two modes:

* ``generate`` — introspect ``AppConfig.model_fields``, cross-reference the ``AGENT_*``
  keys documented in ``.env.example``, and print the AppConfig markdown section to stdout.
  Paste the output between the ``AUTOGEN`` markers in the inventory doc.
* ``verify`` (default) — re-introspect and assert every ``AppConfig`` field name and every
  ``.env.example`` ``AGENT_*`` key appears somewhere in the committed inventory doc. Exits
  non-zero (and lists the gaps) if the document has drifted from the code. This is the
  runnable proof of AC#2 and a regression guard for future field additions.

Run from the repo root:

    uv run python scripts/audit/config_inventory.py verify
    uv run python scripts/audit/config_inventory.py generate
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pydantic.fields import FieldInfo

from personal_agent.config.settings import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_DOC = REPO_ROOT / "docs" / "reference" / "CONFIG_INVENTORY.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# Real secrets only: api-key / password / client-secret / secret-access-key suffixes,
# plus the one internal auth token (which does not end in _api_key). Deliberately does
# NOT match `*_max_tokens` / `*_token_weight` token-budget scalars.
_SECRET_HINT = re.compile(r"(_api_key$|_password$|_secret$|secret_access_key$)")
_SECRET_EXPLICIT: frozenset[str] = frozenset({"artifact_resolve_internal_token"})


def _is_schema_marked_secret(field: FieldInfo) -> bool:
    """A field's `json_schema_extra={"secret": True}` marker — the authoritative source.

    Some fields (e.g. the `managed_*` substrate-profile endpoints/URIs/tokens, ADR-0112)
    carry `_url`/`_endpoint`/`_token` suffixes the name regex below doesn't match.
    """
    extra = field.json_schema_extra
    return isinstance(extra, dict) and bool(extra.get("secret"))


# A documented env key is any `KEY=` assignment (commented or live) in .env.example.
# Anchoring to the start of the (optionally #-prefixed) line captures the full token —
# so PERSONAL_AGENT_EVAL is read whole, not as a spurious `AGENT_EVAL` substring.
_ENV_ASSIGN = re.compile(r"^[#\s]*([A-Z][A-Z0-9_]{2,})=", re.MULTILINE)

# AGENT_-namespace env keys documented in .env.example that intentionally have no
# AppConfig field because they are consumed elsewhere (model-definition loader or infra
# shell/compose), not by pydantic. Kept explicit so the orphan list flags only genuine
# surprises, and each entry is classified in the doc's Findings section.
_KNOWN_ENV_ONLY: frozenset[str] = frozenset(
    {
        "AGENT_EMBEDDING_ENDPOINT",  # model-def endpoint override (models.yaml loader)
        "AGENT_RERANKER_ENDPOINT",  # model-def endpoint override (models.yaml loader)
        "AGENT_MCP_SECRETS_FILE",  # docker/mcp/run-gateway.sh
        "AGENT_GATEWAY_TOKEN_PWA",  # gateway static-token auth (infra)
        "AGENT_GATEWAY_TOKEN_EXTERNAL_AGENT",  # gateway static-token auth (infra)
        "AGENT_CLOUDFLARE_TUNNEL_TOKEN",  # cloudflared container
    }
)


def _is_secret(name: str, field: FieldInfo) -> bool:
    """Secret iff schema-marked (authoritative) or the name matches the regex fallback."""
    return (
        _is_schema_marked_secret(field)
        or name in _SECRET_EXPLICIT
        or bool(_SECRET_HINT.search(name))
    )


# Hosts safe to print verbatim in a public-repo doc: loopback/wildcard binds and
# well-known public vendor endpoints. Any OTHER host in a default URL is treated as a
# deployment identifier and masked (avoids re-publishing prod domains — and hardcodes no
# org name here, so the script itself carries no identifier).
_PUBLIC_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "api.perplexity.ai"}
)
# Any URI (any scheme: http, postgresql, bolt, redis, amqp…) with OPTIONAL `user:pass@`
# userinfo and a host. Credentials embedded in a DSN default (e.g. the Postgres password
# in `postgresql+asyncpg://agent:pw@host/db`) must never reach a committed public doc —
# this is the clear-text-credential class CodeQL flags.
_URI = re.compile(r"([a-zA-Z][\w+.-]*://)([^/@\s'\"\]]+@)?([^/\s'\"\],:]+)")


def _sanitize_urls(text: str) -> str:
    """Strip embedded credentials and mask private deployment hosts in any URI literal."""

    def repl(m: re.Match[str]) -> str:
        scheme, userinfo, host = m.group(1), m.group(2), m.group(3)
        creds = "<redacted>@" if userinfo else ""
        shown_host = host if host in _PUBLIC_HOSTS else "<deployment-host>"
        return f"{scheme}{creds}{shown_host}"

    return _URI.sub(repl, text)


def _safe_default(name: str, field: FieldInfo) -> str:
    """Default cell for the table — never emits a secret value, DSN credential, or host.

    Secret-named fields render a redaction marker (defense-in-depth: even though every
    secret default is currently ``None``/empty, the value must never flow into a committed
    public doc). All other defaults are passed through :func:`_sanitize_urls`, which strips
    ``user:pass@`` credentials from *any* connection-string default (the Postgres-password
    DSN case) and masks private deployment hosts. Together these sever the clear-text
    sensitive-data flow CodeQL flags at the ``print`` sink.
    """
    if _is_secret(name, field):
        return "🔒 redacted (secret — `.env` only)"
    return _md_escape(_sanitize_urls(_default_repr(field)))


def _prefixed(name: str) -> str:
    """The always-valid `AGENT_<FIELD>` env name (prefix + field name)."""
    return f"AGENT_{name.upper()}"


def _alias(field: FieldInfo) -> str | None:
    """A field's alias, if it declares one (an *additional* accepted env name)."""
    alias = field.validation_alias if isinstance(field.validation_alias, str) else field.alias
    return alias if isinstance(alias, str) else None


def _env_cell(name: str, field: FieldInfo) -> str:
    """Markdown for the Env-var column: `AGENT_<FIELD>` plus a distinct alias if any."""
    prefixed = _prefixed(name)
    alias = _alias(field)
    if alias and alias != prefixed:
        return f"`{prefixed}` · `{alias}`"
    return f"`{prefixed}`"


def _accepted_env(name: str, field: FieldInfo) -> set[str]:
    """Every env var that binds this field.

    Empirically (pydantic-settings v2 + `env_prefix="AGENT_"`), a field binds from BOTH
    `AGENT_<FIELD>` (prefix + field name — always valid) AND its `alias` verbatim (prefix
    NOT applied). So `debug` accepts both `AGENT_DEBUG` and `APP_DEBUG`; `service_url`
    accepts both `AGENT_SERVICE_URL` and `SERVICE_URL`. The alias is additive, not a
    replacement — an aliased field therefore has two accepted spellings.
    """
    names = {_prefixed(name)}
    alias = _alias(field)
    if alias:
        names.add(alias)
    return names


def _default_repr(field: FieldInfo) -> str:
    """Render a field's default for the table without instantiating AppConfig."""
    if repr(field.default) != "PydanticUndefined" and field.default is not None:
        return f"`{field.default!r}`"
    if field.default_factory is not None:
        try:
            return f"`{field.default_factory()!r}`"  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001 - audit tool, factory may need args
            return "`<factory>`"
    return "**required**" if field.is_required() else "`None`"


def _type_str(field: FieldInfo) -> str:
    ann = field.annotation
    name = getattr(ann, "__name__", None)
    return (name or str(ann)).replace("typing.", "")


def _md_escape(text: str) -> str:
    """Escape a table cell so an embedded pipe (e.g. `str | None`) doesn't split columns."""
    return text.replace("|", "\\|")


def _env_keys_in_example() -> set[str]:
    """Every `KEY=` assignment documented in .env.example (commented or live)."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(_ENV_ASSIGN.findall(text))


def generate() -> str:
    """Return the machine-generated AppConfig markdown section."""
    fields = AppConfig.model_fields
    env_keys = _env_keys_in_example()
    all_accepted: set[str] = set()
    for n, f in fields.items():
        all_accepted |= _accepted_env(n, f)

    lines: list[str] = []
    lines.append(
        "<!-- AUTOGEN:AppConfig START — regenerate via scripts/audit/config_inventory.py generate -->"
    )
    lines.append("")
    lines.append(
        f"**{len(fields)} typed scalar/path parameters** live in "
        "`src/personal_agent/config/settings.py` (`AppConfig`, a pydantic `BaseSettings` "
        'with `env_prefix="AGENT_"`). Every field is read through the process-wide '
        "`from personal_agent.config import settings` singleton (`settings.<field>`), so "
        "the **reader** is uniformly that accessor; **validation** is pydantic type coercion "
        "at load (`AppConfig()` raises `ValidationError` on a bad value). The **Env var** "
        "column shows `AGENT_<FIELD>` (the prefix+name form, **always valid**); where a field "
        "also declares an `alias=`, that alias is shown after `·` as an **additional** "
        "accepted spelling — empirically both bind (e.g. `debug` accepts `AGENT_DEBUG` *and* "
        "`APP_DEBUG`). A field's default is overridable by either; the *profile-divergence* "
        "for scalars is the set of `docker-compose*.yml` `environment:` blocks that override "
        "it (see §8)."
    )
    lines.append("")
    lines.append(
        "| # | Field (`settings.X`) | Env var | Type | Default | Secret | In `.env.example` |"
    )
    lines.append("|---|---|---|---|---|---|---|")

    orphans = sorted(
        k
        for k in env_keys
        if k.startswith("AGENT_") and k not in all_accepted and k not in _KNOWN_ENV_ONLY
    )
    undocumented: list[str] = []

    for i, (name, field) in enumerate(sorted(fields.items()), start=1):
        documented = bool(_accepted_env(name, field) & env_keys)
        if not documented:
            undocumented.append(name)
        lines.append(
            f"| {i} | `{name}` | {_env_cell(name, field)} | `{_md_escape(_type_str(field))}` "
            f"| {_safe_default(name, field)} | {'🔑' if _is_secret(name, field) else ''} "
            f"| {'✅' if documented else '—'} |"
        )

    lines.append("")
    lines.append(f"### Orphan `AGENT_*` keys in `.env.example` ({len(orphans)})")
    lines.append("")
    lines.append(
        "`AGENT_*` keys documented in `.env.example` that bind to **no `AppConfig` field** "
        "(neither `AGENT_<FIELD>` nor any alias) and are not in the curated "
        f"consumed-elsewhere allow-list ({len(_KNOWN_ENV_ONLY)} entries: model-loader "
        "endpoints + infra scripts). A non-empty list here is a genuine surprise (dead doc "
        "or renamed field):"
    )
    lines.append("")
    if orphans:
        for key in orphans:
            lines.append(f"- `{key}`")
    else:
        lines.append(
            "_None — every documented `AGENT_*` key either binds a field (`AGENT_<FIELD>` or "
            "alias) or is a known consumed-elsewhere key._"
        )

    lines.append("")
    lines.append(f"### AppConfig fields not documented in `.env.example` ({len(undocumented)})")
    lines.append("")
    lines.append(
        "Fields with no matching env-var line in `.env.example` — the coverage gap ADR-0099 "
        "D4 flags as a *policy* finding (undocumented config surface):"
    )
    lines.append("")
    lines.append("<details><summary>%d undocumented fields</summary>" % len(undocumented))
    lines.append("")
    for name in undocumented:
        lines.append(f"- `{name}`")
    lines.append("")
    lines.append("</details>")

    # Emit only the COUNT of secret-heuristic fields — never the filtered list of secret
    # names (that flow is what CodeQL taints as clear-text logging). The names appear
    # generically in the table above and are enumerated in §8 of the hand-written doc.
    secret_count = sum(1 for n, f in fields.items() if _is_secret(n, f))
    lines.append("")
    lines.append(f"### Secret fields ({secret_count})")
    lines.append("")
    lines.append(
        f"{secret_count} `AppConfig` fields match the tightened secret heuristic "
        "(`*_api_key`, `*_password`, `*_secret`, `*secret_access_key`, plus the internal "
        "auth token; token-budget scalars like `*_max_tokens` are excluded). Their **values "
        "are never emitted** — the default column shows a redaction marker, and any "
        "credential embedded in a DSN default (Postgres/Neo4j) is stripped by the sanitizer. "
        "The field names are enumerated in **§8**; prod secrets live only in `.env` "
        "(ADR-0007)."
    )
    lines.append("")
    lines.append("<!-- AUTOGEN:AppConfig END -->")
    return "\n".join(lines)


def find_coverage_gaps() -> tuple[list[str], list[str]]:
    """Return (fields, agent_keys) present in code but absent from the inventory doc.

    Every AppConfig field name must appear as a backtick-quoted table row, and every
    ``AGENT_``-namespace key documented in ``.env.example`` must appear somewhere in the doc
    (its field row, or the orphan / consumed-elsewhere lists). Both lists empty == complete.

    Raises:
        FileNotFoundError: if the inventory doc does not exist.
    """
    doc = INVENTORY_DOC.read_text(encoding="utf-8")
    fields = AppConfig.model_fields
    agent_keys = {k for k in _env_keys_in_example() if k.startswith("AGENT_")}

    missing_fields = [name for name in fields if f"`{name}`" not in doc]
    missing_env = [key for key in sorted(agent_keys) if key not in doc]
    return missing_fields, missing_env


def verify() -> int:
    """Assert the committed doc covers every field + documented env key. Returns exit code."""
    if not INVENTORY_DOC.exists():
        print(f"FAIL: {INVENTORY_DOC.relative_to(REPO_ROOT)} does not exist", file=sys.stderr)
        return 1

    fields = AppConfig.model_fields
    agent_keys = {k for k in _env_keys_in_example() if k.startswith("AGENT_")}
    missing_fields, missing_env = find_coverage_gaps()

    ok = True
    print(f"AppConfig fields: {len(fields)} | .env.example AGENT_ keys: {len(agent_keys)}")
    if missing_fields:
        ok = False
        print(
            f"FAIL: {len(missing_fields)} AppConfig fields absent from inventory doc:",
            file=sys.stderr,
        )
        for name in missing_fields:
            print(f"  - {name}", file=sys.stderr)
    if missing_env:
        ok = False
        print(
            f"FAIL: {len(missing_env)} .env.example AGENT_ keys absent from inventory doc:",
            file=sys.stderr,
        )
        for key in missing_env:
            print(f"  - {key}", file=sys.stderr)

    if ok:
        print("PASS: every AppConfig field and documented AGENT_ env key appears in the inventory.")
        return 0
    return 1


def write_generated_section() -> int:
    """Splice a fresh AppConfig section between the AUTOGEN markers in the inventory doc.

    The generated section is written *to the file*, never logged to stdout — the tool
    handles secret-field metadata, so routing its output through a clear-text logging
    sink (``print``) is exactly what CodeQL flags; in-place file update avoids it and is
    the more useful workflow (no manual copy-paste). Only a non-sensitive status line
    (marker count) is printed.
    """
    section = generate()
    start_marker = "<!-- AUTOGEN:AppConfig START"
    end_marker = "<!-- AUTOGEN:AppConfig END -->"
    doc = INVENTORY_DOC.read_text(encoding="utf-8")
    start = doc.find(start_marker)
    end = doc.find(end_marker)
    if start == -1 or end == -1:
        print(f"FAIL: AUTOGEN markers not found in {INVENTORY_DOC.name}", file=sys.stderr)
        return 1
    updated = doc[:start] + section + doc[end + len(end_marker) :]
    INVENTORY_DOC.write_text(updated, encoding="utf-8")
    print(f"Updated AppConfig section in {INVENTORY_DOC.name}.")
    return 0


def main(argv: list[str]) -> int:
    """Dispatch to generate/verify based on argv[1] (default verify)."""
    mode = argv[1] if len(argv) > 1 else "verify"
    if mode == "generate":
        return write_generated_section()
    if mode == "verify":
        return verify()
    print(f"usage: {argv[0]} [generate|verify]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
