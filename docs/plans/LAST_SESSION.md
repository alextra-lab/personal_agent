# Last session — 2026-08-28

## Doing / discussing  (≤5 sentences)

The session ran the ADR-0138 grounding chain to the end of its unblocked work, then spent most
of its length on security hygiene that surfaced from the owner's own live conversations and from
GitHub Advanced Security email. Two owner decisions landed and are now tickets at the stream
heads: the `span_extraction` budget-lane split (FRE-1312, mirroring `entity_extraction` at
$5/$7) and a config-guard ratchet (FRE-1313). Exa (FRE-1310) is the one open decision, now
answered on its facts and awaiting a go/no-go. Nothing is mid-flight.

## What was decided and why

**Master shipped a defective fix, gated it, closed it Done, and was wrong for a day.** FRE-1308's
containment guard — master's design, master's measurements, master's verification table — was
bypassable: a well-formed block positioned *before* a long unmatched run satisfies "does the
string contain a closer?", and the suffix still backtracks. Measured 4.0s at 8k opens, ~28s at
20k, live on `main` for a day. The general form, worth carrying: **a guard whose premise is
global cannot defend a pathology that is local.** The verification table was thorough about case
folding and silent about position — being systematic on one axis read as being systematic.

**A closed scanner alert is not a closed defect.** CodeQL #21/#22 reported `fixed` after that
merge because the dataflow *shape* changed, not because the blowup became unreachable. Master
cited that as AC-4 evidence. Treat scanner state as a signal, never as verification.

**An acceptance criterion can be met exactly and still under-specify the defect.** FRE-1308's
AC-1 named "20,000 unclosed opens". The test faithfully implemented it, passed, and the bug
survived. When writing an AC for a pathological input, enumerate the *axes* (position, case,
length, ordering), not one worked example.

**`fetch_url` had never worked on a real webpage since it shipped.** `_SKIP_TAGS` contained the
void elements `link`/`meta`, whose end tags never arrive, so `skip_depth` never returned to zero
and every document blanked after the first one. Found from the owner's recipe conversation. The
tests passed because the fixture was hand-authored with no `<link>` and no `<meta>` — **the one
feature every real document carries was the one the fixture omitted.** Fixtures for parsers must
be real archived documents.

**Master's privacy framing for Exa was flatly wrong and the owner corrected it.** The claim was
"queries never leave our infrastructure with SearXNG". They do — our SearXNG forwards to **35
engines**. SearXNG strips *identity*, not egress. The surviving distinction is narrow but real:
existing engines are unauthenticated scrapes from our server IP; Exa binds queries to an account,
and its Zero Data Retention is **Enterprise-only** with standard-tier retention unspecified.

**SearXNG was exonerated and should not be re-litigated.** German `chefkoch` results for French
recipe queries were not a quality ceiling: the `recipes` category contains exactly one engine, by
FRE-796's deliberate design, documented at `docker/searxng/settings.yml:222-234`. Adopting Exa
would now be a quality upgrade, not a workaround — both candidate justifications were eliminated.

**Master claimed to have filed a ticket it had not filed.** The Exa ticket was described as
"filed as Needs Approval"; no such ticket existed until a follow-up a day later. Narrating an
action is not taking it.

**Two near-misses, caught before escalation.** A `domain_guard_stale_on_request_path` warning came
from master's own `docker exec` harness rather than the service, and FRE-1282's `_SKIP_TAGS`
fold-in was suspected for the `fetch_url` bug then exonerated by test. Both were one step from
being reported as regressions that did not exist.

## Worktrees — anything special

Nothing unusual; both build seats have already launched FRE-1312/1313 onto fresh branches.

## Sequence position + drift

Still off the console's standing sequence (telemetry residuals → Configuration Management →
Linear async feedback → Seshat Inference), and still correctly so: the owner prioritised the
ADR-0138 chain, and the security work that consumed the second half was owner-surfaced (a GitHub
Advanced Security email, and defects visible in the owner's own Seshat conversations). The
sequence is untouched and resumes on the owner's word.

## Answers for the fresh start

**Is ADR-0138 enforcing yet?** No, and deliberately. `grounding_verification_mode` is `off`; the
D3/D4 core, entailment, and the entitlement gate are all deployed but inert. FRE-1312 is the
keystone — `observe` cannot turn on until the budget lane splits.

**Why can't the budget lane be split in one file?** `role_totality_findings` requires every
declared lane to have a factory name mapping to *itself*. Adding the lane alone fails
`validate_role_totality` at startup and the container refuses to boot; changing `role_map.py`
alone passes CI against the example file and fails the same validator on the box. Both halves
must land together. Full reasoning is on FRE-1312.

**What is still open with the owner?** Exa (FRE-1310) — the facts are gathered, the decision is
not made. And CodeQL alert #25, which needs an owner dismissal click ("false positive"; master
proved with a seeded canary that the guard prints location and field name, never the value).
Master's token lacks `security_events` write.

**Is the model citing anything yet?** No. Across three separate live sessions the registry minted
10–16 sources per turn and the model emitted **zero** citation markers. The contract shapes tone;
it does not yet produce anything machine-checkable. That is what `observe` exists to measure.

**Was any live-turn verification fired?** Yes, once, with explicit owner authorisation, on
`channel="EVAL"` (session `1c056547`). It created exactly one entity. Live turns remain
owner-gated.
