# ADR-0045: Infrastructure — Cloud Knowledge Layer with Flexible Execution

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Depends on**: ADR-0043 (Three-Layer Separation)
**Related**: ADR-0016 (Service-Based Architecture), ADR-0041 (Event Bus — Redis Streams), ADR-0044 (Provider Abstraction & Dual-Harness)
**Enables**: ADR-0048 (Mobile & Multi-Device UI), ADR-0050 (Remote Agent Harness Integration)

---

## Context

### Everything runs on the laptop today

The current infrastructure is entirely local, running as Docker Compose services on a MacBook Pro M4 Max (128GB):

| Service | Port | Purpose |
|---------|------|---------|
| PostgreSQL (pgvector) | 5432 | Sessions, metrics, conversation history |
| Elasticsearch 8.19 | 9200 | Logs, traces, indexed knowledge |
| Neo4j 5.26 | 7474/7687 | Knowledge graph |
| Redis | 6379 | Event bus (ADR-0041) |
| Kibana 8.19 | 5601 | Log visualization |
| SLM Server | 8000 | Local LLM inference (llama.cpp/MLX) |
| Personal Agent | 9000 | FastAPI service |

Note: there is no reverse proxy in the current local setup. Services are accessed directly on their ports.

This works for development and solo laptop use. It does not work for:

1. **Phone/iPad access**: The agent is unreachable when the laptop is closed, sleeping, or on a different network. There's no persistent endpoint for mobile clients (ADR-0048).

2. **Always-on knowledge**: Conversation history, the knowledge graph, and observation data are trapped on one machine. If the laptop is off, the knowledge is inaccessible — not just to the user, but to any cloud execution profile (ADR-0044) or external agent (ADR-0050).

3. **Cloud execution profiles**: A cloud profile using Claude Sonnet via LiteLLM needs to read context from Neo4j and write results to PostgreSQL. If those databases are on the laptop, the cloud profile only works when the laptop is running. This defeats the purpose of cloud execution.

4. **Background processing**: Brainstem consolidation, knowledge graph freshness updates (ADR-0042), and feedback polling (ADR-0040) require the service to be running. No laptop → no background processing → stale knowledge.

### Three infrastructure options evaluated

| Option | Description | Always-on? | Mobile? | Cost | Complexity |
|--------|-------------|-----------|---------|------|------------|
| **A. Tunnel-based** | Cloudflare Tunnel / ngrok from laptop | No (laptop must run) | Fragile | ~$0/mo | Low |
| **B. Cloud knowledge + flexible execution** | Knowledge Layer on VPS; execution on laptop or cloud | Yes (knowledge) | Yes | ~$20-40/mo | Medium |
| **C. Full cloud hosting** | Everything on cloud VM(s) | Yes | Yes | ~$60-150/mo | High |

Option A is not viable — it solves nothing structurally. The agent works when the laptop is open, which is already true today.

Option C is premature. Full cloud hosting means running LLM inference in the cloud too, which is either expensive (GPU instances) or requires giving up local inference entirely. The hybrid local+cloud model (ADR-0044) specifically requires that local execution remains an option.

---

## Decision

**Option B: Cloud Knowledge Layer with flexible execution.**

Deploy the Knowledge Layer (PostgreSQL, Neo4j, Elasticsearch, Redis) on a cloud VPS. The Execution Layer remains flexible — it can run on the laptop (local inference), in the cloud (LiteLLM to Anthropic/Google/OpenAI), or both simultaneously.

### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Cloud VM(s)                                │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  Reverse Proxy (TLS termination, auth, rate limiting)│    │
│  └──────────────────────┬───────────────────────────────┘    │
│                         │                                     │
│  ┌────────────┐  ┌──────▼──┐  ┌──────────────────────┐      │
│  │ PostgreSQL │  │  Neo4j  │  │  Elasticsearch       │      │
│  │ (pgvector) │  │(knowledge│  │  (logs, traces,     │      │
│  │            │  │  graph)  │  │   indexed knowledge) │      │
│  └────────────┘  └─────────┘  └──────────────────────┘      │
│  ┌────────────┐  ┌──────────────────────────────────┐        │
│  │   Redis    │  │  Seshat API Gateway              │        │
│  │ (event bus)│  │  (FastAPI — Knowledge + Obs API) │        │
│  └────────────┘  └──────────────────────────────────┘        │
└──────────────────────────────────────────────────────────────┘
          │                          │
          │  Knowledge API           │  Observation API
          │  (read/write)            │  (write/query)
          ▼                          ▼
┌─────────────────────┐    ┌─────────────────────────┐
│  Laptop (local)     │    │  Cloud LLM/Embedding    │
│                     │    │  APIs (remote)           │
│  SLM Server (8000)  │    │                          │
│  Execution Layer    │    │  Anthropic, Google,      │
│  (local profile)    │    │  OpenAI, Mistral         │
│  Local embeddings   │    │  (cloud profile)         │
└─────────────────────┘    └─────────────────────────┘
          │
          ▼
┌─────────────────────┐
│  Mobile clients     │
│  (PWA — ADR-0048)   │
│  Phone, iPad        │
└─────────────────────┘
```

**Cloud architecture summary**: VMs host the data stores (Neo4j, PostgreSQL, Redis, Elasticsearch) and the Seshat API Gateway behind a reverse proxy. LLMs and embeddings are consumed as remote APIs — no GPU VMs needed.

### VPS requirements

The Knowledge Layer workload is I/O-bound, not compute-bound. No GPU needed. Target spec:

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Storage | 40 GB SSD | 80 GB SSD |
| Network | 1 Gbps | 1 Gbps |

**Estimated cost**: $20–40/month on Hetzner, OVH, or similar European VPS providers (data residency in EU for a Paris-based user).

**Service footprint on VPS**:

| Service | RAM baseline | Storage |
|---------|-------------|---------|
| PostgreSQL (pgvector) | ~200 MB | Grows with sessions |
| Neo4j Community | ~500 MB – 1 GB | Grows with knowledge graph |
| Elasticsearch | ~1–2 GB | Grows with logs (retention-managed) |
| Redis | ~30 MB | Event bus streams (TTL-managed) |
| Seshat API Gateway | ~100 MB | Stateless |
| Reverse proxy | ~30 MB | Config only |

Total baseline: ~2–3.5 GB RAM, well within a 4 GB VPS. The 8 GB recommendation leaves headroom for Elasticsearch under indexing load.

### Seshat API Gateway

A thin FastAPI service running on the VPS that exposes Knowledge Layer and Observation Layer APIs over HTTPS. This is **not** the full Personal Agent service — it's a subset:

- **Knowledge API**: CRUD for entities, relationships, episodes, sessions. Wraps `MemoryProtocol`.
- **Observation API**: Write traces, query metrics, read insights. Wraps telemetry modules.
- **Event bus proxy**: Publish/subscribe to Redis Streams for clients that need real-time events.
- **No execution logic**: No LLM client, no orchestrator, no request gateway. Those live in the Execution Layer (laptop or cloud profile).

The existing `service/app.py` will need to be split: knowledge/observation endpoints move to the VPS gateway; execution endpoints stay with the local (or cloud) execution service.

### Embedding consistency

Embeddings currently run locally (on the M4 Max). Moving to a cloud architecture introduces a consistency constraint: **all embeddings in the knowledge graph must use the same model and dimensions.** Mixing local and cloud embedding models would corrupt vector similarity searches.

**Decision**: Pin a single embedding model and dimension across all environments:

| Concern | Approach |
|---------|----------|
| **Model choice** | Choose one model (e.g., `text-embedding-3-small` via API, or a self-hosted model). All environments use the same model. |
| **Local execution** | If using a local embedding model, the same model must be available on the VPS (or embeddings must be generated locally and synced). |
| **Cloud execution** | If using an API-based embedding model, the VPS runs embeddings via the same API endpoint. |
| **Migration** | If the embedding model changes, all existing vectors must be re-embedded. This is a one-time batch job, not a runtime concern. |
| **Dimension consistency** | Enforced at the schema level (pgvector column dimension, Neo4j vector index config). Mismatched dimensions fail at insert time. |

The embedding model is a configuration value, not a code decision. It belongs in the profile config (ADR-0044) with validation that all profiles reference the same embedding model.

### Local simulation before cloud deployment

Before provisioning a cloud VPS, the target infrastructure must be validated locally:

**Docker Compose simulation**: Create a `docker-compose.cloud.yml` that mirrors the cloud deployment — same services, same network topology, same resource constraints (memory limits, CPU quotas). Run it on the laptop to validate:
- Service interaction patterns
- Memory footprint under realistic load
- Seshat API Gateway behavior
- Reverse proxy configuration
- Data migration scripts

**Multipass alternative**: For a higher-fidelity simulation, use Multipass to spin up an Ubuntu VM on the Mac that mirrors the target VPS spec (4 vCPU, 8 GB RAM). Deploy Docker Compose inside it. This catches issues that Docker-on-Mac abstractions hide (filesystem performance, networking, systemd service management).

**Acceptance criteria**: The cloud deployment is ready when the local simulation passes:
1. All services start and pass health checks within resource constraints
2. Seshat API Gateway serves Knowledge and Observation APIs over HTTPS
3. Data migration from local to simulated cloud completes without data loss
4. Execution Layer (running outside the simulation) can connect and operate normally

### Infrastructure as Code

All cloud infrastructure is managed declaratively — no manual VPS configuration:

| Concern | Tool | Rationale |
|---------|------|-----------|
| **VM provisioning** | Terraform (or OpenTofu) | Declarative, provider-agnostic (Hetzner, OVH, etc.), versioned state |
| **Secrets management** | HashiCorp Vault (or SOPS + age) | API tokens, database passwords, TLS certs — never in git, rotatable |
| **Service deployment** | Docker Compose (initially) | Simple, already understood. K8s is overkill for a single-user system. |
| **Configuration** | Terraform variables + Vault secrets | Separate infra config from application config |
| **DNS and TLS** | Terraform-managed DNS + Let's Encrypt via reverse proxy | Automated certificate provisioning and renewal |

The Terraform state and Vault configuration are the source of truth for the cloud deployment. Reprovisioning from scratch (disaster recovery) should be a `terraform apply` + data restore.

### Security requirements

| Requirement | Implementation |
|-------------|----------------|
| **TLS everywhere** | Reverse proxy with automatic HTTPS (Let's Encrypt). All API traffic encrypted in transit. |
| **API authentication** | Token-based auth (API keys or JWT). Managed via Vault. Rotatable. No default credentials. |
| **Encryption at rest** | LUKS or provider-level disk encryption for the VPS volume. |
| **Database access** | Not exposed to the internet. Only accessible via the Seshat API Gateway (localhost binding on VPS). |
| **Secrets management** | HashiCorp Vault (or SOPS + age). Database passwords, API keys, LLM provider tokens — never in plaintext, never in git. |
| **Cloud LLM data policies** | Verify no-training-on-input per provider. Anthropic and Google both offer this. Document per-provider status. |
| **Data sovereignty** | All knowledge stored on EU VPS. User owns all data. Export via API (JSON/CSV). Delete via API (GDPR-compatible). Migration via pg_dump + neo4j-admin dump + ES snapshot. |
| **Rate limiting** | Reverse proxy rate limiting on API endpoints. Prevents brute-force and abuse. |
| **Audit logging** | All API access logged with timestamp, client identity, action. Stored in Elasticsearch. |

### Migration path

The transition from all-local to cloud-knowledge is incremental:

1. **Phase 0: Local simulation**. Build `docker-compose.cloud.yml`, validate resource requirements and service topology locally (Docker or Multipass).
2. **Phase 1: Provision VPS via Terraform**. Deploy Knowledge Layer services (PostgreSQL, Neo4j, ES, Redis, reverse proxy). Set up Vault for secrets. No data yet.
3. **Phase 2: Data migration**. Export local data, import to cloud instances. Validate integrity. Test embedding consistency.
4. **Phase 3: Build Seshat API Gateway**. Deploy on VPS. Test from laptop.
5. **Phase 4: Reconfigure local execution** to point at cloud Knowledge Layer instead of local Docker services. Local Docker services become optional (dev/test only).
6. **Phase 5: Enable mobile access** (ADR-0048). The PWA connects to the Seshat API Gateway.

Each phase is independently testable. The local Docker setup remains available as a fallback throughout.

---

## Consequences

### Positive

- **Always-on knowledge**: The knowledge graph, conversation history, and observations persist regardless of laptop state. Mobile clients (ADR-0048) work 24/7.
- **Cloud profiles work independently**: A cloud execution profile can operate entirely without the laptop, reading context from and writing results to the cloud Knowledge Layer.
- **Background processing continues**: Consolidation, freshness updates, and feedback polling run on the VPS even when the laptop is off.
- **Modest cost**: ~$20-40/month for a VPS is negligible compared to cloud LLM API costs.
- **Data sovereignty preserved**: EU-hosted VPS, user-controlled, exportable, deletable. No vendor lock-in on the knowledge layer.

### Negative

- **Network dependency for local execution**: The laptop execution profile now requires internet access to reach the cloud Knowledge Layer. Offline operation breaks. Mitigation: a local cache/sync mechanism could be added later, but it's not in scope for this ADR.
- **Operational overhead**: A VPS requires maintenance — OS updates, certificate renewal (automated via Caddy), backup management, monitoring. This is real work, even if modest.
- **Service split complexity**: The current monolithic `service/app.py` must be split into a VPS-side gateway and a local/cloud execution service. This is a meaningful refactor.
- **Latency for local execution**: Memory queries that were previously in-process (~4ms) will now traverse the network (~10-50ms depending on VPS location). For most operations this is acceptable. For high-frequency access paths (context assembly hitting Neo4j 5-10 times), the cumulative latency increase could reach 100-300ms per request.

### Neutral

- **Local Docker setup remains for development**: Developers (and the project owner during dev) can run the full stack locally as before. The cloud deployment is additive.
- **VPS provider is not locked in**: Docker Compose works on any Linux VPS. Switching from Hetzner to OVH is a migration, not an architecture change.

---

## Alternatives Considered

| Option | Why not chosen |
|--------|---------------|
| **Managed databases (RDS, Atlas, Elastic Cloud)** | More expensive ($100+/month for the combination). Vendor lock-in. The workload doesn't justify managed services — a single-user agent doesn't need HA/auto-scaling. |
| **Kubernetes on cloud** | Massive over-engineering for a single-user system. K8s operational complexity is justified at scale; this is one user with four containers. |
| **Fly.io / Railway / Render** | Convenient but less control over data residency, more expensive at the resource levels needed (especially for persistent storage), and potential cold-start issues for always-on services. |
| **Raspberry Pi / home server** | Avoids cloud costs but introduces home network reliability issues (dynamic IP, ISP outages, power). Not practical for reliable mobile access. |
