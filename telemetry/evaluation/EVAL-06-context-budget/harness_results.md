# Evaluation Results Report

**Generated:** 2026-03-28T20:34:03.807452+00:00

## Summary

| Metric | Value |
|--------|-------|
| Paths Passed | 1/3 |
| Assertions Passed | 10/12 |
| Assertion Pass Rate | 83.3% |
| Avg Response Time | 16881 ms |

## Results by Category

| Category | Passed | Failed | Pass Rate |
|----------|--------|--------|-----------|
| Context Management | 1 | 1 | 50% |
| Memory Quality | 0 | 1 | 0% |

## Path Details

### ❌ CP-19: Long Conversation Trimming

**Category:** Context Management | **Session:** `cbe4a611-9cea-4ebe-8bae-f0cb882047e3`
**Assertions:** 2/3 passed

**Turn 1** (8807 ms)
- **Sent:** Let's talk about our system architecture. We use a microservices pattern with FastAPI services commu...
- **Trace:** `fa61f31e-9626-40ce-b251-83f05234215c`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (12912 ms)
- **Sent:** Our primary database is PostgreSQL for transactional data.
- **Trace:** `7d46b492-732a-44f7-9118-27a6e8745f97`

**Turn 3** (17687 ms)
- **Sent:** We also use Elasticsearch for logging and Neo4j for our knowledge graph.
- **Trace:** `a36c3411-2e00-4171-82f4-73ea1cb666dd`

**Turn 4** (15005 ms)
- **Sent:** The deployment is on Docker Compose locally and Kubernetes in production.
- **Trace:** `9f25979c-df10-4953-9d7a-1149b4f97e4d`

**Turn 5** (15505 ms)
- **Sent:** We've been having issues with service discovery between containers.
- **Trace:** `46173421-f7ad-49f6-9edf-472eb6de195b`

**Turn 6** (16643 ms)
- **Sent:** I tried using Consul but it added too much operational overhead.
- **Trace:** `4812e7fd-0813-4d24-8de9-5d9e0d754bd3`

**Turn 7** (23610 ms)
- **Sent:** We're now evaluating DNS-based service discovery versus Envoy sidecar proxies.
- **Trace:** `21787afa-847e-4aa0-b18c-5a9d0f5a7a44`

**Turn 8** (23734 ms)
- **Sent:** The team is leaning toward Envoy because it also handles load balancing.
- **Trace:** `7143d1a9-5812-4a55-b17a-db9f8b5f1321`

**Turn 9** (20816 ms)
- **Sent:** But I'm worried about the memory overhead of running Envoy sidecars on every service.
- **Trace:** `844ec764-988a-407c-8a56-f81d7f8e3700`

**Turn 10** (35755 ms)
- **Sent:** Going back to the beginning — what was our primary database again?
- **Trace:** `bfa379a0-abf1-4d35-9343-389f0eb6e818`
  - ❌ intent_classified.task_type: expected=memory_recall, actual=conversational
  - ✅ Event 'context_budget_applied': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Turn 10: correctly identifies PostgreSQL as primary database
- [ ] If trimmed, important foundational facts were retained
- [ ] Conversation feels coherent throughout
- [ ] Agent doesn't forget mid-conversation
- [ ] context_budget_applied event fires on Turn 10 with correct trimmed/overflow_action fields

---

### ✅ CP-20: Progressive Token Budget Management

**Category:** Context Management | **Session:** `8f573f9e-140c-4ec9-9e65-cf2197a593bf`
**Assertions:** 5/5 passed

**Turn 1** (13135 ms)
- **Sent:** Run the system health check.
- **Trace:** `637e3bee-e43d-4191-adee-e3b7d6c37a4c`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 2** (19237 ms)
- **Sent:** Now show me the recent error details.
- **Trace:** `0fbcf476-4f2f-4097-ae52-dc441dcc04cc`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 3** (24900 ms)
- **Sent:** Also check the system metrics.
- **Trace:** `a2b52fbf-130c-4239-bf00-5bd7c02ae016`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 4** (14745 ms)
- **Sent:** Summarize everything you've found — is the system healthy overall?
- **Trace:** `15488589-998c-4711-82e2-1b65a30468c8`
  - ✅ Event 'context_budget_applied': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Each tool call returns valid data
- [ ] Turn 4 synthesizes findings coherently
- [ ] If trimmed, most recent tool results preserved
- [ ] Agent identifies any genuine issues
- [ ] context_budget_applied event fires on Turn 4 with correct trimmed/overflow_action fields

---

### ❌ CP-28: Context Budget Trimming Audit

**Category:** Memory Quality | **Session:** `e2fb216e-83a1-45c8-a33f-eb35303a8c98`
**Assertions:** 3/4 passed

**Turn 1** (6117 ms)
- **Sent:** Our production system uses PostgreSQL 16 as the primary database with pgvector for embeddings.
- **Trace:** `5c8c92b3-2717-4836-9b28-cdddbcab736b`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (8596 ms)
- **Sent:** We chose PostgreSQL specifically because we needed ACID guarantees for our financial transaction pro...
- **Trace:** `716a1460-784a-4f5b-b7ce-441440f02726`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (10576 ms)
- **Sent:** The API layer is FastAPI with Pydantic v2 for validation.
- **Trace:** `b665ed4e-fd67-4184-a4f2-c65320b4fce6`

**Turn 4** (13005 ms)
- **Sent:** We use Redis for session caching and rate limiting.
- **Trace:** `d8d13aa5-ffd7-4fc6-9e80-3babce01095e`

**Turn 5** (14505 ms)
- **Sent:** Our observability stack is Prometheus plus Grafana with OpenTelemetry instrumentation.
- **Trace:** `407d1416-a987-4ef3-9ba5-0f42a1b73d0f`

**Turn 6** (14416 ms)
- **Sent:** We deploy using ArgoCD with Kustomize overlays across three environments: dev, staging, production.
- **Trace:** `739a09c7-c13e-4832-a42b-fcdab02726c1`

**Turn 7** (16257 ms)
- **Sent:** The CI pipeline uses GitHub Actions with matrix builds for Python 3.11 and 3.12.
- **Trace:** `f5a4a333-5948-4316-93b8-b2db2446a8cb`

**Turn 8** (14399 ms)
- **Sent:** We also have a Celery worker fleet for async job processing backed by RabbitMQ.
- **Trace:** `49e2a84e-1e2d-4126-bd2a-b2b751986fff`

**Turn 9** (29599 ms)
- **Sent:** Run a full system health check, then tell me about any issues, and also check the recent error log.
- **Trace:** `7580e591-9cef-4caa-ba1d-a6f05643d8c6`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 10** (15183 ms)
- **Sent:** Given everything we've discussed about our stack, what is our primary database and why did we choose...
- **Trace:** `e3426279-1a61-440e-bff5-a49802e24332`
  - ❌ intent_classified.task_type: expected=memory_recall, actual=conversational

**Quality Criteria (Human Eval):**
- [ ] Turn 10 correctly identifies PostgreSQL 16 as primary database
- [ ] Turn 10 mentions ACID guarantees or financial transaction context
- [ ] If context was trimmed, foundational facts (PostgreSQL, financial) survived
- [ ] gateway_output.budget_trimmed field accurately reflects trimming decision
- [ ] If overflow_action is 'dropped_oldest_history', recent tool output is preserved
- [ ] If overflow_action is 'dropped_memory_context', session history is preserved

---
