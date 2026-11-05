# Mac84

**Control your Mac from a TI-84 Plus CE.** Mac84 mirrors your Mac's screen onto the
calculator over USB. From the calculator keypad you can move the pointer, click,
scroll, zoom and pan, type text, and switch Spaces.

> **This is Version 1 (v0.3.3)**: the first build that mirrored reliably on real
> hardware.
> **Designed and tested on macOS Sequoia 15.3.1** (English system language) with a
> TI-84 Plus CE Python. Other macOS versions and languages are untested.

## How it works

Mac84 has two parts:

- **`MAC84`**, a native C program for the calculator (`calculator/`), built with the
  CE C toolchain and started through arTIfiCE.
- **The bridge**, a Python program for the Mac (`mac/mac84_bridge.py`).

The bridge captures the main display, crops a 960×720 region (adjustable with zoom),
scales it to the calculator's 320×240 screen and converts it to 256 colors (RGB332).
It sends only the 16×16 tiles that changed, either raw or run-length encoded, over
USB serial. The calculator acknowledges every four tiles and asks for the next batch.
Keypresses travel the other way and become real mouse and keyboard events on the Mac.

Everything stays on the USB cable. The bridge opens no network connections and
uploads nothing.

## What you need

**Hardware**

- A TI-84 Plus CE (Mac84 was developed on the Python edition).
- A USB cable that carries data. The one that came with the calculator works; a Mac
  with only USB-C ports needs an adapter.
- A Mac running macOS Sequoia (tested on 15.3.1).

**Software**

| What | Where to get it | Why |
| --- | --- | --- |
| TI Connect CE | [education.ti.com](https://education.ti.com/en/products/computer-software/ti-connect-ce-sw) | Sends files to the calculator |
| arTIfiCE | [yvantt.github.io/arTIfiCE](https://yvantt.github.io/arTIfiCE/) | Lets calculator OS 5.5 and later run native programs like MAC84 |
| CE C libraries (`clibs.8xg`) | [CE-Programming/libraries releases](https://github.com/CE-Programming/libraries/releases/latest) | Runtime libraries MAC84 needs (GRAPHX, KEYPADC, USBDRVCE, SRLDRVCE) |
| Python 3.9 or newer | Comes with Xcode Command Line Tools (`xcode-select --install`) | Runs the bridge |
| CEdev toolchain *(optional)* | [CE-Programming/toolchain releases](https://github.com/CE-Programming/toolchain/releases) | Only needed to rebuild `MAC84.8xp` yourself |

## Setup

### 1. Prepare the calculator

1. Install TI Connect CE and connect the calculator.
2. Download `clibs.8xg` from the
   [CE libraries release page](https://github.com/CE-Programming/libraries/releases/latest)
   and send it to the calculator.
3. Install arTIfiCE by following the instructions on
   [its page](https://yvantt.github.io/arTIfiCE/). Pick the version that matches your
   calculator OS (check with `2nd` → `MEM` → `About`):
   - **OS 5.8.4 or earlier:** arTIfiCE v2.1. Send `arTIfiCE_v2.1.8xp`, then open the
     `prgm` menu, choose `A`, and press `enter`.
   - **OS 5.8.5:** arTIfiCE v3, which has its own install steps on the same page.
   - **OS 5.4 or earlier:** arTIfiCE is not needed.
4. Send `calculator/bin/MAC84.8xp` from this repository to the calculator. It is
   already compiled, so you do not need CEdev.
5. **Quit TI Connect CE.** While it is open, it holds the calculator's USB connection.

> Native programs can crash or reset the calculator. Back up anything important first.

### 2. Install the bridge on the Mac

```sh
git clone https://github.com/imaginehavingausername/Mac84.git
cd Mac84
python3 -m venv .venv
./.venv/bin/python -m pip install -r mac/requirements.txt
```

This installs Pillow (image processing), pyserial (USB serial) and PyObjC's Quartz
bindings (screen and input events) into a local `.venv` folder.

### 3. Grant macOS permissions

The bridge runs inside Terminal, so Terminal needs these permissions. Open
**System Settings → Privacy & Security** and allow Terminal under:

| Permission | Used for |
| --- | --- |
| **Screen & System Audio Recording** | Capturing the screen |
| **Accessibility** | Moving the pointer, clicking, typing, and flipping accessibility switches |
| **Automation → System Events** | Operating switches in System Settings (macOS asks on first run) |

Quit and reopen Terminal after granting them. If you use another terminal app, grant
the permissions to that app instead.

### 4. Run it

1. Plug the calculator into the Mac.
2. On the calculator, start arTIfiCE and choose **MAC84**. The screen shows
   `MAC84 v0.3.3 - DIAGNOSTIC STOPS` and `Waiting for Mac bridge...`.
3. Within 80 seconds, start the bridge from the `Mac84` folder:

   ```sh
   ./.venv/bin/python mac/mac84_bridge.py --port auto --pace 0.003
   ```

   You can also double-click `mac/run_mac.command`, which runs the same command.

4. Terminal prints `Mac84 v0.3.3 bridge on /dev/cu.usbmodem...`. The first full screen
   arrives in about ten batches; after that, only changed areas are sent.

**To stop:** press `MODE` on the calculator. In this version the bridge then reports
`USB serial port disappeared`, which is expected after a MODE quit. It restores your
Mac settings before it exits. You can also press `Ctrl+C` in Terminal; wait for the
`[settings]` restore lines before closing the window.

## Controls

| Key | Action |
| --- | --- |
| Arrows | Move the pointer |
| `2nd` + arrows | Pan the view |
| `trace` / `graph` | Left / right click |
| `+` / `−` | Zoom in / out (fully zoomed out fits the whole desktop) |
| `^` / `÷` | Scroll up / down |
| `vars` / `clear` | Switch to the Space on the left / right |
| `alpha` | Open the text editor |
| `mode` | Quit MAC84 |
| `2nd`, then `mode` | Quit from anywhere, including the editor |

There are five zoom levels: the whole desktop, 960×720 (the default), 640×480,
480×360, and 320×240. When the desktop's shape
does not match the calculator screen, black bars fill the gap; pointer moves and
clicks in the bars are ignored.

### Typing text

Press `alpha` to open the editor at the bottom of the screen. Type up to 100
characters, then press `enter` to type them into the focused field on the Mac
(no Return key is sent).

| Key | Editor action |
| --- | --- |
| `enter` | Type the text on the Mac and close the editor |
| `mode` | Cancel |
| `del` / `clear` | Backspace / clear all |
| Left / Right | Move the caret |
| `2nd` + `alpha` | Next mode: letters → numbers → symbols |
| `alpha` | Back to letters |
| `2nd` + `0` | Space |

**Letters mode.** Letters `a` to `n` match the green legends printed on the keys.
From `o` onward this version uses its own order, which a later version corrects:

| Keys | Letters |
| --- | --- |
| `math` `apps` `prgm` | a b c |
| `x⁻¹` `sin` `cos` `tan` `^` | d e f g h |
| `x²` `,` `(` `)` `÷` | i j k l m |
| `log` `ln` `sto→` | n o p |
| `7` `8` `9` | q r s |
| `4` `5` `6` | t u v |
| `1` `2` `3` | w x y |
| `0` | z |

**Numbers mode.** The digit keys and `.`.

**Symbols mode.**

| Keys | Symbols |
| --- | --- |
| `+` `−` `×` `÷` `^` | + - * / ^ |
| `.` `,` `(` `)` | . , ( ) |
| `1` `2` `3` | ! ? : |
| `4` `5` `6` | @ # = |
| `7` `8` | ' " |
| `0` | space |

## Temporary Mac settings

Animations, a ticking clock, and a blinking text cursor change the screen constantly,
and every change has to be sent over USB. To reduce this, the bridge changes a few
settings while it runs and puts them back when it stops:

- **Menu bar clock:** hides seconds and stops the flashing time separators.
- **Accessibility → Display:** turns on **Reduce motion** and **Prefer non-blinking
  cursor**. These are set by clicking the real switches in System Settings, which is
  why that window opens. Switch back to the app you want afterwards.

The settings helper starts only after the first full screen has arrived, and the
mirroring continues while it works. Your original values are saved in
`~/Library/Application Support/Mac84/previous_settings.json` before anything changes.
If the bridge is interrupted, the next run restores them first. If a restore fails,
the file is kept. Do not delete it; with the bridge stopped, run:

```sh
./.venv/bin/python mac/mac84_bridge.py --restore-settings
```

To mirror without touching any settings, add `--no-mac-settings`.

## Command-line options

| Option | Effect |
| --- | --- |
| `--port auto` | Find the calculator's serial port automatically (default) |
| `--port /dev/cu.usbmodem...` | Use a specific port |
| `--list-ports` | List candidate serial ports and exit |
| `--pace 0.003` | Delay in seconds after every 48 bytes sent over USB |
| `--verbose` | Log every request, batch and timing |
| `--no-mac-settings` | Do not change or restore any Mac settings |
| `--restore-settings` | Restore settings saved by an interrupted run, then exit |
| `--link-test` | Test the USB handshake and acknowledgments without capture or input |
| `--preview FILE.png` | Save what the calculator would show, without USB |
| `--benchmark` | Analyze compression for one frame, without USB or input |
| `--mock-image FILE` | Send a static image instead of the live screen |
| `--display N` | Capture macOS display number N (default 1) |

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Calculator says `No bridge after 80 sec` | Start the bridge sooner after launching MAC84 |
| Bridge cannot find the calculator | Quit TI Connect CE, use a data-capable cable, and run `--list-ports` |
| Calculator shows a `MAC84 v0.3.3 STOPPED` panel | It lists the reason and the USB stage. Note it, then press `clear` |
| Image is blank or capture fails | Screen & System Audio Recording permission for Terminal |
| Pointer moves on the calculator but not on the Mac | Accessibility permission for Terminal |
| `Could not find Reduce motion...` | Automation permission, and an English system language. Mirroring continues anyway; `--no-mac-settings` skips this step |
| Terminal output makes the image busy | Keep Terminal outside the captured area |

For a detailed log to attach to an issue:

```sh
./.venv/bin/python mac/mac84_bridge.py --port auto --pace 0.003 --verbose 2>&1 | tee ~/Desktop/mac84_run.log
```

## Running the tests

```sh
./.venv/bin/python -m unittest discover -s tests
```

The 49 tests use mocks and never change your real Mac settings. They include a pipe
test that connects the real Python sender to the real C frame parser, compiled for the
Mac with clang (from Xcode Command Line Tools). These are host simulations, not a
calculator emulator.

## Rebuilding the calculator program (optional)

The included `MAC84.8xp` was built from this source with CEdev v15.0. To build it
yourself, install [CEdev](https://github.com/CE-Programming/toolchain/releases)
([getting started](https://ce-programming.github.io/toolchain/static/getting-started.html)),
then run:

```sh
export PATH="/path/to/CEdev/bin:$PATH"
cd calculator
make clean
make
```

The result is `calculator/bin/MAC84.8xp`.

## Acknowledgments

- [CE C/C++ Toolchain and libraries](https://github.com/CE-Programming/toolchain)
  by the CE-Programming team, which provide the USB serial driver MAC84 is built on.
- [arTIfiCE](https://yvantt.github.io/arTIfiCE/) by YvanTT, which makes it possible
  to run native programs on current calculator OS versions.
