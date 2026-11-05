"""Operate Sequoia's real switches without importing AppKit or activating Python.

Two backends with the same read()/set() interface:
- DirectSwitches: libAccessibility's own setter, for switches whose effective
  value lives in libAccessibility (Prefer non-blinking cursor). No window.
- AccessibilityUI: clicks the switch in System Settings > Accessibility >
  Display (Reduce motion has no working direct setter on Sequoia: the
  libAccessibility one does not change com.apple.universalaccess, which is
  what macOS animations follow).
"""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time
from settings_commands import run_command

LABELS = ('Reduce motion', 'Prefer non-blinking cursor')
SCRIPT = Path(__file__).with_name('accessibility.applescript')
DISPLAY_PANE = 'x-apple.systempreferences:com.apple.preference.universalaccess?Seeing_Display'
LIBACCESSIBILITY = '/usr/lib/libAccessibility.dylib'


class DirectSwitches:
    """libAccessibility getter/setter pairs. Verified by reading the stored
    preference back from a separate process (defaults), not only in-process."""
    FUNCTIONS = {
        'Prefer non-blinking cursor': ('_AXSPrefersNonBlinkingCursorIndicator',
                                       '_AXSSetPrefersNonBlinkingCursorIndicator',
                                       ('com.apple.Accessibility', 'PrefersNonBlinkingCursorIndicator')),
    }

    def __init__(self):
        # Safety interlock: unit tests must never flip the real Mac setting.
        if 'unittest' in sys.modules or os.environ.get('MAC84_NO_DIRECT_SWITCHES'):
            raise RuntimeError('direct switches disabled (tests or MAC84_NO_DIRECT_SWITCHES)')
        lib = ctypes.CDLL(LIBACCESSIBILITY)
        self.calls = {}
        for label, (getter, setter, _) in self.FUNCTIONS.items():
            get, put = getattr(lib, getter), getattr(lib, setter)  # AttributeError if absent
            get.restype, get.argtypes = ctypes.c_bool, []
            put.restype, put.argtypes = None, [ctypes.c_bool]
            self.calls[label] = (get, put)

    def supports(self, label):
        return label in self.calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _stored(self, label):
        domain, key = self.FUNCTIONS[label][2]
        result = run_command(['defaults', 'read', domain, key])
        value = result.stdout.strip().lower() if result.returncode == 0 else ''
        return '1' if value in ('1', 'true', 'yes') else '0' if value in ('0', 'false', 'no') else None

    def read(self, label):
        stored = self._stored(label)
        if stored is not None:
            return stored
        return '1' if self.calls[label][0]() else '0'  # key absent: library default

    def set(self, label, value, expected):
        if value not in ('0', '1'):
            raise ValueError('Invalid switch value')
        current = self.read(label)
        if current != expected:
            return 'conflict'
        if current == value:
            return current
        self.calls[label][1](value == '1')
        for _ in range(20):
            if self._stored(label) == value and self.calls[label][0]() == (value == '1'):
                return value
            time.sleep(.1)
        raise RuntimeError(f'{label}: direct setting did not take effect')


class AccessibilityUI:
    def __enter__(self):
        was_running = self._settings_running()
        self._open()
        if not was_running:
            # Cold launch: Sequoia's System Settings can come up on its last
            # page and drop the Display deep link (seen in the v0.3.6 log as
            # "Could not find" with no other error). Once it is running the
            # link navigates reliably, so send it again.
            for _ in range(30):
                if self._settings_running():
                    break
                time.sleep(.2)
            time.sleep(1.0)
            self._open()
        return self

    @staticmethod
    def _settings_running():
        return run_command(['pgrep', '-x', 'System Settings']).returncode == 0

    @staticmethod
    def _open():
        opened = run_command(['open', DISPLAY_PANE])
        if opened.returncode:
            raise RuntimeError(opened.stderr.strip() or 'Could not open Display settings')

    def read(self, label):
        return self._call('read', label)

    def set(self, label, value, expected):
        return self._call('set', label, value, expected)

    def _call(self, action, label, *values):
        if label not in LABELS:
            raise ValueError('Unsupported accessibility setting')
        for attempt in range(2):
            try:
                result = run_command(['osascript', str(SCRIPT), action, label, *values], timeout=25)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f'{label}: System Events timed out. Mirroring continues. '
                                   'Check Terminal Accessibility and Automation permissions.') from None
            if result.returncode:
                problem = result.stderr.strip() or result.stdout.strip() or \
                    f'{label}: osascript exit {result.returncode}'
                if attempt == 0 and 'Could not find' in problem:
                    # System Settings is on another page (or closed): navigate again.
                    self._open()
                    time.sleep(1.0)
                    continue
                raise RuntimeError(problem)
            actual = result.stdout.strip()
            if actual not in ('0', '1', 'conflict'):
                raise RuntimeError(f'Unexpected accessibility response for {label}: {actual!r}')
            return actual

    def __exit__(self, *args):
        # Leave the panel visible so the result can be inspected. Do not query
        # the frontmost application or activate another process.
        return False
