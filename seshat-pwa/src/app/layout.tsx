import type { Metadata, Viewport } from 'next';

import './globals.css';

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
  themeColor: '#3b82f6',
};

interface RootLayoutProps {
  children: React.ReactNode;
}

/**
 * Root layout — wraps all pages with dark background and PWA manifest.
 *
 * Sets viewport to prevent zoom (better for chat UX) and uses
 * viewport-fit=cover for correct display in iPhone standalone mode.
 */
export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="en" className="dark h-full">
      <body className="h-full bg-slate-900 text-slate-100 antialiased">
        {children}
      </body>
    </html>
  );
}
