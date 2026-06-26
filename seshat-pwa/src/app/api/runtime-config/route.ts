import { NextResponse } from 'next/server';

/**
 * GET /api/runtime-config
 *
 * Returns the server-side runtime config for the PWA.
 * Reads SESHAT_URL from the Node.js environment at request time —
 * not at build time — so the same image works across deployments (FRE-339).
 *
 * NOTE: gateway_token is intentionally omitted — bearer tokens must not
 * be served over an unauthenticated endpoint. The token reaches the client
 * via the root layout's Server Component → RuntimeConfigProvider prop.
 */
export function GET() {
  return NextResponse.json({
    seshat_url: process.env.SESHAT_URL ?? 'http://localhost:9000',
  });
}
