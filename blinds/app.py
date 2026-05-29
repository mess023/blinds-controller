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
    APC_BEAT_VALUES,
    APC_CC_DEVICE_KNOB_BASE, APC_CC_DEVICE_KNOB_RING_BASE,
    APC_BEAT_KNOB_ORDER, APC_SINGLE_RING_POS,
    APC_NOTE_DEVICE_BTN_ALL,
    APC_BTN_BPM_SYNC, APC_BTN_AUDIO_SYNC, APC_BTN_LINK,
    APC_BTN_RESYNC, APC_BTN_TAP, APC_BTN_NUDGE_MINUS, APC_BTN_NUDGE_PLUS,
    APC_CC_GAP_POS_CH, APC_CC_GAP_POS,
    APC_CC_MOTOR_SPD_CH, APC_CC_MOTOR_SPD,
    APC_CC_GAP_SIZE_CH, APC_CC_GAP_SIZE,
    APC40_W, APC40_H, APC40_IMG_PATH, APC40_POS,
    BG, CARD, FG, DIM, BLUE, GREEN, RED, YELLOW,
    BTN, BTNHOV, BTNSEL, BTNFG, BTNSELFG,
    _wave,
)
from .network import send_universe, osc_send, _osc_sock, _osc_parse
from .beat import BeatClock
from .audio import AudioBPMDetector, AUDIO_AVAILABLE, _AUBIO_OK, _PAW_OK, get_audio_devices
from .link import LINK_AVAILABLE, _link, _link_api, _lnk_peers, _lnk_get_tempo, _lnk_get_beat
from .midi import MIDI_AVAILABLE, mido
from .ui_utils import _hr, _hov

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
        self._beat_offset = 0.0   # beats; adjusted by resync / nudge
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
        self._poll_link()
        self._chase_leds_tick()
        self.after(300, self._refresh_status_labels)
        self.after(120, self._draw_preview)   # first draw after layout
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

        # APC40 visualisation + mapped clickable controls
        self._build_apc40_canvas()

        # Everything below the APC40 image is the "config" / unmapped controls
        _hr(self)
        self._build_frame_cards()
        self._build_artnet_controls()
        _hr(self)
        self._build_gap_section()
        _hr(self)
        self._build_bpm_section()
        _hr(self)
        self._build_audio_section()
        tk.Frame(self, bg=BG, height=14).pack()
        # Constrain window so the APC40 canvas always shows fully.
        self.minsize(APC40_W + 40, 640)

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

    def _build_apc40_canvas(self):
        """APC40 image as background with click-only TEXT overlays at the
        positions of each MIDI-mapped control. Same state the MIDI handler
        toggles, so clicks here and hardware presses stay in sync."""
        try:
            from PIL import Image, ImageTk
        except ImportError:
            tk.Label(self, text="(install Pillow to enable the APC40 image: pip install Pillow)",
                     bg=BG, fg=RED, font=("Segoe UI", 9)).pack(pady=6)
            return
        _img: Any = Image   # Pillow ≥10 hides LANCZOS under .Resampling
        lanczos = getattr(_img, "Resampling", _img).LANCZOS
        try:
            pil = Image.open(APC40_IMG_PATH).resize((APC40_W, APC40_H), lanczos)
        except FileNotFoundError:
            tk.Label(self, text=f"(APC40 image not found: {APC40_IMG_PATH})",
                     bg=BG, fg=RED, font=("Segoe UI", 9)).pack(pady=6)
            return

        self._apc40_tkimg = ImageTk.PhotoImage(pil)   # MUST keep reference

        wrap = tk.Frame(self, bg=BG)
        wrap.pack(pady=(6, 0))
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

        pat_labels  = ["Still", "Uniform", "Wave→", "←Wave",
                       "Spread", "Counter", "Scatter"]
        beat_labels = [f"{int(v)}" for v in APC_BEAT_VALUES]

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

            v = APC_BEAT_VALUES[col]
            self._apc_size_beat_btns.append((col, self._mk_canvas_btn(
                cx, y_sb, cw, ch_, beat_labels[col],
                lambda val=v: self._set_size_sync(val))))

            if col == 0:
                self._apc_pos_pat_btns.append((0, self._mk_canvas_btn(
                    cx, y_pp, cw, ch_, "Still",
                    lambda: self._set_pos_sync(None))))
            elif 1 <= col <= 6:
                p = PATTERNS[col - 1]
                self._apc_pos_pat_btns.append((col, self._mk_canvas_btn(
                    cx, y_pp, cw, ch_, pat_labels[col],
                    lambda fn=p[1], nm=p[0]: self._set_pos_pattern(fn, nm))))

            self._apc_pos_beat_btns.append((col, self._mk_canvas_btn(
                cx, y_pb, cw, ch_, beat_labels[col],
                lambda val=v: self._set_pos_sync(val))))

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
        self._max_spd.trace_add("write", _update_motor_marker)

        # ── Gap Size knob (top-left of the 8 top knobs) ─────────────────────
        # Click+drag vertically or scroll-wheel to change. Centre text shows
        # the current value in yellow over the dark knob body.
        kx, ky = APC40_POS["knob_gap_size"]
        self._apc_gap_size_text = c.create_text(
            kx, ky, text=f"{self._gap_size_var.get():.1f}%",
            fill=self.APC_BPM_FG, font=("Segoe UI", 9, "bold"),
            anchor="center", tags=("knob_gs",))
        c.create_oval(kx - 22, ky - 22, kx + 22, ky + 22,
                       fill="", outline="", tags=("knob_gs",))

        knob_drag = {"y0": 0, "v0": 0.0}

        def knob_press(e):
            knob_drag["y0"] = e.y
            knob_drag["v0"] = self._gap_size_var.get()

        def knob_drag_motion(e):
            # 100 px of vertical travel = full 0–25 % range; up = increase
            dy   = knob_drag["y0"] - e.y
            new  = max(0.0, min(25.0, knob_drag["v0"] + dy * (25.0 / 100.0)))
            self._gap_size_var.set(round(new, 1))

        def knob_wheel(e):
            # tag_bind doesn't accept <MouseWheel>, so we bind at canvas level
            # and gate on cursor proximity to the knob centre.
            if (e.x - kx) ** 2 + (e.y - ky) ** 2 > 22 * 22:
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

        # ── Current BPM display (canvas text — fully transparent) ───────────
        bx, by = APC40_POS["bpm_display"]
        self._apc_bpm_text = c.create_text(
            bx, by, text=f"{self._bpm_var.get():.2f}",
            fill=self.APC_BPM_FG, font=("Segoe UI", 18, "bold"),
            anchor="center")
        self._bpm_var.trace_add("write", lambda *_: c.itemconfig(
            self._apc_bpm_text, text=f"{self._bpm_var.get():.2f}"))

        self._refresh_apc_btn_colors()

    def _mk_canvas_btn(self, x: float, y: float, w: float, h: float,
                        label: str, command,
                        label_dy: float = 0,
                        color_off: str | None = None,
                        font: tuple = ("Segoe UI", 12, "bold")) -> dict:
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
            tint(btn, self._size_sync_beats == APC_BEAT_VALUES[col])

        for col, btn in self._apc_pos_pat_btns:
            name = "Still" if col == 0 else PATTERNS[col - 1][0]
            tint(btn, self._pos_pat_selected == name)
        for col, btn in self._apc_pos_beat_btns:
            tint(btn, self._pos_sync_beats == APC_BEAT_VALUES[col])

        tint(self._apc_master_btns["bpm_sync"],    self._bpm_on.get())
        tint(self._apc_master_btns["audio_sync"],
             self._audio_det is not None and self._audio_det.running)
        tint(self._apc_master_btns["link"],        self._link_on.get())

    # ── Frame cards ──────────────────────────────────────────────────────────

    def _build_frame_cards(self):
        row = tk.Frame(self, bg=BG)
        row.pack(padx=16)
        for col, cfg in enumerate(FRAMES):
            self._frame_card(row, col, cfg)

    def _frame_card(self, parent, col, cfg):
        card = tk.Frame(parent, bg=CARD, padx=14, pady=12)
        card.grid(row=0, column=col, padx=5)

        tk.Label(card, text=cfg["name"],
                 font=("Segoe UI", 10, "bold"), bg=CARD, fg=BLUE).pack()
        tk.Entry(card, textvariable=self._ip_vars[col],
                 font=("Segoe UI", 8), bg=BTN, fg=FG,
                 insertbackground=FG, relief="flat", width=15).pack(pady=(0, 8))

        pv   = self._pvars[col]
        lbls = {}
        for key, text in (("b", "Bottom"), ("t", "Top")):
            r = tk.Frame(card, bg=CARD)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=text, font=("Segoe UI", 9),
                     bg=CARD, fg=FG, width=7, anchor="w").pack(side="left")
            ttk.Scale(r, from_=0, to=100, orient="horizontal",
                      variable=pv[key], length=125).pack(side="left", padx=3)
            lbl = tk.Label(r, text="  0%", font=("Segoe UI", 9, "bold"),
                           bg=CARD, fg=GREEN, width=5)
            lbl.pack(side="left")
            lbls[key] = lbl

            def _on_write(*_, idx=col, k=key):
                self._refresh_lbl(idx, k)
                if not self._bpm_on.get():
                    self._send_frame()

            pv[key].trace_add("write", _on_write)

        self._plbls.append(lbls)

        # Calibration trigger + live status (steps) for this window
        tk.Button(card, text="Calibrate window", font=("Segoe UI", 8),
                  bg=BTN, fg=BTNFG, relief="flat", padx=8, pady=4, cursor="hand2",
                  command=lambda i=col: self._calibrate_window(i)).pack(pady=(8, 2))
        stat = tk.Label(card, text="status: —", font=("Consolas", 8),
                        bg=CARD, fg=DIM, justify="left", anchor="w")
        stat.pack(fill="x")
        self._stat_lbls.append(stat)

    # ── Art-Net controls ─────────────────────────────────────────────────────

    def _build_artnet_controls(self):
        row = tk.Frame(self, bg=BG)
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

    def _build_gap_section(self):
        self._param_block(
            title="GAP POSITION   (0 % = bottom  •  100 % = top)",
            var=self._gap_pos_var, vmax=100, unit="%",
            presets=[("Bottom", 0), ("Centre", 50), ("Top", 100)],
            sync_attr="_pos_sync_btns",  set_sync_fn=self._set_pos_sync,
            pat_attr="_pos_pbts",        set_pat_fn=self._set_pos_pattern)

        _hr(self)

        # Gap Size is a small % of the (tall) window — only a few % is useful.
        self._param_block(
            title="GAP SIZE   (% of window the band opens)",
            var=self._gap_size_var, vmax=25, unit="%",
            presets=[("Closed", 0), ("2%", 2), ("5%", 5),
                     ("10%", 10), ("15%", 15), ("25%", 25)],
            sync_attr="_size_sync_btns", set_sync_fn=self._set_size_sync,
            pat_attr="_size_pbts",       set_pat_fn=self._set_size_pattern)

        # Closed-overlap fine-tune + a full-open park button
        ov = tk.Frame(self, bg=BG)
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
                     sync_attr, set_sync_fn, pat_attr, set_pat_fn):
        """One self-contained block: value slider (0..vmax, labelled with `unit`)
        + presets, beat-sync selector, and an independent pattern selector."""
        sec = tk.Frame(self, bg=BG)
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

    def _build_bpm_section(self):
        sec = tk.Frame(self, bg=BG)
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
        self._beat_offset = -self._current_raw_beat()
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

    def _build_audio_section(self):
        sec = tk.Frame(self, bg=BG)
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
        self._audio_dev_var = tk.StringVar(value=dev_names[0])
        ttk.Combobox(dev_row, textvariable=self._audio_dev_var,
                     values=dev_names, width=38, state="readonly").pack(side="left", padx=4)
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
        self._audio_conf_lbl = tk.Label(st, text="(feeds into BPM clock  •  errors shown here)",
                                         font=("Segoe UI", 9), bg=BG, fg=DIM, anchor="w",
                                         wraplength=600, justify="left")
        self._audio_conf_lbl.pack(side="left", fill="x", expand=True)

        self.after(100, self._poll_audio_level)

    def _toggle_audio(self):
        if self._audio_det is None:
            return
        if self._audio_det.running:
            self._audio_det.stop()
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
            except Exception as exc:
                log.error("Audio start failed: %s", exc, exc_info=True)
                self._audio_conf_lbl.config(text=str(exc), fg=RED)
        self._refresh_apc_leds()
        self._refresh_apc_btn_colors()

    def _on_audio_bpm(self, bpm: float, confidence: float, status: str = "tracking"):
        # Called from the audio background thread — bounce to the main thread.
        self.after(0, lambda b=bpm, c=confidence, s=status: self._apply_audio_bpm(b, c, s))

    def _apply_audio_bpm(self, bpm: float, confidence: float, status: str = "tracking"):
        held = (status == "hold")
        col  = BLUE if held else (GREEN if confidence > 0.6 else YELLOW)
        self._audio_bpm_lbl.config(text=f"{bpm:.2f}", fg=col)
        if _AUBIO_OK:
            filled = int(round(confidence * 5))
            dots   = "●" * filled + "○" * (5 - filled)
            tag    = "HOLD (break)" if held else "lock"
            self._audio_conf_lbl.config(text=f"{tag} {dots} {confidence:.2f}", fg=col)
        else:
            self._audio_conf_lbl.config(text="onset detected", fg=DIM)
        # Drive the BPM clock at full precision (keep the decimals).
        self._bpm_var.set(round(bpm, 3))

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
        if self._audio_det is not None:
            self._audio_det.stop()
        self._stop_midi()
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
        def beat_velocity(sync_beats, col):
            return (self.APC_LED_BEAT_ON_VEL
                    if sync_beats == APC_BEAT_VALUES[col]
                    else self.APC_LED_BEAT_OFF_VEL)

        # Size pattern row (row 0)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_SIZE_PAT, col),
                          pat_velocity(self._size_pat_selected, col))
        # Size beat row (row 1)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_SIZE_BEAT, col),
                          beat_velocity(self._size_sync_beats, col))

        # Position pattern row (row 3)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_POS_PAT, col),
                          pat_velocity(self._pos_pat_selected, col))
        # Position beat row (row 4)
        for col in range(8):
            self._led_set(_apc_clip_note(APC_ROW_POS_BEAT, col),
                          beat_velocity(self._pos_sync_beats, col))

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
        self._led_ring(APC_CC_GAP_SIZE, 0)

    def _chase_leds_tick(self):
        """Beat visualisation on the 8 Device Control knob LED rings + buttons.
        An 8-beat cycle through knobs 0-7. Within each beat:
        - Knob ring: single LED scans clockwise around 15-position ring (1 LED per 1/15th beat)
        - Button: brightness increases then decreases across the beat (velocity 20→120→20)"""
        try:
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
                        self._midi_out.send(mido.Message("note_off", note=prev_btn_note, channel=0))
                    self._chase_last_btn = beat_in_cycle

                # Knob ring: position 0-14 → one LED clockwise around ring
                led_pos = min(14, int(beat_frac * 15))
                self._led_ring(APC_CC_DEVICE_KNOB_BASE + knob_offset,
                               APC_SINGLE_RING_POS[led_pos])

                # Button: brightness pulse (20→120→20 across the beat)
                btn_note = APC_NOTE_DEVICE_BTN_ALL[beat_in_cycle]
                # Velocity follows a triangular wave: 0 → 1 → 0
                vel_triangle = 1.0 - abs(beat_frac * 2.0 - 1.0)  # 0→1→0 from 0→0.5→1
                btn_vel = int(20 + vel_triangle * 100)  # 20→120→20
                self._midi_out.send(mido.Message("note_on", note=btn_note, velocity=btn_vel, channel=0))
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
                    b = APC_BEAT_VALUES[col]
                    self.after(0, lambda v=b: self._set_size_sync(v))
                elif row == APC_ROW_POS_PAT:
                    if col == 0:
                        self.after(0, lambda: self._set_pos_sync(None))
                    elif 1 <= col <= 6:
                        pat = PATTERNS[col - 1]
                        self.after(0, lambda p=pat: self._set_pos_pattern(p[1], p[0]))
                elif row == APC_ROW_POS_BEAT:
                    b = APC_BEAT_VALUES[col]
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
