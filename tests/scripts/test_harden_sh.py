# ruff: noqa: D103
"""FRE-1665 — static guards for infrastructure/scripts/harden.sh and the fail2ban template.

Static: reads the committed script text. Proves what is decidable from the repo alone (AC-5).
The live rules (AC-1 to AC-4) are applied and proven by master on the host.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARDEN_SH = REPO_ROOT / "infrastructure" / "scripts" / "harden.sh"
JAIL_TEMPLATE = REPO_ROOT / "infrastructure" / "scripts" / "fail2ban-jail.local.example"

# IPv4 INPUT rules that have no IPv6 twin: ping (IPv6 uses the icmpv6 rule) and the LOG rule.
_V4_ONLY = ("-p icmp --icmp-type echo-request", "-j LOG")
_V6_ONLY = ("-p icmpv6",)


def _input_rules(command: str) -> set[str]:
    """Return the normalised `-A INPUT` rules the script adds with `command`."""
    pattern = re.compile(rf"^{command}\s+-A INPUT\s+(.*)$", re.MULTILINE)
    return {
        re.sub(r"\s+", " ", m.group(1)).strip() for m in pattern.finditer(HARDEN_SH.read_text())
    }


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(HARDEN_SH)], check=True)


def test_ipv6_input_rules_match_ipv4_rules() -> None:
    v4 = {r for r in _input_rules("iptables") if not any(k in r for k in _V4_ONLY)}
    v6 = {r for r in _input_rules("ip6tables") if not any(k in r for k in _V6_ONLY)}
    assert v6 == v4, f"IPv6-only: {sorted(v6 - v4)}; IPv4-only: {sorted(v4 - v6)}"


def test_ipv6_allows_ssh_http_https_and_icmpv6() -> None:
    v6 = _input_rules("ip6tables")
    for port in ('"$SSH_PORT"', "80", "443"):
        assert any(f"--dport {port}" in r and "-j ACCEPT" in r for r in v6), (
            f"no ip6tables rule for {port}"
        )
    assert any("-p icmpv6" in r and "-j ACCEPT" in r for r in v6), "icmpv6 must stay open"


def test_input_policy_drops_only_after_the_last_allow_rule() -> None:
    # A rerun flushes the rules. If the policy stays DROP during the flush, a live session loses
    # its ESTABLISHED rule. The policy must be ACCEPT before the flush and DROP after the last rule.
    lines = HARDEN_SH.read_text().splitlines()
    for command in ("iptables", "ip6tables"):
        at = {
            "accept": [
                i for i, ln in enumerate(lines) if re.match(rf"{command}\s+-P INPUT\s+ACCEPT", ln)
            ],
            "flush": [i for i, ln in enumerate(lines) if ln.strip() == f"{command} -F"],
            "last_rule": [
                i for i, ln in enumerate(lines) if re.match(rf"{command}\s+-A INPUT", ln)
            ],
            "drop": [
                i for i, ln in enumerate(lines) if re.match(rf"{command}\s+-P INPUT\s+DROP", ln)
            ],
        }
        assert all(len(v) >= 1 for v in at.values()), f"{command}: missing step in {at}"
        assert max(at["accept"]) < min(at["flush"]), (
            f"{command}: INPUT must be ACCEPT before the flush"
        )
        assert min(at["drop"]) > max(at["last_rule"]), (
            f"{command}: DROP must come after the last rule"
        )


def test_ssh_dropin_sorts_before_cloud_init() -> None:
    text = HARDEN_SH.read_text()
    written = re.findall(r"cat > /etc/ssh/sshd_config\.d/(\S+)", text)
    assert len(written) == 1, f"expected one sshd drop-in, found {written}"
    assert written[0] < "50-cloud-init.conf", f"{written[0]} loads after 50-cloud-init.conf"
    # The stale file from the previous version would still carry settings; the script removes it.
    assert "rm -f /etc/ssh/sshd_config.d/99-hardening.conf" in text


def test_script_confirms_password_auth_off_with_sshd_t() -> None:
    text = HARDEN_SH.read_text()
    assert re.search(r"sshd -T.*passwordauthentication no", text, re.DOTALL)


def test_fail2ban_template_pins_ipv6_capable_banaction() -> None:
    text = JAIL_TEMPLATE.read_text()
    # Only the [sshd] section: stop at the next section header.
    after_header = re.split(r"^\[sshd\]$", text, maxsplit=1, flags=re.MULTILINE)[1]
    sshd_section = re.split(r"^\[", after_header, maxsplit=1, flags=re.MULTILINE)[0]
    assert re.search(r"^banaction\s*=\s*iptables-multiport\s*$", sshd_section, re.MULTILINE)
