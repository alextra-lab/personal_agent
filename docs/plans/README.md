# Plans Directory — Project Planning & Session Logs

> **Purpose**: Track project planning, velocity, and development session history
> **Philosophy**: Adaptive planning for AI-assisted development

---

## 📂 Directory Structure

```
./
├── README.md (this file)
├── PROJECT_PLAN_v0.1.md              # Current project plan, adaptive methodology
├── VELOCITY_TRACKING.md              # Velocity metrics (batches/session)
├── IMPLEMENTATION_ROADMAP.md         # Detailed 4-week MVP roadmap
├── sprints/ (optional)               # Sprint plans if using sprint model
└── sessions/                         # Session logs (one per work session)
    ├── SESSION_TEMPLATE.md           # Template for session logs
    └── SESSION-YYYY-MM-DD-*.md       # Actual session logs
```

---

## 🎯 What Goes Here

### Project Plan (`PROJECT_PLAN_v0.1.md`)

- **High-level goals and milestones**
- **Phase breakdown** (MVP phases, dependency sequencing)
- **Planning methodology** (batches, sessions, velocity)
- **Decision points and quality gates**
- **Risk register and adaptation strategy**

**Update frequency**: When major changes occur (scope change, pivot, phase completion)

---

### Velocity Tracking (`VELOCITY_TRACKING.md`)

- **Batches per session metric**
- **Velocity log** (table of all sessions)
- **Trend analysis** (moving average, phase comparisons)
- **Blocker impact tracking**
- **Velocity improvement actions**

**Update frequency**: After every session

---

### Implementation Roadmap (`./IMPLEMENTATION_ROADMAP.md`)

- **Week-by-week breakdown**
- **Module structure** (directory tree to create)
- **Critical path dependencies**
- **Testing strategy**
- **Success metrics**

**Update frequency**: Weekly or when priorities shift

---

### Session Logs (`sessions/SESSION-*.md`)

**Each work session produces a log** documenting:

- Goal and planned batches
- Outcomes (completed, deferred, blocked)
- Decisions made
- Velocity achieved
- Learnings and insights
- Next session prep

**Naming**: `SESSION-YYYY-MM-DD-short-description.md`

**Update frequency**: End of every work session (2-4 hours typically)

---

## 🔄 Planning Workflow

### 1. Start of Phase

- Review/update `PROJECT_PLAN_v0.1.md`
- Set phase goals and exit criteria
- Break phase into sessions

### 2. Start of Session

- Create new session log from `SESSION_TEMPLATE.md`
- Define goal and planned batches
- Check prerequisites

### 3. During Session

- Work on batches
- Document decisions inline
- Note blockers as they occur

### 4. End of Session

- Update session log (outcomes, velocity, learnings)
- Update `VELOCITY_TRACKING.md` (add row to velocity log)
- Prep next session (prerequisites, proposed goal)

### 5. End of Phase

- Review phase outcomes vs. exit criteria
- Retrospective (what worked, what didn't)
- Update `PROJECT_PLAN_v0.1.md` with lessons learned

---

## 📊 How to Use Velocity Tracking

### Calculate Your Velocity

After each session:

```
Velocity = Completed Batches / Number of Sessions
```

Example:

- Session 1: Planned 3 batches, completed 2 → Velocity 2/1 = 2.0
- Session 2: Planned 2 batches, completed 1.5 → Velocity 1.5/1 = 1.5
- Average: (2.0 + 1.5) / 2 = 1.75 batches/session

### Use Velocity to Estimate

```
Sessions Needed = Total Batches / Your Velocity × (1 + Buffer)
```

Example:

- Feature needs 6 batches
- Your velocity: 1.75 batches/session
- Estimate: 6 / 1.75 × 1.3 = ~4.5 sessions

---

## 🎓 Session Log Best Practices

### Do

✅ **Write immediately after session** (while fresh)
✅ **Be specific about outcomes** (what actually got done)
✅ **Document decisions made** (with rationale)
✅ **Note blockers and resolutions** (learn from them)
✅ **Calculate velocity honestly** (partial batches count fractionally)
✅ **Prep next session** (clear entry point)

### Don't

❌ **Retroactively write logs** (memory fades, details lost)
❌ **Inflate outcomes** (be honest about what's incomplete)
❌ **Skip velocity calculation** (data drives improvement)
❌ **Omit blockers** (hiding problems doesn't help)
❌ **Write novels** (concise is better, use bullet points)

---

## 📈 Velocity Targets

| Phase | Target Velocity | Rationale |
|-------|-----------------|-----------|
| **Planning** | 0.8-1.2 | Documentation-heavy, fast |
| **Week 1 (Foundation)** | 0.8-1.2 | Learning curve, setup |
| **Weeks 2-3 (Building)** | 1.5-2.0 | In rhythm |
| **Week 4+ (Polishing)** | 2.0-2.5 | High productivity |

**Adjust targets** based on actual velocity after 5-10 sessions.

---

## 🔍 When to Re-Plan

Trigger a planning review if:

- Velocity diverges >30% from target for 3+ sessions
- Major blocker emerges that shifts priorities
- Scope change requested (add/remove features)
- Architectural pivot needed

**Process**: Pause, assess, re-sequence, update plan, resume.

---

## 🚀 Quick Start

### First Session

1. Copy `SESSION_TEMPLATE.md` to `sessions/SESSION-YYYY-MM-DD-first-implementation.md`
2. Fill in goal and planned batches
3. Work on batches
4. Update outcomes and velocity at end
5. Add row to `VELOCITY_TRACKING.md`

### Ongoing

- Repeat for every work session
- Review velocity weekly
- Adjust estimates based on actual velocity

---

## 📚 Related Documents

- `../docs/VISION_DOC.md` — Why we're building this, philosophy
- `../docs/PROJECT_DIRECTORY_STRUCTURE.md` — Where things live
- `../architecture_decisions/` — Technical decisions (ADRs)
- `../ROADMAP.md` — High-level project timeline

---

**The plans directory turns development from chaos into disciplined, measurable progress.**
