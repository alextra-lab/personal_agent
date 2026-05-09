# Personal vault reflection — paused for memory-gap audit

> **Status**: FRE-227 paused 2026-05-09. This note preserves the design-space work done before the pause.
> See also: the cross-session continuity audit ticket (filed same day) for the parallel thread.

---

## What we were trying to build

A persistent, internet-accessible **markdown vault** that both the agent and the user can read and write — long-running curated knowledge: ideas, ongoing research, discussion documents, agent thought-trails. Mental model: Karpathy's LLM Wiki, Obsidian-style.

Requirements (from user):

- "Discussion documents, notes, ideas — so not git" (clarified: a separate dedicated wiki repo separate from this codebase is fine)
- "Like an Obsidian vault. … like Andrej Karpathy's LLM Wiki"
- "I need to be able to read them as well as the agent"
- Internet-accessible (iPad-readable specifically)
- "More functional than technical" — content matters; agent and user co-author
- Subjects could include: recipes, research, journal, project notes, decisions

---

## The key reframing: two needs hidden inside one ticket

Midway through the design session (2026-05-09), the user identified that FRE-227 conflates two distinct concerns:

### Need 1 — Personal knowledge vault (you-as-author, agent-as-collaborator)

Content: recipes, research, journal, ongoing project notes, decision drafts.
- **You** are the primary author and curator.
- The **agent** is a collaborator: amends, drafts, links, summarises, when asked.
- Markdown, hierarchical (folders), free-form prose.
- iPad-readable.
- This is the Obsidian-vault / Karpathy-LLM-wiki use case.

### Need 2 — Agent cross-session continuity (agent-as-author, you-as-occasional-reader)

Content: "things to remember next session", in-progress reasoning, context the agent wants to carry forward, conversation snippets it found valuable.
- The **agent** is the primary author.
- **You** read it occasionally for transparency.
- Original FRE-227 ticket language ("notes-to-self, thought trails") points at this.

These share a tool ("agent writes to a path") but diverge sharply on storage, organisation, and — critically — whether existing systems already serve them.

---

## Does Need 2 already exist?

Mostly yes. Quick audit:

| Need-2 concern | Already covered by | Gap? |
|---|---|---|
| Remember a fact about the user | Neo4j semantic memory + entity extraction (`memory/service.py`) | No |
| Remember an interaction | Episodic memory + Captain's Log captures (`captains_log/`) | No |
| Distil patterns across sessions | Insights engine (`insights/`) — delegation, cost, freshness, skill routing | No |
| Self-reflection on tasks | DSPy reflection loop in Captain's Log; `hit_iteration_limit` signal (FRE-301) | No |
| "Ongoing reasoning preserved verbatim" | Nothing — memory abstracts to entities; Captain's Log is event-shaped, not prose | **Yes, small** |
| "Notes to the next session about a project" | Partial — semantic memory covers facts, not narrative | **Yes, narrative-shaped** |
| "Agent notes mid-task before delegating" | Nothing | **Yes, but unclear if needed in practice** |

The gaps are real but small. Whether they warrant a new tool depends on whether they're actually felt in practice — hence the cross-session continuity audit.

---

## Storage / sync design space explored for Need 1

Three viable options were analysed. All expose the same `/app/agent_workspace/vault/` path to the agent; they differ only in how the user reads + edits from Mac/iPad.

### Option A — GitHub-hosted private repo

- VPS clones `alextra-lab/personal_agent_vault`; agent writes via commit + push.
- User reads/edits via GitHub Mobile, Working Copy (iPad), Obsidian + Git plugin (Mac), github.com.
- Strengths: per-edit diff review (matches user's existing review pattern), mirrors `personal_agent_secrets` deploy pattern, free, decoupled from home network, Karpathy LLM Wiki exact match.
- Weaknesses: ~1–3s per push, possible push conflict if user + agent edit simultaneously.

### Option B — Syncthing (Mac ↔ iOS ↔ VPS)

- Daemon on each device; native filesystem; E2E encrypted.
- Strengths: lowest latency, best Obsidian-vault parity, no cloud vendor.
- Weaknesses: no audit history, three new daemons to run, paid Möbius Sync on iOS, last-write-wins conflicts.

### Option C — Synology file server (already owned)

- VPS mounts share via SMB/NFS over Tailscale or CF Tunnel.
- Strengths: already-owned hardware, mature iOS apps (DS File / Synology Drive).
- Weaknesses: couples vault availability to home-network reachability (contradicts VPS-canonical philosophy from FRE-214), SMB-in-Docker is finicky.

### Comparison

| Aspect | A) GitHub | B) Syncthing | C) Synology |
|---|---|---|---|
| Per-edit audit | ✅ first-class diffs | ❌ none | ⚠️ snapshots only |
| iPad UX | ✅ Working Copy / GitHub | ⚠️ Möbius (paid) | ✅ DS File / Synology Drive |
| Mac UX | ✅ Obsidian + Git | ✅ native + Obsidian | ✅ Synology Drive Client |
| VPS reliability | ✅ GitHub uptime | ✅ peer-to-peer | ⚠️ home-network-bound |
| Write latency | 1–3s | sub-second | sub-second on LAN |
| Conflict resolution | rebase | `.sync-conflict-*` | last-write-wins |
| Cost | free | free + Möbius $5 once | already owned |
| Decouples from home network | ✅ | ✅ | ❌ |
| Karpathy LLM Wiki parity | ✅ exact | ⚠️ similar | ⚠️ different |

**Current bias at pause**: Option A (GitHub). Reason: per-edit diff review, reuses `personal_agent_secrets` pattern, decoupled from home network. But the right choice may shift depending on what the memory-gap audit reveals about how much the agent actually needs to write.

---

## What was ready to ship if paused had not happened

The agent-side tool surface was fully designed and reuses existing plumbing:

- `notes_write` / `notes_read` / `notes_list` / `notes_search` primitives, path-governed via `tools/primitives/_governance.py:_check_path_governance()`.
- `agent_notes_embeddings` pgvector table (migration `0003`, 1024-dim, new table alongside the unused legacy `embeddings` table which has a dim mismatch).
- `NotesIndex` class (upsert + top-k search using `memory.embeddings.generate_embedding()`).
- `VaultReindexJob` brainstem job (following FRE-335 `SkillRoutingThresholdMonitor` pattern).
- ADR-0067 (Personal Knowledge Vault) to pin the design.
- Skill doc for FRE-326 hybrid routing.

Confidence level on the above: high. The agent-side components are straightforward and not at risk of being redesigned regardless of which storage/sync option is chosen.

---

## Open questions at pause

1. Does Need 1 (personal wiki) become clearer once Need 2 (cross-session continuity) gaps are concretely known?
2. Which sync option (GitHub / Syncthing / Synology) fits the actual editing pattern once the user has reflected?
3. Should the agent write to the vault proactively (e.g. end-of-session summary), or only reactively (when explicitly asked)?
4. Frequency of agent writes — if the agent writes 1–2 notes per session, all three storage options are equivalent; if 20+, GitHub's commit-per-write gets chatty.
5. Does Obsidian desktop + Git plugin become part of the Mac editing setup, or is GitHub web/Working Copy enough?

---

## Related work (unblocked by FRE-227 completion)

- **FRE-226** (agent self-updating skills) — needs FRE-227 as a write-surface foundation.
- **FRE-328-Phase-3** (auto-author skill from `missing_skill_requested` events) — also needs a writable surface.
- **Legacy `embeddings` table dim cleanup** — separate ticket when any feature needs pgvector (could be FRE-227 Phase 2, or earlier if another feature lands first).
