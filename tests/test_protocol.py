from pathlib import Path
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# Linux: GNU ld --gc-sections. macOS: Apple ld -dead_strip (clang).
CC = shutil.which('clang') if sys.platform == 'darwin' else shutil.which('gcc')
GC_FLAG = '-Wl,-dead_strip' if sys.platform == 'darwin' else '-Wl,--gc-sections'
from PIL import Image
from test_bridge import DummyScreen
import mac84_bridge as mb


class PipeWire:
    def __init__(self, child):
        self.child = child
        self.acks = 0
    def write(self, data):
        return self.child.stdin.write(data)
    def flush(self):
        self.child.stdin.flush()
    def readline(self):
        data = bytearray()
        deadline = time.monotonic() + 5
        while not data.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.child.stdout], [], [], remaining)[0]:
                raise AssertionError('Calculator parser did not respond')
            byte = os.read(self.child.stdout.fileno(), 1)
            if not byte:
                raise AssertionError('Calculator parser exited prematurely')
            data.extend(byte)
        if data == b'A\n':
            self.acks += 1
        return bytes(data)


@unittest.skipUnless(CC, 'A C compiler (gcc or clang) is required')
class ProtocolTests(unittest.TestCase):
    def test_python_sender_and_real_c_receiver_with_partial_io(self):
        for window in (1, 2):
            with self.subTest(ack_window=window):
                self.run_peer(window)

    def run_peer(self, window):
        here = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            binary, dump = Path(tmp)/'peer', Path(tmp)/'pixels'
            subprocess.run([CC,'-std=c99','-O1','-g','-fsanitize=address,undefined',
                '-fno-omit-frame-pointer','-ffunction-sections','-fdata-sections',
                '-I'+str(here/'host'),str(here/'protocol_peer.c'),GC_FLAG,'-o',str(binary)], check=True)
            source = DummyScreen(Image.new('RGB',(320,240),'red'))
            session = mb.ViewSession(source)
            session.ack_window = window
            # A mixture of incompressible image tiles and flat RLE tiles.
            pixels = bytearray((i*17+i//320)%256 for i in range(320*240*3))
            latest = Image.frombytes('RGB',(320,240),bytes(pixels))
            latest.paste('blue', (0,128,320,240))
            # Antialiased text: few colors per tile, the case packed 'Q' tiles target.
            from PIL import ImageDraw
            draw = ImageDraw.Draw(latest)
            draw.rectangle((0,160,320,240), fill=(250,250,250))
            for row in range(5):
                draw.text((4, 162 + row*15), 'Packed tiles: The quick brown fox 0123', fill=(30,30,30))
            kinds = [mb.encode_tile(mb.tile_at(mb.rgb332_image(latest), x, y), True)[0]
                     for y in range(15) for x in range(20)]
            self.assertGreater(kinds.count('Q'), 10)  # the test really exercises Q
            with subprocess.Popen([str(binary),str(dump)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  bufsize=0, env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'}) as child:
                wire = PipeWire(child)
                frames = diagnostics = 0
                while True:
                    line = wire.readline().decode().strip()
                    if line == 'DONE': break
                    self.assertIn(line.split('|')[0], ('@M84','V','D'))
                    if line.startswith('@M84'): self.assertEqual(line, '@M84|5')
                    if line.startswith('D|'): diagnostics += 1
                    mb.parse_request(wire,session,None,line,0)
                    if line == 'V':
                        frames += 1
                        if frames == 1: source.image = latest
                self.assertEqual(child.wait(timeout=3),0)
            self.assertEqual(frames,11)
            self.assertTrue(session.packed_tiles)
            self.assertEqual(dump.read_bytes(),mb.rgb332_image(latest))
            self.assertGreater(wire.acks,70)
            self.assertGreater(diagnostics,0)  # event codes were reported, not fatal
