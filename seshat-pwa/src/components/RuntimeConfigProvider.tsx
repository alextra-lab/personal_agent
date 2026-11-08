'use client';

import { createContext, useContext, useLayoutEffect } from 'react';

import { initAguiConfig } from '@/lib/agui-client';

interface RuntimeConfigProviderProps {
  seshatUrl: string;
  gatewayToken: string;
  /** Address of Observe (Grafana). Unset or empty means no link is shown (FRE-1754). */
  observeUrl?: string;
  children: React.ReactNode;
}

const ObserveUrlContext = createContext<string>('');

/**
 * Returns the configured Observe (Grafana) address, or an empty string.
 *
 * Returns:
 *     The address from the runtime config, or '' when none is configured.
 */
export function useObserveUrl(): string {
  return useContext(ObserveUrlContext);
}

/**
 * Propagates runtime config (SESHAT_URL, GATEWAY_TOKEN, OBSERVE_URL) from the
 * Server Component that reads env vars. SESHAT_URL and GATEWAY_TOKEN go into
 * the agui-client module singleton. OBSERVE_URL goes into a React context.
 *
 * Uses useLayoutEffect so initAguiConfig is called after DOM commit but
 * before any child useEffect fires — ensuring children's API calls see
 * the correct URL and token (FRE-339).
 */
export function RuntimeConfigProvider({
  seshatUrl,
  gatewayToken,
  observeUrl = '',
  children,
}: RuntimeConfigProviderProps) {
  useLayoutEffect(() => {
    initAguiConfig(seshatUrl, gatewayToken);
  }, [seshatUrl, gatewayToken]);

  return <ObserveUrlContext.Provider value={observeUrl}>{children}</ObserveUrlContext.Provider>;
}
