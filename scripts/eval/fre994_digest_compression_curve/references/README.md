# Hand-authored reference sets — FRE-994's ground truth

Eight sessions, the first eight of the stratified draw at seed 994. For each, a list of
the **consequential conclusions** the session reached, written by reading the full
transcript and **before any digest was generated**.

These files exist to be audited, not trusted. They are the only genuinely independent
ground truth in the study, and their independence is limited in a way worth stating
plainly at the top rather than in a footnote: **the same person wrote these, designed the
arms, and knows the decision thresholds.** Every validity gate in §4.3 of the plan
therefore checks whether two models reproduce *one person's reading* — not whether that
reading is right. If this reading systematically overlooks the kind of conclusion
compression destroys, every gate still passes.

What bounds that, and does not remove it:

- These files are committed, so the judgement can be checked against the transcripts.
- Items are written against ADR-0124 D3's own definition, quoted below, rather than
  against an ad-hoc sense of importance.
- They were written before any generation call was made. The run's records carry the
  timestamps.

## The definition applied

> A conclusion is CONSEQUENTIAL when a future reader who did not attend this session
> would repeat settled work without it, or would be misled about what was established,
> rejected, resolved or contradicted. Passing remarks, restatements of the question,
> pleasantries, and process narration are NOT consequential. Something the session left
> explicitly open IS consequential, because a reader who thinks it was settled is wrong.

The same words are quoted verbatim into the extractor and judge prompts
(`scoring.CONSEQUENTIAL_DEFINITION`), so all three are applying one definition.

## Reproducing the transcripts

The transcripts themselves are **not** committed. They are raw session text — in this
corpus that includes real deployment hostnames — and this repo does not carry log dumps.
Regenerate them from the durable capture store:

```bash
uv run python -m scripts.eval.fre994_digest_compression_curve.run_curve --dump-calibration
```

They land under `telemetry/fre994_curve/calibration-transcripts/` (gitignored).

## Redaction, and why it does not undermine the audit

**This repository is public, and these are the owner's own sessions.** Personal specifics are
therefore redacted from the committed items: locations and itineraries, named individuals, named
venues, dietary details, and — where a session named a real deployment host — the hostname, which is
replaced by its role ("the SLM relay host"). Each redacted file carries a `redaction` field saying so.

The redaction does not weaken what these files are for. The audit question is **which kinds of
statement were judged consequential** — a decision that was left open, a recommendation that was
made, a correction the assistant issued — and that survives intact. What is removed is the content
that identifies the owner's movements and associates, which was never the thing under review.

The unredacted items are reproducible: regenerate the transcripts with `--dump-calibration` and read
them against these files. The redacted spans are unambiguous — each session names one city, one
venue, one person.
