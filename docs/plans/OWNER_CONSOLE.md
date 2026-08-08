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
| **Deploy — everything else**: `seshat-gateway` rebuild · ES type-change or reindex · Postgres schema/migration · cost, budget or governance | `standing-approved` | 2026-08-06 · owner | — (top of ladder) | a standing-class deploy causes an incident |
| **Observe the other sessions** — read seat panes, worktrees and dispatch state | `standing-approved` | 2026-08-06 · owner | — (top of ladder) | observation is used to actuate a seat outside the dispatch contract |
| **Dispatch actuation posture** — daemon on/off + mode | `do-and-report` | 2026-07-31 · owner | dispatch runs a sustained period needing no owner correction | an unreported flip, or a flip that strands a seat |
| **On-merge board transition** — merged ticket → `Awaiting Deploy` | `standing-approved` | 2026-07-31 · owner | — (top of ladder) | a transition is missed and the board drifts |
| **Merge to `main`** | `standing-approved` | 2026-07-31 · owner | — (top of ladder) | a merge lands that the gate should have bounced |
| **Console write** | `do-and-report` — transcription and mechanical retirement **only**; **authoring is forbidden at every level** | 2026-07-31 · owner | — (authoring never promotes) | master authors, or retires without a met condition |

## Standing directives

- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Then, in order: telemetry residuals (FRE-983, ES lifecycle, parked mid-phase) · Configuration Management · Linear async feedback · Seshat Inference. · Retires: all four reach Done in Linear
- [2026-08-06 · owner | relayed 2026-08-06] "Observability project is what we are going to address after the VPS project is reduced to its natural smallest size." · Retires: the Observability Foundation project reaches Done in Linear
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Backlog cull — scope and gate are still to be set. · Retires: a Linear ticket recording the cull scope reaches Approved
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Personal data is already committed to the public repo; the owner sets the redaction scope — redaction alone leaves it in git history. · Retires: a Linear ticket recording the redaction scope reaches Approved
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Master-authored changes to `src/` or a security boundary have no independent gate — route them through a build seat. · Retires: a merged PR adds an independent gate for master-authored `src` changes
- [2026-07-31 · relayed 2026-07-31 (FRE-1085 migration)] Watch `main_inference`'s caps; if one fires, ask whether the spend is real — do not raise the cap. · Retires: a Linear ticket or ADR records the `main_inference` caps decision
- [2026-08-08 · owner | relayed 2026-08-08] "Retire kibana focus on grafana. I will tell you when kibana is retired." New dashboards and alerting go to Grafana; Kibana's actual retirement is the owner's to declare, not a session's to infer. · Retires: FRE-1214 (T9 — Retire Kibana) reaches Done in Linear
