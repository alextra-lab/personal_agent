/**
 * ShareSheet — create a link that anyone can open, then send it (FRE-1784).
 *
 * Implements the PWA part of ADR-0153 Amendment 1 L9, with the exposure sentence of A1.2.
 * The caller shows the sheet only for an item whose metadata has `can_share_link: true`.
 *
 * - The expiry is 1, 7 or 30 days, default 7 (L4).
 * - Create link calls `POST /api/v1/artifacts/{id}/share-links`. An error shows the API's text.
 * - Send calls `navigator.share({url})`. It is a second tap, because a browser allows
 *   `navigator.share` only inside a user gesture, and the create call ends the first one.
 *   Without the Web Share API, the sheet shows Copy link.
 * - The URL holds the link's token. It lives only in this component's state: closing the sheet
 *   unmounts it, and nothing writes it to storage or logs.
 *
 * The "Link for anyone" part is its own section, so FRE-1534 can add "Share with a Seshat user"
 * beside it later.
 */

'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';

import { createShareLink } from '@/lib/agui-client';
import type { ShareLinkExpiryDays } from '@/lib/agui-client';

/** ADR-0153 A1.2, verbatim. */
export const EXPOSURE_SENTENCE =
  'Anyone who gets the link can open the artifact, until the link expires or the owner revokes it. A forwarded link works for anyone.';

const EXPIRY_CHOICES: readonly { days: ShareLinkExpiryDays; label: string }[] = [
  { days: 1, label: '1 day' },
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
];

type LinkState =
  | { phase: 'choose' }
  | { phase: 'creating' }
  | { phase: 'failed'; message: string }
  | { phase: 'created'; url: string; expiresAt: string };

type SendState = { kind: 'idle' } | { kind: 'copied' } | { kind: 'failed'; message: string };

interface ShareSheetProps {
  artifactId: string;
  title: string | null;
  onClose: () => void;
}

/** Return whether this browser has the Web Share API. */
function canWebShare(): boolean {
  return typeof navigator !== 'undefined' && typeof navigator.share === 'function';
}

function formatExpiry(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/** The share sheet: expiry choice, exposure sentence, Create link, then Send or Copy link. */
export function ShareSheet({ artifactId, title, onClose }: ShareSheetProps) {
  const [days, setDays] = useState<ShareLinkExpiryDays>(7);
  const [link, setLink] = useState<LinkState>({ phase: 'choose' });
  const [send, setSend] = useState<SendState>({ kind: 'idle' });
  // A ref, not state: two taps in the same frame both see the state before the first render.
  const pending = useRef(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  // No close while the create call runs: a link created after the sheet closed would be active,
  // and its URL, shown only once, would be lost.
  const close = () => {
    if (!pending.current) onClose();
  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !pending.current) onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  const handleCreate = async () => {
    if (pending.current) return;
    pending.current = true;
    setLink({ phase: 'creating' });
    try {
      const created = await createShareLink(artifactId, days);
      setLink({ phase: 'created', url: created.url, expiresAt: created.expires_at });
    } catch (err) {
      setLink({
        phase: 'failed',
        message: err instanceof Error ? err.message : 'The link could not be created.',
      });
    } finally {
      pending.current = false;
    }
  };

  const handleSend = async (url: string) => {
    setSend({ kind: 'idle' });
    try {
      await navigator.share({ url });
    } catch (err) {
      // The owner closed the phone's share sheet: not an error.
      if (err instanceof DOMException && err.name === 'AbortError') return;
      setSend({ kind: 'failed', message: 'Could not open the share sheet. Copy the link instead.' });
    }
  };

  const handleCopy = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setSend({ kind: 'copied' });
    } catch {
      setSend({ kind: 'failed', message: 'Could not copy. Select the link and copy it.' });
    }
  };

  const displayTitle = title ?? 'Artifact';
  const created = link.phase === 'created' ? link : null;

  return (
    <>
      <div className="fixed inset-0 z-[60] bg-black/60" aria-hidden="true" onClick={close} />

      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Share ${displayTitle}`}
        className={[
          'fixed z-[70] bg-surface border-line shadow-2xl flex flex-col gap-4 p-4 text-ink',
          // Mobile: bottom sheet
          'inset-x-0 bottom-0 max-h-[90vh] overflow-y-auto rounded-t-2xl border-t',
          // Desktop: centred dialog
          'md:inset-x-auto md:bottom-auto md:top-1/2 md:left-1/2 md:-translate-x-1/2 md:-translate-y-1/2',
          'md:w-full md:max-w-md md:rounded-2xl md:border',
        ].join(' ')}
        style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 1rem)' }}
      >
        <div className="flex items-center gap-2">
          <h2 className="flex-1 text-sm font-semibold truncate">Share {displayTitle}</h2>
          <button
            ref={closeRef}
            onClick={close}
            disabled={link.phase === 'creating'}
            aria-label="Close share sheet"
            className="flex items-center justify-center w-6 h-6 rounded text-ink-muted hover:text-ink hover:bg-line/60 transition-colors"
          >
            ✕
          </button>
        </div>

        <section aria-label="Link for anyone" className="flex flex-col gap-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
            Link for anyone
          </h3>

          <p className="text-sm leading-relaxed">{EXPOSURE_SENTENCE}</p>

          {!created && (
            <>
              <fieldset className="flex flex-col gap-1.5">
                <legend className="text-xs text-ink-muted mb-1">The link expires after</legend>
                <div className="flex gap-4">
                  {EXPIRY_CHOICES.map((choice) => (
                    <label key={choice.days} className="flex items-center gap-1.5 text-sm">
                      <input
                        type="radio"
                        name="share-link-expiry"
                        value={choice.days}
                        checked={days === choice.days}
                        onChange={() => setDays(choice.days)}
                        disabled={link.phase === 'creating'}
                      />
                      {choice.label}
                    </label>
                  ))}
                </div>
              </fieldset>

              {link.phase === 'failed' && (
                <p role="alert" className="text-sm text-red-700 dark:text-red-300">
                  {link.message}
                </p>
              )}

              <button
                onClick={handleCreate}
                disabled={link.phase === 'creating'}
                className="self-start px-4 py-2 rounded-lg text-sm font-semibold bg-accent text-white hover:bg-accent-hover disabled:opacity-60 transition-colors"
              >
                {link.phase === 'creating' ? 'Creating…' : 'Create link'}
              </button>
            </>
          )}

          {created && (
            <>
              <input
                type="text"
                readOnly
                value={created.url}
                aria-label="Share link"
                onFocus={(e) => e.currentTarget.select()}
                className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm font-mono text-ink"
              />
              <p className="text-xs text-ink-muted">Expires {formatExpiry(created.expiresAt)}</p>

              {canWebShare() ? (
                <button
                  onClick={() => handleSend(created.url)}
                  className="self-start px-4 py-2 rounded-lg text-sm font-semibold bg-accent text-white hover:bg-accent-hover transition-colors"
                >
                  Send
                </button>
              ) : (
                <button
                  onClick={() => handleCopy(created.url)}
                  className="self-start px-4 py-2 rounded-lg text-sm font-semibold bg-accent text-white hover:bg-accent-hover transition-colors"
                >
                  Copy link
                </button>
              )}

              {send.kind === 'copied' && <p className="text-xs text-ink-muted">Copied</p>}
              {send.kind === 'failed' && (
                <p role="alert" className="text-sm text-red-700 dark:text-red-300">
                  {send.message}
                </p>
              )}
            </>
          )}
        </section>

        <Link
          href="/shared-links"
          onClick={(e) => {
            if (pending.current) e.preventDefault();
            else onClose();
          }}
          className="text-xs text-ink-muted hover:text-ink underline underline-offset-2 self-start"
        >
          Shared links
        </Link>
      </div>
    </>
  );
}
