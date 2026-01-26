# Captain's Log Filename Improvements

**Date**: 2026-01-17
**Component**: `src/personal_agent/captains_log/manager.py`

## Problem

The Captain's Log filename format had three critical issues:

### 1. **Broken Sequence Numbering**
- **Bug**: Code searched for `.yaml` files but saved `.json` files
- **Impact**: All entries got sequence number `001`
- **Result**: No way to distinguish entry order

```python
# Before (BROKEN):
for file in log_dir.glob(f"CL-{date_str}-*.yaml"):  # ❌ Wrong extension
    match = pattern.match(file.stem)
```

### 2. **No Chronological Sorting**
- **Format**: `CL-2026-01-17-001-title.json`
- **Problem**: Date-only, no time component
- **Impact**: Can't sort entries created on same day

### 3. **No Scenario Tracking**
- **Problem**: No trace_id in filename
- **Impact**: Can't group/compare test scenario runs
- **Example**: Multiple runs of "system health check" scenario looked identical

## Solution

### New Filename Format

```
CL-<TIMESTAMP>-<TRACE_PREFIX>-<SEQ>-<TITLE>.json
```

**Components**:
- **TIMESTAMP**: `YYYYMMDD-HHMMSS` (sortable ISO timestamp)
- **TRACE_PREFIX**: First 8 chars of trace_id (scenario grouping)
- **SEQ**: 3-digit sequence (for same-second entries)
- **TITLE**: Sanitized task title (max 50 chars)

**Examples**:
```
Before: CL-2026-01-17-001-task-what-is-python.json
After:  CL-20260117-170613-a9e965fb-001-task-what-is-python.json

Before: CL-2026-01-17-001-task-system-health.json  (confusing!)
After:  CL-20260117-170614-b2c45de8-001-task-system-health.json
```

### Code Changes

**1. Fixed Extension Bug**
```python
# Before:
for file in log_dir.glob(f"CL-{date_str}-*.yaml"):  # ❌ Wrong!

# After:
for file in log_dir.glob(f"CL-{timestamp_str}-{trace_prefix}*.json"):  # ✅ Correct
```

**2. Added Timestamp**
```python
# Before:
date_str = date.strftime("%Y-%m-%d")  # Only date

# After:
timestamp_str = date.strftime("%Y%m%d-%H%M%S")  # Date + time
```

**3. Added Trace ID Support**
```python
# New parameter:
def _generate_entry_id(date: datetime | None = None, trace_id: str | None = None) -> str:
    trace_prefix = f"{trace_id[:8]}-" if trace_id else ""
    return f"CL-{timestamp_str}-{trace_prefix}{next_num:03d}"
```

**4. Updated Callers**
```python
# Extract trace_id from entry's telemetry_refs
trace_id = None
if entry.telemetry_refs and len(entry.telemetry_refs) > 0:
    trace_id = entry.telemetry_refs[0].trace_id
entry.entry_id = _generate_entry_id(entry.timestamp, trace_id=trace_id)
```

## Benefits

### 1. **Proper Sequence Numbering** ✅
- Sequential numbering now works correctly
- Files increment: `001`, `002`, `003`...
- No more duplicate `001` everywhere

### 2. **Chronological Sorting** ✅
```bash
# Files sort naturally by timestamp:
ls -1 telemetry/captains_log/
CL-20260117-170613-a9e965fb-001-task-what-is-python.json
CL-20260117-170614-b2c45de8-001-task-system-health.json
CL-20260117-170615-c3d56ef9-001-task-another-task.json
```

### 3. **Test Scenario Comparison** ✅
```bash
# Group by trace_id prefix:
grep -l "b2c45de8" telemetry/captains_log/*.json
# Shows all entries from same test scenario

# Compare runs:
diff \
  CL-20260117-170614-b2c45de8-001-task-system-health.json \
  CL-20260118-103422-b2c45de8-001-task-system-health.json
# Compare same scenario across different days
```

### 4. **Better Analysis** ✅
```bash
# Find all reflections for a specific trace:
rg "a9e965fb" telemetry/captains_log/

# Sort by time to see evolution:
ls -lt telemetry/captains_log/ | head -10

# Group by scenario for test analysis:
for trace in $(ls telemetry/captains_log/ | cut -d'-' -f4 | sort -u); do
  echo "Scenario: $trace"
  ls telemetry/captains_log/ | grep "$trace"
done
```

## Migration

**Old filenames remain unchanged** - this only affects new entries.

Existing files:
```
CL-2026-01-17-001-task-*.json  (old format, still readable)
```

New files:
```
CL-20260117-170613-a9e965fb-001-task-*.json  (new format)
```

Both formats can coexist. No migration script needed.

## Testing

To verify the fix:

```bash
# Run multiple tasks
python -m personal_agent.ui.cli "test task 1"
python -m personal_agent.ui.cli "test task 2"

# Check filenames include timestamps and trace IDs:
ls -lt telemetry/captains_log/ | head -5

# Verify no duplicate 001 numbers:
ls telemetry/captains_log/ | grep "001" | wc -l
# Should see different timestamps/traces, not all 001
```

## Related Files

- `src/personal_agent/captains_log/manager.py` - Entry ID generation
- `src/personal_agent/captains_log/AGENTS.md` - Documentation updated
- `architecture_decisions/captains_log/README.md` - Architecture docs

## Impact

- ✅ Better traceability of agent reflections
- ✅ Easier test scenario analysis
- ✅ Proper chronological ordering
- ✅ No breaking changes (backward compatible)
