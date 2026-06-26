/**
 * Tests for initAguiConfig + SESHAT_API live binding (FRE-339).
 *
 * Proves that external consumers of SESHAT_API observe the post-init value
 * via ES module live bindings (no snapshot at import time).
 */

import { vi, describe, it, expect, beforeEach } from 'vitest';

describe('initAguiConfig + SESHAT_API live binding', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('SESHAT_API starts at localhost default', async () => {
    const { SESHAT_API } = await import('@/lib/agui-client');
    expect(SESHAT_API).toBe('http://localhost:9000');
  });

  it('initAguiConfig updates SESHAT_API observed by re-import', async () => {
    const client = await import('@/lib/agui-client');
    client.initAguiConfig('https://agent.example.com', 'tok');
    // Re-import same module — live binding should reflect updated value.
    const { SESHAT_API } = await import('@/lib/agui-client');
    expect(SESHAT_API).toBe('https://agent.example.com');
  });

  it('initAguiConfig is exported', async () => {
    const { initAguiConfig } = await import('@/lib/agui-client');
    expect(typeof initAguiConfig).toBe('function');
  });
});
