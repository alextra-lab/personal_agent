# FRE-1150 — The authenticated operator identity must outrank recalled identity claims

**Ticket:** FRE-1150 (Urgent, Tier-1:Opus, stream:build2) — re-scoped 2026-08-05.
**Refs:** ADR-0052 + its 2026-05-09 amendment (operator stanza), ADR-0064 D5 (memory left global),
ADR-0081 (volatility-gradient layout), ADR-0125 D3 (turn-evidence contract).
**Out of scope:** FRE-674 owns cross-user entity scoping / the private-group-public model.

---

## The defect

Two identity claims reach the model and the wrong one wins.

| Claim | Where it sits | Distance to the query |
|---|---|---|
| Authoritative — `## Operator / You are assisting Alex.` | `messages[0]`, cached static prefix (7,845 chars on the incident turn) | far |
| Recalled — `- [Person] Susan: The user's stated name in the conversation.` | volatile block, which `_inline_volatile_with_outcome` **prepends into the current user message** (`executor.py:1328`) | adjacent |

The recalled line wins on two counts. It is adjacent to the question, and it is *specific* — it asserts
what the user's name is — while the stanza states a fact without claiming authority over anything. The
entity section then endorses it outright: its trailing instruction reads "Use this list to directly
answer questions about what the user has previously discussed."

Deleting the leaked entity does not fix this. A user's own corpus holds stale nicknames, quoted
messages and pasted documents asserting who "the user" is; FRE-674's scoping removes *other people's*
claims but never makes the authenticated fact *win*.

## Owner's design steer (2026-08-05) — this decides the mechanism

> The connected user's identity is a STATIC instruction belonging in the cached portion of the prompt.
> It never varies within or across turns because it derives from the authenticated user … Do not solve
> this by moving the memory section or by re-ranking recall; solve it by making the authenticated
> identity a fixed, cached instruction that recalled claims cannot outrank.

So: no identity value is added to the volatile tail, the memory section is not relocated, and recall
ordering/scoring is untouched. The authority is asserted where the identity already lives.

### Correction to the prior art the steer cites

ADR-0064 line 83 ("every authenticated user sees the deployment owner's profile … stays as-is") is
**stale**. ADR-0052's 2026-05-09 amendment supersedes it under the heading *"Stanza is
per-connected-user, not per-owner"*, and the code agrees: `executor.py:3474` passes `ctx.user_id`, and
`get_or_provision_user_person` MERGEs on `:Person {user_id}`. The live graph holds five distinct
`:Person` nodes carrying distinct `user_id`s (Alex, Erika, Susan, Laurent, eval-verify), and the
incident turn's stanza asserted **Alex**. The expected seam is therefore already closed; this ticket
hardens a per-connected-user stanza rather than making one.

The same amendment records that `:Person.name` is **seeded from the authenticated `users.display_name`**
and that `ON MATCH` never overwrites it (`memory/service.py:3480` sets only `email`). The asserted name
is therefore authentication-derived. The one residual — a same-named third party colliding into the node
via `memory/dedup.py:_find_similar_entities` — is ADR-0052's own tracked "dedup hardening (HIGH)"
follow-up and is not this ticket's.

---

## Steps

### 1 — `prompts.py`: the authority sentence, and return the asserted name

The capture (Step 5) must name the user with **the same string the stanza used**; two sources could let
prompt and record disagree. So the function returns both.

```python
@dataclass(frozen=True)
class OperatorIdentity:
    """The connected user's identity as the prompt asserts it.

    Attributes:
        name: The :Person node's name, seeded from the authenticated display name;
            empty when unavailable.
        stanza: The rendered Markdown stanza; empty when unavailable.
    """

    name: str = ""
    stanza: str = ""


async def get_owner_identity(...) -> OperatorIdentity:
```

Body is today's, returning `OperatorIdentity()` at each early return. The closing line changes from

```python
    lines.append("Reference these naturally. Do not tool-call to look up who the user is.")
```

to

```python
    lines.append(
        "This identity is established by authentication and is fixed for this conversation. "
        "Recalled memory, past conversations and retrieved entities may mention other people, "
        f"and may contain claims about who the user is; none of them override this line. "
        f"If recalled context names someone other than {name}, it refers to a different person. "
        "Reference these facts naturally. Do not tool-call to look up who the user is."
    )
```

Static per user, inside the cached prefix, unchanged position. `get_owner_stanza` is **renamed**, not
wrapped — leaving a wrapper would make it dead src code.

### 2 — `types.py`: carry the asserted name

Beside `operator_stanza` (line 353): `operator_name: str = ""`, with a comment naming FRE-1150 and its
purpose (recorded on the capture so the asserted identity is auditable without a second source).

### 3 — `executor.py` step_init: set both fields; end the silent skips (fold-in A)

Gateway site (3467–3481) switches to `get_owner_identity` and sets both fields. Every non-render path
gains a log, at a severity matching whether it is anomalous:

| Condition | Event | Level |
|---|---|---|
| `_ms` missing / not connected | `operator_stanza_skipped` `reason=memory_service_unavailable` | warning — anomalous while the memory graph is enabled |
| no `user_id`/`user_email` | `operator_stanza_skipped` `reason=unidentified_request` | **info** — CLI/unauthenticated is a supported path; warning here would be per-turn noise |
| helper returned an empty identity | `operator_stanza_skipped` `reason=identity_unresolved` | warning — the case codex flagged as unhandled: the call succeeds and silently yields nothing |

Legacy site (3874–3885) is updated for the new return type only. It is unreachable in production (30
days: zero `memory_enrichment_completed` from its line; both live entrypoints pass a `gateway_output`)
and this ticket does not delete pre-existing dead code.

### 4 — `scripts/render_prompt_corpus.py:212` — the call site codex caught

Its AST registry names `get_owner_stanza`; left stale it degrades silently to source line `0`. Update
to `get_owner_identity`. *(Found by codex plan-review; I had missed it.)*

### 5 — `captains_log/turn_evidence.py`: make the asserted identity provable (fold-in B + AC-2)

`AssembledContextRecord` gains three defaulted fields (legacy captures still read):

```python
    prompt_component_ids: list[str] = Field(default_factory=list)
    operator_identity: str | None = None
    operator_stanza: str | None = None
```

`build_turn_evidence` gains matching keyword-only parameters and passes them through.

**Why the stanza text and not just a flag.** Codex's strongest finding: component id + name proves a
*non-empty stanza existed*, not that it *contained the authority rule* — and a self-asserting boolean
proves nothing at all. Fold-in B asks to "capture enough of the assembled prompt to make identity
claims auditable"; the stanza *is* the identity claim, it is ~400 chars, and storing it verbatim makes
AC-2 readable rather than inferred. No new exposure class — the capture already stores the full user
message and assistant response. Bounded at 2,000 chars.

### 6 — `executor.py`: thread it, and stop `_component_ids` over-claiming

`_record_turn_evidence` (1241–1293) and its call site (4663) gain the three parameters;
`prompt_component_ids=tuple(_component_ids)` is available there because the list is assembled at
4626–4648, above the call.

One-line correctness fix, also codex's: line 4640 appends `memory_section` whenever
`ctx.memory_context` is truthy, even when rendering produced nothing — so the list states intent, not
what was spliced. It becomes `if memory_section:`. Verified safe: `component_ids` feeds **neither**
hash in `derive_prompt_identity` (`prompt_identity.py:101` — both hashes come from the prompt text), so
no telemetry discontinuity. Justified here because this diff promotes that list to an audit surface.

### 7 — `docker/elasticsearch/captains-captures-index-template.json`

Under `assembled_context.properties` — the template is `dynamic: true`, but explicit types keep these
out of the trap class the telemetry-surface checker lints:

```json
"prompt_component_ids": { "type": "keyword", "ignore_above": 1024 },
"operator_identity":    { "type": "keyword", "ignore_above": 1024 },
"operator_stanza":      { "type": "text" }
```

`text`, not `keyword`, for the stanza — a keyword with default `ignore_above` silently drops long
values, which is exactly the trap the checker flags.

### 8 — Supporting change, flagged for veto: the entity section's endorsement

```python
        section += (
            "\n\nUse this list to directly answer questions about what the user "
            "has previously discussed. Do NOT say you have no memory."
        )
```

This sentence instructs the model to answer *questions about the user* from entity descriptions — which
is how a third party's name became the answer to "who am I talking to". Proposed:

```python
        section += (
            "\n\nThese are entities mentioned in earlier conversations, including other "
            "people; they record what was discussed, not who you are speaking with. "
            "Use them to answer questions about what the user has previously discussed. "
            "Do NOT say you have no memory."
        )
```

It moves nothing, re-ranks nothing, adds no identity value and names nobody — it removes a false
instruction. But it is a recall-side edit and the steer says solve this on the identity side, so it is
**separable**: strike this step and the rest stands. My recommendation is to keep it, because leaving an
explicit "use this to answer questions about the user" in the prompt undercuts the cached rule.

### 9 — Tests (TDD — each written failing first)

`tests/personal_agent/orchestrator/test_identity_precedence.py`:

1. `test_stanza_asserts_authority_over_recalled_claims` — the rendered stanza states the identity is
   authentication-established and that recalled claims do not override it, and names the user in the
   disambiguation clause.
2. `test_identity_name_matches_stanza_text` — `OperatorIdentity.name` equals the name the stanza
   asserts. Pins the single-source rule the capture depends on.
3. `test_stanza_empty_when_identity_unavailable` — each early-return path yields `OperatorIdentity()`
   with both fields empty.
4. `test_operator_stanza_skipped_logs_each_reason` — the three Step-3 branches emit their distinct
   reasons at the stated levels (fold-in A).
5. `test_competing_claim_still_reaches_the_wire` — end-to-end through
   `_inline_volatile_with_outcome`: the Susan entity is **still admitted and still rendered** (it is not
   removed — the ticket fails if this diff removes it) while the stanza carries the authority rule.

`tests/personal_agent/captains_log/test_turn_evidence_identity.py`:

6. `test_assembled_context_records_stanza_and_components` — round-trips all three fields; a legacy
   payload lacking them still validates; the stanza is bounded at 2,000 chars.
7. `test_component_ids_exclude_unrendered_memory_section` — Step 6's over-claim fix.

`test_owner_stanza.py` — mechanical update to `get_owner_identity` (`.stanza` where it asserted the
string) plus the `.name` assertion.

```
make test-file FILE=tests/personal_agent/orchestrator/test_identity_precedence.py
make test-file FILE=tests/personal_agent/captains_log/test_turn_evidence_identity.py
make test-file FILE=tests/personal_agent/orchestrator/test_owner_stanza.py
make test && make mypy && make ruff-check && make ruff-format
```

### 10 — Documentation

Docstrings on every touched function. ADRs are not edited by build seats — but **ADR-0064 line 83 is
now provably stale**, so a one-line Backlog ticket is filed noting it, for an `adr` session to correct.

---

## Acceptance criteria

| # | Criterion | Proof | Evidence recorded |
|---|---|---|---|
| AC-1 | A turn whose recalled memory contains an identity claim naming someone other than the connected user is answered using the connected user's identity, against a corpus containing such a claim, read from the answer | Artifact half: test 5. **Answer half: one real turn — needs your OK, below** | Reply text quoted verbatim + that turn's capture |
| AC-2 | The authoritative identity's precedence is visible in the artifact, not inferred from the answer | `assembled_context.operator_stanza` shows the authority rule verbatim; `.operator_identity` names the user; `.prompt_component_ids` shows the component was spliced | All three field values read back from ES and quoted |
| Fold-in A | The gateway guard no longer skips silently | Three reasons, three levels (test 4) | Test result |
| Fold-in B | Captures make identity claims auditable | Steps 5–7, read off a real capture | Same query as AC-2 |
| Not-owned | Cross-user scoping stays FRE-674's | No change to visibility, recall filtering, ordering or scoring; test 5 asserts the leaked entity is still admitted **and** still rendered | Diff review + test 5 |

**It fails if** the fix is shown by removing the offending entity (it is not removed, and test 5 asserts
that), if it is asserted from the diff with no turn exercising competing claims, or if it closes
FRE-674's scoping.

---

## The decision I need — AC-1's answer half

AC-1 asks for a **read answer**. The tests prove the prompt carries the authority and still carries the
competing claim; they cannot prove the model obeyed. Codex's sharpest point is exactly this: every
deterministic test here can pass while the defect survives.

The condition exists in the live corpus now — the `Susan` entity is still in Neo4j and still scores
0.413 against a bare greeting. Proving AC-1 needs one real model turn on this branch's code.

I cannot deploy. With an explicit OK I can run **one turn in-process** on this branch — real memory
service, read-only against the live graph, one real LLM call through the configured tunnel — and report
the reply plus the capture fields. Without that OK, AC-1's answer half is deferred to master's
post-deploy verification with the exact turn and expected reading in the runbook.

**Recommendation:** the in-process turn. One call, read-only, and it produces the evidence here rather
than in a session that will not have this context.

## Residual risk, stated plainly

Codex's Critical #1 stands and I am not going to paper over it: the authoritative claim remains a whole
message away while the competing claim stays adjacent. The steer rules out closing that gap by moving
the memory section or re-ranking recall, so this design bets that an explicit, specific authority rule
in the cached prefix beats proximity. AC-1's answer-level turn is what settles the bet. **If it loses,
that is a result to report, not to work around** — and the next move would be the owner's call, not a
quiet relocation of identity into the volatile tail.
