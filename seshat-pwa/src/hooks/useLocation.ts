'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  getLocationPreference,
  updateLocationPreference,
} from '@/lib/agui-client';

/**
 * Geolocation collection state (FRE-230).
 *
 * - `idle`        — no collection attempt yet (consent off, or not requested).
 * - `requesting`  — awaiting the browser permission prompt / fix.
 * - `granted`     — coordinates obtained and sent to the backend.
 * - `denied`      — the user (or iOS Settings) denied location access.
 * - `unavailable` — geolocation unsupported, timed out, or position error.
 */
export type LocationStatus =
  | 'idle'
  | 'requesting'
  | 'granted'
  | 'denied'
  | 'unavailable';

export interface UseLocationResult {
  /** Operator gate — when false the consent toggle must be hidden. */
  featureEnabled: boolean;
  /** Per-user consent gate. */
  consentEnabled: boolean;
  /** Live collection status for inline messaging. */
  status: LocationStatus;
  /** True until the initial preference fetch resolves. */
  loading: boolean;
  /** Toggle consent; enabling triggers a device-location request. */
  setConsent: (enabled: boolean) => Promise<void>;
  /** Re-request the device location (manual refresh) when consent is on. */
  refreshLocation: () => Promise<void>;
}

const GEO_OPTIONS: PositionOptions = {
  enableHighAccuracy: true,
  timeout: 10_000,
  maximumAge: 0,
};

/** Resolve the browser's IANA timezone, falling back to UTC. */
function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/**
 * Manage the FRE-230 location consent toggle and iOS/browser location
 * collection.
 *
 * The device is the precision authority: we request high-accuracy coordinates
 * and send them verbatim with the browser timezone. The backend enforces both
 * gates; this hook never collects until the user flips consent on.
 */
export function useLocation(): UseLocationResult {
  const [featureEnabled, setFeatureEnabled] = useState(false);
  const [consentEnabled, setConsentEnabled] = useState(false);
  const [status, setStatus] = useState<LocationStatus>('idle');
  const [loading, setLoading] = useState(true);

  // Monotonic generation guarding async collection. Every consent transition,
  // manual refresh, and unmount increments it; an in-flight getCurrentPosition
  // whose captured generation is stale must NOT transmit coordinates — this is
  // the privacy guarantee that turning consent off cancels a pending collect.
  const generationRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    getLocationPreference()
      .then((pref) => {
        if (cancelled) return;
        setFeatureEnabled(pref.feature_enabled);
        setConsentEnabled(pref.location_consent_enabled);
      })
      .catch(() => {
        // Treat a fetch failure as feature-off so the toggle stays hidden.
        if (!cancelled) setFeatureEnabled(false);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      // Invalidate any in-flight collection on unmount.
      generationRef.current += 1;
    };
  }, []);

  const collectAndSend = useCallback(async (generation: number): Promise<void> => {
    const isCurrent = () => generation === generationRef.current;
    if (typeof navigator === 'undefined' || !navigator.geolocation) {
      if (isCurrent()) setStatus('unavailable');
      return;
    }
    if (isCurrent()) setStatus('requesting');
    try {
      const position = await new Promise<GeolocationPosition>((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(resolve, reject, GEO_OPTIONS);
      });
      // Consent was withdrawn (or component unmounted) while the device was
      // acquiring a fix — abort before any coordinates leave the browser.
      if (!isCurrent()) return;
      await updateLocationPreference(undefined, {
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
        timezone: browserTimezone(),
      });
      if (isCurrent()) setStatus('granted');
    } catch (err: unknown) {
      if (!isCurrent()) return;
      // GeolocationPositionError.PERMISSION_DENIED === 1.
      const code = (err as GeolocationPositionError | undefined)?.code;
      setStatus(code === 1 ? 'denied' : 'unavailable');
    }
  }, []);

  const setConsent = useCallback(
    async (enabled: boolean): Promise<void> => {
      // New transition: bump the generation so any prior in-flight collect is
      // invalidated and cannot transmit coordinates after this point.
      const generation = (generationRef.current += 1);
      const previous = consentEnabled;
      setConsentEnabled(enabled); // optimistic; revert on failure so the switch never lies
      try {
        await updateLocationPreference(enabled);
      } catch {
        if (generation === generationRef.current) setConsentEnabled(previous);
        return;
      }
      if (generation !== generationRef.current) return;
      if (enabled) {
        await collectAndSend(generation);
      } else {
        setStatus('idle');
      }
    },
    [consentEnabled, collectAndSend],
  );

  const refreshLocation = useCallback(async (): Promise<void> => {
    if (!consentEnabled) return;
    const generation = (generationRef.current += 1);
    await collectAndSend(generation);
  }, [consentEnabled, collectAndSend]);

  return {
    featureEnabled,
    consentEnabled,
    status,
    loading,
    setConsent,
    refreshLocation,
  };
}
