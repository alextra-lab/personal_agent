# Evaluation Results Report

**Generated:** 2026-03-30T15:31:38.926087+00:00

## Summary

| Metric | Value |
|--------|-------|
| Paths Passed | 4/4 |
| Assertions Passed | 28/28 |
| Assertion Pass Rate | 100.0% |
| Avg Response Time | 11044 ms |

## Results by Category

| Category | Passed | Failed | Pass Rate |
|----------|--------|--------|-----------|
| Memory Quality | 4 | 0 | 100% |

## Path Details

### ✅ CP-26: Memory Promotion Quality

**Category:** Memory Quality | **Session:** `6bc81cb3-8ee7-49ea-993f-706871d2936b`
**Assertions:** 12/12 passed

**Turn 1** (5606 ms)
- **Sent:** I'm building a service called DataForge. It uses Apache Flink for stream processing and stores resul...
- **Trace:** `8647f76b-c330-4c8b-8b54-5b6c6ff906f5`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational
  - ✅ decomposition_assessed.strategy: expected=single, actual=single

**Turn 2** (11712 ms)
- **Sent:** The project lead is Priya Sharma. We're targeting a throughput of 50,000 events per second on GCP.
- **Trace:** `bfc78028-6c5d-404c-ae18-eb355eeda5e8`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (7822 ms)
- **Sent:** DataForge also integrates with Grafana for real-time monitoring and uses Kafka as the ingestion laye...
- **Trace:** `888d9ee4-97b9-47d0-b3c3-e9bca5224ec5`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 4** (8032 ms)
- **Sent:** What do you remember about the DataForge project?
- **Trace:** `9750e78d-ce60-48ef-a225-fc6a80e8bcb5`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ decomposition_assessed.strategy: expected=single, actual=single
  - ✅ Event 'memory_recall_broad_query': found (expected: present)

**Post-Path Assertions (Neo4j):**
  - ✅ Neo4j: Entity 'DataForge' exists in Neo4j — 1 rows (need >= 1)
  - ✅ Neo4j: Entity 'Apache Flink' exists in Neo4j — 1 rows (need >= 1)
  - ✅ Neo4j: Entity 'ClickHouse' exists in Neo4j — 1 rows (need >= 1)
  - ✅ Neo4j: Entity 'Priya Sharma' exists in Neo4j — 1 rows (need >= 1)
  - ✅ Neo4j: Entity 'DataForge' promoted to semantic memory — 1 rows (need >= 1)

**Quality Criteria (Human Eval):**
- [ ] Turn 4 references DataForge by name
- [ ] Mentions at least 5 of: Flink, ClickHouse, Priya Sharma, GCP, Grafana, Kafka
- [ ] Information is accurate (no hallucinated technologies or people)
- [ ] Demonstrates entity-relationship awareness (Kafka -> Flink -> ClickHouse pipeline)
- [ ] Does not confuse entities from other conversations

---

### ✅ CP-27: Memory-Informed Context Assembly

**Category:** Memory Quality | **Session:** `91edf7d8-938d-4e08-a88b-a28f008fbad3`
**Assertions:** 5/5 passed

**Turn 1** (6402 ms)
- **Sent:** I'm working on a machine learning pipeline called SentinelML that uses PyTorch for model training an...
- **Trace:** `4bf9c9fb-e8b7-4e1b-a1d1-9734b57c22d9`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (8472 ms)
- **Sent:** SentinelML runs on Kubernetes with GPU node pools. The inference endpoint uses TorchServe behind an ...
- **Trace:** `c0115fd8-8103-4872-8735-97cb83892c60`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (26077 ms)
- **Sent:** What infrastructure changes would you recommend for scaling SentinelML to handle 10x the current inf...
- **Trace:** `e7622df0-fec4-4cca-b223-396a7d9da12a`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.strategy: expected=single, actual=single
  - ✅ Event 'memory_enrichment_completed': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Turn 3 response explicitly references SentinelML by name
- [ ] Recommends scaling TorchServe specifically (not generic model serving)
- [ ] Addresses Kubernetes GPU node pool scaling
- [ ] Mentions Istio service mesh considerations for load balancing
- [ ] Advice is stack-specific, not generic cloud scaling advice
- [ ] Response demonstrates memory-informed reasoning, not generic knowledge

---

### ✅ CP-28: Context Budget Trimming Audit

**Category:** Memory Quality | **Session:** `bd084d1a-26eb-49c7-8b92-afb5b67e5a0b`
**Assertions:** 4/4 passed

**Turn 1** (5033 ms)
- **Sent:** Our production system uses PostgreSQL 16 as the primary database with pgvector for embeddings.
- **Trace:** `c19ec920-9277-41eb-b83f-bc5e2ac46582`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (10470 ms)
- **Sent:** We chose PostgreSQL specifically because we needed ACID guarantees for our financial transaction pro...
- **Trace:** `4e4de784-cbf3-4136-9372-195324e8d8e1`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (5777 ms)
- **Sent:** The API layer is FastAPI with Pydantic v2 for validation.
- **Trace:** `6183c6c4-4b52-4381-9810-e14bdc3f502c`

**Turn 4** (6617 ms)
- **Sent:** We use Redis for session caching and rate limiting.
- **Trace:** `559e1a3c-1a52-4115-a707-cfeed8ddf71f`

**Turn 5** (13762 ms)
- **Sent:** Our observability stack is Prometheus plus Grafana with OpenTelemetry instrumentation.
- **Trace:** `688ce4f2-bcd2-4054-aeac-df14374095bc`

**Turn 6** (8742 ms)
- **Sent:** We deploy using ArgoCD with Kustomize overlays across three environments: dev, staging, production.
- **Trace:** `e24124dc-d625-4667-ba1c-2f96d2013a62`

**Turn 7** (8976 ms)
- **Sent:** The CI pipeline uses GitHub Actions with matrix builds for Python 3.11 and 3.12.
- **Trace:** `73956eca-b55d-4415-bd84-87974821d65d`

**Turn 8** (13990 ms)
- **Sent:** We also have a Celery worker fleet for async job processing backed by RabbitMQ.
- **Trace:** `005d6557-3b9d-44f2-8339-e9ca5b34324b`

**Turn 9** (30850 ms)
- **Sent:** Run a full system health check, then tell me about any issues, and also check the recent error log.
- **Trace:** `09c3b99d-c4a2-4ba4-bd90-c5b9140f1c2c`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 10** (13196 ms)
- **Sent:** Given everything we've discussed about our stack, what is our primary database and why did we choose...
- **Trace:** `91a95939-6156-4771-a48e-8085858cefea`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall

**Quality Criteria (Human Eval):**
- [ ] Turn 10 correctly identifies PostgreSQL 16 as primary database
- [ ] Turn 10 mentions ACID guarantees or financial transaction context
- [ ] If context was trimmed, foundational facts (PostgreSQL, financial) survived
- [ ] gateway_output.budget_trimmed field accurately reflects trimming decision
- [ ] If overflow_action is 'dropped_oldest_history', recent tool output is preserved
- [ ] If overflow_action is 'dropped_memory_context', session history is preserved

---

### ✅ CP-29: Delegation Package Completeness

**Category:** Memory Quality | **Session:** `0f4da2b5-255c-4b70-9ec9-9e1199c03d5c`
**Assertions:** 7/7 passed

**Turn 1** (6520 ms)
- **Sent:** Our API uses FastAPI with SQLAlchemy 2.0 async sessions and Alembic for migrations. The models are i...
- **Trace:** `61a08333-ed50-46d5-9d5e-3002bd5f98af`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (12513 ms)
- **Sent:** We had a bug last week where a migration dropped a column that was still referenced by an API endpoi...
- **Trace:** `3a01dec2-e11b-40f7-b8c1-03d44d4254b1`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (10313 ms)
- **Sent:** Use Claude Code to add a new REST endpoint for bulk user imports with CSV upload support, input vali...
- **Trace:** `8a61cf64-a0ef-4f2d-b7e2-e7cc77692f22`
  - ✅ intent_classified.task_type: expected=delegation, actual=delegation
  - ✅ decomposition_assessed.strategy: expected=delegate, actual=delegate
  - ✅ Event 'delegation_package_created': found (expected: present)
  - ✅ delegation_package_created.criteria_count: 3.0 >= 1 = PASS
  - ✅ delegation_package_created.context_items: 0.0 >= 0 = PASS

**Quality Criteria (Human Eval):**
- [ ] Delegation package references FastAPI + SQLAlchemy from Turn 1
- [ ] Package includes the migration bug from Turn 2 as a known pitfall
- [ ] Acceptance criteria cover CSV parsing, validation, and error reporting
- [ ] Package includes relevant file paths (src/models/, src/routes/)
- [ ] Task description is self-contained for an agent with no prior context
- [ ] Package complexity estimate is reasonable (MODERATE or COMPLEX)

---
