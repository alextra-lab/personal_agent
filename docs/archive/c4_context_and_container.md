# C4 Context & Container Diagrams — Personal Local Agent

*A structural view of the system with biological anchors.*

This document presents a C4-style view of the Personal Local AI Collaborator, aligned with the biological model:

- Context level: who/what the system interacts with
- Container level: main runtime components and data stores

Use this alongside:

- `./system_architecture_v0.1.md`
- `./HUMAN_SYSTEMS_MAPPING.md`
- `./HOMEOSTASIS_MODEL.md`
- `./BRAINSTEM_SERVICE_v0.1.md`

---

## 1. C4 Level 1 — System Context Diagram

The goal of this diagram is to show **the agent as a system** in its environment: you, your Mac, local model servers, and the external web.

```mermaid
C4Context

Person(user, "Human User")

System_Boundary(local_env, "Local Environment") {
    System(personal_agent, "AI Collaborator", "Local agent system", "Local models, coding, research, monitoring")
    System(mac_os, "macOS", "Host OS", "System resources, logs, processes, metrics")
    System(local_llm_stack, "Local LLM Stack", "Model serving", "Coding, reasoning, routing, tools")
}

System_Ext(web, "The Web", "External info", "Web-augmented reasoning")

System_Ext(version_control, "Git / VCS", "Code storage", "Agent code, configs, evolution")

Rel(user, personal_agent, "Chats, requests tasks")
Rel(personal_agent, mac_os, "Reads metrics, logs, tools")
Rel(personal_agent, local_llm_stack, "Prompts, completions")
Rel(personal_agent, web, "Research queries")
Rel(personal_agent, version_control, "Records evolution")

UpdateLayoutConfig($c4ShapeInRow="2", $c4BoundaryInRow="2")
```

Biological anchors:

- The **Personal Agent** is the "organism".
- macOS + local LLM stack are the **body & brain tissue** it runs on.
- The Web is the **environment**.
- Git is a key part of the **reproductive/evolutionary system** (remembered history and evolution).

---

## 2. C4 Level 2 — Container Diagram

This view shows the internal structure of the Personal Local AI Collaborator as a single coherent system.

```mermaid
C4Container
UpdateLayoutConfig($c4ShapeInRow="2", $c4BoundaryInRow="2")

System(personal_agent, "Local AI Collaborator", "Agent system", "Bio-inspired orchestration + safety runtime")

Container_Boundary(core, "Core Services") {
    Container(ui, "User Interface", "CLI/Chat", "Conversation, status, metrics")
    Container(orchestrator, "Orchestrator Core", "Service", "Planning, dialogue, tool routing")
    Container(brainstem, "Brainstem Service", "Background", "Homeostasis, modes, safety")
}

Container_Boundary(data, "Data Layer") {
    Container(kb, "Knowledge Base", "Storage", "RAG, documents, world model")
    Container(telemetry, "Telemetry Store", "DB/Files", "Metrics, traces, logs")
    Container(config_policies, "Config & Policy", "Config/DB", "Modes, thresholds, constraints")
    Container(captains_log, "Captain's Log", "Git files", "Reflection, hypotheses, history")
}

Container_Boundary(integration, "Integration Layer") {
    Container(tools_layer, "Tools & Actions", "Library", "Safe command execution")
    Container(local_llm_client, "LLM Client", "Library", "Local model connection")
}

Container_Ext(local_llm_stack, "Local LLM Stack", "External", "Model serving")
Container_Ext(mac_os, "macOS", "External", "Host system")
Container_Ext(web, "The Web", "External", "HTTPs knowledge")

Rel(ui, orchestrator, "Prompts")
Rel(orchestrator, ui, "Responses")

Rel(orchestrator, local_llm_client, "Prompts")
Rel(local_llm_client, orchestrator, "Results")
Rel(local_llm_client, local_llm_stack, "API")

Rel(orchestrator, tools_layer, "Tool calls")
Rel(tools_layer, mac_os, "Exec")

Rel(orchestrator, kb, "Query/Update")
Rel(brainstem, config_policies, "Read")
Rel(orchestrator, config_policies, "Read")

Rel(brainstem, telemetry, "Events")
Rel(orchestrator, telemetry, "Metrics")
Rel(tools_layer, telemetry, "Metrics")
Rel(kb, telemetry, "Health")

Rel(orchestrator, captains_log, "Hypotheses")
Rel_Right(telemetry, captains_log, "Eval")

Rel(orchestrator, web, "Queries")

Rel(brainstem, orchestrator, "Control")
```
