// seshat-dispatch channel — HTTP transport + sender gate (ADR-0116 Phase 1, FRE-871).
//
// This module is the security-critical, MCP-SDK-free half of the channel: it owns
// config validation, the shared-secret sender gate, and the localhost-only HTTP
// listener. Keeping it free of the `@modelcontextprotocol/sdk` import makes it
// unit-testable with `node --test` alone (no npm install, no Claude Code parent),
// which is where the gate's fail-closed behaviour is proven. `webhook.mjs` wires
// this module to the real MCP notification channel.
//
// Threat model (ADR-0116 "ungated inbound channel = prompt-injection vector"): the
// only thing standing between an arbitrary local POST and text injected into a
// live Claude Code session is (a) the 127.0.0.1 bind and (b) the X-Sender shared
// secret. Both are enforced here; both fail closed.
import http from 'node:http'
import crypto from 'node:crypto'

/** The only interface the channel ever binds to — nothing off-box can reach it. */
export const LOCALHOST = '127.0.0.1'

/**
 * Read and validate the channel config from an environment mapping.
 *
 * Fails closed: a missing/invalid port or an absent secret throws rather than
 * falling back to an insecure default (an ungated channel is a prompt-injection
 * vector). There is deliberately no default port and no default secret.
 *
 * @param {Record<string, string | undefined>} env
 * @returns {{ port: number, secret: string }}
 */
export function readConfig(env) {
  const port = Number(env.SESHAT_CHANNEL_PORT)
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(
      'SESHAT_CHANNEL_PORT must be set to a valid TCP port (1-65535); refusing an insecure default',
    )
  }
  const secret = env.SESHAT_CHANNEL_SECRET
  if (typeof secret !== 'string' || secret.length === 0) {
    throw new Error(
      'SESHAT_CHANNEL_SECRET must be set; an ungated inbound channel is a prompt-injection vector',
    )
  }
  return { port, secret }
}

/**
 * Constant-time shared-secret comparison.
 *
 * Uses `crypto.timingSafeEqual` so the gate does not leak the secret through a
 * byte-by-byte timing side channel. A length mismatch (or a non-string) is a
 * plain, early `false` — `timingSafeEqual` throws on unequal-length buffers.
 *
 * @param {unknown} provided value from the X-Sender header
 * @param {string} expected the configured shared secret
 * @returns {boolean}
 */
export function secretMatches(provided, expected) {
  if (typeof provided !== 'string') return false
  const a = Buffer.from(provided)
  const b = Buffer.from(expected)
  if (a.length !== b.length) return false
  return crypto.timingSafeEqual(a, b)
}

/**
 * Build the channel's HTTP server.
 *
 * Every request is gated on the X-Sender shared secret before `onEvent` is ever
 * called; an unauthorized request is dropped with 403 and never reaches Claude.
 * The server is not yet listening — call {@link listen}.
 *
 * @param {{ secret: string, onEvent: (body: string, meta: Record<string, string>) => unknown }} opts
 * @returns {import('node:http').Server}
 */
export function createServer({ secret, onEvent }) {
  return http.createServer((req, res) => {
    // Gate on the X-Sender header, which is available before the body — an
    // unauthorized request is rejected without buffering any of its payload.
    const header = req.headers['x-sender']
    const sender = Array.isArray(header) ? header[0] : header
    if (!secretMatches(sender, secret)) {
      res.writeHead(403)
      res.end('forbidden')
      req.resume() // drain and discard the incoming body so the socket closes cleanly
      return
    }
    let body = ''
    req.on('data', (chunk) => {
      body += chunk
    })
    req.on('end', () => {
      Promise.resolve(onEvent(body, { path: req.url ?? '/', method: req.method ?? 'POST' }))
        .then(() => {
          res.writeHead(200)
          res.end('ok')
        })
        .catch(() => {
          res.writeHead(500)
          res.end('error')
        })
    })
  })
}

/**
 * Bind the server to {@link LOCALHOST} on `port`.
 *
 * @param {import('node:http').Server} server
 * @param {number} port a TCP port, or 0 for an ephemeral port (tests)
 * @returns {Promise<import('node:http').Server>}
 */
export function listen(server, port) {
  return new Promise((resolve, reject) => {
    // A bind failure (e.g. EADDRINUSE from a stale prior listener) must reject
    // cleanly rather than surface as an uncaught 'error' event that crashes the
    // channel process after its MCP transport has already connected.
    const onError = (err) => reject(err)
    server.once('error', onError)
    server.listen(port, LOCALHOST, () => {
      server.removeListener('error', onError)
      resolve(server)
    })
  })
}
