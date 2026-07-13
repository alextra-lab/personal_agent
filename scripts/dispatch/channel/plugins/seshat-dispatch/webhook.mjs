#!/usr/bin/env node
// seshat-dispatch channel — entrypoint (ADR-0116 Phase 1, FRE-871).
//
// The production dispatch actuation channel: Claude Code spawns this as an MCP
// subprocess of a worker seat; the dispatch gateway POSTs a gating event (a PR
// CI-state payload) to the seat's localhost port, and this process injects it
// into the running session as a <channel source="seshat-dispatch"> tag. It is a
// one-way channel (no reply tool) — the transport swap of ADR-0116, not new
// authority: the seat reasons over the event and pushes fixes to its OWN worker
// branch only (boundary owned by lifecycle-rules § Session boundary).
//
// The security-critical HTTP + sender-gate logic lives in ./server.mjs (SDK-free,
// unit-tested by server.test.mjs). This file wires that gate to the real MCP
// notification and fails closed on config (readConfig throws on a missing port or
// secret) before ever binding the port.
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'

import { readConfig, createServer, listen, LOCALHOST } from './server.mjs'

const { port, secret } = readConfig(process.env)

const mcp = new Server(
  { name: 'seshat-dispatch', version: '1.0.0' },
  {
    // Presence of this experimental key is what registers the channel listener.
    capabilities: { experimental: { 'claude/channel': {} } },
    instructions:
      'Events from the seshat-dispatch channel arrive as ' +
      '<channel source="seshat-dispatch" ...> and carry a dispatch gating event ' +
      '(typically a pull-request CI-state payload). Read the event and act within ' +
      'THIS session only: author and push any fix to this session’s own worker ' +
      'branch/PR. Never push to, merge, approve, close, or deploy a branch/PR you do ' +
      'not own. One-way channel: no reply is expected.',
  },
)

await mcp.connect(new StdioServerTransport())

const server = createServer({
  secret,
  onEvent: (content, meta) =>
    mcp.notification({ method: 'notifications/claude/channel', params: { content, meta } }),
})
await listen(server, port)
process.stderr.write(`seshat-dispatch: listening on http://${LOCALHOST}:${port}\n`)
