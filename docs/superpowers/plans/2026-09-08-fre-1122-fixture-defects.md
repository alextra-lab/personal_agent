# FRE-1122 — clear the defects that block the baseline run

**Ticket:** [FRE-1122](https://linear.app/frenchforest/issue/FRE-1122) · **Branch:** `fre-1122-absence-probe-fixture-defects`
**Prior art:** PR #799 built the fixture. This plan does not re-author it.
**Codex plan-review:** one round, 2026-09-08. Findings folded in below and attributed inline.

---

## What this plan is, and what it is not

The fixture merged in PR #799 and its code is live. The ticket returned to `Approved` because it
was never waiting for a deploy. It waits for the **baseline run**, and the run is blocked by three
defects master recorded across two failed attempts on 2026-08-04 — plus a fourth, found in review,
which would have let the run *appear* to succeed while answering AC-3 wrongly.

This plan clears all four. **It does not fire the run.** The run needs the owner's explicit
authorization, and it is not this seat's to start.

### The acceptance-criteria gap, stated before any code

FRE-1122 carries seven acceptance criteria. **None is decidable from this PR's deliverable.**
AC-1 through AC-7 all assert against the report of a live run:

| AC | What it needs |
|----|---------------|
| AC-1, AC-2 | preflight evidence over the real corpus |
| AC-3 | a run, then cleanup, then a third query pass |
| AC-4, AC-5 | twenty classified answers |
| AC-6 | the substrate decision, which AC-3 produces |
| AC-7 | the real probe set, which is gitignored personal content |

This PR is infrastructure toward those criteria, exactly as PR #799 was. **FRE-1122 must not reach
`Done` on this merge.** The criteria this PR *can* be judged against are stated per defect below,
and each one is a test.

---

## Defect A — the run phase sends no identity, and cannot check the one it sends

### What is wrong

`runner.py:319` posts to `/chat` with no headers. `service/auth.py:195` reads
`Cf-Access-Authenticated-User-Email` and returns 401 when it is absent and `gateway_auth_enabled`
is true. The run returns 401 and fires nothing.

### Master's recorded remedy is off target, and the correction is worth recording

The 2026-08-04 comment proposed `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` service-token
headers. The same-day correction retracted that against ADR-0132 D1, which retires the pattern.

**Both are off target.** Service tokens authenticate *outbound* egress through the Cloudflare
barrier, which is what ADR-0132 governs. This runner calls `http://localhost:{service_port}/chat`
— a loopback call to our own service — and `/chat` depends directly on `get_request_user`
(`service/app.py:2031-2039`). Its 401 is an *inbound identity* failure. ADR-0132 D1 itself
separates outbound egress from retained inbound request identity (ADR-0132:128-137), and Caddy's
egress listeners are ports 8600/8601 scoped to model, embedding and artifact paths — not `/chat`
(`config/cloud-sim/Caddyfile:182-210`). **Caddy does not unblock this.**

The correct pattern is already in the repository: `scripts/eval/recovery_harness.py:147-171` sends
`Cf-Access-Authenticated-User-Email` on the loopback call.

On loopback this header is trusted plain text, not independently verified. That is the deployment's
existing trust model, not something this fixture introduces.

### The second, larger problem the auth fix exposes

`service/auth.py:155-173` **upserts** the email into `users` and returns a fresh `user_id` for an
unknown address. Sessions are then owned by that UUID (`app.py:2141-2153`). So an `--auth-email`
that is not the owner's silently creates a new user with an empty corpus. Every present probe then
reads absent, and the report states a clean, entirely meaningless baseline.

The turn-, claim- and message-history ground-truth checks scope to `--user-id`
(`ground_truth.py:216-240`). The entity check is deliberately global, because `:Entity` is keyed by
name rather than owner (`ground_truth.py:440-443`) — so the mismatch corrupts most of the evidence,
not all of it, which makes it *harder* to notice, not easier.

**Nothing today makes the two identities the same.** A baseline taken with them different is void
and looks fine.

### The change

1. Add `--auth-email`, **required for the `run` phase**. The header wins over the dev-mode fallback
   (`auth.py:195-199`) regardless of `gateway_auth_enabled`, so requiring it is correct on both
   deployments and makes the run identity explicit rather than implicit.
2. Send it as `Cf-Access-Authenticated-User-Email` on every probe POST.
3. **Bind the two identities before firing.** In `_phase_run`, before the HTTP client opens, query
   `SELECT user_id FROM users WHERE email = lower($1)`. Refuse when:
   - no row — the run would *create* the user and measure an empty corpus;
   - the row's `user_id` is not `--user-id` — the run would measure a different corpus from the one
     preflight evidenced.

`users` is the canonical email→UUID mapping (`service/models.py:38-50`) and the email is already
normalised with `.lower()`, so the `SELECT` is the right authority. A session join is not, because
no session exists before the first turn fires.

Refusal, not a warning. The failure it prevents is silent and voids the whole baseline.

### Proof

| Criterion | Test |
|---|---|
| The run sends the owner identity header | `test_the_run_phase_sends_the_owner_identity_header` |
| An unknown email is refused before any turn fires | `test_an_unknown_auth_email_is_refused_before_firing` |
| A mismatched user is refused before any turn fires | `test_an_auth_email_for_another_user_is_refused` |
| `--auth-email` is required for `run`, and only for `run` | `test_the_run_phase_requires_an_auth_email` |

---

## Defect B — twenty probes share one session, so no two are asked under equal conditions

### What is wrong

`runner.py:311-323` threads one `session_id` through all twenty probes. Every probe after the first
is answered inside the conversation history of the ones before it. Present probes seed that history
with real stored content; absent probes seed it with a run of absence declarations. Both bias what
follows. The history cap (`conversation_max_history_messages`, default **20** —
`settings.py:1001`; master's comment said ten, which is stale) then rolls the earliest probes out
partway through, so the twenty are not even biased *uniformly*.

The six-cell classification is per answer and never references a prior turn, so the shared session
buys nothing and costs the independence of every measurement.

### The change, and the trap in it

One session per probe: stop passing `session_id` on the POST, and record the session the service
returns per answer.

**The trap.** Cleanup is written against a single `$sid`. `_DELETE_ENTITIES`
(`ground_truth.py:689`) retains a probe-created entity if any turn *outside `$sid`* discusses it.
Under one shared session, cross-probe adoption inside the run is invisible. Under twenty sessions,
probe 3's entity mentioned on probe 7's turn becomes "adopted, therefore retained" — **residue the
split itself manufactures**, which would push AC-3 toward the test-substrate branch for a reason
that is an artifact of this change rather than a property of the substrate.

So the cleanup unit must become **the run**, not the session. Cleanup semantics then stay exactly
what three Codex rounds reviewed; only the boundary widens.

### The predicate rewrites, corrected

Codex round 4 found three errors in the first draft of this table. Corrected:

| Constant | Predicate today | Becomes |
|---|---|---|
| `_SNAPSHOT_TURNS`, `_DELETE_TURNS` | `t.session_id = $sid OR t.originating_session_id = $sid` | both `IN $sids` |
| `_SNAPSHOT_ENTITIES`, `_DELETE_ENTITIES` | `e.originating_session_id = $sid` **and** `NOT EXISTS { … t.session_id <> $sid AND t.originating_session_id <> $sid }` | `IN $sids`; inner turn predicates become `NOT … IN $sids` |
| `_ADOPTED_ENTITIES` | same entity-origin test, but a **positive** `EXISTS` — *not* a negation, as the first draft grouped it | `e.originating_session_id IN $sids`; inner turn predicates `NOT … IN $sids`; `EXISTS` stays positive |
| `_MUTATED_ENTITIES`, `_FILLED_DESCRIPTIONS` | turn predicate `= $sid` **plus** an entity-origin predicate `coalesce(e.originating_session_id,'') <> $sid` the first draft omitted | turn predicates `IN $sids`; entity-origin becomes `NOT … IN $sids` |
| `_SNAPSHOT_CLAIMS`, `_DELETE_CLAIMS`, `_RUN_CLAIM_IDS` | map-match `(cl:Claim {session_id: $sid})` | `(cl:Claim) WHERE cl.session_id IN $sids` — a property map cannot express list membership |
| `_SNAPSHOT_SESSION`, `_DELETE_SESSION` | map-match `(s:Session {session_id: $sid})` | `(s:Session) WHERE s.session_id IN $sids` |

`_REWRITTEN_DESCRIPTIONS` already keys off `$trace_ids` and is unchanged.

### The guard must not be widened naively — a real weakening

`_VERIFY_SESSION_BINDING` aggregates `turns`, `owned` and `matching_traces` and refuses when
`turns == 0 or owned != turns or matching != turns`. Aggregated over twenty ids, **a session id
contributing zero turns no longer fails**: the other nineteen carry the aggregate past `turns > 0`,
and that id then flows into `_DELETE_ENTITIES`, whose `e.originating_session_id IN $sids` can match
entities the run never created.

So the check becomes **per id, not aggregate**: `UNWIND $sids`, `OPTIONAL MATCH`, require one row
per requested id, and require `turns > 0`, `owned == turns` and `matching == turns` **for every id
individually**. Strictly stronger than today's single-session check, and cheap.

### The rest

- `CleanupResult.session_id: str` → `session_ids: tuple[str, ...]`.
- The snapshot stays **one file per run**. That is why the session set must be one call: the
  function opens the snapshot with `"w"` (`ground_truth.py:859`), so calling it once per session
  would truncate the undo log for the destructive step nineteen times over.
- `ProbeAnswer` gains `session_id: str`; `run_answers.json` `"session_id"` → `"session_ids"`.
- **`results.json` is wrong today and must change.** It writes the loop-invariant outer
  `session_id` into every row (`runner.py:373`), so with one session per probe every row would
  name the last probe's session. It must use each answer's own `session_id`. The first draft of
  this plan claimed it needed no change; that was wrong (Codex round 4).
- `cleanup_eval_data.py` re-resolves session ids from Elasticsearch trace ids rather than trusting
  `results.json` (`cleanup_eval_data.py:115-161`), so the compat file is **not** a guarantee of
  relational cleanup. That is Defect D's job, not the compat file's.

### Proof

| Criterion | Test |
|---|---|
| No probe is answered inside another's history | `test_each_probe_gets_its_own_session` |
| Cleanup treats the run, not the session, as the unit | `test_cleanup_scopes_to_every_probe_session` |
| An entity adopted only *within* the run is still deleted | `test_intra_run_adoption_is_not_counted_as_residue` |
| One bad session id fails the guard even among valid ones | `test_a_single_unbound_session_id_refuses_the_whole_cleanup` |
| Snapshot and delete predicates stay identical | existing delete-scope test, extended to `$sids` |
| Each compat row names its own probe's session | `test_the_compat_results_file_names_each_probes_own_session` |

---

## Defect C — one slow turn discards every completed turn

### What is wrong

`_TURN_TIMEOUT_SECONDS = 300.0` is hardcoded (`runner.py:79`). On 2026-08-04 a legitimate turn ran
five tool iterations in 318.8 seconds — eighteen seconds over. The run aborted on the exception,
wrote no artifact, and **nine completed turns produced nothing**.

Master's steer: raise the ceiling, or tolerate a single timeout without discarding the completed
work, and the second is the one that matters.

### Why tolerance alone is not enough — the single-use property

The absent probes are single-use. Asking about an absent subject creates entities and episodes
about it, so the second firing of an absent probe is answered against a corpus that now contains
the first firing. **A re-run does not repeat the measurement; it destroys it.** So preserving nine
completed turns is only useful if the retry fires the eleven that are *missing*.

### And why "no answer recorded" does not mean "safe to refire"

Codex round 4, and this is the finding that shapes the design. A client-side timeout does not
cancel the server-side turn. `/chat` writes the question into `sessions.messages` independently of
whether the client is still waiting, so **a probe that timed out may have already fired, answered
and polluted the corpus.** Refiring it on that basis destroys the ground truth just as surely as a
deliberate re-run would.

The existing idempotency store is no substitute: it activates only when the caller supplies a
session id, which a first turn by definition does not.

### The change

1. `--turn-timeout`, default `900.0`. Three times the observed legitimate maximum, and a flag so
   the ceiling moves without a code edit.
2. **A durable per-probe attempt ledger, written before the request goes out.** `run_answers.json`
   is rewritten after every probe rather than once at the end, so a crash loses nothing. Each probe
   carries a state:

   | State | Meaning | Refirable on resume |
   |---|---|---|
   | `completed` | an answer was recorded | no — it is done |
   | `not_submitted` | the request never reached the service (`httpx.ConnectError`, `ConnectTimeout`) | **yes** — the corpus is untouched |
   | `submitted_unknown` | the request went out and no usable answer came back (read timeout, HTTP status error, malformed payload) | **no** — the turn may have completed server-side |

3. Per-probe tolerance: one probe's failure is recorded and the loop continues. Exit non-zero when
   any probe did not complete.
4. `--resume`: fire only probes that are absent from the ledger or `not_submitted`. Refuse when the
   existing artifact's `manifest_digest` differs. **Report every `submitted_unknown` probe and
   refuse to guess** — the operator must inspect the corpus and decide, because the choice is
   between losing a probe and silently voiding it.
5. **Refuse to overwrite.** Without `--resume`, a `run` that finds a non-empty `run_answers.json`
   exits non-zero and does nothing.

`_validate_run_artifact` already refuses to report unless the answers cover the manifest one for
one, so a partial run cannot produce a baseline. That stays as is.

### Proof

| Criterion | Test |
|---|---|
| One failing probe does not discard the others | `test_a_failing_probe_does_not_discard_completed_answers` |
| The ledger is durable before the request goes out | `test_the_attempt_is_recorded_before_the_turn_is_fired` |
| A connect failure is refirable | `test_a_pre_submission_failure_is_refirable` |
| A read timeout is never auto-refired | `test_a_submitted_probe_is_never_refired_on_resume` |
| Resume fires only the missing probes | `test_resume_fires_only_the_missing_probes` |
| Resume refuses a foreign manifest | `test_resume_refuses_answers_from_another_manifest` |
| A re-run without `--resume` refuses | `test_a_second_run_refuses_to_overwrite_existing_answers` |
| The timeout is configurable | `test_the_turn_timeout_is_configurable` |

---

## Defect D — cleanup never touches Postgres, so the absent half can never return to zero

**Found in Codex round 4. Not on master's list, and blocking.** Folded in rather than ticketed,
because without it the run completes, the report renders, and AC-3 answers wrongly.

### What is wrong

`gather_evidence` establishes absence against **both** the graph and the message history — the
Postgres query at `ground_truth.py:492-494` over `sessions.messages`. `cleanup_probe_session` takes
only a Neo4j `driver`; it has no Postgres connection and deletes no session rows.

So the absent probe's own question text survives cleanup in `sessions.messages`, the third
evidence pass returns rows, `absent_half_restored` is **False by construction**, and AC-6 takes the
test-substrate branch every time — for a reason that is a gap in cleanup, not a property of the
substrate. This is the failure mode where the fixture works and the answer is wrong.

### The change

Extend `cleanup_probe_session` to take the `pg_conn` it already needs, under the same discipline
the Neo4j side already has:

1. Snapshot the probe sessions' rows (`SELECT * FROM sessions WHERE session_id = ANY($1) AND
   user_id = $2`) into the **same** JSONL undo log, before anything is deleted.
2. Verify ownership on the Postgres side too: every row must belong to `--user-id`, and any
   requested session id whose row is missing or foreign refuses the whole cleanup — the same
   per-id strictness as Defect B's graph guard.
3. Delete the rows only when `dry_run` is false, **after** the snapshot is fsynced and after the
   graph deletes, preserving the existing "durable before destructive" ordering.
4. `CleanupResult` gains `message_rows_removed: int`.

`sessions.messages` is a JSONB column on the session row — there is no separate messages table
(`docker/postgres/init.sql:8-15`), so deleting the session row removes the whole history in one
statement.

### Proof

| Criterion | Test |
|---|---|
| The probe sessions' rows are snapshotted before deletion | `test_the_session_rows_are_snapshotted_before_deletion` |
| A dry run deletes no rows | `test_a_dry_run_removes_no_message_rows` |
| A session row owned by another user refuses cleanup | `test_a_foreign_session_row_refuses_cleanup` |
| Cleanup removes the message history the evidence pass reads | `test_cleanup_removes_the_probe_sessions_message_rows` |

---

## Steps

1. `ground_truth.py` — widen cleanup from one session to the run's session set, with the per-id
   guard. → verify: `uv run pytest tests/evaluation/ -k fre1122 -q`
2. `ground_truth.py` — Defect D: Postgres snapshot, ownership check and delete. → verify: as above.
3. `runner.py` — session per probe; thread `session_ids` through postcheck and report; fix the
   compat rows. → verify: as above.
4. `runner.py` — `--auth-email`, the header, and the identity bind. → verify: the four Defect A
   tests.
5. `runner.py` — `--turn-timeout`, the attempt ledger, `--resume`, overwrite refusal. → verify: the
   eight Defect C tests.
6. `README.md` — correct the auth section, the session model, the resume procedure, and Defect D's
   cleanup scope.
7. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.

## Out of scope, deliberately

- **Firing the run.** Owner-authorized, and master's to run.
- **FRE-1149** (`telemetry/` is not gitignored). The first draft made a README correction
  conditional on it; Codex called that scope creep and it is also moot —
  `telemetry/evaluation/fre1122-absence-probe/` **is** ignored today (`.gitignore:225`), so the
  README's claim is already true and needs no edit.
- **FRE-1140** (window-amnesia probes). A separate ticket that this one blocks.
- **Parallel probe firing.** The session split makes it possible. It clears no defect, and it would
  make per-turn ordering in the corpus non-deterministic.
