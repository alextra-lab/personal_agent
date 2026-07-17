/**
 * Tests for ArtifactViewer sandbox posture (ADR-0089 D2/D3 / FRE-510).
 *
 * The iframe must carry sandbox="allow-scripts" — and never
 * allow-same-origin, which would lift the opaque origin and defeat the
 * ADR-0089 isolation model.
 */

import { render, fireEvent, screen } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

const postCardClick = vi.fn();
vi.mock('@/lib/agui-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/agui-client')>(
    '@/lib/agui-client',
  );
  return {
    ...actual,
    postCardClick: (...args: unknown[]) => postCardClick(...args),
    // ArtifactExportMenu (rendered for HTML) imports this; stub it out.
    fetchArtifactExport: vi.fn(),
  };
});

import { ArtifactViewer } from '@/components/ArtifactViewer';

const PUBLIC_URL = 'https://artifacts.example.com/test-artifact-id';

function renderViewer(contentType = 'text/html; charset=utf-8') {
  return render(
    <ArtifactViewer
      artifactId="test-artifact-id"
      publicUrl={PUBLIC_URL}
      title="Test Artifact"
      contentType={contentType}
      onClose={vi.fn()}
    />,
  );
}

describe('ArtifactViewer — iframe sandbox posture (ADR-0089)', () => {
  it('grants exactly allow-scripts', () => {
    const { container } = renderViewer();
    const iframe = container.querySelector('iframe');
    expect(iframe).not.toBeNull();
    expect(iframe).toHaveAttribute('sandbox', 'allow-scripts');
  });

  it('never grants allow-same-origin (opaque-origin invariant)', () => {
    const { container } = renderViewer();
    const iframe = container.querySelector('iframe');
    expect(iframe?.getAttribute('sandbox')).not.toContain('allow-same-origin');
  });

  it('keeps referrerPolicy=no-referrer', () => {
    const { container } = renderViewer();
    const iframe = container.querySelector('iframe');
    expect(iframe).toHaveAttribute('referrerpolicy', 'no-referrer');
  });

  it('loads the artifact public URL', () => {
    const { container } = renderViewer();
    const iframe = container.querySelector('iframe');
    expect(iframe).toHaveAttribute('src', PUBLIC_URL);
  });
});

describe('ArtifactViewer — export control (FRE-549)', () => {
  it('shows the Export control for HTML and keeps the Open link working', () => {
    renderViewer();
    expect(
      screen.getByRole('button', { name: /export artifact/i }),
    ).toBeInTheDocument();

    // The existing standalone "Open ↗" link still fires its telemetry.
    fireEvent.click(screen.getByRole('link', { name: /open standalone/i }));
    expect(postCardClick).toHaveBeenCalledWith(
      'test-artifact-id',
      'standalone',
      undefined,
    );
  });

  it('hides the Export control for non-HTML artifacts', () => {
    renderViewer('application/json');
    expect(
      screen.queryByRole('button', { name: /export artifact/i }),
    ).not.toBeInTheDocument();
  });
});
