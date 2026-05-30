"""
BlindsApp — the main Tkinter application class.
All imports come from sibling modules; no logic changes.
"""

import logging
import math
import os
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any

from .constants import (
    FRAMES, UNIVERSE, PATTERNS, BEAT_OPTIONS, SPEEDS,
    APC_CLIP_CH, _apc_clip_note,
    APC_ROW_SIZE_PAT, APC_ROW_SIZE_BEAT, APC_ROW_POS_PAT, APC_ROW_POS_BEAT,
    APC_SIZE_BEAT_VALUES, APC_POS_BEAT_VALUES,
    APC_CC_DEVICE_KNOB_BASE, APC_CC_DEVICE_KNOB_RING_BASE,
    APC_BEAT_KNOB_ORDER, APC_SINGLE_RING_POS, APC_KNOB_BEAT_SCALES,
    APC_NOTE_DEVICE_BTN_ALL,
    APC_BTN_BPM_SYNC, APC_BTN_AUDIO_SYNC, APC_BTN_LINK,
    APC_BTN_RESYNC, APC_BTN_TAP, APC_BTN_NUDGE_MINUS, APC_BTN_NUDGE_PLUS,
    APC_CC_GAP_POS_CH, APC_CC_GAP_POS,
    APC_CC_MOTOR_SPD_CH, APC_CC_MOTOR_SPD,
    APC_CC_GAP_SIZE_CH, APC_CC_GAP_SIZE,
    APC_CC_BPM_FINE,
    APC40_W, APC40_H, APC40_IMG_PATH, APC40_POS,
    BG, CARD, FG, DIM, BLUE, GREEN, RED, YELLOW,
    BTN, BTNHOV, BTNSEL, BTNFG, BTNSELFG,
    _wave,
)
from .network import send_universe, osc_send, _osc_sock, _osc_parse
from .beat import BeatClock
from .audio import AudioBPMDetector, AUDIO_AVAILABLE, _AUBIO_OK, _PAW_OK, get_audio_devices, np
from .link import LINK_AVAILABLE, _link, _link_api, _lnk_peers, _lnk_get_tempo, _lnk_get_beat
from .midi import MIDI_AVAILABLE, mido
from .ui_utils import _hr, _hov
from .gpu_spectrum import try_create_renderer as _try_create_gl_spectrum
from .gpu_waveform import try_create_renderer as _try_create_gl_waveform

try:
    from PIL import (Image as _PILImage,
                      ImageTk as _PILImageTk,
                      ImageDraw as _PILImageDraw)
    _PIL_OK = True
except ImportError:
    _PILImage = None       # type: ignore
    _PILImageTk = None     # type: ignore
    _PILImageDraw = None   # type: ignore
    _PIL_OK = False

log = logging.getLogger("blinds")

# ── Application ───────────────────────────────────────────────────────────────

class BlindsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Blinds Controller")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(720, 540)

        self._clock      = BeatClock(120.0)
        self._bpm_on     = tk.BooleanVar(value=False)
        self._bpm_var    = tk.DoubleVar(value=120.0)
        self._gap_pos_var  = tk.DoubleVar(value=50.0)  # 0=bottom, 100=top (%)
        self._gap_size_var = tk.DoubleVar(value=5.0)   # max gap size (% of window)
        self._overlap_var  = tk.DoubleVar(value=0.0)   # closed overlap % (10 → 55% each)
        self._size_sync_beats: "float | None" = None   # None = static
        self._pos_sync_beats:  "float | None" = None
        self._size_sync_btns: list = []
        self._pos_sync_btns:  list = []
        self._speed_idx  = 3                           # default: 1× (literal beats)
        # Independent patterns for position and size
        self._pos_pat_fn  = PATTERNS[0][1]             # default: Uniform
        self._size_pat_fn = PATTERNS[0][1]
        self._pos_pbts: dict  = {}                     # pattern name → button
        self._size_pbts: dict = {}

        # Beat source toggle + unified phase offset (drives resync / nudge).
        # Off by default → clock (audio/manual BPM); check to follow Ableton Link.
        self._link_on    = tk.BooleanVar(value=False)
        self._beat_offset        = 0.0   # beats; adjusted by resync / nudge
        self._resync_time        = 0.0   # wall-clock time of last resync (perf_counter)
        self._beats_since_resync = 0     # audio-detected beats since last resync
        # Highest madmom beat timestamp we've already counted into
        # _beats_since_resync — keeps the counter monotonic across the ~1 s
        # inference cycles (madmom REPLACES its beat_times each cycle).
        self._madmom_beats_seen_t = 0.0
        self._last_kick_time     = 0.0   # perf_counter of last strong kick
        # Phase-lock freeze: madmom's continuous phase tracker is suppressed
        # for a short window after a resync so it can't drift the LEDs away
        # from the beat-1 we just snapped to.
        self._phase_lock_freeze_until: float = 0.0
        # Spectrum bitmap renderer state — lazily built on first frame and
        # rebuilt whenever the canvas resizes.
        self._spec_buf: Any      = None  # uint8 (H, W, 3) — RGB pixels
        self._spec_pil: Any      = None  # PIL.Image wrapping the buffer
        self._spec_photo: Any    = None  # ImageTk.PhotoImage for the canvas
        self._spec_image_id      = None
        self._spec_col_color: Any= None  # (W, 3) per-column tint by frequency
        self._spec_peak: Any     = None  # (W,) peak-hold height per column
        self._spec_y_grid: Any   = None  # (H, 1) cached np.arange(H)
        # GPU-rendered spectrum (moderngl). None if moderngl is not installed
        # or the GL context fails — the CPU bitmap path is used as a fallback.
        self._gl_spectrum: Any   = _try_create_gl_spectrum()
        self._gl_waveform: Any   = _try_create_gl_waveform()
        # Native (embedded glfw) spectrum panel — created in _build_big_spectrum
        # once we have a tk frame to parent into.  None means "use the canvas
        # + PhotoImage fallback path".
        self._native_spec: Any   = None
        # Per-column dB peak with continuous decay (80 dB/s) so transients
        # rise instantly but tails decay smoothly.
        self._spec_decay_db: Any = None
        self._spec_last_t: float = 0.0
        self._wave_last_t: float = 0.0
        # Waveform bitmap renderer state — same architecture as the spectrum.
        self._wave_buf_arr: Any  = None  # uint8 (H, W, 3)
        self._wave_pil: Any      = None
        self._wave_photo: Any    = None
        self._wave_image_id      = None
        self._wave_y_grid: Any   = None
        self._wave_static_bg: Any= None
        self._suspend_send = False  # guard to coalesce batched frame updates

        self._ip_vars = [tk.StringVar(value=f["ip"]) for f in FRAMES]
        self._pvars  = [{"b": tk.DoubleVar(value=0.0), "t": tk.DoubleVar(value=0.0)}
                        for _ in FRAMES]
        self._plbls  = []   # list of {"b": Label, "t": Label} — filled below
        self._sbts   = []   # speed buttons
        self._canvas = None # preview canvas — lives in its own Toplevel window
        self._preview_win = None  # the preview Toplevel
        self._beat   = 0.0  # latest beat value, read by _draw_preview

        self._cur_pos    = [(0.0, 0.0)] * len(FRAMES)  # slew-rate position tracker
        self._last_apply = time.perf_counter()
        self._max_spd    = tk.DoubleVar(value=30.0)     # %/second hard cap

        self._audio_det  = AudioBPMDetector() if AUDIO_AVAILABLE else None
        self._audio_devs: list = []  # populated in _build_audio_section

        self._midi_port   = None
        self._midi_out: Any = None    # output port for LED feedback
        self._midi_active = False
        self._size_pat_name = PATTERNS[0][0]   # underlying pattern fn name (Uniform default)
        self._pos_pat_name  = PATTERNS[0][0]
        # Radio-group selection in the pattern rows. None means the user hasn't
        # clicked anything yet → no button lights up. "Still" or a pattern name
        # otherwise. Distinct from _size_pat_name so the default "Uniform" fn
        # doesn't make Uniform glow at startup.
        self._size_pat_selected: "str | None" = None
        self._pos_pat_selected:  "str | None" = None
        # Beat-chase state: which of the 8 buttons (knobs or device buttons) is
        # currently lit. None means no LED is lit yet (or MIDI isn't connected).
        self._chase_last_btn: "int | None" = None
        # Per-knob last sent CC value + button state — used to skip MIDI sends
        # when nothing would change (saves bandwidth at slow beat scales).
        self._knob_prev_cc:  list = [-1]    * 8
        self._knob_btn_on:   list = [False] * 8

        # Per-window status from the firmware (updated by the telemetry thread).
        # keys: cal[2], homed[2], max[2], pos[2], seen(bool/timestamp)
        self._status = [{"cal": [0, 0], "homed": [0, 0], "max": [0, 0],
                         "pos": [0, 0], "seen": 0.0} for _ in FRAMES]
        self._stat_lbls: list = []   # per-window status labels — filled in _frame_card

        self._build_ui()
        # _param_block's default-highlighting calls _set_*_pattern("Uniform")
        # which (now) also marks "Uniform" as the radio-row selection. Reset
        # to "Still" so the row starts in the sync-off state with only Still
        # lit (deep red on the controller, red text in the canvas overlay).
        self._size_pat_selected = "Still"
        self._pos_pat_selected  = "Still"
        self._refresh_apc_btn_colors()
        self._open_preview()                  # preview lives in its own window
        self._bpm_on.trace_add("write", self._on_bpm_toggle)
        self._link_on.trace_add("write", lambda *_: (
            self._refresh_apc_leds(), self._refresh_apc_btn_colors()))
        self._gap_size_var.trace_add("write", lambda *_: self._refresh_apc_leds())
        threading.Thread(target=self._anim_loop, daemon=True).start()
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        self._heartbeat()
        self._chase_leds_tick()
        self.after(0, self._poll_link)        # schedule after UI is fully laid out
        self.after(300, self._refresh_status_labels)
        self.after(120, self._draw_preview)   # first draw after layout
        # Auto-detect the APC40 MK2 a moment after startup; retry a few times
        # so devices plugged in just after launch still get picked up.
        self.after(400, self._auto_connect_apc40)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ═════════════════════════════════════════════════════════════════════════
    # UI construction
    # ═════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        # ── Header row: title (centred) + compact MIDI picker (right) ───────
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(16, 2))
        # MIDI section first so its packed-right widgets reserve their slot
        # before the centred title fills the remaining space.
        self._build_midi_section(parent=header)
        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left", expand=True)
        tk.Label(title_box, text="BLINDS CONTROLLER",
                 font=("Segoe UI", 15, "bold"), bg=BG, fg=BLUE).pack()
        tk.Label(title_box, text=f"Art-Net unicast  •  Universe {UNIVERSE}",
                 font=("Segoe UI", 9), bg=BG, fg=DIM).pack()

        # ── Three-column layout: Frames (L) | Spectrum/APC40 (C) | Controls (R) ──
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=12, pady=(6, 0))

        # Left column: Frame cards (vertical stack)
        self._build_frame_cards_vertical(parent=main)

        # Right column FIRST (so center gets the leftover horizontal space).
        # expand=False so right_col only takes as wide as its content needs.
        right_col = tk.Frame(main, bg=BG)
        right_col.pack(side="right", fill="y", expand=False, padx=(8, 0))

        _hr(right_col)
        self._build_artnet_controls(parent=right_col)
        _hr(right_col)
        self._build_audio_section(parent=right_col)
        _hr(right_col)
        self._build_beat_visualizer(parent=right_col)
        tk.Frame(right_col, bg=BG, height=14).pack()

        # Center column: big spectrum on top (expanding) + APC40 at bottom.
        center_col = tk.Frame(main, bg=BG)
        center_col.pack(side="left", fill="both", expand=True)
        # Pack APC40 first with side="bottom" so it docks at the bottom; the
        # spectrum then fills all the remaining vertical space above it.
        self._build_apc40_canvas(parent=center_col)
        self._build_big_spectrum(parent=center_col)

        # Keep _clock.bpm in sync whenever _bpm_var changes (audio, tap, Link, slider).
        # _build_bpm_section is not called so this trace must live here.
        self._bpm_var.trace_add("write",
            lambda *_: setattr(self._clock, "bpm", self._bpm_var.get()))

        # _poll_link needs _link_lbl — create a minimal hidden one so it never crashes.
        self._link_lbl = tk.Label(self, text="", font=("Segoe UI", 9),
                                  bg=BG, fg=DIM)
        # (not packed — invisible; status shown in the audio section)

        # ── Window sizing constrained to fit a 1920×1080 monitor ───────────
        # Right-side taskbar takes 65 px; bottom edge is fully usable.
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        target_w = min(screen_w - 65, 1855)   # right taskbar
        target_h = min(screen_h,      1040)   # title bar overhead only
        self.geometry(f"{target_w}x{target_h}+0+0")
        self.maxsize(target_w, target_h)
        self.minsize(1400, 900)

    # ── APC40 image canvas with overlaid mapped controls ─────────────────────
    # All overlays are CANVAS ITEMS, not widgets — so no opaque button face
    # ever sits in front of the image. Only text and a thin fader marker get
    # drawn; clicks are dispatched via canvas tag bindings on invisible hit
    # rectangles. This gives true transparency over the APC40 art.

    # Overlay colours chosen to read against the cream/yellow APC40 art:
    APC_TXT_OFF   = "#1e1e2e"   # dark         — readable on cream pads
    APC_TXT_ON    = "#ff0000"   # red          — high contrast when lit
    APC_TXT_HOV   = "#0066cc"   # blue         — momentary hover highlight
    APC_BPM_FG    = "#f9e2af"   # yellow       — big BPM readout
    APC_LABEL_PRINTED = "#ffffff"   # white — matches the APC40's printed
                                    # "TAP TEMPO" / "NUDGE +/-" labels

    def _build_apc40_canvas(self, parent=None):
        """APC40 image as background with click-only TEXT overlays at the
        positions of each MIDI-mapped control. Same state the MIDI handler
        toggles, so clicks here and hardware presses stay in sync."""
        if parent is None:
            parent = self
        try:
            from PIL import Image, ImageTk
        except ImportError:
            tk.Label(parent, text="(install Pillow to enable the APC40 image: pip install Pillow)",
                     bg=BG, fg=RED, font=("Segoe UI", 9)).pack(pady=6)
            return
        _img: Any = Image   # Pillow ≥10 hides LANCZOS under .Resampling
        lanczos = getattr(_img, "Resampling", _img).LANCZOS
        try:
            pil = Image.open(APC40_IMG_PATH).resize((APC40_W, APC40_H), lanczos)
        except FileNotFoundError:
            tk.Label(parent, text=f"(APC40 image not found: {APC40_IMG_PATH})",
                     bg=BG, fg=RED, font=("Segoe UI", 9)).pack(pady=6)
            return

        self._apc40_tkimg = ImageTk.PhotoImage(pil)   # MUST keep reference

        wrap = tk.Frame(parent, bg=BG)
        # Dock at the bottom of the centre column — bottom of the APC40 image
        # aligns with the bottom of the window, freeing the upper area for the
        # spectrum analyzer.
        wrap.pack(side="bottom", anchor="s", pady=(0, 0))
        c = tk.Canvas(wrap, width=APC40_W, height=APC40_H,
                      bg=BG, highlightthickness=0, bd=0)
        c.pack()
        c.create_image(0, 0, anchor="nw", image=self._apc40_tkimg)
        self._apc_canvas = c

        # Per-overlay state — each entry is {text_id, lit} for _refresh_apc_btn_colors
        self._apc_size_pat_btns:  list = []   # [(col, dict), ...]
        self._apc_size_beat_btns: list = []
        self._apc_pos_pat_btns:   list = []
        self._apc_pos_beat_btns:  list = []
        self._apc_master_btns:    dict = {}   # name → dict

        pat_labels       = ["Still", "Uniform", "Wave→", "←Wave",
                            "Spread", "Counter", "Scatter"]
        size_beat_labels = [str(int(v)) for v in APC_SIZE_BEAT_VALUES]
        pos_beat_labels  = [str(int(v)) for v in APC_POS_BEAT_VALUES]

        origin_x, origin_y = APC40_POS["clip_origin"]
        dx, dy = APC40_POS["clip_dx"], APC40_POS["clip_dy"]
        cw, ch_ = APC40_POS["clip_w"], APC40_POS["clip_h"]

        # ── Clip grid: 4 mapped rows of 8 cols each ──────────────────────────
        for col in range(8):
            cx = origin_x + col * dx
            y_sp = origin_y + APC_ROW_SIZE_PAT  * dy
            y_sb = origin_y + APC_ROW_SIZE_BEAT * dy
            y_pp = origin_y + APC_ROW_POS_PAT   * dy
            y_pb = origin_y + APC_ROW_POS_BEAT  * dy

            # Size pattern row — col 0 = Still(sync off), cols 1-6 = patterns
            if col == 0:
                self._apc_size_pat_btns.append((0, self._mk_canvas_btn(
                    cx, y_sp, cw, ch_, "Still",
                    lambda: self._set_size_sync(None))))
            elif 1 <= col <= 6:
                p = PATTERNS[col - 1]
                self._apc_size_pat_btns.append((col, self._mk_canvas_btn(
                    cx, y_sp, cw, ch_, pat_labels[col],
                    lambda fn=p[1], nm=p[0]: self._set_size_pattern(fn, nm))))

            v_size = APC_SIZE_BEAT_VALUES[col]
            self._apc_size_beat_btns.append((col, self._mk_canvas_btn(
                cx, y_sb, cw, ch_, size_beat_labels[col],
                lambda val=v_size: self._set_size_sync(val))))

            if col == 0:
                self._apc_pos_pat_btns.append((0, self._mk_canvas_btn(
                    cx, y_pp, cw, ch_, "Still",
                    lambda: self._set_pos_sync(None))))
            elif 1 <= col <= 6:
                p = PATTERNS[col - 1]
                self._apc_pos_pat_btns.append((col, self._mk_canvas_btn(
                    cx, y_pp, cw, ch_, pat_labels[col],
                    lambda fn=p[1], nm=p[0]: self._set_pos_pattern(fn, nm))))

            v_pos = APC_POS_BEAT_VALUES[col]
            self._apc_pos_beat_btns.append((col, self._mk_canvas_btn(
                cx, y_pb, cw, ch_, pos_beat_labels[col],
                lambda val=v_pos: self._set_pos_sync(val))))

        # ── Master-section buttons ───────────────────────────────────────────
        # Each label is drawn ABOVE its cream button in the same grey/font as
        # the APC40's own printed labels ("TAP TEMPO", "NUDGE +/-"), so the
        # added overlays blend in with the art. Labels for TAP / NUDGE +/-
        # are omitted because the APC40 art already prints them.
        ms_font = ("Segoe UI", 8, "normal")
        ms_dy   = -16   # ~16 px above the cream button centre — same gap as
                        # the APC40's own "TAP TEMPO" / "NUDGE +/-" labels

        def mb(key: str, label: str, cmd):
            x, y = APC40_POS[key]
            return self._mk_canvas_btn(
                x, y, 56, 22, label, cmd,
                label_dy=ms_dy,
                color_off=self.APC_LABEL_PRINTED,
                font=ms_font)

        self._apc_master_btns["bpm_sync"] = mb(
            "btn_bpm_sync",    "BPM SYNC",
            lambda: self._bpm_on.set(not self._bpm_on.get()))
        self._apc_master_btns["audio_sync"] = mb(
            "btn_audio_sync",  "AUDIO",    self._toggle_audio)
        self._apc_master_btns["link"] = mb(
            "btn_link",        "LINK",
            lambda: self._link_on.set(not self._link_on.get()))
        self._apc_master_btns["resync"] = mb(
            "btn_resync",      "RESYNC",   self._resync)
        # TAP / NUDGE buttons: printed labels exist on the APC40 art already,
        # so we only add an invisible click target — no overlay text.
        self._apc_master_btns["tap"] = mb(
            "btn_tap",         "",         self._tap)
        self._apc_master_btns["nudge_plus"] = mb(
            "btn_nudge_plus",  "",         lambda: self._nudge(0.0625))
        self._apc_master_btns["nudge_minus"] = mb(
            "btn_nudge_minus", "",         lambda: self._nudge(-0.0625))

        # ── Gap Position fader: draggable yellow marker line ────────────────
        fx, fy, fw, fh = APC40_POS["fader_1"]

        # Label above fader
        c.create_text(fx, fy - 11, text="GAP POS",
                     fill=self.APC_LABEL_PRINTED, font=("Segoe UI", 6, "bold"),
                     anchor="center")

        def marker_y() -> float:
            v = max(0.0, min(100.0, self._gap_pos_var.get()))
            return fy + (100.0 - v) / 100.0 * fh

        self._apc_fader_marker = c.create_line(
            fx - fw / 2, marker_y(), fx + fw / 2, marker_y(),
            fill=self.APC_BPM_FG, width=4)
        fader_tag = "fader1"
        c.create_rectangle(fx - fw / 2, fy, fx + fw / 2, fy + fh,
                            fill="", outline="", tags=(fader_tag,))

        def on_fader(e):
            rel = max(0.0, min(1.0, (e.y - fy) / float(fh)))
            self._gap_pos_var.set(round((1.0 - rel) * 100.0, 1))

        c.tag_bind(fader_tag, "<Button-1>",  on_fader)
        c.tag_bind(fader_tag, "<B1-Motion>", on_fader)
        c.tag_bind(fader_tag, "<Enter>",     lambda _e: c.config(cursor="sb_v_double_arrow"))
        c.tag_bind(fader_tag, "<Leave>",     lambda _e: c.config(cursor="arrow"))

        def _update_marker(*_):
            my = marker_y()
            c.coords(self._apc_fader_marker, fx - fw / 2, my, fx + fw / 2, my)
        self._gap_pos_var.trace_add("write", _update_marker)

        # ── Motor Speed fader (master fader — far right of channel strip) ───
        sx, sy, sw, sh = APC40_POS["fader_master"]

        # Motor speed percentage display above fader
        self._apc_motor_speed_text = c.create_text(
            sx, sy - 11, text="0%/s",
            fill=self.APC_BPM_FG, font=("Segoe UI", 6, "bold"),
            anchor="center")

        def motor_marker_y() -> float:
            v = max(5.0, min(100.0, self._max_spd.get()))
            return sy + (1.0 - (v - 5.0) / 95.0) * sh

        self._apc_motor_marker = c.create_line(
            sx - sw / 2, motor_marker_y(), sx + sw / 2, motor_marker_y(),
            fill=self.APC_BPM_FG, width=4)
        motor_tag = "fader_master"
        c.create_rectangle(sx - sw / 2, sy, sx + sw / 2, sy + sh,
                            fill="", outline="", tags=(motor_tag,))

        def on_motor_fader(e):
            rel = max(0.0, min(1.0, (e.y - sy) / float(sh)))
            self._max_spd.set(round(5.0 + (1.0 - rel) * 95.0, 0))

        c.tag_bind(motor_tag, "<Button-1>",  on_motor_fader)
        c.tag_bind(motor_tag, "<B1-Motion>", on_motor_fader)
        c.tag_bind(motor_tag, "<Enter>",     lambda _e: c.config(cursor="sb_v_double_arrow"))
        c.tag_bind(motor_tag, "<Leave>",     lambda _e: c.config(cursor="arrow"))

        def _update_motor_marker(*_):
            my = motor_marker_y()
            c.coords(self._apc_motor_marker, sx - sw / 2, my, sx + sw / 2, my)
            # Update motor speed percentage text
            spd = int(round(self._max_spd.get()))
            c.itemconfig(self._apc_motor_speed_text, text=f"{spd}%/s")
        self._max_spd.trace_add("write", _update_motor_marker)

        # ── Gap Size knob (top-left of the 8 top knobs) ─────────────────────
        # Click+drag vertically or scroll-wheel to change. Centre text shows
        # the current value in yellow over the dark knob body.
        kx, ky = APC40_POS["knob_gap_size"]

        # Label above knob
        c.create_text(kx, ky - 23, text="GAP SIZE",
                     fill=self.APC_LABEL_PRINTED, font=("Segoe UI", 6, "bold"),
                     anchor="center")

        self._apc_gap_size_text = c.create_text(
            kx, ky, text=f"{self._gap_size_var.get():.1f}%",
            fill=self.APC_BPM_FG, font=("Segoe UI", 7, "bold"),
            anchor="center", tags=("knob_gs",))
        c.create_oval(kx - 17, ky - 17, kx + 17, ky + 17,
                       fill="", outline="", tags=("knob_gs",))

        knob_drag = {"y0": 0, "v0": 0.0}

        def knob_press(e):
            knob_drag["y0"] = e.y
            knob_drag["v0"] = self._gap_size_var.get()

        def knob_drag_motion(e):
            # 75 px of vertical travel = full 0–25 % range; up = increase
            dy   = knob_drag["y0"] - e.y
            new  = max(0.0, min(25.0, knob_drag["v0"] + dy * (25.0 / 75.0)))
            self._gap_size_var.set(round(new, 1))

        def knob_wheel(e):
            # tag_bind doesn't accept <MouseWheel>, so we bind at canvas level
            # and gate on cursor proximity to the knob centre.
            if (e.x - kx) ** 2 + (e.y - ky) ** 2 > 17 * 17:
                return
            step = 0.5 if abs(e.delta) >= 120 else 0.1
            sign = 1 if e.delta > 0 else -1
            new  = max(0.0, min(25.0, self._gap_size_var.get() + sign * step))
            self._gap_size_var.set(round(new, 1))

        c.tag_bind("knob_gs", "<Button-1>",  knob_press)
        c.tag_bind("knob_gs", "<B1-Motion>", knob_drag_motion)
        c.bind("<MouseWheel>", knob_wheel, add="+")
        c.tag_bind("knob_gs", "<Enter>",
                    lambda _e: c.config(cursor="sb_v_double_arrow"))
        c.tag_bind("knob_gs", "<Leave>",
                    lambda _e: c.config(cursor="arrow"))

        self._gap_size_var.trace_add("write", lambda *_: c.itemconfig(
            self._apc_gap_size_text,
            text=f"{self._gap_size_var.get():.1f}%"))

        # ── BPM fine adjust knob (±0.01 per encoder click) ──────────────────
        bfx, bfy = APC40_POS["knob_bpm_fine"]

        # Label above knob (two lines, centered)
        c.create_text(bfx, bfy - 29, text="BPM\nfine adjust",
                     fill=self.APC_LABEL_PRINTED, font=("Segoe UI", 6, "bold"),
                     anchor="center", justify="center")

        # Invisible hit area for the knob
        c.create_oval(bfx - 14, bfy - 14, bfx + 14, bfy + 14,
                       fill="", outline="", tags=("knob_bpm_fine",))

        def on_bpm_fine_cc(cc_value):
            """Handle CC 13 input: APC40 relative encoder
            Each message = one encoder click = ±0.01 BPM
            64 = no change (center)
            < 64 = CW turn (right) → +0.01 BPM
            > 64 = CCW turn (left) → -0.01 BPM"""
            if cc_value == 64:
                return  # no change

            if cc_value < 64:
                # CW turn: +0.01 BPM
                adjustment = 0.01
            else:
                # CCW turn: -0.01 BPM
                adjustment = -0.01

            new_bpm = self._bpm_var.get() + adjustment
            self._bpm_var.set(round(new_bpm, 2))

        self._bpm_fine_handler = on_bpm_fine_cc

        # ── Current BPM display (canvas text — fully transparent) ───────────
        bx, by = APC40_POS["bpm_display"]
        self._apc_bpm_text = c.create_text(
            bx, by, text=f"{self._bpm_var.get():.2f}",
            fill=self.APC_BPM_FG, font=("Segoe UI", 14, "bold"),
            anchor="center")
        self._bpm_var.trace_add("write", lambda *_: c.itemconfig(
            self._apc_bpm_text, text=f"{self._bpm_var.get():.2f}"))

        self._refresh_apc_btn_colors()

    def _mk_canvas_btn(self, x: float, y: float, w: float, h: float,
                        label: str, command,
                        label_dy: float = 0,
                        color_off: str | None = None,
                        font: tuple = ("Segoe UI", 9, "bold")) -> dict:
        """Transparent canvas button — text only, hit-detected via an invisible
        rectangle at (x, y). The label is drawn at (x, y + label_dy) — use a
        negative dy to put it ABOVE the hit area. Pass label="" for a fully
        invisible click target (used where the APC40 art already prints the
        button's name). Returns {text, lit} for _refresh_apc_btn_colors."""
        c = self._apc_canvas
        off = color_off if color_off is not None else self.APC_TXT_OFF
        tag = f"apcbtn_{id(command)}_{x}_{y}"
        c.create_rectangle(x - w / 2, y - h / 2, x + w / 2, y + h / 2,
                            fill="", outline="", tags=(tag,))
        text_id = (c.create_text(x, y + label_dy, text=label, fill=off,
                                  font=font, tags=(tag,), anchor="center")
                   if label else None)
        btn = {"text": text_id, "lit": False, "color_off": off}

        def on_enter(_e):
            c.config(cursor="hand2")
            if text_id is not None and not btn["lit"]:
                c.itemconfig(text_id, fill=self.APC_TXT_HOV)

        def on_leave(_e):
            c.config(cursor="arrow")
            if text_id is not None:
                c.itemconfig(text_id,
                              fill=self.APC_TXT_ON if btn["lit"] else off)

        c.tag_bind(tag, "<Button-1>", lambda _e: command())
        c.tag_bind(tag, "<Enter>",    on_enter)
        c.tag_bind(tag, "<Leave>",    on_leave)
        return btn

    def _refresh_apc_btn_colors(self):
        """Re-tint canvas-text overlays to match current app state."""
        if not hasattr(self, "_apc_size_pat_btns"):
            return
        c = self._apc_canvas

        def tint(btn: dict, lit: bool):
            btn["lit"] = lit
            if btn["text"] is None:
                return    # button has no visible label (printed on the art)
            c.itemconfig(btn["text"],
                          fill=self.APC_TXT_ON if lit else btn["color_off"])

        # Pattern row = radio group on _size_pat_selected (None at startup, so
        # nothing lights until the user picks). Beat row stays a separate group.
        for col, btn in self._apc_size_pat_btns:
            name = "Still" if col == 0 else PATTERNS[col - 1][0]
            tint(btn, self._size_pat_selected == name)
        for col, btn in self._apc_size_beat_btns:
            tint(btn, self._size_sync_beats == APC_SIZE_BEAT_VALUES[col])

        for col, btn in self._apc_pos_pat_btns:
            name = "Still" if col == 0 else PATTERNS[col - 1][0]
            tint(btn, self._pos_pat_selected == name)
        for col, btn in self._apc_pos_beat_btns:
            tint(btn, self._pos_sync_beats == APC_POS_BEAT_VALUES[col])

        tint(self._apc_master_btns["bpm_sync"],    self._bpm_on.get())
        tint(self._apc_master_btns["audio_sync"],
             self._audio_det is not None and self._audio_det.running)
        tint(self._apc_master_btns["link"],        self._link_on.get())

    # ── Frame cards (vertical stack on left) ─────────────────────────────────

    def _build_frame_cards_vertical(self, parent=None):
        """Stack frame cards vertically on the left side, ~148 px wide."""
        if parent is None:
            parent = self
        col = tk.Frame(parent, bg=BG)
        col.pack(side="left", fill="y", padx=(0, 6))
        # Zero-height width spacer locks the column width; cards fill="x" inherit it
        tk.Frame(col, bg=BG, height=0, width=148).pack()
        for row_idx, cfg in enumerate(FRAMES):
            self._frame_card(col, row_idx, cfg, layout="vertical")

    def _build_frame_cards(self, parent=None):
        """Legacy horizontal layout (kept for backward compatibility)."""
        if parent is None:
            parent = self
        row = tk.Frame(parent, bg=BG)
        row.pack(padx=16)
        for col, cfg in enumerate(FRAMES):
            self._frame_card(row, col, cfg, layout="horizontal")

    def _frame_card(self, parent, idx, cfg, layout="horizontal"):
        # Compact card. Bottom/Top labels sit above their sliders with the
        # percentage right-aligned on the same row; "offline" centres below
        # the calibrate button. The column itself fixes the width (see
        # _build_frame_cards_vertical), so cards just fill="x".
        card = tk.Frame(parent, bg=CARD, padx=8, pady=8)
        if layout == "horizontal":
            card.grid(row=0, column=idx, padx=3, sticky="n")
        else:  # vertical
            card.pack(fill="x", pady=3)

        tk.Label(card, text=cfg["name"],
                 font=("Segoe UI", 10, "bold"), bg=CARD, fg=BLUE).pack()
        tk.Entry(card, textvariable=self._ip_vars[idx],
                 font=("Segoe UI", 8), bg=BTN, fg=FG, justify="center",
                 insertbackground=FG, relief="flat").pack(fill="x", pady=(0, 6))

        pv   = self._pvars[idx]
        lbls = {}
        for key, text in (("b", "Bottom"), ("t", "Top")):
            # Header row: label left, percentage right
            hdr = tk.Frame(card, bg=CARD)
            hdr.pack(fill="x", pady=(2, 0))
            tk.Label(hdr, text=text, font=("Segoe UI", 9),
                     bg=CARD, fg=FG, anchor="w").pack(side="left")
            lbl = tk.Label(hdr, text="0%", font=("Segoe UI", 9, "bold"),
                           bg=CARD, fg=GREEN, anchor="e")
            lbl.pack(side="right")
            lbls[key] = lbl

            # Slider underneath, stretches to card width
            ttk.Scale(card, from_=0, to=100, orient="horizontal",
                      variable=pv[key]).pack(fill="x", pady=(0, 3))

            def _on_write(*_, frame_idx=idx, k=key):
                self._refresh_lbl(frame_idx, k)
                if not self._bpm_on.get():
                    self._send_frame()

            pv[key].trace_add("write", _on_write)

        self._plbls.append(lbls)

        # Calibration trigger + status (centred under the button)
        tk.Button(card, text="Calibrate window", font=("Segoe UI", 8),
                  bg=BTN, fg=BTNFG, relief="flat", padx=4, pady=4, cursor="hand2",
                  command=lambda i=idx: self._calibrate_window(i)).pack(
                      fill="x", pady=(6, 2))
        stat = tk.Label(card, text="offline", font=("Consolas", 8),
                        bg=CARD, fg=DIM, anchor="center")
        stat.pack(fill="x")
        self._stat_lbls.append(stat)

    # ── Art-Net controls ─────────────────────────────────────────────────────

    def _build_artnet_controls(self, parent=None):
        if parent is None:
            parent = self
        row = tk.Frame(parent, bg=BG)
        row.pack(padx=16, pady=(0, 4), fill="x")

        self._artnet_status = tk.Label(row, text="Unicast to individual IPs",
                                       font=("Segoe UI", 8), bg=BG, fg=DIM)
        self._artnet_status.pack(side="left")

        restore_btn = tk.Button(row, text="✕ Restore IPs",
                                font=("Segoe UI", 8), bg=BTN, fg=BTNFG,
                                relief="flat", padx=8, pady=3, cursor="hand2",
                                command=self._restore_ips)
        restore_btn.pack(side="right", padx=(4, 0))
        _hov(restore_btn)

        bcast_btn = tk.Button(row, text="→ Broadcast",
                              font=("Segoe UI", 8), bg=BTN, fg=BTNFG,
                              relief="flat", padx=8, pady=3, cursor="hand2",
                              command=self._set_broadcast)
        bcast_btn.pack(side="right", padx=(4, 0))
        _hov(bcast_btn)

        cal_all_btn = tk.Button(row, text="Calibrate all windows",
                                font=("Segoe UI", 8), bg=BTN, fg=YELLOW,
                                relief="flat", padx=8, pady=3, cursor="hand2",
                                command=self._calibrate_all)
        cal_all_btn.pack(side="left", padx=(12, 0))
        _hov(cal_all_btn)

        prev_btn = tk.Button(row, text="⊞ Preview window",
                             font=("Segoe UI", 8), bg=BTN, fg=BLUE,
                             relief="flat", padx=8, pady=3, cursor="hand2",
                             command=self._open_preview)
        prev_btn.pack(side="left", padx=(8, 0))
        _hov(prev_btn)

    # ── Preview canvas ───────────────────────────────────────────────────────

    def _open_preview(self):
        """Open (or re-show) the preview in its own resizable window."""
        if self._preview_win is not None and self._preview_win.winfo_exists():
            self._preview_win.deiconify()
            self._preview_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Blinds Preview")
        win.configure(bg=BG)
        win.geometry("760x340")
        win.minsize(400, 200)
        tk.Label(win, text="PREVIEW",
                 font=("Segoe UI", 9, "bold"), bg=BG, fg=DIM).pack(anchor="w", padx=12, pady=(8, 0))
        self._canvas = tk.Canvas(win, bg="#11111b", highlightthickness=0, cursor="arrow")
        self._canvas.pack(fill="both", expand=True, padx=12, pady=10)
        # No <Configure> binding: the continuous 60 fps loop already redraws at
        # the current size every frame, so resizing is handled without spawning
        # extra (overlapping) draw loops.
        # Closing the window just hides it (re-open with the Preview button).
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self._preview_win = win

    def _draw_preview(self):
        c   = self._canvas
        win = self._preview_win
        # Keep the loop alive but idle while the preview is closed/hidden.
        if (c is None or not c.winfo_exists() or win is None
                or not win.winfo_exists() or not win.winfo_viewable()):
            self.after(200, self._draw_preview)
            return
        W = c.winfo_width()
        H = c.winfo_height()
        if W < 10:
            self.after(100, self._draw_preview)
            return
        c.delete("all")

        n         = len(FRAMES)
        lbl_h     = 20      # space below each window for the label
        margin_x  = 14
        margin_y  = 10
        gap_x     = 10      # horizontal gap between windows
        win_w     = (W - 2 * margin_x - (n - 1) * gap_x) // n
        win_h     = H - margin_y - lbl_h - margin_y

        # Room wall background
        c.create_rectangle(0, 0, W, H, fill="#181825", outline="")

        for i, cfg in enumerate(FRAMES):
            x0 = margin_x + i * (win_w + gap_x)
            y0 = margin_y
            x1 = x0 + win_w
            y1 = y0 + win_h

            b_pct = self._pvars[i]["b"].get() / 100.0   # 0=retracted, 1=extended
            t_pct = self._pvars[i]["t"].get() / 100.0

            top_edge = y0 + t_pct * win_h   # pixel where top blind reaches
            bot_edge = y1 - b_pct * win_h   # pixel where bottom blind reaches

            # ── Window reveal (light area) ────────────────────────────────
            c.create_rectangle(x0, y0, x1, y1, fill="#d0e8ff", outline="")

            # ── Top blind (single flat surface) ──────────────────────────
            if top_edge > y0:
                c.create_rectangle(x0, y0, x1, top_edge, fill="#4c4f69", outline="")
                # Bottom hem of top blind
                c.create_line(x0, top_edge, x1, top_edge, fill="#7480c2", width=2)

            # ── Bottom blind (single flat surface) ───────────────────────
            if bot_edge < y1:
                c.create_rectangle(x0, bot_edge, x1, y1, fill="#4c4f69", outline="")
                # Top hem of bottom blind
                c.create_line(x0, bot_edge, x1, bot_edge, fill="#7480c2", width=2)

            # ── Window frame border ───────────────────────────────────────
            c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3,
                                fill="", outline="#45475a", width=3)
            c.create_rectangle(x0, y0, x1, y1,
                                fill="", outline="#6c7086", width=1)

            # ── Frame label ───────────────────────────────────────────────
            cx = x0 + win_w // 2
            c.create_text(cx, y1 + 11, text=cfg["name"],
                          fill=DIM, font=("Segoe UI", 8))

            # ── Gap % label in window ─────────────────────────────────────
            gap_px  = max(0, bot_edge - top_edge)
            gap_pct = int(gap_px / win_h * 100)
            mid_y   = (max(top_edge, y0) + min(bot_edge, y1)) / 2
            if gap_px > 14:
                c.create_text(cx, mid_y, text=f"{gap_pct}%",
                              fill="#1e1e2e", font=("Segoe UI", 7, "bold"))

        # ── Beat phase bar (shown when BPM sync is active) ───────────────
        if self._bpm_on.get():
            phase = self._beat % 1.0
            bar_w = int(phase * W)
            c.create_rectangle(0, H - 3, bar_w, H, fill=BLUE, outline="")

        # Schedule continuous redraws while visible
        self.after(17, self._draw_preview)

    # ── Gap control ──────────────────────────────────────────────────────────

    def _build_gap_section(self, parent=None):
        if parent is None:
            parent = self
        self._param_block(
            parent=parent,
            title="GAP POSITION   (0 % = bottom  •  100 % = top)",
            var=self._gap_pos_var, vmax=100, unit="%",
            presets=[("Bottom", 0), ("Centre", 50), ("Top", 100)],
            sync_attr="_pos_sync_btns",  set_sync_fn=self._set_pos_sync,
            pat_attr="_pos_pbts",        set_pat_fn=self._set_pos_pattern)

        _hr(parent)

        # Gap Size is a small % of the (tall) window — only a few % is useful.
        self._param_block(
            parent=parent,
            title="GAP SIZE   (% of window the band opens)",
            var=self._gap_size_var, vmax=25, unit="%",
            presets=[("Closed", 0), ("2%", 2), ("5%", 5),
                     ("10%", 10), ("15%", 15), ("25%", 25)],
            sync_attr="_size_sync_btns", set_sync_fn=self._set_size_sync,
            pat_attr="_size_pbts",       set_pat_fn=self._set_size_pattern)

        # Closed-overlap fine-tune + a full-open park button
        ov = tk.Frame(parent, bg=BG)
        ov.pack(padx=20, pady=(2, 4), fill="x")
        tk.Label(ov, text="Overlap:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=8, anchor="w").pack(side="left")
        ttk.Scale(ov, from_=0, to=30, orient="horizontal",
                  variable=self._overlap_var, length=170).pack(side="left", padx=4)
        ov_lbl = tk.Label(ov, text="  0%", font=("Segoe UI", 9, "bold"),
                          bg=BG, fg=GREEN, width=6)
        ov_lbl.pack(side="left", padx=(0, 6))
        self._overlap_var.trace_add("write",
            lambda *_: ov_lbl.config(text=f"{int(round(self._overlap_var.get()))}%"))
        tk.Label(ov, text="(blinds overlap when closed — light-tight)",
                 font=("Segoe UI", 8), bg=BG, fg=DIM).pack(side="left")
        open_btn = tk.Button(ov, text="⤢ Open 100%", font=("Segoe UI", 8),
                             bg=BTN, fg=BLUE, relief="flat", padx=8, pady=3,
                             cursor="hand2", command=self._open_full)
        open_btn.pack(side="right")
        _hov(open_btn)

        # Manual-mode traces: push static values to all frames when sliders move
        for v in (self._gap_pos_var, self._gap_size_var, self._overlap_var):
            v.trace_add("write", lambda *_: (
                None if self._bpm_on.get() else self._push_from_gap_controls()))

    def _param_block(self, title, var, vmax, unit, presets,
                     sync_attr, set_sync_fn, pat_attr, set_pat_fn, parent=None):
        """One self-contained block: value slider (0..vmax, labelled with `unit`)
        + presets, beat-sync selector, and an independent pattern selector."""
        if parent is None:
            parent = self
        sec = tk.Frame(parent, bg=BG)
        sec.pack(padx=20, pady=(4, 4), fill="x")

        tk.Label(sec, text=title, font=("Segoe UI", 10, "bold"),
                 bg=BG, fg=FG).pack(anchor="w", pady=(0, 4))

        def _fmt(v):
            return f"{int(round(v)):3d}{unit}"

        # Value slider + readout + presets
        r = tk.Frame(sec, bg=BG)
        r.pack(fill="x", pady=2)
        tk.Label(r, text="Amount:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=8, anchor="w").pack(side="left")
        ttk.Scale(r, from_=0, to=vmax, orient="horizontal",
                  variable=var, length=170).pack(side="left", padx=4)
        val_lbl = tk.Label(r, text=_fmt(var.get()), font=("Segoe UI", 9, "bold"),
                           bg=BG, fg=GREEN, width=6)
        val_lbl.pack(side="left", padx=(0, 10))
        var.trace_add("write",
            lambda *_, v=var, l=val_lbl: l.config(text=_fmt(v.get())))
        for lbl, pv in presets:
            b = tk.Button(r, text=lbl, font=("Segoe UI", 8), bg=BTN, fg=BTNFG,
                          relief="flat", padx=6, pady=2, cursor="hand2",
                          command=lambda p=pv, vv=var: vv.set(p))
            b.pack(side="left", padx=1)
            _hov(b)

        # Beat-sync selector
        sr = tk.Frame(sec, bg=BG)
        sr.pack(fill="x", pady=2)
        tk.Label(sr, text="Beats:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=8, anchor="w").pack(side="left")
        sbtns = []
        for lbl, beats in BEAT_OPTIONS:
            b = tk.Button(sr, text=lbl, font=("Segoe UI", 8), bg=BTN, fg=BTNFG,
                          relief="flat", padx=6, pady=2, cursor="hand2",
                          command=lambda v=beats: set_sync_fn(v))
            b.pack(side="left", padx=1)
            sbtns.append(b)
        setattr(self, sync_attr, sbtns)
        set_sync_fn(None)   # default: Off

        # Pattern selector
        pr = tk.Frame(sec, bg=BG)
        pr.pack(fill="x", pady=2)
        tk.Label(pr, text="Pattern:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=8, anchor="w").pack(side="left")
        pbts = {}
        for name, fn in PATTERNS:
            b = tk.Button(pr, text=name, font=("Segoe UI", 8), bg=BTN, fg=BTNFG,
                          relief="flat", padx=6, pady=2, cursor="hand2",
                          command=lambda f=fn, nm=name: set_pat_fn(f, nm))
            b.pack(side="left", padx=1)
            pbts[name] = b
        setattr(self, pat_attr, pbts)
        set_pat_fn(PATTERNS[0][1], PATTERNS[0][0])   # default: Uniform

    # ── BPM section ──────────────────────────────────────────────────────────

    def _build_bpm_section(self, parent=None):
        if parent is None:
            parent = self
        sec = tk.Frame(parent, bg=BG)
        sec.pack(padx=20, pady=(4, 0), fill="x")

        # Header row
        hdr = tk.Frame(sec, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="BPM SYNC",
                 font=("Segoe UI", 10, "bold"), bg=BG, fg=FG).pack(side="left")
        tk.Checkbutton(hdr, text="Enable", variable=self._bpm_on,
                       font=("Segoe UI", 9), bg=BG, fg=BLUE,
                       selectcolor=CARD, activebackground=BG).pack(side="left", padx=12)
        self._link_lbl = tk.Label(hdr, text="", font=("Segoe UI", 9), bg=BG, fg=DIM)
        self._link_lbl.pack(side="right")
        link_cb = tk.Checkbutton(hdr, text="Ableton Link", variable=self._link_on,
                                 font=("Segoe UI", 9), bg=BG, fg=BLUE,
                                 selectcolor=CARD, activebackground=BG,
                                 command=self._resync)   # re-zero phase on switch
        if not LINK_AVAILABLE:
            link_cb.config(state="disabled")
        link_cb.pack(side="right", padx=10)

        # BPM slider + tap
        bpm_row = tk.Frame(sec, bg=BG)
        bpm_row.pack(fill="x", pady=(10, 4))
        tk.Label(bpm_row, text="BPM:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=5, anchor="w").pack(side="left")
        ttk.Scale(bpm_row, from_=40, to=240, orient="horizontal",
                  variable=self._bpm_var, length=200).pack(side="left", padx=4)
        bpm_val = tk.Label(bpm_row, text="120.00", font=("Segoe UI", 11, "bold"),
                           bg=BG, fg=YELLOW, width=7)
        bpm_val.pack(side="left")

        def _on_bpm(*_):
            v = self._bpm_var.get()
            bpm_val.config(text=f"{v:.2f}")
            self._clock.bpm = v
        self._bpm_var.trace_add("write", _on_bpm)

        tap = tk.Button(bpm_row, text="Tap Tempo",
                        font=("Segoe UI", 9), bg=BTN, fg=BTNFG,
                        relief="flat", padx=10, pady=4, cursor="hand2",
                        command=self._tap)
        tap.pack(side="left", padx=12)
        _hov(tap)

        resync = tk.Button(bpm_row, text="⟳ Resync",
                           font=("Segoe UI", 9), bg=BTN, fg=BTNFG,
                           relief="flat", padx=10, pady=4, cursor="hand2",
                           command=self._resync)
        resync.pack(side="left", padx=2)
        _hov(resync)

        tk.Label(bpm_row, text="Nudge:", font=("Segoe UI", 9),
                 bg=BG, fg=DIM).pack(side="left", padx=(14, 2))
        for symbol, beats in (("◀", -0.0625), ("▶", 0.0625)):
            b = tk.Button(bpm_row, text=symbol,
                          font=("Segoe UI", 10, "bold"), bg=BTN, fg=BTNFG,
                          relief="flat", padx=9, pady=4, cursor="hand2",
                          command=lambda v=beats: self._nudge(v))
            b.pack(side="left", padx=1)
            _hov(b)

        # Speed row
        ds = tk.Frame(sec, bg=BG)
        ds.pack(fill="x", pady=4)
        tk.Label(ds, text="Speed:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=6, anchor="w").pack(side="left")
        for idx, (label, _) in enumerate(SPEEDS):
            b = tk.Button(ds, text=label, font=("Segoe UI", 8),
                          bg=BTN, fg=BTNFG, relief="flat",
                          padx=7, pady=3, cursor="hand2",
                          command=lambda i=idx: self._set_speed(i))
            b.pack(side="left", padx=2)
            self._sbts.append(b)
        self._set_speed(3)   # 1× default — beat selectors map to literal beats

        # Motor speed row (slew-rate cap)
        ms = tk.Frame(sec, bg=BG)
        ms.pack(fill="x", pady=4)
        tk.Label(ms, text="Motor:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=6, anchor="w").pack(side="left")
        ttk.Scale(ms, from_=5, to=100, orient="horizontal",
                  variable=self._max_spd, length=160).pack(side="left", padx=4)
        mspd_lbl = tk.Label(ms, text=" 30%/s", font=("Segoe UI", 9, "bold"),
                             bg=BG, fg=GREEN, width=7)
        mspd_lbl.pack(side="left")
        tk.Label(ms, text="max speed  (lower = smoother for slow motors)",
                 font=("Segoe UI", 8), bg=BG, fg=DIM).pack(side="left", padx=4)
        self._max_spd.trace_add("write",
            lambda *_: mspd_lbl.config(text=f"{int(self._max_spd.get()):3d}%/s"))

    # ═════════════════════════════════════════════════════════════════════════
    # Logic
    # ═════════════════════════════════════════════════════════════════════════

    def _refresh_lbl(self, idx, key):
        self._plbls[idx][key].config(
            text=f"{int(self._pvars[idx][key].get()):3d}%")

    def _send_frame(self):
        """Build and send the full 16-channel universe from current positions."""
        if self._suspend_send:
            return
        positions = [(pv["b"].get(), pv["t"].get()) for pv in self._pvars]
        send_universe([v.get() for v in self._ip_vars], positions)

    def _push_from_gap_controls(self):
        """Push current Gap Position + Gap Size to all frame sliders."""
        pos     = max(0.0, min(1.0, self._gap_pos_var.get()  / 100.0))
        size    = max(0.0, min(1.0, self._gap_size_var.get() / 100.0))
        overlap = self._overlap_var.get() / 100.0
        cover   = (1.0 + overlap) * (1.0 - size)   # >1 when closed → blinds overlap
        self._suspend_send = True
        for pv in self._pvars:
            pv["t"].set(max(0.0, min(100.0, (1.0 - pos) * cover * 100.0)))
            pv["b"].set(max(0.0, min(100.0, pos * cover * 100.0)))
        self._suspend_send = False
        self._send_frame()

    def _open_full(self):
        """Park all blinds fully open (both retracted to 0 %). Manual use; a
        running BPM animation will override on the next tick."""
        self._suspend_send = True
        for pv in self._pvars:
            pv["b"].set(0.0)
            pv["t"].set(0.0)
        self._suspend_send = False
        self._send_frame()

    def _set_size_sync(self, beats: "float | None"):
        self._size_sync_beats = beats
        # beats=None is only ever passed when the user pressed "Still" — that
        # makes Still the active pattern-row selection. Beat clicks pass a
        # number and must not touch _size_pat_selected.
        if beats is None:
            self._size_pat_selected = "Still"
        for btn, (_, v) in zip(self._size_sync_btns, BEAT_OPTIONS):
            btn.config(bg=BTNSEL if v == beats else BTN,
                       fg=BTNSELFG if v == beats else BTNFG)
        self._refresh_apc_leds()
        self._refresh_apc_btn_colors()

    def _set_pos_sync(self, beats: "float | None"):
        self._pos_sync_beats = beats
        if beats is None:
            self._pos_pat_selected = "Still"
        for btn, (_, v) in zip(self._pos_sync_btns, BEAT_OPTIONS):
            btn.config(bg=BTNSEL if v == beats else BTN,
                       fg=BTNSELFG if v == beats else BTNFG)
        self._refresh_apc_leds()
        self._refresh_apc_btn_colors()

    def _tap(self):
        self._bpm_var.set(round(self._clock.tap(), 1))

    def _current_raw_beat(self) -> float:
        """Beat from the active source (Link if enabled+available, else clock)."""
        if self._link_on.get() and LINK_AVAILABLE and _link is not None:
            try:
                self._clock.bpm = _lnk_get_tempo()
                return _lnk_get_beat()
            except Exception:
                pass
        return self._clock.beat

    def _resync(self):
        """Reset the pattern phase to 0 — re-homes the animation to its start.
        Works for both Link and the internal clock."""
        now = time.perf_counter()
        self._beat_offset         = -self._current_raw_beat()
        self._resync_time         = now
        self._beats_since_resync  = 0
        self._madmom_beats_seen_t = now
        # Suppress madmom's continuous phase tracker for 2 s after a resync
        # so it doesn't immediately drift the LEDs away from beat 1.
        self._phase_lock_freeze_until = now + 2.0
        log.info("RESYNC at %.3f — effective beat snapped to 0", now)
        if not self._bpm_on.get():
            self._push_from_gap_controls()

    def _nudge(self, beats: float):
        """Shift phase by `beats` — works for Link and clock."""
        self._beat_offset += beats

    def _set_speed(self, idx: int):
        self._speed_idx = idx
        for i, b in enumerate(self._sbts):
            if i == idx:
                b.config(bg=BTNSEL, fg=BTNSELFG)
            else:
                b.config(bg=BTN, fg=BTNFG)

    def _set_pos_pattern(self, fn, name: str):
        self._pos_pat_fn = fn
        self._pos_pat_name = name
        self._pos_pat_selected = name      # mark this pattern as active in the row
        for n, b in self._pos_pbts.items():
            b.config(bg=BTNSEL if n == name else BTN,
                     fg=BTNSELFG if n == name else BTNFG)
        self._refresh_apc_leds()
        self._refresh_apc_btn_colors()

    def _set_size_pattern(self, fn, name: str):
        self._size_pat_fn = fn
        self._size_pat_name = name
        self._size_pat_selected = name
        for n, b in self._size_pbts.items():
            b.config(bg=BTNSEL if n == name else BTN,
                     fg=BTNSELFG if n == name else BTNFG)
        self._refresh_apc_leds()
        self._refresh_apc_btn_colors()

    def _on_bpm_toggle(self, *_):
        if self._bpm_on.get():
            # Sync slew tracker to current slider positions so the first tick
            # doesn't see a huge "jump" from 0,0 and rate-limit away from it.
            self._cur_pos = [
                (self._pvars[i]["b"].get(), self._pvars[i]["t"].get())
                for i in range(len(FRAMES))
            ]
            self._last_apply = time.perf_counter()
        self._refresh_apc_leds()
        self._refresh_apc_btn_colors()

    # ── Art-Net broadcast helpers ─────────────────────────────────────────────

    def _get_broadcast_addr(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            parts = ip.split(".")
            parts[3] = "255"
            return ".".join(parts)
        except Exception:
            return "255.255.255.255"

    def _set_broadcast(self):
        addr = self._get_broadcast_addr()
        for v in self._ip_vars:
            v.set(addr)
        self._artnet_status.config(text=f"Broadcasting → {addr}", fg=YELLOW)

    def _restore_ips(self):
        for v, f in zip(self._ip_vars, FRAMES):
            v.set(f["ip"])
        self._artnet_status.config(text="Unicast to individual IPs", fg=DIM)

    # ── Calibration + status telemetry (OSC) ──────────────────────────────────

    def _calibrate_window(self, idx: int):
        """Trigger calibration of both screens on one window via OSC."""
        osc_send(self._ip_vars[idx].get(), "/calibrate")
        log.info("Calibrate requested: window %d (%s)", idx + 1, self._ip_vars[idx].get())

    def _calibrate_all(self):
        for v in dict.fromkeys(self._ip_vars[i].get() for i in range(len(FRAMES))):
            osc_send(v, "/calibrate")
        log.info("Calibrate requested: all windows")

    def _telemetry_loop(self):
        """Poll each window for status and store replies (mapped by source IP)."""
        while True:
            try:
                # Map replies by source IP. Include both the (user-editable) send
                # targets and the default device IPs, so replies are recognised
                # even when sending to a broadcast address.
                reply_map = {}
                poll_targets = []
                for i in range(len(FRAMES)):
                    tgt = self._ip_vars[i].get()
                    poll_targets.append(tgt)
                    reply_map[tgt] = i
                    reply_map[FRAMES[i]["ip"]] = i
                for ip in dict.fromkeys(poll_targets):
                    osc_send(ip, "/status")
                # Collect replies for a short window
                deadline = time.perf_counter() + 0.25
                while time.perf_counter() < deadline:
                    try:
                        data, addr = _osc_sock.recvfrom(512)
                    except (socket.timeout, OSError):
                        break
                    a, args = _osc_parse(data)
                    idx = reply_map.get(addr[0])
                    if a == "/status" and idx is not None and len(args) >= 8:
                        st = self._status[idx]
                        st["cal"]   = [args[0], args[4]]
                        st["homed"] = [args[1], args[5]]
                        st["max"]   = [args[2], args[6]]
                        st["pos"]   = [args[3], args[7]]
                        st["seen"]  = time.perf_counter()
            except Exception:
                pass
            time.sleep(0.3)

    def _refresh_status_labels(self):
        now = time.perf_counter()
        for i, lbl in enumerate(self._stat_lbls):
            st = self._status[i]
            online = (now - st["seen"]) < 2.0
            if not online:
                lbl.config(text="offline", fg=RED)
            else:
                def line(j, name):
                    if st["cal"][j]:
                        return f"{name} {st['pos'][j]:>6} / {st['max'][j]} ✓"
                    return f"{name}  not calibrated"
                lbl.config(text=line(0, "B") + "\n" + line(1, "T"),
                           fg=GREEN if (st["cal"][0] and st["cal"][1]) else YELLOW)
        self.after(300, self._refresh_status_labels)

    # ── Audio BPM section ────────────────────────────────────────────────────

    def _build_audio_section(self, parent=None):
        if parent is None:
            parent = self
        sec = tk.Frame(parent, bg=BG)
        sec.pack(padx=20, pady=(4, 0), fill="x")

        # Header
        hdr = tk.Frame(sec, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="AUDIO BPM",
                 font=("Segoe UI", 10, "bold"), bg=BG, fg=FG).pack(side="left")
        if AUDIO_AVAILABLE:
            parts = []
            parts.append("aubio" if _AUBIO_OK else "numpy fallback  (pip install aubio)")
            parts.append("pyaudiowpatch" if _PAW_OK else "no loopback  (pip install pyaudiowpatch)")
            lib = " · ".join(parts)
            tk.Label(hdr, text=f"({lib})",
                     font=("Segoe UI", 8), bg=BG, fg=DIM).pack(side="left", padx=8)
        else:
            tk.Label(hdr, text="→  pip install sounddevice numpy aubio",
                     font=("Segoe UI", 8), bg=BG, fg=RED).pack(side="left", padx=8)
            return   # nothing else to build when deps are missing

        # Device row
        dev_row = tk.Frame(sec, bg=BG)
        dev_row.pack(fill="x", pady=(8, 4))
        tk.Label(dev_row, text="Device:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=7, anchor="w").pack(side="left")
        self._audio_devs = get_audio_devices()
        dev_names = [d[0] for d in self._audio_devs] if self._audio_devs else ["(no devices found)"]
        # Prefer the Roland Rubix loopback if present; otherwise the first
        # loopback device; otherwise whatever's first in the list.
        default = next(
            (n for n in dev_names if "rubix" in n.lower()
             and "loopback" in n.lower()),
            None)
        if default is None:
            default = next(
                (n for n in dev_names if "loopback" in n.lower()),
                None)
        self._audio_dev_var = tk.StringVar(value=default or dev_names[0])
        ttk.Combobox(dev_row, textvariable=self._audio_dev_var,
                     values=dev_names, width=28, state="readonly").pack(side="left", padx=4)
        self._audio_btn = tk.Button(dev_row, text="Start Audio",
                                    font=("Segoe UI", 9), bg=BTN, fg=BTNFG,
                                    relief="flat", padx=10, pady=4, cursor="hand2",
                                    command=self._toggle_audio)
        self._audio_btn.pack(side="left", padx=8)
        _hov(self._audio_btn)

        # Level meter row
        lv = tk.Frame(sec, bg=BG)
        lv.pack(fill="x", pady=(2, 0))
        tk.Label(lv, text="Level:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=7, anchor="w").pack(side="left")
        self._level_canvas = tk.Canvas(lv, bg=CARD, height=12, highlightthickness=0)
        self._level_canvas.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._level_lbl = tk.Label(lv, text="–", font=("Segoe UI", 8),
                                    bg=BG, fg=DIM, width=6, anchor="w")
        self._level_lbl.pack(side="left")

        # Status row
        st = tk.Frame(sec, bg=BG)
        st.pack(fill="x", pady=(4, 4))
        tk.Label(st, text="Detected:", font=("Segoe UI", 9),
                 bg=BG, fg=FG, width=9, anchor="w").pack(side="left")
        self._audio_bpm_lbl = tk.Label(st, text="---",
                                        font=("Segoe UI", 14, "bold"), bg=BG, fg=YELLOW, width=7)
        self._audio_bpm_lbl.pack(side="left")
        tk.Label(st, text="BPM", font=("Segoe UI", 9),
                 bg=BG, fg=DIM).pack(side="left", padx=(2, 12))
        self._audio_conf_lbl = tk.Label(st, text="(waiting for audio)",
                                         font=("Segoe UI", 9), bg=BG, fg=DIM, anchor="w",
                                         wraplength=200, justify="left")
        self._audio_conf_lbl.pack(side="left", fill="x", expand=True)

        # madmom is the sole BPM detector — no toggle, just an availability note
        from .bpm_madmom import is_available as _madmom_available
        if not _madmom_available():
            tk.Label(sec, text="madmom not available — BPM detection disabled",
                     font=("Segoe UI", 8), bg=BG, fg=RED).pack(anchor="w",
                                                                pady=(2, 0))

        self.after(100, self._poll_audio_level)

    # ── Beat counter + phase meter + waveform analyzer ───────────────────────

    def _build_big_spectrum(self, parent=None):
        """Large spectrum analyzer that fills the space above the APC40 canvas.
        Band controls remain in the right column.

        Prefers a native OpenGL panel (glfw child window inside a tk Frame)
        which renders directly to the swap chain and avoids the ~10 ms cost
        of the PhotoImage upload.  Falls back to a tk.Canvas + PhotoImage
        path if glfw / Win32 reparenting isn't available."""
        if parent is None:
            parent = self
        sec = tk.Frame(parent, bg=BG)
        sec.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 4))

        hdr = tk.Frame(sec, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="SPECTRUM ANALYZER",
                 font=("Segoe UI", 11, "bold"), bg=BG, fg=FG).pack(side="left")
        tk.Label(hdr, text="  log frequency · dB FS · peak-hold smoothing",
                 font=("Segoe UI", 8), bg=BG, fg=DIM).pack(side="left")

        # Holder Frame — host for either the native GL child window OR the
        # fallback tk.Canvas.  Border colour matches the previous canvas.
        spec_holder = tk.Frame(sec, bg="#0d0d17", highlightthickness=1,
                                highlightbackground=BTN)
        spec_holder.pack(fill="both", expand=True, pady=(4, 0))
        self._spec_holder = spec_holder

        # Try to embed a native GLFW child window via Win32 SetParent.
        from .gl_native_spectrum import try_create as _try_native_spec
        spec_holder.update_idletasks()
        init_w = max(800, spec_holder.winfo_width())
        init_h = max(150, spec_holder.winfo_height())
        self._native_spec = _try_native_spec(spec_holder, init_w, init_h)

        if self._native_spec is not None:
            # Resize the embedded child window whenever the host frame resizes
            def _on_resize(event, ns=self._native_spec):
                ns.set_geometry(event.width, event.height)
            spec_holder.bind("<Configure>", _on_resize)
            # No tk.Canvas / PhotoImage path is needed in this mode.
            self._spec_canvas = None
            log.info("Spectrum: native GL panel active (no PhotoImage upload)")
        else:
            # Fallback path — tk.Canvas with PIL-baked bitmap
            self._spec_canvas = tk.Canvas(spec_holder, bg="#0d0d17",
                                           highlightthickness=0)
            self._spec_canvas.pack(fill="both", expand=True)
            log.info("Spectrum: tk.Canvas fallback (PhotoImage upload)")

    def _build_beat_visualizer(self, parent=None):
        if parent is None:
            parent = self
        sec = tk.Frame(parent, bg=BG)
        sec.pack(padx=20, pady=(4, 0), fill="x")

        tk.Label(sec, text="BEAT VISUALIZER",
                 font=("Segoe UI", 10, "bold"), bg=BG, fg=FG).pack(anchor="w", pady=(0, 6))

        # ── Beat counter: 4 boxes labelled 1–4 + total count since resync ──
        beat_row = tk.Frame(sec, bg=BG)
        beat_row.pack(fill="x", pady=(0, 4))
        tk.Label(beat_row, text="Beat:", font=("Segoe UI", 9),
                 bg=BG, fg=DIM, width=6, anchor="w").pack(side="left")
        self._beat_boxes = []
        for i in range(4):
            box = tk.Canvas(beat_row, width=46, height=32,
                            bg=BTN, highlightthickness=1,
                            highlightbackground=DIM)
            box.pack(side="left", padx=3)
            lbl = box.create_text(23, 16, text=str(i + 1),
                                  fill=FG, font=("Segoe UI", 13, "bold"))
            self._beat_boxes.append((box, lbl))

        # Counter row: detected beats and clock beats since resync
        ctr_row = tk.Frame(sec, bg=BG)
        ctr_row.pack(fill="x", pady=(0, 6))
        tk.Label(ctr_row, text="Since resync:", font=("Segoe UI", 8),
                 bg=BG, fg=DIM, width=12, anchor="w").pack(side="left")
        self._beats_detected_lbl = tk.Label(ctr_row,
            text="detected: — ", font=("Segoe UI", 8, "bold"), bg=BG, fg=GREEN)
        self._beats_detected_lbl.pack(side="left", padx=(0, 8))
        self._beats_clock_lbl = tk.Label(ctr_row,
            text="clock: — ", font=("Segoe UI", 8), bg=BG, fg=DIM)
        self._beats_clock_lbl.pack(side="left", padx=(0, 8))
        self._beats_drift_lbl = tk.Label(ctr_row,
            text="drift: — ", font=("Segoe UI", 8), bg=BG, fg=YELLOW)
        self._beats_drift_lbl.pack(side="left")

        # ── Phase meter bar ──────────────────────────────────────────────────
        phase_row = tk.Frame(sec, bg=BG)
        phase_row.pack(fill="x", pady=(0, 6))
        tk.Label(phase_row, text="Phase:", font=("Segoe UI", 9),
                 bg=BG, fg=DIM, width=6, anchor="w").pack(side="left")
        self._phase_canvas = tk.Canvas(phase_row, height=18, bg=CARD,
                                       highlightthickness=0)
        self._phase_canvas.pack(side="left", fill="x", expand=True)
        self._phase_bar  = self._phase_canvas.create_rectangle(
            0, 0, 0, 18, fill=BLUE, outline="")
        self._phase_tick = self._phase_canvas.create_line(
            0, 0, 0, 18, fill=YELLOW, width=2)

        # ── Waveform canvas ──────────────────────────────────────────────────
        tk.Label(sec, text="Waveform  (last 16 beats)",
                 font=("Segoe UI", 8), bg=BG, fg=DIM).pack(anchor="w")
        self._wave_canvas = tk.Canvas(sec, height=110, bg="#0d0d17",
                                      highlightthickness=1,
                                      highlightbackground=BTN)
        self._wave_canvas.pack(fill="x", pady=(2, 0))

        # ── Band isolation controls ──────────────────────────────────────────
        # (The full-size spectrum canvas lives above the APC40 — see _build_big_spectrum.)
        # The Kick + Hi-hat bands shown here are what aubio sees for beat detection.
        # Coloured overlays on the spectrum show the active filter ranges.
        det = self._audio_det
        def _dv(attr, default):
            return tk.DoubleVar(value=getattr(det, attr) if det else default)
        self._kick_low_var    = _dv("DEFAULT_KICK_LOW",    40.0)
        self._kick_high_var   = _dv("DEFAULT_KICK_HIGH",   180.0)
        self._hihat_low_var   = _dv("DEFAULT_HIHAT_LOW",   5000.0)
        self._hihat_high_var  = _dv("DEFAULT_HIHAT_HIGH",  12000.0)
        self._kick_weight_var = _dv("DEFAULT_KICK_WEIGHT", 3.0)

        bands = tk.Frame(sec, bg=BG)
        bands.pack(fill="x", pady=(6, 0))
        self._make_band_sliders(bands, "Kick", "#ffa500",
                                self._kick_low_var,  10,   500,
                                self._kick_high_var, 60,  1000, row=0)
        self._make_band_sliders(bands, "Hi-hat", "#00bfff",
                                self._hihat_low_var,  500, 12000,
                                self._hihat_high_var, 2000, 18000, row=1)

        # Wire band sliders + kick weight → audio detector
        for v in (self._kick_low_var, self._kick_high_var,
                  self._hihat_low_var, self._hihat_high_var,
                  self._kick_weight_var):
            v.trace_add("write", self._apply_band_settings)

        # ── Mix weight row ──────────────────────────────────────────────────
        ctrl = tk.Frame(sec, bg=BG)
        ctrl.pack(fill="x", pady=(8, 0))
        tk.Label(ctrl, text="Kick wt:", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg="#ffa500").pack(side="left")
        ttk.Scale(ctrl, from_=0.5, to=6.0, orient="horizontal",
                  variable=self._kick_weight_var, length=90).pack(side="left", padx=4)
        self._kick_weight_lbl = tk.Label(ctrl, text="3.0×",
                                          font=("Segoe UI", 8, "bold"),
                                          bg=BG, fg="#ffa500", width=5)
        self._kick_weight_lbl.pack(side="left", padx=(0, 8))
        self._kick_weight_var.trace_add("write",
            lambda *_: self._kick_weight_lbl.config(
                text=f"{self._kick_weight_var.get():.1f}×"))
        self._kick_weight_lbl.config(text=f"{self._kick_weight_var.get():.1f}×")

        self._auto_resync_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="Auto-resync (15 s)",
                       variable=self._auto_resync_var,
                       font=("Segoe UI", 8), bg=BG, fg=BLUE,
                       selectcolor=CARD, activebackground=BG).pack(side="left")

        # Kick status indicator on its own row so long text never overflows
        kick_row = tk.Frame(sec, bg=BG)
        kick_row.pack(fill="x", pady=(2, 0))
        self._last_kick_lbl = tk.Label(kick_row, text="", font=("Segoe UI", 8),
                                        bg=BG, fg=DIM, anchor="w")
        self._last_kick_lbl.pack(side="left", fill="x", expand=True)

        self.after(30, self._update_beat_viz)

    def _make_band_sliders(self, parent, name, color,
                            low_var, low_from, low_to,
                            high_var, high_from, high_to, row):
        """One row: [label]  Low [slider] [Hz]   High [slider] [Hz]
        Sized to fit a ~450 px right column on 1920×1080."""
        tk.Label(parent, text=f"{name}:", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=color, width=6, anchor="w").grid(row=row, column=0,
                                                              padx=(0, 2), pady=2)
        tk.Label(parent, text="Lo", font=("Segoe UI", 8),
                 bg=BG, fg=DIM).grid(row=row, column=1)
        ttk.Scale(parent, from_=low_from, to=low_to, orient="horizontal",
                  variable=low_var, length=80).grid(row=row, column=2, padx=2)
        lo_lbl = tk.Label(parent, text="", font=("Segoe UI", 8, "bold"),
                          bg=BG, fg=color, width=6, anchor="w")
        lo_lbl.grid(row=row, column=3, padx=(2, 6))
        low_var.trace_add("write",
            lambda *_: lo_lbl.config(text=self._fmt_hz(low_var.get())))
        lo_lbl.config(text=self._fmt_hz(low_var.get()))

        tk.Label(parent, text="Hi", font=("Segoe UI", 8),
                 bg=BG, fg=DIM).grid(row=row, column=4)
        ttk.Scale(parent, from_=high_from, to=high_to, orient="horizontal",
                  variable=high_var, length=80).grid(row=row, column=5, padx=2)
        hi_lbl = tk.Label(parent, text="", font=("Segoe UI", 8, "bold"),
                          bg=BG, fg=color, width=6, anchor="w")
        hi_lbl.grid(row=row, column=6, padx=(2, 0))
        high_var.trace_add("write",
            lambda *_: hi_lbl.config(text=self._fmt_hz(high_var.get())))
        hi_lbl.config(text=self._fmt_hz(high_var.get()))

    @staticmethod
    def _fmt_hz(v: float) -> str:
        return f"{int(v)} Hz" if v < 1000 else f"{v/1000:.1f} kHz"

    def _apply_band_settings(self, *_):
        det = self._audio_det
        if det is None:
            return
        kl, kh = self._kick_low_var.get(),   self._kick_high_var.get()
        hl, hh = self._hihat_low_var.get(),  self._hihat_high_var.get()
        # Enforce min separation so the bandpasses don't collapse
        if kh <= kl + 10:
            self._kick_high_var.set(kl + 10);  return
        if hh <= hl + 100:
            self._hihat_high_var.set(hl + 100); return
        det.set_bands(kick_low=kl, kick_high=kh,
                      hihat_low=hl, hihat_high=hh,
                      kick_weight=self._kick_weight_var.get())

    def _update_beat_viz(self):
        """Redraws the beat counter, phase meter, waveform and spectrum at
        ~60 fps.  Both the waveform and the spectrum are rendered as single
        bitmap blits (no per-canvas-item churn), so the budget fits."""
        try:
            self._draw_beat_viz()
        except Exception:
            pass
        self.after(16, self._update_beat_viz)

    def _draw_beat_viz(self):
        bpm_on = self._bpm_on.get()
        beat   = (self._current_raw_beat() + self._beat_offset) if bpm_on else 0.0
        frac   = beat % 1.0
        beat_n = int(beat) % 4   # 0-3

        # ── Kick status indicator ────────────────────────────────────────────
        if self._last_kick_time > 0:
            gap = time.perf_counter() - self._last_kick_time
            if gap < 2.0:
                self._last_kick_lbl.config(text=f"kick {gap:.1f}s", fg=GREEN)
            elif gap < 15.0:
                self._last_kick_lbl.config(text=f"kick {gap:.0f}s ago", fg=YELLOW)
            else:
                self._last_kick_lbl.config(text=f"WAITING DROP ({gap:.0f}s)", fg=RED)
        else:
            self._last_kick_lbl.config(text="no kick yet", fg=DIM)

        # ── Beat counter labels ──────────────────────────────────────────────
        detected = self._beats_since_resync
        if self._resync_time > 0 and bpm_on:
            bpm_now  = self._bpm_var.get()
            clock_beats = int((time.perf_counter() - self._resync_time)
                              * bpm_now / 60.0)
            drift = detected - clock_beats
            drift_col = RED if abs(drift) > 4 else (YELLOW if abs(drift) > 1 else GREEN)
            self._beats_detected_lbl.config(text=f"detected: {detected}")
            self._beats_clock_lbl.config(text=f"clock: {clock_beats}")
            self._beats_drift_lbl.config(
                text=f"drift: {drift:+d}", fg=drift_col)
        else:
            self._beats_detected_lbl.config(text="detected: —")
            self._beats_clock_lbl.config(text="clock: —")
            self._beats_drift_lbl.config(text="drift: —", fg=YELLOW)

        # ── Beat boxes ───────────────────────────────────────────────────────
        for i, (box, lbl) in enumerate(self._beat_boxes):
            if bpm_on and i == beat_n:
                box.config(bg=BLUE, highlightbackground=BLUE)
                box.itemconfig(lbl, fill="#1e1e2e")
            else:
                box.config(bg=BTN, highlightbackground=DIM)
                box.itemconfig(lbl, fill=FG)

        # ── Phase bar ────────────────────────────────────────────────────────
        pw = self._phase_canvas.winfo_width()
        if pw > 1:
            fill_x = int(pw * frac)
            self._phase_canvas.coords(self._phase_bar, 0, 0, fill_x, 18)
            self._phase_canvas.coords(self._phase_tick,
                                      fill_x, 0, fill_x, 18)

        # ── Waveform + Spectrum (bitmap-rendered) ────────────────────────────
        det = self._audio_det
        if det is None or not det.running:
            return
        self._draw_waveform(det)
        self._draw_spectrum(det)

    # ── Bitmap-rendered waveform ───────────────────────────────────────────
    # Same architecture as the spectrum: render to a numpy uint8 buffer, push
    # one PhotoImage to the canvas per frame. Replaces the polygon-based path
    # that pegged at ~15-20 fps because of canvas-item overhead.
    WAVE_BG_RGB    = np.array([13, 13, 23],   dtype=np.uint8)   # match spec
    WAVE_BAR_RGB   = np.array([137, 180, 250], dtype=np.uint8)  # bright blue
    WAVE_TICK_RGB  = np.array([42, 42, 74],   dtype=np.uint8)   # beat ticks
    WAVE_MARKER_RGB= np.array([249, 226, 175], dtype=np.uint8)  # YELLOW

    def _init_wave_bitmap(self, cw: int, ch: int):
        if not _PIL_OK:
            return False
        self._wave_buf_arr = np.empty((ch, cw, 3), dtype=np.uint8)
        self._wave_pil     = _PILImage.fromarray(self._wave_buf_arr, mode="RGB")
        self._wave_photo   = _PILImageTk.PhotoImage(self._wave_pil)
        c = self._wave_canvas
        if self._wave_image_id is not None:
            try: c.delete(self._wave_image_id)
            except Exception: pass
        self._wave_image_id = c.create_image(0, 0, anchor="nw",
                                              image=self._wave_photo)
        self._wave_y_grid = np.arange(ch, dtype=np.int32).reshape(-1, 1)
        # Pre-bake the dark alternating beat bands (positions never move)
        bg = np.empty((ch, cw, 3), dtype=np.uint8)
        bg[:] = self.WAVE_BG_RGB
        for b in range(17):
            x0 = int(b / 16 * cw)
            x1 = int((b + 1) / 16 * cw)
            bg[:, x0:x1] = (15, 15, 31) if b % 2 == 0 else (18, 18, 31)
        self._wave_static_bg = bg
        return True

    def _draw_waveform(self, det):
        if not _PIL_OK:
            return
        wave_buf = det._wave_buf
        if not wave_buf:
            return
        c  = self._wave_canvas
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 4 or ch < 4:
            return
        if (self._wave_buf_arr is None
                or self._wave_buf_arr.shape[0] != ch
                or self._wave_buf_arr.shape[1] != cw):
            if not self._init_wave_bitmap(cw, ch):
                return

        bpm = self._bpm_var.get()
        if bpm < 20:
            bpm = 120.0
        beat_dur   = 60.0 / bpm
        t_now      = time.perf_counter()
        window     = 16.0 * beat_dur
        t_start    = t_now - window
        inv_window = 1.0 / window

        # ── Vectorised bucketing ─────────────────────────────────────────
        data = np.fromiter(
            (v for pair in wave_buf for v in pair),
            dtype=np.float64,
            count=len(wave_buf) * 2,
        ).reshape(-1, 2)
        t_rel = (data[:, 0] - t_start).astype(np.float32)
        mask  = (t_rel >= 0.0) & (t_rel < window)
        if not mask.any():
            return
        xs = (t_rel[mask] * (inv_window * cw)).astype(np.int32)
        np.clip(xs, 0, cw - 1, out=xs)
        buckets = np.zeros(cw, dtype=np.float32)
        np.maximum.at(buckets, xs, data[:, 1][mask].astype(np.float32))
        peak = float(buckets.max()) or 1e-6
        norm_amp = buckets * (1.0 / peak)         # 0..1 per column

        # Beat tick markers — sourced from madmom's beat times (aubio is gone).
        madmom_det = getattr(self, "_madmom_det", None)
        # Atomic snapshot — avoids torn reads while the madmom worker clears
        # and re-extends beat_times during inference.
        beat_source = (madmom_det.snapshot_beat_times()
                       if madmom_det is not None else [])
        markers = np.zeros(cw, dtype=np.float32)
        for bt in list(beat_source):
            if bt >= t_start:
                x = int((bt - t_start) * inv_window * cw)
                if 0 <= x < cw:
                    markers[x] = 1.0

        # ── GPU path (moderngl) ──────────────────────────────────────────
        if self._gl_waveform is not None:
            try:
                self._gl_waveform.resize(cw, ch)
                pixels = self._gl_waveform.render(norm_amp, markers)
                if pixels is not None:
                    self._wave_pil.frombytes(pixels.tobytes())
                    self._wave_photo.paste(self._wave_pil)
                    return
            except Exception as exc:
                log.warning("GL waveform render failed, CPU fallback: %s", exc)
                try: self._gl_waveform.release()
                except Exception: pass
                self._gl_waveform = None

        # ── CPU fallback ────────────────────────────────────────────────
        buf = self._wave_buf_arr
        buf[:] = self._wave_static_bg
        mid_y    = ch // 2
        scale    = (ch * 0.46) / peak
        bar_half = (buckets * scale).astype(np.int32)
        bt_2d    = (mid_y - bar_half).reshape(1, -1)
        bb_2d    = (mid_y + bar_half).reshape(1, -1)
        in_bar   = (self._wave_y_grid >= bt_2d) & (self._wave_y_grid <= bb_2d)
        buf[in_bar] = self.WAVE_BAR_RGB
        for bt in list(beat_source):
            if bt >= t_start:
                x = int((bt - t_start) * inv_window * cw)
                if 0 <= x < cw:
                    buf[:, x] = self.WAVE_TICK_RGB
        buf[:, cw - 1] = self.WAVE_MARKER_RGB
        if cw >= 2:
            buf[:, cw - 2] = self.WAVE_MARKER_RGB
        self._wave_pil.frombytes(buf.tobytes())
        self._wave_photo.paste(self._wave_pil)

    # ── Bitmap-rendered spectrum analyzer ──────────────────────────────────
    # Renders directly into a numpy uint8 buffer and pushes it to the canvas
    # as a single PhotoImage every frame.  Avoids tkinter's per-canvas-item
    # overhead (the killer that capped the previous polygon approach to ~15
    # fps) — easily hits 60+ fps with a 1200×400 canvas.
    #
    # Tuning for percussive content:
    # - Frequency-mapped column tints emphasise kick (red), snare (green),
    #   hi-hat (blue) without temporal smoothing.
    # - Peak-hold trace: 1-pixel line that follows the highest level seen per
    #   column over the last ~500 ms, decaying slowly. Makes transients pop
    #   without making the main bars feel sticky.
    SPEC_FMIN_HZ   = 25.0
    SPEC_FMAX_HZ   = 20000.0
    SPEC_DB_MIN    = -90.0
    SPEC_DB_MAX    = 0.0
    SPEC_BG_RGB    = np.array([13, 13, 23],    dtype=np.uint8)  # #0d0d17
    SPEC_KICK_TINT = np.array([42, 26, 8],     dtype=np.float32)
    SPEC_HH_TINT   = np.array([10, 31, 42],    dtype=np.float32)
    SPEC_PEAK_RGB  = np.array([240, 245, 255], dtype=np.uint8)  # peak-hold trace
    SPEC_OUTLINE_RGB = np.array([210, 220, 245], dtype=np.uint8) # bar-top outline

    def _init_spec_bitmap(self, cw: int, ch: int):
        """(Re)allocate the spectrum bitmap and per-canvas caches."""
        if not _PIL_OK:
            return False
        self._spec_buf  = np.empty((ch, cw, 3), dtype=np.uint8)
        self._spec_pil  = _PILImage.fromarray(self._spec_buf, mode="RGB")
        self._spec_photo = _PILImageTk.PhotoImage(self._spec_pil)
        c = self._spec_canvas
        if self._spec_image_id is not None:
            try: c.delete(self._spec_image_id)
            except Exception: pass
        self._spec_image_id = c.create_image(0, 0, anchor="nw",
                                              image=self._spec_photo)
        # Column-by-column colour tint: red → orange → green → cyan → blue
        # as frequency rises.  Smooth interpolation between keypoints in log-Hz.
        log_min = math.log(self.SPEC_FMIN_HZ)
        log_max = math.log(self.SPEC_FMAX_HZ)
        log_f   = log_min + (np.arange(cw) + 0.5) / cw * (log_max - log_min)
        keypoints = [
            (math.log(30),    (220,  40,  60)),   # deep red — sub
            (math.log(80),    (255,  90,  50)),   # red — kick fundamental
            (math.log(250),   (255, 170,  80)),   # orange — snare body
            (math.log(800),   (180, 240, 120)),   # yellow-green — vocals
            (math.log(2500),  (100, 220, 200)),   # green-cyan — presence
            (math.log(8000),  (80,  150, 255)),   # blue — hi-hat
            (math.log(18000), (60,   80, 255)),   # deep blue — cymbals
        ]
        xp = np.array([k[0] for k in keypoints])
        colors = np.zeros((cw, 3), dtype=np.float32)
        for ci in range(3):
            yp = np.array([k[1][ci] for k in keypoints], dtype=np.float32)
            colors[:, ci] = np.interp(log_f, xp, yp)
        self._spec_col_color = colors
        self._spec_peak      = np.zeros(cw, dtype=np.float32)
        self._spec_y_grid    = np.arange(ch, dtype=np.int32).reshape(-1, 1)
        # Pre-render the static background (grid + freq/dB labels) once.
        self._spec_static_bg = self._build_spec_static_bg(cw, ch)
        return True

    def _build_spec_static_bg(self, cw: int, ch: int):
        """Return a uint8 (H, W, 3) buffer pre-filled with bg + grid + labels.
        Rebuilt only when the canvas is resized."""
        bg = np.empty((ch, cw, 3), dtype=np.uint8)
        bg[:] = self.SPEC_BG_RGB

        log_min = math.log(self.SPEC_FMIN_HZ)
        log_max = math.log(self.SPEC_FMAX_HZ)
        log_range = log_max - log_min
        db_range  = self.SPEC_DB_MAX - self.SPEC_DB_MIN

        # dB grid (horizontal lines)
        for db in (-12, -24, -36, -48, -60, -72, -84):
            y = int(ch - (db - self.SPEC_DB_MIN) / db_range * ch)
            if 0 <= y < ch:
                bg[y, :] = (21, 21, 31)
        # Frequency grid (vertical lines)
        for f in (50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            if f < self.SPEC_FMIN_HZ or f > self.SPEC_FMAX_HZ:
                continue
            x = int(cw * (math.log(f) - log_min) / log_range)
            if 0 <= x < cw:
                bg[:, x] = (21, 21, 31)

        # Text labels via PIL (only once, no per-frame cost)
        try:
            from PIL import ImageDraw, ImageFont  # type: ignore
            pil_bg = _PILImage.fromarray(bg, mode="RGB")
            draw   = ImageDraw.Draw(pil_bg)
            try:
                font_sm = ImageFont.truetype("segoeui.ttf", 9)
            except OSError:
                font_sm = ImageFont.load_default()
            for db in (-12, -24, -36, -48, -60, -72, -84):
                y = int(ch - (db - self.SPEC_DB_MIN) / db_range * ch)
                draw.text((cw - 22, y - 6), f"{db}", fill=(58, 58, 74),
                          font=font_sm)
            for f in (50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
                if f < self.SPEC_FMIN_HZ or f > self.SPEC_FMAX_HZ:
                    continue
                x = int(cw * (math.log(f) - log_min) / log_range)
                lbl = f"{f}" if f < 1000 else f"{f//1000}k"
                draw.text((x + 2, ch - 14), lbl, fill=(85, 87, 112),
                          font=font_sm)
            bg = np.asarray(pil_bg, dtype=np.uint8).copy()
        except Exception:
            pass
        return bg

    def _draw_spectrum(self, det):
        spec = det.get_spectrum()
        if spec is None:
            return
        freqs, mags_db = spec

        # Determine the active render surface (native GL holder or canvas)
        if self._native_spec is not None:
            host = self._spec_holder
            cw = host.winfo_width()
            ch = host.winfo_height()
        else:
            if not _PIL_OK:
                return
            c  = self._spec_canvas
            if c is None:
                return
            cw = c.winfo_width()
            ch = c.winfo_height()
        if cw < 4 or ch < 4:
            return
        # Lazy init / resize for the CPU bitmap fallback only
        if (self._native_spec is None
                and (self._spec_buf is None
                     or self._spec_buf.shape[0] != ch
                     or self._spec_buf.shape[1] != cw)):
            if not self._init_spec_bitmap(cw, ch):
                return

        log_min = math.log(self.SPEC_FMIN_HZ)
        log_max = math.log(self.SPEC_FMAX_HZ)
        log_range = log_max - log_min
        db_range  = self.SPEC_DB_MAX - self.SPEC_DB_MIN

        # ── FFT bin → pixel column max dB (vectorised) ────────────────────
        mask = (freqs >= self.SPEC_FMIN_HZ) & (freqs <= self.SPEC_FMAX_HZ)
        if not mask.any():
            return
        f_kept = freqs[mask]
        m_kept = mags_db[mask].astype(np.float32)
        xs_all = ((np.log(f_kept) - log_min) / log_range * cw).astype(np.int32)
        np.clip(xs_all, 0, cw - 1, out=xs_all)
        col_max = np.full(cw, self.SPEC_DB_MIN, dtype=np.float32)
        np.maximum.at(col_max, xs_all, m_kept)

        # ── 80 dB/s peak-decay (fast attack, smooth release) ──────────────
        # Rising values are taken immediately; falling values decay by the
        # time-correct amount based on real elapsed frame interval.
        now = time.perf_counter()
        if (self._spec_decay_db is None
                or self._spec_decay_db.shape[0] != cw):
            self._spec_decay_db = col_max.copy()
            self._spec_last_t = now
        else:
            dt = max(0.001, min(0.2, now - self._spec_last_t)) \
                 if self._spec_last_t else 1.0 / 60.0
            self._spec_last_t = now
            decay = 80.0 * dt   # dB lost since last frame
            np.maximum(col_max, self._spec_decay_db - decay,
                       out=self._spec_decay_db)
        col_max = self._spec_decay_db

        # Normalised height per column (0..1)
        norm_h = np.clip((col_max - self.SPEC_DB_MIN) / db_range, 0.0, 1.0)

        # Compute the kick / hi-hat band positions in normalised x coordinates
        kl, kh = self._kick_low_var.get(),  self._kick_high_var.get()
        hl, hh = self._hihat_low_var.get(), self._hihat_high_var.get()
        kx0n = max(0.0, (math.log(max(kl, self.SPEC_FMIN_HZ)) - log_min) / log_range)
        kx1n = min(1.0, (math.log(min(kh, self.SPEC_FMAX_HZ)) - log_min) / log_range)
        hx0n = max(0.0, (math.log(max(hl, self.SPEC_FMIN_HZ)) - log_min) / log_range)
        hx1n = min(1.0, (math.log(min(hh, self.SPEC_FMAX_HZ)) - log_min) / log_range)

        # ── Native-GL path (preferred when available) ──────────────────────
        # Publishes the curve to the embedded glfw child window and renders
        # directly to its swap chain.  No PhotoImage upload — fastest path.
        if self._native_spec is not None:
            try:
                self._native_spec.publish(norm_h, (kx0n, kx1n), (hx0n, hx1n))
                self._native_spec.render()
                return
            except Exception as exc:
                log.warning("Native GL spectrum failed (%s); reverting to offscreen path", exc)
                try: self._native_spec.stop()
                except Exception: pass
                self._native_spec = None

        # ── GPU path (moderngl offscreen FBO → CPU readback → PhotoImage) ──
        if self._gl_spectrum is not None:
            try:
                self._gl_spectrum.resize(cw, ch)
                pixels = self._gl_spectrum.render(
                    norm_h, (kx0n, kx1n), (hx0n, hx1n))
                if pixels is not None:
                    self._spec_pil.frombytes(pixels.tobytes())
                    self._spec_photo.paste(self._spec_pil)
                    return
            except Exception as exc:
                log.warning("GL render failed, dropping to CPU path: %s", exc)
                try:
                    self._gl_spectrum.release()
                except Exception:
                    pass
                self._gl_spectrum = None

        # ── CPU fallback path (PIL bitmap) ─────────────────────────────────
        bar_h    = (norm_h * (ch - 4)).astype(np.int32)
        bar_top  = (ch - bar_h).reshape(1, -1)
        in_bar   = self._spec_y_grid >= bar_top
        denom = np.maximum(bar_h.reshape(1, -1), 1).astype(np.float32)
        t = (self._spec_y_grid - bar_top).astype(np.float32) / denom
        intensity = 0.45 + 0.55 * np.clip(1.0 - t, 0.0, 1.0)

        buf = self._spec_buf
        buf[:] = self._spec_static_bg
        kx0 = int(kx0n * cw); kx1 = int(kx1n * cw)
        hx0 = int(hx0n * cw); hx1 = int(hx1n * cw)
        if kx1 > kx0:
            buf[:, kx0:kx1] = np.clip(
                buf[:, kx0:kx1].astype(np.float32) + self.SPEC_KICK_TINT,
                0, 255).astype(np.uint8)
        if hx1 > hx0:
            buf[:, hx0:hx1] = np.clip(
                buf[:, hx0:hx1].astype(np.float32) + self.SPEC_HH_TINT,
                0, 255).astype(np.uint8)
        col_color = self._spec_col_color.reshape(1, cw, 3)
        bar_rgb = (col_color * intensity[..., None]).astype(np.uint8)
        np.copyto(buf, bar_rgb, where=in_bar[..., None])
        if kx1 > kx0:
            buf[:, max(0, kx0):max(1, kx0 + 1)] = (255, 165, 0)
            buf[:, min(cw - 1, kx1 - 1):min(cw, kx1)] = (255, 165, 0)
        if hx1 > hx0:
            buf[:, max(0, hx0):max(1, hx0 + 1)] = (0, 191, 255)
            buf[:, min(cw - 1, hx1 - 1):min(cw, hx1)] = (0, 191, 255)
        self._spec_pil.frombytes(buf.tobytes())
        draw = _PILImageDraw.Draw(self._spec_pil)
        bar_top_c = np.clip((ch - bar_h).astype(np.int32), 0, ch - 1)
        line_pts = np.empty(cw * 2, dtype=np.int32)
        line_pts[0::2] = np.arange(cw)
        line_pts[1::2] = bar_top_c
        draw.line(line_pts.tolist(), fill=(220, 230, 250), width=1)
        self._spec_photo.paste(self._spec_pil)

    def _start_madmom(self):
        """Auto-starts when audio starts.  Madmom is the *only* BPM source."""
        if getattr(self, "_madmom_det", None) is not None:
            return
        try:
            from .bpm_madmom import is_available, MadmomBPM
        except Exception as exc:
            log.warning("madmom unavailable: %s", exc)
            return
        if not is_available():
            self._audio_conf_lbl.config(text="madmom unavailable", fg=RED)
            return
        # Construct + start madmom on a worker thread so the audio thread isn't
        # starved while the RNN model files load (~1 s the first time).
        src_rate = getattr(self._audio_det, "_rate", 44100) or 44100
        self._audio_bpm_lbl.config(text="---", fg=YELLOW)
        self._audio_conf_lbl.config(
            text=f"madmom loading… (src {src_rate} Hz)", fg=DIM)

        def _bg_build():
            try:
                det = MadmomBPM(window_s=8.0, source_rate=int(src_rate))
                det.start()
            except Exception as exc:
                log.error("Madmom start failed: %s", exc, exc_info=True)
                self.after(0, lambda: self._audio_conf_lbl.config(
                    text=f"madmom failed: {exc}", fg=RED))
                return
            def _attach():
                self._madmom_det = det
                if self._audio_det is not None:
                    self._audio_det.attach_madmom(det)
                self._audio_conf_lbl.config(
                    text=f"madmom warming up… (src {src_rate} Hz)", fg=DIM)
                self.after(250, self._poll_madmom)
            self.after(0, _attach)

        threading.Thread(target=_bg_build, daemon=True,
                          name="madmom-init").start()

    def _stop_madmom(self):
        det = getattr(self, "_madmom_det", None)
        if det is None:
            return
        if self._audio_det is not None:
            self._audio_det.detach_madmom()
        try: det.stop()
        except Exception: pass
        self._madmom_det = None

    def _poll_madmom(self):
        det = getattr(self, "_madmom_det", None)
        if det is None or not det.running:
            return
        if det.bpm > 0:
            # Single writer to the clock BPM.
            self._bpm_var.set(round(det.bpm, 3))
            col = GREEN if det.confidence > 0.5 else YELLOW
            filled = int(round(det.confidence * 5))
            dots = "●" * filled + "○" * (5 - filled)
            self._audio_bpm_lbl.config(text=f"{det.bpm:.2f}", fg=col)
            self._audio_conf_lbl.config(
                text=f"madmom {dots} {det.confidence:.2f}", fg=col)

            # ── Drive the beat-visualizer counter from madmom's beat list ──
            # Atomic snapshot — avoids torn reads while the worker clears
            # and re-extends beat_times during inference.
            for t in det.snapshot_beat_times():
                if t > self._madmom_beats_seen_t:
                    self._beats_since_resync += 1
                    self._madmom_beats_seen_t = t

            # Phase lock: nudge _beat_offset so the most recent madmom beat
            # falls on an integer effective-beat.  Gentle 15 % step per poll
            # — converges in ~1.5 s without ever jumping audibly.
            # Suppressed for 2 s after an auto-resync so the snap-to-beat-1
            # we just did isn't immediately drifted away.
            now = time.perf_counter()
            if (det.last_beat_t > 0 and self._bpm_on.get()
                    and now >= self._phase_lock_freeze_until):
                if now - det.last_beat_t < 1.5:
                    raw_now = self._current_raw_beat()
                    elapsed = now - det.last_beat_t
                    bpm_now = self._bpm_var.get() or 120.0
                    beats_since   = elapsed * bpm_now / 60.0
                    target_beat   = round(raw_now + self._beat_offset - beats_since) + beats_since
                    target_offset = target_beat - raw_now
                    self._beat_offset = 0.85 * self._beat_offset + 0.15 * target_offset
        self.after(200, self._poll_madmom)

    def _toggle_audio(self):
        if self._audio_det is None:
            return
        if self._audio_det.running:
            self._audio_det.stop()
            self._stop_madmom()
            self._audio_btn.config(text="Start Audio", bg=BTN, fg=BTNFG)
            self._audio_bpm_lbl.config(text="---", fg=YELLOW)
            self._audio_conf_lbl.config(text="")
        else:
            sel   = self._audio_dev_var.get()
            match = next((d for d in self._audio_devs if d[0] == sel), None)
            if match is None:
                return
            _, idx, loopback, source = match
            try:
                self._audio_det.start(idx, loopback, self._on_audio_bpm, source=source)
                self._audio_btn.config(text="Stop Audio", bg=RED, fg="#1e1e2e")
                self._audio_conf_lbl.config(text="Listening…", fg=DIM)
                # Madmom is the sole BPM source — starts with audio.
                self._start_madmom()
            except Exception as exc:
                log.error("Audio start failed: %s", exc, exc_info=True)
                self._audio_conf_lbl.config(text=str(exc), fg=RED)
        self._refresh_apc_leds()
        self._refresh_apc_btn_colors()

    def _on_audio_bpm(self, bpm: float, confidence: float,
                       status: str = "tracking",
                       is_beat: bool = False, is_kick: bool = False):
        # Called from the audio background thread — bounce to the main thread.
        # is_beat is accepted for back-compat with the old callback shape but
        # nothing in the new pipeline emits or reads it.
        self.after(0, lambda b=bpm, c=confidence, s=status, ik=is_kick:
                   self._apply_audio_bpm(b, c, s, ik))

    def _apply_audio_bpm(self, bpm: float, confidence: float,
                          status: str = "tracking",
                          is_kick: bool = False):
        """Aubio no longer drives BPM — madmom owns it.  The only thing this
        callback still handles is the kick-band rising-edge for auto-resync
        after a silent breakdown."""
        if is_kick and self._bpm_on.get():
            now = time.perf_counter()
            if self._auto_resync_var.get():
                det = getattr(self, "_madmom_det", None)

                # ── Path A: madmom is armed (just exited a sustained HOLD) ──
                # This fires on the very first kick after a real breakdown,
                # regardless of how long the silence was.  Snaps phase to the
                # drop with zero latency.
                if det is not None and getattr(det, "armed_for_resync", False):
                    log.info("Auto-resync: madmom exited HOLD, first kick = drop")
                    det.armed_for_resync = False
                    self._resync()

                # ── Path B: very long silence safety net (15 s+) ──
                # In case HOLD never fired but the music genuinely stopped
                # for ages — gap measured against madmom's last published
                # beat so false-positive kicks don't reset the timer.
                else:
                    ref_t = (det.last_beat_t if det is not None
                              and det.last_beat_t > 0
                              else self._last_kick_time)
                    gap = now - ref_t if ref_t > 0 else 0.0
                    if gap > 15.0:
                        log.info("Auto-resync: %.1f s without madmom beats", gap)
                        self._resync()

            self._last_kick_time = now


    def _poll_audio_level(self):
        if self._audio_det is not None and self._audio_det.running:
            level = self._audio_det.level
            c = self._level_canvas
            w = c.winfo_width()
            if w > 1:
                c.delete("all")
                # Scale: 0.0 = silence, 0.1 = loud music (RMS of normalised float)
                fill_w = int(min(1.0, level * 10.0) * w)
                if fill_w > 0:
                    color = GREEN if level > 0.005 else YELLOW
                    c.create_rectangle(0, 0, fill_w, 12, fill=color, outline="")
            db = 20 * math.log10(level + 1e-9)
            self._level_lbl.config(text=f"{db:+.0f} dB",
                                   fg=GREEN if level > 0.005 else RED)
        self.after(80, self._poll_audio_level)

    def _on_close(self):
        det = getattr(self, "_madmom_det", None)
        if det is not None:
            try: det.stop()
            except Exception: pass
        if self._audio_det is not None:
            self._audio_det.stop()
        self._stop_midi()
        if self._native_spec is not None:
            try: self._native_spec.stop()
            except Exception: pass
            self._native_spec = None
        if self._gl_spectrum is not None:
            try: self._gl_spectrum.release()
            except Exception: pass
            self._gl_spectrum = None
        if self._gl_waveform is not None:
            try: self._gl_waveform.release()
            except Exception: pass
            self._gl_waveform = None
        self.destroy()

    # ── MIDI controller (APC40 MK2) ──────────────────────────────────────────

    def _build_midi_section(self, parent=None):
        """Build the MIDI device picker. When ``parent`` is given (e.g. the
        header frame), the section is laid out compactly on a single row so it
        fits in a corner next to the BLINDS CONTROLLER title."""
        compact = parent is not None
        parent  = parent if parent is not None else self

        if compact:
            sec = tk.Frame(parent, bg=BG)
            sec.pack(side="right", padx=(0, 4))
        else:
            sec = tk.Frame(parent, bg=BG)
            sec.pack(padx=20, pady=(4, 0), fill="x")

        if not compact:
            hdr = tk.Frame(sec, bg=BG)
            hdr.pack(fill="x")
            tk.Label(hdr, text="MIDI CONTROLLER",
                     font=("Segoe UI", 10, "bold"), bg=BG, fg=FG).pack(side="left")
            if not MIDI_AVAILABLE:
                tk.Label(hdr, text="→  pip install mido python-rtmidi",
                         font=("Segoe UI", 8), bg=BG, fg=RED).pack(side="left", padx=8)
                return
            assert mido is not None
            tk.Label(hdr, text="(APC40 MK2)",
                     font=("Segoe UI", 8), bg=BG, fg=DIM).pack(side="left", padx=8)
            self._midi_status_lbl = tk.Label(hdr, text="Not connected",
                                              font=("Segoe UI", 9), bg=BG, fg=DIM)
            self._midi_status_lbl.pack(side="right")

            dev_row = tk.Frame(sec, bg=BG)
            dev_row.pack(fill="x", pady=(8, 4))
            tk.Label(dev_row, text="Device:", font=("Segoe UI", 9),
                     bg=BG, fg=FG, width=7, anchor="w").pack(side="left")
        else:
            # Compact header-corner layout: single row, narrower combo
            if not MIDI_AVAILABLE:
                tk.Label(sec, text="MIDI: pip install mido python-rtmidi",
                         font=("Segoe UI", 8), bg=BG, fg=RED).pack(side="right")
                return
            assert mido is not None
            tk.Label(sec, text="MIDI", font=("Segoe UI", 9, "bold"),
                     bg=BG, fg=BLUE).pack(side="left", padx=(0, 4))
            dev_row = sec   # everything goes on the single row

        names = mido.get_input_names() or ["(no MIDI devices found)"]
        self._midi_dev_var = tk.StringVar(value=names[0])
        self._midi_combo = ttk.Combobox(
            dev_row, textvariable=self._midi_dev_var,
            values=names, width=22 if compact else 38, state="readonly")
        self._midi_combo.pack(side="left", padx=4)
        self._midi_btn = tk.Button(dev_row, text="Connect",
                                    font=("Segoe UI", 9), bg=BTN, fg=BTNFG,
                                    relief="flat", padx=10, pady=4, cursor="hand2",
                                    command=self._toggle_midi)
        self._midi_btn.pack(side="left", padx=8)
        _hov(self._midi_btn)
        ref_btn = tk.Button(dev_row, text="↻", font=("Segoe UI", 9),
                             bg=BTN, fg=BTNFG, relief="flat", padx=6, pady=4,
                             cursor="hand2", command=self._refresh_midi_devices)
        ref_btn.pack(side="left")
        _hov(ref_btn)
        if compact:
            # Status label sits right next to the refresh button in compact mode
            self._midi_status_lbl = tk.Label(dev_row, text="Not connected",
                                              font=("Segoe UI", 8), bg=BG, fg=DIM)
            self._midi_status_lbl.pack(side="left", padx=(8, 0))

    def _refresh_midi_devices(self):
        if not MIDI_AVAILABLE or mido is None:
            return
        names = mido.get_input_names() or ["(no MIDI devices found)"]
        self._midi_combo.config(values=names)
        if self._midi_dev_var.get() not in names:
            self._midi_dev_var.set(names[0])

    def _toggle_midi(self):
        if self._midi_port is not None:
            self._stop_midi()
        else:
            self._start_midi()

    # APC40 MK2 input ports are exposed with names like "APC40 mkII 0",
    # "APC40 mkII 1", etc. on Windows.  This substring is what we match on.
    _APC40_NAME_MATCH = "APC40"
    # Number of additional polls after the initial attempt (every 2 s).
    # Lets the user plug the controller in within ~12 s of launching.
    _APC40_AUTODETECT_RETRIES = 6

    def _auto_connect_apc40(self, retries_left: int | None = None):
        """Scan available MIDI input ports for an Akai APC40 MK2 and connect.
        Silent no-op when MIDI is unavailable or no matching device exists.
        Retries every 2 s for ~12 s in case the controller is plugged in
        just after launch."""
        if retries_left is None:
            retries_left = self._APC40_AUTODETECT_RETRIES
        if (not MIDI_AVAILABLE or mido is None
                or self._midi_port is not None):
            return
        try:
            names = mido.get_input_names() or []
        except Exception:
            names = []
        match = next(
            (n for n in names if self._APC40_NAME_MATCH in n.upper()),
            None)
        if match is not None:
            log.info("Auto-detect: connecting to %s", match)
            # Refresh dropdown so the user sees what's now available
            if hasattr(self, "_midi_combo"):
                self._midi_combo.config(values=names)
            self._midi_dev_var.set(match)
            self._start_midi()
            return
        # Not found yet — try again later if we have retries left
        if retries_left > 0:
            self.after(2000, lambda: self._auto_connect_apc40(retries_left - 1))

    def _start_midi(self):
        if not MIDI_AVAILABLE or mido is None:
            return
        name = self._midi_dev_var.get()
        if name.startswith("("):
            return
        try:
            self._midi_port = mido.open_input(name)
            # Try to open the matching output port for LED feedback. APC40 MK2
            # exposes input and output with the same device name; if not found,
            # try to match by the leading "APC40" prefix. Failure is non-fatal.
            try:
                self._midi_out = mido.open_output(name)
            except (OSError, IOError):
                out_match = next(
                    (n for n in mido.get_output_names() if "APC40" in n.upper()),
                    None)
                self._midi_out = mido.open_output(out_match) if out_match else None
            self._midi_active = True
            self._midi_btn.config(text="Disconnect", bg=RED, fg="#1e1e2e")
            extra = " + LED out" if self._midi_out is not None else " (no LED out)"
            self._midi_status_lbl.config(text=f"Connected — {name}{extra}", fg=GREEN)
            threading.Thread(target=self._midi_loop, daemon=True).start()
            log.info("MIDI connected: %s (out=%s)", name,
                     "yes" if self._midi_out is not None else "no")
            self._init_apc40()
            self._refresh_apc_leds()
        except Exception as exc:
            log.error("MIDI connect failed: %s", exc, exc_info=True)
            self._midi_status_lbl.config(text=str(exc), fg=RED)

    def _stop_midi(self):
        self._midi_active = False
        # Best-effort: blank every LED we know about before closing the port.
        if self._midi_out is not None:
            try:
                self._apc_leds_off()
            except Exception:
                pass
        if self._midi_port:
            try:
                self._midi_port.close()
            except Exception:
                pass
            self._midi_port = None
        if self._midi_out is not None:
            try:
                self._midi_out.close()
            except Exception:
                pass
            self._midi_out = None
        if MIDI_AVAILABLE and hasattr(self, "_midi_btn"):
            self._midi_btn.config(text="Connect", bg=BTN, fg=BTNFG)
            self._midi_status_lbl.config(text="Disconnected", fg=DIM)

    # ── APC40 LED feedback (output) ──────────────────────────────────────────
    # Generic Mode (Mode 0) per APC40 MK2 Communications Protocol v1.2:
    #   • Clip-pad LEDs are RGB — velocity = palette colour (21 = bright green).
    #   • PAN/SENDS/USER/METRONOME LEDs: velocity 0 = off, 1-127 = on.
    #   • TAP TEMPO, NUDGE +/-, SHIFT, UP/DOWN/LEFT/RIGHT have NO LEDs.
    #   • Knob LED ring: send the knob's own CC to update displayed value;
    #     the ring style is set once via a separate "ring-type" CC (knob CC + 8).

    APC_LED_COLOR_ON     = 21    # bright green from RGB palette (master)
    APC_LED_PAT_OFF_VEL  = 41    # light blue   (#4C88FF) — pattern btn assigned
    APC_LED_PAT_ON_VEL   = 120   # deep red     (#A00000) — selected pattern
    APC_LED_BEAT_OFF_VEL = 116   # light purple (#8E66FF) — beat btn assigned
    APC_LED_BEAT_ON_VEL  = 13    # bright yellow(#FFFF00) — selected beat
    APC_LED_CHASE_VEL    = 9     # bright orange (#FF5400) — chase head
    APC_LED_CHASE_TRAIL  = (10, 11)   # medium → dim orange (palette siblings of 9)
                                      # — successive trail positions behind the
                                      # head; matches APC40's 4-level orange ramp
    APC_CC_GAP_SIZE_RING  = 0x38  # 56 — Track Knob 1 LED Ring Type
    APC_RING_STYLE_VOL    = 2    # ring style: Volume — fills clockwise
    APC_RING_STYLE_SINGLE = 1    # ring style: Single — one scanning LED

    def _led_set(self, note: int, velocity: int, ch: int = 0):
        """Send a single LED note-on with an explicit palette velocity.
        velocity 0 = off; any other value picks a colour from the APC40 MK2
        RGB palette (see protocol PDF pages 18-22)."""
        if self._midi_out is None or mido is None:
            return
        try:
            self._midi_out.send(mido.Message(
                "note_on", channel=ch, note=note,
                velocity=max(0, min(127, int(velocity)))))
        except Exception:
            pass

    def _led_btn(self, note: int, on: bool,
                 color: int | None = None, ch: int = 0):
        """Light or extinguish a button LED on the APC40 MK2 (Generic Mode).
        Defaults to bright green when on; pass `color` for a palette index."""
        vel = (color if color is not None else self.APC_LED_COLOR_ON) if on else 0
        self._led_set(note, vel, ch)

    def _led_ring(self, cc: int, value: int, ch: int = 0):
        """Update a knob LED ring by sending the same CC the knob reads."""
        if self._midi_out is None or mido is None:
            return
        try:
            self._midi_out.send(mido.Message(
                "control_change", channel=ch, control=cc,
                value=max(0, min(127, int(value)))))
        except Exception:
            pass

    def _init_apc40(self):
        """One-time setup pushed to the APC40 right after connect."""
        # Gap Size knob (Track Knob 1) → Volume style ring
        self._led_ring(self.APC_CC_GAP_SIZE_RING, self.APC_RING_STYLE_VOL)
        # Device Control knobs 1-8 → Single style (one scanning LED per beat)
        for i in range(8):
            self._led_ring(APC_CC_DEVICE_KNOB_RING_BASE + i, self.APC_RING_STYLE_SINGLE)
        # Clear any residual values left from a previous session
        for i in range(8):
            self._led_ring(APC_CC_DEVICE_KNOB_BASE + i, 0)

    def _refresh_apc_leds(self):
        """Push current app state to every mapped APC40 LED."""
        if self._midi_out is None:
            return

        # Pattern rows: cols 0-6 are "assigned" buttons — always show light
        # blue when not chosen, deep red when chosen. Col 7 stays unmapped.
        def pat_velocity(selected_name: "str | None", col: int) -> int:
            if col == 0:
                name = "Still"
            elif 1 <= col <= 6:
                name = PATTERNS[col - 1][0]
            else:
                return 0    # col 7 unassigned → OFF
            return (self.APC_LED_PAT_ON_VEL if selected_name == name
                    else self.APC_LED_PAT_OFF_VEL)

        # Beat rows: 8 assigned buttons — light purple when not chosen,
        # bright yellow when chosen (high-contrast counterpart of the purple).
        def beat_velocity(sync_beats, col, beat_vals):
            return (self.APC_LED_BEAT_ON_VEL
                    if sync_beats == beat_vals[col]
                    else self.APC_LED_BEAT_OFF_VEL)

        # Size pattern row (row 0)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_SIZE_PAT, col),
                          pat_velocity(self._size_pat_selected, col))
        # Size beat row (row 1)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_SIZE_BEAT, col),
                          beat_velocity(self._size_sync_beats, col, APC_SIZE_BEAT_VALUES))

        # Position pattern row (row 3)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_POS_PAT, col),
                          pat_velocity(self._pos_pat_selected, col))
        # Position beat row (row 4)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_POS_BEAT, col),
                          beat_velocity(self._pos_sync_beats, col, APC_POS_BEAT_VALUES))

        # Master-section toggle buttons (stateful)
        self._led_btn(APC_BTN_BPM_SYNC,   self._bpm_on.get())
        self._led_btn(APC_BTN_AUDIO_SYNC,
                      self._audio_det is not None and self._audio_det.running)
        self._led_btn(APC_BTN_LINK,       self._link_on.get())

        # Gap Size knob LED ring (Gap Size is 0–25 %, ring is 0–127).
        # MUST use round() not int() here: gap_size is rounded to 2 decimals
        # before this runs, so int(x.99...) would truncate downward and feed
        # back a value 1 lower than the knob just sent — making CW turns get
        # stuck and CCW turns drop 2 LED positions per detent.
        v = int(round(self._gap_size_var.get() / 25.0 * 127))
        self._led_ring(APC_CC_GAP_SIZE, v)

    def _apc_leds_off(self):
        """Blank every LED we drive — called on disconnect."""
        for row in (APC_ROW_SIZE_PAT, APC_ROW_SIZE_BEAT,
                    APC_ROW_POS_PAT, APC_ROW_POS_BEAT):
            for col in range(8):
                self._led_btn(_apc_clip_note(row, col), False)
        for n in (APC_BTN_BPM_SYNC, APC_BTN_AUDIO_SYNC, APC_BTN_LINK):
            self._led_btn(n, False)
        # Device control knob rings (beat visualiser)
        for i in range(8):
            self._led_ring(APC_CC_DEVICE_KNOB_BASE + i, 0)
        # Device control buttons (beat visualiser)
        for btn_note in APC_NOTE_DEVICE_BTN_ALL:
            self._led_btn(btn_note, False)
        self._chase_last_btn = None
        self._knob_prev_cc = [-1] * 8
        self._knob_btn_on  = [False] * 8
        self._led_ring(APC_CC_GAP_SIZE, 0)

    def _chase_leds_tick(self):
        """Beat visualisation on the 8 Device Control knob LED rings + buttons.
        An 8-beat cycle through knobs 0-7. Within each beat:
        - Knob ring: single LED scans clockwise around 15-position ring (1 LED per 1/15th beat)
        - Button: on/off flash (on for first half of beat, off for second half)"""
        try:
            # IMPORTANT: must NOT `return` here — the reschedule at the bottom
            # of the function keeps the loop alive even before MIDI connects.
            if self._midi_out is not None:
                beat          = self._current_raw_beat() + self._beat_offset
                beat_in_cycle = int(beat) % 8              # 0-7 beat index
                knob_offset   = APC_BEAT_KNOB_ORDER[beat_in_cycle]
                beat_frac     = beat % 1.0

                # When the beat advances, blank the previous ring and button
                if beat_in_cycle != self._chase_last_btn:
                    if self._chase_last_btn is not None:
                        prev_offset = APC_BEAT_KNOB_ORDER[self._chase_last_btn]
                        # Clear previous knob ring
                        self._led_ring(APC_CC_DEVICE_KNOB_BASE + prev_offset, 0)
                        # Turn off previous button
                        prev_btn_note = APC_NOTE_DEVICE_BTN_ALL[self._chase_last_btn]
                        self._midi_out.send(mido.Message(
                            "note_off", note=prev_btn_note, channel=0))
                    self._chase_last_btn = beat_in_cycle

                # Knob ring: position 0-14 → one LED clockwise around ring
                led_pos = min(14, int(beat_frac * 15))
                self._led_ring(APC_CC_DEVICE_KNOB_BASE + knob_offset,
                               APC_SINGLE_RING_POS[led_pos])

                # Button: on/off flash (first half of beat → on, second half → off)
                btn_note = APC_NOTE_DEVICE_BTN_ALL[beat_in_cycle]
                btn_on = beat_frac < 0.5
                msg_type = "note_on" if btn_on else "note_off"
                self._midi_out.send(mido.Message(
                    msg_type, note=btn_note,
                    velocity=127 if btn_on else 0, channel=0))
        except Exception:
            pass
        self.after(15, self._chase_leds_tick)   # ≈ 67 Hz

    def _midi_loop(self):
        while self._midi_active:
            try:
                if self._midi_port and not self._midi_port.closed:
                    for msg in self._midi_port.iter_pending():
                        self._handle_midi(msg)
            except Exception as exc:
                log.warning("MIDI loop error: %s", exc)
                break
            time.sleep(0.005)

    def _handle_midi(self, msg):
        if msg.type not in ("note_on", "control_change"):
            return
        ch = msg.channel

        if msg.type == "note_on":
            if msg.velocity == 0:
                return  # note_off encoded as note_on vel=0
            note = int(msg.note)

            # ── Clip grid (channel 1, notes 0–39, see _apc_clip_note) ─────────
            if ch == APC_CLIP_CH and 0 <= note <= 39:
                row = 4 - (note // 8)   # 0=top, 4=bottom
                col = note % 8          # 0..7
                if row == APC_ROW_SIZE_PAT:
                    if col == 0:
                        self.after(0, lambda: self._set_size_sync(None))      # "Still" → Off
                    elif 1 <= col <= 6:
                        pat = PATTERNS[col - 1]
                        self.after(0, lambda p=pat: self._set_size_pattern(p[1], p[0]))
                elif row == APC_ROW_SIZE_BEAT:
                    b = APC_SIZE_BEAT_VALUES[col]
                    self.after(0, lambda v=b: self._set_size_sync(v))
                elif row == APC_ROW_POS_PAT:
                    if col == 0:
                        self.after(0, lambda: self._set_pos_sync(None))
                    elif 1 <= col <= 6:
                        pat = PATTERNS[col - 1]
                        self.after(0, lambda p=pat: self._set_pos_pattern(p[1], p[0]))
                elif row == APC_ROW_POS_BEAT:
                    b = APC_POS_BEAT_VALUES[col]
                    self.after(0, lambda v=b: self._set_pos_sync(v))
                return

            # ── Master-section buttons (channel 1) ────────────────────────────
            if ch == 0:
                if   note == APC_BTN_BPM_SYNC:
                    self.after(0, lambda: self._bpm_on.set(not self._bpm_on.get()))
                elif note == APC_BTN_AUDIO_SYNC:
                    self.after(0, self._toggle_audio)
                elif note == APC_BTN_LINK:
                    self.after(0, lambda: self._link_on.set(not self._link_on.get()))
                elif note == APC_BTN_RESYNC:
                    self.after(0, self._resync)
                elif note == APC_BTN_TAP:
                    self.after(0, self._tap)
                elif note == APC_BTN_NUDGE_PLUS:
                    self.after(0, lambda: self._nudge(0.0625))
                elif note == APC_BTN_NUDGE_MINUS:
                    self.after(0, lambda: self._nudge(-0.0625))

        elif msg.type == "control_change":
            if ch == APC_CC_GAP_POS_CH and msg.control == APC_CC_GAP_POS:
                v = msg.value / 127.0 * 100.0
                self.after(0, lambda x=v: self._gap_pos_var.set(round(x, 1)))
            elif ch == APC_CC_MOTOR_SPD_CH and msg.control == APC_CC_MOTOR_SPD:
                v = 5.0 + msg.value / 127.0 * 95.0   # rightmost fader → 5–100 %/s
                self.after(0, lambda x=v: self._max_spd.set(round(x, 0)))
            elif ch == APC_CC_GAP_SIZE_CH and msg.control == APC_CC_GAP_SIZE:
                v = msg.value / 127.0 * 25.0
                self.after(0, lambda x=v: self._gap_size_var.set(round(x, 2)))
            elif msg.control == APC_CC_BPM_FINE:
                # Fine BPM adjust: ±0.01 BPM per encoder click
                self.after(0, lambda val=msg.value: self._bpm_fine_handler(val))

    # ── Ableton Link polling (main thread, every second) ──────────────────────

    def _poll_link(self):
        if not LINK_AVAILABLE or _link is None:
            color, text = RED, "Link ✗  →  pip install aalink"
        elif not self._link_on.get():
            color, text = DIM, "Link ○ disabled (using audio/manual BPM)"
        else:
            try:
                peers = _lnk_peers()
                tempo = _lnk_get_tempo()
                if peers > 0:
                    self._bpm_var.set(round(tempo, 1))
                color = GREEN if peers > 0 else YELLOW
                text  = f"Link ● {peers} peer{'s' if peers != 1 else ''}  {int(tempo)} BPM"
            except Exception as exc:
                if not getattr(self, "_link_err_shown", False):
                    log.warning("Link poll failed: %s", exc, exc_info=True)
                    self._link_err_shown = True
                color, text = RED, f"Link ✗ {type(exc).__name__}: {exc}"
        self._link_lbl.config(text=text, fg=color)
        self.after(1000, self._poll_link)

    # ── Animation loop (background thread, ~40 fps) ───────────────────────────

    def _anim_loop(self):
        while True:
            try:
                if self._bpm_on.get():
                    self._tick()
            except Exception:
                pass
            time.sleep(1.0 / 60.0)

    def _tick(self):
        beat = self._current_raw_beat() + self._beat_offset
        self._beat = beat   # expose to preview for the phase bar

        speed    = SPEEDS[self._speed_idx][1]
        eff_beat = beat * speed

        max_size   = self._gap_size_var.get() / 100.0   # 0..1
        static_pos = self._gap_pos_var.get()  / 100.0   # 0..1  (0=bottom, 1=top)
        overlap    = self._overlap_var.get()  / 100.0   # closed overlap (10% → 55% each)
        size_beats = self._size_sync_beats               # None or float
        pos_beats  = self._pos_sync_beats                # None or float
        n = len(FRAMES)

        positions = []
        for i in range(n):
            # Each parameter has its own per-window delay in beats
            pos_delay  = self._pos_pat_fn(i, n)
            size_delay = self._size_pat_fn(i, n)

            # Gap Size: 0 → max_size → 0 over `size_beats` beats (one open/close)
            if size_beats:
                size = max_size * _wave((eff_beat - size_delay) / size_beats)
            else:
                size = max_size

            # Gap Position: bottom(0) → top(1) → bottom over `pos_beats` beats
            if pos_beats:
                pos = _wave((eff_beat - pos_delay) / pos_beats)
            else:
                pos = static_pos

            size = max(0.0, min(1.0, size))
            pos  = max(0.0, min(1.0, pos))

            # Overlap scales coverage past 100% when closed (light-tight).
            cover = (1.0 + overlap) * (1.0 - size)
            # pos=0 → top blind fully extended (gap at bottom)
            # pos=1 → bottom blind fully extended (gap at top)
            t_pct = max(0.0, min(100.0, (1.0 - pos) * cover * 100.0))
            b_pct = max(0.0, min(100.0, pos * cover * 100.0))
            positions.append((b_pct, t_pct))

        self.after(0, lambda p=positions: self._apply_positions(p))

    def _apply_positions(self, positions):
        now = time.perf_counter()
        dt  = min(now - self._last_apply, 0.2)   # clamp: ignore pauses > 200 ms
        self._last_apply = now
        max_delta = self._max_spd.get() * dt      # max % change this tick

        self._suspend_send = True
        for i, (b_tgt, t_tgt) in enumerate(positions):
            b_cur, t_cur = self._cur_pos[i]
            b_new = b_cur + max(-max_delta, min(max_delta, b_tgt - b_cur))
            t_new = t_cur + max(-max_delta, min(max_delta, t_tgt - t_cur))
            self._cur_pos[i] = (b_new, t_new)
            self._pvars[i]["b"].set(b_new)
            self._pvars[i]["t"].set(t_new)
        self._suspend_send = False
        self._send_frame()

    # ── Heartbeat (re-send positions every 5 s even in manual mode) ──────────

    def _heartbeat(self):
        if not self._bpm_on.get():
            self._send_frame()
        self.after(5000, self._heartbeat)
