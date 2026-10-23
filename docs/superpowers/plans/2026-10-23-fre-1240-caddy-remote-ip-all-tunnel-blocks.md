# FRE-1240 — Caddy: restrict every inbound tunnel site block to the compose network

Ticket: FRE-1240 (Approved, High). Prior art: FRE-1239 (OTLP block already carries `remote_ip`).
Diff class: security config, Caddy restart on deploy. Not a gateway rebuild.

## Problem

`config/cloud-sim/Caddyfile` has five inbound tunnel blocks: `{$AGENT_HOST}`, `{$GRAPH_HOST}`,
`{$ES_HOST}`, `{$API_HOST}`, `{$OTLP_HOST}`. Only OTLP restricts the peer address. Caddy publishes
80/443 on all interfaces, so a request to the VPS IP with a forged `Host` header skips Access.

## Evidence gathered (read-only, 2026-10-23)

- Caddy access log, source addresses: `172.25.0.x` for service and cloudflared traffic,
  `172.25.0.1` for loopback requests through the docker proxy (the `:8600` egress listener),
  and the VPS public IP for master's own-public-IP check of the ES host.
- So a request from the VPS to its own public IP is **outside** `172.25.0.0/16`. AC-2 will see 403.
- `remote_ip` tests the TCP peer address. `X-Forwarded-For` cannot forge it.
- **IPv6 (found during self-review, measured):** a request to the VPS public IPv4 address reaches
  Caddy with its true source. A request to the public IPv6 address reaches Caddy as `172.25.0.1`,
  the bridge gateway, because Docker serves IPv6 through its userland proxy. `172.25.0.1` is
  inside `172.25.0.0/16`, so the subnet check alone passes a direct IPv6 request. Caddy listens on
  `[::]:80`. `harden.sh` drops inbound IPv6, but the live `ip6tables` INPUT policy is `ACCEPT`.

## Design

One snippet, imported first in each of the four unrestricted blocks:

```
(tunnel_only) {
	@not_tunnel not remote_ip 172.25.0.0/16
	handle @not_tunnel {
		respond "Forbidden" 403
	}
}
```

The snippet also holds `@bridge_gateway remote_ip 172.25.0.1/32` with its own `handle` that
answers 403. The gateway is the host or its userland proxy, never cloudflared, so refusing that one
address costs nothing and closes the IPv6 path. The OTLP block gets `not remote_ip 172.25.0.1/32`
in its `@allowed` matcher for the same reason.

Why `handle`, not a bare `respond`: Caddy orders `handle` before `respond`. A bare
`respond @not_tunnel` would run after the block's own `handle`/`reverse_proxy` and not guard
anything. The `handle` blocks run in file order, so the import must be the first line of the block.

Not changed: the rest of the OTLP block (path, method, body cap, log redaction), the
`localhost` block (SSH-tunnel dev access), the `:8600`/`:8601` egress listeners (loopback-bound).

## Steps

1. **Test first** — `tests/scripts/test_caddyfile_tunnel_blocks_remote_ip.py` (static, AC-1):
   - parse top-level site blocks line by line, inline `import <snippet>` lines,
   - a tunnel block is one whose address contains `{$…_HOST`,
   - assert the five tunnel blocks are found (guards against a parser that finds none),
   - assert each expanded block has `remote_ip 172.25.0.0/16` and a 403 respond,
   - seeded negative: a fixture Caddyfile with one block without the matcher is reported,
   - assert the literal subnet equals the `cloud-sim` subnet in `docker-compose.cloud.yml`.
   Command: `make test-file FILE=tests/scripts/test_caddyfile_tunnel_blocks_remote_ip.py`
   Expect: fails before step 2 (four blocks reported), passes after.
2. **Edit the Caddyfile** — add the snippet and four `import tunnel_only` lines. Update the file
   header comment.
3. **Behavior proof** — `tests/scripts/test_fre1240_tunnel_only_smoke.py` (integration, Docker):
   run the full committed Caddyfile in a disposable Caddy on a throwaway network.
   - Guard subnet replaced by one that excludes the client: every tunnel host answers 403,
     including ES `/_bulk` and `/slm-requests-x`, and the graph WebSocket upgrade shape.
   - Guard keeps the network's real gateway, and the client arrives as that gateway: every route is
     403 although the peer is inside the subnet (the IPv6 case).
   - Guard subnet replaced by the network's own subnet: the same requests are **not** 403
     (they reach `reverse_proxy` and fail, since no upstream exists). The assertion is "not 403":
     the AGENT and API blocks turn the 502 into a 500 through `handle_errors`, while GRAPH and ES
     answer 502 (codex plan-review finding, folded in).
   Command: `PERSONAL_AGENT_INTEGRATION=1 uv run python -m pytest -m integration tests/scripts/test_fre1240_tunnel_only_smoke.py -v`
4. **Gates** — `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`; commit; self-review; PR; handoff comment.

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 | Static test above, with seeded negative |
| AC-2 | Master, post-deploy. Smoke test proves the 403 route for all four hosts. Log evidence above shows the source address is outside the subnet |
| AC-3 | Master, post-deploy (PWA turn, `slm-requests-*` writes). Smoke test proves in-subnet requests still reach the proxy |

## Risks

- Import placed after another `handle` → guard bypassed. Smoke test covers each block.
- Subnet drift between Caddyfile and compose → static test compares both.
- Loopback `127.0.0.1:80` requests arrive as `172.25.0.1`. They are now refused on the tunnel hosts.
  No tool in the repo uses that route for a tunnel host name (the access log shows the gateway only
  on the `:8600` egress listener, which is not guarded).
- `tests/scripts/test_fre1239_otlp_ingress_smoke.py` swaps the gateway literal for an unused
  address, since its client always arrives as the gateway.
