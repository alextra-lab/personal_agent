# FRE-1068 — Telemetry free-text inventory, policy, and emit-seam enforcement

**Ticket:** FRE-1068 (Approved, Tier-1:Opus, stream:build2)
**Refs:** ADR-0090 (telemetry surface contract) · ADR-0129 D5/D7/AC-7 (scopes log-side redaction *out*) · FRE-1175 (the live credential this audit found) · FRE-1113 (in-flight ADR owning the *ingest-pipeline* enforcement tier — deliberately untouched here)

**Revision 2** — rewritten after codex plan-review. Three of the first draft's positions were wrong and are corrected below; the correction is recorded rather than quietly patched.

---

## What the audit established (read-only, before this plan)

1. **The `free_text` dynamic template is not the boundary of the exposure.** It governs
   *searchability*, not *storage*. Elasticsearch retains the full value in `_source` regardless of
   mapping, so a field mapped `keyword`/`ignore_above:1024` — or not indexed at all — still carries
   verbatim content.

2. **Measured proof** (full-corpus scan, 1,916,115 documents). `arguments.command` returns **0** from
   an `exists` query while **262 documents** carry full shell and `curl` command lines in `_source`
   (the `arguments` subtree is `dynamic:false`). `command` is `keyword`/`ignore_above:1024`: 951
   documents carry it, `exists` sees 561 — **390 blind**. Across the family, **43 fields** are wholly
   invisible to `exists` and **174 of 273** show some gap. An inventory built on `exists` counts
   would have called those fields clean — the exact failure the ticket's AC warns about.

3. **A live secret, not a hypothetical.** The current `POSTGRES_PASSWORD` sits in plaintext in four
   `agent-logs-2026-08` documents → **FRE-1175** (remediation is master's). ES is bound to `127.0.0.1`,
   which bounds severity.

4. **Verbatim conversation content incl. special-category data** — health-topic web queries,
   medication-related task text carried in a *filename* field (`checkpoint_reflections`), third-party
   email addresses, and assembled prompt context including KG-derived profile preferences.

---

## Corrections forced by codex review (draft 1 → draft 2)

| Draft 1 claimed | Verified reality | Consequence |
|---|---|---|
| Redacting in `log_event()` covers the index | **False.** `agent-logs-*` has **five** write paths: `log_event` (:174), `log_batch`/`async_bulk` (:214), `index_request_trace_from_snapshot` (:372, :402), `index_latency_breakdown` (:492, :516). Four bypass `log_event`. | Replaced by a **single private chokepoint** every agent-logs write funnels through. |
| Emails can't be redacted because `user_id` is an email join key | **False.** `user_id` in `agent-logs-*` holds **UUIDs** (3 distinct values, all UUID-form). | Emails are redacted **unconditionally**; the exemption had no basis. |
| Narrowing `free_text` would break dashboards | **Unsubstantiated.** **Zero** Kibana dashboard files reference these fields. | Still declined, but for the *real* reason: it does not reduce storage, and post-redaction the stored content is already governed. Recorded as an Open remedy, not justified by a false claim. |
| Fail-open on redaction error | Contradicts "secrets always redacted". | **Fail-closed on content**: redaction never raises; a failing value becomes `[REDACTED:error]`. |
| A structlog processor also covering JSONL/console | It would not have worked as drawn (`foreign_pre_chain` omits it), and it widens scope past the ticket. | **Dropped.** Local-file exposure filed to Backlog instead. |

---

## Scope

| In scope | Out of scope (and why) |
|---|---|
| Committed, re-runnable inventory script + committed inventory artifact | Ingest-pipeline enforcement — FRE-1113's ADR owns that tier |
| Written content policy per content class | Rotating the credential / deleting documents — FRE-1175, master's |
| Redaction at a single agent-logs write chokepoint, fail-closed | Captain's Log captures, Postgres, Neo4j — different substrates |
| Positive control proving the rule fires, against a real index | Narrowing `free_text`; capping conversation length (owner call, see below) |

---

## Steps

### 1 — Inventory script → `scripts/audit/fre1068_free_text_inventory.py`

- **Full streaming scan** of `agent-logs-*` (~1.9M docs) via `search_after`, bounded memory — not a
  sample, because the AC says *every* field. Aggregates as it streams; never accumulates documents.
- Enumerates every string-valued leaf field from `_source` (**not** `exists`).
- Per field: mapping type, claiming dynamic template, `exists` count, `_source` count, **blind-spot
  delta**, max/mean length, top event types, content class.
- Runs the detector corpus and reports **detector + field + doc count** — never the matched value.
- `--json`; exits non-zero when the field set is empty (a finding-nothing audit is a *failed* audit).

**Verify:** `uv run python -m scripts.audit.fre1068_free_text_inventory --json` reports a non-empty
field set and a non-zero blind-spot delta for `arguments.command`.

### 2 — Inventory artifact → `docs/research/2026-08-06-fre1068-telemetry-free-text-inventory.md`

Every free-text-carrying field with document count, content class, blind-spot column. **No secret
values, no third-party PII, no deployment identifiers** — counts, field names and content *classes*
only (public repo).

Classes: `conversation` · `third-party-content` · `agent-action` · `system-diagnostic` · `structural`.

**Verify:** ≥12 fields incl. `arguments.command`; `check_no_deployment_identifier.py` +
`check_no_personal_paths.py` pass.

### 3 — Policy section, per content class

| Class | Decision |
|---|---|
| Secrets / tokens / credentials | **Redacted, fail-closed, unconditionally** |
| Email addresses | **Redacted** (no join-key cost — `user_id` is a UUID) |
| Conversation / prompt content | **Permitted, uncapped** — the debugging value is the reason these fields exist. Length capping is a behaviour change deferred to the owner (see Open question). |
| Agent actions (commands, tool args) | Permitted, but secret-redacted — this is where FRE-1175 landed |
| System diagnostics, structural | Permitted unchanged |

### 4 — `src/personal_agent/telemetry/redaction.py` (TDD)

```python
def redact_text(value: str) -> str: ...
def redact_mapping(data: Mapping[str, object]) -> dict[str, object]: ...  # recursive: dicts + lists
```

Detectors, one compiled alternation: AWS access key · AWS secret-access-key assignment · PEM private
key **body, not just the header** · JWT · GitHub PAT · Slack token · `sk-`-style key ·
`scheme://user:pass@` · `password|passwd|secret|api_key|token` assignment in `=`, `:` and `--flag`
forms · email.

**False-positive guard (measured from live data):** the assignment detector must not fire on env-lookup
or placeholder forms — `os.environ.get(...)`, `os.getenv(...)`, `environ[...]`, `${VAR}`, `$VAR`,
`<...>`, `changeme`, `xxx`. Live telemetry contains `password=os.environ.get(`; redacting it protects
nothing and destroys diagnostic value.

**Fail-closed:** `redact_*` never raises. A value whose redaction errors becomes `[REDACTED:error]`.
Marker format `[REDACTED:<detector>]` so a fired rule is visible, never silent.

**Verify:** `tests/test_telemetry/test_redaction.py` — a corpus of **positive** cases (one per detector)
and **negative** cases (env lookups, placeholders, ordinary prose, code, URLs without credentials),
plus nesting, idempotency, non-string passthrough. Written first, confirmed failing.

### 5 — The single chokepoint (TDD)

Add to `ElasticsearchLogger`:

```python
async def _index_agent_log(self, document, *, id=None) -> str | None
```

— applies `redact_mapping`, then indexes to `_get_index_name()`. **Every** agent-logs write is routed
through it: `log_event`, `index_request_trace`, `index_request_trace_from_snapshot`,
`index_latency_breakdown`, and the per-step/per-phase inner writes. `log_batch` redacts each action's
`_source` before `async_bulk`.

`index_document()` (Captain's Log named indices) is **not** routed through it — different family,
different template, out of this ticket's scope.

**Structural guard:** a test asserting no `self.client.index(` call in `es_logger.py` targets
`_get_index_name()` outside the chokepoint, so a future write path cannot silently reopen the hole.

**Verify:** `make test-file FILE=tests/test_telemetry/test_es_logger.py`.

### 6 — Positive control (the AC's own test)

Two layers, because the AC says *in the index*:

- **Unit (gates CI):** a record with a `free_text`-matching field (`raw_preview`) holding a planted
  secret-shaped value goes through each of the five write paths; the document handed to the client
  carries the marker and not the secret. Plus the negative half — a clean record passes through
  byte-identical, so a never-firing rule is distinguishable from a working one.
- **Real round-trip (recorded as AC evidence):** the same record written to the **test-stack ES
  (:9201, FRE-375 isolation)**, then read back with `_source` and asserted clean. Output recorded
  verbatim in the handoff.

### 7 — Docs + quality gates

Update `src/personal_agent/telemetry/AGENTS.md` (the chokepoint and its guarantee) and the
`_meta.description` in `docker/elasticsearch/index-template.json` (record that `free_text` governs
searchability, not storage, so the next reader does not repeat the ticket's assumption).

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` ·
code-review at **high** · security-review.

---

## Acceptance criteria

| # | Criterion (from the ticket) | How proven | Evidence recorded |
|---|---|---|---|
| AC-1 | A committed inventory listing every field carrying free text, with document count and content class. Finding nothing is a failure. | Full streaming scan via `scripts/audit/fre1068_free_text_inventory.py`; output written to `docs/research/…`. | Field count, per-field doc counts, the `arguments.command` 262-vs-0 blind-spot delta, detector hit counts. |
| AC-2 | After enforcement, a positive control: a record with a pattern-matching field holding a planted secret-shaped value is absent or redacted in the index. | `test_redaction.py` positive/negative controls across all five write paths + a real round-trip through test-stack ES. | Test names + pass output; the observed `_source` read back from the index. |

Both decidable from this ticket's own deliverable. No ADR criteria carried (ADR-0130 D1).

## Open remedies

- Narrow `free_text` / set `index:false` on content fields — defence in depth for *searchability*; does not reduce storage. **File to Backlog.**
- Local rotating JSONL logs (`telemetry/logs/current.jsonl`) receive the same unredacted content; out of this ticket's index-scoped AC. **File to Backlog.**
- Whether the same credential reached other substrates — carried on FRE-1175.

## Open question for the owner

**Should verbatim conversation content be length-capped at emit?** Today `user_message`, `command` and
`task_name` are uncapped while sibling fields use a ~100–200 char `*_preview` convention. Capping is a
behaviour change with real debugging cost, so the plan's default is **no cap** — permitted and
documented. Say the word and it becomes a fourth policy rule.

## Risks

| Risk | Mitigation |
|---|---|
| Redaction false-positives destroy diagnostic value | High-precision detectors; env-lookup/placeholder guard measured against live data; explicit negative corpus; marker names the detector |
| Redaction on every ES write costs latency | One compiled alternation, min-length gate, non-string short-circuit; ~73k records/day |
| A future write path bypasses the chokepoint | Structural guard test (step 5) |
| Template `_meta` edit reads as a schema change | Comment-only; no mapping change, no reindex, no deploy-class change |
