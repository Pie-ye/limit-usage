#!/usr/bin/env bash
# Switch production from user systemd → Docker Compose.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
systemctl --user disable --now limit-usage.service 2>/dev/null || true
cd "$ROOT"
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:50048/api/health
echo
echo "limit-usage is on Docker (127.0.0.1:50048)"
