import type { Metadata, Viewport } from 'next';

import './globals.css';
import { RegisterSW } from '@/components/RegisterSW';
import { RuntimeConfigProvider } from '@/components/RuntimeConfigProvider';
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
  themeColor: '#2f6bff',
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
 * Root layout — wraps all pages with dark background and PWA manifest.
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
    <html lang="en" className="dark h-full">
      <body className="h-full bg-slate-900 text-slate-100 antialiased">
        <RegisterSW />
        <RuntimeConfigProvider seshatUrl={seshatUrl} gatewayToken={gatewayToken}>
          {children}
        </RuntimeConfigProvider>
      </body>
    </html>
  );
}
