"""v0.3.6: zoom around the cursor, version check, @BYE, restore cross-check."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

from test_bridge import FakeSerial, DummyScreen
import test_settings
import test_regressions
import mac84_bridge as mb
import mac_settings as ms

W, H = 3024, 1964  # this Mac's display


def session_at(zoom=1):
    s = mb.ViewSession(DummyScreen(Image.new('RGB', (W, H), 'gray')))
    s.zoom = zoom
    s.render()
    return s


class ZoomAtCursorTests(unittest.TestCase):
    def test_zoom_in_at_top_clamps_to_full_view_and_keeps_point_under_cursor(self):
        s = session_at(zoom=0)                          # whole desktop, letterboxed
        geo = s.current_geometry
        cursor = (160, geo.view_y + 2)                  # very top of the picture
        px, py = s.zoom_at(+1, *cursor)
        new = s.current_geometry
        self.assertEqual(new.crop_y, 0)                 # clamped: no area above the desktop
        self.assertEqual((new.crop_w, new.crop_h), (960, 720))
        self.assertTrue(0 <= new.crop_x and new.crop_x + new.crop_w <= W)
        # The cursor is moved to where that desktop point now appears.
        vx, vy = s.pending_cursor
        back = new.point_from_view(vx, vy)
        self.assertAlmostEqual(back[0], px, delta=new.crop_w / 320 + 1)
        self.assertAlmostEqual(back[1], py, delta=new.crop_h / 240 + 1)
        self.assertLess(vy, 20)                         # still near the top

    def test_zoom_in_centres_when_there_is_room_and_the_image_matches(self):
        s = session_at(zoom=1)
        s.zoom_at(+1, 160, 120)                         # centre of the view
        geo = s.current_geometry
        self.assertEqual((geo.crop_w, geo.crop_h), (640, 480))
        self.assertEqual(s.pending_cursor, (160, 120))
        batch, geo2, _ = s.render()                     # next real frame agrees
        self.assertEqual((geo2.crop_x, geo2.crop_y), (geo.crop_x, geo.crop_y))

    def test_corner_cursor_clamps_both_axes(self):
        s = session_at(zoom=0)                          # picture corner = desktop corner
        g = s.current_geometry
        s.zoom_at(+1, g.view_x + g.view_w - 1, g.view_y + g.view_h - 1)
        geo = s.current_geometry
        self.assertEqual((geo.crop_x + geo.crop_w, geo.crop_y + geo.crop_h), (W, H))
        vx, vy = s.pending_cursor
        self.assertGreater(vx, 300); self.assertGreater(vy, 220)

    def test_letterbox_cursor_uses_nearest_desktop_edge_and_zoom_out_to_fit(self):
        s = session_at(zoom=0)
        self.assertGreater(s.current_geometry.view_y, 0)  # bars exist
        s.zoom_at(+1, 160, 0)                            # in the top bar
        self.assertEqual(s.current_geometry.crop_y, 0)
        s.zoom_at(-1, 160, 120)
        geo = s.current_geometry
        self.assertIsNone(mb.ZOOM_MODES[s.zoom])
        vx, vy = s.pending_cursor
        self.assertTrue(geo.view_y <= vy < geo.view_y + geo.view_h)

    def test_limits_do_nothing_and_old_requests_keep_old_behaviour(self):
        s = session_at(zoom=len(mb.ZOOM_MODES) - 1)
        self.assertIsNone(s.zoom_at(+1, 10, 10))
        self.assertIsNone(s.pending_cursor)
        s = session_at(zoom=1)
        mb.parse_request(FakeSerial(), s, None, 'Z|+', 0)  # v0.3.5 calculator
        self.assertEqual(s.zoom, 2)
        self.assertIsNone(s.pending_cursor)

    def test_request_moves_mac_pointer_and_next_batch_starts_with_pointer_line(self):
        s = session_at(zoom=1)
        moved = []
        class Dev:
            def move(self, x, y): moved.append((x, y))
        mb.parse_request(FakeSerial(), s, Dev(), 'Z|+|100|50', 0)
        self.assertEqual(len(moved), 1)
        wire = FakeSerial()
        mb.send_frame(wire, s, pace=0)
        first = bytes(wire.sent).split(b'\n')[:2]
        self.assertEqual(first[0], 'M|{}|{}'.format(*s.current_geometry.view_from_point(*moved[0])).encode())
        self.assertTrue(first[1].startswith(b'F|'))
        wire = FakeSerial(); mb.send_frame(wire, s, pace=0)
        self.assertTrue(bytes(wire.sent).startswith(b'F|'))  # only once

    def test_pan_from_an_edge_responds_immediately(self):
        s = session_at(zoom=1)
        for _ in range(10): s.pan(60, 0); s.render()       # far past the right edge
        right = s.current_geometry.crop_x
        s.pan(-60, 0); s.render()
        self.assertLess(s.current_geometry.crop_x, right)


class VersionAndByeTests(unittest.TestCase):
    def greet(self, message):
        s = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240))))
        wire, out = FakeSerial(), io.StringIO()
        with contextlib.redirect_stdout(out):
            mb.parse_request(wire, s, None, message, 0)
        return s, bytes(wire.sent), out.getvalue()

    def test_matching_version_no_warning(self):
        s, reply, out = self.greet('@M84|5|' + mb.BRIDGE_VERSION)
        self.assertEqual(reply, b'@M84READY|5\n')
        self.assertTrue(s.packed_tiles)
        self.assertIn('MAC84 v' + mb.BRIDGE_VERSION, out)
        self.assertNotIn('WARNING', out)

    def test_stale_calculator_program_is_reported(self):
        s, reply, out = self.greet('@M84|5')            # v0.3.4/v0.3.5 program
        self.assertEqual(reply, b'@M84READY|5\n')
        self.assertIn('WARNING', out)
        self.assertIn('MAC84.8xp', out)

    def test_bye_is_a_normal_quit_during_ack_or_idle(self):
        s = mb.ViewSession(DummyScreen(Image.frombytes('RGB', (320, 240),
                           bytes((i * 7) % 256 for i in range(320 * 240 * 3)))))
        wire = FakeSerial(); wire.responses = [b'A\n', b'@BYE\n']
        with self.assertRaises(mb.CalculatorQuit):
            mb.send_frame(wire, s, pace=0)
        with self.assertRaises(mb.CalculatorQuit):
            mb.parse_request(FakeSerial(), s, None, '@BYE', 0)
        self.assertFalse(issubclass(mb.CalculatorQuit, RuntimeError))


class RestoreCrossCheckTests(unittest.TestCase):
    def run_restore(self, state):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'saved.json'
            journal.write_text(json.dumps({'version': 4, 'entries': [
                {'backend': 'accessibility_ui', 'key': 'Reduce motion', 'original': '0', 'temporary': '1'},
                {'backend': 'accessibility_ui', 'key': 'Prefer non-blinking cursor', 'original': '0', 'temporary': '1'},
            ]}))
            class BrokenUI:  # what the logs showed: window vanished / not found
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self, label): raise RuntimeError(f"Can't get window \"Display\" ({label})")
            out = io.StringIO()
            with patch.object(ms, '_supports_ui', return_value=True), \
                 patch.object(ms, 'LEGACY_JOURNALS', ()), \
                 patch.object(ms, '_run', side_effect=test_settings.SettingsTests().fake_defaults(state)), \
                 patch.object(ms, 'AccessibilityUI', BrokenUI), contextlib.redirect_stdout(out):
                try:
                    ms.restore_settings(journal)
                    error = None
                except RuntimeError as ex:
                    error = str(ex)
            return journal.exists(), error, out.getvalue()

    def test_failed_ui_but_preferences_show_originals_clears_backup(self):
        exists, error, out = self.run_restore({
            ('com.apple.universalaccess', 'reduceMotion'): '0',
            ('com.apple.Accessibility', 'PrefersNonBlinkingCursorIndicator'): '0'})
        self.assertIsNone(error)
        self.assertFalse(exists)
        self.assertIn('already shows the original value', out)

    def test_still_changed_or_unknown_keeps_backup(self):
        for state in ({('com.apple.universalaccess', 'reduceMotion'): '1',
                       ('com.apple.Accessibility', 'PrefersNonBlinkingCursorIndicator'): '0'},
                      {}):  # missing keys prove nothing
            exists, error, _ = self.run_restore(state)
            self.assertTrue(exists)
            self.assertIn('Settings restore incomplete', error)


if __name__ == '__main__':
    unittest.main()
