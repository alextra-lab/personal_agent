/**
 * The Share button follows `can_share_link` only (FRE-1784 AC-1; ADR-0153 Amendment 1 L9).
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

import type { ArtifactSummary } from '@/lib/agui-client';

const getArtifactMetadata = vi.fn();
const listArtifacts = vi.fn();
const listShareLinks = vi.fn();
vi.mock('@/lib/agui-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/agui-client')>('@/lib/agui-client');
  return {
    ...actual,
    getArtifactMetadata: (...args: unknown[]) => getArtifactMetadata(...args),
    listArtifacts: (...args: unknown[]) => listArtifacts(...args),
    listShareLinks: (...args: unknown[]) => listShareLinks(...args),
    postCardClick: vi.fn(),
  };
});

import { ShareLinkError } from '@/lib/agui-client';
import { ArtifactCard } from '@/components/ArtifactCard';
import { ArtifactsIndex } from '@/components/ArtifactsIndex';

function meta(id: string, extra: Partial<ArtifactSummary>): ArtifactSummary {
  return {
    artifact_id: id,
    public_url: `https://artifacts.example.com/${id}`,
    slug: id,
    title: `Title ${id}`,
    summary: null,
    content_type: 'text/html; charset=utf-8',
    size_bytes: 10,
    tags: [],
    created_at: new Date().toISOString(),
    ...extra,
  };
}

beforeEach(() => {
  getArtifactMetadata.mockReset();
  listArtifacts.mockReset();
  listShareLinks.mockReset();
  listShareLinks.mockRejectedValue(new ShareLinkError(403, 'Forbidden'));
});

describe('ArtifactCard Share button', () => {
  it('shows Share when can_share_link is true, and opens the sheet', async () => {
    getArtifactMetadata.mockResolvedValue(meta('a-1', { can_share_link: true }));
    render(<ArtifactCard artifactId="a-1" fallbackHref="https://artifacts.example.com/a-1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Share' }));
    expect(screen.getByRole('dialog', { name: 'Share Title a-1' })).toBeInTheDocument();
  });

  it.each([
    ['false', { can_share_link: false }],
    ['missing', {}],
  ])('shows no Share when can_share_link is %s', async (_label, extra) => {
    getArtifactMetadata.mockResolvedValue(meta('a-2', extra));
    render(<ArtifactCard artifactId="a-2" fallbackHref="https://artifacts.example.com/a-2" />);
    await screen.findByRole('button', { name: 'Expand' });
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();
  });
});

describe('ArtifactsIndex Share button and Shared links link', () => {
  it('shows Share on a row only where can_share_link is true, and the Shared links link', async () => {
    listArtifacts.mockResolvedValue([
      meta('a-1', { can_share_link: true }),
      meta('a-2', { can_share_link: false }),
    ]);
    render(<ArtifactsIndex />);
    expect(await screen.findAllByRole('button', { name: 'Share' })).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'Shared links' })).toHaveAttribute(
      'href',
      '/shared-links',
    );
  });

  it('shows no Shared links link for a caller whom the list endpoint refuses', async () => {
    listArtifacts.mockResolvedValue([meta('a-2', { can_share_link: false })]);
    listShareLinks.mockRejectedValue(new ShareLinkError(403, 'Forbidden'));
    render(<ArtifactsIndex />);
    await screen.findByText('Title a-2');
    await vi.waitFor(() => expect(listShareLinks).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Shared links' })).not.toBeInTheDocument();
  });

  it('shows the Shared links link to a creator with no shareable item on this page', async () => {
    listArtifacts.mockResolvedValue([meta('a-2', { can_share_link: false })]);
    listShareLinks.mockResolvedValue([]);
    render(<ArtifactsIndex />);
    expect(await screen.findByRole('link', { name: 'Shared links' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();
  });
});
