# GPU Metrics Setup Guide

## Problem

Apple Silicon GPU metrics require privileged access to system APIs. The personal agent needs GPU monitoring for:
- LLM performance tracking (GPU is heavily used)
- Homeostatic control loops (detect GPU overload)
- Captain's Log insights (GPU utilization context)

## Solution: Sudo-less powermetrics

Configure sudo to allow `powermetrics` without password prompts, enabling background GPU monitoring.

---

## Installation Steps

### 1. Create sudoers rule

```bash
# Edit sudoers configuration (safe method)
sudo visudo -f /etc/sudoers.d/powermetrics
```

### 2. Add permission rule

Add this **exact line** to the file:

```
%admin ALL=(root) NOPASSWD: /usr/bin/powermetrics
```

**Explanation:**
- `%admin` - Members of admin group (includes your user)
- `ALL=(root)` - Can run as root on all hosts
- `NOPASSWD:` - No password prompt required
- `/usr/bin/powermetrics` - **Only** this specific command (secure!)

### 3. Save and exit

In `visudo`:
- Press `Ctrl+X` to exit
- Press `Y` to confirm save
- Press `Enter` to confirm filename

### 4. Verify configuration

```bash
# Should run without password prompt:
sudo powermetrics -n 1 -i 1000 --samplers gpu_power

# Should see GPU metrics output (no password prompt)
```

### 5. Test with agent

```bash
cd ~/Dev/personal_agent
python -m personal_agent.ui.cli "Hello, test GPU metrics"

# Check logs for:
# ✅ gpu_metrics_via_powermetrics
# ✅ perf_system_gpu_load (not None)
```

---

## Security Considerations

### ✅ Secure Approach

This configuration is **more secure** than general sudo access because:

1. **Limited scope**: Only `powermetrics` command allowed
2. **Specific path**: Must use `/usr/bin/powermetrics` (not custom scripts)
3. **No escalation**: Cannot run other commands without password
4. **Auditable**: All powermetrics calls logged via sudo

### Alternative: Full sudo access (NOT RECOMMENDED)

```bash
# DON'T DO THIS - grants unlimited sudo:
%admin ALL=(ALL) NOPASSWD: ALL  # ❌ INSECURE
```

Our approach is equivalent to:
- Docker requiring privileged access for metrics
- Prometheus node_exporter running with limited privileges
- System monitoring tools with specific permissions

---

## Troubleshooting

### Issue: "sudo: a terminal is required"

**Cause**: Running in non-interactive mode (background task)

**Solution**: Already handled - sudoers rule allows non-interactive sudo

### Issue: Still prompts for password

**Cause**: sudoers file not saved correctly or syntax error

**Solution**:
```bash
# Check sudoers syntax
sudo visudo -c -f /etc/sudoers.d/powermetrics

# Should output: parsed OK
```

### Issue: Permission denied

**Cause**: User not in admin group

**Solution**:
```bash
# Check group membership
groups

# Should include 'admin'

# If not, add to admin group:
sudo dseditgroup -o edit -a $USER -t user admin
```

### Issue: GPU metrics still None

**Cause**: powermetrics output parsing failed

**Solution**: Check logs for `powermetrics_invalid_json` warnings

---

## Verification Checklist

- [ ] sudoers rule created (`/etc/sudoers.d/powermetrics`)
- [ ] Rule syntax verified (`sudo visudo -c ...`)
- [ ] Manual test works (no password prompt)
- [ ] Agent captures GPU metrics (not None)
- [ ] Captain's Log includes GPU utilization

---

## Reverting (If Needed)

To remove sudo-less access:

```bash
sudo rm /etc/sudoers.d/powermetrics
```

GPU metrics will fall back to None (graceful degradation).

---

## Alternative Tools (Not Used)

- **macmon**: Broken on macOS 14+ (`Failed to create subscription`)
- **asitop**: Requires separate Python package, less tested
- **ioreg**: Limited GPU metrics, complex parsing

**powermetrics** is Apple's official tool, most reliable and secure.

---

## References

- Apple powermetrics documentation: `man powermetrics`
- sudoers documentation: `man sudoers`
- Security best practices: Only grant minimal required permissions
- Homeostasis model: GPU metrics enable control loop decisions

**Created**: 2026-01-17  
**Status**: Production-ready
