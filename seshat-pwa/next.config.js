/** @type {import('next').NextConfig} */
const nextConfig = {
  // Enable static export capability for PWA hosting
  // output: 'export', // Uncomment if deploying as static site

  // Rewrites to proxy AG-UI backend in development
  async rewrites() {
    return process.env.NODE_ENV === 'development'
      ? [
          {
            source: '/api/seshat/:path*',
            destination: `${process.env.NEXT_PUBLIC_SESHAT_URL || 'http://localhost:9000'}/:path*`,
          },
        ]
      : [];
  },
};

module.exports = nextConfig;
