# Path privacy (no local machine layout in repo)

> **Purpose:** Keep documentation and examples from embedding a specific developer’s filesystem layout.

## Rules

- Prefer **neutral placeholders** in docs and examples:
  - `<project-root>` — clone location of this repository
  - `$HOME` — user home directory (shell examples)
  - `<path-to-primary-repo-clone>` — when describing git worktrees vs. primary checkout
- **Avoid** concrete patterns that mirror one machine:
  - Absolute paths under the macOS multi-user home volume (the segment spelled `Users` after the root)
  - Home-relative shortcuts that encode a personal directory layout (e.g. `~` + `/Dev/` + project name)
  - Windows profile-style paths under `C:` + `\` + `Users` + `\` + account name

## Enforcement

Run:

```bash
uv run python scripts/check_no_personal_paths.py
```

This scans `git ls-files` text files and exits with a non-zero status if disallowed patterns appear.

Pre-commit can run the same check (see `.pre-commit-config.yaml`).

## Related

- Directory and doc hygiene: [PROJECT_DIRECTORY_STRUCTURE.md](PROJECT_DIRECTORY_STRUCTURE.md)
