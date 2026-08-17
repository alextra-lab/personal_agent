# Last session — 2026-08-16/17 (four acceptance criteria that measured the wrong thing)

## Doing / discussing

**FRE-1269 is the only open work and it is parked on an owner action, not on us.** The owner must run a
live experiment on their phone — 5 taps on the header title opens a diagnostic overlay, then a button
applies a candidate `100vh` fix as an inline style and self-reverts. Their screenshot decides whether
the fix ships or is ruled out. Everything else from two days is Done and deployed. Do not close FRE-1269
on a green suite; that gate is the owner's and it is the whole point of the ticket.

## What was decided and why

**Four of master's acceptance criteria failed the same way, and the build seats implemented each one
faithfully.** FRE-1263's AC-4 specified a three-row textarea, which was simply wrong against the
reference. FRE-1264 carried nine criteria and not one measured geometry — it verified palettes, fonts,
contrast and cache names while never asserting the page fits on the screen. FRE-1266's AC-4 named the
`footer`, which was already flush, instead of the card inside it. FRE-1267's AC-1 named the right element
but forced a synthetic 34px inset in a harness that reports zero. **Every one checked the source, or a
synthetic condition, rather than the rendered result on the owner's device.** Do not read the repeated
PWA bounces as build-seat quality; the specs were the defect.

**Nothing in this repository can observe the mode the owner actually runs.** Headless Chromium reports
`env(safe-area-inset-bottom)` as 0 and cannot emulate standalone `display-mode`, so a headless screenshot
is flush by construction whether or not the bug is present. This was stated plainly rather than papered
over, and it is why three green attempts shipped nothing. The owner **declined** an iOS device harness
when offered — correctly; a device cloud is not what this project should grow. Their screenshots are the
instrument. Do not re-propose the harness.

**Instructions in ticket comments do not get executed.** The seeded-negative requirement was written into
ticket comments twice and dropped both times; the identical instruction on a **PR/bounce comment** landed
immediately. Lifecycle-rules designates ticket comments read-for-context and never instructions — the
contract was working and master was using the wrong channel. This was misread as seat indiscipline for a
day and a half before the channel was identified as the variable.

**The seeded-negative practice paid for itself on FRE-1266 and is now evidence-backed rather than
theoretical.** Master's diagnosis there (missing `min-h-0`) was necessary but not load-bearing; the
required pre-change test kept failing after it, which surfaced the real cause — `sr-only` spans resolving
their containing block to the root div and inflating the document's own `scrollHeight`. No amount of
source reading produces that. FRE-1255 remains the natural vehicle for making the rule structural.

**A GitHub 503 reported a merge as failed that had actually applied server-side.** During an outage, check
PR state rather than trusting the command's exit code — blindly retrying would have made a mess.

## Worktrees — anything special

**build1** holds the full FRE-1269 context: the WebKit-301994 research, the codex review transcript, and
the diagnostic tooling design. Its own handoff asks to **keep** that context if the next pickup is the
owner's experiment round. Re-deriving it cold would be wasteful. build2 and adr are idle and clean.

## Sequence position + drift

**The entire two days ran off the console's Observability directive, deliberately and at the owner's
direction** — the PWA arc began with "i want Seshat's UI to have the same layout and font size/design as
Claude Code" and never returned to Observability. That is an owner-directed deviation, not drift. No
console write this session; the file sits at 41 of its 60-line bound and carries no commits from these
two days.

## Answers for the fresh start

- **Why is FRE-1269 sitting in Awaiting Deploy?** Deliberately. It is merged and deployed; the owner's
  screenshot validation is the closing condition, and it is an owner action that cannot be done for them.
  The exact experiment steps are in its ticket comments.
- **Why is the owner's app in a worse state than this morning?** Master advised re-adding the home-screen
  icon to activate standalone mode. It worked — and our layout handles standalone worse than the broken
  state it replaced. Master caused that; the Safari-tab path still renders correctly and is the working
  fallback. Say so plainly if asked rather than reframing it.
- **Is the 62px gap ours?** Unresolved, and that is the live question. Measured on device: viewport 812,
  screen 874, composer and footer both at 812 — so the layout is flush to a viewport that stops short of
  the screen. `100vh` alone reports 874. Codex's objection is that a probe proves the unit *resolves*,
  not that content painted there is *visible* — WebKit 301994's own distinction. The experiment settles it.
- **Why three diagnostic PRs and no fix on FRE-1269?** Each was gated on that reasoning, not dithering.
  The third added a live toggle so the owner tests without a deploy cycle.
- **Open for the owner, unchanged:** FRE-1255 (ADR-0137 mapping re-audit, and the vehicle for the
  seeded-negative rule) · FRE-1268 (flaky ModelPicker contrast assertion, Backlog) · whether
  screenshot-validation becomes a standing console directive for PWA visual work.
