/**
 * FRE-1264 AC-3 — "Component sources contain no direct slate or blue colour
 * utilities for background and body text; those resolve through the custom
 * properties."
 *
 * A static text scan of the converted sources, not a rendered-DOM check:
 * robust against jsdom's inability to resolve CSS custom properties, and it
 * catches a regression the moment someone reaches for `bg-slate-900` again
 * rather than only when a snapshot happens to render that element.
 *
 * One documented exception: MarkdownContent.tsx's CodeBlock stays a fixed
 * dark island in both themes (the highlight.js github-dark theme it wraps
 * assumes a dark background) — bracketed by `palette-allowlist:start/end`
 * comments and stripped before the scan, per AC-3's "must be written down
 * in the PR rather than left implicit."
 */

import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, it, expect } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC_ROOT = resolve(__dirname, '..');

// The files FRE-1264 converted to the theme-aware token system — the
// ticket's own 6-component + layout scope, plus ChatInput.tsx and
// TurnSummaryPanel.tsx (scope expanded during build so light mode isn't a
// dark composer/tool-summary floating on a light page).
const CONVERTED_FILES = [
  'app/layout.tsx',
  'components/ChatMessage.tsx',
  'components/MarkdownContent.tsx',
  'components/ToolIndicator.tsx',
  'components/StreamingChat.tsx',
  'components/TurnStatusBar.tsx',
  'components/SessionList.tsx',
  'components/ChatInput.tsx',
  'components/TurnSummaryPanel.tsx',
  // FRE-1265 — the remaining hardcoded-dark surfaces.
  'components/ApprovalModal.tsx',
  'components/DecisionCard.tsx',
  'components/ClassifiedErrorCard.tsx',
  'components/ModelPicker.tsx',
  'components/PhaseIndicator.tsx',
  'components/TurnRating.tsx',
  'components/LocationConsent.tsx',
  'components/ArtifactViewer.tsx',
  'components/ArtifactExportMenu.tsx',
  'components/ArtifactCard.tsx',
  'components/ArtifactsIndex.tsx',
  'components/ObserveView.tsx',
  'components/MermaidBlock.tsx',
];

// Matches bg-/text-/border-/ring-/divide-/from-/to- slate or blue utilities,
// with an optional Tailwind opacity suffix (e.g. `border-slate-700/50`).
// ring-/divide-/from-/to- added in FRE-1265 (MermaidBlock.tsx's
// ring-slate-700/30 and from-slate-900/60 to-slate-900/20, ArtifactsIndex's
// divide-slate-800/60) — none of the FRE-1264 CONVERTED_FILES use these
// prefixes, confirmed before widening so this doesn't regress them.
const DIRECT_PALETTE_UTILITY =
  /\b(?:bg|text|border|ring|divide|from|to)-(?:slate|blue)-\d{2,3}(?:\/\d{1,3})?\b/g;

function stripAllowlistedRegions(source: string): string {
  return source.replace(
    /\/\/\s*palette-allowlist:start[\s\S]*?\/\/\s*palette-allowlist:end/g,
    '',
  );
}

describe('AC-3 — palette centralization', () => {
  it.each(CONVERTED_FILES)('%s uses no direct slate/blue background or text utilities', (relPath) => {
    const source = readFileSync(resolve(SRC_ROOT, relPath), 'utf-8');
    const scanned = stripAllowlistedRegions(source);
    const matches = scanned.match(DIRECT_PALETTE_UTILITY) ?? [];
    expect(matches).toEqual([]);
  });

  it('the CodeBlock allowlist region actually contains what it exists to exempt', () => {
    // A guard against the allowlist itself going stale/vacuous — if the
    // markers stop bracketing any slate usage, they're dead weight and the
    // real exemption might have leaked outside them undetected.
    const source = readFileSync(resolve(SRC_ROOT, 'components/MarkdownContent.tsx'), 'utf-8');
    const region = source.match(
      /\/\/\s*palette-allowlist:start[\s\S]*?\/\/\s*palette-allowlist:end/,
    )?.[0];
    expect(region).toBeDefined();
    expect(region!.match(DIRECT_PALETTE_UTILITY)?.length ?? 0).toBeGreaterThan(0);
  });

  it('the MermaidBlock allowlist region actually contains what it exists to exempt', () => {
    // Same guard as above (FRE-1265): the rendered diagram canvas stays a
    // fixed-dark island — mermaid's own theme (hardcoded hex passed to
    // mermaid.initialize, not a Tailwind utility) assumes a dark backdrop.
    // The source/error fallback reuses CodeBlock's bg-[#0d1117] arbitrary
    // value directly and needs no allowlist — it was never a slate/blue
    // utility to begin with.
    const source = readFileSync(resolve(SRC_ROOT, 'components/MermaidBlock.tsx'), 'utf-8');
    const region = source.match(
      /\/\/\s*palette-allowlist:start[\s\S]*?\/\/\s*palette-allowlist:end/,
    )?.[0];
    expect(region).toBeDefined();
    expect(region!.match(DIRECT_PALETTE_UTILITY)?.length ?? 0).toBeGreaterThan(0);
  });
});
