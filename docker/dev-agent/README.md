# Development agent: network, proxy and images

ADR-0159 (D3.3, D4.1, D4.2, D4.5, D2.1, D7.1, D7.3, D8.8). Ticket FRE-1682.

The gateway starts one agent container for each job. It also starts a check container and a handoff
container. This directory holds the images and the egress proxy. The container boundary is data in
`config/dev_agent/container_profiles.yaml`.

## Images

| Image | Built from | Holds | Network |
|-------|-----------|-------|---------|
| `seshat-dev-agent:0.1` | `Dockerfile.browser`, target `agent` | OpenCode (pinned), git, Chromium, Playwright, CSP test server, probe | `dev-agent` |
| `seshat-dev-check:0.1` | `Dockerfile.browser`, target `check` | Chromium, Playwright, CSP test server, probe | none |
| `seshat-dev-handoff:0.1` | `Dockerfile.handoff` | git, `handoff-bundle.sh` | none |
| `seshat-dev-proxy:0.1` | `Dockerfile.proxy` | Squid, `squid.conf` | `dev-agent` and `dev-agent-egress` |

Build all four: `make dev-agent-build`. The proxy also builds with `docker compose up` because it is a compose service.

## The network

- `dev-agent` is `internal: true`. It has no route to the internet, the data stores, Caddy or the gateway.
- Only `dev-agent-proxy` joins it from compose. The gateway starts the job containers on it.
- `dev-agent-egress` is the proxy's way out. Only the proxy joins it.
- The proxy tunnels HTTPS (`CONNECT`) to `registry.npmjs.org` on port 443. It refuses plain HTTP, other ports, other hosts and any IP address.
- The agent uses it through `HTTPS_PROXY=http://dev-agent-proxy:3128`.

## Check the setup

```bash
make dev-agent-build
make dev-agent-verify ARGS="--compose-project <throwaway> --up --down --output report.json"
```

Use a throwaway project name. The script refuses `--up` and `--down` for the project `seshat`, because
`down` would stop production and `up` is a deploy. After a deploy, run it with no flag. It then uses the
running proxy of project `seshat`.

The script starts canary listeners on a separate network, on the production ports. It does not connect to
production containers unless you pass `--include-production-ips`. It runs the probe in a container built
from the spec, compares `docker inspect` with the spec, and runs the same probe on the canary network as a
control. A probe that never reaches anything proves nothing, so the control must see every canary.

## Notes for the next tickets

- **OpenCode pin.** `config/dev_agent/container_profiles.yaml` (`opencode_version`) and `ARG OPENCODE_VERSION` in
  `Dockerfile.browser` hold the same value. A test fails if they differ. OpenCode 2.x is the npm package
  `@opencode/cli`. The command prints `opencode v<version>`.
- **Permission file.** `/etc/opencode/opencode.json` is `{}` (`OPENCODE_CONFIG` points to it). No rule matches, so OpenCode asks, and a headless job cannot answer. FRE-1684 replaces the file with the D2.4 permission file.
- **Toolkit mount.** The read-only toolkit volume mounts at `/srv/artifact-lib`. Do not mount at `/lib`: on the base image `/lib` is a symlink to `usr/lib`, and a volume there replaces the system libraries. The CSP server serves one root. To serve the toolkit under `/lib/`, link it into the root: `ln -s /srv/artifact-lib <root>/lib`.
- **Console message `webrtc`.** The production CSP has a `webrtc` directive. Chromium does not know it. It logs
  `Unrecognized Content-Security-Policy directive 'webrtc'` as a console error on every page. The publish check
  (D7.4, "zero console errors") must ignore that one message.
- **CSP manifest.** `csp-manifest.json` holds the placeholder origins. A test pins it to `expected_csp_directives()`.
  On a production host the real `frame-ancestors` origin differs. The check does not frame the page, so this does not change its result.
- **Chromium.** Playwright starts Chromium with `--no-sandbox` by default. The container is the boundary. `/dev/shm` is 64 MB, so tests pass `--disable-dev-shm-usage`.
- **Handoff.** `handoff-bundle.sh` sets every git setting that can run a program on the command line. The container has no network and no secret. Both layers apply.
- **Residual risk.** The proxy decides by the host name of the `CONNECT` target. It cannot read the encrypted tunnel.
  The agent can send data to `registry.npmjs.org`. It cannot send it anywhere else.
- **Observed on 2026-11-06.** On `dev-agent`, external names do not resolve (no DNS path out). A connection to the network's `.1` address on port 22 times out.
