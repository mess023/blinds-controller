# Blinds Controller

A desktop app (Python + Tkinter) that drives four motorised window blinds over
**Art-Net** and can animate them in time with music via **Ableton Link** or
**live audio BPM detection**.

It is the software counterpart to the [`ESP32blinds`](../ESP32blinds) firmware
that runs on each window. This README covers getting *this app* running on a
fresh computer. For the full end-to-end install (firmware + wiring + network +
console integration) see **[SYSTEM_SETUP.md](SYSTEM_SETUP.md)**.

---

## What it does

- Sends one **Art-Net universe** (UDP 6454, universe 1) containing all four
  windows. Each window is a 4-channel 16-bit fixture:
  - Window 1 → DMX channels 1–4 &nbsp;&nbsp; Window 2 → 5–8
  - Window 3 → 9–12 &nbsp;&nbsp; Window 4 → 13–16
  - Within a window: channels 1–2 = bottom blind, 3–4 = top blind (16-bit, coarse+fine).
- **Manual control** – per-window bottom/top sliders.
- **Gap Position** and **Gap Size**, each with its own beat-synced oscillation
  (¼ … 64 beats) and its own spatial pattern (Wave →, Wave ←, Counter, …).
- **BPM sources**: internal clock, **Tap Tempo**, **Ableton Link**, or **live
  audio** (mic / line-in / Windows loopback). Resync + ±1/16-beat Nudge.
- **Broadcast button** to spray the universe to the whole subnet (handy for
  testing or when device IPs are unknown).

The app runs with nothing but Python + Tkinter installed; the audio and Link
features are optional extras.

---

## 1. Install Python (fresh system)

You need **Python 3.8 or newer** (developed on 3.14). Tkinter must be available.

### Windows
1. Download the installer from <https://www.python.org/downloads/windows/>.
2. Run it and **tick “Add python.exe to PATH”** on the first screen.
3. Keep the default options — the **“tcl/tk and IDLE”** feature (Tkinter) is
   included by default. Finish the install.
4. Verify in a new PowerShell / Command Prompt window:
   ```powershell
   python --version
   python -m tkinter      # a small test window should pop up
   ```

### macOS
The system Python does **not** ship a working Tkinter — install from python.org:
1. Download the macOS installer from <https://www.python.org/downloads/macos/>
   and run it (this bundles the correct Tcl/Tk).
2. Verify in Terminal:
   ```bash
   python3 --version
   python3 -m tkinter     # a small test window should pop up
   ```
   *(Homebrew alternative: `brew install python-tk` alongside `brew install python`.)*

### Linux (Debian / Ubuntu)
Tkinter is a separate package:
```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv python3-tk
python3 --version
python3 -m tkinter        # a small test window should pop up
```
Fedora: `sudo dnf install python3 python3-pip python3-tkinter`
Arch:   `sudo pacman -S python python-pip tk`

---

## 2. Get the code

```bash
git clone <this-repo-url> blinds-controller
cd blinds-controller
```
(or download and unzip it, then `cd` into the folder.)

---

## 3. Create a virtual environment (recommended)

Keeps dependencies isolated from system Python.

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
If activation is blocked, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

---

## 4. Install dependencies

The app works with **zero** dependencies (Art-Net only). To enable audio BPM
and Ableton Link:

```bash
pip install -r requirements.txt
```

What each package unlocks:

| Package          | Feature                                              | Platforms |
|------------------|------------------------------------------------------|-----------|
| `numpy`          | required by the audio engine                          | all       |
| `sounddevice`    | capture from microphone / line-in                     | all       |
| `aubio`          | accurate beat/tempo detection (else numpy fallback)   | all       |
| `aalink`         | sync to Ableton Link (Traktor, Ableton Live, …)       | all       |
| `pyaudiowpatch`  | capture the PC’s own audio (WASAPI loopback)          | Windows   |

Notes & troubleshooting:
- **aubio** sometimes lacks a prebuilt wheel for the very newest Python. If
  `pip install aubio` fails, the app still runs and falls back to a built-in
  numpy onset detector (lower confidence). You can also try a slightly older
  Python (3.11/3.12) which has wheels.
- **aalink** needs a C++ toolchain only if no wheel exists for your platform
  (Linux/macOS: install build tools; Windows wheels are usually available).
- **Linux audio**: `sounddevice` needs PortAudio — `sudo apt install libportaudio2`.
- **macOS audio**: the first time you start audio, grant the microphone
  permission prompt. Capturing *system* audio on macOS isn’t built in (use a
  virtual device like BlackHole and select it as the input).

---

## 5. Run

```bash
python blinds_controller.py          # Windows
python3 blinds_controller.py         # macOS / Linux
```

A window titled **“Blinds Controller”** opens. A log file
`blinds_controller.log` is written next to the script.

---

## 6. Point it at your windows

Out of the box the app targets `10.0.0.101–104` (one IP per window). Two ways
to change this:

- **Per window**: type each window’s IP into the IP field on its card.
- **Editing defaults**: change the `FRAMES` list near the top of
  `blinds_controller.py` (also where the **DMX address** per window is defined —
  must match each ESP’s configuration).
- **Broadcast**: click **“→ Broadcast”** to send to the current subnet’s
  broadcast address (e.g. `192.168.1.255`). Every window on the LAN receives the
  universe and reads its own channels. Click **“✕ Restore IPs”** to go back.

Your computer must have an IP address on the same subnet as the windows
(see [SYSTEM_SETUP.md](SYSTEM_SETUP.md) → Network).

---

## 7. Using the UI

- **Frame cards** – per-window bottom/top sliders + editable IP.
- **GAP POSITION** – where the open band sits (0 % = bottom, 100 % = top).
  *Amount* slider sets the static position; *Beats* makes it travel up/down on
  the beat; *Pattern* staggers windows (e.g. Wave → = each window 1 beat later).
- **GAP SIZE** – how far the blinds open. Same Amount / Beats / Pattern controls,
  independent from position.
- **BPM SYNC** – tick **Enable** to start the animation. Choose the source:
  internal clock + Tap Tempo, or tick **Ableton Link**. **⟳ Resync** re-homes
  the animation to beat 0; **◀ ▶** nudge the phase by 1/16 beat to compensate
  for motor latency. **Speed** scales all timing; **Motor** caps how fast the
  blinds may move (protects slow steppers).
- **AUDIO BPM** – pick an input device and **Start Audio** to detect tempo from
  live sound; the detected BPM feeds the clock.

> Before any window will move from Art-Net, that window must be **calibrated and
> homed once** via its own web UI — see [SYSTEM_SETUP.md](SYSTEM_SETUP.md).

---

## Repo contents

```
blinds_controller.py     the application (single file)
requirements.txt         optional dependencies
assets/                  reference images (APC40 layout planning, etc.)
README.md                this file
SYSTEM_SETUP.md          full system: firmware + wiring + network + console
```
