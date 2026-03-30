# Evaluation Results Report

**Generated:** 2026-03-30T15:44:15.828731+00:00

## Summary

| Metric | Value |
|--------|-------|
| Paths Passed | 7/7 |
| Assertions Passed | 52/52 |
| Assertion Pass Rate | 100.0% |
| Avg Response Time | 55272 ms |

## Results by Category

| Category | Passed | Failed | Pass Rate |
|----------|--------|--------|-----------|
| Decomposition Strategies | 4 | 0 | 100% |
| Expansion & Sub-Agents | 3 | 0 | 100% |

## Path Details

### ✅ CP-08: SINGLE Strategy (Simple Question)

**Category:** Decomposition Strategies | **Session:** `c1e43650-94f5-4605-9391-e6ac54d53394`
**Assertions:** 6/6 passed

**Turn 1** (7614 ms)
- **Sent:** What is dependency injection?
- **Trace:** `ccb4e03e-bb62-4e3d-b6be-84a8efc60978`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational
  - ✅ decomposition_assessed.complexity: expected=simple, actual=simple
  - ✅ decomposition_assessed.strategy: expected=single, actual=single
  - ✅ Event 'expansion_dispatch_started': not found (expected: absent)

**Turn 2** (12514 ms)
- **Sent:** Can you give me a quick example in Python?
- **Trace:** `231f62f7-565b-425b-ac3b-17a506985be0`
  - ✅ decomposition_assessed.strategy: expected=single, actual=single
  - ✅ Event 'expansion_dispatch_started': not found (expected: absent)

**Quality Criteria (Human Eval):**
- [ ] Explanation is clear and accurate
- [ ] Appropriate depth for a definitional question
- [ ] Python example in Turn 2 is correct and illustrative
- [ ] Fast response time (no expansion overhead)

---

### ✅ CP-09: HYBRID Strategy (Moderate Analysis)

**Category:** Decomposition Strategies | **Session:** `6f4a6bc6-c1e2-49dc-b9a9-5178110d5996`
**Assertions:** 9/9 passed

**Turn 1** (58331 ms)
- **Sent:** Research the advantages of event sourcing versus CRUD for session storage, and evaluate their suitab...
- **Trace:** `b2514e62-a72d-4c9d-ad1b-2fc75a5dfb46`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.complexity: expected=moderate, actual=moderate
  - ✅ decomposition_assessed.strategy: expected=hybrid, actual=hybrid
  - ✅ Event 'planner_started': found (expected: present)
  - ✅ Event 'expansion_dispatch_started': found (expected: present)
  - ✅ Event 'expansion_controller_complete': found (expected: present)
  - ✅ expansion_controller_complete.sub_agent_count: 4.0 >= 1 = PASS

**Turn 2** (11220 ms)
- **Sent:** Given what you found, which approach would you recommend for our use case?
- **Trace:** `55f40be1-7a86-4bd9-9cc9-f45d3115e693`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.strategy: expected=single, actual=single

**Quality Criteria (Human Eval):**
- [ ] Response covers both event sourcing AND CRUD approaches
- [ ] PostgreSQL-specific considerations addressed
- [ ] Sub-agent contributions synthesized coherently
- [ ] Turn 2 recommendation grounded in Turn 1 analysis
- [ ] Quality noticeably better than a single-pass response

---

### ✅ CP-10: DECOMPOSE Strategy (Complex Multi-Part Analysis)

**Category:** Decomposition Strategies | **Session:** `b8402246-9ec0-4d9e-a7e5-ae492d91a4b4`
**Assertions:** 7/7 passed

**Turn 1** (57727 ms)
- **Sent:** Compare three approaches to distributed caching, evaluate their performance under load, analyze the ...
- **Trace:** `8937e910-441b-4ee4-86e7-4b0cc6c1571e`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.complexity: expected=complex, actual=complex
  - ✅ decomposition_assessed.strategy: expected=decompose, actual=decompose
  - ✅ Event 'planner_started': found (expected: present)
  - ✅ Event 'expansion_dispatch_started': found (expected: present)
  - ✅ Event 'expansion_controller_complete': found (expected: present)
  - ✅ expansion_controller_complete.sub_agent_count: 4.0 >= 2 = PASS

**Quality Criteria (Human Eval):**
- [ ] At least 3 caching approaches compared
- [ ] Performance evaluation includes metrics or benchmarks
- [ ] Cost analysis is concrete, not vague
- [ ] Recommendation is specific with clear reasoning
- [ ] Response well-structured with sections for each part

---

### ✅ CP-11: Complexity Escalation Across Turns

**Category:** Decomposition Strategies | **Session:** `877a9023-2481-45f5-91c6-84314d6c5ba4`
**Assertions:** 12/12 passed

**Turn 1** (6943 ms)
- **Sent:** What is a knowledge graph?
- **Trace:** `2a4c6866-3d66-4ab8-866f-341c6a4f06a5`
  - ✅ decomposition_assessed.complexity: expected=simple, actual=simple
  - ✅ decomposition_assessed.strategy: expected=single, actual=single
  - ✅ Event 'expansion_dispatch_started': not found (expected: absent)

**Turn 2** (129383 ms)
- **Sent:** Compare Neo4j and Dgraph for entity storage, and evaluate their query performance and Python ecosyst...
- **Trace:** `a5cb44f0-ec5d-41f3-9110-e4455bfb0a79`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.complexity: expected=moderate, actual=moderate
  - ✅ decomposition_assessed.strategy: expected=hybrid, actual=hybrid
  - ✅ Event 'planner_started': found (expected: present)
  - ✅ Event 'expansion_dispatch_started': found (expected: present)
  - ✅ expansion_controller_complete.sub_agent_count: 4.0 >= 1 = PASS

**Turn 3** (13906 ms)
- **Sent:** Based on that comparison, which should we use?
- **Trace:** `86a7b3a3-6496-4aaf-bea1-7f9c0895599a`
  - ✅ decomposition_assessed.complexity: expected=simple, actual=simple
  - ✅ decomposition_assessed.strategy: expected=single, actual=single
  - ✅ Event 'expansion_dispatch_started': not found (expected: absent)

**Quality Criteria (Human Eval):**
- [ ] Turn 1 is concise and accurate
- [ ] Turn 2 is noticeably more detailed (HYBRID effect)
- [ ] Turn 2 covers both databases across both dimensions
- [ ] Turn 3 recommendation references Turn 2 analysis
- [ ] No classification bleed-over between turns

---

### ✅ CP-16: HYBRID Synthesis Quality

**Category:** Expansion & Sub-Agents | **Session:** `c8f5cd59-313b-4403-b35c-d70471fa062e`
**Assertions:** 9/9 passed

**Turn 1** (135914 ms)
- **Sent:** Research microservices communication patterns and evaluate the trade-offs between synchronous HTTP, ...
- **Trace:** `4962e558-a021-4b99-9bee-1ac1317b2c99`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.complexity: expected=moderate, actual=moderate
  - ✅ decomposition_assessed.strategy: expected=hybrid, actual=hybrid
  - ✅ Event 'planner_started': found (expected: present)
  - ✅ Event 'expansion_dispatch_started': found (expected: present)
  - ✅ Event 'expansion_controller_complete': found (expected: present)
  - ✅ expansion_controller_complete.sub_agent_count: 4.0 >= 1 = PASS

**Turn 2** (38795 ms)
- **Sent:** Which pattern would you recommend for a system with both low-latency and high-throughput requirement...
- **Trace:** `2d73595a-b711-436f-85e3-af780c2f5b24`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.strategy: expected=single, actual=single

**Quality Criteria (Human Eval):**
- [ ] All three communication patterns covered (HTTP, async, gRPC)
- [ ] Trade-offs are concrete (latency, complexity, tooling)
- [ ] Response feels unified — not three stitched answers
- [ ] Synthesis adds value (comparison table, decision framework)
- [ ] Turn 2 recommendation grounded in Turn 1 analysis

---

### ✅ CP-17: Sub-Agent Concurrency

**Category:** Expansion & Sub-Agents | **Session:** `e8115642-a6a0-44aa-95fa-985fb583a3e2`
**Assertions:** 8/8 passed

**Turn 1** (97045 ms)
- **Sent:** Compare the performance characteristics of Redis, Memcached, and Hazelcast for distributed caching. ...
- **Trace:** `cf1b9338-b84b-459e-8725-e40014bd7518`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.complexity: expected=complex, actual=complex
  - ✅ decomposition_assessed.strategy: expected=decompose, actual=decompose
  - ✅ Event 'planner_started': found (expected: present)
  - ✅ Event 'expansion_dispatch_started': found (expected: present)
  - ✅ Event 'expansion_controller_complete': found (expected: present)
  - ✅ expansion_controller_complete.sub_agent_count: 5.0 >= 2 = PASS
  - ✅ Event 'user_visible_timeout': not found (expected: absent)

**Quality Criteria (Human Eval):**
- [ ] All three caching systems compared
- [ ] Performance includes throughput, latency, memory efficiency
- [ ] Memory management differences explained
- [ ] Operational complexity addressed
- [ ] Final recommendation is specific and justified

---

### ✅ CP-18: Expansion Budget Enforcement

**Category:** Expansion & Sub-Agents | **Session:** `afb4fd09-5394-4715-99a2-ffd1a6b394c7`
**Assertions:** 1/1 passed

**Turn 1** (93867 ms)
- **Sent:** Research the advantages of container orchestration and evaluate Kubernetes versus Docker Swarm for s...
- **Trace:** `8e230eb4-c32d-47ca-88cb-14d33a5e9c1a`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis

**Quality Criteria (Human Eval):**
- [ ] Under load: provides reasonable response (graceful degradation)
- [ ] Under load: response less detailed than HYBRID version
- [ ] Budget enforcement transparent in telemetry
- [ ] Compare quality: SINGLE vs HYBRID version of same question

---
