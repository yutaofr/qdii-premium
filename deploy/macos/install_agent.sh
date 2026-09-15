#!/bin/bash
# 安装 / 重装采集 LaunchAgent（用户级，无需 sudo）。
set -euo pipefail

LABEL="com.qdii-premium.collector"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="${QDII_DATA_ROOT:-$HOME/qdii-data}"
PYTHON="$REPO/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[ -x "$PYTHON" ] || { echo "missing $PYTHON; run 'uv sync' in $REPO first" >&2; exit 1; }
mkdir -p "$DATA/logs" "$HOME/Library/LaunchAgents"

sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__REPO__#$REPO#g" -e "s#__DATA__#$DATA#g" \
  "$REPO/deploy/macos/$LABEL.plist.template" > "$PLIST"
plutil -lint "$PLIST"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"
launchctl print "$DOMAIN/$LABEL" | grep -E "state|pid" || true
