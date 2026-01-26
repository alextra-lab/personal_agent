# GPU Metrics Access - Security Guide

## Overview

Accessing GPU metrics on Apple Silicon requires special permissions because `powermetrics` (Apple's official tool) requires root access. This document outlines secure approaches to collect GPU metrics.

## Recommended Solutions

### Option 1: Use `socpowerbud` (Recommended - No sudo, programmatic)

**socpowerbud** is a sudoless alternative to powermetrics that provides programmatic access.

**Installation:**

```bash
brew install socpowerbud
```

**Advantages:**

- ✅ No sudo required
- ✅ More secure (no elevated privileges)
- ✅ Provides programmatic JSON output
- ✅ Real-time CPU/GPU frequency, voltage, usage, power

**Usage:**

```bash
socpowerbud --json  # JSON output for programmatic access
```

**Implementation Status:** Can be added to `apple.py` to parse JSON output

### Option 2: Use `macmon` (Alternative - No sudo, interactive)

**macmon** uses private macOS APIs to access GPU metrics without requiring sudo.

**Installation:**

```bash
brew install macmon
```

**Advantages:**

- ✅ No sudo required
- ✅ More secure (no elevated privileges)
- ✅ Real-time monitoring
- ✅ Provides CPU, GPU, ANE, RAM, temperature

**Disadvantages:**

- ⚠️ Primarily interactive (limited programmatic access)
- ⚠️ Uses private APIs (may break with macOS updates)

**Usage:**

```bash
macmon  # Interactive display
```

### Option 3: Configure sudoers (Less Secure - Convenient)

If you must use `powermetrics`, you can configure sudo to allow it without a password for a specific user.

**⚠️ Security Warning:** This reduces security by allowing passwordless sudo for specific commands. Only use if you understand the risks.

**Configuration:**

1. Edit sudoers file:

```bash
sudo visudo
```

1. Add this line (replace `username` with your actual username):

```
username ALL=(ALL) NOPASSWD: /usr/bin/powermetrics
```

1. Save and exit

**Advantages:**

- ✅ Uses official Apple tool
- ✅ No password prompts
- ✅ Comprehensive metrics

**Disadvantages:**

- ⚠️ Requires sudo configuration (security risk)
- ⚠️ Still requires root privileges
- ⚠️ Not suitable for production/multi-user systems

### Option 4: Use Private macOS APIs (Most Complex - Most Secure)

Directly use macOS private APIs (IOKit, CoreGraphics) to access GPU metrics programmatically.

**Advantages:**

- ✅ No external tools
- ✅ No sudo required
- ✅ Full control

**Disadvantages:**

- ⚠️ Complex implementation
- ⚠️ Private APIs (may break with updates)
- ⚠️ Requires Objective-C/Swift or bindings

## Current Implementation

The code currently supports:

- ✅ `powermetrics` (requires sudo, fallback method)
- 📝 `macmon` detection (detected but not parsed - interactive tool)
- 📝 `socpowerbud` support (recommended for future implementation)

## Recommendation

For the Personal Agent project:

1. **Primary (Future):** Implement support for `socpowerbud` as it provides:
   - No sudo required
   - JSON output for programmatic access
   - Similar metrics to powermetrics

2. **Current:** Keep `powermetrics` support as fallback
   - Users can configure sudoers if they prefer this method
   - Most comprehensive metrics

3. **Alternative:** Use `macmon` for interactive monitoring
   - Good for manual checks
   - Not ideal for automated polling

## Implementation Example (socpowerbud)

To add socpowerbud support, add this to `apple.py`:

```python
def _poll_gpu_via_socpowerbud() -> dict[str, Any]:
    """Poll GPU metrics using socpowerbud (no sudo required)."""
    try:
        result = subprocess.run(
            ["socpowerbud", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            # Parse socpowerbud JSON structure
            # Extract GPU metrics
            return metrics
    except Exception:
        pass
    return {}
```

## References

- [macmon GitHub](https://github.com/vladkens/macmon)
- [socpowerbud GitHub](https://github.com/dehydratedpotato/socpowerbud)
- [macpm PyPI](https://pypi.org/project/macpm/)
- [Apple powermetrics man page](https://www.unix.com/man-page/macos/1/powermetrics/)

## Security Best Practices

1. **Prefer tools that don't require sudo** (socpowerbud, macmon)
2. **If using sudoers**, limit to specific commands only
3. **Never store sudo passwords** in code or config files
4. **Use principle of least privilege** - only grant what's needed
5. **Document security implications** for users
