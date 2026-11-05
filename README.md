# Mac84

**Control your Mac from a TI-84 Plus CE.** Mac84 mirrors your Mac's screen onto the
calculator over USB. From the keypad you can point, click, drag, scroll, zoom, type,
use shortcuts like copy, paste and undo, open Mission Control, and switch Spaces.

> **This is Version 4 (v0.3.7), the current version**: it starts by itself when you
> plug in the calculator, and Mission Control works.
> **Designed and tested on macOS Sequoia 15.3.1** (English system language) with a
> TI-84 Plus CE Python running OS 5.8.0.022. Other setups are untested.

## How it works

Mac84 has two parts: **MAC84**, a C program for the calculator (`calculator/`), and
**the bridge**, a Python program for the Mac (`mac/`). The bridge captures the screen,
scales part of it to the calculator's 320×240 display in 256 colors, and sends only
the 16×16 tiles that changed. Each tile goes out in whichever encoding is smallest:
raw, run-length, or a packed palette that shrinks text tiles to as little as 34 bytes
instead of 256. The calculator confirms every four tiles before more are sent.
Keypresses travel back and become real mouse and keyboard events. Everything stays on
the USB cable; nothing goes over the network.

## What you need

- A **TI-84 Plus CE** and a USB cable that carries data (the one that came with the
  calculator works; USB-C-only Macs need an adapter).
- A **Mac running macOS Sequoia** (tested on 15.3.1).

| Software | Where to get it | Why |
| --- | --- | --- |
| TI Connect CE | [education.ti.com](https://education.ti.com/en/products/computer-software/ti-connect-ce-sw) | Sends files to the calculator |
| arTIfiCE | [yvantt.github.io/arTIfiCE](https://yvantt.github.io/arTIfiCE/) | Lets calculator OS 5.5 and later run native programs like MAC84 |
| CE C libraries (`clibs.8xg`) | [CE-Programming/libraries releases](https://github.com/CE-Programming/libraries/releases/latest) | Runtime libraries MAC84 needs (GRAPHX, KEYPADC, USBDRVCE, SRLDRVCE) |
| Python 3.9 or newer | Comes with Xcode Command Line Tools (`xcode-select --install`) | Runs the bridge |
| CEdev toolchain *(optional)* | [CE-Programming/toolchain releases](https://github.com/CE-Programming/toolchain/releases) | Only needed to rebuild `MAC84.8xp` yourself |

## Setup

### 1. Prepare the calculator

1. Install TI Connect CE and connect the calculator.
2. Send it `clibs.8xg` from the
   [CE libraries release page](https://github.com/CE-Programming/libraries/releases/latest).
3. Install arTIfiCE for your calculator OS (check with `2nd` → `mem` → `About`), following
   [its page](https://yvantt.github.io/arTIfiCE/):
   - **OS 5.8.4 or earlier** (including the tested 5.8.0.022): arTIfiCE v2.1. Send
     `arTIfiCE_v2.1.8xp`, then open the `prgm` menu, choose `A`, and press `enter`.
   - **OS 5.8.5:** arTIfiCE v3, which has its own install steps.
   - **OS 5.4 or earlier:** not needed.
4. Send `calculator/bin/MAC84.8xp` from this repository. It is already compiled.
5. **Quit TI Connect CE**, which otherwise holds the calculator's USB connection.

> Native programs can crash or reset the calculator. Back up anything important first.

### 2. Install the bridge on the Mac

```sh
git clone https://github.com/imaginehavingausername/Mac84.git
cd Mac84
zsh mac/install_autostart.command
```

This sets up the Python environment and turns on [auto-start](#auto-start). To start
the bridge only by hand instead, run
`python3 -m venv .venv && ./.venv/bin/python -m pip install -r mac/requirements.txt`.

### 3. Grant macOS permissions

In **System Settings → Privacy & Security**, allow Terminal (or whichever terminal app
you use) under:

- **Screen & System Audio Recording**, to capture the screen
- **Accessibility**, to move the pointer, click, type and operate System Settings
- **Automation → System Events**, to flip switches in System Settings (macOS asks on
  first run)

Then quit and reopen Terminal. If macOS later asks whether something may control
Terminal, allow it; that is auto-start opening and closing its window.

### 4. Run it

1. Plug in the calculator. With auto-start on, a Terminal window opens and waits.
2. On the calculator, start arTIfiCE and choose **MAC84**. The screen shows
   `MAC84 v0.3.6` (the calculator program did not change in v0.3.7) and
   `Waiting for Mac bridge...`.
3. Without auto-start, start the bridge within 80 seconds from the `Mac84` folder (or
   double-click `mac/run_mac.command`):

   ```sh
   ./.venv/bin/python mac/mac84_bridge.py --port auto --pace 0.003
   ```

4. Terminal shows `[usb] Calculator program MAC84 v0.3.6; packed tiles on`, and the
   screen fills in over a few seconds. After that, only changed areas are sent.

**To stop,** press `mode` on the calculator; the bridge restores your Mac settings. With
auto-start, the window stays open and reconnects if you start MAC84 again; unplug the
calculator to close it. Without auto-start, the bridge exits. (`Ctrl+C` also works;
wait for the `[settings]` lines to finish.)

## Controls

| Key | Action |
| --- | --- |
| Arrows / `2nd` + arrows | Move the pointer / pan the view |
| `trace` / `graph` / `zoom` | Left click / right click / double-click |
| `2nd` + `trace` | Drag lock: holds the left button (square cursor); arrows drag, `trace` drops |
| `+` / `−` | Zoom in / out around the pointer |
| `^` / `÷` | Scroll up / down |
| `y=` | Quick Look on a Finder item (press again to close); Look Up the word under the pointer elsewhere |
| `window` / `2nd` + `window` | Mission Control, like F3 (`trace` a window to switch to it, `window` again to close) / App Exposé |
| `vars` / `clear` | Switch to the Space on the left / right |
| `enter` / `2nd` + `clear` | Return / Escape |
| `math` / `apps` / `prgm` | Cut / Copy / Paste |
| `x,t,θ,n` / `2nd` + `x,t,θ,n` | Undo / Redo |
| `2nd` + `alpha` | Select all |
| `alpha` | Open the text editor |
| `mode` | Quit (`2nd` then `mode` also quits from inside the editor) |

Zoom has five levels, from the whole desktop down to a 320×240 area; the default shows
960×720. The view always stays on the desktop, and the pointer stays on the same spot
as you zoom. Where the desktop's shape doesn't fill the calculator screen, black bars
appear, and pointer moves and clicks there are ignored.

### Typing text

Press `alpha` to open the editor, type up to 100 characters, then press `enter` to type
them into the focused field on the Mac (no Return is sent).

| Key | In the editor |
| --- | --- |
| `enter` / `mode` | Type the text on the Mac / cancel |
| `del` / `clear` | Backspace / clear all |
| Left / Right | Move the caret |
| `2nd` + `alpha` / `alpha` | Next mode (abc → ABC → 123 → symbols) / back to abc |
| `2nd` + `0` | Space, in any mode |

In letter modes, each key types the green letter printed above it. The full chart:

| Keys | abc / ABC | 123 | symbols |
| --- | --- | --- | --- |
| `math` `apps` `prgm` | a b c | – | – |
| `x⁻¹` `sin` `cos` `tan` `^` | d e f g h | – – – – ^ | > – – – ~ |
| `x²` `,` `(` `)` `÷` | i j k l m | – , ( ) / | < \| [ ] \\ |
| `log` `7` `8` `9` `×` | n o p q r | – 7 8 9 * | { ' " ; $ |
| `ln` `4` `5` `6` `−` | s t u v w | – 4 5 6 - | } @ # = % |
| `sto→` `1` `2` `3` `+` | x y z – " | – 1 2 3 + | \` ! ? : & |
| `0` `.` `(−)` | space : ? | 0 . - | space . _ |

## Temporary Mac settings

A ticking clock, animations and a blinking cursor keep changing the screen, and every
change costs USB time. So while the bridge runs, it hides the menu bar clock's seconds
and flashing separators, and turns on **Reduce motion** and **Prefer non-blinking
cursor**. System Settings opens briefly to flip Reduce motion; switch back to your app
afterwards. This starts after the first full screen, and mirroring continues meanwhile.

Your original values are saved to
`~/Library/Application Support/Mac84/previous_settings.json` and restored when the
bridge stops, or on the next run if it was interrupted. If a restore fails, the file is
kept. Don't delete it; with the bridge stopped, run:

```sh
./.venv/bin/python mac/mac84_bridge.py --restore-settings
```

To leave your settings alone entirely, add `--no-mac-settings`.

## Auto-start

`mac/install_autostart.command` installs a small background watcher (LaunchAgent
`com.local.mac84watcher`) that checks USB every 2 seconds. When the calculator appears,
it opens Terminal running the bridge in `--auto` mode, which waits for MAC84 and
reconnects each time you start it. About 8 seconds after you unplug, the bridge
finishes restoring settings and exits, and the watcher closes the window. The Mac can't
start MAC84 for you; always launch it from arTIfiCE.

- **Moved the Mac84 folder?** Run the installer again from the new location.
- **Turn it off:** `zsh mac/uninstall_autostart.command`
- **Logs:** `~/Library/Application Support/Mac84/autostart.log` (watcher) and
  `~/Library/Logs/Mac84/last_auto_session.log` (the last window's output).

## Command-line options

| Option | Effect |
| --- | --- |
| `--port auto` / `--port /dev/cu.usbmodem...` | Find the calculator automatically (default) / use a specific port |
| `--list-ports` | List candidate serial ports and exit |
| `--auto` | Wait for MAC84, reconnect after each session, exit when unplugged (used by auto-start) |
| `--verbose` | Log every request, plus each batch's tile kinds, timing and KB/s |
| `--no-mac-settings` | Don't change or restore any Mac settings |
| `--restore-settings` | Restore settings saved by an interrupted run, then exit |
| `--pace 0.003` | Delay in seconds after every 48 bytes sent over USB |
| `--ack-window 2` | *Experimental:* one more tile group in flight before waiting (default 1). If mirroring stops, leave it off |

For testing without the full setup: `--link-test` checks only the USB link,
`--preview FILE.png` saves what the calculator would show, `--benchmark` reports
compression for one frame, `--mock-image FILE` sends a static image, and `--display N`
captures another display.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Calculator says `No bridge after 80 sec` | Start the bridge sooner after launching MAC84 |
| Bridge can't find the calculator | Quit TI Connect CE, use a data cable, and try `--list-ports` |
| No window opens when you plug in | Check `autostart.log` (see [Auto-start](#auto-start)); run the installer again |
| Bridge can't lock the serial port | Another bridge already has the calculator. Only one runs at a time, so don't start one by hand while the auto-start window is open |
| Terminal shows a `WARNING` about the calculator version | The calculator has an older MAC84; send `calculator/bin/MAC84.8xp` again |
| Calculator shows a `MAC84 v0.3.6 STOPPED` panel | It lists the reason and the USB stage. Note it, then press `clear` |
| Image is blank, or the pointer doesn't move on the Mac | Screen Recording or Accessibility permission for Terminal |
| Terminal output makes the image busy | Keep Terminal outside the captured area |

With auto-start, the last session's output is in `last_auto_session.log`. To capture a
detailed log by hand for an issue report:

```sh
./.venv/bin/python mac/mac84_bridge.py --port auto --pace 0.003 --verbose 2>&1 | tee ~/Desktop/mac84_run.log
```

## For developers

Run the 103 tests (they use mocks and never change your real settings; the C tests build
with clang from Xcode Command Line Tools):

```sh
./.venv/bin/python -m unittest discover -s tests
```

The included `MAC84.8xp` was built from this source with CEdev v15.0. To rebuild it,
install [CEdev](https://ce-programming.github.io/toolchain/static/getting-started.html),
then:

```sh
export PATH="/path/to/CEdev/bin:$PATH"
cd calculator && make clean && make
```

## Acknowledgments

- [CE C/C++ Toolchain and libraries](https://github.com/CE-Programming/toolchain) by
  the CE-Programming team, which provide the USB serial driver MAC84 is built on.
- [arTIfiCE](https://yvantt.github.io/arTIfiCE/) by YvanTT, which makes it possible to
  run native programs on current calculator OS versions.

## License

MIT. See [LICENSE](LICENSE).
