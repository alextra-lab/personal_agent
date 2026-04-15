#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# VPS OS hardening script — Debian 12 (Bookworm)
#
# Usage: sudo bash harden.sh <ssh_port>
#
# BEFORE RUNNING:
#   1. Verify your SSH key auth is working: ssh -p <port> debian@<host>
#   2. Open a second SSH session as a keepalive — this script restarts sshd
#   3. Ensure the OVH network firewall Terraform has been applied first
#
# What this script does:
#   - Installs required security packages
#   - Hardens SSH daemon (disables root + password auth)
#   - Configures iptables with a minimal allow-list
#   - Persists iptables rules across reboots
#   - Configures fail2ban for the custom SSH port
#   - Applies kernel hardening via sysctl
#   - Enables automatic security updates
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Argument validation ───────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    echo "Usage: sudo bash harden.sh <ssh_port>" >&2
    exit 1
fi

SSH_PORT="$1"

if ! [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || [[ "$SSH_PORT" -le 1024 ]] || [[ "$SSH_PORT" -ge 65535 ]]; then
    echo "Error: ssh_port must be an integer between 1025 and 65534, got: $SSH_PORT" >&2
    exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
    echo "Error: this script must be run as root (sudo bash harden.sh ...)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> VPS hardening started (SSH port: $SSH_PORT)"

# ── 1. System update and package installation ─────────────────────────────────

echo "==> Updating system packages..."
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    fail2ban \
    iptables \
    iptables-persistent \
    netfilter-persistent \
    unattended-upgrades \
    apt-listchanges \
    curl \
    htop \
    vim

# ── 2. SSH hardening ──────────────────────────────────────────────────────────
# NOTE: We do NOT touch the Port directive here — that must already be set
# in /etc/ssh/sshd_config (OVH Debian images set this via cloud-init).
# This drop-in only enforces auth and session hardening.

echo "==> Hardening SSH daemon configuration..."

if [[ -f /etc/ssh/sshd_config ]]; then
    cp /etc/ssh/sshd_config "/etc/ssh/sshd_config.bak.$(date +%Y%m%d%H%M%S)"
fi

cat > /etc/ssh/sshd_config.d/99-hardening.conf << 'SSHEOF'
# SSH hardening — applied by harden.sh
# Do NOT set Port here; it must be in the main sshd_config.

PermitRootLogin no
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
UsePAM yes
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys

MaxAuthTries 3
MaxSessions 5
LoginGraceTime 30s
ClientAliveInterval 300
ClientAliveCountMax 2

AllowAgentForwarding no
AllowTcpForwarding no
X11Forwarding no
PrintMotd no

AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
SSHEOF

# Validate config before reloading
sshd -t
systemctl reload ssh
echo "    SSH daemon reloaded."

# ── 3. iptables rules ─────────────────────────────────────────────────────────
# Mirrors the OVH network firewall rules at the OS level (defence in depth).
# Docker will add its own FORWARD rules; we set INPUT/OUTPUT only.
# When Docker is installed it manages DOCKER-USER chain — do not conflict.

echo "==> Configuring iptables..."

# Flush all existing user-space rules
iptables -F
iptables -X
ip6tables -F
ip6tables -X

# Default policies
iptables  -P INPUT   DROP
iptables  -P FORWARD DROP
iptables  -P OUTPUT  ACCEPT
ip6tables -P INPUT   DROP
ip6tables -P FORWARD DROP
ip6tables -P OUTPUT  ACCEPT

# ── IPv4 ──
# Loopback — always allow
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Established / related connections (return traffic)
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# SSH
iptables -A INPUT -p tcp --dport "$SSH_PORT" -m conntrack --ctstate NEW -j ACCEPT

# HTTP and HTTPS (Caddy)
iptables -A INPUT -p tcp --dport 80  -m conntrack --ctstate NEW -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -m conntrack --ctstate NEW -j ACCEPT

# ICMP echo (ping)
iptables -A INPUT -p icmp --icmp-type echo-request -j ACCEPT

# Log and drop everything else
iptables -A INPUT -m limit --limit 5/min -j LOG --log-prefix "iptables-drop: " --log-level 7

# ── IPv6 ──
# Loopback
ip6tables -A INPUT  -i lo -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT
# Established
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# ICMPv6 (required for neighbour discovery, etc.)
ip6tables -A INPUT -p icmpv6 -j ACCEPT

# Persist rules across reboots
netfilter-persistent save
echo "    iptables rules saved."

# ── 4. sysctl kernel hardening ────────────────────────────────────────────────

echo "==> Applying kernel hardening (sysctl)..."
cp "$SCRIPT_DIR/sysctl-hardening.conf" /etc/sysctl.d/99-hardening.conf
sysctl --system --quiet
echo "    sysctl applied."

# ── 5. fail2ban ───────────────────────────────────────────────────────────────
# If the user already has a jail.local, we back it up and apply ours.
# Review /etc/fail2ban/jail.local.bak to merge any custom jails afterwards.

echo "==> Configuring fail2ban..."

if [[ -f /etc/fail2ban/jail.local ]]; then
    cp /etc/fail2ban/jail.local "/etc/fail2ban/jail.local.bak.$(date +%Y%m%d%H%M%S)"
    echo "    Existing jail.local backed up."
fi

# Install our jail.local, substituting the actual SSH port
sed "s/SSH_PORT_PLACEHOLDER/$SSH_PORT/" \
    "$SCRIPT_DIR/fail2ban-jail.local.example" \
    > /etc/fail2ban/jail.local

systemctl enable fail2ban
systemctl restart fail2ban
echo "    fail2ban configured and started."

# ── 6. Unattended security upgrades ──────────────────────────────────────────

echo "==> Enabling automatic security updates..."

cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'APTEOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
APTEOF

cat > /etc/apt/apt.conf.d/20auto-upgrades << 'APTEOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
APT::Periodic::Unattended-Upgrade "1";
APTEOF

systemctl enable unattended-upgrades
systemctl restart unattended-upgrades
echo "    Unattended-upgrades enabled."

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "==> Hardening complete. Summary:"
echo "    SSH port:         $SSH_PORT"
echo "    Root login:       disabled"
echo "    Password auth:    disabled"
echo "    iptables:         active (INPUT DROP default)"
echo "    fail2ban:         active"
echo "    Auto-upgrades:    enabled"
echo ""
echo "    Verify services:"
echo "      systemctl status ssh fail2ban netfilter-persistent"
echo ""
echo "    Verify iptables rules:"
echo "      iptables -L -n -v"
echo ""
echo "IMPORTANT: Keep your current SSH session open and verify you can"
echo "open a new connection before closing this terminal."
