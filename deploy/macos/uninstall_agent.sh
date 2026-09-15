#!/bin/bash
set -euo pipefail
LABEL="com.qdii-premium.collector"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "removed $LABEL (data in ~/qdii-data is kept)"
