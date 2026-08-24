# Adjudication guidance — span labelling for ADR-0138 D1

**Version:** 1 · **Owner:** FRE-1281 · **Governs:** `corpus.yaml`

ADR-0138 D1 says recurring adjudication guidance "lives with the labelled corpus (AC-7), where it
can be versioned and measured, not in this document." This is that file. It is the *labelling*
contract — what a labeller decides and how — and deliberately **not** the extractor prompt. The
inter-labeller agreement in `corpus_agreement.json` measures how well this document does its job;
if κ falls below the preregistered 0.70 in `bars.py`, this file is revised before anything is
scored, because a corpus labelled from ambiguous guidance cannot measure the distinction it claims
to measure.

---

## The one rule

**The default is deny.** Outside the exempt regions below, any span making a claim about the world
requires a citation. Do not ask "is this the kind of thing that usually needs a source?" — ask
"is there an exempt region this falls in?" If there is not, it is `CLAIM_NON_EXEMPT`.

Enumerating what must be cited is unbounded. Enumerating what need not be is finite, and the finite
list is below.

**Ambiguity resolves to assertion.** *"A is better value than B"* reads either as an ordering over
cited prices (exempt) or as a market-value claim (not exempt). When you cannot decide, label
`CLAIM_NON_EXEMPT`. This is not a tie-break convention; it is D1 governing its own edge cases.

---

## Segmentation: what is one span

A span is **one atomic proposition**. Spans never overlap and never nest.

- *"Paris is France's capital and has 2.1 million residents"* → **two** spans. A conjunction of two
  checkable propositions is two claims, each of which would bind its own citation.
- *"The library is fast and well documented"* → **two** spans, both `checkable_evaluative`.
- *"Ortiz, which is packed in olive oil, is sold in most French supermarkets"* → **two** spans (the
  packing medium; the retail availability). A relative clause carrying its own checkable
  proposition is its own span.
- Do **not** split a single proposition across its own grammar: *"has 2.1 million residents"* is one
  claim, not one about *having residents* and one about *2.1 million*.

Every character of the text belongs to exactly one segment. Text that asserts nothing —
connectives, greetings, questions, instructions to the user, offers to continue — is `NOT_A_CLAIM`.
`NOT_A_CLAIM` is a *decision*, not a gap: it means you read the text and judged it to make no claim
about the world.

---

## The exempt regions

| Class | Exempt? | Rule |
|---|---|---|
| `code_body` | **yes** | Code the user is being offered to run. The exemption attaches to *code*, never to fencing. |
| `attributed_restatement` | **yes** | The user's own words repeated **with attribution** — *"you asked about X"*, *"you mentioned you use Y"*. The claim is about what the user said, which the turn record holds. |
| `derived_arithmetic` | **yes** | Arithmetic whose every input is itself cited. Computing 5 from a cited 2 and a cited 3 introduces no new world fact. |
| `connective_evaluative` | **yes** | Judgement over cited material that introduces **no externally checkable predicate of its own**. Comparatives and orderings over cited attributes qualify: *"the first is cheaper"* where both prices are cited. |
| `system_record` | **yes** | Claims about **this turn's own execution** — what was searched, what was retrieved, that nothing was found. Their referent is the turn record, not the world. Deliberately narrow: it covers no claim whose truth depends on anything outside the turn record. |

### And the eight ways text is *not* exempt

| Class | Rule |
|---|---|
| `factual_entity` | A checkable claim carrying a named entity or a figure. The ordinary case. |
| `factual_bare_predicate` | A checkable claim with **no named entity and no numeral** — *"this fish is high in mercury"*. This is the class the rejected draft of D1 missed entirely, and it is why default-deny exists. **A span in this class must contain no capitalised non-initial token and no digit**; the loader enforces it, because this class is what proves an entity-triggered extractor broken. |
| `prose_in_fence` | Prose placed inside a fence. A fence claiming a natural-language or unrecognized type, or one whose content does not parse as its declared language, is not an exempt region. Prose inside a fence is prose. |
| `nl_in_code` | A world-fact claim in a string literal, comment or docstring inside otherwise valid code. `print("Paris has 9 million residents")` parses cleanly as Python; a parse check alone would exempt it, making a string literal a delivery channel for an uncited assertion. |
| `dependency_declaration` | Imports, package manifest entries, install commands. Categorically not exempt — they are verified against the registry or documentation. This is the anti-squatting property that motivated covering coding turns at all. |
| `prose_about_code` | *Using* `httpx.AsyncClient` in code is a proposal to run and test. *Asserting* "it accepts `timeout=`" is a claim requiring a documentation source. **Use versus assert is the line.** |
| `checkable_evaluative` | *well regarded*, *safe*, *popular*, *recommended*, *reliable* — each is an externally checkable claim about the world, however evaluative it sounds. An earlier ADR draft used "are both well regarded" as its exemplar of *exempt* evaluation; that was wrong, and it was the common-knowledge trap reappearing one level down. |
| `unattributed_restatement` | The user's content presented as the model's **own** recommendation. Restatement is exempt because of the attribution, not because of the content. |

---

## Precedence, and the trap in it

**Overlap precedence is one-directional: non-exempt wins.** Where a span falls under both an exempt
region and the default-deny rule, the citation obligation stands. An exemption never rescues a span
that independently requires a source.

The worked case, which is also AC-4's probe: the user writes *"I'm using `fastapi-turbo`"*, and the
model replies *"You mentioned `fastapi-turbo` — I'd recommend `fastapi-turbo` for this."* The first
mention is `attributed_restatement` and exempt. The second is `unattributed_restatement` and is
**not**, because presenting the same content as the model's own recommendation is a new claim about
the world — that this package exists and is suitable. Label them separately. The restatement
exemption does not travel with the string.

Two more that catch labellers out:

- A dependency declaration **inside** an otherwise exempt code body is still
  `dependency_declaration`. The code exemption has a hole in it by design.
- *"Well regarded"* applied to something cited is still `checkable_evaluative`. Having a citation
  nearby does not make a new predicate exempt; it makes it a claim that needs its own.

---

## Worked disagreements

Recorded here as they recur, so the same call is made twice. Each entry names the resolution and
the rule it follows from.

1. **"This is the standard approach."** → `checkable_evaluative`. *Standard* is externally
   checkable — someone could establish whether it is.
2. **"I searched the web for tuna brands and found nothing relevant."** → `system_record`, exempt.
   The referent is this turn's record. But **"there is no reliable data on tuna mercury levels"**
   → `factual_bare_predicate`, not exempt: that is a claim about the world, not about the search.
3. **"Let me know if you'd like me to look further."** → `NOT_A_CLAIM`.
4. **A comment reading `# returns the parsed config`** → `NOT_A_CLAIM`. It describes the code being
   offered, not the world. But `# ISO 8601 requires a leading zero here` → `prose_about_code`,
   because it asserts something about a standard that a source would have to support.
5. **`import httpx`** → `dependency_declaration`. **`client = httpx.AsyncClient()`** → `code_body`.
6. **"At €4.20 and €3.80, the second is cheaper"** where both prices are cited →
   `connective_evaluative`, exempt. With the prices *uncited*, both figures are `factual_entity`.
