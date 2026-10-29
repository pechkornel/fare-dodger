#!/usr/bin/env bash
# Install the host-side updater behind the admin page's "Update now" button (run once):
#   ./deploy/install-updater.sh
# It adds two systemd units: a path unit that watches data/admin/update-request, and a
# one-shot service that runs deploy/update.sh as you (your git login, your Docker group).
# Remove with: sudo systemctl disable --now fare-dodger-update.path && \
#   sudo rm /etc/systemd/system/fare-dodger-update.{path,service} && sudo systemctl daemon-reload
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
user=$(id -un)
mkdir -p "$repo/data/admin"
chmod +x "$repo/deploy/update.sh"

sudo tee /etc/systemd/system/fare-dodger-update.service >/dev/null <<UNIT
[Unit]
Description=Update Fare Dodger (requested from its admin page)

[Service]
Type=oneshot
User=$user
WorkingDirectory=$repo
ExecStart=$repo/deploy/update.sh
UNIT

sudo tee /etc/systemd/system/fare-dodger-update.path >/dev/null <<UNIT
[Unit]
Description=Watch for Fare Dodger update requests

[Path]
PathExists=$repo/data/admin/update-request
Unit=fare-dodger-update.service

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now fare-dodger-update.path
echo "Installed. The admin page's Update now button now runs $repo/deploy/update.sh as $user."
