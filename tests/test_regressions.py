import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac'))
import mac84_bridge as mb
import mac_settings as ms
from test_bridge import DummyScreen, FakeSerial
import test_settings


class GeometryTests(unittest.TestCase):
    def test_letterboxed_edges_land_on_desktop_edges(self):
        for size in ((2560, 1600), (1920, 1080), (1000, 2000), (1365, 767), (800, 600)):
            with self.subTest(size=size):
                shown, g = mb.crop_view(Image.new('RGB', size, 'white'), 0, 0, 0)
                self.assertEqual(g.point_from_view(g.view_x, g.view_y), (0, 0))
                end = (g.view_x + g.view_w - 1, g.view_y + g.view_h - 1)
                self.assertEqual(g.point_from_view(*end), (size[0]-1, size[1]-1))
                # The rendering and pointer transform use exactly the same bounds.
                self.assertEqual(shown.getbbox(), (g.view_x, g.view_y, end[0]+1, end[1]+1))
                for point in ((g.view_x - 1, g.view_y), (g.view_x, g.view_y - 1),
                              (end[0] + 1, end[1]), (end[0], end[1] + 1)):
                    self.assertIsNone(g.point_from_view(*point))

    def test_clicks_and_moves_use_same_mapping_ignore_bars(self):
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (1920, 1080))))
        session.zoom = 0
        session.render()
        dev, wire = Mock(), FakeSerial()
        mb.parse_request(wire, session, dev, 'C|L|319|209', 0)
        dev.click.assert_called_once_with('L', 1919.0, 1079.0)
        mb.parse_request(wire, session, dev, 'M|319|209', 0)
        dev.move.assert_called_once_with(1919.0, 1079.0)
        mb.parse_request(wire, session, dev, 'C|R|160|239', 0)
        mb.parse_request(wire, session, dev, 'M|160|0', 0)
        self.assertEqual(dev.click.call_count, 1)
        self.assertEqual(dev.move.call_count, 1)

    def test_cropped_zoom_maps_corners_without_exceeding_source(self):
        for zoom in range(1, 5):
            _, g = mb.crop_view(Image.new('RGB', (2560, 1600)), zoom, 2500, 1590)
            self.assertEqual(g.point_from_view(0, 0), (g.crop_x, g.crop_y))
            self.assertEqual(g.point_from_view(319, 239),
                             (g.crop_x + g.crop_w - 1, g.crop_y + g.crop_h - 1))

    def test_editor_close_resends_strip_when_mac_has_not_changed(self):
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240), 'white')))
        wire = FakeSerial()
        for _ in range(25):
            mb.send_frame(wire, session, pace=0)
        self.assertEqual(len(session.old_tiles), 300)
        mb.parse_request(wire, session, Mock(), 'I', 0)
        self.assertEqual(len(session.old_tiles), 240)
        patches, _, more = session.render()
        self.assertEqual(len(patches), 36)
        self.assertTrue(more)
        self.assertEqual({p[1] for p in patches + session.pending}, {12, 13, 14})


class UISettingsTests(unittest.TestCase):
    def fake_ui(self, values, journal, fail_restore=False):
        class UI:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, label): return values[label]
            def set(self, label, value, expected):
                # Ensure the disk backup exists BEFORE the actual mutation.
                saved = json.loads(journal.read_text())
                assert any(x.get('key') == label for x in saved['entries'])
                if fail_restore and value == '0': raise RuntimeError('UI access denied')
                if values[label] != expected: return 'conflict'
                values[label] = value
                return value
        return UI

    def test_ui_switches_and_clock_restore_originals(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp)/'saved.json'
            state = {('com.apple.menuextra.clock', 'ShowSeconds'): '1'}
            original = state.copy()
            values = dict(zip(ms.LABELS, ('0', '1')))
            with patch.object(ms, '_supports_ui', return_value=True), \
                 patch.object(ms, 'LEGACY_JOURNALS', ()), \
                 patch.object(ms, '_run', side_effect=test_settings.SettingsTests().fake_defaults(state)), \
                 patch.object(ms, 'AccessibilityUI', self.fake_ui(values, journal)):
                with ms.MacSettings(journal=journal):
                    self.assertEqual(list(values.values()), ['1','1'])
                    self.assertNotIn(('com.apple.universalaccess','reduceMotion'), state)
                self.assertEqual(list(values.values()), ['0','1'])
                self.assertEqual(state.keys(), original.keys())
                for ident in original:
                    self.assertTrue(ms._same(state[ident], original[ident], "-bool"))
                self.assertFalse(journal.exists())

    def test_ui_failure_keeps_backup_while_restoring_clock(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp)/'saved.json'
            state = {('com.apple.menuextra.clock', 'ShowSeconds'): '1'}
            values = dict.fromkeys(ms.LABELS, '0')
            with patch.object(ms, '_supports_ui', return_value=True), \
                 patch.object(ms, 'LEGACY_JOURNALS', ()), \
                 patch.object(ms, '_run', side_effect=test_settings.SettingsTests().fake_defaults(state)), \
                 patch.object(ms, 'AccessibilityUI', self.fake_ui(values, journal, True)):
                with self.assertRaisesRegex(RuntimeError, 'UI access denied'):
                    with ms.MacSettings(journal=journal): pass
                self.assertTrue(journal.exists())
                self.assertTrue(ms._same(state[('com.apple.menuextra.clock','ShowSeconds')], '1', '-bool'))

    def test_ui_permission_denial_does_not_block_mirroring(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp)/'saved.json'
            state = {}
            with patch.object(ms, '_supports_ui', return_value=True), \
                 patch.object(ms, 'LEGACY_JOURNALS', ()), \
                 patch.object(ms, '_run', side_effect=test_settings.SettingsTests().fake_defaults(state)), \
                 patch.object(ms, 'AccessibilityUI', side_effect=RuntimeError('permission denied')):
                with ms.MacSettings(journal=journal):
                    self.assertEqual(state[('com.apple.menuextra.clock','ShowSeconds')], 'false')
                self.assertEqual(state, {})
                self.assertFalse(journal.exists())
