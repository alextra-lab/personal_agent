# Evaluation Results Report

**Generated:** 2026-03-28T18:06:18.198683+00:00

## Summary

| Metric | Value |
|--------|-------|
| Paths Passed | 4/4 |
| Assertions Passed | 17/17 |
| Assertion Pass Rate | 100.0% |
| Avg Response Time | 34567 ms |

## Results by Category

| Category | Passed | Failed | Pass Rate |
|----------|--------|--------|-----------|
| Memory System | 4 | 0 | 100% |

## Path Details

### ✅ CP-12: Entity Seeding and Targeted Recall

**Category:** Memory System | **Session:** `5602f371-8762-4427-8469-56751c37a32e`
**Assertions:** 6/6 passed

**Turn 1** (4696 ms)
- **Sent:** I've been working on a project called Project Atlas. It's a data pipeline that processes satellite i...
- **Trace:** `68fde068-c054-4454-9a60-9e5e4bcdb275`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational
  - ✅ decomposition_assessed.strategy: expected=single, actual=single

**Turn 2** (6340 ms)
- **Sent:** The team lead is Maria Chen and we're deploying to AWS with a target of processing 500 images per ho...
- **Trace:** `e78bdb36-19f9-4c60-bcc4-1fb3f74f0f6d`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (183167 ms)
- **Sent:** What do you know about Project Atlas?
- **Trace:** `a32283d8-76e9-42a9-a06d-e679def68b9c`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ intent_classified.confidence: expected=0.9, actual=0.9
  - ✅ decomposition_assessed.strategy: expected=single, actual=single

**Quality Criteria (Human Eval):**
- [ ] Turn 3 references Project Atlas by name
- [ ] Mentions at least 3 of: pipeline, imagery, Kafka, Spark, Maria Chen, AWS
- [ ] Information is accurate (not hallucinated)
- [ ] Demonstrates synthesis, not just parroting

---

### ✅ CP-13: Broad Recall

**Category:** Memory System | **Session:** `4bd641e6-2b15-4786-9a26-3a3c2e8bb408`
**Assertions:** 4/4 passed

**Turn 1** (64691 ms)
- **Sent:** I've been evaluating Django and FastAPI for our new web service. FastAPI seems faster but Django has...
- **Trace:** `a6d6060c-4ae4-44c0-8eb9-037421bb3f9c`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (51106 ms)
- **Sent:** We also need to decide between PostgreSQL and MongoDB for the storage layer. Our data is mostly rela...
- **Trace:** `883a6191-9fba-45ab-9692-39fbf3932f10`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (9914 ms)
- **Sent:** What topics have we covered in this conversation?
- **Trace:** `fa2823f4-5cf3-4de0-ac2f-7838e1c6f114`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ decomposition_assessed.strategy: expected=single, actual=single

**Quality Criteria (Human Eval):**
- [ ] Identifies at least 2 distinct topics (web frameworks, databases)
- [ ] Mentions specific technologies (Django, FastAPI, PostgreSQL, MongoDB)
- [ ] Response is organized — groups topics
- [ ] Captures key considerations (speed vs batteries, relational vs document)
- [ ] Does not hallucinate topics not discussed

---

### ✅ CP-14: Multi-Entity Tracking

**Category:** Memory System | **Session:** `39f4fb9e-5ae8-4dd8-8396-30c656f22f73`
**Assertions:** 4/4 passed

**Turn 1** (6861 ms)
- **Sent:** Alice on our team is building a CI/CD automation tool called BuildBot. She's using Python and GitHub...
- **Trace:** `a10c6b7b-00cb-4b33-8a1f-c8c9952508bf`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (4878 ms)
- **Sent:** Bob is working on a deployment tool called DeployTool. He's focused on Terraform and AWS infrastruct...
- **Trace:** `3a1eb45a-240d-44d4-92c0-e3b6dcdc6f06`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 3** (7440 ms)
- **Sent:** What do you know about Alice and her work?
- **Trace:** `ea081e7d-5f53-41aa-992f-c001d9672735`
  - ✅ intent_classified.task_type: expected=memory_recall, actual=memory_recall
  - ✅ intent_classified.confidence: expected=0.9, actual=0.9

**Quality Criteria (Human Eval):**
- [ ] Correctly associates Alice with BuildBot, Python, GitHub Actions
- [ ] Does NOT mention Bob, DeployTool, Terraform, or AWS
- [ ] Demonstrates entity-relationship awareness
- [ ] Clean separation between the two people

---

### ✅ CP-15: Memory-Informed Response

**Category:** Memory System | **Session:** `4aaa212e-fc10-48e2-b20c-65198f5f0fa1`
**Assertions:** 3/3 passed

**Turn 1** (28436 ms)
- **Sent:** I'm building a real-time dashboard using WebSockets and React to monitor IoT sensor data produced by...
- **Trace:** `2785ae27-f4fc-4e6c-84e7-9a97aff23760`
  - ✅ intent_classified.task_type: expected=conversational, actual=conversational

**Turn 2** (12710 ms)
- **Sent:** What technology stack would you recommend for the backend of this project?
- **Trace:** `75c0548e-07b7-4979-b43a-6fc619b6d467`
  - ✅ intent_classified.task_type: expected=analysis, actual=analysis
  - ✅ decomposition_assessed.strategy: expected=single, actual=single

**Quality Criteria (Human Eval):**
- [ ] Recommendation explicitly references WebSockets from Turn 1
- [ ] Addresses IoT/real-time requirements (not generic web stack)
- [ ] Technologies compatible with stated stack
- [ ] Does not recommend conflicting technologies
- [ ] Feels like a conversation, not two isolated questions

---
