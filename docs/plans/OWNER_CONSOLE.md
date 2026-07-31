# Owner Console

> **Authorship contract (ADR-0131 D2) — the owner writes; master transcribes and retires.** Master may
> append a directive the owner gave conversationally, **verbatim, attributed and dated**, and may delete
> one **only when its stated retirement condition is met, citing that condition in the commit**. Master
> never authors content here. Only the owner moves a ladder row.
> **Size bound: 60 lines.** Exceeding it is a contract violation to **surface to the owner** — never a
> compaction chore. This file has no growth engine by construction; if it grows, the contract broke.
> Nothing with an authoritative home elsewhere belongs here (D1): derived state → the dispatch resolver,
> Linear, git, the health probe; analysis and findings → a ticket or a research document.

**Record schemas (ADR-0131 AC-4).**
Directive — `- [YYYY-MM-DD · owner | relayed <session-date>] <text> · Retires: <event>`, where
`<event>` is **objectively decidable** (a merge, a date, a deploy, a measured threshold).
"When no longer needed" is not a retirement condition and the directive is refused.
Ladder row — class · level · dated grant · promotes-when · demotes-when.

## Trust ladder — the single source of standing authority (D3)

A grant exists **iff** this table records it. Skills and lifecycle-rules describe *mechanics*; they no
longer *grant* authority. Levels: `ask-first` → `do-and-report` → `standing-approved`.

| Action class | Level | Granted | Promotes when | Demotes when |
|---|---|---|---|---|
| **Deploy — reversible classes**: PWA-only rebuild · additive ES template (no type change) · Kibana dashboard import | `standing-approved` | 2026-06-26 · owner | — (top of ladder) | a standing-class deploy causes an incident |
| **Deploy — everything else**: `seshat-gateway` rebuild · ES type-change or reindex · Postgres schema/migration · cost, budget or governance | `ask-first` | 2026-06-26 · owner | a class accrues a clean track record the owner accepts | — (bottom of ladder) |
| **Dispatch actuation posture** — daemon on/off + mode | `do-and-report` | 2026-07-31 · owner | dispatch runs a sustained period needing no owner correction | an unreported flip, or a flip that strands a seat |
| **On-merge board transition** — merged ticket → `Awaiting Deploy` | `standing-approved` | 2026-07-31 · owner | — (top of ladder) | a transition is missed and the board drifts |
| **Merge to `main`** | `standing-approved` | 2026-07-31 · owner | — (top of ladder) | a merge lands that the gate should have bounced |
| **Console write** | `do-and-report` — transcription and mechanical retirement **only**; **authoring is forbidden at every level** | 2026-07-31 · owner | — (authoring never promotes) | master authors, or retires without a met condition |

## Standing directives

- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Then, in order: telemetry residuals (FRE-983, ES lifecycle, parked mid-phase) · Configuration Management · Linear async feedback · Seshat Inference. · Retires: all four land
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Accept ADR-0129, or hold ADR-0128 — the largest open call. · Retires: the ADR-0128-vs-0129 decision is recorded on either ADR
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] FRE-1039 (Grafana over Postgres) is decided together with ADR-0129, never separately. · Retires: ADR-0129 is accepted or rejected
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Backlog cull — scope and gate are still to be set. · Retires: the cull scope is set
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Personal data is already committed to the public repo; the owner sets the redaction scope — redaction alone leaves it in git history. · Retires: the redaction scope is set
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Master-authored changes to `src/` or a security boundary have no independent gate — route them through a build seat. · Retires: an independent gate for master-authored `src` changes exists
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Keep bare `FRE-XXXX` out of docs PR titles, bodies and branch names — a docs PR naming a ticket moves it. · Retires: FRE-1086 merges
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Watch `main_inference`'s caps; if one fires, ask whether the spend is real — do not raise the cap. · Retires: the `main_inference` caps decision is taken
