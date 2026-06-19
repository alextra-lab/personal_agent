/**
 * Guards the FRE-532 toolkit convergence (ADR-0089 Addendum A6).
 *
 * The PWA bundles its OWN pinned copies (npm) of the curated artifact toolkit's
 * rendering libs, at the versions in config/artifact_lib_substitution_map.json.
 * It must not regress back to prismjs/react-syntax-highlighter, and must keep
 * math (katex) + the highlight.js code highlighter aligned to the toolkit pins.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

// vitest runs from the seshat-pwa project root.
const pkg = JSON.parse(
  readFileSync(path.resolve(process.cwd(), 'package.json'), 'utf8'),
) as {
  dependencies: Record<string, string>;
  devDependencies: Record<string, string>;
  overrides?: Record<string, string>;
};

const all = { ...pkg.dependencies, ...pkg.devDependencies };

describe('toolkit convergence — versions aligned to the artifact /lib/ pins', () => {
  it('pins katex to the toolkit version (0.16.47)', () => {
    expect(pkg.dependencies.katex).toBe('^0.16.47');
  });

  it('pins highlight.js to the toolkit version (11.9.0)', () => {
    expect(pkg.dependencies['highlight.js']).toBe('11.9.0');
  });

  it('renders math via remark-math + rehype-katex', () => {
    expect(pkg.dependencies['remark-math']).toBeDefined();
    expect(pkg.dependencies['rehype-katex']).toBeDefined();
  });
});

describe('toolkit convergence — no divergent highlighter remains', () => {
  it('does not depend on react-syntax-highlighter or prismjs anywhere', () => {
    expect(all['react-syntax-highlighter']).toBeUndefined();
    expect(all['@types/react-syntax-highlighter']).toBeUndefined();
    expect(all.prismjs).toBeUndefined();
    expect(pkg.overrides?.prismjs).toBeUndefined();
  });
});
