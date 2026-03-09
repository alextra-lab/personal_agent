# Dependency Security

This document tracks known dependency vulnerabilities and mitigations.

## Current status

| Package      | CVE            | Status | Notes |
|-------------|----------------|--------|--------|
| cryptography | CVE-2026-26007 | **Fixed** | Overridden to `>=46.0.5` in `[tool.uv]` (pyproject.toml). |
| diskcache    | CVE-2025-69872 | **No fix yet** | Transitive via `dspy`. Unsafe pickle deserialization; no patched release on PyPI. |

## diskcache (CVE-2025-69872)

- **Affected:** diskcache through 5.6.3 (used by dspy for caching).
- **Issue:** Default pickle serialization allows arbitrary code execution if an attacker can write to the cache directory.
- **Mitigation:** No fixed version exists. When upstream releases a patched version, add it to `[tool.uv] override-dependencies` and run `uv lock`.
- **Audit:** To run `pip-audit` without failing on this CVE until a fix is released:
  ```bash
  uv run pip-audit --ignore-vuln CVE-2025-69872
  ```

## Auditing

With dev dependencies installed:

```bash
uv sync --extra dev
uv run pip-audit
```

To fix what can be fixed automatically (and respect overrides):

```bash
uv lock   # after adding override-dependencies
uv sync --extra dev
uv run pip-audit
```
