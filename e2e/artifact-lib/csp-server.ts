/**
 * In-process static server that serves the FRE-531 fixtures under the **exact**
 * artifact CSP directive set (ADR-0089 D2), the host token rebound to this
 * server's own localhost origin.
 *
 * The CSP header string is read from the fixtures' `build-manifest.json`, which
 * the Python builder derived from `EXPECTED_CSP_DIRECTIVES` — so the policy has a
 * single source of truth and the harness never re-declares it. The one fidelity
 * gap (localhost host token vs `artifacts.frenchforet.com`) is what the live
 * `make verify-envelope` closes post-merge.
 *
 * `/lib/` assets are served with the executable/typed MIME + `nosniff` the real
 * Worker uses, so a script genuinely loads-and-executes under `nosniff` (the A7
 * "reachable under the artifact CSP" property), not merely 200s.
 */
import { createServer, type Server } from 'node:http';
import { readFile } from 'node:fs/promises';
import { join, normalize, extname } from 'node:path';
import { AddressInfo } from 'node:net';

const MIME_BY_EXT: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.woff2': 'font/woff2',
  '.json': 'application/json',
};

export interface CspServer {
  readonly origin: string;
  url(path: string): string;
  close(): Promise<void>;
}

interface Manifest {
  csp_header_template: string;
}

/**
 * Start a CSP static server rooted at `rootDir` (the built fixtures directory).
 * Resolves once listening; `origin` is `http://127.0.0.1:<port>`.
 */
export async function startCspServer(rootDir: string): Promise<CspServer> {
  const manifest = JSON.parse(
    await readFile(join(rootDir, 'build-manifest.json'), 'utf-8'),
  ) as Manifest;

  const server: Server = createServer(async (req, res) => {
    const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    // Strip query/hash, prevent path traversal outside rootDir.
    const rawPath = decodeURIComponent((req.url ?? '/').split('?')[0].split('#')[0]);
    const rel = normalize(rawPath).replace(/^(\.\.[/\\])+/, '').replace(/^\/+/, '');
    const filePath = join(rootDir, rel);
    if (!filePath.startsWith(rootDir)) {
      res.writeHead(403).end('forbidden');
      return;
    }

    let body: Buffer;
    try {
      body = await readFile(filePath);
    } catch {
      res.writeHead(404).end('not found');
      return;
    }

    const ext = extname(filePath).toLowerCase();
    const mime = MIME_BY_EXT[ext] ?? 'application/octet-stream';
    const headers: Record<string, string> = { 'Content-Type': mime };
    // Executable assets carry nosniff (the curated-/lib/ MIME-role property).
    if (ext === '.js' || ext === '.css' || ext === '.woff2') {
      headers['X-Content-Type-Options'] = 'nosniff';
    }
    // HTML documents carry the exact artifact CSP, host token rebound to us.
    if (ext === '.html') {
      headers['Content-Security-Policy'] = manifest.csp_header_template.replaceAll(
        '{ORIGIN}',
        origin,
      );
    }
    res.writeHead(200, headers).end(body);
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  return {
    origin,
    url: (path: string) => `${origin}${path.startsWith('/') ? path : `/${path}`}`,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((err) => (err ? reject(err) : resolve())),
      ),
  };
}
