# Window Blinds — Full System Setup

End-to-end guide for the motorised window-blinds installation at the cocktail
bar. It covers **both** repositories and the hardware that ties them together:

- **[ESP32blinds](../ESP32blinds)** — firmware on each window (one ESP32 per
  window, two blinds per window).
- **[blinds-controller](.)** — the desktop app that animates the blinds to music.

The system is designed to run **with the desktop controller** *or* **without it**
(driven by a lighting console such as MagicQ over Art-Net, or by any OSC source).
Both control paths are always available on the firmware.

> If you only need one half, the per-repo READMEs are quicker:
> [firmware README](../ESP32blinds/README.md) · [controller README](README.md).

---

## 0. System overview

```
                       Ethernet switch (one flat LAN, e.g. 10.0.0.0/24)
                                     │
        ┌───────────┬───────────┬────┴──────┬───────────────┬──────────────┐
        │           │           │           │               │              │
   window1     window2     window3     window4        Controller PC    (optional)
  10.0.0.101  10.0.0.102  10.0.0.103  10.0.0.104       10.0.0.50      Lighting console
   DMX 1-4     DMX 5-8     DMX 9-12    DMX 13-16                       (MagicQ etc.)
   2 blinds    2 blinds    2 blinds    2 blinds
```

- Each window = a **WT32-ETH01** ESP32 with **wired Ethernet**, driving a
  **bottom** and a **top** stepper blind.
- All windows listen on **one Art-Net universe (1)**; each reads its own
  4 channels (16-bit bottom + 16-bit top).
- Control comes from **either** the desktop controller **or** a console/OSC
  source — or you can mix (e.g. console for shows, controller for testing).

---

## 1. Hardware you need

### Per window (×4)
| Item | Notes |
|------|-------|
| **WT32-ETH01** ESP32 board | ESP32 + LAN8720 Ethernet (RJ45). Wired networking, no Wi-Fi. |
| **2 × stepper motors** | bottom screen + top screen |
| **2 × stepper drivers** (step/dir) | e.g. DM542 / TB6600 / TMC. Microstepping set to taste. |
| **4 × limit switches** | one start + one end endstop per screen (used for calibration/homing) |
| **Motor PSU** | matched to your drivers/motors (commonly 24–48 V) |
| **5 V supply** for the ESP32 | feeds the board’s 5V pin |

### Shared
| Item | Notes |
|------|-------|
| **Ethernet switch** | enough ports for 4 windows + computer (+ console) |
| **Ethernet cables** | one per device |
| **USB-to-TTL serial adapter (3.3 V logic)** | CP2102 / FT232 / CH340 — only for the **first** flash of each board |
| **Controller PC** *(optional)* | Windows / macOS / Linux, runs the desktop app |
| **Lighting console / Art-Net node** *(optional)* | e.g. MagicQ — alternative to the PC |

### Wiring per board (defaults — see firmware README for the pin table)
- Stepper 0 = **bottom** screen → STEP `GPIO32`, DIR `GPIO33`
- Stepper 1 = **top** screen → STEP `GPIO15`, DIR `GPIO14`
- Limit switches → `GPIO2` (bottom start), `GPIO4` (bottom end),
  `GPIO17` (top start), `GPIO12` (top end)
- Each STEP/DIR pair goes to one stepper driver’s step/dir inputs; the driver
  powers the motor from the motor PSU. Share a common ground between the ESP32
  and the drivers.

---

## 2. Network setup

Everything lives on **one flat LAN/subnet**. Defaults baked into the project:

| Device | IP | Notes |
|--------|----|-------|
| window1 | `10.0.0.101` | DMX 1–4 |
| window2 | `10.0.0.102` | DMX 5–8 |
| window3 | `10.0.0.103` | DMX 9–12 |
| window4 | `10.0.0.104` | DMX 13–16 |
| gateway | `10.0.0.1` | router/switch (only needed for internet, not for control) |
| subnet  | `255.255.255.0` | |
| Controller PC / console | any free `10.0.0.x` (e.g. `10.0.0.50`) | must be on this subnet |

The four window IPs are set in each board’s `config.json` (firmware). The
controller’s expected IPs are the `FRAMES` list in `blinds_controller.py`.
**Keep the two in sync**, or use the controller’s **Broadcast** button.

### Give the controlling computer a matching IP
A managed/unmanaged switch with no DHCP means you should set a **static IP** on
the PC’s Ethernet adapter:

- **Windows**: Settings → Network & Internet → Ethernet → IP assignment → Edit →
  Manual → IPv4 on → IP `10.0.0.50`, mask `255.255.255.0`, gateway `10.0.0.1`.
- **macOS**: System Settings → Network → Ethernet → Details → TCP/IP →
  Configure IPv4 “Manually” → `10.0.0.50` / `255.255.255.0`.
- **Linux**: NetworkManager → wired connection → IPv4 → Manual → `10.0.0.50/24`.

> **Testing on a different network?** If your test LAN is e.g. `192.168.1.x`,
> you don’t have to re-IP everything: in the controller click **“→ Broadcast”**.
> It sends the universe to the subnet broadcast (`192.168.1.255`) so any
> Art-Net listener/monitor on that LAN sees it. Click **“✕ Restore IPs”** to
> return to unicast.

### Verify connectivity
```bash
ping 10.0.0.101      # repeat for .102/.103/.104
```
or open `http://window1.local/` in a browser (mDNS).

---

## 3. Flash the firmware (first time, per board)

Full detail in the [firmware README](../ESP32blinds/README.md). Summary:

1. Install **VS Code + PlatformIO** extension.
2. For each board, put its `config.json` in `ESP32blinds/data/` with the right
   `mdnsName` (`window1`…`window4`) and `ipadress`. The **DMX address is derived
   automatically from the mdnsName** — no extra field needed.
3. Connect the USB-TTL adapter (5V/GND/TX→RX0/RX→TX0). Enter bootloader: tie
   **GPIO0 → GND**, power on / reset.
4. Set `upload_port` in the `wt32-eth01-COM3` env to your serial port, then:
   ```bash
   pio run -e wt32-eth01-COM3 -t uploadfs     # writes config.json to the board
   pio run -e wt32-eth01-COM3 -t upload       # writes the firmware
   ```
5. Power-cycle, confirm via serial monitor (115200) that it gets its IP and
   prints its DMX address.

Repeat for all four boards.

### Later updates — OTA (over the network)
```bash
cd ESP32blinds
pio run -t upload          # reflashes all four windows over Ethernet
```
> Use `-t upload` only. **Never** `-t uploadfs` over OTA — it would overwrite
> every board’s unique `config.json`.

---

## 4. Calibrate & home each window (one-time, mandatory)

**The firmware will not move a screen over Art-Net/OSC until that screen is
calibrated.** This learns the travel range from the limit switches.

For each window (browse to `http://10.0.0.10X/` or `http://windowX.local/`):

1. Open the **Control** tab.
2. Use **forward/stop** to confirm the motor turns the right way. If reversed,
   set `reverseStepper0`/`reverseStepper1` in that board’s `config.json` and
   re-`uploadfs` (USB) — or just reflash that one board.
3. Click **Calibrate stepper0** (bottom) and **Calibrate stepper1** (top). The
   screen drives to its endstops and records the max position. The status label
   should change to **✅ Calibrated**.
4. Click **Home stepper0/1** to send them to the home reference.
5. *(Optional)* Open the **Settings** tab to tune **Speed**, **Acceleration**
   and **Safety Margin** for smooth, safe motion.

Calibration persists across reboots; homing is done once per power-up (the
controller’s heartbeat and the web UI make this easy).

---

## 5A. Operate WITH the desktop controller

1. Install and launch the app — see the [controller README](README.md)
   (Python + Tkinter; optional audio/Link extras).
2. Confirm the IPs on the four frame cards match your windows (or hit
   **Broadcast**).
3. Move a window’s **Bottom/Top** sliders — the blind should follow. If a
   monitor like *Artnetominator* is running you’ll see universe 1 traffic.
4. For music sync:
   - **Ableton Link**: tick **Ableton Link**, start Link from Traktor/Ableton on
     the same LAN — BPM follows automatically.
   - **Audio**: in **AUDIO BPM**, pick an input (mic, line-in, or a Windows
     `[Loopback]` device) and **Start Audio**.
   - Tick **BPM SYNC → Enable**. Dial in **Gap Position** and **Gap Size**
     (each with its own *Beats* rate and *Pattern*). **⟳ Resync** on the
     downbeat; **◀ ▶** nudge to align with the physical motion.

See the controller README §7 for the full UI walkthrough.

## 5B. Operate WITHOUT the controller (lighting console / Art-Net)

Any Art-Net source on the LAN can drive the windows — they’re just 16-bit DMX
fixtures on **universe 1**.

**Channel map (universe 1):**

| Window | Bottom blind (16-bit) | Top blind (16-bit) |
|--------|-----------------------|--------------------|
| 1 | ch 1 (coarse) + 2 (fine) | ch 3 + 4 |
| 2 | ch 5 + 6 | ch 7 + 8 |
| 3 | ch 9 + 10 | ch 11 + 12 |
| 4 | ch 13 + 14 | ch 15 + 16 |

`0` = fully retracted, `65535` = fully extended (toward centre). The firmware
remaps that 16-bit value to each screen’s calibrated travel range.

**MagicQ (example):**
1. Connect the console to the same LAN; give it a `10.0.0.x` IP.
2. Setup → **Art-Net** output enabled, mapped so console **universe 1 → Art-Net
   universe 1** (net 0, subnet 0, universe 1).
3. Patch **four fixtures**, each a generic **2×16-bit channel** dimmer/fixture,
   at addresses **1, 5, 9 and 13**. (Two 16-bit “virtual dimmers” per fixture:
   one for the bottom blind, one for the top.)
4. Raise the faders — the corresponding blind moves.

This works alongside the firmware’s calibration: as long as each window was
calibrated once, the console can take over at any time. (Don’t run the desktop
controller and a console at the same time on the same universe, or they’ll fight
over the values.)

## 5C. Operate via OSC

OSC is intended for external sources (a DAW, Max/MSP, TouchOSC, a show-control
system). Send UDP **OSC to the specific window’s IP on port 7000**:

| Address | Argument | Effect |
|---------|----------|--------|
| `/btm/pos` | float `0.0–1.0` | bottom screen position |
| `/top/pos` | float `0.0–1.0` | top screen position |

Because OSC is addressed by destination IP, you target an individual window by
sending to its IP (e.g. `10.0.0.103:7000`). Example with the Python
`python-osc` package:
```python
from pythonosc.udp_client import SimpleUDPClient
c = SimpleUDPClient("10.0.0.103", 7000)   # window 3
c.send_message("/btm/pos", 0.25)          # bottom screen to 25 %
c.send_message("/top/pos", 0.80)          # top screen to 80 %
```

The same calibration requirement applies (the screen must be calibrated first).

---

## 6. Quick reference

| Thing | Value |
|-------|-------|
| Art-Net | UDP **6454**, **universe 1**, net 0 / subnet 0 |
| OSC | UDP **7000**, `/btm/pos`, `/top/pos` (float 0–1, per-IP) |
| DMX addresses | window1 = 1, window2 = 5, window3 = 9, window4 = 13 |
| Per-window channels | bottom = start+0/+1, top = start+2/+3 (16-bit) |
| Default window IPs | `10.0.0.101–104`, gw `10.0.0.1`, mask `/24` |
| Web UI | `http://10.0.0.10X/` or `http://windowX.local/` |
| Serial monitor | 115200 baud |
| Flash all (OTA) | `cd ESP32blinds && pio run -t upload` |
| Run controller | `python blinds_controller.py` |

---

## 7. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Blind doesn’t move from Art-Net/OSC | Screen not **calibrated** — do it in the web UI. |
| Nothing on the network | Wrong subnet on the PC/console, or no link light. Check static IP and cabling. |
| Controller shows movement but motor doesn’t | Wrong target IP on the frame card, or device offline. Ping it. |
| Wrong window responds | `mdnsName`/IP mismatch, or console patched at the wrong DMX address. |
| Motor runs the wrong way | Set `reverseStepper0/1` in that board’s `config.json`, re-`uploadfs`. |
| Movement looks “stepped”/laggy at fast beats | The **Motor** slew-rate cap (controller) or the device Speed/Accel is limiting it — raise them or slow the beat rate. |
| Console and controller fighting | Only run one Art-Net source on universe 1 at a time. |
| OTA fails | Device must be reachable on its IP; firmware must already be running on it (first flash is USB). |
