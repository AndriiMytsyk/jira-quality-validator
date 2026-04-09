#!/bin/bash
# setup_scheduler.sh
# Installs a macOS launchd job that runs jira_quality_checker.py every hour.
# Run once with:  bash setup_scheduler.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.jira.quality.validator"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PYTHON="$(which python3)"
LOG_DIR="$HOME/Library/Logs/JiraQualityValidator"

mkdir -p "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT_DIR}/jira_quality_checker.py</string>
    </array>

    <!-- Run once a day at 11:30 AM local time (Europe/Kiev) -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>11</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>

    <!-- Keep env vars available -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF

echo "✅  plist written to: $PLIST_PATH"

# Unload first in case it was already registered
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

echo "✅  Scheduler loaded. The checker will run now and then every hour."
echo ""
echo "Useful commands:"
echo "  Check status : launchctl list | grep jira"
echo "  View logs    : tail -f ${LOG_DIR}/stdout.log"
echo "  Stop         : launchctl unload ${PLIST_PATH}"
echo "  Start again  : launchctl load -w ${PLIST_PATH}"
