/**
 * Command-line entry point of the CSP static server (FRE-1682, ADR-0159 D7.1).
 *
 * The dev-agent images run it with Node's type stripping, so it uses only erasable syntax:
 *
 *   node csp-serve.ts <root-dir> <manifest.json>
 *
 * It prints the server origin (`http://127.0.0.1:<port>`) on the first line of stdout and serves
 * until it gets SIGTERM or SIGINT. HTML files carry the artifact CSP from the manifest, with the
 * origin token set to that localhost origin.
 */
import { startCspServer } from './csp-server.ts';

const [rootDir, manifestPath] = process.argv.slice(2);
if (!rootDir || !manifestPath) {
  console.error('usage: node csp-serve.ts <root-dir> <manifest.json>');
  process.exit(2);
}

const server = await startCspServer(rootDir, manifestPath);
console.log(server.origin);

for (const signal of ['SIGTERM', 'SIGINT'] as const) {
  process.on(signal, () => {
    void server.close().finally(() => process.exit(0));
  });
}
