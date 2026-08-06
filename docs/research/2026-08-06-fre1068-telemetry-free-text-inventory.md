# Telemetry free-text inventory and content policy — `agent-logs-*`

**Ticket:** FRE-1068 · **Measured:** 2026-08-06 · **Corpus:** 1,916,115 documents across 26 `agent-logs-*` indices (full scan, not a sample)

Every figure here is produced by `scripts/audit/fre1068_free_text_inventory.py`. Re-run it and diff
rather than trusting the committed numbers:

```
uv run python -m scripts.audit.fre1068_free_text_inventory --json --out /tmp/inventory.json
```

Secret values, third-party identifiers and personal content are **never** reproduced in this
document — only field names, counts, content classes, and the detector that fired.

---

## 1. The finding that reframes the ticket

FRE-1068 was filed against the `free_text` dynamic template in
`docker/elasticsearch/index-template.json`, on the reading that it "auto-maps as full text, stored and
searchable, anything matching" a name pattern, and that tightening the pattern would therefore reduce
exposure.

**The template governs searchability, not storage.** Elasticsearch retains the submitted JSON in
`_source` regardless of mapping. A field can be unmapped, mapped `keyword` with `ignore_above`, or sit
under a `dynamic: false` subtree, and its full value is still stored and still retrievable by anyone who
can read the index. Narrowing the pattern would have changed which fields are *searchable* and closed
nothing.

That distinction is not academic, because it also breaks the obvious way to audit the problem:

| Question | Cheap method | What it returns |
|---|---|---|
| Does `arguments.command` carry data? | `exists` query | **0 documents** |
| Does `arguments.command` carry data? | read `_source` | **262 documents**, up to 3,836 chars of shell and `curl` command lines |

**43 fields carry content that an `exists` query reports as entirely absent**, and **174 of 273
string-valued fields** have a non-zero gap between the two counts. An inventory built on `exists` — the
natural way to write one — would have reported those 43 fields clean and called the audit a pass. This
is exactly the failure the ticket's own acceptance criterion warns about: *"an inventory that finds
nothing is a failure of the audit, not a pass."*

Two mapping features produce the blind spot:

- **`dynamic: false` subtrees.** The `arguments` object was pinned `dynamic: false` in FRE-544 to stop
  field sprawl. It worked — and it also made every tool-call argument invisible to `exists` while
  leaving it fully stored. `arguments.plan` holds documents averaging **7,055 characters** (max
  13,879); `arguments.script`, `arguments.content` and `arguments.query` are all in the same state.
- **`ignore_above: 1024`.** The `default_string_keyword` template stops indexing a value past 1,024
  characters but still stores it. `command` shows 951 documents carrying content against 561 that
  `exists` can see — **390 blind**, and by construction the blind ones are the *longest*.

## 2. What is actually in the telemetry

### 2.1 A live credential

The detectors found the current `POSTGRES_PASSWORD` in plaintext, in four documents in the current
month's index, written by a single agent bash turn on 2026-08-05. Verified by SHA-256 comparison
against the deployment's environment file. Filed as **FRE-1175**; remediation (rotation, document
deletion) is an operational action and is not part of this ticket.

Bounding the severity: Elasticsearch is bound to `127.0.0.1` and is not reachable off-host, and the
30-day ILM policy expires those documents around 2026-09-04.

### 2.2 Conversation content, including special-category data

`user_message` holds verbatim user turns (219 documents, up to 4,437 chars). Beyond it:

- **Health-topic content.** Web-search queries in `query_preview` and the model's own reasoning in
  `raw_preview` include medical questions — condition names and dietary advice — which is
  special-category personal data under any reading.
- **A user task leaked through a *filename*.** `checkpoint_reflections` (4,327 documents) stores
  Captain's Log paths whose basenames embed the opening words of the user's task. One observed value
  encodes a medication-related request. The field is a path, would be classified `structural` by any
  naive audit, and carries conversation content anyway.
- **Assembled prompt context.** `messages_preview.content_preview` (589 documents) carries turn
  context including standing behavioural preferences drawn from the knowledge graph — profile data
  about the owner, not just their words.
- **Third-party identities.** A real business email address belonging to someone who is not a user of
  this system appears across five fields (`user_message`, `task`, `task_name`,
  `context_messages.content_preview`, `messages_preview.content_preview`).

### 2.3 A field literally named `email`

1,512 documents, emitted by `request_user_resolved` and `display_name_seeded`. Fully indexed, no blind
spot. This is the single largest concentration of personal identifiers in the corpus and it was not
mentioned in the ticket.

### 2.4 A mapping defect worth its own note

`task_name` is claimed by the `enums_keyword` dynamic template — because it matches `.*_name` — while
holding **up to 4,459 characters of verbatim task text**. A free-text field is mapped as a low-cardinality
aggregation key. 32 of its 38 documents exceed `ignore_above` and are unindexed. The same applies to
`original_name`, `proposed_name` and `canonical_name`.

## 3. The inventory

Fields whose observed maximum length is ≥ 40 characters — i.e. those capable of carrying content rather
than an identifier or enum. `docs` counts documents carrying the field; `exists` is the `exists`-query
count; `blind` is the difference. The full 273-field set is in the script's `--json` output.

<!-- BEGIN GENERATED TABLE — regenerate with scripts/audit/fre1068_free_text_inventory.py -->
| field | class | mapping | claimed by | docs | exists | blind | max len | mean |
|---|---|---|---|---:|---:|---:|---:|---:|
| `arguments.plan` | agent-action | not-in-mapping | explicit | 13 | 0 | 13 | 13,879 | 7,055 |
| `arguments.content` | agent-action | not-in-mapping | explicit | 2 | 0 | 2 | 8,949 | 6,258 |
| `exception` | system-diagnostic | keyword | unmapped | 35 | 7 | 28 | 5,132 | 2,265 |
| `task` | conversation | keyword | unmapped | 38 | 6 | 32 | 4,459 | 236 |
| `task_name` | conversation | keyword | enums_keyword | 38 | 6 | 32 | 4,459 | 236 |
| `user_message` | conversation | text | explicit | 219 | 219 | 0 | 4,437 | 94 |
| `arguments.script` | agent-action | not-in-mapping | explicit | 2 | 0 | 2 | 4,103 | 2,133 |
| `command` | agent-action | keyword | unmapped | 951 | 561 | 390 | 3,836 | 379 |
| `arguments.command` | agent-action | not-in-mapping | explicit | 262 | 0 | 262 | 3,836 | 511 |
| `bad_segment` | agent-action | keyword | unmapped | 67 | 26 | 41 | 3,305 | 374 |
| `error` | system-diagnostic | text | explicit | 589 | 595 | 0 | 751 | 281 |
| `arguments.query` | agent-action | not-in-mapping | explicit | 150 | 0 | 150 | 490 | 72 |
| `csp_header` | unclassified | keyword | explicit | 13 | 13 | 0 | 402 | 402 |
| `detail` | unclassified | keyword | unmapped | 1,209 | 527 | 682 | 315 | 50 |
| `response_preview` | conversation | text | free_text | 510 | 309 | 201 | 200 | 200 |
| `raw_preview` | conversation | text | free_text | 35 | 19 | 16 | 200 | 199 |
| `context_messages.content_preview` | conversation | text | explicit | 38 | 6 | 32 | 200 | 176 |
| `stderr` | system-diagnostic | not-in-mapping | free_text | 1 | 0 | 1 | 200 | 200 |
| `arguments.summary` | agent-action | not-in-mapping | explicit | 13 | 0 | 13 | 164 | 135 |
| `hint` | system-diagnostic | text | free_text | 1 | 1 | 0 | 139 | 139 |
| `file_path` | structural | keyword | unmapped | 308 | 189 | 119 | 122 | 95 |
| `file` | unclassified | keyword | unmapped | 169 | 127 | 42 | 122 | 53 |
| `query_preview` | conversation | text | free_text | 14 | 2 | 12 | 120 | 110 |
| `output_format` | unclassified | keyword | unmapped | 38 | 6 | 32 | 117 | 67 |
| `path` | unclassified | keyword | unmapped | 6,220 | 6,220 | 0 | 112 | 30 |
| `checkpoint_reflections` | agent-action | keyword | unmapped | 4,327 | 4,328 | 0 | 110 | 103 |
| `last_processed_path` | agent-action | keyword | unmapped | 1,728 | 1,652 | 76 | 110 | 85 |
| `query` | unclassified | keyword | unmapped | 128 | 92 | 36 | 108 | 63 |
| `messages_preview.content_preview` | conversation | text | explicit | 589 | 301 | 288 | 100 | 90 |
| `arguments.query_text` | agent-action | not-in-mapping | explicit | 66 | 0 | 66 | 96 | 47 |
| `title` | unclassified | keyword | explicit | 118 | 118 | 0 | 95 | 47 |
| `checkpoint_captures` | agent-action | keyword | unmapped | 4,327 | 4,328 | 0 | 84 | 84 |
| `query_text` | conversation | text | free_text | 66 | 49 | 17 | 80 | 47 |
| `entity_id` | structural | keyword | ids_keyword | 1,869 | 881 | 988 | 75 | 12 |
| `target` | unclassified | keyword | unmapped | 1,712 | 843 | 869 | 75 | 12 |
| `entity_name` | structural | keyword | explicit | 1,456 | 1,456 | 0 | 75 | 14 |
| `overflow_file` | unclassified | keyword | unmapped | 3 | 1 | 2 | 73 | 73 |
| `source` | unclassified | keyword | unmapped | 2,068 | 2,068 | 0 | 70 | 14 |
| `public_url` | unclassified | keyword | unmapped | 13 | 1 | 12 | 70 | 70 |
| `arguments.title` | agent-action | keyword | explicit | 13 | 13 | 0 | 67 | 53 |
| `original_name` | structural | keyword | enums_keyword | 592 | 335 | 257 | 65 | 11 |
| `proposed_name` | structural | keyword | enums_keyword | 134 | 43 | 91 | 65 | 13 |
| `scratch` | unclassified | not-in-mapping | unmapped | 2 | 0 | 2 | 65 | 65 |
| `scratch_dir` | unclassified | not-in-mapping | unmapped | 2 | 0 | 2 | 65 | 65 |
| `certs_url` | unclassified | keyword | unmapped | 11 | 3 | 8 | 62 | 62 |
| `event_type` | structural | keyword | explicit | 1,916,115 | 1,916,319 | 0 | 61 | 18 |
| `message` | system-diagnostic | text | explicit | 1,915,994 | 1,916,198 | 0 | 61 | 18 |
| `reason` | system-diagnostic | keyword | explicit | 1,330 | 1,330 | 0 | 59 | 23 |
| `logger` | structural | keyword | unmapped | 1,915,994 | 1,916,198 | 0 | 57 | 33 |
| `canonical_name` | structural | keyword | enums_keyword | 719 | 371 | 348 | 57 | 12 |
| `consumers` | unclassified | keyword | unmapped | 77 | 65 | 12 | 53 | 42 |
| `remedy` | unclassified | not-in-mapping | unmapped | 1 | 0 | 1 | 53 | 53 |
| `trace_id` | structural | keyword | explicit | 194,672 | 194,685 | 0 | 52 | 35 |
| `message_preview` | conversation | text | free_text | 243 | 117 | 126 | 50 | 47 |
| `threshold_violations` | unclassified | keyword | unmapped | 1 | 1 | 0 | 50 | 39 |
| `endpoint` | unclassified | keyword | explicit | 4,334 | 4,334 | 0 | 49 | 25 |
| `resolved` | unclassified | boolean/keyword | explicit | 15 | 3 | 12 | 48 | 38 |
| `arguments.path` | agent-action | not-in-mapping | explicit | 15 | 0 | 15 | 48 | 38 |
| `element_id` | structural | keyword | ids_keyword | 1,428 | 707 | 721 | 44 | 43 |
| `arguments.topic` | agent-action | not-in-mapping | explicit | 21 | 0 | 21 | 43 | 14 |
| `stream` | unclassified | keyword | unmapped | 1,016,903 | 1,017,009 | 0 | 40 | 22 |
| `tool_call_id` | structural | keyword | ids_keyword | 7 | 2 | 5 | 40 | 40 |
<!-- END GENERATED TABLE -->

### Detector results

Documents where a detector would **actually redact** (the safe-value guard applied — a pattern match on
`password=os.environ.get(...)` is not a finding and is excluded):

| detector | field | documents |
|---|---|---:|
| email | `email` | 1,512 |
| credential_assignment | `command` | 23 |
| credential_assignment | `messages_preview.content_preview` | 10 |
| credential_assignment | `arguments.command` | 10 |
| credential_assignment | `bad_segment` | 7 |
| email | `context_messages.content_preview` | 2 |
| email | `user_message` | 1 |
| email | `task` | 1 |
| email | `task_name` | 1 |
| email | `entity_id` | 1 |
| email | `target` | 1 |
| email | `entity_name` | 1 |
| email | `messages_preview.content_preview` | 1 |
| credential_assignment | `arguments.content` | 1 |

### Reading caveats

- **The corpus is live.** A few `exists` counts exceed the scanned document count (e.g. `error`: 589
  scanned, 595 by `exists`) because indexing continued during the ~25-minute scan. Differences of that
  order are drift, not error.
- **`checkpoint_reflections` shows `blind = 0` while `exists` exceeds `docs` by 1** — same cause.
- **162 of 273 fields are `unclassified`.** That is deliberate: the classifier reports what it does not
  recognise rather than defaulting everything into a bucket, so a new content-bearing field surfaces as
  a gap. Most unclassified fields are short identifiers, but `detail` (1,209 docs, 315 chars) and
  `query` (128 docs) are content and should gain classes when someone next touches this.

## 4. Content policy

What may reach `agent-logs-*`, by class. Approved by the owner on 2026-08-06.

| Class | Decision | Enforced by |
|---|---|---|
| **Secrets / tokens / credentials** | **Never stored.** Redacted unconditionally, fail-closed. | `telemetry/redaction.py` at the write chokepoint |
| **Email addresses** | **Redacted.** No join-key cost: `user_id` in this family holds UUIDs, not emails (verified — 3 distinct values, all UUID-form). | same |
| **Conversation / prompt content** | **Permitted, uncapped.** The debugging value is why these fields exist. Recorded as an accepted exposure, not an oversight. | policy only |
| **Agent actions** (commands, tool arguments) | **Permitted**, secret-redacted. This class is where the live credential landed. | same |
| **System diagnostics, structural** | **Permitted** unchanged. | — |

The conversation decision was put to the owner explicitly, with length-capping at 200 or 500 characters
offered as alternatives; the answer was no cap. Capping remains available if the retained volume of
verbatim content later becomes the concern.

**Email redaction is the one change with a real cost**: `request_user_resolved` and
`display_name_seeded` will emit `[REDACTED:email]` where they previously emitted an address. Identity
correlation is unaffected because it runs on `user_id`.

## 5. Enforcement

Redaction happens at a **single write chokepoint**, `ElasticsearchLogger._index_agent_log`, not in the
index template — for the reason in §1, the template cannot govern storage.

The chokepoint exists because the audit found `agent-logs-*` had **five** write paths and four bypassed
`log_event`, so any guarantee stated on `log_event` was false:

| Write path | Before | Now |
|---|---|---|
| `log_event` | direct `client.index` | chokepoint |
| `log_batch` (`async_bulk`) | direct, bypassed | redacted per action |
| `index_request_trace_from_snapshot` — summary | direct, bypassed | chokepoint |
| `index_request_trace_from_snapshot` — per-step | direct, bypassed | chokepoint |
| `index_latency_breakdown` — summary and per-phase | direct, bypassed | chokepoint |

`test_no_agent_logs_write_bypasses_the_chokepoint` parses `es_logger.py` with `ast` and fails if a sixth
path appears. `index_document` is deliberately excluded — it writes the Captain's Log named indices, a
different family under a different template.

**Fail-closed:** a value whose redaction raises becomes `[REDACTED:error]` rather than being forwarded
intact. Markers name the detector that fired (`[REDACTED:credential_assignment]`), so an active rule is
visible rather than silent.

**Precision over recall, deliberately.** Detectors skip environment-lookup and placeholder forms,
because live telemetry contains `password=os.environ.get(...)` — redacting that would destroy diagnostic
value while protecting nothing. The cost is that a novel secret shape can pass. `detect_secrets()` over
the corpus is how that drift is measured rather than assumed; re-run this audit periodically.

### Verification

Written to a real index on the test stack (`:9201`, FRE-375 isolation) and read back:

```
raw_preview : 'connecting with PGPASSWORD=[REDACTED:credential_assignment] psql -h postgres now'
command     : 'PGPASSWORD=[REDACTED:credential_assignment] psql -h postgres'

planted literal present in stored _source : False
redaction marker present                  : True
clean record byte-identical               : True
full-text search for the planted secret   : 0 hits
```

The negative control matters as much as the positive one: a clean record passes through
byte-identical, so a detector that never fires is distinguishable from one that works.

### Known detector gaps

Found in self-review and recorded rather than left implicit. These are limits of a new control, not
regressions — nothing was redacted before this change.

- **JSON-quoted keys are not matched.** `{"token": "abc123"}` does not fire the assignment detector,
  which requires `key<separator>value` with no intervening quote. Secrets embedded in serialised JSON
  inside a log field therefore pass. The key-aware rule catches this only when the credential is a
  real field of the document, not when it is text inside one.
- **The safe-value guard is prefix-based**, so a literal secret that happens to begin with `$`, `{` or
  `<` is treated as an indirection and left alone.
- **`--flag value` covers only common flag spellings**, and `-p secret` (single-letter) is not matched.

The right response to all three is measurement, not speculative pattern-widening: re-run this audit
periodically and let `detect_secrets` over the real corpus say which shapes actually occur.

## 6. What this does not cover

- **Existing documents are unchanged.** Redaction applies at write time; the ~1.9M documents already
  stored keep their content until the 30-day ILM expires them. FRE-1175 covers the one credential.
- **Local rotating JSONL logs** (`telemetry/logs/current.jsonl`) receive the same unredacted content.
  Out of scope here, which is index-scoped; filed to Backlog.
- **Other index families** — `agent-captains-captures-*` stores full turn payloads including
  `assistant_response` and `tool_results` **by design**, and is a larger content surface than
  `agent-logs-*`. It was not audited.
- **Narrowing `free_text` / `index: false` on content fields** remains available as searchability
  hygiene. It closes nothing on storage, so it was not done here; filed to Backlog.
- **The `arguments` blind spot is a mapping decision, not a defect to fix here.** `dynamic: false` was
  correct for field sprawl (FRE-544). The lesson is that it must not be read as "this data is not
  retained".
