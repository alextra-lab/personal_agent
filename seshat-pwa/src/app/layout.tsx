import type { Metadata, Viewport } from 'next';
import Script from 'next/script';

import './globals.css';
import { RegisterSW } from '@/components/RegisterSW';
import { RuntimeConfigProvider } from '@/components/RuntimeConfigProvider';
import { SafeAreaDebugOverlay } from '@/components/SafeAreaDebugOverlay';
import { THEME_INIT_SCRIPT } from '@/lib/theme';
// Curated artifact toolkit alignment (FRE-532): bundle our own pinned copies of
// the toolkit's chat-render stylesheets, ordered after Tailwind layers.
import 'katex/dist/katex.min.css';
import 'highlight.js/styles/github-dark.css';

export const metadata: Metadata = {
  title: 'Seshat',
  description: 'Seshat Personal Agent — streaming chat interface',
  manifest: '/manifest.json',
  // PWA meta tags for iOS
  appleWebApp: {
    capable: true,
    statusBarStyle: 'black-translucent',
    title: 'Seshat',
  },
  // Next.js 15's appleWebApp API stopped emitting the Apple-prefixed
  // apple-mobile-web-app-capable tag (only the generic W3C
  // mobile-web-app-capable one — vercel/next.js#70272/#74524). Apple's own
  // docs state apple-mobile-web-app-status-bar-style (above) has no effect
  // without this tag present, so restore it explicitly (FRE-1269).
  other: {
    'apple-mobile-web-app-capable': 'yes',
  },
  formatDetection: {
    telephone: false,
  },
  icons: {
    apple: '/icons/icon-192.png',
  },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  viewportFit: 'cover', // handles iPhone notch
  // Matches the page background in each theme (FRE-1264) — system-preference
  // only, same as the browser chrome itself; does not read the stored override.
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#faf9f5' },
    { media: '(prefers-color-scheme: dark)', color: '#1e2940' },
  ],
};

// Must be force-dynamic so process.env.SESHAT_URL and GATEWAY_TOKEN are
// read at runtime from the Node.js environment, not baked at build time.
// Without this, Next.js may pre-render this layout and freeze the empty
// build-time value (FRE-339).
export const dynamic = 'force-dynamic';

interface RootLayoutProps {
  children: React.ReactNode;
}

/**
 * Root layout — wraps all pages with the theme-aware background (FRE-1264)
 * and PWA manifest.
 *
 * Reads SESHAT_URL and GATEWAY_TOKEN from the runtime Node.js environment
 * (not NEXT_PUBLIC_ build-time bake) and passes them to RuntimeConfigProvider,
 * which initializes agui-client before any child API calls are made (FRE-339).
 *
 * Sets viewport to prevent zoom (better for chat UX) and uses
 * viewport-fit=cover for correct display in iPhone standalone mode.
 */
export default function RootLayout({ children }: RootLayoutProps) {
  const seshatUrl = process.env.SESHAT_URL ?? 'http://localhost:9000';
  const gatewayToken = process.env.GATEWAY_TOKEN ?? '';

  if (process.env.NODE_ENV === 'production' && !process.env.SESHAT_URL) {
    console.error('[seshat-pwa] SESHAT_URL not set — requests will route to localhost:9000');
  }

  return (
    // suppressHydrationWarning: THEME_INIT_SCRIPT (below) mutates the `dark`
    // class before hydration based on localStorage/system preference — an
    // intentional, expected mismatch with the server-rendered class list.
    <html lang="en" className="h-full" suppressHydrationWarning>
      <head>
        <Script id="theme-init" strategy="beforeInteractive">
          {THEME_INIT_SCRIPT}
        </Script>
      </head>
      <body className="h-full bg-bg text-ink antialiased">
        <RegisterSW />
        <SafeAreaDebugOverlay />
        <RuntimeConfigProvider seshatUrl={seshatUrl} gatewayToken={gatewayToken}>
          {children}
        </RuntimeConfigProvider>
      </body>
    </html>
  );
}
