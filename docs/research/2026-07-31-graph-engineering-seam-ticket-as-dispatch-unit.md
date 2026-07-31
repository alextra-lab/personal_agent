# Graph engineering — the seam ticket as the dispatch unit

**Date:** 2026-07-31 · **Author:** adr session · **Requested by:** owner, in the FRE-1078 session
**Status:** Design study. Not an ADR, and deliberately not one — nothing here is decided. No code, board
or deploy state was changed in producing it.

---

## The proposal, in the owner's framing

> A new idea is reviewed and an ADR is created. The ADR session writes the ADR, the seam ticket and the
> sub-tickets, and PRs that. Master qualifies it and puts it in Linear. **The new dispatch is for the
> seam ticket to be given to a session which completes all the sub-tickets using sub-agents or graph
> engineering.**

Today the unit of dispatch is a **leaf**: one build session takes one sub-ticket, opens one PR, passes
one gate, and the chain advances when master runs advance-dispatch at the merge. An N-ticket chain is
N cold starts, N plans, N gates, N merges.

Under the proposal the unit of dispatch is the **objective**: one session takes the seam ticket, which
ADR-0130 defines as the ticket that owns the ADR's overall objective, and drives the whole chain from
inside — fanning out to sub-agents, or orchestrating them over the chain's dependency graph.

---

## Where the evidence stops, stated first

This is a study of documents and one worked specimen. It is **not** an empirical result, and the
numbers that would decide it have not been measured.

| used | not used |
|---|---|
| `.claude/skills/{lifecycle-rules,master,build,adr,prime-master}` read in full | **Per-session orientation cost — never measured.** The central efficiency claim rests on it |
| ADR-0130 (merged as PR #774) and its three tickets | **Sub-agent fan-out cost on a real chain — never measured** |
| ADR-0129 + its eight-ticket chain, read as the specimen | Transcripts. No claim here about what any session believed |
| `docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md` | Any A/B of the two delivery models. None has been run |

Two facts are verified rather than asserted: ADR-0129 is 376 lines (`wc -l`), and its chain is **eight**
tickets — B1 through B8, gapless: FRE-1064, 1065, 1067, 1069, 1070, 1071, 1072, 1073 (enumerated from
Linear). Everything about what those eight cost to deliver under either model is unmeasured.

**A correction, recorded rather than quietly fixed, because it is the failure this table exists to
prevent.** The first version of this document stated the chain was **ten** tickets, adding FRE-1066 and
FRE-1068 — and stated it in this table, under the heading *verified rather than asserted*. It was not
verified. The number came from MASTER_PLAN's prose, and neither ticket is a chain member: both say
"independent of that ADR" in their own bodies, and FRE-1066 was already **Done** — merged as PR #771 and
deployed at 05:18Z on the day this was written. The error's source is worth keeping in view: the count
was read from a derived state document, in a session that had spent the morning establishing that the
same document is a state mirror which rots. **A "where the evidence stops" table is worthless if its own
contents are not held to it.**

**The strongest claim this document makes is that the proposal is worth one narrow experiment.** It does
not claim the proposal is better.

---

## 1. What the proposal actually fixes, and it is not throughput

The obvious argument is amortization: eight sessions each cold-read the same 376-line ADR and the same
chain. That is real but unquantified, and it is the weaker argument.

The stronger one is **coherence at the seam**, and ADR-0130 paid for the lesson hours before this was
proposed. Severing criterion inheritance created a hole — a design obligation can fall between children
while every child passes — which forced a coverage clause into D1 and an obligation → owner mapping into
AC-2. Both are compensating controls for the same structural fact:

> A leaf-dispatched chain has no participant who sees more than one leaf.

A session holding the whole chain sees the seam by construction. That is a quality argument, and it is
the one worth testing.

There is a second structural fix. As ADR-0130 stands, the seam ticket is a **late adjudicator of work
somebody else did** — accountability for the ADR's objective sits with an entity that never built any
of it. Dispatching the seam ticket unifies ownership and accountability in one place. That is a real
improvement to a design decided the same day, and it is the part of the proposal least likely to be
wrong.

---

## 2. The proposal is two changes welded together

This is the load-bearing observation of the study, because the two have opposite risk profiles and
nothing forces them to travel together.

**(a) One session holds the chain.** Reads the ADR once, keeps it warm, fans out to sub-agents per
child, owns the objective end to end.

**(b) One PR for the chain.** Master gates once, over a diff that is the whole chain wide.

Almost every benefit in §1 comes from **(a)**. Almost every risk below comes from **(b)**. A session can
hold the chain *and* push a PR per child — sequentially, or on stacked branches — keeping master's
per-child bounce lever intact.

Any experiment that changes both at once cannot attribute its result to either.

---

## 3. Three problems the proposal creates

### 3.1 The seam session marks its own homework

ADR-0130 puts the ADR's objective on the seam ticket **so that it is tested by someone other than the
children that implemented it**. If the entity that built the chain also adjudicates whether the
objective was met, that independence is gone.

Two things could preserve it, and they are not equally strong:

- **The window.** Seam criteria are long-horizon by construction — ADR-0130's own run to thirty and
  ninety days. The building session is long gone by then, so a context boundary exists for free. The
  awkwardness of the window turns out to be the source of the independence.
- **Master's gate.** Real but thin: ADR-0130 § Signal trust boundary has master trusting the handoff
  contract rather than re-deriving the work.

Where this bites is an ADR whose criteria are **immediately adjudicable**. There the window provides
nothing and only master stands between building and self-certifying. Any adoption needs an explicit
rule for that case; this study does not propose one.

### 3.2 It reverses a decision taken the same day, and the reversal is partly justified

ADR-0130 D2 states: *"Scope is frozen to evaluating. The seam ticket produces verdicts; it never
implements fixes."* That was a codex round-2 blocking finding — **scope absorption**, the seam swallowing
work until it cannot close — and it was accepted.

The finding is right about a real failure mode and wrong about which act causes it. Codex's concern was
the seam absorbing **remediation of its own red verdicts**, an unbounded fix-judge-fix loop with no
defined end. **Building the original chain is a different act with a defined end** — the chain is
enumerated in advance, in the ADR.

So the rule should target remediation, not construction. If the proposal is adopted, D2 needs an
explicit amendment with that reasoning recorded — not a silent reinterpretation.

### 3.3 The gate collapses, if (b) travels with (a)

- It contradicts a standing halt condition: lifecycle-rules lists *"plan would bundle multiple ADR
  phases into one PR"* as a halt. The rule may deserve to change, but overriding it should be deliberate.
- Master's cheapest lever disappears. Today a bad call in child 2 bounces at child 2, before children
  further along the chain are written against it.
- The rollback unit grows with the chain. A child mid-chain fails its criteria — bounce the whole chain, or land
  the rest and revert one? Neither answer is good.
- ADR-0130's per-ticket gate has nothing to attach to. Sub-ticket criteria still exist, but the
  artifact they were to be proven against at a gate does not.

---

## 4. Where the two ideas compose rather than fight

Under this model sub-ticket criteria become **more** necessary, not less. They are the internal
checkpoints that keep a long-running session honest between its start and an adjudication it partly
owns. ADR-0130's two-tier rule is what would make the proposal safe, not what it displaces.

Similarly, the busy-guard concern recorded in ADR-0130's risk table — an activated seam ticket occupying
the `adr` stream for the length of its window — turns out to be **conditional on what the seam is
doing**. A seam occupying a stream while *waiting* is a stall. A seam occupying it while *building* is
a stream doing its job. The lifecycle becomes two activations: dispatched at chain start to build,
returned to parked when the PRs land, activated again on its due date to adjudicate.

---

## 5. Machinery that already exists, and what "graph engineering" would add

**`context:keep` is (a) at N = 2.** The label already hands the next ticket to the same warm seat, and
lifecycle-rules already treats a follow-on ticket sharing files or substrate as the case for it. The
proposal is that label generalised to the full chain with sub-agent fan-out. That reframing matters: the first
experiment needs **no new machinery at all**.

**Sub-agent and dependency-graph orchestration is available in the harness** (fan-out, pipelines with
per-item stages, dependency-ordered phases). Its cost profile on a real chain is unmeasured, and the
project has recent history — the cost audit and the deliberate kill-switch halt of 2026-07-25/26 — that
makes an unmeasured fan-out on an eight-ticket chain a poor first move.

The honest reading is that "graph engineering" is the **end state**, not the experiment. The chain's
dependency graph is already written down as Linear `blockedBy` relations; a graph orchestrator would
consume that graph rather than have the resolver walk it one node per session. Nothing about that
requires proving itself before (a) does.

---

## 6. The experiment

**Run one three-ticket chain on a single warm seat using `context:keep`, one PR per child.** That is
change (a) alone, with machinery that already exists, and it tests the amortization and coherence claims
without collapsing the gate.

**What must be measured, against the current leaf-dispatched baseline:**

1. Tokens and wall-clock per child.
2. Whether the seam obligations were caught — the coherence claim making a falsifiable prediction.
3. Defects found at each gate.

**The trap, named because success and failure look identical here.** Measure (3) alone and a bundled or
warm-seat run that *hides* defects produces fewer gate findings — which reads as a cleaner gate. A
reduction in gate findings is the predicted outcome of both the model working and the model failing, so
it cannot be the deciding measure. **(2) is the discriminator**, because only the coherence claim
predicts it.

**Which chain not to use.** ADR-0129's is the obvious candidate — eight tickets, already filed — and the
worst available: cross-repo, telemetry-wide, and FRE-1065 touches `authenticated` and `user_id`, where a
bridge that works for tracing while silently dropping a field widens data access. Testing a new delivery
model on the highest-stakes chain in the queue yields two problems and no way to separate them. Use
something small and reversible.

---

## 7. Recommendation

1. **Do not write an ADR yet.** There is nothing to decide until (a) has been run once. An ADR now would
   record a preference, not a decision.
2. **Run the N = 3 `context:keep` experiment**, one PR per child, measuring (1)–(3) with (2) as the
   discriminator.
3. **Hold (b) — the single bundled PR — out of the first experiment entirely.** It carries every risk in
   §3.3 and none of the benefits in §1 depend on it.
4. **If (a) holds, the ADR that follows has three things to settle:** independence of adjudication for
   immediately-adjudicable criteria (§3.1); the amendment to ADR-0130 D2 separating construction from
   remediation (§3.2); and whether each sub-ticket still gets its own PR (§3.3).
5. **ADR-0130 landed unchanged (PR #774), which was the right call.** Amending it to accommodate an
   untested proposal would have been worse than amending it later with the reasoning recorded.

---

## Limitations

- **No measurement supports the efficiency claim.** Per-session orientation cost, sub-agent fan-out
  cost, and any comparison between the two models are all absent. §1's stronger argument is structural
  and does not depend on them; §1's weaker one does, entirely.
- **The coherence claim is an argument, not a finding.** That a chain-holding session catches seam
  obligations a leaf-dispatched chain misses is a prediction. ADR-0130's coverage clause exists because
  the gap is real; that a warm seat closes it is untested.
- **n = 1 specimen.** ADR-0129's chain is the only chain examined, and it is unusually large and
  unusually cross-cutting. Conclusions drawn from it may not transfer to a three-ticket chain — which is
  also, awkwardly, what the recommended experiment uses.
- **The self-marking analysis reads against ADR-0130 as merged.** It landed as PR #774 unchanged, so
  §3.1 and §3.2 are read against the merged text. A later amendment to D2 would require re-reading both.
