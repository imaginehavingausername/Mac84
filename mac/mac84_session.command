#!/bin/zsh
# Opened in Terminal by the Mac84 auto-start watcher when the calculator is
# plugged in. Waits for MAC84 on the calculator, reconnects after each
# session, and exits (restoring Mac settings first) once it is unplugged.
# Output also goes to ~/Library/Logs/Mac84/last_auto_session.log.
cd "${0:A:h:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "Mac84: no Python environment in $PWD."
  print "Run:  zsh '$PWD/mac/install_autostart.command'"
  read -k1 "?Press any key to close."
  exit 1
fi
LOGDIR="$HOME/Library/Logs/Mac84"
mkdir -p "$LOGDIR"
# script(1) keeps a real terminal (Ctrl+C works normally) while logging.
exec /usr/bin/script -q "$LOGDIR/last_auto_session.log" \
  ./.venv/bin/python mac/mac84_bridge.py --auto --port auto --pace 0.003 "$@"
