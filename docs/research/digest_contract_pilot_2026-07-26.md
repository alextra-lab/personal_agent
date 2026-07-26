# Does a schema contract remove the session digest's parse failures?

**Ticket:** FRE-996 · **Date:** 2026-07-26 · **Author:** build session (Opus)
**Backing:** ADR-0124 D3 / Amendment B · **Blocked by:** FRE-995 (audit, merged `0c0b6330`)
**Hands off to:** FRE-993 (output ceiling) · FRE-994 (compression curve)
**Raw records:** `telemetry/evaluation/fre996-digest-contract/` (gitignored — this write-up is the committed artifact)

---

## 1. What was run

Thirty real sessions from the durable capture corpus (2,856 captures; 314 sessions carry the
two-turn minimum), sampled deterministically by a hash of the session id so the sample is
arbitrary but reproducible. Each session was put through the digest generation call three ways:

| Arm | What it sends |
|---|---|
| **A** | Today's call — the JSON shape described in prose in the system prompt, nothing enforced |
| **B** | The wire schema as a **forced tool call** |
| **C** | Arm B plus per-slot item ceilings (`maxItems`) |

Identical prompts, identical sessions, arms interleaved per session. 90 calls, **$1.21 actual against
a $2.33 worst-case estimate**, billed to the `study` budget lane (FRE-839's one-off-corpus-run lane,
so the pilot never contended with the live `captains_log` cap nor polluted its cost series). The
producer stayed disabled throughout — the harness calls the model directly and never marks a session
clean.

---

## 2. Results

| | Arm A (prose) | Arm B (contract) | Arm C (bounded) |
|---|---:|---:|---:|
| Parsed successfully | 25/30 | **30/30** | **30/30** |
| Truncated | 5 | 0 | 0 |
| **Needed fence/trailing-prose unwrapping** | **11/30 (37%)** | **0/30** | **0/30** |
| Hit the 2048 output ceiling | 5/30 | 0/30 | 0/30 |
| Output tokens (min / p50 / max) | 57 / 968 / 2048 | 94 / 556 / 901 | 94 / 557 / 1050 |
| Cost | $0.50 | $0.35 | $0.36 |

Arm A's failure rate is **5/30 = 16.7%**; arms B and C are 0/30.

> **A correction to this pilot's own first pass.** The harness initially scored one of those
> five as `empty` rather than `truncated`, because it tested for an empty payload *before*
> testing whether the ceiling had been hit. That reply had `finish_reason=length` at exactly
> 2048 output tokens — a generation that exhausted its budget before emitting anything usable,
> which is truncation, not a model declining to answer. Caught in self-review, fixed in the
> classifier, pinned by a regression test, and the records above are the corrected
> reclassification (done offline from the stored raw records — no re-run, no extra spend).
> Worth recording because it is the same class of error the whole ticket is about: a sizing
> failure wearing a formatting failure's label.

---

## 3. The finding that is carried by mechanism

**Wrapping went from 37% to zero, and it cannot come back.**

Eleven of thirty arm-A replies arrived fence-wrapped or with trailing prose after the closing brace.
Every one of them still *parsed* — `_strip_fences` recovered them, so they sit in the "ok" column.
That is the important detail: **the fence heuristic is load-bearing on more than a third of
production replies today.** It is not a dormant safety net; it is doing real work on every third
call, and it is one unhandled variation away from failing. FRE-995 measured exactly that failure in
the skill-routing path, where the same class of unwrap breaks on trailing prose 16.3% of the time.

Under the contract the payload arrives in the tool call's `input` field. There is no free text to
unwrap, so the failure has nowhere to occur. This claim rests on the mechanism, not on the sample —
0/30 alone would only bound the rate at ~11.6% (rule of three), which would prove very little.

---

## 4. Where my prediction was wrong, and why it matters

**I predicted truncation would persist. It went to zero. The contract did not prevent it —
shorter outputs did.**

The plan said, following master's Correction Two and the FRE-995 audit, that a contract cannot
prevent truncation: a schema-constrained generation cut off at the ceiling is still a fragment. That
reasoning is still correct. But truncation went 5/30 → 0/30 anyway, and the cause is visible in the
output-token column: **the contract roughly halved output length** (p50 968 → 556, max 2048 → 901).
Nothing in arms B and C came close to the ceiling, so nothing was cut off.

This distinction is not pedantic, and reporting "truncation eliminated" would be wrong:

- The contract offers **no guarantee** against truncation. It shortened outputs enough, *on sessions
  of this size*, that the ceiling stopped being reached.
- Restore the conditions and truncation returns: a longer session, a lower ceiling, a more verbose
  model. The mechanism that would prevent it does not exist.
- **This sample does not reproduce the incident regime.** The sampled sessions are small — 2 to 10
  turns, p50 3. The FRE-995 audit measured ~100% at-ceiling on 2026-07-25/26 production traffic;
  arm A here is 17%. The pilot samples the whole corpus, not the recent large-session regime, so it
  under-represents exactly the conditions that caused the incident.

The honest summary: **a contract is not a substitute for the sizing work.** FRE-993 and FRE-994
remain load-bearing, precisely as master's correction said.

---

## 5. Second, separate finding — bounding the schema did not bound the length

This is reported apart from the contract result because it answers a different question, and it is
the one FRE-994 should read.

Arm C added per-slot item ceilings (`established`/`decisions`/`unresolved` ≤ 5, `corrections` ≤ 2).
It made essentially no difference to length:

| | Arm B | Arm C |
|---|---:|---:|
| Rendered digest tokens (p50) | 221 | 224 |
| Rendered digest tokens (max) | 413 | 419 |
| Over the 250-token budget | 10/30 | 9/30 |

**Item counts are not a proxy for token length**, because item *text* is unbounded — the model
satisfies "at most five items" by writing five longer ones. Any attempt to control digest size
through countable structure alone should be expected to fail the same way. The schema dialect cannot
express a character bound (`maxLength` is unsupported, FRE-995 §8.2), so this avenue is closed:
**length has to be controlled by the prompt's token target and the ceiling, not by the schema.**

---

## 6. What FRE-994 inherits

Digest length, measured on a pipeline no longer dominated by truncation — which is what the audit
said FRE-994 needed and could not previously get:

| | Arm A | Arm B | Arm C |
|---|---:|---:|---:|
| Rendered digest tokens (p50) | 172 | 221 | 224 |
| Rendered digest tokens (max) | 312 | 413 | 419 |
| Over the 250-token hard budget | 6/25 | 10/30 | 9/30 |

**A third of contract-produced digests would fail `DIGEST_OVER_BUDGET` in production.** That is a
success for this ticket by its own stated terms — "a run that produces valid output that is still
too long is a success" — and it is precisely the number FRE-994 exists to act on. Note arm A's
figures are computed only over its 25 parsed replies, so its apparent advantage is survivorship: the
long ones are missing because they truncated.

---

## 7. A signal worth watching, not yet a conclusion

Arm B returned **5/30 entirely empty digests** — a reply that parsed cleanly but filled no slot,
rendering to 0 tokens — against arm A's 1/25 and arm C's 3/30. (This is distinct from the truncated
reply in §2: these parsed fine and simply had nothing in them.) An empty digest is legal by design — ADR-0124 says "Empty is a valid digest" and the scarcity
of corrections is a feature. But a contract that makes it *easier* to emit nothing would be a quality
regression hiding inside a conformance win, and 5 vs 1 at N=30 is too small to call either way.

Flagged rather than resolved. If the contract ships to production, empty-digest rate is the thing to
watch, and it is cheaply observable — `session_summary_generated` already logs per-slot counts.

---

## 8. Limitations

Stated plainly, because several of them bound what the numbers above can support.

1. **Temperature could not be controlled.** `claude-sonnet-5` rejects any value but 1 (litellm
   `UnsupportedParamsError`). The arms therefore carry sampling variance that no harness discipline
   removes, and per-class differences are not deterministic counterfactuals. This is why §3 leans on
   the mechanism and not on 11 → 0.
2. **The sample under-represents the incident regime** (§4): p50 3 turns against a production regime
   of much larger sessions.
3. **N = 30 per arm.** A 0/30 result bounds a rate at roughly 11.6%, no lower.
4. **Per-call rates, not production rates.** The producer retries once, so production final-outcome
   rates are strictly better than arm A's 16.7%.
5. **Arms B/C send a tool definition**, which Anthropic bills as input (~1,700 tokens/call here).
   B and C cost less than A overall only because output more than halved.
6. **Shape and enum drift were not exercised.** Zero occurred in any arm, so the contract's effect on
   them is untested here — and it could not have been proven anyway: Anthropic's strict tool use is
   unreachable through litellm 1.89.2, so conformance is guided, not guaranteed (§9).

---

## 9. Mechanism, for the record

Verified against the installed litellm 1.89.2 source, not inherited from the audit:

- The deployed model is `claude-sonnet-5`. litellm routes `response_format` to Anthropic's native
  `output_format` only for a hardcoded model-substring allowlist (`transformation.py:1467`) that
  **omits sonnet-5**, so `response_format` becomes a synthetic forced tool with `json_mode=True`.
- With `json_mode`, litellm **overwrites the provider's `stop_reason` with `"stop"`**
  (`transformation.py:2485`) before deriving `finish_reason`. A truncated reply would report
  `finish_reason == "stop"` — destroying the very signal this pilot needed. **`response_format` was
  therefore rejected** in favour of an explicit tool, which sets no `json_mode` and leaves the stop
  reason intact. A test pins this so a litellm upgrade cannot silently invalidate the measurement.
- Neither route reaches Anthropic's **strict** tool use: litellm sets no `strict` on its synthetic
  tool and drops the key on the explicit-tool path. This qualifies the audit's §8.1 "capability is
  already wired" — the *plumbing* is wired; strict conformance is not available.

Consequently the contract eliminates wrapping structurally, constrains shape and enum strongly but
without guarantee, and does nothing about truncation.

### 9.1 A newer litellm does not fix any of this — checked, not assumed

We pin `litellm>=1.84.0` and run 1.89.2. The current release is **1.93.0**, 19 stable releases newer.
All three properties above were checked against 1.93.0's own source:

| Property | 1.89.2 | 1.93.0 |
|---|---|---|
| `output_format` allowlist includes `sonnet-5` | No | **No** — the substring set is byte-identical |
| `stop_reason` overwritten to `"stop"` under `json_mode` | Yes | **Yes** — same line, unchanged |
| Anthropic strict tool use reachable | No | **No** — `strict` occurs once in the module, in an unrelated comment |

So upgrading would neither have made `response_format` usable on our model nor made conformance
enforceable. **The design choice holds against current upstream, not merely against our pin.**

Two forward-looking notes:

1. **The allowlist is hand-maintained per model, so this recurs by construction.** Every new Claude
   release is unsupported until someone edits that set —
   [BerriAI/litellm#20533](https://github.com/BerriAI/litellm/issues/20533) is the same complaint for
   Opus 4.5/4.6, and [#16949](https://github.com/BerriAI/litellm/pull/16949) was the manual PR that
   added Sonnet 4.5 / Opus 4.1. "Wait for an upgrade" is therefore not a plan; reaching native
   structured outputs on sonnet-5 would need an upstream contribution or a local override.
2. **Unverified, flagged as such:** the upstream thread states Anthropic has deprecated
   `output_format` in favour of `output_config.format`, scheduled for removal. litellm 1.93.0 uses
   `output_config` only for *reasoning effort*, so that migration is still pending there. This has
   **not** been confirmed against Anthropic's own documentation and should be before anyone relies
   on it.

---

## 10. Recommendation

1. **Ship the contract for the digest.** The wrapping class is eliminated by mechanism, the failure
   rate on this sample went 16.7% → 0%, and it costs less per call than the status quo.
2. **Do not read this as the digest being fixed.** A third of contract-produced digests still exceed
   the token budget. FRE-993/FRE-994 are the fix for that, and this result makes them more clearly
   load-bearing, not less.
3. **Do not generalise the contract on this evidence alone.** FRE-995 §8.3 sequences the other
   sites; skill routing (A4) is the natural second subject and the cheapest confirmation, because its
   16.3% failure rate is pure format drift with zero truncation — the one place the contract's
   remaining claim can be tested cleanly.
4. **Drop item-count bounds as a length lever** (§5), and tell FRE-994 to measure against the prompt
   target and the ceiling instead.
