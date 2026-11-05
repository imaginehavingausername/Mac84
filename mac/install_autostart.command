#!/bin/zsh
# Mac84 auto-start: open the Mac84 bridge whenever the calculator is plugged in.
# Run once per Mac84 folder; running it
# from a newer version's folder switches auto-start to that folder.
# Undo with mac/uninstall_autostart.command.
HERE="${0:A:h:h}"
STATE_DIR="$HOME/Library/Application Support/Mac84"
LABEL=com.local.mac84watcher
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

print "Mac84 auto-start installer"
print "Mac84 folder: $HERE"
if [[ ! -f "$HERE/mac/mac84_bridge.py" || ! -f "$HERE/mac/mac84_watcher.sh" ]]; then
  print "ERROR: run this from inside the Mac84 folder (mac/install_autostart.command)."
  exit 1
fi

# 1. Python environment for this folder.
if [[ ! -x "$HERE/.venv/bin/python" ]]; then
  print "Creating the Python environment (one time)..."
  /usr/bin/python3 -m venv "$HERE/.venv" &&
    "$HERE/.venv/bin/python" -m pip install -q -r "$HERE/mac/requirements.txt" || {
      print "ERROR: Python setup failed; nothing was installed."; exit 1; }
fi

# 2. Watcher files (outside Downloads/Desktop, so launchd can always read them).
mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents" || exit 1
print -r -- "$HERE" > "$STATE_DIR/autostart_folder"
cp "$HERE/mac/mac84_watcher.sh" "$STATE_DIR/mac84-watcher.sh" && chmod 755 "$STATE_DIR/mac84-watcher.sh" || exit 1
chmod 755 "$HERE/mac/mac84_session.command"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$STATE_DIR/mac84-watcher.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null || { print "ERROR: bad LaunchAgent file $PLIST"; exit 1; }

# 3. (Re)load the watcher.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
launchctl enable "$DOMAIN/$LABEL"
if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
  print "ERROR: launchctl could not load $PLIST"; exit 1
fi
sleep 1
if launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -q "state = running"; then
  print "Auto-start is ON. Plug in the calculator: a Terminal window opens and waits"
  print "for MAC84. Unplug it: Mac settings are restored and the window closes."
  print "Watcher log: $STATE_DIR/autostart.log"
  print "Bridge log:  ~/Library/Logs/Mac84/last_auto_session.log"
else
  print "WARNING: the watcher was loaded but is not running. Check: launchctl print $DOMAIN/$LABEL"
fi
