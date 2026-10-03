#!/bin/bash
# Reel Studio launchd uninstall.
#
# Removes exactly the two services install.sh created. Logs, studio.db, claims and every
# receipt are left on disk: the daemon's state is derived from disk and the receipts are
# evidence, so an uninstall that deleted them would be destroying the record of what the
# machine did, not cleaning up after it.
set -euo pipefail

APP_LABEL="com.reelfactory.reel-studio"
DAEMON_LABEL="com.reelfactory.reel-studio-daemon"
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"

say() { printf '%s\n' "$*"; }

for label in "$DAEMON_LABEL" "$APP_LABEL"; do
  if /bin/launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    /bin/launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    say "booted out $DOMAIN/$label"
  else
    say "$DOMAIN/$label was not loaded"
  fi
  if [ -f "$AGENTS/$label.plist" ]; then
    /bin/rm -f "$AGENTS/$label.plist"
    say "removed $AGENTS/$label.plist"
  fi
done

say ""
say "left in place: ${REEL_FACTORY_HOME:-$HOME/reel-production}/.studio (logs, studio.db, claims) and all receipts"
