// Unit tests for the seshat-dispatch channel HTTP gate (FRE-871, ADR-0116 Phase 1).
//
// Run: `node --test server.test.mjs` (no npm install — server.mjs is SDK-free).
// These prove the fail-closed sender gate and localhost-only bind that stand
// between an arbitrary local POST and text injected into a live Claude session.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { LOCALHOST, readConfig, secretMatches, createServer, listen } from './server.mjs'

function post(port, body, headers = {}) {
  return fetch(`http://${LOCALHOST}:${port}/`, { method: 'POST', body, headers })
}

test('readConfig requires a valid port (no insecure default)', () => {
  assert.throws(() => readConfig({ SESHAT_CHANNEL_SECRET: 's' }), /SESHAT_CHANNEL_PORT/)
  assert.throws(
    () => readConfig({ SESHAT_CHANNEL_PORT: '0', SESHAT_CHANNEL_SECRET: 's' }),
    /SESHAT_CHANNEL_PORT/,
  )
  assert.throws(
    () => readConfig({ SESHAT_CHANNEL_PORT: 'nope', SESHAT_CHANNEL_SECRET: 's' }),
    /SESHAT_CHANNEL_PORT/,
  )
})

test('readConfig requires a secret (fail closed)', () => {
  assert.throws(() => readConfig({ SESHAT_CHANNEL_PORT: '8791' }), /SESHAT_CHANNEL_SECRET/)
  assert.throws(
    () => readConfig({ SESHAT_CHANNEL_PORT: '8791', SESHAT_CHANNEL_SECRET: '' }),
    /SESHAT_CHANNEL_SECRET/,
  )
})

test('readConfig accepts a valid port + secret', () => {
  assert.deepEqual(readConfig({ SESHAT_CHANNEL_PORT: '8791', SESHAT_CHANNEL_SECRET: 'sekret' }), {
    port: 8791,
    secret: 'sekret',
  })
})

test('secretMatches is exact and length-safe', () => {
  assert.equal(secretMatches('abc', 'abc'), true)
  assert.equal(secretMatches('abc', 'abd'), false)
  assert.equal(secretMatches('ab', 'abc'), false)
  assert.equal(secretMatches('abcd', 'abc'), false)
  assert.equal(secretMatches(undefined, 'abc'), false)
  assert.equal(secretMatches(123, 'abc'), false)
})

test('http gate: authorized POST invokes onEvent and returns 200', async () => {
  const events = []
  const server = createServer({
    secret: 'topsecret',
    onEvent: (body, meta) => events.push({ body, meta }),
  })
  await listen(server, 0)
  const { port } = server.address()
  try {
    const res = await post(port, 'ci failed on PR #7', { 'X-Sender': 'topsecret' })
    assert.equal(res.status, 200)
    assert.equal(events.length, 1)
    assert.equal(events[0].body, 'ci failed on PR #7')
    assert.equal(events[0].meta.method, 'POST')
  } finally {
    server.close()
  }
})

test('http gate: wrong secret returns 403 and drops (no event)', async () => {
  const events = []
  const server = createServer({ secret: 'topsecret', onEvent: () => events.push(1) })
  await listen(server, 0)
  const { port } = server.address()
  try {
    const res = await post(port, 'injected', { 'X-Sender': 'guess' })
    assert.equal(res.status, 403)
    assert.equal(events.length, 0)
  } finally {
    server.close()
  }
})

test('http gate: missing X-Sender returns 403 and drops (no event)', async () => {
  const events = []
  const server = createServer({ secret: 'topsecret', onEvent: () => events.push(1) })
  await listen(server, 0)
  const { port } = server.address()
  try {
    const res = await post(port, 'injected')
    assert.equal(res.status, 403)
    assert.equal(events.length, 0)
  } finally {
    server.close()
  }
})

test('server binds localhost only', async () => {
  const server = createServer({ secret: 's', onEvent: () => {} })
  await listen(server, 0)
  try {
    assert.equal(server.address().address, LOCALHOST)
  } finally {
    server.close()
  }
})

// --- hardening (FRE-872, ADR-0116): method/path rejection + body-size cap --
// The ADR names this exact system as a prompt-injection-adjacent ingress; this
// closes two gaps a codex plan-review found once the gateway starts actually
// POSTing to it in anger.

test('http gate: non-POST method is rejected before the secret check', async () => {
  const events = []
  const server = createServer({ secret: 'topsecret', onEvent: () => events.push(1) })
  await listen(server, 0)
  const { port } = server.address()
  try {
    // No X-Sender header at all -- if this reached the secret check it would
    // be a 403, not a 404/405; asserting the method-rejection status proves
    // the shape check runs first.
    const res = await fetch(`http://${LOCALHOST}:${port}/`, { method: 'GET' })
    assert.equal(res.status, 405)
    assert.equal(events.length, 0)
  } finally {
    server.close()
  }
})

test('http gate: non-root path is rejected before the secret check', async () => {
  const events = []
  const server = createServer({ secret: 'topsecret', onEvent: () => events.push(1) })
  await listen(server, 0)
  const { port } = server.address()
  try {
    const res = await fetch(`http://${LOCALHOST}:${port}/other`, { method: 'POST' })
    assert.equal(res.status, 404)
    assert.equal(events.length, 0)
  } finally {
    server.close()
  }
})

test('http gate: an oversized authorized body is rejected with 413 and never reaches onEvent', async () => {
  const events = []
  const server = createServer({ secret: 'topsecret', onEvent: () => events.push(1) })
  await listen(server, 0)
  const { port } = server.address()
  try {
    const oversized = 'x'.repeat(64 * 1024 + 1)
    const res = await post(port, oversized, { 'X-Sender': 'topsecret' })
    assert.equal(res.status, 413)
    assert.equal(events.length, 0)
  } finally {
    server.close()
  }
})

test('http gate: a body at the cap is still accepted', async () => {
  const events = []
  const server = createServer({ secret: 'topsecret', onEvent: (body) => events.push(body) })
  await listen(server, 0)
  const { port } = server.address()
  try {
    const atCap = 'x'.repeat(64 * 1024)
    const res = await post(port, atCap, { 'X-Sender': 'topsecret' })
    assert.equal(res.status, 200)
    assert.equal(events.length, 1)
  } finally {
    server.close()
  }
})
