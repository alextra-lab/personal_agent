# FRE-1665 — IPv6 firewall parity in harden.sh — Implementation Plan

> Ticket: FRE-1665 (Approved 2026-10-24). Master applies host rules live; this PR changes the repo only.
> Tier: Standard (firewall + SSH security). Codex plan-review required.

**Goal:** `infrastructure/scripts/harden.sh` produces the same IPv6 INPUT rules as IPv4, writes its sshd
settings in a file that sorts before `50-cloud-init.conf`, and the fail2ban template bans IPv6 addresses
through `ip6tables`. Master gets the exact live commands.

## Measured facts (2026-10-24, this host)

- fail2ban 1.1.0. `iptables-multiport` carries `[Init?family=inet6] iptables = ip6tables`, so it bans
  IPv6 addresses. `DNSUtils.IPv6IsAllowed()` returns `True` (auto-detect works; no `allowipv6` change needed).
- `jail.d/defaults-debian.conf` sets `banaction = nftables`. The repo template `fail2ban-jail.local.example`
  does not set `banaction`, so a fresh host would ban with nftables, not `ip6tables`. The live host's
  `jail.local` pins `iptables-multiport` (`fail2ban-client -d` shows `addaction iptables-multiport`).
- Live sshd: `Include sshd_config.d/*.conf` is the first directive. `99-hardening.conf` sorts after
  `50-cloud-init.conf` (`PasswordAuthentication yes`), and sshd keeps the first value.

## Steps

1. **Failing test first** — `tests/scripts/test_harden_sh.py` (static, reads the script as text):
   - IPv6 `-A INPUT` rules equal the IPv4 rules after normalisation, except: IPv4 `icmp echo-request`
     and the IPv4 `LOG` rule have no IPv6 twin; IPv6 has `icmpv6` instead.
   - The IPv6 set contains `--dport "$SSH_PORT"`, `--dport 80`, `--dport 443`.
   - The SSH drop-in file name sorts before `50-cloud-init.conf`; the script removes the old
     `99-hardening.conf`; the script runs `sshd -T` and fails if `passwordauthentication no` is absent.
   - `fail2ban-jail.local.example` pins `banaction = iptables-multiport` in `[sshd]`.
   - `bash -n infrastructure/scripts/harden.sh` exits 0.
   - Command: `make test-file FILE=tests/scripts/test_harden_sh.py` — expect failures before step 2.
2. **Edit `infrastructure/scripts/harden.sh`**:
   - Add IPv6 rules: tcp `$SSH_PORT`, 80, 443 (NEW, conntrack), same as IPv4.
   - Rename the drop-in to `01-hardening.conf`; `rm -f` the old `99-hardening.conf`.
   - After `systemctl reload ssh`, check `sshd -T` reports `passwordauthentication no`; else exit 1.
   - Update the header comment lines that describe the firewall and SSH steps.
3. **Edit `infrastructure/scripts/fail2ban-jail.local.example`**: add `banaction = iptables-multiport`
   to `[sshd]` with a comment that it is IPv6-capable and that the Debian default is nftables.
4. **Docs**: `docs/guides/CLOUD_DEPLOYMENT.md` §3.3 — one line on IPv6 parity if the section lists rules.
5. **Gates**: `make test-file FILE=tests/scripts/test_harden_sh.py` · `make test` · `make mypy` ·
   `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
6. **Runbook for master** (in the Linear handoff comment, not in the repo): add-then-drop order so an
   in-flight IPv6 SSH session never loses its allow rule; `ip6tables-save`-based persistence; AC-1..AC-4 checks.

## Out of scope

- No `allowipv6` change (auto-detect verified True).
- No IPv6 LOG rule (the ticket lists lo, established, icmpv6, SSH, 80, 443).
- The live `jail.local` differs from the repo template — see below.
- The live `jail.local` is a stock `jail.conf` copy with `[sshd] port = <ssh port>`, not the repo template. Reported, not changed.

## Codex plan-review outcome (2026-10-24)

- **Accepted:** a rerun flushed the ESTABLISHED rule while the policy stayed DROP. Fix: set `INPUT ACCEPT`
  before the flush and `DROP` after the last rule, for both families. New test:
  `test_input_policy_drops_only_after_the_last_allow_rule`. This replaces the earlier "out of scope" note.
- **Accepted:** the jail test read past the `[sshd]` section. It now stops at the next section header.
- **Refuted by measurement:** "a duplicate `Subsystem sftp` makes `sshd -t` fail". A throwaway config with the
  drop-in and the main file both defining `Subsystem sftp` gives `sshd -t` exit 0, and `sshd -T` reports
  `passwordauthentication no` when the `01-` file sets it before a `50-` file sets `yes`.
- **Not testable in the repo:** real connectivity, a reboot, a live fail2ban ban. These are AC-1 to AC-4 on the host.
