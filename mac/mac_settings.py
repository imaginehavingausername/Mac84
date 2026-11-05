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
from mac_accessibility import AccessibilityUI, DirectSwitches, LABELS
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


# Read-only cross-check for restore: the preference behind each switch. It is
# never WRITTEN (writing it does not apply the change on Sequoia).
UI_PREFERENCE = {
    'Reduce motion': ('com.apple.universalaccess', 'reduceMotion'),
    'Prefer non-blinking cursor': ('com.apple.Accessibility', 'PrefersNonBlinkingCursorIndicator'),
}


def _stored_switch_value(label):
    """'0'/'1' if the preference exists and is a clear Boolean, else None."""
    domain, key = UI_PREFERENCE[label]
    value = _read(domain, key)
    if value is None:
        return None  # missing proves nothing
    value = value.strip().lower()
    return '1' if value in ('1', 'true', 'yes') else '0' if value in ('0', 'false', 'no') else None


# Journal backends for the two Accessibility switches. 'direct' (v0.3.7) is
# libAccessibility's setter; 'accessibility_ui' clicks System Settings.
SWITCH_BACKENDS = ('accessibility_ui', 'direct')


def _direct_switches():
    """DirectSwitches, or None (missing library/symbols, or under unit tests)."""
    try:
        return DirectSwitches()
    except (OSError, AttributeError, RuntimeError) as ex:
        print(f'[settings] Direct switch control unavailable ({ex}); using System Settings.',
              flush=True)
        return None


def _restore_switch_entries(backend, entries):
    """Put switches back with one backend. Returns (restored, [(item, error)])."""
    restored, failures = 0, []
    for item in reversed(entries):
        try:
            current = backend.read(item['key'])
            if current == item['original']:
                continue
            owned = [item['temporary'], item.get('previous_temporary')]
            if current not in owned:
                print(f'[settings] Preserved external change: {item["key"]}')
                continue
            actual = backend.set(item['key'], item['original'], current)
            if actual == 'conflict':
                print(f'[settings] Preserved external change: {item["key"]}')
            elif actual != item['original']:
                raise RuntimeError('UI verification failed for ' + item['key'])
            else:
                restored += 1
        except Exception as ex:
            failures.append((item, ex))
    return restored, failures


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
        if item.get('backend') in SWITCH_BACKENDS:
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
    switch_entries = [item for item in entries if item.get('backend') in SWITCH_BACKENDS]
    ui_entries = switch_entries
    ui_failures = []  # (item, error) for switches the UI could not restore
    if any(item['backend'] == 'direct' for item in switch_entries):
        direct = _direct_switches()
        if direct is not None:
            mine = [item for item in switch_entries
                    if item['backend'] == 'direct' and direct.supports(item['key'])]
            count, failed = _restore_switch_entries(direct, mine)
            restored += count
            for item, ex in failed:
                print(f'[settings] {item["key"]}: direct restore failed ({ex}); trying System Settings.')
            retry = {id(item) for item, _ in failed}
            ui_entries = [item for item in switch_entries
                          if not any(item is m for m in mine) or id(item) in retry]
    if ui_entries:
        try:
            with AccessibilityUI() as ui:
                count, ui_failures = _restore_switch_entries(ui, ui_entries)
                restored += count
        except Exception as ex:
            done = {id(item) for item, _ in ui_failures}
            ui_failures += [(item, ex) for item in ui_entries if id(item) not in done]
    for item, ex in ui_failures:
        # v0.3.6: when switch automation fails (e.g. System Settings closed or
        # still loading), the switch may already be back. Accept that only if
        # its stored preference EXISTS and reads the original value.
        try:
            stored = _stored_switch_value(item['key'])
        except Exception:
            stored = None
        if stored is not None and stored == item['original']:
            print(f'[settings] {item["key"]}: switch automation failed ({ex}); its stored '
                  f'preference already shows the original value, so it is restored.')
            continue
        problems.append(str(ex))
    if problems:
        # Keep backup to retry; do not silently declare success.
        raise RuntimeError('Settings restore incomplete: ' + '; '.join(problems) +
                           f'. Backup remains at {journal}')
    journal.unlink()
    print(f'[settings] Recovery complete ({restored} preference changes restored).')
    if ui_entries:
        print('[settings] Accessibility switch restoration was checked through the UI.')
    elif switch_entries:
        print('[settings] Accessibility switch restoration was verified directly.')


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
                direct = _direct_switches()
                ui_labels = []
                for label in LABELS:
                    if direct is None or not direct.supports(label):
                        ui_labels.append(label)
                        continue
                    check_cancelled()
                    try:
                        self._apply_switch(direct, 'direct', label, records)
                    except Exception as ex:
                        print(f'[settings] {label}: direct setting failed ({ex}); '
                              'using System Settings.', flush=True)
                        ui_labels.append(label)
                if ui_labels:
                    try:
                        with AccessibilityUI() as ui:
                            for label in ui_labels:
                                check_cancelled()
                                try:
                                    self._apply_switch(ui, 'accessibility_ui', label, records)
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

    def _apply_switch(self, backend, backend_name, label, records):
        """Turn one switch on, journaling before every change. A switch that
        is already on is re-applied off then on, verifying each transition."""
        current = backend.read(label)
        item = dict(backend=backend_name, key=label, original=current, temporary='1')
        records.append(item)
        _atomic_journal(self.journal, records)
        targets = ('0', '1') if current == '1' else ('1',)
        for target in targets:
            item['previous_temporary'] = current
            item['temporary'] = target
            _atomic_journal(self.journal, records)
            actual = backend.set(label, target, current)
            if actual == 'conflict':
                print(f'[settings] Preserved external change: {label}', flush=True)
                records.remove(item)
                _atomic_journal(self.journal, records)
                return
            if actual != target:
                raise RuntimeError('Switch did not reach ' + target)
            current = actual
            item.pop('previous_temporary', None)
            _atomic_journal(self.journal, records)
            if target == '0':
                time.sleep(.2)
        how = 'set directly, verified' if backend_name == 'direct' else 'UI transition verified'
        print(f'[settings] {label}: ON ({how}).', flush=True)

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
