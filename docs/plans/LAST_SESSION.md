# Last session — 2026-08-28 → 29

## Doing / discussing  (≤5 sentences)
Grounding went to `observe` and immediately produced its own indictment: FRE-1328 (Urgent,
`Needs Approval`) says ADR-0138 D2 makes tool-derived evidence structurally uncitable, so
`passed_count: 0` was never a model signal and `enforce` is blocked until it's amended. The
local primary was swapped to `qwen3.8-flash-next` with `sub_agent` on `gpt-5.4-mini`; the swap
is **live but unadjudicated** — the owner values its answer quality, and I have no measurement
that separates that from the 3× decode-rate loss. Owner decisions still queued: FRE-1328's ADR
amendment, the preserve-thinking question, CodeQL #25's dismissal click.

## What was decided and why

**Grounding — the finding that reframes the programme.** D1 default-denies; D2 rejects any tool
whose arguments are model-authored (`bash`, `run_python`) because such a tool can echo the
model's own words back wearing a tool's identifier. So on any turn that reasons from `bash`,
D1 demands citations and D2 has removed the only citable source — **no compliant answer
exists**. Measured on trace `dba5b2cba1e0bece6c8b9396465a265c`: 4 `bash` calls succeeded,
~27k tokens of real Tempo data reached the model (input 14,638 → 41,520 with no tool call
between), all four logged `source_registry_tool_inadmissible`, and the registry held 7 sources
— the user message and six unrelated memories. Consequences: FRE-1284's metric measures
"did this turn use citable sources", not honesty, so it cannot make the discrimination
FRE-1285 keys enforcement on; and FRE-1316 (vision) is one member of this class, not a
separate defect — recommend FRE-1328 absorb it. **The general law: what is learned by *doing*
has no admissible referent; only what is learned by *retrieving* does.**
Separately and still true: that turn had the ground truth in context and wrote a nine-row
table matching none of it (FRE-1327) — two failures, not one.

**How I missed it, which is the reusable part.** I reported `passed_count: 0` four times across
**two different primaries** as a finding. A model-behaviour explanation predicts variance; a
constant across two models is apparatus. The owner caught it — *"something is very off and I
don't see what it is yet."* Also: I asked the model why it fabricated, and treated the answer
as evidence. That explanation turn scored 0/6 itself. Its account of itself is a fresh
generation, not a retrieval.

**Model swap — three corrections, all mine, all from the owner.** (1) tok/s ≠ turn latency:
*"It provides more sophisticated responses."* (2) *"Wall clock is not the only factor."*
(3) The whole litellm premise: I asserted litellm had never heard of Qwen3.6-27B (**it carries
it under 7 keys — and that evidence was in my own earlier probe output, which I read and then
wrote the opposite of**), that the `ovhcloud/` prefix was missing (`register_model_pricing`
adds it at startup, `service/app.py:777`), and implied local calls bypass litellm. Owner:
*"ALL CALLS ARE CONFIGURED TO PASS THROUGH litellm. All cost analysis — all of it."* Cost does;
generation splits two-path by placement (ADR-0033). Assume litellm knows a model until measured.

**Why `sub_agent` couldn't be the OVH 27b**, which looked like an arbitrary refusal and wasn't:
`sub_agent` is non-thinking by ADR-0033; the outgoing local companion expressed that with
`disable_thinking`, a local-dispatch field; on the cloud path the equivalent lever is a verified
`reasoning_effort: none`, and litellm exposes **no reasoning lever for `ovhcloud`**. The
FRE-1007 guard was correctly refusing. I tried two fixes first and reverted both — registering
our catalog into litellm's map before checking (would have made *every* unknown model pass;
the existing test caught it) and asking litellm about the provider rather than the model
(disproved by measurement: `anthropic/total-garbage-model-xyz` also returns False). Reasoning
gave the answer the third attempt found by hand.

**Caddy 504s were prefill, not the model.** `response_header_timeout` bounds time-to-first-header;
Flash-Next prefills at 285 t/s vs the incumbent's 923, and a 55–62k-token tool turn needs ~218s.
60s → 300s. It does **not** reintroduce the two `LOAD-BEARING OMISSION` body-timeout exclusions.

**My own process cost ~80 minutes of live outage.** I had the config fix validated at 18:41,
reverted it, and wrote a ticket; it shipped at 20:01 and took 12 minutes when done directly.
The reasoning for reverting was sound (uncommitted config on the primary path is unversioned
config). The error was concluding "so I'll file a ticket" instead of "so I'll branch it, PR it,
merge it." During an outage master fixes it; the ticket is the record, never the mechanism.

**Secrets.** The owner offered to paste the Exa key here; I declined — this conversation lands
in `.claude/projects/.../*.jsonl` in plaintext. Owner placed it in `pass` themselves and I
verified the copy by comparing SHA-256 digests, never reading the value. `docker/searxng/settings.yml`
is **tracked in a public repo** — the Exa `api_key` must use the FRE-1209 gitignored-real /
tracked-`.example` pattern, never go in that file.

## Worktrees
build2 is on FRE-1321. build1 idle, next is FRE-1324 — **FRE-1324 must precede FRE-1320**
(owner: *"there is a dependancy"*).

## Sequence position + drift
On plan. The unplanned work was the model swap (owner-directed) and FRE-1323's security floors;
both closed. FRE-1328 is a genuine insertion ahead of the grounding chain — `enforce` cannot
proceed over it.

## Answers for the fresh start
- **Is `passed_count: 0` a model-quality problem?** No. Structural. Read FRE-1328 before
  drawing any conclusion from a grounding compliance number.
- **Is the Flash-Next swap settled?** No. Live, owner prefers its answers, decode measured at
  21.1 vs 62.3 tok/s. The missing instrument is per-turn wall clock on real work, not benchmarks.
- **Why is `sub_agent` a cloud model now?** Not preference — the MBP holds one model at
  Flash-Next's 87 GiB, and OVH has no reasoning lever. See FRE-1319's test docstring.
- **HYBRID never fires** — `conversational_always_single` forces SINGLE (501 vs 4 over 30 days).
  That is configuration, not a sub-agent bug; don't re-diagnose it.
- **`telemetry/dispatch_state.json.bak-163808`** is the owner's untracked file — leave it (my
  `git add -A` swept it into PR #980; removed in #981).
