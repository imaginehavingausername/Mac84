import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac'))
import mac84_bridge as mb
import mac_settings as ms
import mac_accessibility as ma
from test_bridge import DummyScreen, FakeSerial
import test_settings


class FreshnessTests(unittest.TestCase):
    def test_newer_screenshot_replaces_unsent_old_tiles(self):
        screen = DummyScreen(Image.new('RGB', (320,240), 'red'))
        session, wire = mb.ViewSession(screen), FakeSerial()
        mb.send_frame(wire, session, 0)
        self.assertEqual(len(session.old_tiles), 36)
        screen.image.paste('blue', (0,0,320,240))
        batch, _, _ = session.render()
        self.assertTrue(batch)
        for _, _, fmt, packet, block in batch + session.pending:
            self.assertEqual(block, bytes([3]) * 256)
            self.assertEqual(mb.rle_decode(packet) if fmt == 'R' else packet, block)
        # Replacing pending data must not pretend it was received.
        self.assertTrue(all(block == bytes([224])*256 for block in session.old_tiles.values()))

    def test_reverted_pixels_are_not_sent_as_stale_pending_changes(self):
        screen = DummyScreen(Image.new('RGB',(320,240),'white'))
        session = mb.ViewSession(screen)
        session.old_tiles = {(x,y): bytes([255])*256 for y in range(15) for x in range(20)}
        screen.image.paste('black', (0,0,320,240))
        session.render()  # Deliberately not ACKed.
        screen.image.paste('white', (0,0,320,240))
        batch, _, more = session.render()
        self.assertEqual(batch, [])
        self.assertFalse(more)

    def test_animated_top_rows_do_not_starve_bottom_rows(self):
        screen = DummyScreen(Image.new('RGB',(320,240),'white'))
        session, wire = mb.ViewSession(screen), FakeSerial()
        for i in range(25):
            screen.image.paste('red' if i%2 else 'blue', (0,0,320,16))
            mb.send_frame(wire, session, 0)
        self.assertEqual(len(session.old_tiles), 300)
        self.assertEqual(session.old_tiles[(19,14)], bytes([255])*256)

    def test_vectorized_rgb332_matches_reference_for_all_channel_pairs(self):
        data = bytes(v for r in range(256) for g in range(256) for v in (r,g,(r+g)%256))
        im = Image.frombytes('RGB',(256,256),data)
        expected = bytes((data[i]&224)|((data[i+1]&224)>>3)|(data[i+2]>>6)
                         for i in range(0,len(data),3))
        self.assertEqual(mb.rgb332_image(im),expected)

    def test_both_previous_calculators_get_their_own_ready(self):
        for hello, ready in [('@M84', b'@M84READY\n'), ('@M84|4', b'@M84READY|4\n')]:
            wire = FakeSerial()
            mb.parse_request(wire, mb.ViewSession(DummyScreen(Image.new('RGB',(320,240)))), Mock(), hello, 0)
            self.assertEqual(bytes(wire.sent), ready)


class SpaceTests(unittest.TestCase):
    def device(self):
        dev = mb.MacInput(True,(320,240)); dev.dry_run = False
        # Real Quartz values: Control, Command, Shift, fn (SecondaryFn), keypad.
        q = Mock(kCGEventFlagMaskControl=1<<18, kCGEventFlagMaskCommand=1<<20,
                 kCGEventFlagMaskShift=1<<17, kCGEventFlagMaskSecondaryFn=1<<23,
                 kCGEventFlagMaskNumericPad=1<<21, kCGHIDEventTap=0)
        q.CGEventCreateKeyboardEvent.side_effect = lambda source,key,down: (key,down)
        dev.quartz = q
        return dev,q

    def test_space_directions_send_control_arrows_and_release(self):
        for direction, code in [('L',123),('R',124)]:
            dev,q = self.device()
            with patch.object(mb.time,'sleep'):
                dev.switch_space(direction)
            self.assertEqual([c.args[1] for c in q.CGEventPost.call_args_list],
                             [(59,True),(code,True),(code,False),(59,False)])
            self.assertEqual(q.CGEventSetFlags.call_args_list[-1].args, ((59,False),0))
            # macOS's Control+arrow shortcuts are defined WITH fn (0x840000);
            # real arrow keys also carry the keypad flag.
            arrow_flags = {c.args[1] for c in q.CGEventSetFlags.call_args_list
                           if c.args[0][0] == code}
            self.assertEqual(arrow_flags, {(1<<18) | (1<<23) | (1<<21)})

    def test_control_is_released_if_arrow_posting_fails(self):
        dev,q = self.device()
        def post(tap,event):
            if event == (123,True): raise RuntimeError('posting failed')
        q.CGEventPost.side_effect = post
        with self.assertRaisesRegex(RuntimeError,'posting failed'):
            dev.switch_space('L')
        self.assertEqual(q.CGEventPost.call_args_list[-1].args[1],(59,False))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        for name, value in [('_supports_ui',True)]:
            p=patch.object(ms,name,return_value=value); p.start(); self.addCleanup(p.stop)
        p=patch.object(ms,'LEGACY_JOURNALS',());p.start();self.addCleanup(p.stop)

    def test_clock_legacy_boolean_is_canonicalized_and_nonzero_success_verifies(self):
        state = {('com.apple.menuextra.clock','ShowSeconds'):'false'}
        calls=[]
        base=test_settings.SettingsTests().fake_defaults(state)
        def run(*args):
            calls.append(args)
            result=base(*args)
            if args[:2] == ('defaults','write'):
                result.returncode=1  # Value did persist despite command status.
            return result
        with patch.object(ms,'_run',side_effect=run):
            ms._write_preference('com.apple.menuextra.clock','ShowSeconds','-bool','1')
        self.assertIn(('defaults','write','com.apple.menuextra.clock','ShowSeconds','-bool','true'),calls)
        self.assertEqual(state[('com.apple.menuextra.clock','ShowSeconds')],'true')

    def test_blank_error_includes_exit_expected_and_actual(self):
        state={('com.apple.menuextra.clock','ShowSeconds'):'false'}
        base=test_settings.SettingsTests().fake_defaults(state)
        def run(*args):
            if args[:2] == ('defaults','write'): return test_settings.DummyResult(rc=1)
            return base(*args)
        with patch.object(ms,'_run',side_effect=run), patch.object(ms.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError, "exit=1.*expected='true'.*actual='false'"):
                ms._write_preference('com.apple.menuextra.clock','ShowSeconds','-bool','1')

    def test_enabled_switches_are_reapplied_off_then_on_and_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=Path(temp)/'saved.json'; state={}; values=dict.fromkeys(ms.LABELS,'1'); calls=[]
            class UI:
                def __enter__(self): return self
                def __exit__(self,*args): return False
                def read(self,label): return values[label]
                def set(self,label,value,expected):
                    entry=next(x for x in json.loads(journal.read_text())['entries'] if x.get('key')==label)
                    assert entry['temporary']==value
                    assert entry['previous_temporary']==expected
                    assert values[label]==expected
                    calls.append((label,value));values[label]=value;return value
            with patch.object(ms,'AccessibilityUI',UI), \
                 patch.object(ms,'_run',side_effect=test_settings.SettingsTests().fake_defaults(state)), \
                 patch.object(ms.time,'sleep'):
                with ms.MacSettings(journal=journal):
                    self.assertEqual(calls,[(ms.LABELS[0],'0'),(ms.LABELS[0],'1'),(ms.LABELS[1],'0'),(ms.LABELS[1],'1')])
                self.assertTrue(all(v=='1' for v in values.values()))
                self.assertFalse(journal.exists())

    def test_crash_between_off_and_on_restores_initially_on_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=Path(temp)/'saved.json'; values={ms.LABELS[0]:'0'}
            journal.write_text(json.dumps({'entries':[dict(backend='accessibility_ui',key=ms.LABELS[0],original='1',temporary='1',previous_temporary='0')]}))
            ui=Mock();ui.__enter__=Mock(return_value=ui);ui.__exit__=Mock(return_value=False)
            ui.read.side_effect=lambda label:values[label]
            def set_value(label,value,expected):
                self.assertEqual(values[label],expected);values[label]=value;return value
            ui.set.side_effect=set_value
            with patch.object(ms,'AccessibilityUI',return_value=ui),patch.object(ms,'_refresh_clock'):
                ms.restore_settings(journal)
            self.assertEqual(values[ms.LABELS[0]],'1');self.assertFalse(journal.exists())

    def test_recovery_temporarily_shields_signals_and_restores_handlers(self):
        before=signal.getsignal(signal.SIGINT)
        with ms._finish_recovery():
            self.assertEqual(signal.getsignal(signal.SIGINT),signal.SIG_IGN)
        self.assertIs(signal.getsignal(signal.SIGINT),before)

    def test_focus_query_failure_does_not_invoke_system_events_before_open(self):
        with patch.dict(sys.modules,{'AppKit':None}), \
             patch.object(ma,'run_command',return_value=test_settings.DummyResult()) as run:
            with ma.AccessibilityUI(): pass
        # v0.3.7: a pgrep for System Settings precedes open; still no System Events.
        commands=[c.args[0][0] for c in run.call_args_list]
        self.assertEqual(commands,['pgrep','open'])
        self.assertNotIn('osascript',commands)

