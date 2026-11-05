#!/bin/zsh
# Turn Mac84 auto-start off. The Mac84 folder and any running bridge are left alone.
STATE_DIR="$HOME/Library/Application Support/Mac84"
LABEL=com.local.mac84watcher
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
rm -f "$PLIST" "$STATE_DIR/mac84-watcher.sh" "$STATE_DIR/autostart_folder"
print "Mac84 auto-start is OFF."
