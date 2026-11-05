"""v0.3.3: a vanished calculator must produce a located, actionable error."""
import contextlib
import io
import unittest
from PIL import Image

from test_bridge import FakeSerial, DummyScreen
import mac84_bridge as mb


class VanishingSerial(FakeSerial):
    """ACKs the first N groups, then fails like pyserial on a removed device."""
    def __init__(self, good_acks):
        super().__init__()
        self.good_acks = good_acks

    def readline(self):
        if self.good_acks <= 0:
            raise OSError(6, 'read failed: [Errno 6] Device not configured')
        self.good_acks -= 1
        return b'A\n'


def noisy_image():
    return Image.frombytes('RGB', (320, 240),
                           bytes((i * 17 + i // 320) % 256 for i in range(320 * 240 * 3)))


class LinkDiagnosticsTests(unittest.TestCase):
    def test_device_vanishing_mid_batch_reports_batch_and_tile(self):
        session = mb.ViewSession(DummyScreen(noisy_image()))
        wire = FakeSerial()
        mb.send_frame(wire, session, pace=0)          # batch 1 completes
        wire = VanishingSerial(good_acks=2)           # batch 2 dies at group 3
        with self.assertRaises(RuntimeError) as caught:
            mb.send_frame(wire, session, pace=0)
        text = str(caught.exception)
        self.assertIn('batch 2, tile 12 of 36', text)
        self.assertIn('44/300 tiles confirmed', text)
        self.assertIn('Device not configured', text)
        self.assertIn('MAC84 stopped first', text)

    def test_empty_ack_explains_timeout(self):
        session = mb.ViewSession(DummyScreen(noisy_image()))
        wire = FakeSerial()
        wire.responses = [b'']
        with self.assertRaisesRegex(RuntimeError, r'batch 1, tile 4 of 36.*2 s timeout'):
            mb.send_frame(wire, session, pace=0)

    def test_calculator_diagnostic_line_is_logged_not_rejected(self):
        session = mb.ViewSession(DummyScreen(noisy_image()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mb.parse_request(FakeSerial(), session, None, 'D|usb_event_error|3|1', 0)
        self.assertIn('[calc] usb_event_error 3 1', out.getvalue())
        self.assertNotIn('malformed', out.getvalue())


if __name__ == '__main__':
    unittest.main()
