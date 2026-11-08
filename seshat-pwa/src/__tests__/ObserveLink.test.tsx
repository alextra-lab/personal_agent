/**
 * Tests for ObserveLink (FRE-1754).
 *
 * The link opens Observe (Grafana) in a new tab. The address comes from the
 * runtime config, so the public repo holds no domain. With no valid address
 * the link does not render.
 */

import { render, screen } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  initAguiConfig: vi.fn(),
}));

import { ObserveLink } from '@/components/ObserveLink';
import { RuntimeConfigProvider } from '@/components/RuntimeConfigProvider';

function renderWith(observeUrl: string | undefined) {
  return render(
    <RuntimeConfigProvider seshatUrl="http://test:9000" gatewayToken="" observeUrl={observeUrl}>
      <ObserveLink />
    </RuntimeConfigProvider>,
  );
}

describe('ObserveLink', () => {
  it('links to the configured address and opens a new tab', () => {
    renderWith('https://observe.example.com');
    const link = screen.getByRole('link', { name: /grafana/i });
    expect(link.getAttribute('href')).toBe('https://observe.example.com');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it('renders nothing when no address is configured', () => {
    renderWith(undefined);
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('renders nothing when the address is empty', () => {
    renderWith('');
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('renders nothing without a provider', () => {
    render(<ObserveLink />);
    expect(screen.queryByRole('link')).toBeNull();
  });

  it.each(['javascript:alert(1)', 'data:text/html,x', 'not a url', 'observe.example.com'])(
    'renders nothing for the unsafe address %s',
    (bad) => {
      renderWith(bad);
      expect(screen.queryByRole('link')).toBeNull();
    },
  );
});
