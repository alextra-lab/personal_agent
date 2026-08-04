# Last session — 2026-08-02 → 08-04 (the session that stopped trusting its own instruments)

## Doing / discussing

Began as delivery (five merges, four deploys) and became, on the owner's question about a context
meter in the PWA, an investigation into whether *any* of our context and cost numbers are real. They
mostly are not. The owner then set the rule that resolves it — accurate context size comes from the
inference server, everything else is guesswork — and that rule is now FRE-1142 and ADR-0132's
sequencing anchor. **Pick that up.** Two audits were reconciled against each other along the way and
both were partly wrong, which is the session's real lesson.

## What was decided and why

**The owner's rule, and master could not refute it.** Context size must come from the inference
server's own accounting. FRE-908 already proved the local estimator misses reasoning content by >1.5×.
The refinement that survives: `usage` is authoritative but post-hoc, so a pre-flight trim must *ask*
the server (a tokenize endpoint), never estimate. The owner's differencing method —
`prompt(N+1) − prompt(N) − completion(N)` = that tool result's exact cost — yields the session-vs-tool
split as a by-product and needs no estimator anywhere. It is derivable from `api_costs` today.

**`api_costs` (Postgres) is the authoritative per-call series, not Elasticsearch.** The ES per-call
`prompt_tokens` emit last wrote **2026-05-10** — dark ~3 months, unnoticed. That absence is *why* the
Fable audit had to infer, and why its arithmetic went wrong. Do not reach for ES for per-call tokens.

**Fable's audit: structure sound, derived numbers unreliable.** Three errors, all conceded on
reconciliation. Its "compaction fired 0×" queried an event name that has never existed
(`within_session_compressed`); the real emits show **260 all-time / 2 since 07-20**. So compaction
*is* reachable and FRE-942's tail-band repair is vindicated live. Corrected in the committed audit
doc's erratum. **The reversal matters:** the conversational axis is starved by the 10-message slice
(that half stands), the within-turn axis is thinly but genuinely governed, and the dominant spend
there is **re-transmission** — ~32K/call × 25 calls — which sits below every threshold by design.
That is the open economic question, and nothing is designed to reduce it.

**Master made the same class of error four times and each was caught by someone else.** Cited an
unmerged script as available infrastructure; misread `kind: local` in `substrate.yaml` as "served by
that container" when it means "resolve via the catalog"; gated FRE-1134 on Fable's unverified 12K
figure; and wrote an acceptance criterion equating session context to the first call's prompt when
that also carries tool schemas. Pattern: asserting a *derived* fact without checking the layer beneath
it. The owner caught two, Fable caught two.

**FRE-1143 was assigned to Fable deliberately, as a capability probe.** Its audit failure was narrow —
derived numbers — while its structural reasoning held. An ADR is almost all structural reasoning, so
the assignment played to the observed strength. It produced ADR-0132 with two *measured* discoveries
that corrected master's own facts: `DomainGuard.check_url` has **zero production callers** (the guard
enforces nothing today), and the CF pair also authenticates the artifact origins, so "three call
sites" was an undercount. Verdict: when Fable measures rather than infers, it is excellent.

**FRE-1109 stays unapproved pending the ADR-0132/FRE-1113 line** — unchanged from last session.

## Worktrees — anything special

- **explore** must stay pinned to the **deployed** SHA, not `origin/main`, for any analysis. It was
  re-pinned to `41e76267` for FRE-1131.
- build / build2 / adrs hold merged-but-undeleted branches, so `--delete-branch` fails locally on
  every merge. Known, harmless.

## Sequence position + drift

Still entirely in memory/recall + context, owner-directed. The console's standing sequence (telemetry
residuals → Config Management → Linear async feedback → Seshat Inference) remains untouched and
unstarted. No console commits this session; contract intact, 38 of its 60 lines.

## Answers for the fresh start

- **Why is the recall work stalled behind a probe run?** FRE-1122's baseline must be taken *after*
  the mechanical recall fixes deploy and *before* FRE-1118 changes anything, or the delta is
  confounded. FRE-1118 and FRE-465 are parked for exactly this. This trap was caught four separate
  times this week; assume any measurement straddling a change is wrong until proven otherwise.
- **Why is `conversation_max_history_messages = 10` such a big deal?** It slices history *before* the
  gateway runs, so occupancy cannot grow, so every compaction mechanism downstream is starved on the
  conversational axis, and the KG becomes the sole memory from turn 6. FRE-1134 decides it, gated on
  a three-arm experiment with the owner's grow-then-recall design as primary candidate.
- **Is the `Needs Approval` queue ten tickets?** No — it is **60+**, and master had been reporting
  only the current session's slice. Three Urgent among the older ones, plus three entire unapproved
  ADR chains (0127, 0128, 0129). The owner was offered two triage options and has not chosen.
- **Can master open docs PRs naming FRE ids?** FRE-1075 (Urgent, 07-30) says they corrupt ticket
  state; FRE-1011 records 11 occurrences across 7 sessions. Master ran it four times this session and
  verified no damage — but it is a live hazard, unfixed, and the check is not automatic.
