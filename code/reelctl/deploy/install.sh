#!/bin/bash
# Reel Studio launchd install.
#
# Installs exactly two services and touches nothing else on this machine. Every launchctl
# verb below is scoped to one of the two labels named here — there is no glob, no
# `bootout gui/501` of a domain, and no kickstart of anything that already exists. Never
# restarting services this project does not own is enforced structurally: this script
# cannot name a service it was not written to own.
#
# The shipped plists carry placeholders (__HOME__, __REEL_FACTORY_HOME__) because launchd
# does not expand `~`; they are rendered here. REEL_FACTORY_HOME defaults to
# $HOME/reel-production.
set -euo pipefail

APP_LABEL="com.reelfactory.reel-studio"
DAEMON_LABEL="com.reelfactory.reel-studio-daemon"
DOMAIN="gui/$(id -u)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
REEL_FACTORY_HOME="${REEL_FACTORY_HOME:-$HOME/reel-production}"
LOGS="$REEL_FACTORY_HOME/.studio/logs"
PORT=7335

say() { printf '%s\n' "$*"; }

mkdir -p "$AGENTS" "$LOGS"

for label in "$APP_LABEL" "$DAEMON_LABEL"; do
  src="$HERE/$label.plist"
  dst="$AGENTS/$label.plist"
  [ -f "$src" ] || { say "missing plist: $src"; exit 1; }
  /usr/bin/sed -e "s#__REEL_FACTORY_HOME__#$REEL_FACTORY_HOME#g" -e "s#__HOME__#$HOME#g" "$src" > "$dst"
  /usr/bin/plutil -lint "$dst" >/dev/null
  say "installed $dst"

  # Only ever boots out the label this script owns, and only to replace it.
  if /bin/launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    say "replacing running $label"
    /bin/launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # bootout is asynchronous; bootstrap fails with EBUSY if the old job is still dying.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      /bin/launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
      sleep 1
    done
  fi

  /bin/launchctl bootstrap "$DOMAIN" "$dst"
  say "bootstrapped $DOMAIN/$label"
done

say ""
say "waiting for the app to answer on 127.0.0.1:$PORT"
for _ in $(seq 1 20); do
  if /usr/bin/curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    say "health endpoint is answering"
    break
  fi
  sleep 1
done

say ""
/bin/launchctl print "$DOMAIN/$APP_LABEL" | /usr/bin/grep -E "state|pid|program|last exit" || true
/bin/launchctl print "$DOMAIN/$DAEMON_LABEL" | /usr/bin/grep -E "state|pid|program|last exit" || true
say ""
say "logs: $LOGS"
say "board: http://127.0.0.1:$PORT/"
