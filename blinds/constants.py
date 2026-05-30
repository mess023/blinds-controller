"""
All module-level constants, configuration, and pure-utility functions.
No imports from sibling modules — only stdlib.
"""

import math
import os

# ── Configuration — edit these to match your network ─────────────────────────

ARTNET_PORT = 6454
UNIVERSE    = 1        # Must match firmware:  universe1 = 1
OSC_PORT    = 7000     # firmware OSC listen port (calibration + status)

# All four windows live in ONE universe, laid out as 4-channel fixtures so a
# lighting console (MagicQ etc.) can patch them and a single broadcast packet
# drives everything.  Each window: bottom = chan dmx, dmx+1 ; top = dmx+2, dmx+3.
#   Window 1 → ch 1-4    Window 2 → ch 5-8
#   Window 3 → ch 9-12   Window 4 → ch 13-16
# Each ESP32 must be configured with the matching "dmxAddress" in its config.json.
FRAMES = [
    {"name": "Frame 1", "ip": "10.0.0.101", "dmx": 1},
    {"name": "Frame 2", "ip": "10.0.0.102", "dmx": 5},
    {"name": "Frame 3", "ip": "10.0.0.103", "dmx": 9},
    {"name": "Frame 4", "ip": "10.0.0.104", "dmx": 13},
]

# ── Patterns ──────────────────────────────────────────────────────────────────
#
# Each pattern is a function:  f(i, n) -> float  returning a per-window DELAY
# measured in BEATS.  In _tick this delay is subtracted from the beat before the
# Size / Position oscillations, so "Wave →" makes each window begin its cycle one
# beat after the previous one (regardless of the chosen cycle length).
#   i  : window index  0 .. n-1
#   n  : total number of windows

def _wave(x):
    """Smooth cosine 0→1→0; one full cycle per unit x  (x=0→0, 0.5→1, 1→0)."""
    return (1.0 - math.cos(x * math.tau)) / 2.0

PATTERNS = [
    ("Uniform",  lambda i, n: 0.0),                # all windows together
    ("Wave →",   lambda i, n: float(i)),           # 1 beat later per window 1→4
    ("Wave ←",   lambda i, n: float(n - 1 - i)),   # 1 beat later, reversed 4→1
    ("Spread",   lambda i, n: i * 0.5),            # half-beat stagger per window
    ("Counter",  lambda i, n: 2.0 * (i % 2)),      # odd windows lag 2 beats
    ("Scatter",  lambda i, n: [0.0, 1.3, 2.6, 0.9][i % 4]),
]

# Beat-frequency options for independent Gap Size and Gap Position sync.
# None = static (no oscillation); float = number of beats per full cycle.
BEAT_OPTIONS = [
    ("Off", None),
    ("¼",   0.25),
    ("½",   0.5),
    ("1",   1.0),
    ("2",   2.0),
    ("4",   4.0),
    ("8",   8.0),
    ("16",  16.0),
    ("32",  32.0),
    ("64",  64.0),
    ("128", 128.0),
]

# Global speed multiplier applied to eff_beat before the per-parameter divisions.
SPEEDS = [("⅛×", 0.125), ("¼×", 0.25), ("½×", 0.5), ("1×", 1.0), ("2×", 2.0)]

# ── APC40 MK2 MIDI mapping ────────────────────────────────────────────────────
# Derived from akai-APC40-MK2-MIDI.jpg reference + apc40_mk2-MAPPING.png overlay.
# All notes/CCs are on channel 1 unless noted (mido is 0-indexed: ch1 = channel 0).
#
# Clip launch grid (5 rows × 8 cols) — all on channel 1. Notes are NOT row-based;
# each pad has a unique note. Bottom row = 0–7, then +8 per row going UP:
#       Row 0 (top): 32 33 34 35 36 37 38 39
#       Row 1:       24 25 26 27 28 29 30 31
#       Row 2:       16 17 18 19 20 21 22 23  (unmapped — visual gap between groups)
#       Row 3:        8  9 10 11 12 13 14 15
#       Row 4 (bot):  0  1  2  3  4  5  6  7
APC_CLIP_CH = 0   # mido channel for all clip pads (=channel 1)

def _apc_clip_note(row: int, col: int) -> int:
    """row 0(top)..4(bot), col 0..7 → APC40 MK2 clip-pad note number."""
    return (4 - row) * 8 + col

# Rows used for our mappings (row index, 0=top):
APC_ROW_SIZE_PAT  = 0   # blue patterns row
APC_ROW_SIZE_BEAT = 1   # blue beats row
APC_ROW_POS_PAT   = 3   # green patterns row (row 2 is the visual gap)
APC_ROW_POS_BEAT  = 4   # green beats row
# Separate beat-value menus per parameter row (8 buttons each, cols 0-7).
APC_SIZE_BEAT_VALUES = [4.0,  8.0,  16.0,  32.0,  64.0,  128.0,  256.0,  512.0]
APC_POS_BEAT_VALUES  = [16.0, 32.0, 64.0, 128.0, 256.0,  512.0, 1024.0, 2048.0]

# Device Control knob rings (right-side encoder knobs).
# In Generic Mode the 8 knobs share CC 0x10-0x17 on channel 0 (track-1 bank).
# Ring type is set via CC 0x18-0x1F (0=off, 1=single, 2=volume, 3=pan).
# Physical layout: CC 0x14-0x17 = upper row (knobs 5-8), 0x10-0x13 = lower row.
APC_CC_DEVICE_KNOB_BASE      = 0x10   # 16 — device knob 1 value CC
APC_CC_DEVICE_KNOB_RING_BASE = 0x18   # 24 — device knob 1 ring-type CC

# Beat-visualiser order: upper row first (knobs 1-4 = offsets 0-3),
# then lower row (knobs 5-8 = offsets 4-7).
APC_BEAT_KNOB_ORDER = [0, 1, 2, 3, 4, 5, 6, 7]

# How many beats each device-control knob represents.  The LED ring fills
# (Volume style) from empty → full over this many beats, then wraps.
# Layout: top row left→right, then bottom row left→right.
APC_KNOB_BEAT_SCALES = [1, 2, 4, 8, 16, 32, 64, 128]

# Device control buttons: 8-beat chase sequence (notes from MIDI mapping image).
# Beat order: DEVICE LEFT, DEVICE RIGHT, BANK LEFT, BANK RIGHT,
#             DEVICE ON/OFF, DEVICE LOCK, CLIP/DEVICE VIEW, DETAIL VIEW
APC_NOTE_DEVICE_BTN_ALL = [0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41]
# = [58, 59, 60, 61, 62, 63, 64, 65]

# Single-style ring: 15 cc values, one per LED position (0 = leftmost).
# Formula: centre of each 127/15-wide bucket.
APC_SINGLE_RING_POS = [int((i + 0.5) * 127 / 15) for i in range(15)]
# = [4, 12, 21, 29, 38, 46, 55, 63, 72, 80, 89, 97, 106, 114, 123]

# Master-section buttons (channel 1). Hardware label → APC40 MK2 note → our function.
APC_BTN_BPM_SYNC     = 87   # PAN button (D#5)        → toggle BPM sync
APC_BTN_AUDIO_SYNC   = 88   # SENDS button (E5)       → toggle audio detection
APC_BTN_LINK         = 89   # USER button (F5)        → toggle Ableton Link
APC_BTN_RESYNC       = 90   # METRONOME button (F#5)  → resync phase
APC_BTN_TAP          = 99   # TAP TEMPO button (D#6)  → tap tempo
APC_BTN_NUDGE_MINUS  = 100  # NUDGE − button (E6)
APC_BTN_NUDGE_PLUS   = 101  # NUDGE + button (F6)

# Continuous controls (CC). Channel listed where relevant.
APC_CC_GAP_POS_CH    = 0    # leftmost channel fader (Track Fader 1)
APC_CC_GAP_POS       = 7    # CC 7 on that fader  → gap position 0–100 %
APC_CC_MOTOR_SPD_CH  = 0    # master fader (CC 0x0E = 14, channel 0)
APC_CC_MOTOR_SPD     = 14   # CC 14 (Master Fader)  → motor speed 5–100 %/s
APC_CC_GAP_SIZE_CH   = 0    # top-left device knob (Track Knob 1)
APC_CC_GAP_SIZE      = 48   # CC 48  → gap size 0–25 %
APC_CC_BPM_FINE      = 13   # CC 13  → fine BPM adjust (±0.01 per encoder click)

# ── APC40 canvas layout (image background + overlaid widgets) ────────────────
# Reference dimensions are 1222×733 (matches original image aspect ratio
# 4968:2982 = 1.666:1).  APC40_SCALE shrinks the whole canvas — buttons,
# faders, knob positions, fader widths — so it fits a smaller window without
# losing layout correctness.  Change the scale here and everything follows.
APC40_SCALE = 0.75
_APC40_W_REF = 1222
_APC40_H_REF = 733
APC40_W = round(_APC40_W_REF * APC40_SCALE)
APC40_H = round(_APC40_H_REF * APC40_SCALE)
APC40_IMG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "assets", "apc40_mk2-EMPTY.png")

def _scale_pos(v):
    """Multiply a number or a tuple of numbers by APC40_SCALE and round."""
    if isinstance(v, tuple):
        return tuple(round(x * APC40_SCALE) for x in v)
    return round(v * APC40_SCALE)

_APC40_POS_REF = {
    "clip_origin": (56, 124),     # row 0 col 0 centre
    "clip_dx":     96,            # spacing between columns
    "clip_dy":     43,
    "clip_w":      70,
    "clip_h":      32,
    "btn_bpm_sync":    (894,  94),
    "btn_audio_sync":  (894, 152),
    "btn_link":        (894, 204),
    "btn_resync":      (983, 152),
    "btn_tap":        (1073, 152),
    "btn_nudge_plus":  (983, 204),
    "btn_nudge_minus":(1073, 204),
    "fader_1":      (52,  520, 63, 188),
    "fader_master": (801, 520, 63, 188),
    "knob_gap_size": (52, 54),
    "knob_bpm_fine": (1160, 178),
    "bpm_display": (1028, 50),
}
APC40_POS = {k: _scale_pos(v) for k, v in _APC40_POS_REF.items()}

# ── Colours (Catppuccin Mocha) ────────────────────────────────────────────────

BG      = "#1e1e2e"
CARD    = "#313244"
FG      = "#cdd6f4"
DIM     = "#a6adc8"
BLUE    = "#89b4fa"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
YELLOW  = "#f9e2af"
BTN     = "#45475a"
BTNHOV  = "#585b70"
BTNSEL  = "#89b4fa"
BTNFG   = "#cdd6f4"
BTNSELFG= "#1e1e2e"
