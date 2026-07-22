/**
 * Tests for the root route (FRE-938 — server fallback when the
 * localStorage key is missing).
 */

import { render, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockReplace = vi.fn();
const mockResolveLastSessionId = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: mockReplace }),
}));

vi.mock('@/lib/session', () => ({
  resolveLastSessionId: (...args: unknown[]) => mockResolveLastSessionId(...args),
}));

vi.mock('@/lib/uuid', () => ({
  generateUUID: () => 'freshly-minted-uuid',
}));

import Home from '@/app/page';

beforeEach(() => {
  mockReplace.mockReset();
  mockResolveLastSessionId.mockReset();
});

describe('root route — session resolution (AC-4)', () => {
  it('navigates to the most recent session when one is resolved', async () => {
    mockResolveLastSessionId.mockResolvedValue('resolved-session-id');

    render(<Home />);

    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/c/resolved-session-id'));
  });

  it('mints a new session when the user has none at all', async () => {
    mockResolveLastSessionId.mockResolvedValue(undefined);

    render(<Home />);

    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/c/freshly-minted-uuid'));
  });
});
