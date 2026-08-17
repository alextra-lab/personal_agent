'use client';

import { useLocation } from '@/hooks/useLocation';

/** Inline status copy for each collection state (FRE-230). */
const STATUS_COPY: Record<string, string> = {
  requesting: 'Requesting your location…',
  granted: 'Location shared with Seshat.',
  denied: 'Location access denied. Enable it in iOS Settings → Privacy → Location.',
  unavailable: 'Location unavailable. Tap to try again.',
};

/**
 * Location consent toggle for the session drawer (FRE-230).
 *
 * Renders nothing when the operator gate is off (`feature_enabled=false`) so
 * the control only appears where the deployment supports it. The toggle is the
 * per-user consent gate; enabling it requests the device location (iOS controls
 * precise-vs-approximate) and sends coordinates + browser timezone to the
 * backend. Coordinates are stored privately in the user's knowledge graph.
 */
export function LocationConsent() {
  const { featureEnabled, consentEnabled, status, loading, setConsent, refreshLocation } =
    useLocation();

  if (loading || !featureEnabled) return null;

  const statusCopy = consentEnabled ? STATUS_COPY[status] : undefined;
  const showRetry = consentEnabled && status === 'unavailable';

  return (
    <div className="px-4 py-2.5 border-b border-line">
      <label className="flex items-center justify-between gap-3 cursor-pointer">
        <span className="flex items-center gap-2 text-sm text-ink-muted">
          <span aria-hidden="true">📍</span>
          Share location with Seshat
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={consentEnabled}
          aria-label="Share location with Seshat"
          onClick={() => void setConsent(!consentEnabled)}
          className={`relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition-colors ${
            consentEnabled ? 'bg-violet-600' : 'bg-line'
          }`}
        >
          <span
            className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
              consentEnabled ? 'translate-x-5' : 'translate-x-1'
            }`}
          />
        </button>
      </label>
      <p className="mt-1 text-xs text-ink-muted">
        Used for location-aware answers. Stored privately in your knowledge graph.
      </p>
      {statusCopy &&
        (showRetry ? (
          <button
            type="button"
            onClick={() => void refreshLocation()}
            className="mt-1 block text-left text-xs text-amber-700 dark:text-amber-400 cursor-pointer hover:text-amber-600 dark:hover:text-amber-300"
          >
            {statusCopy}
          </button>
        ) : (
          <p
            className={`mt-1 text-xs ${
              status === 'denied' ? 'text-amber-700 dark:text-amber-400' : 'text-ink-muted'
            }`}
          >
            {statusCopy}
          </p>
        ))}
    </div>
  );
}
