"""v0.3.4: packed tiles, negotiation, top-down passes, coalesced groups, new keys."""
import random
import unittest
from PIL import Image

from test_bridge import FakeSerial, DummyScreen
import mac84_bridge as mb


def parse_stream(data: bytes):
    """Decode the Mac->TI byte stream the way the calculator does."""
    pos, tiles = 0, []
    def line():
        nonlocal pos
        end = data.index(b'\n', pos); text = data[pos:end].decode(); pos = end + 1
        return text
    header = line(); assert header.startswith('F|')
    for _ in range(int(header.split('|')[1])):
        _, x, y, fmt, length = line().split('|')
        payload = data[pos:pos + int(length)]; pos += int(length)
        tiles.append((int(x), int(y), fmt, mb.decode_tile(fmt, payload)))
    assert line() == 'E' and pos == len(data)
    return tiles


class Recorder:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        return lambda *a: self.calls.append((name,) + a) or 'ok'


class PackedTileTests(unittest.TestCase):
    def test_round_trip_every_bit_width_and_color_count(self):
        rng = random.Random(84)
        for colors in (1, 2, 3, 4, 5, 9, 16):
            palette = rng.sample(range(256), colors)
            block = bytes(rng.choice(palette) for _ in range(256))
            packet = mb.pack_tile(block)
            self.assertEqual(len(packet), 1 + colors + 32 * mb.packed_bits(colors))
            self.assertEqual(mb.unpack_tile(packet), block)

    def test_packed_used_only_when_negotiated_and_smaller(self):
        text = bytes((0, 255, 146, 73)[(i * 7 + i // 16) % 4] for i in range(256))
        self.assertEqual(mb.encode_tile(text)[0], 'P')
        fmt, packet = mb.encode_tile(text, packed=True)
        self.assertEqual((fmt, len(packet)), ('Q', 69))
        flat = bytes(256)
        self.assertEqual(mb.encode_tile(flat, packed=True)[0], 'R')  # RLE beats Q
        noisy = bytes(range(256))
        self.assertEqual(mb.encode_tile(noisy, packed=True)[0], 'P')

    def test_malformed_packed_tiles_are_rejected(self):
        good = mb.pack_tile(bytes((1, 2, 3) * 85 + (1,)))
        with self.assertRaises(ValueError):
            mb.unpack_tile(good[:-1])
        bad_index = bytearray(good); bad_index[-1] = 0xFF  # index 3 with 3 colors
        with self.assertRaises(ValueError):
            mb.unpack_tile(bytes(bad_index))


class NegotiationTests(unittest.TestCase):
    def test_greeting_versions(self):
        for greeting, reply, packed in (('@M84', b'@M84READY\n', False),
                                        ('@M84|4', b'@M84READY|4\n', False),
                                        ('@M84|5', b'@M84READY|5\n', True)):
            session = mb.ViewSession(DummyScreen(Image.new('RGB', (320, 240))))
            wire = FakeSerial()
            mb.parse_request(wire, session, None, greeting, 0)
            self.assertEqual(bytes(wire.sent), reply)
            self.assertEqual(session.packed_tiles, packed)


class StreamTests(unittest.TestCase):
    def test_coalesced_stream_is_exactly_decodable_and_matches_screen(self):
        img = Image.frombytes('RGB', (320, 240),
                              bytes((i * 31 + i // 960) % 256 for i in range(320 * 240 * 3)))
        img.paste((240, 240, 240), (0, 120, 320, 240))
        session = mb.ViewSession(DummyScreen(img)); session.packed_tiles = True
        pixels = mb.rgb332_image(mb.crop_view(img, mb.ZOOM_DEFAULT, 160, 120)[0])
        wire = FakeSerial()
        mb.send_frame(wire, session, pace=0)
        tiles = parse_stream(bytes(wire.sent))
        self.assertEqual(len(tiles), 36)
        for x, y, _, block in tiles:
            self.assertEqual(block, mb.tile_at(pixels, x, y))
        self.assertEqual(wire.ack_count, 9)
        # Paced writes are at most 48 bytes (the proven chunk size).
        chunks = []
        class Chunky(FakeSerial):
            def write(self, data):
                chunks.append(len(data)); return super().write(data)
        mb.send_frame(Chunky(), session, pace=0)
        self.assertLessEqual(max(chunks), 48)

    def test_next_pass_starts_at_top_after_screen_caught_up(self):
        screen = DummyScreen(Image.frombytes('RGB', (320, 240),
                             bytes((i * 13) % 256 for i in range(320 * 240 * 3))))
        session = mb.ViewSession(screen)
        while True:  # finish the initial pass
            batch, _, more = session.render()
            for tx, ty, *_rest, block in batch: session.ack_tile(tx, ty, block)
            if not more: break
        # A small change in the middle leaves the rotating start mid-screen ...
        screen.image = screen.image.copy(); screen.image.paste('red', (150, 110, 170, 130))
        batch, _, more = session.render()
        for tx, ty, *_rest, block in batch: session.ack_tile(tx, ty, block)
        self.assertFalse(more)
        # ... but a full-screen change (Settings, Mission Control) starts at row 0.
        screen.image = Image.new('RGB', (320, 240), (10, 200, 30))
        screen.image.paste('white', (0, 0, 320, 240), mask=Image.effect_noise((320, 240), 90).point(lambda v: 255 * (v > 128)))
        batch, _, more = session.render()
        self.assertTrue(more)
        self.assertEqual(batch[0][:2], (0, 0))
        self.assertEqual([t[1] for t in batch], sorted(t[1] for t in batch))


class ControlTests(unittest.TestCase):
    def test_new_top_row_commands(self):
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (3024, 1964))))
        session.render()
        dev = Recorder()
        for message in ('C|D|160|120', 'L|160|120', 'X|M', 'X|A'):
            mb.parse_request(FakeSerial(), session, dev, message, 0)
        names = [c[0] for c in dev.calls]
        self.assertEqual(names, ['double_click', 'look_up', 'show_windows', 'show_windows'])
        self.assertEqual(dev.calls[2][1:], (False,)); self.assertEqual(dev.calls[3][1:], (True,))
        x, y = dev.calls[0][1:]
        self.assertAlmostEqual(x, 1512, delta=2); self.assertAlmostEqual(y, 982, delta=2)

    def test_v035_named_keys_and_drag(self):
        session = mb.ViewSession(DummyScreen(Image.new('RGB', (3024, 1964))))
        session.render()
        dev = Recorder()
        for name in ('RETURN', 'ESC', 'CUT', 'COPY', 'PASTE', 'ALL', 'UNDO', 'REDO'):
            mb.parse_request(FakeSerial(), session, dev, 'B|' + name, 0)
        mb.parse_request(FakeSerial(), session, dev, 'B|DELETE_EVERYTHING', 0)  # not whitelisted
        mb.parse_request(FakeSerial(), session, dev, 'G|D|160|120', 0)
        mb.parse_request(FakeSerial(), session, dev, 'G|U|170|120', 0)
        self.assertEqual([c[1] for c in dev.calls if c[0] == 'press_named'],
                         ['RETURN', 'ESC', 'CUT', 'COPY', 'PASTE', 'ALL', 'UNDO', 'REDO'])
        self.assertEqual([c[1] for c in dev.calls if c[0] == 'drag'], ['D', 'U'])
        self.assertEqual(mb.MacInput.NAMED_KEYS['REDO'], (6, (55, 56)))  # Cmd+Shift+Z
        self.assertEqual(mb.MacInput.NAMED_KEYS['COPY'], (8, (55,)))     # Cmd+C

    def test_arrow_shortcuts_carry_fn_flag_and_drag_is_released(self):
        posted = []
        class Q:  # minimal Quartz stand-in recording flags and event kinds
            kCGEventFlagMaskControl, kCGEventFlagMaskCommand, kCGEventFlagMaskShift = 1, 2, 4
            kCGEventFlagMaskSecondaryFn, kCGEventFlagMaskNumericPad = 8, 16
            kCGHIDEventTap = 0
            kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventLeftMouseDragged, kCGEventMouseMoved = 'down', 'up', 'drag', 'move'
            kCGMouseButtonLeft, kCGMouseEventClickState = 0, 0
            def CGEventCreateKeyboardEvent(self, _, key, down): return {'key': key, 'down': down}
            def CGEventSetFlags(self, ev, flags): ev['flags'] = flags
            def CGEventCreateMouseEvent(self, _, kind, point, button): return {'kind': kind}
            def CGEventSetIntegerValueField(self, *a): pass
            def CGEventPost(self, tap, ev): posted.append(ev)
            def CGMainDisplayID(self): return 1
            def CGDisplayBounds(self, _):
                from types import SimpleNamespace as N
                return N(origin=N(x=0, y=0), size=N(width=1512, height=982))
        dev = mb.MacInput(True, (3024, 1964)); dev.dry_run = False; dev.quartz = Q()
        dev.switch_space('L')
        arrow = [e for e in posted if e.get('key') == 123]
        self.assertTrue(arrow and all(e['flags'] == 1 | 8 | 16 for e in arrow))
        posted.clear(); dev.press_named('COPY')
        self.assertTrue(all(e['flags'] & 8 == 0 for e in posted))  # no fn on letters
        posted.clear()
        dev.drag('D', 100, 100); dev.move(120, 100); dev.release_drag()
        self.assertEqual([e['kind'] for e in posted], ['down', 'drag', 'up'])
        self.assertFalse(dev.dragging)

    def test_dry_run_input_methods_are_safe(self):
        dev = mb.MacInput(True, (3024, 1964))
        dev.double_click(1, 1); dev.show_windows(); dev.show_windows(True)
        self.assertEqual(dev.look_up(1, 1), 'dry run')


if __name__ == '__main__':
    unittest.main()
