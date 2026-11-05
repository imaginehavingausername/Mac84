"""Bounded, cancellable commands used only by the settings helper."""
from contextlib import contextmanager
import subprocess
import threading
import time

stop_requested = threading.Event()
_recovering = 0


class SettingsCancelled(BaseException):
    pass


def check_cancelled():
    if not _recovering and stop_requested.is_set():
        raise SettingsCancelled()


@contextmanager
def recovery_commands():
    global _recovering
    _recovering += 1
    try:
        yield
    finally:
        _recovering -= 1


def run_command(command, timeout=8):
    check_cancelled()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as child:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    out, err = child.communicate(timeout=min(.2, remaining))
                    return subprocess.CompletedProcess(command, child.returncode, out, err)
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            child.terminate()
            try:
                child.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
            raise
