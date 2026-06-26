/**
 * Tests for RuntimeConfigProvider (FRE-339).
 *
 * Verifies that initAguiConfig is called with the props before children
 * make API calls, and that children are rendered.
 */

import { render, screen } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock initAguiConfig — we test that it gets called with the right values.
// vi.mock is hoisted so we cannot reference a const declared above it;
// use vi.fn() inline and retrieve the mock via vi.mocked() after import.
vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  initAguiConfig: vi.fn(),
}));

import { initAguiConfig } from '@/lib/agui-client';
import { RuntimeConfigProvider } from '@/components/RuntimeConfigProvider';

const mockInitAguiConfig = vi.mocked(initAguiConfig);

describe('RuntimeConfigProvider', () => {
  beforeEach(() => {
    mockInitAguiConfig.mockClear();
  });

  it('renders children', () => {
    render(
      <RuntimeConfigProvider seshatUrl="http://test:9000" gatewayToken="tok">
        <span>child content</span>
      </RuntimeConfigProvider>,
    );
    expect(screen.getByText('child content')).toBeTruthy();
  });

  it('calls initAguiConfig with seshatUrl and gatewayToken', () => {
    render(
      <RuntimeConfigProvider seshatUrl="https://agent.example.com" gatewayToken="secret">
        <div />
      </RuntimeConfigProvider>,
    );
    expect(mockInitAguiConfig).toHaveBeenCalledWith('https://agent.example.com', 'secret');
  });

  it('calls initAguiConfig exactly once on mount', () => {
    render(
      <RuntimeConfigProvider seshatUrl="http://x:9000" gatewayToken="">
        <div />
      </RuntimeConfigProvider>,
    );
    expect(mockInitAguiConfig).toHaveBeenCalledTimes(1);
  });

  it('re-calls initAguiConfig when props change', () => {
    const { rerender } = render(
      <RuntimeConfigProvider seshatUrl="http://a:9000" gatewayToken="t1">
        <div />
      </RuntimeConfigProvider>,
    );
    rerender(
      <RuntimeConfigProvider seshatUrl="http://b:9000" gatewayToken="t2">
        <div />
      </RuntimeConfigProvider>,
    );
    expect(mockInitAguiConfig).toHaveBeenLastCalledWith('http://b:9000', 't2');
  });
});
