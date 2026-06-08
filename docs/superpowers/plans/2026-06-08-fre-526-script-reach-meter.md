# FRE-526 — Fix the blind demand meter: count + classify external `<script src>` reaches

**Linear:** FRE-526 (Approved · Tier-2:Sonnet · project *Artifact Execution Security*)
**Realizes:** ADR-0089 Addendum A1 (ticket #1). **Closes:** FRE-498 (master/Linear action).
**Constraint:** strictly **non-load-bearing** (ADR-0089 D1/D5) — nothing gates, strips, or rejects on these counts. The served-CSP envelope is the boundary.

## Problem

The only artifact-demand instrument is `cdn_count`, computed by `_CDN_LINK_RE`
(`src/personal_agent/tools/artifact_tools.py:792`), which matches **only**
`<link href="http…">` (stylesheets/fonts). It does **not** see
`<script src="https://…">` — the dominant CDN-reach vector (mermaid/charting/3-D).
The most common reach is invisible to telemetry.

## Approach

Add a **parallel, additive** measurement alongside the existing `cdn_count` label:
detect external `<script src="http(s)://…">` and classify each reach as

* **host-allowed** — origin matches the configured artifacts host **and** path starts with `/lib/` (a satisfied need).
* **host-blocked** — any other origin (an unmet capability demand — the real signal).

Emit three new fields on the existing `artifact_gate_decision` event:
`external_script_count`, `script_reach_allowed`, `script_reach_blocked`.

Do **not** repurpose `cdn_count` or touch `_count_sandbox_violations`'s `(script, handler, cdn)` tuple (ES field continuity).

## Files touched

1. `src/personal_agent/tools/artifact_tools.py` — new regex + classifier helper; new `_emit_gate_decision` params; compute at both commit sites.
2. `docker/elasticsearch/index-template.json` — explicit `long` properties for the 3 new fields.
3. `tests/personal_agent/tools/test_artifact_tools.py` — TDD unit tests.

No new tool, no governance entry, no `__init__.py` change.

## ES mapping walk (per team discipline — audit every new field)

The 3 new fields are integer counts. Walk each through `dynamic_templates` in
`index-template.json` (top-to-bottom, first match wins):

| field | `*_id`? | enums regex `^(.*_type\|_name\|_role\|_status\|_decision\|...)$`? | free_text? | default_string_keyword? | dynamic result | action |
|---|---|---|---|---|---|---|
| `external_script_count` | no | no (`_count`) | no | string-only (value is int) → no | dynamic `long` | add explicit `long` |
| `script_reach_allowed` | no | no (`_allowed`) | no | string-only → no | dynamic `long` | add explicit `long` |
| `script_reach_blocked` | no | no (`_blocked`) | no | string-only → no | dynamic `long` | add explicit `long` |

No name collides with a dynamic_template pattern. Integers map dynamically to
`long` already, but per ES-mapping discipline I add **explicit `long`
properties** (mirrors the FRE-512 envelope fields `http_status` / `probe_duration_ms`
added explicitly). No float fields, so no 0.0→long trap here.

## Implementation steps (TDD)

### Step 1 — Write failing tests
Add to `test_artifact_tools.py` (new section under the FRE-506 block):

* `test_classify_script_reaches_blocks_external_cdn` — `<script src="https://cdn.jsdelivr.net/npm/chart.js">` → `_classify_script_reaches(html) == (1, 0, 1)`.
* `test_classify_script_reaches_allows_lib_host` — with `settings.artifacts_public_base_url="https://artifacts.test"`, `<script src="https://artifacts.test/lib/katex@0.16.js">` → `(1, 1, 0)`.
* `test_classify_script_reaches_mixed` — one `/lib/` + one cdn → `(2, 1, 1)`.
* `test_classify_script_reaches_protocol_relative_cdn_blocked` — `<script src="//cdn.jsdelivr.net/x.js">` → `(1, 0, 1)` (codex finding: protocol-relative is a real reach vector — must be counted).
* `test_classify_script_reaches_ignores_inline_script` — `<script>alert(1)</script>` → `(0, 0, 0)`.
* `test_classify_script_reaches_ignores_empty_src` — `<script src=""></script>` → `(0, 0, 0)`.
* `test_classify_script_reaches_no_script_tags` — plain `<p>` doc → `(0, 0, 0)`.
* `test_classify_script_reaches_two_tags_one_line` — two `<script src>` on one line (one lib, one cdn) → `(2, 1, 1)`.
* `test_classify_script_reaches_no_base_url_all_blocked` — base URL None, `<script src="https://artifacts.test/lib/x.js">` → `(1, 0, 1)` (can't prove host-allowed without a configured host).
* `test_gate_decision_emits_script_reach_fields` — direct write of HTML with one cdn `<script src>` → gate event carries `external_script_count==1`, `script_reach_blocked==1`, `script_reach_allowed==0`.
* `test_gate_decision_script_reach_fields_zero_for_non_html` — JSON write → all three new fields `== 0`.
* `test_gate_decision_allowed_lib_reach` — direct write of HTML with a `https://artifacts.test/lib/…` script (base URL set by `_install_fakes`) → `script_reach_allowed==1`.

Run: `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` → confirm the new tests FAIL (helper/fields absent).

### Step 2 — Implement helper
In `artifact_tools.py`, near `_CDN_LINK_RE` (detection-only regex block), add:

```python
from urllib.parse import urlparse  # module-level import

# Matches <script ... src="…"> where src is an absolute (https://…) or
# protocol-relative (//cdn…) URL. Inline <script> blocks (no src) and
# relative-path srcs are not external reaches and are intentionally excluded:
# ADR-0089 A3 specifies the model uses absolute version-pinned /lib/ URLs, so a
# relative /lib/ path is not the expected reach form. Protocol-relative // is
# included because under the served https origin it resolves to a real CDN
# fetch (codex review). Non-load-bearing (ADR-0089 D1/D5).
_SCRIPT_SRC_RE = re.compile(
    r"""<\s*script\b[^>]*\bsrc\s*=\s*["']?((?:https?:)?//[^"'>\s]+)""",
    re.IGNORECASE,
)


def _artifacts_lib_netloc() -> str | None:
    """Return the lowercased netloc (host[:port]) of the configured artifacts host.

    A `<script src>` reach is *host-allowed* only when it targets this host's
    `/lib/` path — the single place the served CSP admits executable JS
    (ADR-0089 A3). Matching on netloc (not scheme) so an absolute https URL and
    a protocol-relative //host/lib/ reference to our own shelf both classify
    allowed. Without a configured host nothing can be proven allowed → None.
    """
    base = settings.artifacts_public_base_url
    if not base:
        return None
    netloc = urlparse(base).netloc
    return netloc.lower() or None


def _is_lib_reach(url: str, lib_netloc: str | None) -> bool:
    """True when `url` targets the artifacts host's `/lib/` path (a satisfied need)."""
    if lib_netloc is None:
        return False
    parsed = urlparse(url)
    return parsed.netloc.lower() == lib_netloc and parsed.path.startswith("/lib/")


def _classify_script_reaches(html: str) -> tuple[int, int, int]:
    """Count + classify external `<script src>` reaches for the analytics label.

    Non-load-bearing (ADR-0089 D1/D5, A1): the served-CSP envelope is the
    boundary; this only makes unmet-capability demand observable.

    Args:
        html: The artifact HTML text.

    Returns:
        ``(external_script_count, host_allowed, host_blocked)`` where
        host_allowed targets the artifacts `/lib/` shelf and host_blocked is
        any other origin (the real demand signal).
    """
    lib_netloc = _artifacts_lib_netloc()
    allowed = 0
    blocked = 0
    for url in _SCRIPT_SRC_RE.findall(html):
        if _is_lib_reach(url, lib_netloc):
            allowed += 1
        else:
            blocked += 1
    return (allowed + blocked, allowed, blocked)
```

### Step 3 — Thread through emit
* Add `script_reaches: tuple[int, int, int]` param to `_emit_gate_decision`; unpack and add `external_script_count`, `script_reach_allowed`, `script_reach_blocked` to the `log.info("artifact_gate_decision", ...)` call. Update docstring.
* In `artifact_write_executor`, compute `script_reaches = _classify_script_reaches(content)` for HTML, `(0, 0, 0)` otherwise; pass to `_emit_gate_decision`.

### Step 4 — ES template
Add after the FRE-512 envelope properties in `index-template.json`:
```json
"external_script_count": { "type": "long" },
"script_reach_allowed":  { "type": "long" },
"script_reach_blocked":  { "type": "long" },
```

### Step 5 — Quality gates
* `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` → all green.
* `make test` (full unit suite) → green.
* `make mypy` · `make ruff-check` · `make ruff-format` → clean.
* `pre-commit run --all-files` → clean.
* Validate ES JSON: `python -c "import json; json.load(open('docker/elasticsearch/index-template.json'))"`.

## Acceptance (from ticket)

* ✅ Unit test: cdn `<script src>` → `script_reach_blocked ≥ 1`; `/lib/` reference → `script_reach_allowed ≥ 1`.
* ✅ New fields indexed correctly (explicit `long`, verified against template).
* ✅ Non-load-bearing — no gate/strip/reject added; `cdn_count` untouched.
* Closes FRE-498 (master action on merge).

## Out of scope
Hosting the toolkit (A3/#2), prompt reframe (#3), skill doc (#4), export (#5).
The classifier is the meter only.
