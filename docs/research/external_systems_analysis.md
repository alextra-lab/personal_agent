# External Systems Analysis

*Learning from production agentic systems and research projects*

**Purpose**: Document insights from other projects to inform our architectural decisions and avoid reinventing wheels.

**Status**: Living document - update regularly as we discover new systems and patterns.

---

## Production Systems

### Factory.ai - Agent-Native Development Platform

**Website**: <https://factory.ai/>

**What they built**:

- Multi-agent system called "Droids" embedded across entire development workflow
- Agents work in IDE, CLI, Slack, Linear, CI/CD without changing tools
- Enterprise-scale with security, compliance, vendor agnosticism

**Key achievements**:

- #1 on Terminal-Bench with 58.75% score
- Proof that "agent design, not just choice of model, is decisive factor"
- Works with any model provider, any dev tooling, any interface

**Published research**:

- Context compression evaluation framework for long-running agent sessions
- Security case study: Detecting and disrupting AI-orchestrated cyber fraud campaigns
- Integration with Palo Alto Networks Prisma AIRS for AI security

**What we can learn**:

- Multi-agent coordination patterns that work at production scale
- Context management strategies for long-running sessions
- Security considerations for agentic systems (prompt injection, fraud detection)
- Interface design: meet users where they work, don't force workflow changes

**Applicable to our project**:

- Their multi-agent approach provides empirical validation
- Context compression research informs our memory consolidation design
- Security patterns relevant to our safety/governance layer
- "Agent design matters" validates our focus on cognitive architecture

**Questions to explore**:

- How do they coordinate multiple agents? Message-passing? Shared state?
- What's their orchestration strategy? Centralized vs distributed?
- How do they handle agent failures and error recovery?

---

## Research Projects

### Microsoft Research - Modular Agentic Planner

**Key insight**: Brain-inspired role decomposition (Actor, Monitor, Predictor, Evaluator, Orchestrator)

**Status**: Referenced in our cognitive_architecture_principles.md

**Applicable patterns**:

- Role-based specialization with clear boundaries
- Separate monitoring from execution
- Evaluation as distinct cognitive function

---

## Framework Comparisons

### LangGraph vs AutoGen vs Custom Architecture

**To be evaluated**: Once we start implementation, document comparisons here.

**Evaluation criteria**:

- Alignment with our cognitive architecture principles
- Observability and debuggability
- Integration with our governance model
- Learning curve and maintainability

---

## Integration Strategy

**Weekly review cycle**:

1. Scan for new agentic system papers, blog posts, releases
2. Extract applicable patterns
3. Note what worked/didn't work for them
4. Propose hypotheses for our architecture (document in HYPOTHESIS_LOG.md)
5. Design experiments to validate (document in experiments/)

**Sources to monitor**:

- arXiv: cs.AI, cs.MA (multi-agent systems)
- AI company research blogs: Anthropic, OpenAI, Microsoft Research
- Production systems: Factory.ai, GitHub Copilot, Cursor updates
- Academic conferences: NeurIPS, ICML, AAAI (agentic AI tracks)

---

## Document Status

**Created**: 2025-12-29
**Last updated**: 2025-12-29
**Next review**: Weekly (suggested: Sunday evenings)
