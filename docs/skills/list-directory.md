# list-directory — List directory contents with type and size

**Category:** `filesystem_read` · **Risk:** none · **Approval:** auto-approved in all non-LOCKDOWN modes

## Counting files — use bash find, not list_directory

`list_directory` is **non-recursive** — it sees only the top level. For any question asking "how many files" or "how many X files under a path", use `bash find` directly:

```bash
# Count all YAML files recursively (the right approach)
bash find /app/config -name "*.yaml" | wc -l

# Count all Python files recursively
bash find /app/src -name "*.py" | wc -l
```

This is a single tool call and returns the correct total across all subdirectories. Using `list_directory` followed by drilling into each subdir one-by-one takes 4–6 extra turns.

## Quick reference

| Task | Command |
|------|---------|
| Count files matching pattern **recursively** | `bash find /path -name "*.yaml" \| wc -l` |
| List current directory | `bash ls -la /path` |
| Count files at top level only | `bash find /path -maxdepth 1 -name "*.yaml" \| wc -l` |
| All files recursively | `bash find /path -type f \| sort` |

## Basic listing

```bash
# Human-readable, shows hidden files (matches legacy tool default)
bash ls -la /path/to/dir

# Shell auto-expands ~ and $HOME
bash ls -la ~/
bash ls -la /app/agent_workspace/
```

## Structured listing (machine-parseable)

Use `find` when you need to parse type and size programmatically:

```bash
# Format: <type> <size_bytes> <filename>  (f=file, d=directory)
bash find /path -maxdepth 1 -mindepth 1 -printf '%y %s %f\n' | sort
```

Example output:
```
d 4096 config
f 12480 main.py
f 2048 README.md
```

## Files only / directories only

```bash
# Files only
bash find /path -maxdepth 1 -mindepth 1 -type f | sort

# Directories only
bash find /path -maxdepth 1 -mindepth 1 -type d | sort

# Files only, with size
bash find /path -maxdepth 1 -mindepth 1 -type f -printf '%s %f\n' | sort -n
```

## Filtering by pattern

```bash
# Count files by extension — RECURSIVE (no depth limit, spans all subdirectories)
bash find /path -name "*.yaml" | wc -l

# Count files by extension — NON-RECURSIVE (current directory only)
bash find /path -maxdepth 1 -name "*.yaml" | wc -l

# List Python files with sizes (current dir only)
bash find /path -maxdepth 1 -type f -name "*.py" -printf '%s %f\n' | sort -rn
```

## Recursive listing

```bash
# All files under a directory, relative paths
bash find /path -type f | sort

# Limit depth
bash find /path -maxdepth 3 -type f -name "*.py" | sort
```

## Gotchas

**Count files recursively in one call — do not explore subdirectories one by one:**

```bash
# CORRECT — single pipe, counts across all subdirs, 2 turns max
bash find /app/config -name "*.yaml" | wc -l

# WRONG — exploring each subdir manually burns 4-6 turns and 25K extra tokens
bash ls /app/config/governance    # turn 2
bash ls /app/config/profiles      # turn 3  ... etc.
```

`-maxdepth 1` limits to the current directory only. Drop it when the question asks "under" or "in" a path — those words imply recursive search across subdirectories.

## Governance

- `ls` is auto-approved in NORMAL, ALERT, and DEGRADED — no PWA prompt.
- `find` is auto-approved in NORMAL only — in ALERT/DEGRADED it requires PWA approval. Prefer `ls -la` in ALERT/DEGRADED modes.
- Available in LOCKDOWN via the `read` primitive for single-file inspection (not directory listing).
- Read-only; neither `ls` nor `find` modifies the filesystem.
- Hidden files: `ls -la` includes dotfiles; `ls -l` omits them. Use `-la` to match legacy `list_directory` behaviour.
- See also: `bash.md` for the full auto-approve list and output cap (50 KiB).
