"""Isolate optional settings automation from the USB request/ACK loop."""
from pathlib import Path
import signal
import subprocess
import sys
import threading
from mac_settings import MacSettings, JOURNAL, _finish_recovery
from settings_commands import stop_requested, SettingsCancelled


class SettingsProcess:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.process = None
        self.started = False

    def __enter__(self):
        return self

    def start(self):
        if self.started or not self.enabled:
            return
        self.started = True
        print('[settings] Starting after the first image; mirroring continues.', flush=True)
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-u', str(Path(__file__).resolve()), '--session'],
                stdin=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            print('[settings] Could not start helper:', exc, flush=True)

    def __exit__(self, *args):
        if self.process is None:
            return False
        # EOF also reaches the helper if the parent crashes. Its only thread
        # watches this pipe; all preference and AppleScript work is in its main thread.
        if self.process.stdin:
            self.process.stdin.close()
        print('[settings] Restoring saved preferences…', flush=True)
        with _finish_recovery():
            try:
                result = self.process.wait(timeout=55)
            except subprocess.TimeoutExpired:
                # The helper owns the journal/lock. Do not kill it mid-restore.
                print('[settings] Recovery is still running. Backup retained at', JOURNAL,
                      'until recovery succeeds.', flush=True)
            else:
                if result:
                    print('[settings] Recovery needs attention; see the error above. '
                          'Retry --restore-settings after this bridge exits.', flush=True)
        return False


def run_session():
    def wait_for_parent():
        try:
            sys.stdin.buffer.read()
        finally:
            stop_requested.set()

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: stop_requested.set())
    threading.Thread(target=wait_for_parent, name='mac84-settings-stop', daemon=True).start()
    try:
        with MacSettings():
            stop_requested.wait()
        return 0
    except SettingsCancelled:
        # MacSettings.__enter__ has already attempted journal recovery.
        return 1 if JOURNAL.exists() else 0
    except Exception as exc:
        print('[settings] ERROR:', exc, flush=True)
        return 1


if __name__ == '__main__':
    if sys.argv[1:] != ['--session']:
        raise SystemExit('Start mac84_bridge.py; this is its internal settings helper.')
    raise SystemExit(run_session())
