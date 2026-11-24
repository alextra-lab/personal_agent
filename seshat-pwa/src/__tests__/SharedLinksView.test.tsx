/**
 * Tests for the Shared links view (FRE-1784 AC-5; ADR-0153 Amendment 1 L9).
 */

import { render, screen, fireEvent, within } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const listShareLinks = vi.fn();
const revokeShareLink = vi.fn();
vi.mock('@/lib/agui-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/agui-client')>('@/lib/agui-client');
  return {
    ...actual,
    listShareLinks: (...args: unknown[]) => listShareLinks(...args),
    revokeShareLink: (...args: unknown[]) => revokeShareLink(...args),
  };
});

import { ShareLinkError } from '@/lib/agui-client';
import { SharedLinksView } from '@/components/SharedLinksView';

const LINKS = [
  {
    link_id: 'l-2',
    artifact_id: 'a-2',
    artifact_title: 'Budget sheet',
    created_at: '2026-11-24T10:00:00Z',
    expires_at: '2026-12-24T10:00:00Z',
    view_count: 0,
    last_viewed_at: null,
  },
  {
    link_id: 'l-1',
    artifact_id: 'a-1',
    artifact_title: null,
    created_at: '2026-11-23T10:00:00Z',
    expires_at: '2026-11-30T10:00:00Z',
    view_count: 3,
    last_viewed_at: '2026-11-24T09:00:00Z',
  },
];

beforeEach(() => {
  listShareLinks.mockReset();
  revokeShareLink.mockReset();
});

describe('SharedLinksView', () => {
  it('lists one row per active link with title, expiry, view count and last view', async () => {
    listShareLinks.mockResolvedValue(LINKS);
    render(<SharedLinksView />);
    const rows = await screen.findAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText('Budget sheet')).toBeInTheDocument();
    expect(within(rows[0]).getByText('0 views')).toBeInTheDocument();
    expect(within(rows[0]).getByText(/Last view: never/)).toBeInTheDocument();
    expect(within(rows[0]).getByText(/Expires/)).toBeInTheDocument();
    expect(within(rows[1]).getByText('Untitled')).toBeInTheDocument();
    expect(within(rows[1]).getByText('3 views')).toBeInTheDocument();
    expect(within(rows[1]).queryByText(/Last view: never/)).not.toBeInTheDocument();
  });

  it('Revoke sends one DELETE and removes the row', async () => {
    listShareLinks.mockResolvedValue(LINKS);
    revokeShareLink.mockResolvedValue(undefined);
    render(<SharedLinksView />);
    const rows = await screen.findAllByRole('listitem');
    fireEvent.click(within(rows[1]).getByRole('button', { name: 'Revoke' }));
    await screen.findByText('Budget sheet');
    await vi.waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(1));
    expect(revokeShareLink).toHaveBeenCalledTimes(1);
    expect(revokeShareLink).toHaveBeenCalledWith('l-1');
  });

  it('a failed revoke keeps the row and shows the API text', async () => {
    listShareLinks.mockResolvedValue(LINKS);
    revokeShareLink.mockRejectedValue(new ShareLinkError(503, 'Service Unavailable'));
    render(<SharedLinksView />);
    const rows = await screen.findAllByRole('listitem');
    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Revoke' }));
    expect(await within(rows[0]).findByRole('alert')).toHaveTextContent('Service Unavailable');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('says so when there is no active link', async () => {
    listShareLinks.mockResolvedValue([]);
    render(<SharedLinksView />);
    expect(await screen.findByText('No active links.')).toBeInTheDocument();
  });

  it('a 403 says that only a link creator sees links', async () => {
    listShareLinks.mockRejectedValue(new ShareLinkError(403, 'Forbidden'));
    render(<SharedLinksView />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Only a link creator can see shared links.',
    );
  });

  it('another failure shows the API text', async () => {
    listShareLinks.mockRejectedValue(new ShareLinkError(500, 'share link request failed: 500'));
    render(<SharedLinksView />);
    expect(await screen.findByRole('alert')).toHaveTextContent('share link request failed: 500');
  });
});
