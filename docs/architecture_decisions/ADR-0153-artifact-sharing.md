# ADR-0153: Artifact Sharing — Owner-to-User Grants

**Status:** Proposed, amended 2026-11-21 (Amendment 1 — share links for anyone, FRE-1748)
**Date:** 2026-10-01
**Deciders:** Project owner
**Tags:** artifacts, identity, authorization, sharing, prompt-injection

---

## Context

On 2026-09-16 the owner asked: "Are artifacts shareable with other seshat users?" The answer was no.
FRE-1525 asks whether that changes, and how.

### Today's behaviour (verified on `main` at `bc6c2206`, 2026-10-01)

- **One owner per artifact.** The `artifacts` table (`docker/postgres/init.sql:404-421`) has a
  single `user_id` and no visibility, grant, or token column.
- **Every caller-facing read filters on the caller's `user_id`.** Some also filter on `type` or
  `upload_pending`. None admits a second user:

  | Caller-facing read | Location | On mismatch |
  |---|---|---|
  | `GET /internal/artifacts/{id}` (the Worker calls it) | `service/artifacts_router.py:141-240` | HTTP 404 |
  | `GET /api/v1/artifacts` (list) | `artifacts_router.py:243-290` | empty list |
  | `GET /api/v1/artifacts/{id}` (metadata) | `artifacts_router.py:293-336` | HTTP 404 |
  | `GET /api/v1/artifacts/{id}/export` (HTML only) | `artifacts_router.py:428-520` | HTTP 404 |
  | `artifact_list` tool (`type='artifact'` only) | `tools/artifact_tools.py:520-597` | empty list |
  | `artifact_read` tool | `tools/artifact_tools.py:600-735` | `ToolExecutionError` |
  | Chat attachments, first turn | `service/app.py:155-210` (`_validate_attachments`) | id dropped silently |
  | `notes_search` (pgvector) | `tools/notes_tools.py:387-484` | empty |

- **Three other reads exist. Two can serve bytes without the ownership check:**
  - **Attachment continuations.** A cloud-cost confirmation and a PDF page continuation rebuild
    `AttachmentRef` from a stored `r2_key` with no new check
    (`orchestrator/executor.py:4392-4405`, `4554-4565`). The bytes are then fetched from R2
    (`orchestrator/attachment_resolution.py:195-198`, `orchestrator/document_resolution.py:296-299`).
  - **`expand_tool_result`** passes a caller-supplied R2 key to `store.get` with only a content
    hash guard (`tools/tool_result_expand.py:75-102`). The store checks no namespace
    (`storage/artifact_store.py:230-266`). The tool is dormant: it registers only when
    `tool_result_compression_enabled` is on (`tools/__init__.py:137`, ADR-0085 Parked), and the
    live gateway leaves it off.
  - **System reads:** the joinability walk reads artifact ids by session
    (`observability/joinability/walk.py:595-620`), and `grafana_ro` can select the whole table
    (`docker/postgres/migrations/0028_grafana_ro_artifacts.sql`). Neither serves bytes to a user.
- **The only ID-addressed mutation is upload completion**, `POST /api/uploads/{artifact_id}/complete`
  (`service/uploads_router.py:223`), which filters on owner. The two `DELETE FROM artifacts`
  statements touch pending uploads only (`uploads_router.py:314`, `:402`).
- **404, not 403.** ADR-0069 D3 inherits ADR-0064 D3: an ownership mismatch returns 404, which
  hides existence. ADR-0069 Verification item 4 states the case: user A writes, user B gets 404.
- **Memory is global.** ADR-0064 D5 keeps the knowledge graph shared. Authenticated sessions write
  `group` visibility (`second_brain/consolidator.py:789-791`), and reads apply the visibility
  filter at `memory/service.py:187-212`. So user B's agent can recall a fact from the owner's
  turns, but B cannot open the artifact that the fact came from.
- **Sessions have one user.** `sessions.user_id` is a single column. The multi-participant
  session work is FRE-420, which is in Backlog and unscheduled.
- **No content from outside is fenced.** Tool output enters the model context as raw JSON
  (`orchestrator/tool_dispatch.py:256`). `artifact_read` decodes R2 bytes straight into
  `output["content"]` (`artifact_tools.py:721`). `web_fetch` has the same property.
- **The main API path trusts a plaintext header.** `get_request_user` (`service/auth.py:176`)
  takes identity from `Cf-Access-Authenticated-User-Email` at line 195, with no JWT check. The JWT
  verifier (`service/cf_access_jwt.py`) has one call site, the Worker path
  (`artifacts_router.py:190`, confirmed by FRE-1530). Reachability of this header without
  Cloudflare Access is not verified.
- **Tool approval fails open.** `check_permission` allows a tool that requires approval when there
  is no session id, when `approval_ui_enabled` is false, or when no transport is attached
  (`tools/executor.py:226-280`). No `ToolExecutionLayer` is built with a transport
  (`orchestrator/executor.py:3252`, `orchestrator/tool_dispatch.py:58`, `config/bootstrap.py:122`),
  and no code assigns one later. The live gateway sets `AGENT_APPROVAL_UI_ENABLED=true`. So a
  tool with `requires_approval: true` runs with a warning log and no prompt. This affects `bash`
  and seven MCP write tools today (`config/governance/tools.yaml`).

### Demand

The live `users` table holds 29 rows on 2026-10-01. The owner holds 105 of the 113 artifacts. One
other real user holds 8 artifacts across 11 sessions. The owner confirmed on 2026-10-01 that this
second user is the intended recipient.

### The owner's answers (2026-10-01)

1. The recipient is a real user.
2. Access control must be **per individual**: share with one person and not with another.
3. Sharing with people outside the Cloudflare Access allowlist is **not planned**. *(Reversed for
   links by the owner on 2026-11-06. See Amendment 1.)*
4. The grantee's **session and agent must be able to read** a shared artifact inside Seshat, not
   only the grantee in a browser.

The owner also confirmed five design choices, recorded as D3, D7, and three items in "Out of
scope" below.

---

## Decision

### D1 — A grant table, which is also the audit record

Add `artifact_grants`:

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PRIMARY KEY` | |
| `artifact_id` | `UUID NOT NULL` | FK `artifacts(id) ON DELETE RESTRICT` |
| `grantee_user_id` | `UUID NOT NULL` | FK `users(user_id)` |
| `granted_by` | `UUID NOT NULL` | FK `users(user_id)`. Always the artifact owner (D2) |
| `granted_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `revoked_at` | `TIMESTAMPTZ NULL` | NULL means active |
| `revoked_by` | `UUID NULL` | FK `users(user_id)` |

A partial unique index on `(artifact_id, grantee_user_id) WHERE revoked_at IS NULL` allows one
active grant per pair. A revoke sets `revoked_at` and `revoked_by`. It never deletes the row. A
later re-grant inserts a new row, so the history keeps every grant period.

`ON DELETE RESTRICT` keeps the history: an artifact with grant rows cannot be deleted. This costs
nothing today, because the only delete paths remove pending uploads, which cannot hold a grant
(D3). A future delete path for shared types must decide how to keep the history. It must not add a
cascade.

The schema change goes in `docker/postgres/init.sql` and a new file in
`docker/postgres/migrations/`, run as the `agent` superuser (FRE-808).

### D2 — Only the owner grants, and a grantee only reads

The owner is `artifacts.user_id`. Only the owner can create, revoke, or list the grants on an
artifact. A grantee cannot re-share, overwrite, or delete it.

**Check order.** Every grant-management request checks ownership first. A caller who does not own
the artifact gets the unknown-id response before any check of type, email, or self-grant. So a
non-owner cannot learn whether an id is a note, a capture, or a shareable artifact.

### D3 — Shareable types: `artifact` and `upload`

A grant can target `type IN ('artifact', 'upload')` with `upload_pending = FALSE`. `note` is
excluded. A note is a chain of revisions under one slug (`notes_tools.py:204-219`), and a grant on
one revision id does not fit that model. `capture` is excluded because it is not a user-facing
type. After the D2 ownership check, a grant request on an excluded or pending item returns 400 to
the owner.

### D4 — Grant by email, to existing users only

The owner names the grantee by email. The grant resolves the email to an existing `users` row.
It never creates a row. This settles the question of a person who never signs in: that person
cannot receive a grant until they sign in once. An unknown email returns a "no Seshat user with
that email" error.

There is no user directory to browse. The error tells the owner whether an email belongs to a
Seshat user. This is accepted, because every caller is already on the Access allowlist. A grant to
oneself returns 400.

### D5 — One read predicate, in the database, used by every read path

A Postgres function `artifact_readable_by(artifact_id UUID, user_id UUID) RETURNS boolean`
decides read access. It returns true when the artifact is not pending and one of these holds:

- the caller owns it, or
- the type is `artifact` or `upload` and an `artifact_grants` row exists for the artifact and
  the caller with `revoked_at IS NULL`.

The function is the one place the rule lives. It works from raw SQL and from SQLAlchemy
(`func.artifact_readable_by`), which covers both query styles in the read paths. These paths call
it and drop their own ownership filter:

- `GET /internal/artifacts/{id}` (browser access through the Worker)
- `GET /api/v1/artifacts/{id}` (metadata)
- `GET /api/v1/artifacts/{id}/export`
- `artifact_read`
- chat attachments on the first turn (`_validate_attachments`)
- **attachment continuations**: before a cloud-confirmation or PDF-continuation turn re-injects an
  attachment, it calls the function by `artifact_id` and drops any attachment that fails. A
  stored `r2_key` is never authority.

The list paths call the function for scopes `shared` and `all` (D6). Scope `own` keeps its owner
filter, because it lists only the caller's own rows.

Write, delete, upload-completion, and grant-management paths keep the owner-only filter.
`notes_search` keeps the owner-only filter, because notes are not shareable (D3). The system reads
(joinability walk, `grafana_ro`) do not change.

**`expand_tool_result` is confined to its namespace.** It rejects any key that does not start with
`tool-results/{session_id}/` for a session that the caller owns. It returns one error for a
rejected key, a missing object, and a hash mismatch, so it cannot confirm that a key exists. This
closes a byte path to `artifact/`, `upload/`, and `note/` keys that bypasses the function. The tool
is dormant today, so this is a condition for turning `tool_result_compression_enabled` on, built
with the read-path work because it is small.

### D6 — Discovery: a scope on the list paths

`artifact_list` and `GET /api/v1/artifacts` take a scope: `own` (the default), `shared`, or `all`.
Each row in a result appears once.

| Scope | `GET /api/v1/artifacts` (`type` filter still applies) | `artifact_list` |
|---|---|---|
| `own` | Caller's own rows of the requested type, as today | Caller's own `artifact` rows, as today |
| `shared` | Rows with an active grant to the caller. A `type` other than `artifact` or `upload` returns an empty list | Rows of type `artifact` or `upload` with an active grant to the caller |
| `all` | Union of `own` and `shared` | Union of `own` and `shared` |

Each shared row carries `shared: true` and `shared_by`. Pending uploads never appear. Revoked
grants never appear. The default stays `own`, so the existing behaviour of both paths does not
change.

The grantee's agent finds shared items with `artifact_list(scope="shared")` and reads them with
`artifact_read`. This meets the owner's answer 4.

### D7 — The agent can share, only with the owner's approval, and the tool fails closed

A new tool, `artifact_share(artifact_id, email)`, creates a grant on an artifact that the caller
owns, through the same D2 to D4 checks as the API.

- **The mechanism is an injected approval callable.** Today an executor receives only the frozen
  `TraceContext` (`tools/executor.py:453`), so a tool cannot prompt. `ToolExecutionLayer` passes
  an `approve` callable to executors that declare an `approve` parameter, the same way it passes
  `ctx` today. The callable sends one approval request through the layer's transport and returns
  the decision. When the layer has no transport or no session id, the callable is a stub that
  returns `deny`.
- **It fails closed.** The tool creates a grant only after an affirmative `approve` decision. With
  no transport, no session id, approval UI disabled, a timeout, or a denial, it creates nothing and
  returns a refusal. It does not rely on the generic `check_permission` approval branch, because
  that branch fails open (Context). Its entry in `config/governance/tools.yaml` therefore sets
  `requires_approval: false` and no `requires_approval_in_modes`, so the generic branch never sends
  a second request.
- **The prompt shows trusted values.** The tool runs the D2 to D4 checks, reads the artifact title
  from the `artifacts` row, and resolves the email to a user, all before its single approval
  request. The request shows that title and email, not only the model's arguments.
- **Dependency.** Today no `ToolExecutionLayer` is built with a transport, so the stub denies every
  call until the transport is wired into the primary tool path (FRE-1535). Until then, granting
  works through the API and the PWA only.

**Why approval:** text injected into a web page or a shared artifact can tell the agent to share
the owner's artifacts. The blast radius is limited to allowlisted users, but the owner must still
decide each grant.

Revoke is available through the API and the PWA. The agent has no revoke tool in v1.

### D8 — Existence hiding stays, and revocation is immediate

A caller who cannot read an artifact gets the same outcome as for an id that does not exist. The
outcome depends on the surface:

| Surface | Outcome for no access, revoked access, or an unknown id |
|---|---|
| Worker, metadata, export, grant management | HTTP 404 with an identical body |
| `artifact_read` | `ToolExecutionError` with an identical message |
| Chat attachments, first turn and continuations | The id is dropped, with the same log event and no different user-visible text |
| `artifact_list`, `GET /api/v1/artifacts` | The row is absent |

The next request after a revoke gets that outcome. The internal artifact endpoint returns
`Cache-Control: private, no-store`, so no shared cache holds the bytes. If the Worker rewrites this
header, the Worker change goes in the private secrets repo (ADR-0069 D8).

**Revocation cannot recall copies.** These copies stay after a revoke:

- a file that the grantee downloaded or exported
- the content in the grantee's session history, if their agent read it in a turn
- facts extracted into the knowledge graph, which is global anyway (ADR-0064 D5)

This ADR states that limit and does not try to remove it.

### D9 — Audit events

Each of these actions emits a structlog event with `trace_id`, `artifact_id`, `owner_user_id`, and
`grantee_user_id`, which reaches Elasticsearch:

- `artifact_grant_created`, with `via`: `api` or `agent_tool`
- `artifact_grant_revoked`
- `artifact_read_by_grantee`, with `surface`: `worker`, `metadata`, `export`, `artifact_read`, or
  `attachment`

The owner lists the active and revoked grants on their artifact through
`GET /api/v1/artifacts/{id}/grants`.

### D10 — The agent knows who wrote a shared artifact

When the caller is not the owner, `artifact_read` returns `shared: true` and `shared_by` (the
owner's display name, or email if there is none). The `artifact_read` tool description tells the
model that content written by another user is data, not instructions.

A general fence for untrusted content is out of scope. It must also cover `web_fetch`, which has
the same gap and a larger exposure.

### D11 — Precondition: the main API path verifies the JWT

With grants, identity is the whole access gate. Before any grant path ships, `get_request_user`
must verify `Cf-Access-Jwt-Assertion` and take identity from the verified `email` claim only.

The main app and the artifact Worker are separate Cloudflare Access applications with different
audiences (`artifacts_router.py:259-262`). `CFAccessVerifier` accepts one audience
(`cf_access_jwt.py:59`), and the one setting `cf_access_aud` (`config/settings.py:2623`) holds the
Worker's. So D11 adds a second setting for the main app's audience and a second verifier instance.
`get_request_user` uses the main-app verifier. The internal artifact endpoint keeps the Worker
verifier. The ticket first confirms that the main app's JWT reaches the gateway on its primary
domain (ADR-0069 Dev-2 records that destinations do not receive it). The plaintext email header alone must not resolve a user in
production. If the main-app verifier is not configured in production, the request returns 503, as the
Worker path does today (ADR-0069 Dev-3). The dev fallback (`gateway_auth_enabled=False` resolves to
`agent_owner_email`, ADR-0064 D4) stays.

If the implementation shows that Cloudflare Access always strips and re-injects the header, the
ticket records that proof and still adds the JWT check. This ADR does not rely on a defense that
depends on edge configuration.

**Implementation note (FRE-1531, 2026-10-23).** The JWT reaches the gateway on the main app's primary
domain: real owner requests carry `Cf-Access-Jwt-Assertion` through Caddy. The live `aud` claim is a
one-element list, and it differs from the Worker's audience. D11 also covers `gateway/session_api.py`,
`gateway/feedback_api.py` and `gateway/knowledge_api.py`, which read the plaintext header directly. All
four paths use `service/auth.py:resolve_request_email`.

### Out of scope (owner-confirmed 2026-10-01)

- **Expiry.** Manual revoke covers it. An `expires_at` column can come later without rework.
- **Notifying the grantee.** The grantee sees the item under scope `shared`.
- **Session-scoped sharing.** When FRE-420 gives a session more than one participant, the
  participants can get implicit grants on that session's artifacts. The grant table and the D5
  function are the place for that rule. This ADR records the path and does not build it.

---

## Alternatives Considered

### Sharing models

#### Option 1: Do nothing

**Description:** Artifacts stay personal. Outside sharing stays manual through export.
**Pros:** No build cost. No new access path.
**Cons:** A real second user exists, and the owner asked for sharing. The artifact wall also stays
stricter than the memory wall, which already shares facts.
**Why Rejected:** The owner wants per-individual sharing inside Seshat.

#### Option 2: A group visibility flag

**Description:** A per-artifact `visibility` column with `private` and `group`, reusing the
ADR-0064 D6 levels. `group` means every Cloudflare Access user.
**Pros:** One column. No grantee identity needed. No user picker.
**Cons:** It cannot express "this person but not that one".
**Why Rejected:** The owner requires individual access control (answer 2).

#### Option 3: Share links

**Description:** A per-artifact token that the Worker accepts in place of an identity.
**Pros:** Reaches people outside the allowlist.
**Cons:** The token is the whole gate, so existence hiding is lost and a forwarded URL works for
anyone. It removes the artifact from the identity model of ADR-0069 D3 and Dev-3. It serves the
owner's HTML to anonymous visitors from the owner's domain. It needs a Worker change in the
private secrets repo.
**Why Rejected:** Outside sharing is not planned (answer 3). Export plus a file covers the rare
case.
**Reversed (Amendment 1, 2026-11-21):** the owner reversed answer 3, so Option 3 is adopted for
links. Amendment 1 answers each con above.

#### Option 4: Session-scoped sharing only

**Description:** Every participant of a shared session sees the artifacts created in it.
**Pros:** It follows the collaborative-sessions North Star with no separate sharing concept.
**Cons:** Sessions have one `user_id`, and FRE-420 is unscheduled. It also cannot share an
artifact from a solo session.
**Why Rejected:** Blocked today. Kept as a future rule on top of D1 and D5.

#### Option 5: Grants without the JWT precondition

**Description:** Ship D1 to D10 and leave `get_request_user` on the plaintext header.
**Pros:** One ticket less.
**Cons:** A forged header reads every artifact granted to the spoofed user, not only that user's
own artifacts.
**Why Rejected:** Grants raise the value of a spoofed identity. D11 closes the gap first.

### Enforcement mechanisms for D5

#### Option 6: The predicate repeated in application code

**Description:** Each read path adds its own "owned or granted" SQL.
**Pros:** No database object.
**Cons:** The read paths use raw SQL and SQLAlchemy in different files. Eight copies drift, and one
missed copy is a bypass. Two such bypasses already exist (attachment continuations, live, and
`expand_tool_result`, dormant).
**Why Rejected:** D5 needs one definition that both query styles can call.

#### Option 7: Postgres row-level security

**Description:** An RLS policy on `artifacts`, with the caller id set per request through a session
variable.
**Pros:** The database enforces the rule even for a query that forgets it.
**Cons:** The gateway shares pooled asyncpg connections, so a per-request session variable can leak
to the next request if one reset is missed. System reads (joinability walk, `grafana_ro`,
consolidation) need a bypass role or policy. Owner-only paths need a second policy. The failure is
silent: a wrong variable returns another user's rows with no error.
**Why Rejected:** The risk moves from a missed filter to a missed reset, which is harder to see. The
D5 function plus AC-11 gives one definition without per-connection state. RLS can come later if
the read paths grow.

---

## Consequences

### Positive Consequences

- The owner can share one artifact with one person, and revoke it.
- The grantee's agent can find and read shared artifacts in a turn, with provenance.
- The grant table is a complete record of who shared what with whom.
- The main API path gains JWT verification, which protects the owner-only paths too.
- Two bypasses close: attachment continuations (live) and `expand_tool_result` (dormant).

### Negative Consequences

- Every read path calls a function that may query `artifact_grants`.
- A new PWA surface (share dialog, access list, shared items) needs build and maintenance.
- An owner can learn whether an email belongs to a Seshat user (D4).
- Revocation cannot recall a downloaded file, session history, or knowledge-graph facts (D8).
- `artifact_share` refuses every call until the approval channel reaches the tool layer (D7).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A read path keeps its own owner filter, or a new one skips the function | Medium | D5 function. AC-11 flags any `artifacts` query with neither the function nor an exemption marker |
| A persisted attachment serves bytes after a revoke | Medium | D5 continuation check. AC-3 covers continuations |
| Injected text makes the agent share an artifact | Medium | D7 fail-closed approval with trusted values. AC-6 seeds the negative |
| A shared artifact carries instructions to the grantee's agent | Low | D10 provenance and a tool-description rule. The sharer is an allowlisted user |
| A spoofed identity reads granted artifacts | Medium | D11 precondition. AC-8 |
| A cache serves bytes after a revoke | Low | D8 `Cache-Control: private, no-store`. AC-3 |

---

## Implementation Notes

- **Order:** D11 first. Then D1, D5, D6, D8, and D10 (schema and read paths). Then D2, D3, D4, and
  D9 (grant API and audit). Then the PWA. D7 comes last and depends on the approval channel.
- **Files:** `service/auth.py`, `service/artifacts_router.py`, `service/app.py`,
  `orchestrator/executor.py` (continuations), `tools/artifact_tools.py`,
  `tools/tool_result_expand.py`, `config/governance/tools.yaml`, `docker/postgres/init.sql`, a new
  migration, `seshat-pwa/src/components/ArtifactCard.tsx` and the artifact list surface.
- **D11 and FRE-1530** both touch the JWT code path. The D11 ticket follows FRE-1530.
- **The approval fail-open** in `check_permission` affects `bash` and seven MCP write tools today.
  D7 does not depend on fixing it, because `artifact_share` enforces its own approval. Fixing the
  generic branch is separate work: FRE-1535, filed outside this chain on the owner's choice.
- **Testing:** the test stack (`tests/CLAUDE.md`, FRE-375) with three `users` rows. Live
  verification uses the owner and the real second user, with the owner's OK.

---

## Verification / Acceptance Criteria

Adjudicated on the umbrella ticket FRE-1525, after the implementation chain lands and deploys.

**Fixtures.** User A owns HTML artifact X, text-tier PDF upload Y, vision-tier PDF upload V,
which is over the page budget so the resolver offers a continuation, and image upload W. A also
owns note N, capture K, and pending upload P. B owns pending upload Q. User B holds active grants
on X, Y, V, and W. User C holds no grant.
"Unknown id" means a random UUID.

- **AC-1 — A grant opens every read surface to the grantee.** For B: the Worker URL returns X's
  bytes. Metadata returns X and Y. Export returns X's HTML. `artifact_read` returns X's content
  with `shared: true` and `shared_by` equal to A's display name. A chat turn with Y attached
  produces a text content block with Y's extracted text. A chat turn with V attached produces V's
  first pages and a continuation offer, and the continuation turn re-injects V's next pages. · **Check:** integration test on the test stack, plus one post-deploy browser and turn
  check by B with the owner's OK. · *Fails if* any listed surface withholds X, Y, or V from B,
  V's continuation is missing or injects nothing, or `shared_by` is absent or names B.
- **AC-2 — A non-grantee cannot tell that X exists.** For C, each D8 surface gives exactly the
  outcome it gives for an unknown id: identical HTTP status and body, identical tool error message,
  identical attachment drop and log event, identical absence from lists. · **Check:** the same
  test, comparing C's outcome for X and Y with the outcome for an unknown id, surface by surface. ·
  *Fails if* any surface differs in status, body, message, log event, or list content.
- **AC-3 — A revoke takes effect on the next request, including continuations.** After A revokes
  B's grants, B's next request on every D8 surface gives the unknown-id outcome. A PDF continuation
  for V and a cloud-confirmation turn for W, both persisted before the revoke, inject nothing. W is
  attached under a cloud-priced primary model with the confirmation threshold set so that the cost
  gate fires. The
  internal endpoint response carries `Cache-Control: private, no-store`, and after deploy the
  response from the public Worker URL carries it too. · **Check:** test: grant,
  read, persist a continuation, revoke, then read and continue with no wait, plus one post-deploy
  header check on the Worker URL. · *Fails if* any surface or continuation serves X, Y, V, or W to B after the revoke, or either response lacks the header.
- **AC-4 — A grantee only reads.** B's requests to create, revoke, or list grants on X, and B's
  `POST /api/uploads/{Y}/complete`, give the unknown-id outcome. X's and Y's rows and R2 bytes are
  unchanged afterwards. · **Check:** test comparing rows and R2 hashes before and after B's calls.
  · *Fails if* B creates a grant, changes a row or bytes, or gets an outcome that differs from the
  unknown-id one.
- **AC-5 — Only shareable types can be shared, and the check order hides existence.** A's grant
  on Y succeeds. A's grants on N, K, and P return 400 and create no row. B's and C's grant requests
  on N, K, and P give the unknown-id 404. B's grant requests on Y, naming an unknown email and
  naming B's own email, also give the unknown-id 404. B's `notes_search` never returns N. · **Check:** test, with N's
  embedding matching B's query. · *Fails if* a grant row exists for N, K, or P, a non-owner gets 400 or an email-specific error,
  or N appears in B's results.
- **AC-6 — The agent cannot share without an affirmative approval.** `artifact_share` creates no
  grant row with no transport, with approval UI disabled, on timeout, or on denial. With approval,
  it creates one grant row and one `artifact_grant_created` event with `via: agent_tool`. The prompt shows X's title from the database and the
  resolved email, even when the model passes a false title in its text. Seeded negative: a fixture
  page that instructs the agent to share X with C produces no row unless A approves. · **Check:**
  test on the tool path for each case, with the injected `approve` callable returning each
  decision and with no transport, counting `artifact_grants` rows, counting approval requests, and
  querying the test Elasticsearch for `artifact_grant_created`. · *Fails if* a row exists without
  an `approve` decision, the prompt omits the database title or the email, a transport case sends
  more or fewer than one approval request, or the approved case lacks exactly one
  `artifact_grant_created` event with `via: agent_tool` and every D9 field.
- **AC-7 — The audit record is complete.** For the sequence grant X and Y to B, B reads X through
  the Worker, metadata, export, and `artifact_read`, B attaches Y in a chat turn, A revokes X, A
  re-grants X: `artifact_grants` holds two rows for (X, B), both with `granted_by` equal to A, the
  first with `revoked_at` set and `revoked_by` equal to A, and the second active. Elasticsearch
  holds three `artifact_grant_created` events with `via: api`, one `artifact_grant_revoked`, and
  five `artifact_read_by_grantee` events, one each with `surface` values `worker`, `metadata`,
  `export`, `artifact_read`, and `attachment`. Every event
  carries `trace_id` and both user ids. `GET /api/v1/artifacts/{X}/grants` by A lists both rows. ·
  **Check:** test on the test stack, querying the test Elasticsearch on `:9201`. · *Fails if* any
  event or field is missing or duplicated, or the list omits a row.
- **AC-8 — A header alone does not resolve a user.** With `gateway_auth_enabled=True`, a request to
  `GET /api/v1/artifacts/{id}` with a valid email header and no JWT returns 401. With a JWT signed
  by a foreign key, it returns 401. With the main-app verifier unconfigured, it returns 503. A valid
  Worker-audience JWT returns 401 on the main API, and a valid main-app JWT returns 404 on the
  internal artifact endpoint. A valid main-app JWT on the main API returns 200. · **Check:** test
  for each case. · *Fails if* any rejection case returns 200 or artifact data, or the valid
  main-app case fails.
- **AC-9 — A grant never creates a user, and grants to oneself fail.** A grant to an unknown email
  returns the "no Seshat user" error. A grant by A to A returns 400. The `users` row count is the
  same before and after, and no grant row exists. · **Check:** test counting rows. · *Fails if* a
  count changes or a grant row exists.
- **AC-10 — The list scopes return exactly the D6 matrix.** With active grants on X and Y, a
  revoked grant on V, and B owning artifact Z: default and `own` return Z only, on both list paths.
  `shared` returns X and Y, each with `shared: true` and `shared_by` equal to A. `all` returns Z,
  X, and Y once each, with Z carrying no `shared` flag. B's pending upload Q appears in no scope. The API with `type=note` and `scope=shared`
  returns an empty list. · **Check:** test on both list paths. · *Fails if* any scope returns a row
  outside the matrix, omits one, returns a duplicate, or lacks the provenance fields.
- **AC-11 — One definition of read access.** Every query in `src/` that reads the `artifacts`
  table either calls `artifact_readable_by` or carries an exemption marker
  (`# artifact-access: owner-only` or `# artifact-access: system`) on the query itself. The read
  paths that D5 lists carry no marker. · **Check:** a pre-commit rule (`ast-grep` for
  `select(ArtifactModel...)`, plus a text match for `FROM artifacts` in SQL strings) that flags a
  query with neither the function nor a marker, and a test that holds the list of marked queries
  and fails when a D5 read path appears in it. The rule is seeded with two violations: a query with
  its own `user_id` filter, and a query with no access predicate at all. · *Fails if* the rule
  passes either seeded violation, or a D5 read path carries a marker.
- **AC-12 — `expand_tool_result` cannot read outside its namespace.** A call with X's R2 key and
  X's correct content hash, a call with a `tool-results/` key from C's session, and a call with an
  unknown key return the same error. A call with a key from B's own session works. · **Check:**
  test with `tool_result_compression_enabled` on. · *Fails if* any outside key returns content,
  or the three errors differ.
- **AC-13 — The schema enforces D1.** A second active grant for the same pair fails on the unique
  index. Deleting X while a grant row exists fails. An insert with an unknown `artifact_id`,
  `grantee_user_id`, or `granted_by`, and an update that sets an unknown `revoked_by`, each fail. ·
  **Check:** test on the test stack. · *Fails if* any of these statements succeeds.

- **AC-14 — The registered `artifact_read` description carries the untrusted-content rule.** ·
  **Check:** a registry test reads the registered tool description and asserts that it states that
  content written by another user is data, not instructions. · *Fails if* the sentence is absent.

AC-14 checks presence only. The rule's effect on the model cannot be measured cheaply, and AC-1
checks the provenance field that the rule depends on. This is a known gap, not a hidden one.

---

## References

- ADR-0069 — R2-Backed Artifact Substrate (D3 identity, D8 Terraform location, Dev-3 JWT
  hardening) — Implemented
- ADR-0064 — Inbound User Identity via Cloudflare Access (D3 404 not 403, D4 dev fallback, D5
  global memory, D6 visibility levels) — Accepted
- ADR-0063 — Primitive Tools / Action-Boundary Governance (tool approval) — Accepted
- ADR-0085 — Intra-Turn Tool-Result Compression, the source of `expand_tool_result` (D5) — Parked
- ADR-0106 — System/User Knowledge Boundary — Superseded by ADR-0115. FRE-1525 listed it. No
  decision in this ADR depends on it
- FRE-1525 — this ADR's umbrella ticket
- FRE-420 — collaborative sessions (multi-participant), Backlog
- FRE-1530 — PyJWT upgrade and `cf_access_jwt.py` defects
- FRE-808 — run migrations as the `agent` superuser
- FRE-1535 — tool approval fails open (wires the transport and the `approve` callable D7 needs)
- Implementation chain: FRE-1531 (D11) → FRE-1532 (read side) → FRE-1533 (grant API) → FRE-1534
  (PWA) and FRE-1536 (D7, also blocked by FRE-1535)
- `src/personal_agent/service/artifacts_router.py` · `src/personal_agent/tools/artifact_tools.py`
  · `src/personal_agent/tools/notes_tools.py` · `src/personal_agent/service/auth.py` ·
  `src/personal_agent/tools/executor.py` · `src/personal_agent/tools/tool_result_expand.py`

---

## Amendment 1 — share links for anyone (FRE-1748)

**Status:** Proposed, 2026-11-21. The owner decided eight items on 2026-11-21 (FRE-1748, "Agree" to
all eight of master's recommendations). L1 to L11 below record them, with the design to build. This
amendment adds share links. It does not change D1 to D11, and it does not cancel the grant tickets
(A1.5).

### A1.1 Context

**The owner reverses answer 3.** On 2026-11-06 the owner told cc-explore: "shareable artifacts.
Packaged and usable by others. I should be able to send via email or sms." Asked who can open a
shared artifact, the owner chose "Anyone with the link". Answer 3 of 2026-10-01 ("Sharing with
people outside the Cloudflare Access allowlist is not planned") no longer holds for links.

**Option 3 moves from rejected to adopted, for links only.** Option 3 was rejected for one reason:
outside sharing was not planned. That reason is gone. Its cons stay true, and this amendment
answers each one:

| Option 3 con | Answer in this amendment |
|---|---|
| The token is the whole gate, and a forwarded URL works for anyone | Accepted and stated as the exposure (A1.2). Expiry is mandatory (L4). Revoke is immediate (L6) |
| Existence hiding is lost | A guessed token gets the same 404 as a malformed one. Only a holder of a real link sees 410 (L8) |
| It removes the artifact from the identity model of ADR-0069 D3 and Dev-3 | Only the share hostname leaves that model. The artifact and agent hostnames keep Access and the JWT check (L2) |
| It serves the owner's HTML to anonymous visitors from the owner's domain | The page runs in the ADR-0089 sealed box: an opaque origin with no network. The share hostname serves nothing else (L5, L7) |
| It needs a Worker change in the private secrets repo | Accepted. Ticket (b) makes it, on the owner's go (L2) |

Grants (D1 to D11) and links solve different problems. A grant lets a Seshat user and that user's
agent read an artifact inside Seshat. A link lets a person with no Seshat account open one
artifact in a browser. The owner keeps both: links ship first, and the grants are decided later.

**Terms used below.**

- **Link:** one row of `artifact_share_links` and its URL. A link opens exactly one artifact.
- **Token:** the random secret in the link URL.
- **Recipient:** anyone who opens a link. A recipient has no Seshat identity.
- **Active link:** a link with `revoked_at IS NULL` and `expires_at > now()`.
- **Share hostname:** the new public hostname that serves links. This ADR writes it as
  `share.example.com`. The real name lives only in the private secrets repo and in `.env`.
- **The owner:** the deployment owner. Two checks decide it together. `get_request_user` verifies
  the main-app JWT and takes the email from it (D11, `service/auth.py`). Then
  `settings_edit.is_owner` (`service/settings_edit.py:113`) requires `gateway_auth_enabled` and an
  email equal to `agent_owner_email`. A dev-mode identity never counts.

**Today's behaviour that this amendment builds on** (verified on `main` at `fe06f962`, 2026-11-21):

- The Worker reaches the gateway over the API route, which sends every path to the gateway and
  writes a JSON access log. That route sets `X-Forwarded-For` from `CF-Connecting-IP`
  (`config/cloud-sim/Caddyfile:203-231`).
- `GET /internal/artifacts/{id}` checks `X-Internal-Token` and then a Worker-audience JWT
  (`service/artifacts_router.py:141-235`). It has no path for a caller without a JWT.
- Generated HTML loads the curated toolkit from absolute URLs on the artifact host, for example
  `https://artifacts.example.com/lib/katex@0.18.2/katex.min.js` (`tools/artifact_tools.py:1214-1234`).
  That host is behind Access, so a recipient's browser cannot load these files.
- The export transform solves that problem for static references. Its `inline` mode fetches the
  pinned `/lib/` bytes, checks them against the pinned SRI hashes, and inlines them into one file
  (`storage/artifact_export.py`, ADR-0089 Addendum A5). It finds only `<script src>`, `<link href>`,
  and CSS `url()` references (`storage/artifact_export.py:220`). A toolkit URL that page script
  builds at run time stays unchanged.
- No API changes a completed HTML artifact. The only `UPDATE artifacts` statement in `src/`
  completes an upload: it sets `upload_pending` and `size_bytes` (`service/uploads_router.py:323-330`).
  One internal path can rewrite the R2 bytes of an id: a retry with a preallocated id writes R2
  before its `ON CONFLICT (id) DO NOTHING` insert (`tools/artifact_tools.py:545-590`).
- Uploads accept PNG, JPEG, GIF, WebP, SVG, PDF, plain text, Markdown, CSV and JSON
  (`service/uploads_router.py:45-58`). Only the image and PDF types are shareable by link (L5).
- Tool approval no longer fails open by default. FRE-1535 (ADR-0063 Amendment A) and ADR-0156 D8
  (ADR-0063 Amendment B) deny a call that needs approval when no approval surface exists. The
  Context bullet "Tool approval fails open" above records the state on 2026-10-01.

### A1.2 Exposure, in plain words (FRE-1748 AC-2)

**Anyone who gets the link can open the artifact, until the link expires or the owner revokes
it. A forwarded link works for anyone.** Cloudflare Access does not protect a link, and Seshat
cannot tell who opened it. A recipient can download, copy, or screenshot what they open, and a
revoke cannot recall that copy. Message and mail apps that fetch a link for a preview or a security
scan also open it. Cloudflare sees each request at its edge, as it does for every hostname of the
zone. Seshat stores no IP address (L11).

### A1.3 Decisions

#### L1 — Scope: links first, as a separate small piece

Links ship as their own chain of three tickets (A1.6). They do not depend on the grant tickets,
and the grant tickets do not depend on them, except for the one PWA overlap in A1.5. D11 (FRE-1531,
Done) is the only precondition, because the owner-only API (L9) needs a verified identity.

#### L2 — A separate share hostname with an Access bypass, served by the artifact Worker

- **Hostname.** A new hostname, `share.example.com`, with its own DNS record and a new route on
  the existing artifact Worker. The Worker branches on the hostname. On the share hostname it
  serves only `GET /s/{token}` (L6). Every other path on the share hostname returns the 404 page.
  The artifact hostname gets no `/s/` path.
- **Access.** A new Cloudflare Access application whose primary domain is the share hostname,
  with one policy: decision `bypass`, include `everyone`. The bypass makes the public state an
  explicit, reviewed line of Terraform. The share hostname is not added to the `destinations` of
  another application. ADR-0069 Dev-2 requires a hostname that needs the JWT to be the primary
  domain of its own application. The share hostname needs no JWT, and the bypass application keeps
  it apart from the applications that do.
- **No change to the other hostnames.** The artifact hostname and the agent hostname keep their
  Access applications, their policies, and the JWT checks of ADR-0069 Dev-3 and D11.
- **Rate limit.** The Terraform change adds one Cloudflare rate-limiting rule on the share
  hostname, so a token scan cannot load the VPS. Its threshold lives in the private repo.
- **Where the change lives.** All of it is Terraform and Worker code in the private secrets repo
  (ADR-0069 D8), in one change named `share-links`: the DNS record, the Worker route, the bypass
  Access application, the rate-limiting rule, and the Worker code for the share hostname. Master
  applies it only on the owner's go. This public repo holds no real hostname, account id, or
  application id (`scripts/check_no_personal_paths.py` and ADR-0069 D8).

#### L3 — The token: 32 random bytes, stored only as a hash

- **Generation.** The gateway draws 32 bytes from the operating system's secure random source
  (`secrets.token_bytes(32)`) and encodes them as unpadded base64url: 43 characters.
- **URL.** `https://share.example.com/s/{token}`. The base comes from a new setting,
  `share_public_base_url` (`AGENT_SHARE_PUBLIC_BASE_URL`), with no default. If it is unset, the
  create endpoint returns 503.
- **Storage.** Postgres stores only the SHA-256 digest of the 32 raw bytes, in `token_hash`. A
  plain SHA-256 is enough: a 256-bit random secret cannot be found from its hash, and a slow hash
  adds CPU work on the VPS for every anonymous request.
- **Shown once.** The create response returns the URL once. The gateway keeps no copy, and the
  list endpoint never returns a URL, a token, or a hash. To send a link again, the owner creates a
  new link.
- **Never logged.** No log event, span, or error message holds the token or its hash. Events name
  the link by `link_id`. The Worker does not log the request path. Ticket (b) confirms that no
  persisted Cloudflare Worker log holds the path, or turns that log off.

#### L4 — The `artifact_share_links` table

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PRIMARY KEY` | The `link_id` |
| `artifact_id` | `UUID NOT NULL` | FK `artifacts(id) ON DELETE RESTRICT`, as D1 |
| `created_by` | `UUID NOT NULL` | FK `users(user_id)`. Always the owner (L9) |
| `token_hash` | `BYTEA NOT NULL UNIQUE` | SHA-256 of the token. `CHECK (octet_length(token_hash) = 32)` |
| `snapshot_r2_key` | `TEXT NULL UNIQUE` | R2 key of the HTML snapshot (L5). NULL for an upload |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `expires_at` | `TIMESTAMPTZ NOT NULL` | No link without an expiry |
| `revoked_at` | `TIMESTAMPTZ NULL` | NULL means not revoked |
| `view_count` | `BIGINT NOT NULL DEFAULT 0` | `CHECK (view_count >= 0)` |
| `last_viewed_at` | `TIMESTAMPTZ NULL` | Time of the last served open |

- **Expiry.** A table check holds `expires_at > created_at AND expires_at <= created_at + INTERVAL
  '30 days'`. The API accepts `expires_in_days` of 1, 7, or 30, and 7 is the default. The INSERT
  computes `expires_at` in SQL as `now() + make_interval(days => :days)`, so the check holds
  exactly.
- **No IP.** The table has no column for an IP address, a user agent, a referrer, or a location.
- **Indexes.** The unique index on `token_hash` serves the open path. An index on
  `(created_by, created_at DESC)` serves the list. An index on `artifact_id` serves the foreign key.
- **No delete.** A revoke sets `revoked_at`. No path deletes a row, so the table is the record of
  every link.
- **Roles.** `seshat_app` gets its grants from the existing `ALTER DEFAULT PRIVILEGES FOR ROLE
  agent`. `grafana_ro` and `seshat_dev_broker` get no grant on this table.
- **Migration.** The change goes in `docker/postgres/init.sql` and a new file in
  `docker/postgres/migrations/`, with the next free number when the ticket is built. It runs as the
  `agent` superuser through `AGENT_DATABASE_ADMIN_URL`, not the app's `AGENT_DATABASE_URL`
  (FRE-808).

#### L5 — What a link opens: one artifact, read-only, in today's sandbox

- **Shareable items.** A row with `type = 'artifact'` and a `text/html` content type, and a row with
  `type = 'upload'`, `upload_pending = FALSE`, and one of the upload types (PNG, JPEG, GIF, WebP,
  SVG, PDF). Notes, captures, non-HTML artifacts, pending uploads, and text, Markdown, CSV and
  JSON uploads are not shareable. The owner's decision 5 names HTML artifacts, PDF, and images.
- **HTML: a snapshot made at creation.** The create endpoint runs the existing export transform in
  `inline` mode on the artifact. It writes the result to R2 at `share/{owner_user_id}/{link_id}.html`
  and stores that key in `snapshot_r2_key`. The snapshot holds the artifact's bytes at link
  creation, and nothing writes it after that, so a link always serves the same bytes.
- **Self-contained check.** After the export, the create endpoint searches the snapshot for the
  artifact host origin (`artifacts_public_base_url`). If the origin remains anywhere, for example
  in a toolkit URL that script builds, the endpoint returns 422 with "this artifact loads files
  that a link cannot carry", and writes nothing. So every snapshot that a link serves needs nothing
  from the artifact host, and it renders for a recipient. This check decides only whether a link
  works. It is not a security check, so ADR-0089 D1 still holds: the CSP is the boundary.
- **Failure cleanup.** If the export fails, the create endpoint returns 502 and writes nothing. If
  the INSERT or its commit fails after the R2 write, the endpoint deletes the snapshot object
  before it returns the error. So a failed create leaves no row and no R2 object.
- **Uploads: the original bytes.** The Worker serves the artifact's own `r2_key`. No snapshot is
  made.
- **Nothing else in Seshat.** The share hostname serves only `/s/{token}`. It serves no artifact by
  id, no `/lib/` file, and no API. A page cannot reach the network (`connect-src 'none'`, L7). A
  part of an HTML artifact that points to another URL on the artifact host does not load for a
  recipient. This is correct, because a link opens exactly one artifact.

#### L6 — The open path: the Worker asks the gateway on every request

1. **The Worker checks the request shape.** On the share hostname, the Worker accepts only `GET`
   with a path that matches `^/s/[A-Za-z0-9_-]{43}$`. Another method returns 405. Another path
   returns the 404 page. Neither calls the gateway.
2. **The Worker calls the gateway.** It sends `POST /internal/share-links/resolve` over the same
   route that it uses today for `/internal/artifacts/{id}`. The request carries `X-Internal-Token`
   (the existing `artifact_resolve_internal_token`) and the JSON body `{"token": "<token>"}`. The
   Worker builds a new header set. It forwards no header of the recipient's request: no
   `CF-Connecting-IP`, `X-Forwarded-For`, `User-Agent`, `Cookie`, or `Referer`. The token travels in
   the body, so it is in no URL or header that Caddy logs.
3. **The gateway checks and counts in one statement.** A view is a resolved open (L11). It checks `X-Internal-Token` (401 on
   failure). It takes no JWT and does not call `get_request_user`. It decodes the token and hashes
   the 32 bytes, then runs one `UPDATE artifact_share_links ... SET view_count = view_count + 1,
   last_viewed_at = now() ... RETURNING ...`. The statement matches only an active link whose
   artifact is not pending. A revoke that commits before this statement wins, so the next open
   after a revoke fails.
4. **The gateway answers.** On a match: 200 with the R2 key (the snapshot key, or the artifact's
   key for an upload), the content type, and the safe filename (L7), with
   `Cache-Control: private, no-store`. With no match, a second lookup by `token_hash` decides the
   status: 410 if a row exists, 404 if not.
5. **The Worker serves.** On 200, it reads the R2 object through its R2 binding and returns it
   with the L7 headers. It takes the size from the R2 object, because a snapshot is larger than
   the artifact's `size_bytes`. On 404 or 410, it returns the fixed page for that status (L8). On any other
   answer, a timeout, or a missing R2 object, it returns the 503 page. The Worker serves bytes only
   after a 200 from the gateway in the same request. It does not use the Cloudflare Cache API on
   the share hostname.

**Why the gateway and not a direct database read from the Worker:** the access rule lives in one
place, next to D5. Postgres stays unreachable from Cloudflare, and the Worker holds no database
credential. The check and the count stay in one statement.

**Why the existing internal token and not a new secret:** the share token is the authority. The
internal token only filters callers that are not the Worker. A caller with a valid share token can
already open the link through the public URL, so a second secret adds rotation work and no
protection.

#### L7 — Response headers and Content-Disposition

**On every response of the share hostname** (200, 404, 405, 410, 503):

- `X-Robots-Tag: noindex, nofollow`
- `Referrer-Policy: no-referrer`
- `Cache-Control: private, no-store`
- `X-Content-Type-Options: nosniff`
- no `Set-Cookie` from the Worker. If a zone feature adds a cookie at the edge, ticket (b) records
  which one, and the owner decides.

**Content-Security-Policy, by kind:**

- **HTML snapshot, SVG upload, and the fixed error pages** get the **share sandbox CSP**. It is the
  ADR-0089 D2 policy with two changes, and both make it stricter. The host-sources go, because the
  snapshot is self-contained. `frame-ancestors` becomes `'none'`, because nothing embeds a link.

  ```
  default-src 'none';
  script-src 'unsafe-inline';
  style-src 'unsafe-inline';
  img-src data:;
  font-src data:;
  connect-src 'none';
  worker-src 'none';
  form-action 'none';
  base-uri 'none';
  frame-ancestors 'none';
  webrtc 'block';
  sandbox allow-scripts;
  ```

  `sandbox allow-scripts` without `allow-same-origin` gives the page an opaque origin, as on the
  artifact host (ADR-0089 D2 tier 1). The ADR-0089 D2 residuals (self-navigation, and WebRTC on a
  browser without `webrtc` support) stay.

  **The token is visible to the page.** Page script can read `location.href`, which holds the
  token, and send it out through a residual channel. The token opens only this one snapshot, which
  the script already holds, and it gives no management operation. It keeps the same expiry and
  revoke. So a leaked token gives a third party these things, and only these: repeated opens of the
  same content on the share hostname, its `Content-Disposition` filename, more `view_count` and
  events, and load on the open path, which the rate limit (L2) bounds. The ADR-0089 bound holds in
  this form: a page can leak only its own content, and a token with that bounded use.
  `Referrer-Policy: no-referrer` stops the passive leak through a followed link.
- **PNG, JPEG, GIF, WebP, and PDF** get `default-src 'none'; frame-ancestors 'none'; form-action
  'none'; base-uri 'none'`, with no `sandbox` directive. Chromium does not show a PDF in its
  built-in viewer inside a sandboxed document. These types run no page script, and SVG, the one
  script-capable upload type, gets the sandbox CSP above. Ticket (b) confirms that each type renders
  in Chromium, Firefox, and Safari on a real iOS device with this policy. If one does not, ticket (b) records the
  smallest change and the reason here, as an implementation note.

**Content-Type** comes from the `artifacts` row, which holds an allowlisted type. The snapshot is
`text/html; charset=utf-8`.

**Content-Disposition** is `inline; filename="<name>"` for every kind. The gateway builds `<name>`
from the slug, else the title. It keeps only `[A-Za-z0-9._-]`, cuts the result to 80 characters,
and adds the extension of the content type. An empty result becomes `artifact.<ext>`. No raw user
text reaches a header.

#### L8 — What a recipient sees: 404 or 410

| Case | Status | Body |
|---|---|---|
| Any path other than `/s/{token}`, or a malformed token | 404 | The one "not found" page |
| A well-formed token with no row | 404 | The same page |
| An expired link | 410 | The one "this link is no longer active" page |
| A revoked link | 410 | The same page |
| A gateway or R2 failure | 503 | The one "try again later" page |

- **Existence hiding where it matters.** A guessed token is an unknown token, so a scan gets only
  404, the same as a malformed path. Only a holder of a real link can get 410, and that holder
  already knows that the link existed.
- **Expired and revoked look the same.** A recipient does not learn that the owner withdrew a link.
- **No page names anything.** No 404, 410, or 503 page or header holds the artifact id, title,
  owner, or creation time.
- **The artifact host does not change.** It keeps 404 for no access (ADR-0069 D3, D8 above).

#### L9 — Only the owner creates, lists, and revokes links, from the PWA

**Endpoints.** All three use `get_request_user` (D11, verified main-app JWT) and then
`settings_edit.is_owner`. A caller who is not the owner gets 403 before any lookup, so a 403
reveals nothing about an id.

| Endpoint | Behaviour |
|---|---|
| `POST /api/v1/artifacts/{artifact_id}/share-links`, body `{"expires_in_days": 1 \| 7 \| 30}` (default 7) | Checks in this order: the owner (403), then an artifact that the caller owns and that exists (404, the same as an unknown id), then a shareable item (400), then the setting (503). Then it makes the snapshot (502 on failure, 422 when the snapshot is not self-contained, L5), inserts the row, and returns 201 with `link_id`, `url`, and `expires_at`. Any other `expires_in_days` returns 422 |
| `GET /api/v1/share-links` | The owner's active links, newest first: `link_id`, `artifact_id`, `artifact_title`, `created_at`, `expires_at`, `view_count`, `last_viewed_at`. No URL, token, or hash |
| `DELETE /api/v1/share-links/{link_id}` | Sets `revoked_at = now()` on the owner's link and returns 204. A second revoke returns 204 and keeps the first time. An unknown id returns 404 |

The artifact list and metadata responses (`GET /api/v1/artifacts`, `GET /api/v1/artifacts/{id}`)
gain `can_share_link`. It is true only when the caller is the owner and the item is shareable
(L5). The PWA shows Share only when it is true.

**Other allowlisted users** cannot create links in this version. The owner's decision 6 reads "Only
the owner makes a link". This amendment reads "owner" as the deployment owner, because a link
publishes to anyone and the decision is the owner's alone.

**The PWA.**

- A **Share** button on an HTML artifact or an upload with `can_share_link`. It opens a sheet with
  the expiry choice (1, 7, or 30 days, default 7), the exposure sentence of A1.2, and **Create
  link**.
- After the create call, the sheet shows the link and **Send**. Send calls `navigator.share({url})`,
  which opens the phone's share sheet for SMS, mail, or any app. Send is a second tap, because a
  browser allows `navigator.share` only inside a user gesture, and the create call ends the first
  one. Without the Web Share API, the sheet shows **Copy link**.
- A **Shared links** view lists the active links with the artifact title, expiry, view count, and
  last view time, and a **Revoke** button on each row.
- The service worker `CACHE_NAME` is bumped, per the PWA convention.

#### L10 — The agent cannot create links

No tool, skill, MCP server, or sub-agent gets a path that creates, lists, or revokes a link. The
only writer of `artifact_share_links` is the share-links router module (the L9 endpoints and the
L6 resolve endpoint), and the create endpoint requires the owner's verified main-app JWT, which no
tool holds. `fetch_url` sends only GET (`tools/fetch.py:863`). A `bash` request to the endpoint
gets 401 from D11.

Why:

1. **The owner decided it** (decision 6): "The agent cannot make links in the first version (tool
   approval can fail open, ADR-0153 D7)."
2. **A link has no bound on its audience.** D7 accepts an agent grant behind approval because a
   grant reaches only an allowlisted user. Text injected into a page can tell the agent to share an
   artifact, and a link sends it to anyone, with no recall (A1.2).
3. **Approval is a configurable gate.** FRE-1535 and ADR-0063 Amendments A and B now deny a call
   with no approval surface. But an `allow` rule (ADR-0156 D8) runs a call with no prompt, so the
   protection depends on configuration, not on the tool.

A later amendment can add an agent path. It needs its own owner decision.

#### L11 — Views: count and time only, no IP address

- **What a view is.** A view is a resolved open: the L6 statement matched an active link. It adds
  1 to `view_count` and sets `last_viewed_at`. A 404 or 410 changes nothing, and neither does a 503
  when the gateway did not answer 200. In the rare case that the R2 read fails after a 200, the
  recipient gets 503 and the view still counts. A second call to count after the bytes are served
  is rejected: it adds an internal endpoint and a race with a revoke, for a case that a missing R2
  object alone causes.
- **No IP address anywhere in Seshat.** No column, event, or log line holds a recipient's IP
  address, user agent, or referrer. The Worker forwards no recipient header (L6 step 2). The
  resolve endpoint logs no request address. The Caddy API route sets `X-Forwarded-For` from
  `CF-Connecting-IP` and writes an access log. Ticket (b) confirms on the live path that the
  Worker's call carries no recipient address there. If it does, ticket (b) removes that header in
  the Caddy API block for the resolve path before it is logged.
- **Events** (structlog, to Elasticsearch, with `trace_id`). None holds the token, its hash, or an
  address:
  - `share_link_created`: `link_id`, `artifact_id`, `owner_user_id`, `expires_at`
  - `share_link_revoked`: `link_id`, `artifact_id`, `owner_user_id`
  - `share_link_opened`: `link_id`, `artifact_id`
  - `share_link_refused`: `outcome` (`unknown` or `inactive`), and `link_id` for `inactive`
- **Limit.** `view_count` counts resolved fetches, not people. A preview fetch, a mail security
  scan, and a repeat open each count.

### A1.4 gVisor does not apply to the share path

The owner asked on 2026-11-21 whether to use gVisor (a user-space kernel that isolates a container
from the host kernel). It does not apply here. No artifact code runs on Seshat's servers on the
share path: the Worker runs in Cloudflare's V8 isolates, and the page script runs in the
recipient's browser, inside the opaque-origin sandbox. The gateway on the open path runs only its
own lookup. gVisor fits the places where untrusted code runs on the VPS: `run_python`, the `bash`
child, and the FRE-1679 development sandbox. That is a separate decision.

### A1.5 Effect on the grant tickets (FRE-1748 AC-3)

The owner decided that links ship first and the grants are decided later.

| Ticket | Changes? | What changes |
|---|---|---|
| FRE-1531 (D11 JWT) | No | Done. It is the precondition of L9, and it is met |
| FRE-1532 (read side) | Yes, one item | Its pre-commit rule (ADR AC-11) flags every query on `artifacts` without `artifact_readable_by` or a marker. The share resolve query joins `artifacts` and has no caller identity, so it must not call the function. FRE-1532 adds a third marker, `# artifact-access: share-link`, and lists the resolve query under it. Nothing else changes |
| FRE-1533 (grant API) | No | Independent. Its `/grants` endpoints and the `/share-links` endpoints do not overlap |
| FRE-1534 (grant PWA) | Yes | Ticket (c) adds the **Share** button and sheet first. FRE-1534 adds "Share with a Seshat user" to that same sheet, not a second Share action, and it gains a blocked-by relation on ticket (c) |
| FRE-1535 (approval) | No | Done. L10 records its effect |
| FRE-1536 (`artifact_share` tool) | No | Independent. The tool creates grants, never links (L10) |

Master updates FRE-1532 and FRE-1534 (FRE-1748 AC-3).

### A1.6 Implementation chain

Filed in Linear under the project *Artifact Execution Security*, state Needs Approval:

1. **(a) Gateway:** L3, L4, L5, L6 steps 3 and 4, L9 endpoints, L10 guard tests, L11 events. Schema,
   migration, the setting, the create, list, and revoke endpoints, `can_share_link`, the snapshot,
   and the internal resolve endpoint.
2. **(b) Worker, share hostname, and Access bypass:** L2, L6 steps 1, 2, and 5, L7, L8, and the
   L11 live checks. The Terraform and Worker change is in the private secrets repo and is applied
   on the owner's go. The public repo gets a share profile in the envelope verifier
   (`observability/artifact_envelope/`). Blocked by (a).
3. **(c) PWA:** L9 PWA. Blocked by (b), because its live check sends a real link.

### A1.7 Alternatives considered

**Token design**

- **A signed URL (an HMAC over the artifact id and the expiry), with no table.** *Pros:* no
  database read to check a link. *Cons:* revocation needs a deny list, and the view count needs a
  write, so every open reads the database anyway. It adds a signing key to keep and rotate.
  *Why rejected:* the owner requires immediate revocation (decision 3) and a view count (decision
  8). A table gives both, and a signature adds nothing.
- **Store the raw token, or a slow hash of it.** *Raw:* a database leak opens every active link.
  *Slow hash (bcrypt, Argon2):* protects low-entropy passwords, but a 256-bit random token needs no
  stretching, and the slow hash costs CPU on the VPS for each anonymous request. *Why rejected:*
  SHA-256 of a 256-bit token gives both safety and low cost.
- **R2 presigned GET URLs.** *Pros:* no Worker change. *Cons:* a presigned URL lasts at most 7 days,
  so the 30-day choice is not possible. It cannot be revoked, it carries no CSP, and it counts no
  views. *Why rejected:* it fails decisions 3, 4, 5, and 8.

**Hostname**

- **A path on the artifact host (`artifacts.example.com/s/{token}`) with a path-scoped Access
  bypass.** *Pros:* no new DNS record or Worker route. *Cons:* the public and the private paths
  share one hostname, so one wrong path rule or Worker branch exposes the private host. *Why
  rejected:* the owner keeps the artifact hostname behind Access (decision 2). A whole hostname is a
  simpler boundary to check.

**Token lookup**

- **The Worker reads Postgres directly, through Cloudflare Hyperdrive.** *Pros:* one hop less.
  *Cons:* Postgres becomes reachable from Cloudflare, the Worker holds a database credential, and
  the access rule exists in two languages in two repos. *Why rejected:* L6 keeps one rule in one
  place.

**The toolkit problem for HTML**

- **Make `/lib/` on the artifact host public with a path-scoped bypass.** *Why rejected:* the owner
  keeps the artifact hostname behind Access (decision 2).
- **The Worker rewrites `/lib/` URLs in each response (HTMLRewriter).** *Cons:* toolkit URLs
  appear in attributes, in CSS `url()`, and in strings that script builds, so a rewrite misses
  cases. It parses artifact content on every request, which ADR-0089 D1 avoids. *Why rejected:* the
  export transform runs once, checks SRI, and the L5 self-contained check refuses what it misses.
- **Run the export on every open.** *Cons:* every anonymous request makes the VPS fetch and inline
  the toolkit. *Why rejected:* the snapshot does that work once, at creation.

**Token visibility to page script**

- **A trusted wrapper page that puts the artifact in a sandboxed `srcdoc` child, so the child's
  location holds no token.** *Pros:* page script cannot read the token. *Cons:* a second document
  and an escaping step on every HTML open, and SVG needs its own wrapper. *Why rejected:* the token
  opens only the content that the script already holds (L7), so hiding it protects nothing.
- **Exchange the token for an HttpOnly cookie and redirect to a URL with no token.** *Why rejected:*
  it adds a cookie to the share hostname, a second request, and session state, for the same zero
  gain.

**Recipient outcomes**

- **404 for every case.** *Pros:* the most hiding. *Cons:* a recipient with a real, expired link
  sees "not found" and thinks that the sender made a mistake. *Why rejected:* the holder of a real
  link already knows that it existed, so 410 tells them nothing new.
- **Different pages for expired and revoked.** *Why rejected:* it tells a recipient that the owner
  withdrew the link.

### A1.8 Consequences

**Positive**

- The owner can send one artifact to anyone by SMS or mail, and revoke it at once.
- The artifact and agent hostnames keep Access and the JWT checks unchanged.
- A recipient's page runs in the same sealed box as on the artifact host.
- The table records every link, its expiry, its revoke, and its use.

**Negative**

- A public hostname now reaches the gateway. The rate limit and the shape check bound the load.
- A link cannot be shown again. To send it again, the owner creates a new one.
- Each HTML link stores one snapshot in R2, which stays after expiry or revoke. No access path
  reads it after that.
- Previews and scanners inflate `view_count`.
- The Worker has a second hostname to keep correct, in the private repo.

**Risks and mitigations**

| Risk | Severity | Mitigation |
|---|---|---|
| A Worker branch serves private content on the share hostname | High | L2: only `/s/{token}` exists there. V-L5 checks the other paths |
| A link outlives the owner's intent | Medium | Mandatory expiry of at most 30 days (L4), immediate revoke (L6), the Shared links view (L9) |
| The token leaks through a log | Medium | Token in the request body, no path logging, no token in events (L3, L6). V-L8 |
| A recipient's IP address is stored | Medium | No column, no forwarded header (L6, L11). V-L7 checks the live path |
| Injected text makes the agent publish an artifact | High | No agent path (L10). V-L10 |
| A cache serves bytes after a revoke | Low | `Cache-Control: private, no-store`, no Cache API (L6, L7). V-L3 |
| A token scan loads the VPS | Low | The shape check answers malformed paths at the edge. The rate-limiting rule (L2) |

### A1.9 Verification / Acceptance Criteria (Amendment 1)

Adjudicated on FRE-1748 after the chain (A1.6) lands and deploys. **Fixtures:** the owner A owns
HTML artifact X, which loads KaTeX from `/lib/`, and the uploads PNG P, JPEG J, GIF G, WebP W,
PDF F, and SVG S. A also
owns note N, capture K, pending upload U, and a `text/markdown` artifact M. User B, an allowlisted
user who is not the owner, owns artifact Z. "Fresh browser" means a profile with no Cloudflare
Access session. Each check runs on the test stack (`tests/CLAUDE.md`, FRE-375) unless it says
"post-deploy". A post-deploy check runs only with the owner's OK.

- **V-L1 — A recipient opens each shareable kind.** A fresh browser opens a link to X, P, J, G, W,
  F, and S. Each returns 200 with no Access redirect. X renders a KaTeX element. Each image and S
  display inline with a non-zero rendered size. F displays in the browser's PDF viewer. ·
  **Check:** a Worker test with the gateway stubbed, post-deploy Playwright against the share
  hostname in Chromium, WebKit, and Firefox, and one post-deploy open of each kind in Safari on a
  real iOS device. · *Fails if* any link redirects to Access, returns another status, X shows no
  KaTeX output, an item does not display in any of these browsers,
  or the console reports a CSP violation for a snapshot resource.
- **V-L2 — Expiry has three choices and a hard limit.** Creates with `expires_in_days` 1, 7, and
  30 give `expires_at - created_at` of exactly 1, 7, and 30 days. A create with no value gives 7.
  The values 0, 2, 31, and null return 422 and insert no row. A direct INSERT with a NULL
  `expires_at`, or one of 31 days, fails. A link with `created_at` and `expires_at` set in the past
  returns 410 with the inactive page. · **Check:** API and schema tests, plus a resolve test. ·
  *Fails if* any value differs, any rejected value inserts a row, or the expired link serves bytes.
- **V-L3 — A revoke stops the next request.** Open X's link (200), revoke it (204), and open it
  again with no wait: 410 with the same body as an expired link. The 200 response carried
  `Cache-Control: private, no-store`. · **Check:** resolve test, and the same sequence post-deploy
  through the share hostname. · *Fails if* the request after the revoke returns bytes, or the 200
  lacks the header.
- **V-L4 — Every response carries the share headers.** The 200 responses for X, S, P, and F, a 404,
  a 405, and a 410 each carry `X-Robots-Tag: noindex, nofollow`, `Referrer-Policy: no-referrer`,
  `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`, no `Set-Cookie`, and the
  exact CSP directive set of its kind (L7). · **Check:** the envelope verifier's share profile, with
  an exact directive comparison, post-deploy against the share hostname, plus Worker unit tests. ·
  *Fails if* any header is missing, a `Set-Cookie` is present, or one directive or value differs.
- **V-L5 — The Access boundary holds.** From a fresh browser: the share hostname serves X's link
  with no Access cookie set. `https://artifacts.example.com/{X}` (the real host) still returns the
  302 to the Access login, as ADR-0069 Verification item 2 states. The agent hostname also
  redirects to Access. On the share hostname, `/{X}`, `/lib/katex@0.18.2/katex.min.js`, `/`, and
  `/internal/artifacts/{X}` each return the 404 page. · **Check:** post-deploy requests, and a
  Worker routing test for the paths. · *Fails if* the artifact or agent hostname serves content
  without Access, or any listed share path returns other than 404.
- **V-L6 — Pages hide what they do not need to show.** A malformed token and an unknown
  well-formed token return identical 404 status and body. An expired and a revoked link return
  identical 410 status and body. No 404, 410, or 503 body or header holds X's id, X's title, A's
  email, or A's name. · **Check:** resolve and Worker tests comparing the bytes. · *Fails if* the
  bodies differ, or any of those values appears.
- **V-L7 — No IP address is stored.** (1) `artifact_share_links` has no column of type `inet` or
  `cidr`, and no column whose name holds `ip`, `agent`, or `referer`. (2) The Worker's call to the
  gateway carries only `X-Internal-Token` and `Content-Type` and the JSON body. (3) Post-deploy, a
  link opened from the VPS shell (whose public address master knows) leaves that address in no
  Elasticsearch event and in no Caddy access log line of that minute. · **Check:** a schema test, a
  Worker unit test, and the post-deploy search. · *Fails if* any of the three finds an address.
- **V-L8 — The token is never stored or logged.** After a create in the test stack, `token_hash`
  equals SHA-256 of the decoded token, the raw token appears in no column of the row, in no event in
  the test Elasticsearch on `:9201`, and in no captured log line of the create, an open, a 404, and
  a 410. The list response holds no URL and no token. Post-deploy, after one 200, one 404, and one
  410 through the share hostname, a search of the persisted Cloudflare Worker logs (if enabled) and
  of the Caddy access log for those three tokens finds nothing. · **Check:** an integration test,
  and the post-deploy search. · *Fails if* the token or its hash appears in any of these places,
  other than `token_hash` itself.
- **V-L9 — Only the owner manages links.** With no JWT, each endpoint returns 401. B gets 403 on
  create, list, and revoke, also for B's own Z. A's create on Z returns the same 404 as for a random
  UUID. A's create on N, K, U, and M returns 400 and inserts no row. A's revoke of an unknown link
  id returns 404. `can_share_link` is true for A on X, P, F, and S, and false for A on N and M and
  for B on Z. · **Check:** API tests. · *Fails if* any outcome differs, or a row exists after a
  refused create.
- **V-L10 — The agent has no path to create a link.** (1) Outcome: through `ToolExecutionLayer`,
  with every approval granted, a `bash` call sends `POST /api/v1/artifacts/{X}/share-links` to the
  gateway with every credential that the tool process holds. The gateway records that the request
  reached `get_request_user`, it returns 401, and `artifact_share_links` holds no row afterwards.
  (2) The registered tool catalog (native tools, MCP gateway tools, and skill tools) holds no tool
  whose name or executor creates, lists, or revokes a link. (3) Defense in depth: a test proves that the only module that writes `artifact_share_links` is the share-links
  router module, and that no module under `tools/`, `orchestrator/`, `mcp/`, or `skills/` imports
  it. A seeded violation, a module under `tools/` that inserts into the table, makes that test
  fail. · **Check:** both tests, the second against the seeded violation and the real tree. ·
  *Fails if* the bash call creates a row or gets other than 401, the catalog holds such a tool, the
  structural test passes the seeded
  violation, or the real tree has a second writer.
- **V-L11 — Views count only resolved opens.** Three opens of X's link give `view_count` 3 and a
  `last_viewed_at` at or after the start of the third request. A 404, a 410, and a 503 with no
  gateway 200 (the gateway stubbed down) change neither value. A resolve that returns 200 followed
  by a failed R2 read gives the recipient 503 and adds 1. The list shows both values. An expired
  link is absent from the list. · **Check:** an integration test, and a Worker test with the R2
  read stubbed to fail. · *Fails if* any count, time, or status differs.
- **V-L12 — Filenames are safe.** A title with `"`, `;`, a carriage return, and a line feed gives a
  `Content-Disposition` filename of only `[A-Za-z0-9._-]` with the right extension. An empty
  result gives `artifact.<ext>`. · **Check:** a unit test that parses the header. · *Fails if* the
  parsed `filename` parameter holds a character outside `[A-Za-z0-9._-]`, has the wrong extension,
  or the header holds a carriage return or line feed.
- **V-L13 — A failed create leaves nothing.** Four cases: a fixture whose `/lib/` asset fails its
  SRI check (502), a fixture whose script builds a `/lib/` URL at run time (422), an injected
  INSERT failure, and an injected commit failure. In each case no row
  and no `share/` R2 object exist afterwards. · **Check:** an integration test per case. · *Fails
  if* a row or an object exists after any case.
- **V-L14 — A hostile page cannot reach the network.** A hostile HTML fixture and a hostile SVG
  fixture, each opened by link, try `fetch`, XHR, `WebSocket`, `sendBeacon`, an external `<img>`, an
  external font, an external `<script>`, a form POST, `window.open`, and an iframe of a foreign
  site. In Chromium, WebKit, and Firefox, Playwright's network log shows no request to any host
  other than the share hostname, and each API that reports failure (`fetch`, XHR, `WebSocket`,
  `window.open`) reports it.
  The ADR-0089 residuals (self-navigation, and WebRTC where `webrtc 'block'` is not supported) are
  out of this check and stay named in L7. · **Check:** post-deploy Playwright, and the same against
  a Worker test harness. · *Fails if* any listed attempt reaches another host.
- **V-L15 — The owner sends a link from the phone.** Post-deploy: the owner taps Share on an
  artifact in the PWA on the phone, sends the link by SMS to a device with no Access session, and
  the recipient opens it. The owner revokes it in the Shared links view, and the recipient's reload
  shows the inactive page. · **Check:** the owner, with master watching the events. · *Fails if* any
  step fails.

V-L7 part 3 checks the address on Seshat's own path only. Cloudflare's edge sees every request,
and this amendment does not change that (A1.2).

### A1.10 References

- FRE-1748 — this amendment's ticket, with the owner's eight decisions of 2026-11-21
- ADR-0069 — D3 identity, D8 Terraform location, Dev-2 primary-domain rule, Dev-3 JWT hardening
- ADR-0089 — D2 sealed box and CSP, D1 no content parsing, Addendum A5 export transform
- ADR-0063 — Amendments A and B, approval fails closed
- ADR-0156 — D8, the permission rules
- FRE-1531 (D11, Done) · FRE-1535 (approval, Done) · FRE-1532, FRE-1533, FRE-1534, FRE-1536 (grants)
- FRE-1679 — the development sandbox, where gVisor fits
- `src/personal_agent/service/artifacts_router.py` · `src/personal_agent/service/settings_edit.py`
  · `src/personal_agent/storage/artifact_export.py` · `src/personal_agent/service/uploads_router.py`
  · `src/personal_agent/tools/artifact_tools.py` · `config/cloud-sim/Caddyfile`

---

## Status Updates

### 2026-10-01 - Proposed
**Changed By:** adr session (Opus), with the owner
**Reason:** Owner-led discussion on FRE-1525. The owner chose per-individual grants and confirmed
the design choices on 2026-10-01.

### 2026-11-21 - Proposed, amended
**Changed By:** adr worktree agent (Opus), for master
**Reason:** Amendment 1 (FRE-1748). The owner reversed answer 3 on 2026-11-06 and decided the
eight link items on 2026-11-21. Option 3 is adopted for links. D1 to D11 do not change.
