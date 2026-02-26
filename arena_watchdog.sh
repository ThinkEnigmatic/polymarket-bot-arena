#!/bin/bash
# Watchdog for the arena process.
# Restarts the arena if its log file hasn't been updated in 5 minutes.

LOG="/Users/ben/clawd/trading_bot/logs/arena.log"
WATCHDOG_LOG="/Users/ben/clawd/trading_bot/logs/arena_watchdog.log"
STALE_SECONDS=300  # 5 minutes

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$WATCHDOG_LOG"
}

if [ ! -f "$LOG" ]; then
    log "WARN: arena.log not found, skipping check"
    exit 0
fi

# Get seconds since last modification
if [[ "$OSTYPE" == "darwin"* ]]; then
    LAST_MOD=$(stat -f %m "$LOG")
else
    LAST_MOD=$(stat -c %Y "$LOG")
fi
NOW=$(date +%s)
AGE=$((NOW - LAST_MOD))

if [ "$AGE" -gt "$STALE_SECONDS" ]; then
    log "RESTART: arena.log is ${AGE}s stale (threshold: ${STALE_SECONDS}s), restarting arena"
    launchctl kickstart -k "gui/$(id -u)/com.polymarket.botarena" >> "$WATCHDOG_LOG" 2>&1
    log "Restart command sent"
else
    log "OK: arena.log updated ${AGE}s ago"
fi
