'use client';

import { useLayoutEffect } from 'react';

import { initAguiConfig } from '@/lib/agui-client';

interface RuntimeConfigProviderProps {
  seshatUrl: string;
  gatewayToken: string;
  children: React.ReactNode;
}

/**
 * Propagates runtime config (SESHAT_URL, GATEWAY_TOKEN) from the Server
 * Component that reads env vars into the agui-client module singleton.
 *
 * Uses useLayoutEffect so initAguiConfig is called after DOM commit but
 * before any child useEffect fires — ensuring children's API calls see
 * the correct URL and token (FRE-339).
 */
export function RuntimeConfigProvider({
  seshatUrl,
  gatewayToken,
  children,
}: RuntimeConfigProviderProps) {
  useLayoutEffect(() => {
    initAguiConfig(seshatUrl, gatewayToken);
  }, [seshatUrl, gatewayToken]);

  return <>{children}</>;
}
