# Velocity Tracking — AI-Assisted Development Metrics

> **Purpose**: Track implementation velocity in AI-assisted development context
> **Metric**: Batches per session (not story points or hours)
> **Version**: 1.0
> **Date**: 2025-12-28

---

## 📊 Why Track Velocity in AI-Assisted Dev?

Traditional metrics (story points, hours) don't fit AI-assisted development:

- **Code generation is fast** (AI writes most code)
- **Human bottlenecks**: Understanding requirements, making decisions, reviewing quality
- **Variable session length**: Work ends when outcome achieved, not after N hours

**New metric**: **Batches per session**

- **Batch** = Coherent implementation unit with verifiable outcome
- **Session** = Focused work period with clear goal
- **Velocity** = Batches completed / Session

---

## 🎯 Target Velocity

| Experience Level | Target Velocity | Notes |
|------------------|-----------------|-------|
| **First week** | 0.8-1.2 batches/session | Learning curve, setup overhead |
| **Weeks 2-3** | 1.5-2.0 batches/session | Normal productivity |
| **Week 4+** | 2.0-2.5 batches/session | High productivity, well-oiled |

**Factors affecting velocity**:

- ✅ Clear requirements → faster
- ✅ Familiar tech stack → faster
- ⚠️ Ambiguous scope → slower
- ⚠️ Unexpected blockers → slower
- ⚠️ Complex dependencies → slower

---

## 📋 Velocity Log

| Session Date | Goal | Planned Batches | Completed | Velocity | Phase | Notes |
|--------------|------|-----------------|-----------|----------|-------|-------|
| 2025-12-28 | Architecture kickoff | 5 (docs) | 5 | 1.0 | Planning | Vision doc, validation checklist, PR rubric, project plan, Captain's Log README |
| YYYY-MM-DD | [Goal] | X | Y | Y/X | [Phase] | [Blockers, insights] |

---

## 📈 Velocity Analysis

### Weekly Rollup

| Week | Sessions | Total Batches | Avg Velocity | Phase | Assessment |
|------|----------|---------------|--------------|-------|------------|
| 2025-W52 | 1 | 5 | 1.0 | Planning | On target for planning phase |
| YYYY-WXX | X | Y | Z | [Phase] | [Above/On/Below target] |

### Trend Analysis

- **Moving average** (last 5 sessions): [Calculate when enough data]
- **Velocity trend**: [Increasing / Stable / Decreasing]
- **Phase impact**: [How velocity changes between phases]

---

## 🔍 Batch Completion Breakdown

### By Type

| Batch Type | Count | Avg Time | Success Rate |
|------------|-------|----------|--------------|
| Code module | X | Y hours | Z% |
| Documentation | X | Y hours | Z% |
| Tests | X | Y hours | Z% |
| Configuration | X | Y hours | Z% |

**Insight**: [Which batch types are fastest? Which are bottlenecks?]

---

## 🚧 Blocker Impact Analysis

| Blocker Category | Frequency | Avg Time Lost | Mitigation Strategy |
|------------------|-----------|---------------|---------------------|
| LM Studio issues | X times | Y minutes | [Strategy] |
| Config errors | X times | Y minutes | [Strategy] |
| Test failures | X times | Y minutes | [Strategy] |
| Scope ambiguity | X times | Y minutes | [Strategy] |

---

## 💡 Velocity Improvement Actions

### Completed Improvements

| Date | Action | Expected Impact | Actual Impact |
|------|--------|-----------------|---------------|
| YYYY-MM-DD | [Improvement] | +X% velocity | [Measured] |

### Planned Improvements

| Priority | Action | Expected Impact | Target Date |
|----------|--------|-----------------|-------------|
| High | [Improvement] | +X% velocity | YYYY-MM-DD |
| Medium | [Improvement] | +X% velocity | YYYY-MM-DD |

---

## 🎓 Lessons Learned

### What Increases Velocity

- ✅ **[Factor]**: [How it helps]
- ✅ **[Factor]**: [How it helps]

### What Decreases Velocity

- ❌ **[Factor]**: [How it hurts]
- ❌ **[Factor]**: [How it hurts]

---

## 📊 Comparison: AI-Assisted vs Traditional

| Metric | Traditional | AI-Assisted | Ratio |
|--------|-------------|-------------|-------|
| Lines of code/hour | 50-100 | 500-1000 | 10x |
| Time on coding | 70% | 20% | 0.3x |
| Time on design/review | 30% | 80% | 2.7x |
| Batches/week | 3-5 | 8-15 | 2.5x |

**Takeaway**: AI amplifies output but shifts work to higher-level thinking.

---

## 🔄 Velocity Calibration

### When to Recalibrate

- **After 10 sessions**: Enough data for baseline
- **Between phases**: Different complexity levels
- **After major architectural change**: Reset expectations
- **If velocity diverges >30% from target**: Investigate root cause

### Calibration Process

1. **Review last 10 sessions**
2. **Calculate mean and variance**
3. **Identify outliers** (unusually high/low)
4. **Adjust target** if consistently above/below
5. **Document** in this file

---

## 🎯 Using Velocity for Planning

### How to Estimate

1. **Break work into batches** (well-defined outcomes)
2. **Count batches** for feature/phase
3. **Divide by target velocity** → sessions required
4. **Add buffer** (20-30% for unknowns)

**Example**:

- Feature: "Brainstem mode management"
- Batches: 4 (mode manager, sensors, orchestrator integration, tests)
- Target velocity: 2 batches/session
- Estimate: 4 / 2 = 2 sessions
- With buffer: 2 × 1.3 = ~3 sessions

---

## 📝 Quick Reference

### Calculate Velocity

```
Velocity = Completed Batches / Number of Sessions
```

### Estimate Sessions Needed

```
Sessions = Total Batches / Target Velocity × (1 + Buffer)
```

### Assess Performance

- **Above target (+20%)**: Excellent, sustainable?
- **On target (±10%)**: Good, maintain
- **Below target (-20%)**: Investigate blockers

---

## 🚀 Next Steps

1. **After each session**: Update velocity log
2. **Weekly**: Calculate rolling average
3. **Monthly**: Analyze trends, identify improvements
4. **Quarterly**: Recalibrate targets if needed

---

**Velocity tracking helps adapt plans to reality, not enforce rigid timelines.**
