# FRE-816 — ADR-0112 Backend-selection seam + config profiles (AC-2 foundation)

**Backing:** ADR-0112 §D3 + AC-2. Foundation ticket for the Configurable Substrate Backends project.
**Model:** Tier-1 Opus · **Stream:** build2 · **Context:** CLEAR.

## Scope (AC-2 only — the definition of done)

Deliver a **declarative, validated backend-selection seam** where **every** D3 substrate
component is pointable, per **config profile**, at a local/self-hosted OR managed backend
**with no code change** — proven by a config test. Explicitly NOT in scope (later chain
tickets): AC-1 storage allowlist guard, AC-3 managed terms-gate, AC-5 embedder-off-host,
AC-6 same-space fallback, AC-8/AC-9 dev-test resource + data isolation, and runtime
adoption (rewiring MemoryService/ES/embedder callers to consume the resolver). The ADR
itself scopes this as *"Additive; no rewrite of callers."* Master asserts the **assembled**
seam (AC-2+AC-3+AC-4+AC-6) at the integration gate — this ticket does not close the ADR.

### The D3 component set (canonical, the 7 the seam must cover)

`postgres`, `neo4j`, `elasticsearch` (stores) · `embedder` · `reranker` · `slm` ·
`vector_index` (the "search/vector index" — Neo4j-backed today). The ADR **fails the seam**
if any is omitted (an embedder-only seam, or one omitting the search/vector index, fails).

### Current reality (why a seam is needed)

- Stores: flat `AppConfig` fields — `database_url`, `neo4j_uri`, `elasticsearch_url`.
- Embedder/reranker/slm: `endpoint` on the model definition in the active `config/models*.yaml`.
- Vector index: Neo4j `entity_embedding` (rides the Neo4j store).
- **No profile axis** groups these, **no validation** proves every component is covered, and
  the custody stance is implicit/scattered — exactly what AC-2 forbids.

## Design

A new **substrate-backend manifest** parallel to the existing ADR-0099 `config/deployment.yaml`,
resolved by a new resolver + CLI, validated by the existing config-guard. Three concepts:

1. **`config/substrate.yaml`** — per-profile, per-component backend descriptors.
   Profiles: `private` (default, owner-controlled/local — the "local profile" AC-2 names),
   `managed` (opt-in), `dev`, `test` (mechanism established here; AC-9 wires enforcement).
   Each row: `{ kind: local|managed, source: <source-ref> }`. Source grammar (3 kinds, all
   resolve through `settings` — never `os.getenv`):
   - `setting:<field>` — read `getattr(settings, field)` (an existing/new AppConfig field).
   - `model_endpoint:<role>` — the `endpoint` of the active model file's model-def for that role.
   - `backed_by:<component>` — this component rides another's backend (`vector_index → neo4j`).

   ```yaml
   default_profile: private
   profiles:
     private:
       postgres:      { kind: local,   source: setting:database_url }
       neo4j:         { kind: local,   source: setting:neo4j_uri }
       elasticsearch: { kind: local,   source: setting:elasticsearch_url }
       embedder:      { kind: local,   source: model_endpoint:embedding }
       reranker:      { kind: local,   source: model_endpoint:reranker }
       slm:           { kind: local,   source: setting:llm_base_url }
       vector_index:  { kind: local,   source: backed_by:neo4j }
     managed:
       postgres:      { kind: managed, source: setting:managed_database_url }
       neo4j:         { kind: managed, source: setting:managed_neo4j_uri }
       elasticsearch: { kind: managed, source: setting:managed_elasticsearch_url }
       embedder:      { kind: managed, source: setting:managed_embedding_endpoint }
       reranker:      { kind: managed, source: setting:managed_reranker_endpoint }
       slm:           { kind: managed, source: setting:managed_slm_endpoint }
       vector_index:  { kind: managed, source: backed_by:neo4j }
     dev:  { ... local sources (mechanism only; AC-9 enforces isolation) }
     test: { ... local sources (mechanism only; AC-9 enforces isolation) }
   ```

2. **`src/personal_agent/config/substrate.py`** — the resolver + typed models + CLI.
   - `REQUIRED_SUBSTRATE_COMPONENTS: frozenset[str]` — the canonical 7 (the completeness contract).
   - `class ResolvedBackend(BaseModel, frozen)` — `component`, `kind: Literal["local","managed"]`,
     `source: str`, `target: str | None` (None = source unconfigured, e.g. managed_* unset;
     a first-class state AC-1/AC-3 will judge later, not this ticket).
   - `class SubstrateResolution(BaseModel, frozen)` — `profile: str`, `backends: dict[str, ResolvedBackend]`.
   - `load_substrate_manifest(root)`, `resolve_substrate(profile, *, settings=..., root=...) -> SubstrateResolution`.
     `backed_by` resolves transitively to the referenced component's `target`.
   - CLI `main(argv)` mirroring `resolve.py`: `--profile managed [--component postgres]` prints
     the resolved table / a single target from committed files + settings, no running container.

3. **AppConfig additions** (all `str | None = None`, documented, standards-compliant — no os.getenv):
   `managed_database_url`, `managed_neo4j_uri`, `managed_elasticsearch_url`,
   `managed_embedding_endpoint`, `managed_reranker_endpoint`, `managed_slm_endpoint`;
   plus `substrate_profile: str = "private"` (env `AGENT_SUBSTRATE_PROFILE`) selecting the active profile.
   Credential-bearing managed URLs (they may embed a password) carry
   `json_schema_extra={"secret": True}`, mirroring `neo4j_password` — so the committed-secret guard
   covers them (`managed_database_url`, `managed_neo4j_uri` at minimum; ES/endpoint URLs too for safety).

4. **Validation (the machine-checkable AC-2 gate)** — `check_substrate_manifest(root)` in
   `config_guard.py`, added to `run_all_checks`:
   - every profile declares **all** `REQUIRED_SUBSTRATE_COMPONENTS` → else a `substrate_component_missing`
     finding (this catches a component **omitted from the seam/manifest** — it does NOT, on its own,
     prove no serving path still reads `settings.X` directly; that "no code edit in the actual serving
     path" property arrives with runtime adoption, a later chain ticket).
   - every `source` is well-formed and its referent exists (`setting:` → a real AppConfig field;
     `model_endpoint:` → a declared matrix role; `backed_by:` → a declared component).
   - `kind` ∈ {local, managed}.
   Severity `policy` (blocks CI/pre-commit; consistent with the deployment-manifest checks).

## TDD steps (failing test first each time)

1. **`config/substrate.yaml`** — author the manifest (4 profiles × 7 components).
2. **`substrate.py`** — models + `load_substrate_manifest` + `resolve_substrate` + CLI.
   - Test `tests/personal_agent/config/test_substrate_resolve.py`:
     - **AC-2 core:** parametrized over the 7 components — `resolve_substrate("private").backends[c].target`
       ≠ `resolve_substrate("managed", settings=<managed_* set>).backends[c].target`, through the
       *same* `resolve_substrate` call, no code edit. (`vector_index` differs because its
       `backed_by:neo4j` target follows neo4j's per-profile target.)
     - private-profile targets **equal the live settings** (postgres→`settings.database_url`, …) —
       proves the seam mirrors reality, not fiction.
     - all 7 components resolve for every profile; unknown profile raises; unknown component raises.
     - `pyproject`/CLI smoke (mirror `test_resolve_cli.py`): `main(["--profile","managed"])` exit 0.
3. **`config_guard.check_substrate_manifest`** + wire into `run_all_checks`.
   - Test in `test_check_config.py` (or new `test_substrate_manifest_guard.py`) using a fixture root:
     - the real manifest passes (0 findings).
     - a fixture manifest **missing `vector_index`** → a `substrate_component_missing` finding
       (the ADR's explicit "omitting search/vector index must fail").
     - a bad `source` ref (`setting:nonexistent_field`, unknown role, unknown `backed_by`) → finding.
4. **AppConfig fields** — add the six `managed_*` + `substrate_profile`; document in `.env.example`.
   - Test: defaults are None/"private"; `AGENT_SUBSTRATE_PROFILE=managed` binds.
5. **Startup observability (light)** — `load_app_config()` logs the resolved substrate table for the
   active `substrate_profile` (structlog, non-fatal). Proves "boots under profile X" via a boot log line.
6. **Docs** — manifest header comment; `.env.example` keys; note in `src/personal_agent/config/AGENTS.md`
   if it enumerates config files; reference ADR-0112 AC-2.

## Acceptance-criteria proof (what master's gate reads)

**Which AC-2 branch this ticket satisfies.** AC-2 offers two branches: (a) "boots and serves under a
local profile **and** a managed profile", OR (b) "where only one backend is currently wired, a config
test shows a second profile resolves through the same interface **with no code edit**." Only local
backends are wired today, so **FRE-816 satisfies branch (b) — the config-test escape-hatch — not the
"boots and serves" branch.** The "no code edit in the actual serving path" property (rewiring
MemoryService/ES/embedder to consume the resolver) is **runtime adoption, out of scope here** and lands
with the downstream AC-5/AC-9 tickets. Per the ADR "Seam owner" paragraph, master asserts the
**assembled** seam at the integration gate; this ticket does not close the ADR.

| ADR-0112 AC-2 (branch b) clause | Proof in this PR |
|---|---|
| A second profile resolves through the **same interface**, no code edit | `test_component_swaps_by_profile[*]` — one `resolve_substrate` fn, driven by manifest+settings only; all 7 components |
| Covers **every** D3 component, not just the embedder | same test parametrized over all 7; `vector_index` included (via `backed_by:neo4j`) |
| A component **omitted from the manifest/seam** is rejected | `check_substrate_manifest` + guard test with a `vector_index`-missing fixture |
| The seam **mirrors reality** (not a fictional parallel) | private-profile resolution == live `settings` values test |
| **Out of scope, stated plainly:** runtime callers consuming the resolver | deferred to runtime-adoption ticket(s); not claimed here |

## Quality gates
`make test-file FILE=tests/personal_agent/config/test_substrate_resolve.py` → module green;
then `make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Risk / blast radius
Additive: new file + new resolver + 7 nullable AppConfig fields + one guard check + one boot log line.
No caller rewired, no store/embedder/SLM runtime path touched, no schema/migration, no deploy-class
change. Reversible (delete the file + fields). Default profile `private` == today's behavior exactly.
