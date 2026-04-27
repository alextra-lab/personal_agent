# list-directory — List directory contents with type and size

**Category:** `filesystem_read` · **Risk:** none · **Approval:** auto-approved in all non-LOCKDOWN modes

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
# Count YAML files
bash find /path -maxdepth 1 -mindepth 1 -name "*.yaml" | wc -l

# List Python files with sizes
bash find /path -maxdepth 1 -mindepth 1 -name "*.py" -printf '%s %f\n' | sort -rn
```

## Recursive listing

```bash
# All files under a directory, relative paths
bash find /path -type f | sort

# Limit depth
bash find /path -maxdepth 3 -type f -name "*.py" | sort
```

## Governance

- `ls` is auto-approved in NORMAL, ALERT, and DEGRADED — no PWA prompt.
- `find` is auto-approved in NORMAL only — in ALERT/DEGRADED it requires PWA approval. Prefer `ls -la` in ALERT/DEGRADED modes.
- Available in LOCKDOWN via the `read` primitive for single-file inspection (not directory listing).
- Read-only; neither `ls` nor `find` modifies the filesystem.
- Hidden files: `ls -la` includes dotfiles; `ls -l` omits them. Use `-la` to match legacy `list_directory` behaviour.
- See also: `bash.md` for the full auto-approve list and output cap (50 KiB).
