"""v0.3.7: WINDOW via LaunchServices, plug-in auto-start, reliable switches."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from test_bridge import FakeSerial, DummyScreen
import test_settings
import mac84_bridge as mb
import mac_settings as ms
import mac_accessibility as ma

MAC = Path(mb.__file__).resolve().parent


class FakeQuartz:
    kCGEventFlagMaskControl, kCGEventFlagMaskCommand, kCGEventFlagMaskShift = 1, 2, 4
    kCGEventFlagMaskSecondaryFn, kCGEventFlagMaskNumericPad = 8, 16
    kCGHIDEventTap = 0

    def __init__(self):
        self.posted = []

    def CGEventCreateKeyboardEvent(self, _, key, down): return {'key': key, 'down': down}
    def CGEventSetFlags(self, ev, flags): ev['flags'] = flags
    def CGEventPost(self, tap, ev): self.posted.append(ev)


def live_input():
    dev = mb.MacInput(True, (3024, 1964))
    dev.dry_run, dev.quartz = False, FakeQuartz()
    return dev


class WindowKeyTests(unittest.TestCase):
    """v0.3.6 executed Mission Control's binary directly; Sequoia's launch
    constraints SIGKILL that (AMFI log on this Mac), so WINDOW did nothing."""

    def ok(self):
        return patch.object(mb.subprocess, 'run', return_value=SimpleNamespace(
            returncode=0, stdout='', stderr=''))

    def test_window_asks_the_dock_like_f3_without_launching_anything(self):
        dev = live_input()
        with patch.object(mb.MacInput, '_dock_notify', return_value=None) as dock, \
             self.ok() as run, patch.object(mb.subprocess, 'Popen') as popen:
            self.assertEqual(dev.show_windows(), 'Mission Control')
        dock.assert_called_once_with('com.apple.expose.awake')
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(dev.quartz.posted, [])  # no stray keystrokes

    def test_app_expose_uses_launchservices_with_argument_2(self):
        dev = live_input()
        with patch.object(mb.MacInput, '_dock_notify', return_value=None) as dock, \
             self.ok() as run, patch.object(mb.subprocess, 'Popen') as popen:
            self.assertEqual(dev.show_windows(True), 'App Expose')
        self.assertEqual(run.call_args.args[0],
                         ['/usr/bin/open', '-b', 'com.apple.exposelauncher', '--args', '2'])
        dock.assert_not_called()
        popen.assert_not_called()  # never executes the app binary itself

    def test_dock_unavailable_falls_back_to_the_app(self):
        dev = live_input()
        with patch.object(mb.MacInput, '_dock_notify', return_value='unavailable'), \
             self.ok() as run:
            self.assertEqual(dev.show_windows(), 'Mission Control (via app)')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-b', 'com.apple.exposelauncher'])

    def test_real_dock_call_refuses_under_unittest(self):
        self.assertIn('unit tests', live_input()._dock_notify('com.apple.expose.awake'))

    def test_everything_failing_ends_with_control_arrow_and_fn_flags(self):
        dev = live_input()
        with patch.object(mb.MacInput, '_dock_notify', return_value='no dock'), \
             patch.object(mb.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=1, stdout='', stderr='LSOpen failed')):
            what = dev.show_windows()
        self.assertIn('Control+Up', what)
        self.assertIn('no dock', what)
        self.assertIn('LSOpen failed', what)
        up = [e for e in dev.quartz.posted if e['key'] == 126]
        self.assertTrue(up and all(e['flags'] == 1 | 8 | 16 for e in up))
        self.assertEqual(dev.quartz.posted[-1], {'key': 59, 'down': False, 'flags': 0})

    def test_app_expose_timeout_tries_dock_then_keys(self):
        dev = live_input()
        calls = []
        def dock(self_, name):
            calls.append(name); return 'no dock'
        with patch.object(mb.MacInput, '_dock_notify', dock), \
             patch.object(mb.subprocess, 'run', side_effect=subprocess.TimeoutExpired('open', 5)):
            self.assertIn('Control+Down', dev.show_windows(True))
        self.assertEqual(calls, ['com.apple.expose.front.awake'])

    def test_window_is_always_logged(self):
        s = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240))))
        class Dev:
            def show_windows(self, app_only): return 'App Expose' if app_only else 'Mission Control'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mb.parse_request(FakeSerial(), s, Dev(), 'X|M', 0, log=False)
            mb.parse_request(FakeSerial(), s, Dev(), 'X|A', 0, log=False)
        self.assertIn('[input] WINDOW: Mission Control', out.getvalue())
        self.assertIn('[input] WINDOW: App Expose', out.getvalue())


class CalculatorVersionTests(unittest.TestCase):
    def greet(self, message):
        s = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240))))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mb.parse_request(FakeSerial(), s, None, message, 0)
        return out.getvalue()

    def test_v036_calculator_needs_no_resend(self):
        self.assertEqual(mb.BRIDGE_VERSION, '0.3.7')
        out = self.greet('@M84|5|0.3.6')
        self.assertIn('MAC84 v0.3.6', out)
        self.assertNotIn('WARNING', out)

    def test_older_calculators_still_warn(self):
        for greeting in ('@M84|5|0.3.5', '@M84|5', '@M84'):
            self.assertIn('WARNING', self.greet(greeting), greeting)


IOREG_TI_OS = '''+-o Root  <class IORegistryEntry>
  +-o TI-84 Plus CE@02100000  <class IOUSBHostDevice>
      |   "idProduct" = 57352
      |   "idVendor" = 1105
'''
IOREG_OTHER = '''+-o Root
  +-o Some Hub@01000000  <class IOUSBHostDevice>
      |   "idVendor" = 11050
      |   "USB Product Name" = "Hub"
'''


class PlugInDetectionTests(unittest.TestCase):
    def ioreg(self, text=None, error=None):
        if error:
            return patch.object(mb.subprocess, 'run', side_effect=error)
        return patch.object(mb.subprocess, 'run', return_value=SimpleNamespace(
            returncode=0, stdout=text, stderr=''))

    def test_ti_vendor_detected_and_lookalike_vendor_ignored(self):
        with self.ioreg(IOREG_TI_OS):
            self.assertTrue(mb.calculator_plugged_in())
        with self.ioreg(IOREG_OTHER):
            self.assertFalse(mb.calculator_plugged_in())
        with self.ioreg('  "idVendor" = 1105\n'):  # MAC84's own serial device
            self.assertTrue(mb.calculator_plugged_in())
        with self.ioreg(error=OSError('no ioreg')):
            self.assertIsNone(mb.calculator_plugged_in())


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WaitForMac84Tests(unittest.TestCase):
    def run_wait(self, port_at=None, present=lambda t: True, gone_after=8.0):
        t = FakeTime()
        ports = lambda: ['/dev/cu.usbmodemTI'] if port_at is not None and t.now >= port_at else []
        with patch.object(mb, 'matching_ports', side_effect=ports), \
             patch.object(mb, 'calculator_plugged_in', side_effect=lambda: present(t.now)):
            return mb.wait_for_mac84_port(gone_after=gone_after, sleep=t.sleep, clock=t.clock), t.now

    def test_returns_port_when_mac84_starts(self):
        port, when = self.run_wait(port_at=30)
        self.assertEqual(port, '/dev/cu.usbmodemTI')
        self.assertGreaterEqual(when, 30)

    def test_unplugged_calculator_ends_wait(self):
        port, when = self.run_wait(present=lambda t: t < 5)
        self.assertIsNone(port)
        self.assertGreaterEqual(when, 5 + 8 - 2)

    def test_short_reenumeration_gap_is_not_unplugging(self):
        # TI-OS link disappears ~4 s before MAC84's port shows up.
        port, _ = self.run_wait(port_at=14, present=lambda t: not (10 <= t < 14))
        self.assertEqual(port, '/dev/cu.usbmodemTI')

    def test_ioreg_failure_counts_as_still_plugged_in(self):
        port, _ = self.run_wait(port_at=40, present=lambda t: None)
        self.assertEqual(port, '/dev/cu.usbmodemTI')


class AutoLoopTests(unittest.TestCase):
    def test_sessions_repeat_until_unplugged(self):
        sessions, closes = [], []
        ports = iter(['/dev/cu.A', '/dev/cu.B', None])
        def fake_main(argv):
            sessions.append(argv)
            if len(sessions) == 1:
                raise RuntimeError('USB serial port disappeared')  # cable pulled mid-session
            return 0
        out, err = io.StringIO(), io.StringIO()
        with patch.object(mb, 'wait_for_mac84_port', side_effect=lambda: next(ports)), \
             patch.object(mb, 'wait_for_port_to_close', side_effect=closes.append), \
             patch.object(mb, 'main', side_effect=fake_main), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(mb.run_auto(['--auto', '--pace', '0.003']), 0)
        self.assertEqual(sessions, [['--pace', '0.003', '--port', '/dev/cu.A'],
                                    ['--pace', '0.003', '--port', '/dev/cu.B']])
        self.assertEqual(closes, ['/dev/cu.A', '/dev/cu.B'])
        self.assertIn('USB serial port disappeared', err.getvalue())
        self.assertIn('Calculator unplugged', out.getvalue())

    def test_ctrl_c_stops_the_loop(self):
        with patch.object(mb, 'wait_for_mac84_port', side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                mb.run_auto(['--auto'])

    def test_main_dispatches_auto(self):
        with patch.object(mb, 'run_auto', return_value=0) as auto:
            self.assertEqual(mb.main(['--auto', '--pace', '0.003']), 0)
        auto.assert_called_once_with(['--auto', '--pace', '0.003'])

    def test_port_close_wait_returns_once_released(self):
        seen = iter([['/dev/cu.A'], ['/dev/cu.A'], []])
        with patch.object(mb, 'matching_ports', side_effect=lambda: next(seen)):
            mb.wait_for_port_to_close('/dev/cu.A', sleep=lambda s: None)

    def test_serial_port_is_opened_exclusively(self):
        source = Path(mb.__file__).read_text()
        self.assertIn('exclusive=True) as wire', source)


class DirectSwitchSafetyTests(unittest.TestCase):
    def test_real_switches_refuse_to_load_under_unittest(self):
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            ma.DirectSwitches()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(ms._direct_switches())
        self.assertIn('using System Settings', out.getvalue())


class FakeDirect:
    """Stands in for libAccessibility: supports only the cursor switch."""
    def __init__(self, values, journal, fail=False):
        self.values, self.journal, self.fail = values, journal, fail
        self.calls = []

    def supports(self, label): return label == 'Prefer non-blinking cursor'
    def read(self, label): return self.values[label]

    def set(self, label, value, expected):
        if self.journal.exists():  # journal is written before every change
            saved = json.loads(self.journal.read_text())
            assert any(x.get('key') == label for x in saved['entries'])
        if self.fail:
            raise RuntimeError('direct setting did not take effect')
        if self.values[label] != expected:
            return 'conflict'
        self.calls.append((label, value))
        self.values[label] = value
        return value


class FakeUI:
    instances = 0

    def __init__(self, values, calls):
        self.values, self.calls = values, calls

    def __enter__(self):
        FakeUI.instances += 1
        return self

    def __exit__(self, *a): return False
    def read(self, label): return self.values[label]

    def set(self, label, value, expected):
        if self.values[label] != expected:
            return 'conflict'
        self.calls.append((label, value))
        self.values[label] = value
        return value


class SwitchBackendTests(unittest.TestCase):
    def session(self, values, direct_factory, check):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'saved.json'
            state, ui_calls = {}, []
            direct = direct_factory(journal)
            FakeUI.instances = 0
            out = io.StringIO()
            with patch.object(ms, '_supports_ui', return_value=True), \
                 patch.object(ms, 'LEGACY_JOURNALS', ()), \
                 patch.object(ms, '_run', side_effect=test_settings.SettingsTests().fake_defaults(state)), \
                 patch.object(ms, '_direct_switches', return_value=direct), \
                 patch.object(ms, 'AccessibilityUI', lambda: FakeUI(values, ui_calls)), \
                 patch.object(ms.time, 'sleep'), contextlib.redirect_stdout(out):
                with ms.MacSettings(journal=journal):
                    check(journal, direct, ui_calls, out)
            return journal.exists(), direct, ui_calls, out.getvalue()

    def test_cursor_set_directly_and_only_reduce_motion_uses_system_settings(self):
        values = {'Reduce motion': '0', 'Prefer non-blinking cursor': '0'}
        def during(journal, direct, ui_calls, out):
            self.assertEqual(values, {'Reduce motion': '1', 'Prefer non-blinking cursor': '1'})
            self.assertEqual(direct.calls, [('Prefer non-blinking cursor', '1')])
            self.assertEqual(ui_calls, [('Reduce motion', '1')])
            backends = {e['key']: e['backend'] for e in json.loads(journal.read_text())['entries']
                        if 'backend' in e}
            self.assertEqual(backends, {'Prefer non-blinking cursor': 'direct',
                                        'Reduce motion': 'accessibility_ui'})
        exists, direct, ui_calls, out = self.session(
            values, lambda j: FakeDirect(values, j), during)
        self.assertFalse(exists)
        self.assertEqual(values, {'Reduce motion': '0', 'Prefer non-blinking cursor': '0'})
        self.assertEqual(direct.calls[-1], ('Prefer non-blinking cursor', '0'))
        self.assertEqual(ui_calls[-1], ('Reduce motion', '0'))
        self.assertIn('Prefer non-blinking cursor: ON (set directly, verified)', out)
        self.assertIn('Reduce motion: ON (UI transition verified)', out)

    def test_direct_failure_falls_back_to_system_settings(self):
        values = {'Reduce motion': '0', 'Prefer non-blinking cursor': '0'}
        def during(journal, direct, ui_calls, out):
            self.assertEqual(sorted(ui_calls), [('Prefer non-blinking cursor', '1'),
                                                ('Reduce motion', '1')])
        exists, _, _, out = self.session(values, lambda j: FakeDirect(values, j, fail=True), during)
        self.assertFalse(exists)
        self.assertEqual(values, {'Reduce motion': '0', 'Prefer non-blinking cursor': '0'})
        self.assertIn('direct setting failed', out)

    def test_no_direct_backend_uses_ui_for_both(self):
        values = {'Reduce motion': '0', 'Prefer non-blinking cursor': '0'}
        def during(journal, direct, ui_calls, out):
            self.assertEqual(ui_calls, [('Reduce motion', '1'), ('Prefer non-blinking cursor', '1')])
        exists, _, _, _ = self.session(values, lambda j: None, during)
        self.assertFalse(exists)
        self.assertEqual(values, {'Reduce motion': '0', 'Prefer non-blinking cursor': '0'})

    def test_direct_journal_restores_through_ui_when_library_missing(self):
        values = {'Prefer non-blinking cursor': '1'}
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'saved.json'
            journal.write_text(json.dumps({'version': 4, 'entries': [
                {'backend': 'direct', 'key': 'Prefer non-blinking cursor',
                 'original': '0', 'temporary': '1'}]}))
            calls = []
            with patch.object(ms, '_direct_switches', return_value=None), \
                 patch.object(ms, 'AccessibilityUI', lambda: FakeUI(values, calls)), \
                 patch.object(ms, '_refresh_clock'), contextlib.redirect_stdout(io.StringIO()):
                ms.restore_settings(journal)
            self.assertEqual(values, {'Prefer non-blinking cursor': '0'})
            self.assertFalse(journal.exists())

    def test_direct_restore_needs_no_system_settings_window(self):
        values = {'Prefer non-blinking cursor': '1'}
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'saved.json'
            journal.write_text(json.dumps({'version': 4, 'entries': [
                {'backend': 'direct', 'key': 'Prefer non-blinking cursor',
                 'original': '0', 'temporary': '1'}]}))
            out = io.StringIO()
            with patch.object(ms, '_direct_switches', return_value=FakeDirect(values, journal)), \
                 patch.object(ms, 'AccessibilityUI', side_effect=AssertionError('UI opened')), \
                 patch.object(ms, '_refresh_clock'), contextlib.redirect_stdout(out):
                ms.restore_settings(journal)
            self.assertEqual(values, {'Prefer non-blinking cursor': '0'})
            self.assertIn('verified directly', out.getvalue())


class Result:
    def __init__(self, rc=0, out='', err=''):
        self.returncode, self.stdout, self.stderr = rc, out, err


class SystemSettingsNavigationTests(unittest.TestCase):
    def test_cold_launch_sends_the_display_link_twice(self):
        running = iter([1, 0, 0])  # not running, then running
        calls = []
        def run(cmd, timeout=8):
            calls.append(cmd[0])
            return Result(next(running) if cmd[0] == 'pgrep' else 0)
        with patch.object(ma, 'run_command', side_effect=run), patch.object(ma.time, 'sleep'):
            with ma.AccessibilityUI(): pass
        self.assertEqual(calls.count('open'), 2)
        self.assertNotIn('osascript', calls)

    def test_already_open_sends_it_once(self):
        calls = []
        def run(cmd, timeout=8):
            calls.append(cmd[0]); return Result(0)
        with patch.object(ma, 'run_command', side_effect=run):
            with ma.AccessibilityUI(): pass
        self.assertEqual(calls, ['pgrep', 'open'])

    def test_not_found_renavigates_and_retries_once(self):
        replies = iter([Result(1, err="Could not find Reduce motion (System Settings showed 'General' with 3 switches)"),
                        Result(0, out='0\n')])
        calls = []
        def run(cmd, timeout=8):
            calls.append(cmd[0])
            return next(replies) if cmd[0] == 'osascript' else Result(0)
        ui = ma.AccessibilityUI()
        with patch.object(ma, 'run_command', side_effect=run), patch.object(ma.time, 'sleep'):
            self.assertEqual(ui.read('Reduce motion'), '0')
        self.assertEqual(calls, ['osascript', 'open', 'osascript'])

    def test_second_miss_reports_what_was_on_screen(self):
        message = "Could not find Reduce motion (System Settings showed 'General' with 3 switches)"
        def run(cmd, timeout=8):
            return Result(1, err=message) if cmd[0] == 'osascript' else Result(0)
        with patch.object(ma, 'run_command', side_effect=run), patch.object(ma.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, "showed 'General'"):
                ma.AccessibilityUI().read('Reduce motion')

    def test_script_searches_every_window_with_a_time_bound(self):
        script = ma.SCRIPT.read_text()
        self.assertIn('repeat with winItem in windowList', script)
        self.assertNotIn('entire contents of window 1', script)
        self.assertIn('((current date) - startedAt) > 6', script)
        if shutil.which('osacompile'):
            with tempfile.TemporaryDirectory() as temp:
                subprocess.run(['osacompile', '-o', str(Path(temp) / 'a.scpt'), str(ma.SCRIPT)],
                               check=True, capture_output=True)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS launchd tools')
class AutoStartInstallTests(unittest.TestCase):
    """Runs the real installer against a temporary HOME with a fake launchctl."""

    def test_installer_writes_agent_leaves_other_agents_and_never_touches_real_launchd(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            home, folder, fakebin = temp / 'home', temp / 'Mac84_v0_3_7', temp / 'bin'
            shutil.copytree(MAC, folder / 'mac', ignore=shutil.ignore_patterns('__pycache__'))
            (folder / '.venv' / 'bin').mkdir(parents=True)
            (folder / '.venv' / 'bin' / 'python').write_text('#!/bin/sh\n')
            (folder / '.venv' / 'bin' / 'python').chmod(0o755)
            agents = home / 'Library' / 'LaunchAgents'
            agents.mkdir(parents=True)
            other = agents / 'com.example.other.plist'
            other.write_text('<plist/>')
            fakebin.mkdir()
            calls = temp / 'launchctl.log'
            (fakebin / 'launchctl').write_text(
                f'#!/bin/sh\necho "$@" >> "{calls}"\n'
                '[ "$1" = print ] && echo "state = running"\nexit 0\n')
            (fakebin / 'launchctl').chmod(0o755)
            env = dict(os.environ, HOME=str(home), PATH=f'{fakebin}:/usr/bin:/bin:/usr/sbin')
            result = subprocess.run(['/bin/zsh', str(folder / 'mac' / 'install_autostart.command')],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Auto-start is ON', result.stdout)
            state = home / 'Library' / 'Application Support' / 'Mac84'
            self.assertEqual((state / 'autostart_folder').read_text().strip(), str(folder.resolve()))
            watcher = state / 'mac84-watcher.sh'
            self.assertTrue(watcher.stat().st_mode & stat.S_IXUSR)
            plist = plistlib.loads((agents / 'com.local.mac84watcher.plist').read_bytes())
            self.assertEqual(plist['ProgramArguments'], ['/bin/zsh', str(watcher)])
            self.assertTrue(plist['RunAtLoad'] and plist['KeepAlive'])
            log = calls.read_text()
            uid = os.getuid()
            self.assertNotIn('com.example.other', log)  # other agents are left alone
            self.assertIn(f'bootstrap gui/{uid} {agents}/com.local.mac84watcher.plist', log)
            self.assertTrue(other.exists())

    def test_watcher_reads_only_its_own_state_folder(self):
        source = (MAC / 'mac84_watcher.sh').read_text()
        subprocess.run(['/bin/zsh', '-n', str(MAC / 'mac84_watcher.sh')], check=True)
        subprocess.run(['/bin/zsh', '-n', str(MAC / 'mac84_session.command')], check=True)
        for protected in ('Downloads', 'Desktop', 'Documents'):
            code = [l for l in source.splitlines() if not l.lstrip().startswith('#')]
            self.assertFalse(any(protected in l for l in code), protected)
        self.assertIn('busy of selected tab', source)  # never force-closes a running bridge
        self.assertIn('--auto', (MAC / 'mac84_session.command').read_text())


if __name__ == '__main__':
    unittest.main()
