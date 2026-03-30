# Evaluation Results Report

**Generated:** 2026-03-30T15:01:32.448634+00:00

## Summary

| Metric | Value |
|--------|-------|
| Paths Passed | 8/8 |
| Assertions Passed | 24/24 |
| Assertion Pass Rate | 100.0% |
| Avg Response Time | 10580 ms |

## Results by Category

| Category | Passed | Failed | Pass Rate |
|----------|--------|--------|-----------|
| Context Management | 8 | 0 | 100% |

## Path Details

### ✅ CP-19: Long Conversation Trimming

**Category:** Context Management | **Session:** `32bd43f6-e65c-4ec7-9ff2-c3763659ecb1`
**Assertions:** 3/3 passed

**Turn 1** (14475 ms)
- **Sent:** Let's talk about our system architecture. We use a microservices pattern with FastAPI services commu...
- **Trace:** `7fbe8b32-9047-4c34-b917-1f7c31b1797f`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (9481 ms)
- **Sent:** Our primary database is PostgreSQL for transactional data.
- **Trace:** `039066ff-0682-4cf0-950d-db00b59c29c7`

**Turn 3** (6384 ms)
- **Sent:** We also use Elasticsearch for logging and Neo4j for our knowledge graph.
- **Trace:** `b9510215-f1f7-4610-8a53-0227fa6fb564`

**Turn 4** (7546 ms)
- **Sent:** The deployment is on Docker Compose locally and Kubernetes in production.
- **Trace:** `75b7e321-2000-4730-969c-4b29198cdcfa`

**Turn 5** (15184 ms)
- **Sent:** We've been having issues with service discovery between containers.
- **Trace:** `f0563254-78b9-42a4-9730-330bb151df26`

**Turn 6** (16654 ms)
- **Sent:** I tried using Consul but it added too much operational overhead.
- **Trace:** `fded8a0c-a2db-49a4-b60a-91152e4a6fe8`

**Turn 7** (14529 ms)
- **Sent:** We're now evaluating DNS-based service discovery versus Envoy sidecar proxies.
- **Trace:** `e4025b83-75d1-40e0-98a4-3c43246dc33f`

**Turn 8** (13652 ms)
- **Sent:** The team is leaning toward Envoy because it also handles load balancing.
- **Trace:** `1d70c10a-8e80-40a8-a9b2-cbf81f72192c`

**Turn 9** (13013 ms)
- **Sent:** But I'm worried about the memory overhead of running Envoy sidecars on every service.
- **Trace:** `504d150a-a93b-456b-84f0-a04e64b38f7f`

**Turn 10** (6143 ms)
- **Sent:** Going back to the beginning — what was our primary database again?
- **Trace:** `3e6d6c45-4f68-4133-baa8-880b94d0e300`
  - ✅ Event 'recall_cue_detected': found (expected: present)
  - ✅ Event 'context_budget_applied': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Turn 10: correctly identifies PostgreSQL as primary database
- [ ] If trimmed, important foundational facts were retained
- [ ] Conversation feels coherent throughout
- [ ] Agent doesn't forget mid-conversation
- [ ] context_budget_applied event fires on Turn 10 with correct trimmed/overflow_action fields

---

### ✅ CP-19-v2: Implicit Recall — 'again' cue

**Category:** Context Management | **Session:** `e834f8d4-e709-47af-bea3-d4d98d775f38`
**Assertions:** 3/3 passed

**Turn 1** (9848 ms)
- **Sent:** We need to pick a primary database for the project. Let's go with PostgreSQL.
- **Trace:** `e5acc50e-a5ae-40da-9a65-50d89fd974dc`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (7226 ms)
- **Sent:** Now let's discuss the API framework. We should use FastAPI.
- **Trace:** `c25af377-e106-452b-8e63-6daf3e8c5d35`

**Turn 3** (2497 ms)
- **Sent:** What was our primary database again?
- **Trace:** `5e56bd84-9bc2-48e6-a5fd-df390264ba55`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ Event 'recall_cue_detected': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Correctly identifies PostgreSQL as primary database
- [ ] Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL
- [ ] Agent does not claim ignorance or ask user to repeat

---

### ✅ CP-19-v3: Implicit Recall — 'earlier' cue

**Category:** Context Management | **Session:** `206c0efc-f253-4225-bf10-df4f5ca0d7de`
**Assertions:** 3/3 passed

**Turn 1** (6914 ms)
- **Sent:** We decided to use Redis for our caching layer.
- **Trace:** `57ebba79-5dbc-4b2e-b386-a4bd2a0b420b`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (12162 ms)
- **Sent:** Let's move on to discussing monitoring.
- **Trace:** `546b1870-ede7-46fb-b2ad-7e8515df01bd`

**Turn 3** (4272 ms)
- **Sent:** Going back to earlier — what caching system did we pick?
- **Trace:** `7404d924-b937-4882-b458-7f029edf3a80`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ Event 'recall_cue_detected': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Correctly identifies Redis as caching system
- [ ] Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL

---

### ✅ CP-19-v4: Implicit Recall — 'remind me' cue

**Category:** Context Management | **Session:** `5e93991a-eae3-4c87-91a9-d11c33666196`
**Assertions:** 2/2 passed

**Turn 1** (8618 ms)
- **Sent:** For the message queue, let's use RabbitMQ.
- **Trace:** `b0755a2d-a8c7-47a2-ac9f-bf0f3e99ac9e`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (14367 ms)
- **Sent:** Actually, let's also consider the deployment strategy.
- **Trace:** `13ffb768-a21b-4de4-944a-e0e607c2d174`

**Turn 3** (8795 ms)
- **Sent:** Remind me what we decided on the message queue?
- **Trace:** `d73a8df9-2743-4db9-aa77-c498ceb10fc3`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall

**Quality Criteria (Human Eval):**
- [ ] Correctly identifies RabbitMQ as message queue
- [ ] Intent classifier or recall controller classifies as MEMORY_RECALL

---

### ✅ CP-19-v5: Implicit Recall — 'what did we decide' cue

**Category:** Context Management | **Session:** `0e307483-f3ee-4fae-b6ee-50ea6871a4c7`
**Assertions:** 2/2 passed

**Turn 1** (4679 ms)
- **Sent:** For the CI/CD pipeline, we should go with GitHub Actions.
- **Trace:** `4b7d1e68-7a0c-46f4-a447-d24ff5bbf668`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (6262 ms)
- **Sent:** Let me also think about the testing strategy.
- **Trace:** `d7432999-2797-4f45-a603-8e905e002ccd`

**Turn 3** (4424 ms)
- **Sent:** What did we decide on the CI/CD pipeline?
- **Trace:** `d50472cb-9857-4ed1-933b-1242aaffa99d`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall

**Quality Criteria (Human Eval):**
- [ ] Correctly identifies GitHub Actions as CI/CD choice
- [ ] Intent classifier or recall controller classifies as MEMORY_RECALL

---

### ✅ CP-19-v6: Implicit Recall — 'refresh my memory' cue

**Category:** Context Management | **Session:** `335eab71-240f-4397-afad-d3a8f5496688`
**Assertions:** 3/3 passed

**Turn 1** (5176 ms)
- **Sent:** Our main programming language will be Python 3.12.
- **Trace:** `850fbad6-2927-43a2-b058-1c4e4d05293d`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (8687 ms)
- **Sent:** We also need a frontend framework. Let's use React.
- **Trace:** `e9ff24e6-7034-40e0-8864-dda182955639`

**Turn 3** (9924 ms)
- **Sent:** Refresh my memory — what was our main programming language?
- **Trace:** `ccda101d-1a62-48c8-9f45-2eb586086832`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ Event 'recall_cue_detected': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Correctly identifies Python 3.12 as main language
- [ ] Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL

---

### ✅ CP-19-v7: Implicit Recall — 'the X we discussed' cue

**Category:** Context Management | **Session:** `5a091601-2775-4a6f-80e4-be45f9f20ac8`
**Assertions:** 3/3 passed

**Turn 1** (6151 ms)
- **Sent:** We should use Terraform for infrastructure as code.
- **Trace:** `96a9c2d6-914e-4eb0-a479-6fa0a3b2082c`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (16145 ms)
- **Sent:** Let's also set up monitoring with Grafana.
- **Trace:** `9781758a-47d7-42eb-adb4-456636d365e6`

**Turn 3** (20732 ms)
- **Sent:** The tool we discussed earlier — can you confirm what it was?
- **Trace:** `673d555b-41a5-4106-bd4d-832d2e457614`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ Event 'recall_cue_detected': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Correctly identifies Terraform as infrastructure tool
- [ ] Recall controller reclassifies from CONVERSATIONAL to MEMORY_RECALL

---

### ✅ CP-20: Progressive Token Budget Management

**Category:** Context Management | **Session:** `ce9b62ee-53e0-4595-9711-991185e85837`
**Assertions:** 5/5 passed

**Turn 1** (13596 ms)
- **Sent:** Run the system health check.
- **Trace:** `8fbdcf37-07f7-4e5d-a182-ce79ea180cf2`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 2** (19398 ms)
- **Sent:** Now show me the recent error details.
- **Trace:** `6a00de63-3f48-4114-bb0c-5a5be4ae3226`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 3** (20456 ms)
- **Sent:** Also check the system metrics.
- **Trace:** `a70997ec-61d3-4bde-8450-813344d9ea59`
  - ✅ Event 'tool_call_completed': found (expected: present)

**Turn 4** (11170 ms)
- **Sent:** Summarize everything you've found — is the system healthy overall?
- **Trace:** `c642bd0b-ba69-4e7c-b005-ae19ece1190c`
  - ✅ Event 'context_budget_applied': found (expected: present)

**Quality Criteria (Human Eval):**
- [ ] Each tool call returns valid data
- [ ] Turn 4 synthesizes findings coherently
- [ ] If trimmed, most recent tool results preserved
- [ ] Agent identifies any genuine issues
- [ ] context_budget_applied event fires on Turn 4 with correct trimmed/overflow_action fields

---
