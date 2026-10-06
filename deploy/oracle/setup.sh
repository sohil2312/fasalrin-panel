#!/usr/bin/env bash
# One-time setup of the Fasalrin panel on an Oracle Cloud "Always Free" Ubuntu 24.04 (Ampere / arm64) machine.
# Run it from inside the cloned repo:   bash deploy/oracle/setup.sh
#
# Installs: a light desktop (XFCE) + Remote Desktop (xrdp), Tailscale, Python venv, Playwright + Chromium.
# Remote Desktop is only reachable over Tailscale (your own devices); no new port is opened to the internet.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ME="$(id -un)"
echo "== Fasalrin setup in $REPO for user $ME"

echo "== 1/6 system packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip git curl \
    xfce4 xfce4-goodies xrdp dbus-x11 firefox iptables-persistent

echo "== 2/6 time zone India (records and dates use local time)"
sudo timedatectl set-timezone Asia/Kolkata

echo "== 3/6 Remote Desktop (xrdp) with the XFCE desktop"
echo "xfce4-session" > "$HOME/.xsession"
sudo adduser xrdp ssl-cert >/dev/null 2>&1 || true
sudo systemctl enable --now xrdp

echo "== 4/6 Tailscale"
if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi
# Oracle's Ubuntu image rejects every port except SSH. Allow traffic that comes over Tailscale only.
if ! sudo iptables -C INPUT -i tailscale0 -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 1 -i tailscale0 -j ACCEPT
    sudo netfilter-persistent save
fi

echo "== 5/6 Python environment + Playwright Chromium"
cd "$REPO"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m playwright install-deps chromium
.venv/bin/python -m playwright install chromium

echo "== 6/6 desktop shortcut 'Fasalrin panel'"
chmod +x "$REPO/deploy/oracle/start-panel.sh"
mkdir -p "$HOME/Desktop" "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/fasalrin-panel.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Fasalrin panel
Exec=xfce4-terminal --title "Fasalrin panel" --hold -x "$REPO/deploy/oracle/start-panel.sh"
Icon=applications-internet
Terminal=false
EOF
cp "$HOME/.local/share/applications/fasalrin-panel.desktop" "$HOME/Desktop/"
chmod +x "$HOME/Desktop/fasalrin-panel.desktop"

echo
echo "== Done. Still to do by you:"
echo "   1. sudo tailscale up          (open the link it prints, sign in with your Tailscale account)"
echo "   2. sudo passwd $ME            (the password you will type in Remote Desktop)"
echo "   3. On your laptop: Remote Desktop to this machine's Tailscale name / 100.x address"
