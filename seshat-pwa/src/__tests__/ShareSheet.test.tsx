/**
 * Tests for the share sheet (FRE-1784 AC-2, AC-3, AC-4; ADR-0153 Amendment 1 L9 and A1.2).
 */

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

const createShareLink = vi.fn();
vi.mock('@/lib/agui-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/agui-client')>('@/lib/agui-client');
  return {
    ...actual,
    createShareLink: (...args: unknown[]) => createShareLink(...args),
  };
});

import { ShareLinkError } from '@/lib/agui-client';
import { EXPOSURE_SENTENCE, ShareSheet } from '@/components/ShareSheet';

const URL_ = 'https://share.example.com/s/abcdef';

function renderSheet(onClose = vi.fn()) {
  render(<ShareSheet artifactId="a-1" title="Quarterly report" onClose={onClose} />);
  return onClose;
}

function setShare(fn: ((data: ShareData) => Promise<void>) | undefined) {
  Object.defineProperty(navigator, 'share', { value: fn, configurable: true, writable: true });
}

function setClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
    writable: true,
  });
}

beforeEach(() => {
  createShareLink.mockReset();
});

afterEach(() => {
  setShare(undefined);
});

describe('ShareSheet', () => {
  it('shows the A1.2 exposure sentence before Create link is tapped (AC-3)', () => {
    renderSheet();
    expect(
      screen.getByText(
        'Anyone who gets the link can open the artifact, until the link expires or the owner revokes it. A forwarded link works for anyone.',
      ),
    ).toBeInTheDocument();
    expect(EXPOSURE_SENTENCE).toBe(
      'Anyone who gets the link can open the artifact, until the link expires or the owner revokes it. A forwarded link works for anyone.',
    );
    expect(createShareLink).not.toHaveBeenCalled();
  });

  it('offers 1, 7 and 30 days with 7 checked by default', () => {
    renderSheet();
    expect(screen.getByLabelText('1 day')).not.toBeChecked();
    expect(screen.getByLabelText('7 days')).toBeChecked();
    expect(screen.getByLabelText('30 days')).not.toBeChecked();
  });

  it('creates with the chosen expiry and shows the link (AC-2)', async () => {
    createShareLink.mockResolvedValue({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    renderSheet();
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    expect(await screen.findByDisplayValue(URL_)).toBeInTheDocument();
    expect(createShareLink).toHaveBeenCalledTimes(1);
    expect(createShareLink).toHaveBeenCalledWith('a-1', 7);
  });

  it('sends the 30-day choice when picked', async () => {
    createShareLink.mockResolvedValue({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    renderSheet();
    fireEvent.click(screen.getByLabelText('30 days'));
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    await screen.findByDisplayValue(URL_);
    expect(createShareLink).toHaveBeenCalledWith('a-1', 30);
  });

  it('Send calls navigator.share once with the URL (AC-2)', async () => {
    const share = vi.fn().mockResolvedValue(undefined);
    setShare(share);
    createShareLink.mockResolvedValue({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    renderSheet();
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    const send = await screen.findByRole('button', { name: 'Send' });
    // Send is a second tap: creating does not call share.
    expect(share).not.toHaveBeenCalled();
    fireEvent.click(send);
    expect(share).toHaveBeenCalledTimes(1);
    expect(share).toHaveBeenCalledWith({ url: URL_ });
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument();
  });

  it('a cancelled share shows no error', async () => {
    setShare(vi.fn().mockRejectedValue(new DOMException('cancelled', 'AbortError')));
    createShareLink.mockResolvedValue({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    renderSheet();
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  it('without the Web Share API, Copy link writes the URL to the clipboard (AC-2)', async () => {
    setShare(undefined);
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);
    createShareLink.mockResolvedValue({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    renderSheet();
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    const copy = await screen.findByRole('button', { name: 'Copy link' });
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument();
    fireEvent.click(copy);
    expect(writeText).toHaveBeenCalledWith(URL_);
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  it.each([
    [422, 'this artifact loads files from the artifact host that a link cannot reach'],
    [502, 'Bad Gateway'],
    [400, 'this item cannot be shared by link'],
    [503, 'Service Unavailable'],
  ])('a %i shows the API text and no link (AC-4)', async (status, text) => {
    createShareLink.mockRejectedValue(new ShareLinkError(status, text));
    renderSheet();
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument();
    // The owner can try again.
    expect(screen.getByRole('button', { name: 'Create link' })).toBeEnabled();
  });

  it('a second tap while the create is pending sends no second request', async () => {
    let resolve!: (v: unknown) => void;
    createShareLink.mockReturnValue(new Promise((r) => { resolve = r; }));
    renderSheet();
    const create = screen.getByRole('button', { name: /Create link|Creating/ });
    fireEvent.click(create);
    fireEvent.click(create);
    expect(createShareLink).toHaveBeenCalledTimes(1);
    resolve({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    await screen.findByDisplayValue(URL_);
  });

  it('cannot close while the create call is pending, so the URL is never lost', async () => {
    let resolve!: (v: unknown) => void;
    createShareLink.mockReturnValue(new Promise((r) => { resolve = r; }));
    const onClose = renderSheet();
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Close share sheet' }));
    fireEvent.click(screen.getByRole('link', { name: 'Shared links' }));
    expect(onClose).not.toHaveBeenCalled();
    resolve({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    await screen.findByDisplayValue(URL_);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a failed share keeps the link and offers Copy link', async () => {
    setShare(vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')));
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);
    createShareLink.mockResolvedValue({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    renderSheet();
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Send' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByDisplayValue(URL_)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));
    expect(writeText).toHaveBeenCalledWith(URL_);
  });

  it('the backdrop closes the sheet before a link exists, but not after', async () => {
    createShareLink.mockResolvedValue({ link_id: 'l-1', url: URL_, expires_at: '2026-12-01T00:00:00Z' });
    const onClose = renderSheet();
    const backdrop = document.querySelector('div.fixed[aria-hidden="true"]') as HTMLElement;
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    await screen.findByDisplayValue(URL_);
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape and Close call onClose', () => {
    const onClose = renderSheet();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Close share sheet' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
