# macmon GPU Metrics Collection

## Overview

We've implemented support for collecting GPU metrics from `macmon`, a sudoless tool for Apple Silicon that uses private macOS APIs.

## GPU Metrics We Collect

From macmon's JSON output, we extract:

1. **perf_system_gpu_load** (GPU Utilization)
   - Source: `gpu_usage[1] * 100` (converts ratio to percentage)
   - Example: `7.65%` (from `gpu_usage: [338, 0.0765]`)

2. **perf_system_gpu_power_w** (GPU Power Consumption)
   - Source: `gpu_power` field
   - Example: `0.489 W`

3. **perf_system_gpu_temp_c** (GPU Temperature)
   - Source: `temp.gpu_temp_avg` field
   - Example: `46.13°C`

## Expected JSON Structure

macmon outputs JSON lines with this structure:

```json
{
  "gpu_power": 0.48911234736442566,
  "gpu_usage": [338, 0.07654736191034317],
  "temp": {
    "gpu_temp_avg": 46.13256072998047
  },
  "timestamp": "2026-01-01T15:29:00.546896+00:00",
  "cpu_power": 0.549686849117279,
  "all_power": 1.0387991666793823,
  ...
}
```

## Implementation Status

✅ **Code Implementation**: Complete

- Parsing logic implemented in `_poll_gpu_via_macmon()`
- Extracts all three GPU metrics
- Handles errors gracefully

⚠️ **Subprocess Capture**: May need adjustment

- macmon may require interactive/TTY mode
- Some systems may need different subprocess handling
- Code is ready but may need system-specific tuning

## Testing

To test macmon output manually:

```bash
# Run macmon pipe and capture first line
macmon pipe | head -1 | python3 -m json.tool
```

Expected output shows GPU metrics in the structure above.

## Alternative: socpowerbud

For more reliable programmatic access, consider `socpowerbud`:

- Also sudoless
- Designed for programmatic use
- JSON output format
- Install: `brew install socpowerbud`

## Code Location

Implementation: `src/personal_agent/brainstem/sensors/platforms/apple.py`

- Function: `_poll_gpu_via_macmon()`
- Called by: `poll_apple_gpu_metrics()` (tries macmon first, then powermetrics)
