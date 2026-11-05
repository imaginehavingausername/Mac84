#!/usr/bin/env python3
"""Mac84 v0.3.6 desktop mirroring USB bridge (independent of Gemini84).

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
BRIDGE_VERSION = '0.3.6'


class CalculatorQuit(Exception):
    """The calculator sent @BYE: the user quit with MODE. Not an error."""


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



def packed_bits(colors: int) -> int:
    return 1 if colors <= 2 else 2 if colors <= 4 else 4


def pack_tile(block: bytes) -> bytes:
    """'Q' tile: [n][n palette bytes][256 indices at 1/2/4 bits, MSB first].

    Antialiased text usually has 3..16 RGB332 colors per tile, where RLE fails
    and raw costs 256 bytes; packed costs 34..145 bytes."""
    palette = sorted(set(block))
    if len(palette) > 16:
        raise ValueError('Packed tiles support at most 16 colors')
    bits = packed_bits(len(palette))
    table = bytearray(256)
    for index, color in enumerate(palette):
        table[color] = index
    idx = block.translate(bytes(table))
    if bits == 4:
        body = bytes((idx[i] << 4) | idx[i + 1] for i in range(0, 256, 2))
    elif bits == 2:
        body = bytes((idx[i] << 6) | (idx[i + 1] << 4) | (idx[i + 2] << 2) | idx[i + 3]
                     for i in range(0, 256, 4))
    else:
        body = bytes(sum(idx[i + k] << (7 - k) for k in range(8)) for i in range(0, 256, 8))
    return bytes((len(palette),)) + bytes(palette) + body


def unpack_tile(packet: bytes) -> bytes:
    """Reference decoder for tests; mirrors the calculator's decode."""
    colors = packet[0]
    if not 1 <= colors <= 16:
        raise ValueError('Malformed packed tile: color count')
    bits = packed_bits(colors)
    if len(packet) != 1 + colors + 32 * bits:
        raise ValueError('Malformed packed tile: length')
    palette, body, out = packet[1:1 + colors], packet[1 + colors:], bytearray()
    for byte in body:
        for shift in range(8 - bits, -1, -bits):
            index = (byte >> shift) & ((1 << bits) - 1)
            if index >= colors:
                raise ValueError('Malformed packed tile: index')
            out.append(palette[index])
    return bytes(out)


def encode_tile(block: bytes, packed: bool = False) -> tuple[str, bytes]:
    """Return smallest reversible representation; photos/text often defeat RLE.

    packed=True only after the calculator advertised support (@M84|5)."""
    encoded = rle_encode(block)
    best = ('R', encoded) if len(encoded) < len(block) else ('P', block)
    if packed:
        colors = len(set(block))
        if colors <= 16 and 1 + colors + 32 * packed_bits(colors) < len(best[1]):
            best = ('Q', pack_tile(block))
    return best


def decode_tile(fmt: str, packet: bytes) -> bytes:
    if fmt == 'P':
        return packet
    if fmt == 'R':
        return rle_decode(packet)
    if fmt == 'Q':
        return unpack_tile(packet)
    raise ValueError(f'Unknown tile format {fmt!r}')

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

    def clamped_point_from_view(self, view_x: int, view_y: int) -> tuple[float, float]:
        """Like point_from_view, but a cursor in a letterbox bar maps to the
        nearest desktop edge (used to choose a zoom centre)."""
        x = min(max(view_x, self.view_x), self.view_x + self.view_w - 1)
        y = min(max(view_y, self.view_y), self.view_y + self.view_h - 1)
        return self.point_from_view(x, y)

    def view_from_point(self, px: float, py: float) -> tuple[int, int]:
        """Inverse of point_from_view: where a desktop pixel appears on the TI."""
        x = (px - self.crop_x) * max(1, self.view_w - 1) / max(1, self.crop_w - 1)
        y = (py - self.crop_y) * max(1, self.view_h - 1) / max(1, self.crop_h - 1)
        x = min(max(round(x), 0), self.view_w - 1)
        y = min(max(round(y), 0), self.view_h - 1)
        return self.view_x + x, self.view_y + y


def layout(width: int, height: int, zoom_index: int, center_x: float,
           center_y: float) -> Geometry:
    """Where the view sits on the desktop, without needing pixels. Cropped
    zooms are clamped so the view never extends past a desktop edge."""
    mode = ZOOM_MODES[zoom_index]
    if mode is None:
        scale = min(WIDTH / width, HEIGHT / height)
        fit_w = max(1, round(width * scale))
        fit_h = max(1, round(height * scale))
        return Geometry(width, height, 0, 0, width, height,
                        (WIDTH - fit_w) // 2, (HEIGHT - fit_h) // 2, fit_w, fit_h)
    wanted_w, wanted_h = mode
    scale = min(1.0, width / wanted_w, height / wanted_h)
    crop_w = max(1, round(wanted_w * scale))
    crop_h = max(1, round(wanted_h * scale))
    crop_x = max(0, min(round(center_x - crop_w / 2), width - crop_w))
    crop_y = max(0, min(round(center_y - crop_h / 2), height - crop_h))
    return Geometry(width, height, crop_x, crop_y, crop_w, crop_h)


def crop_view(image: Image.Image, zoom_index: int, center_x: float,
              center_y: float) -> tuple[Image.Image, Geometry]:
    width, height = image.size
    geo = layout(width, height, zoom_index, center_x, center_y)
    if ZOOM_MODES[zoom_index] is None:
        fitted = image.resize((geo.view_w, geo.view_h), Image.Resampling.LANCZOS)
        shown = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))
        shown.paste(fitted, (geo.view_x, geo.view_y))
        return shown, geo
    shown = image.crop((geo.crop_x, geo.crop_y, geo.crop_x + geo.crop_w, geo.crop_y + geo.crop_h))
    return shown.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS), geo


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
        self.dragging = False
        self.drag_point: Optional[tuple[float, float]] = None
        self._helpers: list[subprocess.Popen] = []

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
        # While drag lock holds the button, apps expect drag events, not moves.
        kind = q.kCGEventLeftMouseDragged if self.dragging else q.kCGEventMouseMoved
        event = q.CGEventCreateMouseEvent(None, kind, point, q.kCGMouseButtonLeft)
        q.CGEventPost(q.kCGHIDEventTap, event)
        if self.dragging:
            self.drag_point = (px, py)

    def drag(self, state: str, px: float, py: float):
        """2ND+TRACE drag lock: 'D' presses and holds the left button, arrows
        drag, TRACE ('U') releases. Used for drag-and-drop and text selection."""
        if state not in ('D', 'U'):
            raise ValueError('Invalid drag state')
        if self.dry_run or (state == 'U' and not self.dragging) or (state == 'D' and self.dragging):
            return
        q = self.quartz
        point = self.to_mac_points(px, py)
        kind = q.kCGEventLeftMouseDown if state == 'D' else q.kCGEventLeftMouseUp
        event = q.CGEventCreateMouseEvent(None, kind, point, q.kCGMouseButtonLeft)
        q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, 1)
        q.CGEventPost(q.kCGHIDEventTap, event)
        self.dragging = state == 'D'
        self.drag_point = (px, py) if self.dragging else None

    def release_drag(self):
        """Never leave the Mac's mouse button stuck down when the bridge stops."""
        if self.dragging and self.drag_point is not None:
            self.drag('U', *self.drag_point)

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

    CONTROL, COMMAND, SHIFT = 59, 55, 56  # left modifier key codes
    ARROWS = (123, 124, 125, 126)
    # B|name requests from the calculator; a fixed whitelist, never arbitrary keys.
    NAMED_KEYS = {
        'RETURN': (36, ()), 'ESC': (53, ()),
        'CUT': (7, (COMMAND,)), 'COPY': (8, (COMMAND,)), 'PASTE': (9, (COMMAND,)),
        'ALL': (0, (COMMAND,)), 'UNDO': (6, (COMMAND,)), 'REDO': (6, (COMMAND, SHIFT)),
    }

    def key_combo(self, key: int, modifiers: tuple[int, ...] = ()):
        """Press key with modifiers held, releasing every modifier even if
        posting fails part way. Uses the same explicit modifier down/up
        sequence as the working v0.3.2 Space switching."""
        if self.dry_run:
            return
        q = self.quartz
        masks = {self.CONTROL: q.kCGEventFlagMaskControl,
                 self.COMMAND: q.kCGEventFlagMaskCommand,
                 self.SHIFT: q.kCGEventFlagMaskShift}
        # Real Mac arrow keys carry the fn + keypad flags, and macOS defines
        # Control+arrow shortcuts (Mission Control, Spaces) WITH fn. Without it
        # v0.3.4's Control+Up reached the app as a plain Up arrow.
        key_flags = (q.kCGEventFlagMaskSecondaryFn | q.kCGEventFlagMaskNumericPad
                     if key in self.ARROWS else 0)
        flags = 0
        held = []
        try:
            for mod in modifiers:
                flags |= masks[mod]
                ev = q.CGEventCreateKeyboardEvent(None, mod, True)
                q.CGEventSetFlags(ev, flags)
                q.CGEventPost(q.kCGHIDEventTap, ev)
                held.append(mod)
            for down in (True, False):
                ev = q.CGEventCreateKeyboardEvent(None, key, down)
                q.CGEventSetFlags(ev, flags | key_flags)
                q.CGEventPost(q.kCGHIDEventTap, ev)
                time.sleep(.015)
        finally:
            for mod in reversed(held):
                flags &= ~masks[mod]
                ev = q.CGEventCreateKeyboardEvent(None, mod, False)
                q.CGEventSetFlags(ev, flags)
                q.CGEventPost(q.kCGHIDEventTap, ev)

    def switch_space(self, direction: str):
        """macOS's standard Control+Left/Right action, including full-screen Spaces."""
        if direction not in ('L', 'R'):
            raise ValueError('Invalid Space direction')
        self.key_combo(123 if direction == 'L' else 124, (self.CONTROL,))

    MISSION_CONTROL = '/System/Applications/Mission Control.app/Contents/MacOS/Mission Control'

    def show_windows(self, app_only: bool = False) -> str:
        """WINDOW: Mission Control. 2ND+WINDOW: App Expose (argument 2).
        Launching Mission Control directly does not depend on keyboard-shortcut
        settings or synthetic modifier flags. Falls back to Control+Up/Down."""
        if self.dry_run:
            return 'dry run'
        self._helpers = [h for h in self._helpers if h.poll() is None]  # reap
        if os.path.exists(self.MISSION_CONTROL):
            self._helpers.append(subprocess.Popen(
                [self.MISSION_CONTROL] + (['2'] if app_only else []),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True))
            return 'App Expose' if app_only else 'Mission Control'
        self.key_combo(125 if app_only else 126, (self.CONTROL,))
        return 'Control+Down' if app_only else 'Control+Up'

    def press_named(self, name: str):
        key, modifiers = self.NAMED_KEYS[name]
        self.key_combo(key, modifiers)

    def double_click(self, px: float, py: float):
        if self.dry_run:
            return
        q = self.quartz
        point = self.to_mac_points(px, py)
        # macOS recognises a double click by click state 1 then 2.
        for state in (1, 2):
            for typ in (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp):
                event = q.CGEventCreateMouseEvent(None, typ, point, q.kCGMouseButtonLeft)
                q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, state)
                q.CGEventPost(q.kCGHIDEventTap, event)
                time.sleep(.02)

    def owner_at(self, point: tuple[float, float]) -> Optional[str]:
        """App owning the frontmost on-screen window under point (Quartz only,
        no AppKit, so Python never becomes a visible app)."""
        q = self.quartz
        windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly,
                                               q.kCGNullWindowID) or ()
        x, y = point
        for info in windows:  # front to back
            bounds = info.get(q.kCGWindowBounds) or {}
            if info.get(q.kCGWindowAlpha, 1) == 0:
                continue
            bx, by = bounds.get('X', 0), bounds.get('Y', 0)
            if bx <= x < bx + bounds.get('Width', 0) and by <= y < by + bounds.get('Height', 0):
                return str(info.get(q.kCGWindowOwnerName) or '')
        return None

    def look_up(self, px: float, py: float) -> str:
        """Three-finger-tap equivalent. Over Finder (windows or desktop icons):
        select the item and press Space for Quick Look; press Y= again to close
        (Escape - harmless if focus has moved on, unlike a second Space).
        Elsewhere: Control+Command+D, Look Up for the word under the pointer,
        which never clicks (so links are not followed)."""
        if self.dry_run:
            return 'dry run'
        point = self.to_mac_points(px, py)
        if getattr(self, '_quick_look_open', False):
            self._quick_look_open = False
            self.key_combo(53)  # Escape closes Quick Look
            return 'closed Quick Look'
        self.move(px, py)
        if self.owner_at(point) == 'Finder':
            self.click('L', px, py)
            time.sleep(.06)
            self.key_combo(49)
            self._quick_look_open = True
            return 'Quick Look (Finder)'
        self.key_combo(2, (self.CONTROL, self.COMMAND))  # kVK_ANSI_D
        return 'Look Up'

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
        self.packed_tiles = False   # enabled only by the calculator's @M84|5
        self.ack_window = 1         # ACK groups in flight; 1 = proven behaviour
        self.synced = True          # last batch emptied the change list
        self.pending_cursor: Optional[tuple[int, int]] = None  # sent before next F|
        self.calculator_version: Optional[str] = None
        self.greeted = False
        self.restart_from_top()

    def restart_from_top(self):
        # A new pass over a mostly changed screen draws top-to-bottom. The
        # rotating start is kept only while a pass is still unfinished, which is
        # what prevents top-row animation from starving the lower screen.
        self.next_tile = 0
        self.synced = True

    def zoom_by(self, delta: int):
        new_index = max(0, min(len(ZOOM_MODES) - 1, self.zoom + delta))
        if new_index != self.zoom:
            self.zoom = new_index
            self.old_tiles.clear()
            self.pending.clear()
            self.restart_from_top()

    def zoom_at(self, delta: int, view_x: int, view_y: int) -> Optional[tuple[float, float]]:
        """v0.3.6 +/-: re-centre on the desktop point under the calculator
        cursor, clamped so the view stays fully on the desktop, and queue the
        cursor's new position so it stays on that point. Returns the point."""
        geo = self.current_geometry
        if geo is None:
            self.zoom_by(delta)
            return None
        new_index = max(0, min(len(ZOOM_MODES) - 1, self.zoom + delta))
        if new_index == self.zoom:
            return None
        px, py = geo.clamped_point_from_view(view_x, view_y)
        new_geo = layout(geo.image_width, geo.image_height, new_index, px, py)
        self.zoom = new_index
        self.center_x = new_geo.crop_x + new_geo.crop_w / 2
        self.center_y = new_geo.crop_y + new_geo.crop_h / 2
        self.current_geometry = new_geo  # input mapping matches the next image
        self.pending_cursor = new_geo.view_from_point(px, py)
        self.old_tiles.clear()
        self.pending.clear()
        self.restart_from_top()
        return px, py

    def pan(self, dx: int, dy: int):
        geo = self.current_geometry
        if geo is None or self.center_x is None or self.center_y is None:
            return
        self.center_x += dx * geo.crop_w / WIDTH
        self.center_y += dy * geo.crop_h / HEIGHT
        self.old_tiles.clear()
        self.pending.clear()
        self.restart_from_top()

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
        if ZOOM_MODES[self.zoom] is not None:
            # Keep the centre at the CLAMPED crop's centre, so panning back from
            # an edge responds immediately instead of first undoing overshoot.
            self.center_x = geometry.crop_x + geometry.crop_w / 2
            self.center_y = geometry.crop_y + geometry.crop_h / 2
        pixels = rgb332_image(output)
        if self.synced:
            # The previous pass finished: start this one at the top instead of
            # wherever the last small update happened to end (v0.3.3 quirk).
            self.next_tile = 0
        changed = []
        for offset in range(COLS * ROWS):
            index = (self.next_tile + offset) % (COLS * ROWS)
            tx, ty = index % COLS, index // COLS
            block = tile_at(pixels, tx, ty)
            if self.old_tiles.get((tx, ty)) != block:
                fmt, packed = encode_tile(block, self.packed_tiles)
                changed.append((tx, ty, fmt, packed, block))
        batch = changed[:self.patches_per_frame]
        self.pending = changed[self.patches_per_frame:]
        self.latest_frame_changes = len(changed)
        self.synced = not self.pending
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


def _read_ack(wire, session: ViewSession, where: str):
    try:
        ack = wire.readline()
    except OSError as exc:  # pyserial's SerialException is an OSError
        raise RuntimeError(
            f'Calculator USB link vanished while waiting for an ACK ({where}): {exc}. '
            'MAC84 stopped first; its screen should show the reason '
            '(v0.3.3+). Note that message before pressing CLEAR.') from exc
    if ack == b'@BYE\n':
        raise CalculatorQuit()
    if ack != b'A\n':
        raise RuntimeError(f'ACK failure ({where}): got {ack!r} '
                           f'(empty means no reply within the 2 s timeout). '
                           'Check the calculator screen for the reason; restart MAC84.')


def send_frame(wire, session: ViewSession, pace: float = .003, log=False):
    """Send one batch. Each ACK group (<= 4 tiles) is written as ONE paced
    stream of headers + payloads, so small tiles share 48-byte chunks and no
    header needs its own flushed write. The byte rate per chunk is unchanged.

    session.ack_window = 1 waits for each group's ACK before sending the next
    (the proven v0.3 behaviour). 2 lets one more group be in flight; the
    calculator protocol is identical either way."""
    started = time.monotonic()
    changes, _, more = session.render()
    session.batches_sent += 1
    batch = session.batches_sent
    rendered = time.monotonic()
    if log:
        kinds = ''.join(sorted(set(c[2] for c in changes)))
        print(f'Batch {batch}: {len(changes)} tiles [{kinds}], more={int(more)}, '
              f'capture+encode {1000 * (rendered - started):.0f} ms, '
              f'{len(session.old_tiles)}/{COLS * ROWS} tiles confirmed', flush=True)
    stream = bytearray()
    if session.pending_cursor is not None:
        # Only queued for v0.3.6+ calculators (they send Z with coordinates).
        stream += 'M|{}|{}\n'.format(*session.pending_cursor).encode('ascii')
        session.pending_cursor = None
    stream += f'F|{len(changes)}|{int(more)}\n'.encode('ascii')
    window = max(1, int(session.ack_window))
    in_flight = []  # [(where, [(tx, ty, block), ...])], oldest first
    group = []
    sent = 0
    for index, (tx, ty, fmt, packet, block) in enumerate(changes):
        stream += f'T|{tx}|{ty}|{fmt}|{len(packet)}\n'.encode('ascii')
        stream += packet
        group.append((tx, ty, block))
        sent += len(packet)
        # The calculator ACKs a GROUP of up to four tiles, not each tile.
        if (index + 1) % ACK_GROUP == 0 or index + 1 == len(changes):
            write_paced(wire, bytes(stream), pace)
            stream.clear()
            in_flight.append((f'batch {batch}, tile {index + 1} of {len(changes)} at ({tx},{ty}); '
                              f'{len(session.old_tiles)}/{COLS * ROWS} tiles confirmed before this group',
                              group))
            group = []
            while len(in_flight) >= window or (in_flight and index + 1 == len(changes)):
                where, confirmed = in_flight.pop(0)
                _read_ack(wire, session, where)
                for cx, cy, block_bytes in confirmed:
                    session.ack_tile(cx, cy, block_bytes)
    stream += b'E\n'
    write_paced(wire, bytes(stream), pace)
    if log and changes:
        elapsed = time.monotonic() - rendered
        print(f'Batch {batch} complete: {sent:,} payload bytes in {1000 * elapsed:.0f} ms '
              f'({sent / max(elapsed, 1e-6) / 1024:.1f} KB/s)', flush=True)


def parse_request(wire, session: ViewSession, input_dev: MacInput,
                  message: str, pace: float, log: bool = False,
                  link_test: bool = False):
    fields = message.split('|')
    command = fields[0]
    if message == '@BYE':
        raise CalculatorQuit()
    if message in ('@M84', '@M84|4') or message.startswith('@M84|5'):
        session.old_tiles.clear()
        session.pending.clear()
        session.pending_cursor = None
        session.restart_from_top()
        # |5 = v0.3.4+ calculator: understands packed 'Q' tiles. Older greetings
        # keep the original raw/RLE tiles, so mixed versions still work.
        session.packed_tiles = message.startswith('@M84|5')
        reply = b'@M84READY|5\n' if session.packed_tiles else (
            b'@M84READY|4\n' if message == '@M84|4' else b'@M84READY\n')
        wire.write(reply)
        wire.flush()
        version = fields[2] if len(fields) > 2 else None  # v0.3.6+ sends it
        if not session.greeted or version != session.calculator_version or log:
            session.greeted = True
            session.calculator_version = version
            shown = version or ('0.3.4/0.3.5' if session.packed_tiles else 'older than 0.3.4')
            print(f'[usb] Calculator program MAC84 v{shown}; packed tiles '
                  f'{"on" if session.packed_tiles else "off"}', flush=True)
            if version != BRIDGE_VERSION:
                print(f'[usb] WARNING: this bridge is v{BRIDGE_VERSION} but the calculator runs '
                      f'MAC84 v{shown}. Send calculator/bin/MAC84.8xp from THIS folder '
                      f'({Path(__file__).resolve().parents[1]}) with TI Connect CE, or newer '
                      'buttons will not work.', flush=True)
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
    if command == 'Z' and len(fields) in (2, 4) and fields[1] in ('+', '-'):
        delta = -1 if fields[1] == '-' else 1
        if len(fields) == 4:  # v0.3.6+: zoom around the calculator cursor
            point = session.zoom_at(delta, int(fields[2]), int(fields[3]))
            if point is not None and input_dev is not None:
                input_dev.move(*point)  # Mac pointer stays on the zoomed spot
        else:
            session.zoom_by(delta)
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
        if command == 'C' and len(fields) == 4 and fields[1] in ('L', 'R', 'D'):
            x, y = map(int, fields[2:])
            point = session.view_to_point(x, y)
            if point is not None:
                if fields[1] == 'D':
                    input_dev.double_click(*point)
                else:
                    input_dev.click(fields[1], *point)
            return
    if command == 'L' and len(fields) == 3:
        # Y=: what a three-finger tap does - Quick Look on Finder items,
        # Look Up (dictionary/data detectors) elsewhere.
        point = session.view_to_point(int(fields[1]), int(fields[2]))
        if point is not None:
            what = input_dev.look_up(*point)
            if log:
                print(f'[input] Y= at pointer: {what}', flush=True)
        return
    if command == 'X' and len(fields) == 2 and fields[1] in ('M', 'A'):
        # WINDOW: Mission Control (all windows). 2ND+WINDOW: App Expose.
        what = input_dev.show_windows(fields[1] == 'A')
        session.pending.clear()
        if log:
            print(f'[input] WINDOW: {what}', flush=True)
        return
    if command == 'G' and len(fields) == 4 and fields[1] in ('D', 'U'):
        # 2ND+TRACE drag lock: D holds the left button, U releases it.
        point = session.view_to_point(int(fields[2]), int(fields[3]))
        if point is None and fields[1] == 'U':
            point = input_dev.drag_point  # release even if the pointer is in a bar
        if point is not None:
            input_dev.drag(fields[1], *point)
        return
    if command == 'B' and len(fields) == 2 and fields[1] in MacInput.NAMED_KEYS:
        # ENTER, 2ND+CLEAR, MATH/APPS/PRGM, 2ND+ALPHA, X,T,O,n (+2ND).
        input_dev.press_named(fields[1])
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
    p.add_argument('--ack-window', type=int, default=1, choices=(1, 2),
                   help='EXPERIMENTAL: tile groups in flight before waiting for an ACK (default 1)')
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
    device = None
    try:
        session = ViewSession(screen)
        session.ack_window = a.ack_window
        if a.benchmark:
            screenshot = screen.capture()
            view, geo = crop_view(screenshot, ZOOM_DEFAULT, screenshot.width / 2,
                                  screenshot.height / 2)
            pixels = rgb332_image(view)
            blocks = [(x, y, tile_at(pixels, x, y)) for y in range(ROWS) for x in range(COLS)]
            print(f'Source: {screenshot.width}x{screenshot.height}  Zoom #2: {geo.crop_w}x{geo.crop_h}')
            print(f'Tiles: {len(blocks)}  raw 8-bit: {len(pixels):,} bytes')
            for label, packed, grouped in (('v0.3.3 (raw/RLE, header per write)', False, False),
                                           ('v0.3.4 (raw/RLE/packed, grouped)', True, True)):
                tiles = [(x, y) + encode_tile(block, packed) for x, y, block in blocks]
                payload = sum(len(t[3]) for t in tiles)
                if grouped:
                    chunks = 0
                    for start in range(0, len(tiles), ACK_GROUP):
                        size = sum(len(f'T|{x}|{y}|{f}|{len(d)}\n') + len(d)
                                   for x, y, f, d in tiles[start:start + ACK_GROUP])
                        chunks += (size + 47) // 48
                else:
                    chunks = sum((len(d) + 47) // 48 for *_, d in tiles)
                kinds = {k: sum(1 for t in tiles if t[2] == k) for k in 'PRQ'}
                print(f'{label}: payload {payload:,} B, {chunks} paced chunks -> '
                      f'{chunks * a.pace:.2f}s of pacing  tiles {kinds}')
            print('Pacing is a lower bound; USB, ACK round trips and calculator drawing add to it.')
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
        print(f'Mac84 v{BRIDGE_VERSION} bridge on', port, '| v0.3 USB protocol (+packed tiles) | default zoom 960x720'
              + (f' | ack window {a.ack_window}' if a.ack_window != 1 else ''), flush=True)
        print(f'Running from {Path(__file__).resolve().parents[1]}', flush=True)
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
                except CalculatorQuit:
                    print('[usb] MAC84 was quit on the calculator (MODE). Bridge stopping normally.',
                          flush=True)
                    return 0
                except (ValueError, RuntimeError) as exc:
                    print('Request error:', exc, file=sys.stderr, flush=True)
                    # Stop rather than allow a mismatched tile stream to corrupt display.
                    if line == 'V':
                        raise
        return 0
    finally:
        backend = getattr(device, 'backend', None) if device is not None else None
        if backend is not None:
            try:
                backend.release_drag()
            except Exception as exc:  # report, but never mask the real error
                print(f'[input] Could not release drag-lock mouse button: {exc}', file=sys.stderr)
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
