import importlib.util
import io
from pathlib import Path
import sys
import unittest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mac'))
import mac84_bridge as mb


class FakeSerial:
    def __init__(self):
        self.sent = bytearray()
        self.responses = []
        self.ack_count = 0

    def write(self, data):
        self.sent.extend(data)
        return len(data)

    def flush(self):
        pass

    def readline(self):
        self.ack_count += 1
        return self.responses.pop(0) if self.responses else b'A\n'


class DummyScreen:
    def __init__(self, image):
        self.image = image

    def capture(self):
        return self.image.copy()


class Tests(unittest.TestCase):
    def test_zoom_default_matches_image_two(self):
        self.assertEqual(mb.ZOOM_MODES[mb.ZOOM_DEFAULT], (960, 720))
        im = Image.new('RGB', (2560, 1600))
        out, geo = mb.crop_view(im, mb.ZOOM_DEFAULT, 1280, 800)
        self.assertEqual(out.size, (320, 240))
        self.assertEqual((geo.crop_x, geo.crop_y, geo.crop_w, geo.crop_h),
                         (800, 440, 960, 720))

    def test_rle_roundtrip(self):
        for block in [bytes([0] * 256), bytes(range(256)),
                      bytes([255, 255, 0, 0] * 64)]:
            packed = mb.rle_encode(block)
            self.assertLessEqual(len(packed), 512)
            self.assertEqual(mb.rle_decode(packed), block)

    def test_rgb332(self):
        im = Image.new('RGB', (2, 1))
        im.putpixel((0, 0), (255, 0, 0))
        im.putpixel((1, 0), (0, 255, 255))
        self.assertEqual(mb.rgb332_image(im), bytes([0xE0, 0x1F]))

    def test_only_changed_tiles_sent(self):
        im = Image.new('RGB', (320, 240), 'white')
        session = mb.ViewSession(DummyScreen(im))
        wire = FakeSerial()
        mb.send_frame(wire, session, pace=0)
        self.assertIn(b'F|36|1\n', wire.sent)
        self.assertEqual(len(session.old_tiles), 36)
        # Repeat until screen cache is complete, then expect no changes.
        for _ in range(24):
            mb.send_frame(wire, session, pace=0)
        wire.sent.clear()
        mb.send_frame(wire, session, pace=0)
        self.assertEqual(bytes(wire.sent), b'F|0|0\nE\n')
        # Only one tile changes.
        im.putpixel((18, 20), (0, 0, 0))
        wire.sent.clear()
        mb.send_frame(wire, session, pace=0)
        self.assertTrue(wire.sent.startswith(b'F|1|0\nT|1|1|'))
        self.assertEqual(len(session.old_tiles), 300)

    def test_ack_required_before_cache_commit(self):
        im = Image.new('RGB', (320, 240), 'white')
        session = mb.ViewSession(DummyScreen(im))
        wire = FakeSerial()
        wire.responses = [b'N\n']
        with self.assertRaisesRegex(RuntimeError, 'ACK'):
            mb.send_frame(wire, session, pace=0)
        self.assertEqual(session.old_tiles, {})

    def test_quartz_input_is_disabled_in_mock(self):
        input_dev = mb.MacInput(dry_run=True, display_px=(320, 240))
        input_dev.move(1, 1)
        input_dev.click('L', 1, 1)
        input_dev.scroll(4)
        input_dev.type_text('hello')


if __name__ == '__main__':
    unittest.main()


class HandshakeTests(unittest.TestCase):
    def test_greeting_generates_ready(self):
        mb = sys.modules['mac84_bridge']
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240), 'white')))
        wire = FakeSerial()
        mb.parse_request(wire, session,
                         mb.MacInput(dry_run=True, display_px=(320, 240)),
                         '@M84', 0, False, link_test=True)
        self.assertEqual(bytes(wire.sent), b'@M84READY\n')

    def test_link_test_frame_does_not_transfer_pixels(self):
        mb = sys.modules['mac84_bridge']
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240), 'white')))
        wire = FakeSerial()
        mb.parse_request(wire, session,
                         mb.MacInput(dry_run=True, display_px=(320, 240)),
                         'V', 0, False, link_test=True)
        self.assertEqual(bytes(wire.sent), b'F|0|0\nE\n')
        self.assertEqual(session.old_tiles, {})


class OptimizationTests(unittest.TestCase):
    def test_photo_tiles_are_raw_and_only_256_bytes(self):
        raw = bytes(range(256))
        kind, data = mb.encode_tile(raw)
        self.assertEqual(kind, 'P')
        self.assertEqual(data, raw)

    def test_flat_tiles_compress_below_raw(self):
        raw = bytes([0] * 256)
        kind, data = mb.encode_tile(raw)
        self.assertEqual(kind, 'R')
        self.assertEqual(mb.rle_decode(data), raw)

    def test_each_batch_rechecks_the_desktop(self):
        class CounterScreen:
            def __init__(self):
                self.calls = 0
            def capture(self):
                self.calls += 1
                return Image.new('RGB', (320, 240), 'white')
        screen = CounterScreen()
        session = mb.ViewSession(screen)
        wire = FakeSerial()
        for _ in range(3):
            mb.send_frame(wire, session, pace=0)
        self.assertEqual(screen.calls, 3)
        self.assertEqual(len(session.old_tiles), 108)

    def test_pending_invalidated_by_pan_and_zoom(self):
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (1200, 900))))
        session.render()
        self.assertTrue(session.pending)
        session.pan(60, 0)
        self.assertFalse(session.pending)
        session.render()
        session.zoom_by(1)
        self.assertFalse(session.pending)



class GroupedACKTests(unittest.TestCase):
    def test_first_36_tile_batch_only_9_ack_roundtrips(self):
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240), 'white')))
        wire = FakeSerial()
        mb.send_frame(wire, session, pace=0)
        self.assertEqual(wire.ack_count, 9)
        self.assertEqual(len(session.old_tiles), 36)

    def test_partial_batch_ack(self):
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240), 'white')))
        session.patches_per_frame = 5
        wire = FakeSerial()
        mb.send_frame(wire, session, pace=0)
        self.assertEqual(wire.ack_count, 2)
        self.assertEqual(len(session.old_tiles), 5)
