#!/usr/bin/env python3
"""Mac84 v0.3.3 desktop mirroring USB bridge (independent of Gemini84).

Local USB only. Captures the main macOS display, crops/resizes to 320x240,
converts to RGB332 (256 colors), and sends only changed 16x16 tiles.
The calculator requests each update and ACKs each group of four tiles.
This is a protocol change from v0.2: update both calculator and Mac bridge. Do not run alongside Gemini bridge.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import io
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Optional

from PIL import Image, ImageOps, ImageChops
from mac_settings import restore_all_settings
from settings_worker import SettingsProcess

WIDTH, HEIGHT = 320, 240
TILE = 16
COLS, ROWS = WIDTH // TILE, HEIGHT // TILE
MAX_PATCHES = 36
ACK_GROUP = 4   # Acknowledge every four completed tiles (both ends must match)
ZOOM_MODES = (None, (960, 720), (640, 480), (480, 360), (320, 240))
ZOOM_DEFAULT = 1  # screenshot 02_960x720, as requested


def rgb332_image(image: Image.Image) -> bytes:
    """Quantize to fixed RGB332 palette for direct palettized screen writes."""
    red, green, blue = image.convert('RGB').split()
    red = red.point([i & 0xE0 for i in range(256)])
    green = green.point([(i & 0xE0) >> 3 for i in range(256)])
    blue = blue.point([i >> 6 for i in range(256)])
    # The bit fields do not overlap, so addition is exactly bitwise OR.
    return ImageChops.add(ImageChops.add(red, green), blue).tobytes()


def rle_encode(block: bytes) -> bytes:
    """Pairs of (count, palette index). Count is 1..255."""
    out = bytearray()
    index = 0
    while index < len(block):
        value = block[index]
        end = index + 1
        while end < len(block) and block[end] == value and end - index < 255:
            end += 1
        out.extend((end - index, value))
        index = end
    return bytes(out)



def encode_tile(block: bytes) -> tuple[str, bytes]:
    """Return smallest reversible representation; photos/text often defeat RLE."""
    encoded = rle_encode(block)
    return ('R', encoded) if len(encoded) < len(block) else ('P', block)

def rle_decode(packet: bytes, expected: int = TILE * TILE) -> bytes:
    if len(packet) % 2:
        raise ValueError('Malformed RLE: odd byte count')
    out = bytearray()
    for count, value in zip(packet[::2], packet[1::2]):
        if count == 0 or len(out) + count > expected:
            raise ValueError('Malformed RLE: invalid count')
        out.extend(bytes((value,)) * count)
    if len(out) != expected:
        raise ValueError(f'Malformed RLE: decoded {len(out)} pixels, expected {expected}')
    return bytes(out)


def tile_at(pixels: bytes, tx: int, ty: int) -> bytes:
    result = bytearray()
    for y in range(ty * TILE, (ty + 1) * TILE):
        start = y * WIDTH + tx * TILE
        result.extend(pixels[start:start + TILE])
    return bytes(result)


@dataclass
class Geometry:
    image_width: int
    image_height: int
    crop_x: int
    crop_y: int
    crop_w: int
    crop_h: int
    view_x: int = 0
    view_y: int = 0
    view_w: int = WIDTH
    view_h: int = HEIGHT

    def point_from_view(self, view_x: int, view_y: int) -> Optional[tuple[float, float]]:
        # This is the exact rendered rectangle, including integer rounding.
        x, y = view_x - self.view_x, view_y - self.view_y
        if not (0 <= x < self.view_w and 0 <= y < self.view_h):
            return None  # A letterbox pixel has no desktop coordinate.
        px = self.crop_x + x * (self.crop_w - 1) / max(1, self.view_w - 1)
        py = self.crop_y + y * (self.crop_h - 1) / max(1, self.view_h - 1)
        return px, py


def crop_view(image: Image.Image, zoom_index: int, center_x: float,
              center_y: float) -> tuple[Image.Image, Geometry]:
    width, height = image.size
    mode = ZOOM_MODES[zoom_index]
    if mode is None:
        fitted = ImageOps.contain(image, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        offset_x, offset_y = (WIDTH - fitted.width) // 2, (HEIGHT - fitted.height) // 2
        shown = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))
        shown.paste(fitted, (offset_x, offset_y))
        return shown, Geometry(width, height, 0, 0, width, height,
                               offset_x, offset_y, fitted.width, fitted.height)
    wanted_w, wanted_h = mode
    scale = min(1.0, width / wanted_w, height / wanted_h)
    crop_w = max(1, round(wanted_w * scale))
    crop_h = max(1, round(wanted_h * scale))
    crop_x = max(0, min(round(center_x - crop_w / 2), width - crop_w))
    crop_y = max(0, min(round(center_y - crop_h / 2), height - crop_h))
    shown = image.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
    return shown.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS), Geometry(
        width, height, crop_x, crop_y, crop_w, crop_h)


class Screen:
    def __init__(self, mock_image: Optional[Path] = None, display: int = 1):
        self.mock_image = mock_image
        self.display = display
        self._temp = tempfile.TemporaryDirectory(prefix='mac84-screen-')
        self.image_path = Path(self._temp.name) / 'screen.png'

    def capture(self) -> Image.Image:
        if self.mock_image is not None:
            with Image.open(self.mock_image) as src:
                return src.convert('RGB')
        if sys.platform != 'darwin':
            raise RuntimeError('Live capture needs macOS; use --mock-image for testing')
        # In-memory capture avoids launching screencapture and writing a PNG each cycle.
        try:
            import Quartz
            ids = Quartz.CGGetActiveDisplayList(16, None, None)[1]
            display_id = ids[self.display - 1]
            cg = Quartz.CGDisplayCreateImage(display_id)
            if cg is None:
                raise RuntimeError('Screen capture returned no image')
            w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
            stride = Quartz.CGImageGetBytesPerRow(cg)
            data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg)))
            info = Quartz.CGImageGetBitmapInfo(cg)
            order = int(info) & int(Quartz.kCGBitmapByteOrderMask)
            # Most Mac displays expose 32-bit little-endian BGRA.
            if Quartz.CGImageGetBitsPerPixel(cg) != 32 or order != Quartz.kCGBitmapByteOrder32Little:
                raise ValueError('Unsupported Quartz screen pixel format')
            return Image.frombytes('RGB', (w, h), data, 'raw', 'BGRX', stride, 1)
        except Exception as exc:
            if not getattr(self, '_fallback_logged', False):
                print(f'[capture] Quartz direct capture unavailable ({exc}); using screencapture fallback', flush=True)
                self._fallback_logged = True
            result = subprocess.run(['screencapture', '-x', '-D', str(self.display),
                                     '-t', 'png', str(self.image_path)], capture_output=True, text=True, timeout=6)
            if result.returncode or not self.image_path.exists():
                raise RuntimeError('Screen capture failed; check Screen Recording permission: '
                                   + result.stderr.strip())
            with Image.open(self.image_path) as src:
                return src.convert('RGB')

    def close(self):
        self._temp.cleanup()


class MacInput:
    """Quartz input backend. Safe dry-run when --mock-image is supplied."""
    def __init__(self, dry_run: bool, display_px: tuple[int, int]):
        self.dry_run = dry_run
        self.display_px = display_px
        self.quartz = None
        if not dry_run:
            try:
                import Quartz
            except ImportError as ex:
                raise RuntimeError('Install macOS Quartz bindings: pip install pyobjc-framework-Quartz') from ex
            self.quartz = Quartz

    def to_mac_points(self, px: float, py: float) -> tuple[float, float]:
        if self.dry_run:
            return px, py
        q = self.quartz
        display = q.CGMainDisplayID()
        bounds = q.CGDisplayBounds(display)
        # A Retina screenshot may have 2x the physical pixel dimensions of
        # mouse event coordinates. Convert using actual display bounds.
        return (float(bounds.origin.x) + px * float(bounds.size.width) / self.display_px[0],
                float(bounds.origin.y) + py * float(bounds.size.height) / self.display_px[1])

    def move(self, px: float, py: float):
        if self.dry_run:
            return
        q = self.quartz
        point = self.to_mac_points(px, py)
        event = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, point,
                                          q.kCGMouseButtonLeft)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def click(self, button: str, px: float, py: float):
        if self.dry_run:
            return
        q = self.quartz
        point = self.to_mac_points(px, py)
        right = (button == 'R')
        btn = q.kCGMouseButtonRight if right else q.kCGMouseButtonLeft
        down = q.kCGEventRightMouseDown if right else q.kCGEventLeftMouseDown
        up = q.kCGEventRightMouseUp if right else q.kCGEventLeftMouseUp
        for typ in (down, up):
            event = q.CGEventCreateMouseEvent(None, typ, point, btn)
            q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, 1)
            q.CGEventPost(q.kCGHIDEventTap, event)
            time.sleep(.025)

    def scroll(self, lines: int):
        if self.dry_run:
            return
        q = self.quartz
        ev = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1,
                                             max(-20, min(20, lines)))
        q.CGEventPost(q.kCGHIDEventTap, ev)

    def switch_space(self, direction: str):
        """macOS's standard Control+Left/Right action, including full-screen Spaces."""
        if direction not in ('L', 'R'):
            raise ValueError('Invalid Space direction')
        if self.dry_run:
            return
        q = self.quartz
        key = 123 if direction == 'L' else 124
        # Send explicit modifier down/up and release it even if posting fails.
        control_down = False
        try:
            ev = q.CGEventCreateKeyboardEvent(None, 59, True)
            q.CGEventSetFlags(ev, q.kCGEventFlagMaskControl)
            q.CGEventPost(q.kCGHIDEventTap, ev)
            control_down = True
            for down in (True, False):
                ev = q.CGEventCreateKeyboardEvent(None, key, down)
                q.CGEventSetFlags(ev, q.kCGEventFlagMaskControl)
                q.CGEventPost(q.kCGHIDEventTap, ev)
                time.sleep(.015)
        finally:
            if control_down:
                ev = q.CGEventCreateKeyboardEvent(None, 59, False)
                q.CGEventSetFlags(ev, 0)
                q.CGEventPost(q.kCGHIDEventTap, ev)

    def type_text(self, message: str):
        if self.dry_run:
            return
        q = self.quartz
        # Unicode keyboard events; no clipboard access or arbitrary commands.
        for character in message:
            for down in (True, False):
                ev = q.CGEventCreateKeyboardEvent(None, 0, down)
                q.CGEventKeyboardSetUnicodeString(ev, 1, character)
                q.CGEventPost(q.kCGHIDEventTap, ev)
            time.sleep(.006)


class DeferredMacInput:
    """Do not capture a screen or load input frameworks before USB is ready."""
    def __init__(self, session, dry_run):
        self.session = session
        self.dry_run = dry_run
        self.backend = None

    def __getattr__(self, name):
        geometry = self.session.current_geometry
        if geometry is None:
            raise ValueError('Wait for the first image before controlling the Mac')
        size = (geometry.image_width, geometry.image_height)
        if self.backend is None:
            self.backend = MacInput(self.dry_run, size)
        self.backend.display_px = size
        return getattr(self.backend, name)


class ViewSession:
    def __init__(self, screen: Screen):
        self.screen = screen
        self.zoom = ZOOM_DEFAULT
        self.center_x: Optional[float] = None
        self.center_y: Optional[float] = None
        self.current_geometry: Optional[Geometry] = None
        self.old_tiles: dict[tuple[int, int], bytes] = {}
        self.patches_per_frame = MAX_PATCHES
        self.pending: list[tuple[int, int, str, bytes, bytes]] = []
        self.latest_frame_changes = 0
        self.next_tile = 0
        self.batches_sent = 0

    def zoom_by(self, delta: int):
        new_index = max(0, min(len(ZOOM_MODES) - 1, self.zoom + delta))
        if new_index != self.zoom:
            self.zoom = new_index
            self.old_tiles.clear()
            self.pending.clear()

    def pan(self, dx: int, dy: int):
        geo = self.current_geometry
        if geo is None or self.center_x is None or self.center_y is None:
            return
        self.center_x += dx * geo.crop_w / WIDTH
        self.center_y += dy * geo.crop_h / HEIGHT
        self.old_tiles.clear()
        self.pending.clear()

    def render(self) -> tuple[list[tuple[int, int, str, bytes, bytes]], Geometry, bool]:
        """Recheck the desktop at every batch boundary; keep only latest pixels.

        old_tiles contains ACKed pixels actually on the TI, not a past complete
        frame. Rotating the scan start prevents animated top rows starving the
        lower screen. Never interrupt a tile/ACK group already on the wire.
        """
        raw = self.screen.capture()
        if self.center_x is None:
            self.center_x, self.center_y = raw.width / 2, raw.height / 2
        self.center_x = max(0, min(raw.width, self.center_x))
        self.center_y = max(0, min(raw.height, self.center_y))
        output, geometry = crop_view(raw, self.zoom, self.center_x, self.center_y)
        self.current_geometry = geometry
        pixels = rgb332_image(output)
        changed = []
        for offset in range(COLS * ROWS):
            index = (self.next_tile + offset) % (COLS * ROWS)
            tx, ty = index % COLS, index // COLS
            block = tile_at(pixels, tx, ty)
            if self.old_tiles.get((tx, ty)) != block:
                fmt, packed = encode_tile(block)
                changed.append((tx, ty, fmt, packed, block))
        batch = changed[:self.patches_per_frame]
        self.pending = changed[self.patches_per_frame:]
        self.latest_frame_changes = len(changed)
        if batch:
            tx, ty = batch[-1][:2]
            self.next_tile = (ty * COLS + tx + 1) % (COLS * ROWS)
        return batch, geometry, bool(self.pending)

    def ack_tile(self, tx: int, ty: int, block: bytes):
        self.old_tiles[(tx, ty)] = block

    def view_to_point(self, view_x: int, view_y: int) -> Optional[tuple[float, float]]:
        if self.current_geometry is None:
            raise RuntimeError('No screenshot captured yet')
        return self.current_geometry.point_from_view(view_x, view_y)


def write_paced(wire, payload: bytes, pace: float):
    for i in range(0, len(payload), 48):
        wire.write(payload[i:i + 48])
        wire.flush()
        if pace:
            time.sleep(pace)


def send_frame(wire, session: ViewSession, pace: float = .003, log=False):
    started = time.monotonic()
    changes, _, more = session.render()
    session.batches_sent += 1
    batch = session.batches_sent
    if log:
        print(f'Batch {batch}: {len(changes)} tiles, more={int(more)}, '
              f'capture+encode {1000 * (time.monotonic() - started):.0f} ms, '
              f'{len(session.old_tiles)}/{COLS * ROWS} tiles confirmed', flush=True)
    wire.write(f'F|{len(changes)}|{int(more)}\n'.encode('ascii'))
    wire.flush()
    sent = 0
    unconfirmed = []
    for index, (tx, ty, fmt, packet, block) in enumerate(changes):
        wire.write(f'T|{tx}|{ty}|{fmt}|{len(packet)}\n'.encode('ascii'))
        wire.flush()
        write_paced(wire, packet, pace)
        unconfirmed.append((tx, ty, block))
        # The calculator ACKs a GROUP of up to four tiles, not each tile.
        # A short group bounds buffering while removing most round trips.
        if (index + 1) % ACK_GROUP == 0 or index + 1 == len(changes):
            where = (f'batch {batch}, tile {index + 1} of {len(changes)} at ({tx},{ty}); '
                     f'{len(session.old_tiles)}/{COLS * ROWS} tiles confirmed before this group')
            try:
                ack = wire.readline()
            except OSError as exc:  # pyserial's SerialException is an OSError
                raise RuntimeError(
                    f'Calculator USB link vanished while waiting for an ACK ({where}): {exc}. '
                    'MAC84 stopped first; its screen should show the reason '
                    '(v0.3.3+). Note that message before pressing CLEAR.') from exc
            if ack != b'A\n':
                raise RuntimeError(f'ACK failure ({where}): got {ack!r} '
                                   f'(empty means no reply within the 2 s timeout). '
                                   'Check the calculator screen for the reason; restart MAC84.')
            for cx, cy, confirmed in unconfirmed:
                session.ack_tile(cx, cy, confirmed)
            unconfirmed.clear()
        sent += len(packet)
    wire.write(b'E\n')
    wire.flush()
    if log and changes:
        print(f'Batch complete: {sent} bytes', flush=True)


def parse_request(wire, session: ViewSession, input_dev: MacInput,
                  message: str, pace: float, log: bool = False,
                  link_test: bool = False):
    fields = message.split('|')
    command = fields[0]
    if message in ('@M84', '@M84|4'):
        session.old_tiles.clear()
        session.pending.clear()
        session.next_tile = 0
        wire.write(b'@M84READY|4\n' if message.endswith('|4') else b'@M84READY\n')
        wire.flush()
        return
    if command == 'V' and len(fields) == 1:
        if link_test:
            wire.write(b'F|0|0\nE\n')
            wire.flush()
        else:
            send_frame(wire, session, pace, log)
        return
    if command == 'D':
        # v0.3.3 calculator diagnostic, sent only between batches.
        print('[calc] ' + ' '.join(f[:40] for f in fields[1:5]), flush=True)
        return
    if command == 'I' and len(fields) == 1:
        # The calculator editor covered rows 192..239. Resend this strip.
        session.pending.clear()
        for coord in list(session.old_tiles):
            if coord[1] >= 12:
                del session.old_tiles[coord]
        return
    if command == 'Z' and len(fields) == 2:
        session.zoom_by(-1 if fields[1] == '-' else 1)
        return
    if command == 'P' and len(fields) == 3:
        session.pan(int(fields[1]), int(fields[2]))
        return
    if command in ('M', 'C'):
        if command == 'M' and len(fields) == 3:
            x, y = map(int, fields[1:])
            point = session.view_to_point(x, y)
            if point is not None:
                input_dev.move(*point)
            return
        if command == 'C' and len(fields) == 4 and fields[1] in ('L', 'R'):
            x, y = map(int, fields[2:])
            point = session.view_to_point(x, y)
            if point is not None:
                input_dev.click(fields[1], *point)
            return
    if command == 'S' and len(fields) == 2 and fields[1] in ('L', 'R'):
        input_dev.switch_space(fields[1])
        session.pending.clear()
        return
    if command == 'W' and len(fields) == 2:
        input_dev.scroll(int(fields[1]))
        return
    if command == 'K' and len(fields) == 2:
        # Incoming text is ASCII; reject control/escape bytes.
        text = fields[1]
        if len(text) <= 100 and all(32 <= ord(c) <= 126 for c in text):
            input_dev.type_text(text)
        return
    print(f'Ignored malformed request: {message[:100]!r}', flush=True)


def ports():
    from serial.tools import list_ports
    return list(list_ports.comports())


def select_port(override: str) -> str:
    if override != 'auto':
        return override
    found = [p.device for p in ports() if (
        'usbmodem' in p.device.lower() or 'usbserial' in p.device.lower() or
        'texas instruments' in (p.description or '').lower() or p.vid == 0x0451)]
    if len(found) != 1:
        raise RuntimeError('Found ' + str(len(found)) + ' matching USB ports. '
                           'Start MAC84 first; use --list-ports or --port DEVICE.')
    return found[0]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', default='auto')
    p.add_argument('--display', type=int, default=1, help='macOS display number, default 1')
    p.add_argument('--list-ports', action='store_true')
    p.add_argument('--mock-image', type=Path, help='Test a static image instead of live Mac display')
    p.add_argument('--preview', type=Path, help='Save current zoom view as PNG without USB')
    p.add_argument('--pace', type=float, default=.003, help='Delay after every 48 bytes over USB')
    p.add_argument('--verbose', action='store_true')
    p.add_argument('--benchmark', action='store_true', help='Analyze one frame; no USB or input control')
    p.add_argument('--no-mac-settings', action='store_true', help='Do not adjust/restore macOS defaults')
    p.add_argument('--restore-settings', action='store_true', help='Recover settings after a previous crash')
    p.add_argument('--link-test', action='store_true',
                   help='USB handshake and frame headers only; no image or Mac controls')
    a = p.parse_args(argv)
    if a.restore_settings:
        restore_all_settings()
        return 0
    if a.list_ports:
        for port in ports():
            print(port.device, port.description)
        return 0
    screen = Screen(a.mock_image, a.display)
    try:
        session = ViewSession(screen)
        if a.benchmark:
            screenshot = screen.capture()
            view, geo = crop_view(screenshot, ZOOM_DEFAULT, screenshot.width / 2,
                                  screenshot.height / 2)
            pixels = rgb332_image(view)
            packets = [encode_tile(tile_at(pixels, x, y))[1] for y in range(ROWS)
                       for x in range(COLS)]
            payload = sum(map(len, packets))
            estimated_pacing = sum((len(packet) + 47) // 48 for packet in packets) * a.pace
            print(f'Source: {screenshot.width}x{screenshot.height}  Zoom #2: {geo.crop_w}x{geo.crop_h}')
            print(f'Tiles: {len(packets)}  raw 8-bit: {len(pixels):,} bytes')
            print(f'Compressed pixel payload: {payload:,} bytes')
            print(f'Mac write pacing alone: {estimated_pacing:.1f}s; actual USB/hardware can be slower')
            return 0
        if a.preview:
            screenshot = screen.capture()
            view, _ = crop_view(screenshot, ZOOM_DEFAULT, screenshot.width / 2,
                                screenshot.height / 2)
            a.preview.parent.mkdir(parents=True, exist_ok=True)
            view.save(a.preview)
            print('Wrote', a.preview)
            return 0
        try:
            import serial
        except ImportError:
            raise RuntimeError('pyserial missing: pip install pyserial')
        device = DeferredMacInput(session, dry_run=a.mock_image is not None or a.link_test)
        port = select_port(a.port)
        print('Mac84 v0.3.3 bridge on', port, '| v0.3 USB protocol | default zoom 960x720', flush=True)
        print('Do not run Gemini84 bridge at the same time. Ctrl+C to stop.', flush=True)
        # SIGTERM should unwind the settings context, not leave modified preferences.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        with SettingsProcess(not a.no_mac_settings and not a.link_test and a.mock_image is None) as settings, serial.Serial(port, 9600, timeout=2, write_timeout=20) as wire:
            # The calculator might have sent HELLO before the Mac opened the
            # serial port, so send a proactive READY. Also answer later HELLOs.
            # The v0.3 calculator explicitly ignores duplicate READYs.
            # Do NOT reset_input_buffer() here: it can discard a queued HELLO.
            time.sleep(.25)
            wire.write(b'@M84READY\n')
            wire.flush()
            print('[usb] Port open, READY sent; waiting for calculator requests.', flush=True)
            if a.verbose:
                print('<- @M84READY (startup)', flush=True)
            requests = 0
            while True:
                try:
                    raw = wire.readline()
                except (OSError, serial.SerialException) as exc:
                    raise RuntimeError(
                        f'USB serial port disappeared: {exc}. '
                        'Check whether MAC84 exited/reset, and whether the port still exists. '
                        'Restart the calculator program before reconnecting.') from exc
                if not raw:
                    continue
                if not raw.endswith(b'\n'):
                    if a.verbose:
                        print('Incomplete inbound line:', raw[:80], flush=True)
                    continue
                line = raw.decode('ascii', 'replace').strip('\r\n')
                if a.verbose and (line != 'V' or requests % 25 == 0):
                    print('->', line, flush=True)
                try:
                    parse_request(wire, session, device, line, a.pace,
                                  a.verbose and not a.link_test,
                                  link_test=a.link_test)
                    if line == 'V' and len(session.old_tiles) == COLS * ROWS:
                        settings.start()
                    requests += 1
                except (ValueError, RuntimeError) as exc:
                    print('Request error:', exc, file=sys.stderr, flush=True)
                    # Stop rather than allow a mismatched tile stream to corrupt display.
                    if line == 'V':
                        raise
        return 0
    finally:
        screen.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, KeyboardInterrupt, OSError) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print('\nMac84 stopped.')
            sys.exit(0)
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
