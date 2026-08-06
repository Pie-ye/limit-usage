#!/usr/bin/env bash
# Install limit-usage as a systemd --user service (starts on boot when linger is on).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/limit-usage.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/limit-usage.service"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Missing $ROOT/.venv — create venv and install requirements first." >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
# Rewrite WorkingDirectory / paths if repo is not under /home/pieye/Container/limit-usage
sed \
  -e "s|/home/pieye/Container/limit-usage|$ROOT|g" \
  "$UNIT_SRC" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now limit-usage.service

# Boot without interactive login (needed for user services)
if command -v loginctl >/dev/null 2>&1; then
  if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
    echo "Enabling linger for $USER (so service starts at boot)..."
    loginctl enable-linger "$USER" || sudo loginctl enable-linger "$USER"
  fi
fi

echo "Installed and started: limit-usage.service"
systemctl --user --no-pager status limit-usage.service || true
echo
echo "Useful commands:"
echo "  systemctl --user status limit-usage"
echo "  systemctl --user restart limit-usage"
echo "  systemctl --user stop limit-usage"
echo "  journalctl --user -u limit-usage -f"
echo "Dashboard: http://127.0.0.1:50048"
