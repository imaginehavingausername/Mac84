"""Regression coverage for the full startup path and a stalled settings process."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
import mac84_bridge as mb
import mac_settings as ms
import settings_commands as sc
import settings_worker as sw
import test_settings


class ConversationWire:
    def __init__(self):
        self.frames = self.acks = 0
        self.hello = False
        self.ready = False
        self.closed = False
        self.on_request = lambda: None
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def flush(self): pass
    def write(self, data):
        if data == b'@M84READY\n': self.ready = True
        if data.startswith(b'F|'):
            count = int(data.split(b'|')[1])
            self.acks = (count+3)//4
        if data == b'E\n': self.frames += 1
        return len(data)
    def readline(self):
        if self.acks:
            self.acks -= 1
            return b'A\n'
        if not self.hello:
            self.hello = True
            return b'@M84\n'
        self.on_request()
        if self.frames == 12: raise KeyboardInterrupt()
        return b'V\n'


class StartupTests(unittest.TestCase):
    def tearDown(self): sc.stop_requested.clear()

    def test_full_startup_keeps_sending_frames_while_settings_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            started, restored = Path(temp)/'started', Path(temp)/'restored'
            source_dir = str(Path(mb.__file__).parent)
            code = f'''
import sys
from pathlib import Path
sys.path.insert(0, {source_dir!r})
import settings_worker as sw
from settings_commands import stop_requested
class SlowSettings:
    def __enter__(self):
        Path({str(started)!r}).write_text('started')
        if not stop_requested.wait(10): raise RuntimeError('parent failed to close pipe')
        return self
    def __exit__(self, *args):
        Path({str(restored)!r}).write_text('restored')
sw.MacSettings = SlowSettings
raise SystemExit(sw.run_session())
'''
            wire = ConversationWire()
            children = []
            real_popen = subprocess.Popen
            def helper_popen(command, **kwargs):
                self.assertEqual(wire.frames,9) # all 300 initial tiles ACKed
                child = real_popen([sys.executable,'-u','-c',code], **kwargs)
                children.append(child)
                return child
            def check_live():
                if wire.frames == 9:
                    deadline = time.monotonic()+3
                    while not started.exists() and time.monotonic()<deadline:
                        time.sleep(.01)
                    self.assertTrue(started.exists())
                    self.assertIsNone(children[0].poll())
                if wire.frames in (10,11):
                    self.assertIsNone(children[0].poll())
                    self.assertFalse(restored.exists())
            wire.on_request = check_live
            class Screen:
                def __init__(self,*args): pass
                def capture(self):
                    assert wire.ready # no capture or GUI setup before port/READY
                    return Image.new('RGB',(320,240),'white')
                def close(self): pass
            with patch.dict(sys.modules, {'serial':SimpleNamespace(Serial=lambda *a,**k:wire,SerialException=OSError)}), \
                 patch.object(mb,'Screen',Screen), patch.object(mb,'select_port',return_value='test'), \
                 patch.object(mb.signal,'signal'), patch.object(sw.subprocess,'Popen',side_effect=helper_popen):
                with self.assertRaises(KeyboardInterrupt):
                    mb.main(['--pace','0'])
            self.assertEqual(wire.frames,12)
            self.assertEqual(len(children),1)
            self.assertTrue(wire.closed)
            self.assertTrue(restored.exists())
            self.assertEqual(children[0].returncode,0)

    def test_clock_is_applied_and_refreshed_before_ui_and_restored_after_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp)/'saved.json'
            state = {('com.apple.menuextra.clock','ShowSeconds'):'true'}
            calls = []
            base = test_settings.SettingsTests().fake_defaults(state)
            def command(*args):
                calls.append(args)
                return base(*args)
            class BlockedUI:
                def __enter__(inner):
                    self.assertEqual(state[('com.apple.menuextra.clock','ShowSeconds')],'false')
                    self.assertIn(('killall','ControlCenter'),calls)
                    self.assertTrue(journal.exists())
                    sc.stop_requested.set()
                    sc.check_cancelled()
                def __exit__(inner,*args): pass
            with patch.object(ms,'LEGACY_JOURNALS',()), patch.object(ms,'_supports_ui',return_value=True), \
                 patch.object(ms,'_run',side_effect=command), patch.object(ms,'AccessibilityUI',BlockedUI):
                with self.assertRaises(sc.SettingsCancelled):
                    with ms.MacSettings(journal=journal): pass
            self.assertEqual(state,{('com.apple.menuextra.clock','ShowSeconds'):'true'})
            self.assertFalse(journal.exists())

    def test_settings_command_is_cancellable_but_restore_commands_are_not(self):
        timer = threading.Timer(.2,sc.stop_requested.set)
        timer.start()
        try:
            start = time.monotonic()
            with self.assertRaises(sc.SettingsCancelled):
                sc.run_command([sys.executable,'-c','import time; time.sleep(30)'])
            self.assertLess(time.monotonic()-start,3)
            with sc.recovery_commands():
                result = sc.run_command([sys.executable,'-c','print("recovered")'])
            self.assertEqual(result.stdout.strip(),'recovered')
        finally:
            timer.cancel()
            timer.join()

    def test_settings_timeout_is_bounded(self):
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            sc.run_command([sys.executable,'-c','import time; time.sleep(30)'],timeout=.15)
        self.assertLess(time.monotonic()-start,3)

    def test_disabled_settings_never_spawn(self):
        with patch.object(sw.subprocess,'Popen') as spawn:
            with sw.SettingsProcess(False) as worker: worker.start()
        spawn.assert_not_called()
