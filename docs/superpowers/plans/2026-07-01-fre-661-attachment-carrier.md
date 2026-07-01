# FRE-661 — Structured attachment carrier (ADR-0101 ticket 1)

Backing: ADR-0101 §2 (carrier) + §8a (`processing_target`, folded via FRE-690), ADR-0069 (artifacts).
Branch: `fre-661-attachments-carrier`.

## Goal

Thread a structured `attachments` parameter through `handle_user_request` → `ExecutionContext`,
kept separate from `ctx.user_message`. The user message stays clean original text (Captain's Log +
entity extraction read it). Service layer passes the validated structured list instead of the
augmented string; the text-pointer augmentation is retired from the orchestrator path.

## Acceptance criteria (proof of Done)

- **AC-5 (clean task description)** — for an attachment turn, `ctx.user_message` equals the original
  submitted message byte-for-byte; no artifact_id / content_type / title / r2_key / stringified block
  appears in it. Attachment metadata lives only in `ctx.attachments`.
- **AC-9 slice (processing_target threaded)** — a `processing_target` set on the inbound attachment is
  present on the `ExecutionContext.attachments` entry unchanged.

## Steps

1. **`orchestrator/types.py`** — add `Literal` import; define frozen `AttachmentRef`
   (`artifact_id, content_type, title, r2_key, processing_target: Literal["cloud","local"] | None = None`);
   add `attachments: tuple[AttachmentRef, ...] = ()` field to `ExecutionContext`.
   → verify: `make mypy` clean.

2. **`orchestrator/orchestrator.py`** — add `attachments: Sequence[AttachmentRef] | None = None` param to
   `handle_user_request`; store `attachments=tuple(attachments or ())` on the `ExecutionContext`.
   → verify: new unit test proves ctx carries clean message + attachments.

3. **`service/app.py`** — `_validate_attachments` SELECTs `r2_key`, reads inbound `processing_target`
   (validated to `{"cloud","local"}` else `None`), returns `list[AttachmentRef]`. Remove
   `_augment_message_with_attachments`; call site passes clean `message` + `attachments=` to orchestrator.
   → verify: new unit test on `_validate_attachments`.

4. **Tests**
   - `tests/personal_agent/orchestrator/test_attachment_carrier.py` (new): AttachmentRef immutability;
     ExecutionContext default empty; handle_user_request keeps `user_message` byte-for-byte clean AND
     stores attachments separately (AC-5); processing_target reaches ctx (AC-9). Mock `execute_task_safe`.
   - `tests/personal_agent/service/test_uploads_router.py`: replace the two `_augment_*` tests with
     `_validate_attachments` tests (returns AttachmentRef with r2_key + processing_target; drops bad rows).
   → verify: `make test-file` on both, then `make test`.

## Deploy-sequencing constraint (not a soft note — codex plan-review flagged this)

Retiring `_augment_message_with_attachments` means uploads become **invisible to the model** for
any turn with attachments, from this ticket landing until FRE-666 (turn-assembly content-block
injection) lands. There is no interim signal — do **not** work around this with a synthetic prefix
on `ctx.user_message`; that would violate AC-5 (byte-for-byte clean text). This is ADR-0101's
intended sequencing (ticket 1 of 4), but it is a live production regression if this PR deploys alone
and attachment uploads are in active use. The PR + Linear handoff comment must state explicitly:
**do not deploy this PR to prod in isolation — hold for the FRE-664/665/666 chain, or confirm with
the owner that uploads are not in active use before deploying standalone.** Master decides deploy
timing; this is surfaced, not decided, here.
