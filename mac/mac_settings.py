"""Mac84 settings: clock defaults plus verified Sequoia UI switches.

The journal is committed before any change. Recovery preserves later user edits.
A failed UI restore retains the journal for an explicit retry.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import platform
import signal
import time
import threading
from mac_accessibility import AccessibilityUI, LABELS
from settings_commands import run_command, check_cancelled, recovery_commands

STATE_DIR = Path.home() / 'Library' / 'Application Support' / 'Mac84'
JOURNAL = STATE_DIR / 'previous_settings.json'
LOCK = STATE_DIR / 'session.lock'
# Recover real sibling locations, not <current project>/Mac84_v0_2/mac.
_PROJECTS = (Path(__file__).resolve().parents[2], Path.home() / 'Downloads')
LEGACY_JOURNALS = tuple(dict.fromkeys(
    [Path(__file__).resolve().parent / '.mac84_previous_settings.json'] +
    [root / version / 'mac' / '.mac84_previous_settings.json'
     for root in _PROJECTS for version in ('Mac84_v0_2', 'Mac84_v0_3', 'Mac84_v0_3_1')]))
PREFS = [
    ('com.apple.menuextra.clock', 'ShowSeconds', '-bool', 'false'),
    ('com.apple.menuextra.clock', 'FlashDateSeparators', '-bool', 'false'),
]
LEGACY_TARGETS = {
    ('com.apple.universalaccess', 'reduceMotion'): 'true',
    ('com.apple.Accessibility', 'PrefersNonBlinkingCursorIndicator'): 'true',
}


def _supports_ui():
    return platform.system() == 'Darwin' and platform.mac_ver()[0].split('.')[0] == '15'


def _ui_warning(error):
    print('[settings] Accessibility switches could not be verified:', error)
    print('[settings] On Sequoia: System Settings > Accessibility > Display. '
          'Enable Reduce motion and Prefer non-blinking cursor there if needed. '
          'Allow Terminal under Privacy & Security > Accessibility, and Automation > System Events.')


def _run(*cmd):
    return run_command(cmd)


def _read(domain, key):
    # Same command-line preferences backend as the working v0.3.1.
    result = _run('defaults', 'read', domain, key)
    if result.returncode == 0:
        return result.stdout.strip()
    error = (result.stderr or result.stdout).strip()
    if 'does not exist' in error.lower() or 'not found' in error.lower():
        return None
    raise RuntimeError(f'{domain}:{key}: defaults read exit={result.returncode}, '
                       f'stdout={result.stdout.strip()!r}, stderr={result.stderr.strip()!r}')


def _typed_value(kind, value):
    if value is None:
        return None
    if kind == '-bool':
        normalized = str(value).strip().lower()
        if normalized in ('true', 'yes', '1'):
            return True
        if normalized in ('false', 'no', '0'):
            return False
        raise ValueError(f'Invalid saved Boolean: {value!r}')
    if kind == '-int':
        return int(value)
    if kind == '-float':
        return float(value)
    return str(value)


def _write_preference(domain, key, kind, value):
    """Typed writes with readback verification, including deletion.

    Legacy journals store bools as 0/1 strings. Never pass ambiguous old text to
    defaults -bool. A nonzero command status can still have persisted the value.
    """
    typed = _typed_value(kind, value)
    expected = None if typed is None else ('true' if typed else 'false') if kind == '-bool' else str(typed)
    errors = []
    for attempt in range(3):
        try:
            if expected is None:
                result = _run('defaults', 'delete', domain, key)
            else:
                result = _run('defaults', 'write', domain, key, kind, expected)
            actual = _read(domain, key)
            if _same(actual, expected, kind):
                return
            errors.append(f'exit={result.returncode}, stdout={result.stdout.strip()!r}, '
                          f'stderr={result.stderr.strip()!r}, expected={expected!r}, actual={actual!r}')
        except Exception as ex:
            errors.append(str(ex))
        if attempt < 2:
            time.sleep(.15)
    raise RuntimeError(f'{domain}:{key}: ' + '; '.join(errors))


@contextmanager
def _finish_recovery():
    # An initial Ctrl+C requests exit. Further signals must not interrupt the
    # saved-value restore halfway through. Child commands use their own session.
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
    try:
        with recovery_commands():
            yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _same(a, b, kind):
    if a is None or b is None:
        return a is b
    if kind == '-bool':
        def norm(v):
            return {'yes': True, 'true': True, '1': True,
                    'no': False, 'false': False, '0': False}.get(str(v).strip().lower(), v)
        return norm(a) == norm(b)
    return a == b


def _refresh_clock():
    # Recent macOS versions use ControlCenter; older ones use SystemUIServer.
    # A missing process is normal, so neither killall result is fatal.
    _run('killall', 'ControlCenter')
    _run('killall', 'SystemUIServer')


def _atomic_journal(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'version': 4, 'entries': rows}, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _restore_settings(journal=JOURNAL):
    """Recover preferences from a v0.2 or v0.3 journal; never clobber other edits.

    Only restore a key when its current value matches Mac84's temporary value.
    If an app/user changed the key meanwhile, keep their newer value and log it.
    """
    journal = Path(journal)
    if not journal.exists():
        print(f'[settings] No saved backup at {journal}; nothing to restore.')
        return
    saved = json.loads(journal.read_text(encoding='utf-8'))
    if isinstance(saved, list):
        # v0.2 original journal format; map its per-key target values.
        temp_by_key = {(d, k): value for d, k, _, value in PREFS}
        temp_by_key.update(LEGACY_TARGETS)
        temp_by_key.update({
            ('-g', 'NSTextInsertionPointBlinkPeriodOn'): '99999999',
            ('-g', 'NSTextInsertionPointBlinkPeriodOff'): '0',
        })
        entries = [dict(domain=d, key=k, kind=kind, original=old,
                        temporary=temp_by_key.get((d, k)), verified=True)
                   for d, k, kind, old in saved]
    else:
        entries = saved['entries']
    problems = []
    restored = 0
    for item in reversed(entries):
        if item.get('backend') == 'accessibility_ui':
            continue
        domain, key = item['domain'], item['key']
        kind, original = item['kind'], item['original']
        temporary = item.get('temporary')
        try:
            current = _read(domain, key)
        except Exception as ex:
            problems.append(f'{domain}:{key}: read failed: {ex}')
            continue
        # If a user changed it after Mac84 started, don't overwrite their choice.
        if temporary is not None and not _same(current, temporary, kind):
            if _same(current, original, kind):
                continue
            print(f'[settings] Skipped {key}: it changed outside Mac84 ({current!r}).')
            continue
        if _same(current, original, kind):
            continue
        try:
            _write_preference(domain, key, kind, original)
            restored += 1
        except Exception as ex:
            problems.append(str(ex))
    _refresh_clock()
    ui_entries = [item for item in entries if item.get('backend') == 'accessibility_ui']
    if ui_entries:
        try:
            with AccessibilityUI() as ui:
                for item in reversed(ui_entries):
                    try:
                        current = ui.read(item['key'])
                        if current == item['original']:
                            continue
                        owned = [item['temporary'], item.get('previous_temporary')]
                        if current not in owned:
                            print(f'[settings] Preserved external change: {item["key"]}')
                            continue
                        actual = ui.set(item['key'], item['original'], current)
                        if actual == 'conflict':
                            print(f'[settings] Preserved external change: {item["key"]}')
                        elif actual != item['original']:
                            raise RuntimeError('UI verification failed for ' + item['key'])
                        else:
                            restored += 1
                    except Exception as ex:
                        problems.append(str(ex))
        except Exception as ex:
            problems.append(str(ex))
    if problems:
        # Keep backup to retry; do not silently declare success.
        raise RuntimeError('Settings restore incomplete: ' + '; '.join(problems) +
                           f'. Backup remains at {journal}')
    journal.unlink()
    print(f'[settings] Recovery complete ({restored} preference changes restored).')
    if ui_entries:
        print('[settings] Accessibility switch restoration was checked through the UI.')


def restore_settings(journal=JOURNAL):
    with _finish_recovery():
        return _restore_settings(journal)


class MacSettings:
    def __init__(self, enabled=True, journal=JOURNAL, lock_path=None):
        self.enabled = enabled
        self.journal = Path(journal)
        self.lock_path = Path(lock_path) if lock_path else (LOCK if self.journal == JOURNAL else self.journal.with_suffix('.lock'))
        self.active = False
        self.lockfile = None

    def __enter__(self):
        if not self.enabled:
            return self
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lockfile = open(self.lock_path, 'a+')
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lockfile.close()
            self.lockfile = None
            raise RuntimeError('Another Mac84 settings session is still running.') from None
        try:
            # v0.2 stored its backup in a different source folder. Recover it
            # before capturing the new baseline or we'd preserve the modified
            # clock as if it were the user's original preference.
            for previous in dict.fromkeys((*LEGACY_JOURNALS, self.journal)):
                if previous.exists() and previous != self.journal.with_suffix('.lock'):
                    print(f'[settings] Recovering an interrupted session: {previous}')
                    try:
                        restore_settings(previous)
                    except Exception as ex:
                        print('[settings] Recovery warning:', ex)
                        print('[settings] Starting without preference changes. '
                              'You can retry --restore-settings later.')
                        self.enabled = False
                        fcntl.flock(self.lockfile, fcntl.LOCK_UN)
                        self.lockfile.close()
                        self.lockfile = None
                        return self
            # Commit and apply clock settings BEFORE any UI call.
            records = [dict(domain=d, key=k, kind=kind,
                            original=_read(d, k), temporary=value)
                       for d, k, kind, value in PREFS]
            _atomic_journal(self.journal, records)
            self.active = True
            for item in records:
                try:
                    _write_preference(item['domain'], item['key'], item['kind'], item['temporary'])
                    print(f'[settings] Set {item["key"]} to {item["temporary"]} (verified).', flush=True)
                except Exception as ex:
                    print('[settings] Preference warning:', ex, flush=True)
            _refresh_clock()
            check_cancelled()
            if _supports_ui():
                print('[settings] Applying Accessibility > Display switches; USB remains active.', flush=True)
                try:
                    with AccessibilityUI() as ui:
                        for label in LABELS:
                            check_cancelled()
                            try:
                                current = ui.read(label)
                                item = dict(backend='accessibility_ui', key=label,
                                            original=current, temporary='1')
                                records.append(item)
                                _atomic_journal(self.journal, records)
                                targets = ('0', '1') if current == '1' else ('1',)
                                for target in targets:
                                    item['previous_temporary'] = current
                                    item['temporary'] = target
                                    _atomic_journal(self.journal, records)
                                    actual = ui.set(label, target, current)
                                    if actual == 'conflict':
                                        print(f'[settings] Preserved external change: {label}', flush=True)
                                        records.remove(item)
                                        _atomic_journal(self.journal, records)
                                        break
                                    if actual != target:
                                        raise RuntimeError('Switch did not reach ' + target)
                                    current = actual
                                    item.pop('previous_temporary', None)
                                    _atomic_journal(self.journal, records)
                                    if target == '0':
                                        time.sleep(.2)
                                else:
                                    print(f'[settings] {label}: ON (UI transition verified).', flush=True)
                            except Exception as ex:
                                _ui_warning(ex)
                except Exception as ex:
                    _ui_warning(ex)
            else:
                print('[settings] Automatic accessibility switches target macOS Sequoia 15.', flush=True)
            return self
        except BaseException:
            if self.active:
                try:
                    restore_settings(self.journal)
                except Exception as ex:
                    print('[settings] Automatic cleanup warning:', ex)
            self.active = False
            if self.lockfile is not None:
                fcntl.flock(self.lockfile, fcntl.LOCK_UN)
                self.lockfile.close()
                self.lockfile = None
            raise

    def __exit__(self, typ, val, tb):
        try:
            if self.active:
                restore_settings(self.journal)
        finally:
            self.active = False
            if self.lockfile is not None:
                fcntl.flock(self.lockfile, fcntl.LOCK_UN)
                self.lockfile.close()
                self.lockfile = None
        return False


def restore_all_settings():
    """Manual recovery must not race a live bridge's settings session."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Stop the running Mac84 bridge before restoring settings.') from None
        for path in dict.fromkeys((*LEGACY_JOURNALS, JOURNAL)):
            if path.exists():
                restore_settings(path)
