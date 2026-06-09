/**
 * Tests for ArtifactExportMenu (FRE-549).
 *
 * The Export ▾ control calls the FRE-530 endpoint via `fetchArtifactExport`
 * and downloads the returned file. Verifies: both modes call through, the
 * blob is downloaded (object URL created + revoked), the client-suggested
 * filename rides the anchor, and the two endpoint failure shapes (502 vs.
 * other) render the right friendly message instead of crashing.
 */

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

import { ArtifactExportError } from '@/lib/agui-client';

vi.mock('@/lib/agui-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/agui-client')>(
    '@/lib/agui-client',
  );
  return {
    ...actual,
    fetchArtifactExport: vi.fn(),
  };
});

import { fetchArtifactExport } from '@/lib/agui-client';
import { ArtifactExportMenu } from '@/components/ArtifactExportMenu';

const mockFetchExport = vi.mocked(fetchArtifactExport);

let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;
let anchorClick: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockFetchExport.mockReset();
  createObjectURL = vi.fn(() => 'blob:mock-url');
  revokeObjectURL = vi.fn();
  // jsdom does not implement object-URL APIs.
  Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, writable: true });
  Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, writable: true });
  anchorClick = vi.fn();
  // Capture the synthesized download anchor's click without navigating.
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(
    anchorClick as () => void,
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderMenu(filename = 'My Chart.html') {
  return render(<ArtifactExportMenu artifactId="art-1" filename={filename} />);
}

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: /export/i }));
}

describe('ArtifactExportMenu', () => {
  it('renders both export modes when opened', () => {
    renderMenu();
    openMenu();
    expect(screen.getByText(/offline/i)).toBeInTheDocument();
    expect(screen.getByText(/online/i)).toBeInTheDocument();
  });

  it('downloads via inline mode and cleans up the object URL', async () => {
    mockFetchExport.mockResolvedValue(new Blob(['<html></html>'], { type: 'text/html' }));
    renderMenu();
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /offline/i }));

    await waitFor(() => expect(mockFetchExport).toHaveBeenCalledTimes(1));
    expect(mockFetchExport).toHaveBeenCalledWith('art-1', 'inline');
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(anchorClick).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledTimes(1));
  });

  it('downloads via substitute mode for the online option', async () => {
    mockFetchExport.mockResolvedValue(new Blob(['<html></html>'], { type: 'text/html' }));
    renderMenu();
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /online/i }));

    await waitFor(() => expect(mockFetchExport).toHaveBeenCalledWith('art-1', 'substitute'));
  });

  it('puts the client-suggested filename on the download anchor', async () => {
    mockFetchExport.mockResolvedValue(new Blob(['<html></html>'], { type: 'text/html' }));
    let captured: string | null = null;
    anchorClick.mockImplementation(function (this: HTMLAnchorElement) {
      captured = this.getAttribute('download');
    });
    renderMenu('My Chart.html');
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /offline/i }));

    await waitFor(() => expect(captured).toBe('My Chart.html'));
  });

  it('shows the friendly inline-unavailable message on 502', async () => {
    mockFetchExport.mockRejectedValue(new ArtifactExportError(502, 'asset fetch failed'));
    renderMenu();
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /offline/i }));

    await waitFor(() =>
      expect(screen.getByText(/offline export unavailable/i)).toBeInTheDocument(),
    );
    // No download was triggered.
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it('shows a generic message on other failures (503)', async () => {
    mockFetchExport.mockRejectedValue(new ArtifactExportError(503, 'no substrate'));
    renderMenu();
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /online/i }));

    await waitFor(() => expect(screen.getByText(/export failed/i)).toBeInTheDocument());
  });
});
