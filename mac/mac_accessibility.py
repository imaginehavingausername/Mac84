"""Operate Sequoia's real switches without importing AppKit or activating Python."""
from pathlib import Path
import subprocess
from settings_commands import run_command

LABELS = ('Reduce motion', 'Prefer non-blinking cursor')
SCRIPT = Path(__file__).with_name('accessibility.applescript')


class AccessibilityUI:
    def __enter__(self):
        opened = run_command(['open',
            'x-apple.systempreferences:com.apple.preference.universalaccess?Seeing_Display'])
        if opened.returncode:
            raise RuntimeError(opened.stderr.strip() or 'Could not open Display settings')
        return self

    def read(self, label):
        return self._call('read', label)

    def set(self, label, value, expected):
        return self._call('set', label, value, expected)

    def _call(self, action, label, *values):
        if label not in LABELS:
            raise ValueError('Unsupported accessibility setting')
        try:
            result = run_command(['osascript', str(SCRIPT), action, label, *values], timeout=25)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'{label}: System Events timed out. Mirroring continues. '
                               'Check Terminal Accessibility and Automation permissions.') from None
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or
                               f'{label}: osascript exit {result.returncode}')
        actual = result.stdout.strip()
        if actual not in ('0', '1', 'conflict'):
            raise RuntimeError(f'Unexpected accessibility response for {label}: {actual!r}')
        return actual

    def __exit__(self, *args):
        # Leave the panel visible so the result can be inspected. Do not query
        # the frontmost application or activate another process.
        return False
