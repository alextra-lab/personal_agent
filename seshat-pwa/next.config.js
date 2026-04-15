/** @type {import('next').NextConfig} */
const nextConfig = {
  // Standalone output for Docker — bundles only what next start needs
  output: 'standalone',

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
