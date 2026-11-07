// Serve dist/ with the production CSP for the tests (ADR-0159 D7.1, D7.2).
//
// It copies dist/ to a temp folder, links the toolkit in as /lib/, and starts the CSP test
// server of the image. The server prints its origin on the first line. PAGE_URL is the page.
import { spawn } from 'node:child_process';
import { cpSync, existsSync, mkdtempSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';

const SERVER = process.env.CSP_SERVER ?? '/opt/csp/csp-serve.ts';
const MANIFEST = process.env.CSP_MANIFEST ?? '/opt/csp/csp-manifest.json';
const TOOLKIT = process.env.ARTIFACT_LIB ?? '/srv/artifact-lib';

export default async function globalSetup() {
  const root = mkdtempSync(join(tmpdir(), 'page-'));
  cpSync(resolve('dist'), root, { recursive: true });
  if (existsSync(TOOLKIT)) symlinkSync(TOOLKIT, join(root, 'lib'));
  const server = spawn('node', [SERVER, root, MANIFEST], { stdio: ['ignore', 'pipe', 'inherit'] });
  const origin = await new Promise((ok, fail) => {
    createInterface({ input: server.stdout }).once('line', ok);
    server.once('exit', (code) => fail(new Error(`the CSP server exited with ${code}`)));
  });
  process.env.PAGE_URL = `${origin}/index.html`;
  return async () => {
    server.kill('SIGTERM');
  };
}
