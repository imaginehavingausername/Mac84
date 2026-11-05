#!/bin/zsh
# Mac84 auto-start watcher (LaunchAgent com.local.mac84watcher).
# install_autostart.command copies this to ~/Library/Application Support/Mac84.
#
# Plug in the calculator -> opens a Terminal window running the Mac84 bridge in
# --auto mode (it waits for MAC84 to be started on the calculator).
# Unplug it -> the bridge restores Mac settings and exits on its own; this then
# closes that window. A window that is still busy is never force-closed.
#
# Reads only ~/Library/Application Support/Mac84; Terminal opens the bridge, so
# this background job never needs access to Downloads or Desktop.

STATE_DIR="$HOME/Library/Application Support/Mac84"
CONFIG="$STATE_DIR/autostart_folder"
LOG="$STATE_DIR/autostart.log"
CONNECTED=0
MISSING=0
CLOSE_PENDING=0
CLOSE_DEADLINE=0
WINDOW_ID=""

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

calculator_present() {
  # TI vendor ID 0x0451 = 1105: TI-OS link mode before MAC84 starts, and MAC84's own port.
  /usr/sbin/ioreg -p IOUSB -l -w 0 2>/dev/null | grep -Eq '"idVendor" = 1105([^0-9]|$)|TI-84 Plus CE'
}

window_state() {  # prints busy, idle or gone. Never launches Terminal.
  if [[ -z "$WINDOW_ID" ]]; then print gone; return; fi
  /usr/bin/osascript - "$WINDOW_ID" 2>/dev/null <<'APPLESCRIPT' || print gone
on run argv
  if application "Terminal" is not running then return "gone"
  set targetID to (item 1 of argv) as integer
  tell application "Terminal"
    if not (exists window id targetID) then return "gone"
    if busy of selected tab of window id targetID then return "busy"
    return "idle"
  end tell
end run
APPLESCRIPT
}

close_window() {
  [[ -n "$WINDOW_ID" ]] || return
  /usr/bin/osascript - "$WINDOW_ID" >/dev/null 2>&1 <<'APPLESCRIPT'
on run argv
  if application "Terminal" is not running then return
  set targetID to (item 1 of argv) as integer
  tell application "Terminal"
    if (exists window id targetID) and not (busy of selected tab of window id targetID) then close window id targetID
  end tell
end run
APPLESCRIPT
  log "Closed Mac84 window $WINDOW_ID"
  WINDOW_ID=""
}

start_session() {
  local folder=""
  [[ -r "$CONFIG" ]] && folder=$(<"$CONFIG")
  if [[ -z "$folder" ]]; then
    log "No Mac84 folder configured. Run mac/install_autostart.command again."
    return
  fi
  if /usr/bin/pgrep -f "mac/mac84_bridge.py" >/dev/null 2>&1; then
    log "A Mac84 bridge is already running; not opening another."
    return
  fi
  WINDOW_ID=$(/usr/bin/osascript - "$folder/mac/mac84_session.command" 2>>"$LOG" <<'APPLESCRIPT'
on run argv
  set commandText to "exec /bin/zsh " & quoted form of (item 1 of argv)
  set wasRunning to application "Terminal" is running
  tell application "Terminal"
    if wasRunning then
      do script commandText
    else
      -- A fresh Terminal opens its own window; run in it instead of a second one.
      launch
      repeat 30 times
        if (count windows) > 0 then exit repeat
        delay 0.1
      end repeat
      if (count windows) > 0 then
        do script commandText in window 1
      else
        do script commandText
      end if
    end if
    return id of window 1 as string
  end tell
end run
APPLESCRIPT
)
  [[ "$WINDOW_ID" == <-> ]] || WINDOW_ID=""
  log "Calculator connected; opened Mac84 from $folder (window ${WINDOW_ID:-unknown})"
}

trap 'exit 0' TERM INT
log "Watcher started"

while true; do
  if calculator_present; then
    MISSING=0
    if (( ! CONNECTED )); then
      CONNECTED=1
      CLOSE_PENDING=0
      if [[ "$(window_state)" == busy ]]; then
        # Replugged while the last window is still running: its --auto bridge
        # keeps waiting for MAC84, so reuse it rather than start a second one.
        log "Calculator reconnected; reusing running Mac84 window $WINDOW_ID"
      else
        close_window
        start_session
      fi
    fi
  else
    (( MISSING++ ))
    # ~10 s absent: real unplug, not MAC84 starting/quitting (USB re-enumerates).
    if (( CONNECTED && MISSING >= 5 )); then
      CONNECTED=0
      CLOSE_PENDING=1
      CLOSE_DEADLINE=$(( SECONDS + 180 ))
      log "Calculator unplugged"
    fi
  fi
  if (( CLOSE_PENDING )); then
    case "$(window_state)" in
      gone) CLOSE_PENDING=0; WINDOW_ID="" ;;
      idle) close_window; CLOSE_PENDING=0 ;;
      busy) if (( SECONDS > CLOSE_DEADLINE )); then
              log "Mac84 window $WINDOW_ID still busy after 3 minutes; left open"
              CLOSE_PENDING=0
            fi ;;
    esac
  fi
  sleep 2
done
