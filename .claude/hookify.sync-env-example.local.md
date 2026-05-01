---
name: sync-env-example
enabled: true
event: file
conditions:
  - field: file_path
    operator: ends_with
    pattern: settings.py
---

**You just edited `settings.py`.**

Check whether you added any new `Field(...)` definitions. If so, you MUST also update `.env.example` before finishing:

1. Identify any new `AGENT_` (or other) env var fields added to `settings.py`
2. Check if they appear in `.env.example`
3. If missing, add them with a commented-out default and a short description — matching the style of the surrounding section

This has been missed repeatedly and causes the live `.env` to drift from the template.
