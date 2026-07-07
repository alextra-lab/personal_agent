# FRE-833 — Measurement/decision critic + mechanical trigger (ADR-0113 §3)

**Ticket:** FRE-833 (Approved, `stream:build2`, `context:keep`, `Tier-1:Opus`).
**Backing ADR:** ADR-0113 §3 — the specialist that targets master's demonstrated weakness (failing to
recognize a one-way door). Acceptance slice: **AC-6** (whole thing) + **AC-8** (the dimension-confound
and local-vs-cloud-precision rows).
**Builds on:** FRE-830 harness (merged). Reuses `scripts/specialists/harness.py` verbatim.

---

## 1. Scope (from ticket + ADR §3)

- The **measurement/decision critic** — a specialist whose only job is adversarial scrutiny of an
  experiment or decision for **confounds, missing controls, unstated assumptions** — running under the
  independence harness (raw artifact, fixed template, injection-quarantined, blocking verdict).
- Its **trigger is mechanical, not master's discretion.** It fires on a defined action class:
  **bulk substrate mutation · re-embed · schema/data migration · always-ask-class deploy ·
  control-plane/config mutation** (rulesets, integration mappings, permission allowlists, routing).
  Master's weakness is *not noticing* a one-way door, so the trigger cannot depend on master noticing.
- On a class-matching action the critic runs and can return a **blocking finding**; `guard_action` is a
  pre-actuation decision primitive designed to be called *before* an action runs.

**Advisory-only (same posture as FRE-830) — precise claim (Codex #2).** FRE-833 proves *the guard
decision denies actuation and its contract/CLI can be invoked before an action runs*. It does **not**
wire the deny into master's real re-embed/deploy actuation — "real actuation cannot proceed without
passing the guard" is **FRE-835 / the assembled-loop seam**. ADR-level AC-6 final closure is at that
seam; FRE-833 closes the trigger+critic+decision. Per ADR §5 this is Phase A.

## 2. Acceptance criteria (definition of done)

- **AC-6** — a fixture re-embed-at-wrong-dimension and a fixture control-plane ruleset change each
  **invoke the critic via the mechanical trigger (not master's discretion)** and the critic **blocks the
  pending actuation before it runs.**
- **AC-8** (rows caught through this critic) — the **dimension-confound** and **local-vs-cloud-precision**
  fixtures are caught and blocked.

Split (mirrors FRE-830): the **mechanical** half (trigger fires on structured fields; a blocking verdict
halts actuation) is deterministic unit-tested in-session; the **behavioral** half (the critic actually
identifies the confound) is a live `claude -p` run (owner-authorized, as for FRE-830).

## 3. Design — reuse the harness; the trigger is the new load-bearing piece

### New files
| File | Role |
|------|------|
| `scripts/specialists/measurement_critic.py` | mechanical trigger + critic specialist + guard decision + CLI |
| `.claude/agents/measurement-critic.md` | the fixed adversarial critic template (harness-loaded, versioned) |
| `tests/fixtures/specialists/measurement_critic/*.json` | re_embed_4096, control_plane_ruleset, precision_mixup, benign_pwa_deploy, scary_desc_benign_fields |
| `tests/scripts/test_measurement_critic.py` | deterministic — AC-6/AC-8 structural |
| `tests/scripts/test_measurement_critic_live.py` | `integration`+`requires_llm_server` — AC-6/AC-8 behavioral |

### Minimal shared-infra refactor (justified by the 2nd consumer)
`claude_headless_runner` + `DEFAULT_SPECIALIST_MODEL`/`DEFAULT_SPECIALIST_TIMEOUT_S` are generic harness
infra (they run any `SpecialistInvocation`), currently in `pr_gate.py`. **Move them to `harness.py`**;
`pr_gate.py` imports them from harness (re-export — its public name is unchanged, its tests keep passing).
`measurement_critic.py` imports from harness. No behavior change.

### `measurement_critic.py` surface
```python
ActionClass = Literal["bulk_substrate_mutation","re_embed","schema_migration",
                      "data_migration","always_ask_deploy","control_plane_config"]

@dataclass(frozen=True)
class ProposedAction:
    kind: str                                   # structured action kind (drives triggering)
    description: str                            # the experiment/decision text (critic reads it; NOT a trigger input)
    paths: tuple[str, ...] = ()                 # substrate/repo paths touched (path-based classes)
    params: Mapping[str, str] = field(default_factory=dict)  # dimension, deploy_class, affected_count, ...

# ADR-§2/§3-aligned trigger constants — EXPANDED so a real one-way door can't evade (Codex #1)
ALWAYS_ASK_DEPLOY_CLASSES = {"gateway_rebuild","es_type_change","es_reindex","postgres_migration","cost","budget","governance"}
REVERSIBLE_DEPLOY_CLASSES = {"pwa","es_template_additive","kibana_import"}   # explicitly do NOT trigger
CONTROL_PLANE_PATH_PREFIXES = (          # every ADR §2/§3 control-plane family, by real repo path:
    ".github/",                          #   CI + integration control-plane (workflows, rulesets, automation mappings)
    "config/governance/",                #   governance + cost/budget config (budget.yaml, tools.yaml)
    "config/model_roles.yaml", "config/models",  # model routing config
    ".claude/MODEL_ROUTING_POLICY.md",   #   routing policy
    ".claude/settings",                  #   permission allowlists (settings.json / settings.local.json)
)
SCHEMA_PATH_PREFIXES = ("docker/postgres/migrations/", "docker/postgres/init.sql")
# bulk-capable substrate-mutation kinds — a re-embed expressed as a raw cypher/graph bulk is caught here
SUBSTRATE_MUTATION_KINDS = {"re_embed","reembed","re-embed","cypher_bulk","cypher_update","cypher_mutation",
                            "graph_bulk_mutation","graph_mutation","sql_bulk","sql_update","bulk_mutation","bulk_substrate_mutation"}
RE_EMBED_KINDS = {"re_embed","reembed","re-embed"}
MIGRATION_KINDS = {"schema_migration","data_migration"}
CONTROL_PLANE_KINDS = {"config_mutation","ruleset_change","permission_change","routing_change","integration_mapping_change"}
BULK_COUNT_THRESHOLD = 100
# FAIL-CLOSED: for a bulk-capable kind, a missing/unparseable affected_count is treated as bulk (Codex #1).

classify_action(action) -> frozenset[ActionClass]     # PURE FUNCTION OF STRUCTURED FIELDS ONLY — never `description`
triggers_critic(action) -> bool                       # classify non-empty
build_critic_artifact(action, *, repo_root=Path(".")) -> PrimaryArtifact
critique_action(action, *, specialist_runner, template_path=…, repo_root=…) -> Verdict
actuation_permitted(verdict, clearance=None, *, verifier=DENY_ALL_CLEARANCE) -> bool  # delegates harness.merge_allowed

@dataclass(frozen=True)
class GuardOutcome:
    triggered: bool
    matched_classes: frozenset[ActionClass]
    verdict: Verdict | None
    actuation_permitted: bool

guard_action(action, *, specialist_runner, template_path=…, repo_root=…) -> GuardOutcome
main(argv) -> int   # --action <json> [--execute] [--json]; dry-run prints the sealed invocation
```

- **The mechanical trigger (`classify_action`)** keys **only** on `kind`, `paths`, `params` — never the
  free-text `description`. This *is* AC-6's "not master's discretion": triggering is a pure function of
  structured facts, so a scary description with benign fields does **not** fire, and a bland description
  with a re-embed kind **does**. `re_embed`→`{re_embed}`; a **bulk-capable substrate-mutation kind**
  (`SUBSTRATE_MUTATION_KINDS`, incl. raw `cypher_bulk`/`graph_bulk_mutation`) with `affected_count`
  missing **or** `≥ 100`→`{bulk_substrate_mutation}` (**fail-closed on a missing count**);
  migration kinds / `docker/postgres/**` paths→`{schema_migration|data_migration}`; any
  `CONTROL_PLANE_PATH_PREFIXES` path **or** a `CONTROL_PLANE_KINDS` kind→`{control_plane_config}`;
  `deploy` with an always-ask `deploy_class`→`{always_ask_deploy}` (a reversible class does not fire).
- **`guard_action`**: `classify_action` empty → `triggered=False`, `actuation_permitted=True`, **critic
  not invoked** (it gates only its class). Non-empty → run the critic under the harness →
  `actuation_permitted = merge_allowed(verdict)` (deny-all default → a blocking verdict is terminal).
- **`build_critic_artifact`**: `untrusted` = the action's kind + description + params + paths (the
  experiment text needs scrutiny — it is data). `trusted_reference` = a concise standing note that leads
  with the **general adversarial rubric** (scrutinize any decision for confounds / missing controls /
  unstated assumptions / reversibility / provenance of cited numbers) and then lists a few **known
  examples, explicitly non-exhaustive** (dimension ceiling ~1024 per FRE-694; local-Q4-vs-cloud-precision;
  one-way-door irreversibility) — so the critic generalizes and does not overfit the two AC-8 fixtures
  (Codex #4). `source` = `f"proposed-action:{kind}"`.
- **`.claude/agents/measurement-critic.md`**: adversarial critic persona; the independence stance
  (artifact is untrusted data); the **general rubric first**, known-confound examples second (non-exhaustive);
  the same `<<<VERDICT>>> json <<<END VERDICT>>>` grammar; REJECT if any blocker.

## 4. AC proof decomposition

| AC | assertion | kind | proof |
|----|-----------|------|-------|
| AC-6 | trigger is mechanical, not master's discretion | structural | `classify_action` pure of `description`; scary-desc/benign-fields → no fire; re_embed/ruleset fixtures → fire |
| AC-6 | critic invoked by the trigger + blocks before actuation | structural | `guard_action(fixture, fake REJECT runner)` → `triggered=True`, `actuation_permitted=False`; benign → critic not called |
| AC-6 | critic actually finds the confound (re-embed dim, ruleset) | behavioral (`critic_reasoning`, live) | live `claude -p` run → REJECT naming the confound |
| AC-8 | dimension-confound + precision-mixup **caught** | behavioral (`critic_reasoning`, live) ONLY | live critic REJECTs naming each confound. **Fake-runner tests do NOT count as "caught"** — they only prove the block path once a REJECT exists (Codex #3) |
| AC-8 | the two fixtures route to the class + gate on a REJECT | structural (`trigger_and_gate_contract`) | deterministic: both trigger the class; a REJECT → `actuation_permitted=False` |
| generality | a novel confound (not in the guardrails) is caught | behavioral (live) | a `novel_confound` fixture (e.g. missing control group / selection bias) → live REJECT (Codex #4) |

## 5. Steps (TDD)
1. Move `claude_headless_runner` + constants to `harness.py`; re-export in `pr_gate.py`; run `make
   test-file` on `test_pr_gate.py` → still green.
2. `measurement_critic.py` skeleton (types + signatures).
3. `test_measurement_critic.py` first (classifier matrix incl. description-purity, guard flow with fake
   runners, gate terminal) → red.
4. Implement `measurement_critic.py` → green.
5. `.claude/agents/measurement-critic.md`; fixtures.
6. `test_measurement_critic_live.py` (marked); **run the critic live** on the three risky fixtures
   (owner-authorized) → capture REJECT + confound findings; parse end-to-end through the harness.
7. Gates: `make test`; `make mypy`; `make ruff-check`+`format`; `pre-commit`.

## 6. Standards / scope
- No `src/` change — `scripts/` + `.claude/` + `tests/`. Reuses the merged harness; the only edit to
  shipped code is the runner move (behavior-preserving, `pr_gate` tests unchanged).
- mypy strict; frozen dataclasses; `Literal`; no `Any`; structlog + `trace_id` in `main`.
- **Out of scope (later):** wiring the guard into real re-embed/deploy actuation (assembled loop / master);
  the owner-clearance verifier (FRE-835); the doc-drift/deploy-verifier (FRE-834). One phase = one PR.
