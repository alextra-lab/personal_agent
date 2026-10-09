// End-to-end test of the channel entrypoint's advertised server instructions
// (FRE-1555, ADR-0155 D2 track A, AC-2).
//
// It starts webhook.mjs as a child process once per SESHAT_SEAT value, sends the MCP
// `initialize` request over stdio, and reads the `instructions` the server advertises.
// It needs the MCP SDK (`npm ci` in this folder), so it skips when the SDK is absent.
// server.test.mjs proves the same selection without the SDK.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import net from 'node:net'
import { fileURLToPath } from 'node:url'

import { MASTER_SEAT, instructionsFor } from './server.mjs'

const here = fileURLToPath(new URL('.', import.meta.url))
const sdkMissing = !existsSync(new URL('./node_modules/@modelcontextprotocol/sdk', import.meta.url))

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.once('error', reject)
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address()
      probe.close(() => resolve(port))
    })
  })
}

/** Start webhook.mjs for `seat` (undefined = variable unset) and return its advertised instructions. */
async function advertisedInstructions(seat) {
  const env = {
    ...process.env,
    SESHAT_CHANNEL_PORT: String(await freePort()),
    SESHAT_CHANNEL_SECRET: 'test-secret',
  }
  delete env.SESHAT_SEAT
  if (seat !== undefined) env.SESHAT_SEAT = seat
  const child = spawn(process.execPath, ['webhook.mjs'], { cwd: here, env })
  try {
    const reply = new Promise((resolve, reject) => {
      let buffer = ''
      const timer = setTimeout(() => reject(new Error('no initialize reply in 10 s')), 10_000)
      child.once('error', reject)
      child.once('exit', (code) => reject(new Error(`webhook.mjs exited early: ${code}`)))
      child.stdout.on('data', (chunk) => {
        buffer += chunk
        const line = buffer.split('\n').find((l) => l.trim().startsWith('{'))
        if (line === undefined) return
        clearTimeout(timer)
        resolve(JSON.parse(line))
      })
    })
    child.stdin.write(
      JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'initialize',
        params: {
          protocolVersion: '2025-03-26',
          capabilities: {},
          clientInfo: { name: 'webhook.test', version: '1' },
        },
      }) + '\n',
    )
    return (await reply).result.instructions
  } finally {
    child.removeAllListeners('exit')
    child.kill()
  }
}

test('webhook.mjs advertises the master text to cc-master only', { skip: sdkMissing }, async () => {
  const master = await advertisedInstructions(MASTER_SEAT)
  assert.equal(master, instructionsFor(MASTER_SEAT))
  assert.match(master, /master skill/)
  assert.doesNotMatch(master, /Never push to, merge/)
})

test('webhook.mjs advertises the worker text to every other seat', { skip: sdkMissing }, async () => {
  const worker = instructionsFor('cc-2build')
  for (const seat of ['cc-1build', 'cc-2build', 'cc-adrs', '', undefined]) {
    const got = await advertisedInstructions(seat)
    assert.equal(got, worker, `seat ${JSON.stringify(seat)}`)
    assert.match(got, /Never push to, merge, approve, close, or deploy/)
    assert.doesNotMatch(got, /master skill/)
  }
})
