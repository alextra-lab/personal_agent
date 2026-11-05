# FRE-1733: fix the verified defects from the ponytail audit

Ticket: FRE-1733. Source: FRE-1730 audit and review. Risk tier: Standard (cost and session write
path). Diff class: escalated (production write path, cost code).

## Facts read before this plan

- `_chat_impl` loads the session, then waits for the prior write. `expire_on_commit` is False, and a
  second `select` does not overwrite a loaded attribute. `session.messages` can be stale.
- `append_message` reads through the same identity map, so a stale list can also be written back.
- Production has 0 rows for `deployment.*`, `role_estimate.*`, `tool.*`, `effort.*` and the two
  governance maps. No stored row needs a repair.
- Only the cloud path of `LiteLLMClient.respond` holds a reservation. The local path has none.
- `litellm.register_model` cannot remove a price. See "Known limits".

## Step 1: AC-1 proof, before any fix

File: `tests/personal_agent/service/test_fre1733_session_writes_db.py` (marker `integration`, real
test Postgres).

- Drive `_chat_impl` with a real `AsyncSession`. Patch the orchestrator and the gateway pipeline.
- Patch `await_previous_session_write` with a coroutine that commits an assistant message on a
  separate session. This is the seeded write during the wait.
- Assert that the DB list keeps the seeded message, and that the orchestrator history holds it.

Command: `uv run pytest tests/personal_agent/service/test_fre1733_session_writes_db.py -m integration`

Run this on the unfixed code. Record the result on the ticket.

## Step 2: item 1 fix

File: `src/personal_agent/service/repositories/session_repository.py`

```python
await self.db.execute(
    text(
        "UPDATE sessions "
        "SET messages = COALESCE(messages, '[]'::jsonb) || CAST(:message AS jsonb), "
        "last_active_at = :now, purged_at = NULL "
        "WHERE session_id = :sid"
    ),
    {"message": json.dumps([message]), "now": datetime.now(timezone.utc), "sid": session_id},
)
await self.db.commit()
session = await self.get(session_id)
if session is not None:
    await self.db.refresh(session)
return session
```

The message goes inside a one-item array, so a message is always one new element. The read before
the write goes away. The validation of assistant fields stays.

File: `src/personal_agent/service/app.py`, `_chat_impl`: wait for the prior write before the load,
as `/chat/stream` does. A new session has no prior write. A first version kept the load first and
called `db.refresh` after the wait. `/code-review high` found that it raised on a session deleted
during the wait, and that it held a transaction across the wait.

Tests: 20 concurrent appends on separate sessions all survive, in the same file. A session that holds
a stale identity map appends without a loss. The returned session holds the new message.

## Step 3: item 2 fix

File: `src/personal_agent/llm_client/litellm_client.py`, the `except asyncio.CancelledError` branch.

- Call `gate.commit(reservation_id, reservation_amount, ...)`, not `refund`.
- Log `litellm_cancelled_call_committed_estimate` with the amount, and a note that the estimate can
  overcount a call that the provider did not bill.
- Rename the failure event to `litellm_commit_after_cancel_failed`. No dashboard or test reads the
  old name (grep result: only this file).
- The `except Exception` branch and the slot timeout branch still refund. A cancel during the slot
  wait sends nothing, and the reaper still frees it.

Test: rewrite `tests/personal_agent/llm_client/test_litellm_cancellation_refund.py`. A cancel commits
`Decimal("0.01")` and does not refund. An error refunds and does not commit. A cancel still
propagates as `CancelledError`.

## Step 4: item 3 fix

File: `src/personal_agent/cost_gate/gate.py`, `reap_stale`: add up `amount_usd` per counter id in a
dict, then update `for cid in sorted(refunds)`.

Tests in `tests/personal_agent/cost_gate/test_reaper.py`:

- A fake connection returns reservations with counters `[10, 11]`, then `[3, 11]`. The `UPDATE` order
  is 3, 10, 11, and counter 11 gets one update with the sum. This runs in `make test`.
- On test Postgres, two expired reservations that share a counter leave the right totals.

## Step 5: item 4 fix

Files: `model_loader.validate_deployment_override`, `policy.validate_role_estimate_override`,
`governance_loader.validate_governance_override` (tool branch).

Each validator keeps a sent field only when its validated value differs from the file value. The
comparison reads the validated values, not the JSON dump. The list order of a dumped `frozenset`
(`reasoning_effort_domain`) depends on the hash seed, so a dump comparison reports a false change
(self-review probe: 7 of 12 seeds).

`set_override`: for these three families, an empty result means that the row equals the file. So it
calls `clear_override` before it takes `_edit_lock` (codex plan review: the lock is not reentrant),
and returns `{}`. An empty stored object can show the row as "override" with
nothing in it (`_override_row` uses `all()`). The family check is needed, because an `AppConfig`
setting can legally store `{}`.

`_after_edit`: for `deployment.*`, call `register_model_pricing(load_model_config())` inside a `try`.
A failure logs `model_pricing_registration_failed` with `trace_id` and does not undo the edit. This
covers Set and Clear.

Tests in `tests/personal_agent/service/test_settings_edit_api.py` (fake store) and the validator test
files:

- A price edit, then `litellm.model_cost` holds the new price. After Clear it holds the file price.
- The whole row with one changed field stores one field.
- A later file change to another field takes effect (patch the file content, read the catalog).
- Sending every file field unchanged, for every deployment, tool and role estimate, stores nothing.

## Step 6: gates

`make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`, the
integration tests of this ticket on the test stack, then self-review and the PR.

## Not changed, and why

- Effort presets store every field. `_with_override` replaces a whole level, by the FRE-1723 design.
- `sub_agent_tools` and `mode_transition_rules` are whole rows (`whole=True`). A missing key has a
  meaning, so a field diff does not apply.
- `dspy_gate` settles from its own cost sink, so item 2 does not apply.

## Known limits

- A price that an edit or a Clear removes stays in `litellm.model_cost` until a restart, and so does
  the key of an old `id` or `provider`. `register_model_pricing` skips a definition with no price,
  and litellm has no remove call.
- A row that names a field equal to a later file value stays stored until the next edit. It has no
  effect.
