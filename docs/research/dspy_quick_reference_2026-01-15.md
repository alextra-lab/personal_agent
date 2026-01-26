# DSPy Quick Reference for Personal Agent

**Date**: 2026-01-15  
**Full Analysis**: `./dspy_framework_analysis_2026-01-15.md`  
**Experiment**: `../architecture_decisions/experiments/E-008-dspy-prototype-evaluation.md`  
**Hypothesis**: H-008 in `../architecture_decisions/HYPOTHESIS_LOG.md`

---

## TL;DR: What is DSPy?

**DSPy** = **D**eclarative **S**elf-improving **Py**thon (Stanford NLP framework)

**Core Idea**: Treat LLM programs as **code, not strings**

- **Signatures**: Declare _what_ the LM should do (typed input/output)
- **Modules**: Reusable reasoning patterns (ChainOfThought, ReAct, Predict)
- **Optimizers**: Automatically improve prompts/weights from data (MIPROv2, BootstrapFewShot)

**Example**:

```python
# Instead of manual prompts...
prompt = "Given question, provide detailed answer with reasoning..."

# Use declarative signatures
class AnswerQuestion(dspy.Signature):
    """Answer questions with reasoning."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
    confidence: float = dspy.OutputField()

# Use composable modules
qa = dspy.ChainOfThought(AnswerQuestion)
result = qa(question="What is DSPy?")

# Later: optimize with data
optimizer = dspy.MIPROv2(metric=answer_quality, auto="light")
optimized_qa = optimizer.compile(qa, trainset=examples)
```

---

## Strategic Fit with Personal Agent

### ✅ **Strong Alignments**

| Our Value | DSPy Feature | Synergy |
|-----------|--------------|---------|
| Type-safe, explicit interfaces | Pydantic-based signatures | Natural extension of our architecture |
| Composable, testable modules | DSPy modules (Predict, ReAct, etc.) | Matches control loop design |
| Evidence-based improvement | Optimizers (systematic prompt tuning) | Aligns with HDD approach |
| Local LLM support | OpenAI-compatible endpoint support | Works with LM Studio |
| Research-oriented learning | Stanford NLP research project | High learning value |

### ⚠️ **Tensions & Concerns**

| Concern | Impact | Mitigation |
|---------|--------|-----------|
| Framework complexity | Adds abstraction layer | Selective adoption (Option B) |
| Optimization dependencies | Needs datasets + metrics | Defer optimization to post-MVP |
| Overlaps with `instructor` (ADR-0010) | Two solutions for structured outputs | Choose one or complement carefully |
| Learning curve | 2-3 days to learn basics | Time-boxed prototype validates fit first |

---

## Three Adoption Options

### Option A: Full Adoption (Ambitious)

**Use DSPy for all LLM interactions**

**Pros**: Systematic optimization, composable modules, research-backed  
**Cons**: Framework lock-in, learning curve, abstraction complexity  
**When**: Prototype validates strong fit, committed to building eval datasets

---

### Option B: Selective Adoption (Pragmatic) ⭐ **RECOMMENDED**

**Use DSPy for complex workflows, keep simple approaches for simple cases**

**Use DSPy for**:

- Captain's Log reflection (complex structured output)
- Future cognitive architecture (planning, metacognition)
- Optimizers as tools (extract patterns, don't use framework)

**Keep `instructor` or manual for**:

- Simple structured outputs
- Cases where we need tight control
- MVP orchestrator core

**Pros**: Best of both worlds, low risk, flexible, reversible  
**Cons**: Two abstractions (DSPy + instructor)  
**When**: Pragmatic balance during pre-implementation phase

---

### Option C: Defer to Post-MVP (Conservative)

**Wait until MVP complete, then evaluate with real data**

**Pros**: Focus on MVP, validate with production data  
**Cons**: Manual prompt engineering may be inefficient, harder to refactor later  
**When**: Prototype shows poor fit or complexity outweighs benefits

---

## Immediate Action: Prototype (E-008)

**Timeline**: Week 5, Days 26-27 (1-2 days, time-boxed)

**Test 3 Use Cases**:

1. **Captain's Log reflection**: Manual prompt vs. DSPy ChainOfThought
2. **Router decision**: Manual prompt vs. DSPy signature
3. **Tool-using agent**: Manual orchestrator vs. DSPy ReAct

**Success Criteria (proceed to integration)**:

- ✅ ≥1 test case shows ≥30% code reduction
- ✅ LM Studio compatibility confirmed
- ✅ Telemetry integration feasible
- ✅ No showstopper issues (performance, debugging)

**Failure Criteria (defer DSPy)**:

- ❌ No test cases show benefit
- ❌ LM Studio incompatibility
- ❌ Telemetry integration infeasible
- ❌ Debugging significantly harder

---

## Key Learning Opportunities

Even if we don't adopt DSPy, studying it provides:

### 1. **Technical Patterns**

- Advanced prompt engineering (few-shot, chain-of-thought)
- Program synthesis & optimization (bootstrapping, discrete search)
- Evaluation-driven development (metrics, LM-as-judge)
- Modular LM system architecture

### 2. **Conceptual Shifts**

- Programming vs. prompting mindset (declarative interfaces)
- Systematic vs. ad-hoc optimization (search, not just design)
- Composability as evolvability (small modules, big systems)

### 3. **Research Insights**

- Stanford NLP papers (MIPROv2, GEPA, SemanticF1)
- Production use cases (STORM, IReRa, Haize red-teaming)
- Community expertise (250+ contributors, active Discord)

---

## Integration Roadmap (If Adopted)

### Week 5: Prototype & Validate

- **Day 26-27**: Run E-008 (test 3 use cases)
- **Decision point**: Adopt (A/B) or Defer (C)

### Week 5-6: Selective Integration (if Option B)

- **Day 31-32**: Migrate Captain's Log to DSPy
- **Day 33-35**: Extract routing patterns, apply manually
- **Measure**: Code reduction, parse failures, maintainability

### Week 6+: Optimization & Expansion (post-MVP)

- **Build evaluation harness** (Day 26-28 already planned)
- **Run MIPROv2 optimizer** on Captain's Log reflection
- **Expand to cognitive architecture** (Weeks 8-16)
- **Document learnings** (ADR update, blog post)

---

## Quick Decision Tree

```
Start: Should we adopt DSPy?
│
├─ Run E-008 prototype (Days 26-27)
│  │
│  ├─ Results: ≥1 clear benefit
│  │  │
│  │  ├─ All 3 use cases better? → Option A (Full Adoption)
│  │  ├─ Mixed results? → Option B (Selective) ⭐
│  │  └─ 1 use case better? → Option B (Selective) ⭐
│  │
│  └─ Results: No clear benefits / showstoppers
│     └─ Option C (Defer to post-MVP)
│
└─ Can't prototype now?
   └─ Option C (Defer to post-MVP)
```

---

## Resources

### Documentation

- **DSPy Website**: <https://dspy.ai>
- **GitHub**: <https://github.com/stanfordnlp/dspy>
- **Discord**: <https://discord.gg/XCGy2XXKRS>

### Papers

- **DSPy Paper**: "Compiling Declarative Language Model Calls into Self-Improving Pipelines" (Khattab et al., 2023)
- **MIPROv2**: "Multi-Prompt Instruction Optimization" (Opsahl-Ong et al., 2024)
- **GEPA**: "Genetic Prompt Evolution" (2024)

### Internal

- **Full Analysis**: `./dspy_framework_analysis_2026-01-15.md` (20+ pages)
- **Experiment Plan**: `../architecture_decisions/experiments/E-008-dspy-prototype-evaluation.md`
- **Hypothesis**: H-008 in `../architecture_decisions/HYPOTHESIS_LOG.md`
- **Related ADRs**: ADR-0010 (Structured Outputs), ADR-0003 (Model Stack)

---

## Bottom Line

**DSPy is a powerful, research-backed framework that aligns philosophically with our type-safe, composable, evidence-based architecture.**

**Recommendation**: **Run focused prototype (E-008) to validate fit, then adopt selectively (Option B) for high-value use cases like Captain's Log reflection and future cognitive architecture.**

**Learning value is high regardless of adoption** - studying DSPy teaches principled LLM system design patterns applicable beyond the framework itself.

**Risk is low**: Prototype is time-boxed (1-2 days), selective adoption is reversible, and we're in pre-implementation phase where architecture is still flexible.

---

**Next Step**: Review analysis, approve E-008 experiment, execute prototype on Days 26-27.
