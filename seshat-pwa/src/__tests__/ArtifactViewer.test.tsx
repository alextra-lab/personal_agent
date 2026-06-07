/**
 * Tests for ArtifactViewer sandbox posture (ADR-0089 D2/D3 / FRE-510).
 *
 * The iframe must carry sandbox="allow-scripts" — and never
 * allow-same-origin, which would lift the opaque origin and defeat the
 * ADR-0089 isolation model.
 */

import { render } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

vi.mock('@/lib/agui-client', () => ({
  postCardClick: vi.fn(),
}));

import { ArtifactViewer } from '@/components/ArtifactViewer';

const PUBLIC_URL = 'https://artifacts.frenchforet.com/test-artifact-id';

function renderViewer() {
  return render(
    <ArtifactViewer
      artifactId="test-artifact-id"
      publicUrl={PUBLIC_URL}
      title="Test Artifact"
      contentType="text/html; charset=utf-8"
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
