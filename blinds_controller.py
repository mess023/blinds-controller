#!/usr/bin/env python3
"""
Blinds Controller with BPM Sync
---------------------------------
Controls 4 ESP32 window-blind frames via Art-Net (unicast).
Optionally syncs movement patterns to Ableton Link / DAW BPM.

Requirements:
  Python 3.8+                     (no pip install needed for basic use)
  pip install aalink          (optional – for Ableton Link BPM sync)

Run:
  python blinds_controller.py
"""

import asyncio
import logging
import math
import os
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "blinds_controller.log"),
            encoding="utf-8",
        ),
        logging.StreamHandler(),   # also print to terminal
    ],
)
log = logging.getLogger("blinds")

# ── Ableton Link (optional) — tries link-python first, then aalink ───────────
_link      = None
_link_api  = None   # "link" | "aalink" | None
_link_loop = None   # background asyncio loop used by aalink
LINK_AVAILABLE = False

# link-python is synchronous — no event loop needed
try:
    import importlib as _il
    _lm = _il.import_module("link")
    _link = _lm.Link(120.0)
    try:
        _link.enabled = True
    except AttributeError:
        pass
    _link_api = "link"
    LINK_AVAILABLE = True
except Exception as _e:
    print(f"[Link] link-python: {_e}")

# aalink requires a *running* asyncio event loop even for construction,
# so we spin one up in a daemon thread and create Link inside it.
if not LINK_AVAILABLE:
    try:
        import aalink as _aalink_mod
        _link_loop = asyncio.new_event_loop()
        threading.Thread(target=_link_loop.run_forever,
                         daemon=True, name="aalink-loop").start()

        async def _mk_aalink():
            lnk = _aalink_mod.Link(120.0)
            try:
                lnk.enabled = True
            except AttributeError:
                pass
            return lnk

        _link = asyncio.run_coroutine_threadsafe(
            _mk_aalink(), _link_loop).result(timeout=5.0)
        _link_api = "aalink"
        LINK_AVAILABLE = True
        print("[Link] aalink loaded OK")
    except Exception as _e:
        import traceback as _tb
        print(f"[Link] aalink: {_e}")
        _tb.print_exc()

def _lnk_peers() -> int:
    if _link_api == "aalink":
        return int(_link.num_peers)         # type: ignore[union-attr]
    return int(_link.numPeers())            # type: ignore[union-attr]

def _lnk_get_tempo() -> float:
    """Return current tempo in BPM — works for both aalink and link-python."""
    if _link_api == "aalink":
        return float(_link.tempo)           # type: ignore[union-attr]
    state = _link.captureSessionState()     # type: ignore[union-attr]
    return float(state.tempo())

def _lnk_get_beat() -> float:
    """Return current beat counter — works for both aalink and link-python."""
    if _link_api == "aalink":
        return float(_link.beat)            # type: ignore[union-attr]
    state  = _link.captureSessionState()    # type: ignore[union-attr]
    micros = _link.clock().micros()         # type: ignore[union-attr]
    return float(state.beatAtTime(micros, 4.0))

# ── Audio BPM detection (optional) ───────────────────────────────────────────
try:
    import sounddevice as sd
    import numpy as np
    AUDIO_AVAILABLE = True
except ImportError:
    sd = np = None
    AUDIO_AVAILABLE = False

try:
    import aubio as _aubio
    _AUBIO_OK = True
except ImportError:
    _aubio = None
    _AUBIO_OK = False

try:
    import pyaudiowpatch as _paw   # Windows WASAPI loopback support
    _PAW_OK = True
except ImportError:
    _paw = None
    _PAW_OK = False

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

# ── Art-Net ───────────────────────────────────────────────────────────────────

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

def _to16(pct: float):
    v = max(0, min(65535, int(float(pct) / 100.0 * 65535)))
    return (v >> 8) & 0xFF, v & 0xFF

def _artnet_packet(dmx: bytes) -> bytes:
    return (b'Art-Net\x00'
            + struct.pack('<H', 0x5000)
            + struct.pack('>H', 14)
            + b'\x00\x00'
            + struct.pack('<H', UNIVERSE)
            + struct.pack('>H', 512)
            + dmx)

def send_universe(targets, positions):
    """Send ONE full universe carrying all windows.

    positions : list of (bottom_pct, top_pct) — one tuple per window, in the
                same order as FRAMES (window i → DMX address FRAMES[i]["dmx"]).
    targets   : list of destination IP strings (deduplicated before sending).
                In broadcast mode all entries are identical → a single packet.
    """
    dmx = bytearray(512)
    for cfg, (bottom_pct, top_pct) in zip(FRAMES, positions):
        off = cfg["dmx"] - 1                      # DMX address 1 → byte index 0
        dmx[off],     dmx[off + 1] = _to16(bottom_pct)
        dmx[off + 2], dmx[off + 3] = _to16(top_pct)
    pkt = _artnet_packet(bytes(dmx))
    for ip in dict.fromkeys(targets):             # dedupe, preserve order
        try:
            _sock.sendto(pkt, (ip, ARTNET_PORT))
        except OSError:
            pass

# ── OSC (calibration triggers + status telemetry) ────────────────────────────
#
# Minimal hand-rolled OSC so no extra dependency is needed.
# We send:    /calibrate, /btm/calibrate, /top/calibrate, /status   (no args)
# We receive: /status  with 8 ints:
#   [cal0, homed0, max0, pos0, cal1, homed1, max1, pos1]
# and map each reply to a window by the packet's source IP.

def _osc_string(s: str) -> bytes:
    b = s.encode("ascii") + b"\x00"
    return b + b"\x00" * ((4 - len(b) % 4) % 4)

def _osc_message(addr: str, *int_args: int) -> bytes:
    msg = _osc_string(addr) + _osc_string("," + "i" * len(int_args))
    for a in int_args:
        msg += struct.pack(">i", int(a))
    return msg

def _osc_read_string(data: bytes, pos: int):
    end = data.index(b"\x00", pos)
    s = data[pos:end].decode("ascii", "replace")
    field = (end - pos) + 1
    field += (4 - field % 4) % 4
    return s, pos + field

def _osc_parse(data: bytes):
    """Return (address, [args]). Supports int and float args."""
    addr, pos = _osc_read_string(data, 0)
    if pos >= len(data):
        return addr, []
    tags, pos = _osc_read_string(data, pos)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack_from(">i", data, pos)[0]); pos += 4
        elif t == "f":
            args.append(struct.unpack_from(">f", data, pos)[0]); pos += 4
        elif t == "s":
            _, pos = _osc_read_string(data, pos)
    return addr, args

# Dedicated socket: polls go out from it, ESP replies come back to it (the
# firmware replies to remoteIP:remotePort, i.e. this socket's address).
_osc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_osc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
_osc_sock.settimeout(0.25)

def osc_send(ip: str, addr: str, *int_args: int):
    try:
        _osc_sock.sendto(_osc_message(addr, *int_args), (ip, OSC_PORT))
    except OSError:
        pass

# ── Beat Clock (used when Ableton Link is absent) ────────────────────────────

class BeatClock:
    def __init__(self, bpm: float = 120.0):
        self._bpm   = bpm
        self._start = time.perf_counter()
        self._taps  = []

    @property
    def bpm(self) -> float:
        return self._bpm

    @bpm.setter
    def bpm(self, value: float):
        beat        = self.beat            # preserve beat position across BPM changes
        self._bpm   = max(40.0, min(300.0, float(value)))
        self._start = time.perf_counter() - beat * 60.0 / self._bpm

    @property
    def beat(self) -> float:
        return (time.perf_counter() - self._start) * self._bpm / 60.0

    def resync(self):
        """Snap beat counter to 0 at this instant — use before the downbeat."""
        self._start = time.perf_counter()

    def nudge(self, beats: float):
        """Shift pattern phase by `beats` (+ = patterns run ahead, − = run behind)."""
        self._start -= beats * 60.0 / self._bpm

    def tap(self) -> float:
        now = time.perf_counter()
        self._taps = [t for t in self._taps if now - t < 4.0]
        self._taps.append(now)
        if len(self._taps) >= 2:
            gaps     = [self._taps[i+1] - self._taps[i] for i in range(len(self._taps) - 1)]
            self.bpm = 60.0 / (sum(gaps) / len(gaps))
        return self._bpm

# ── Audio device enumeration ─────────────────────────────────────────────────

def get_audio_devices():
    """Return [(label, index, is_loopback, source), ...] for all usable inputs.

    source is "sd" (sounddevice) or "paw" (pyaudiowpatch).
    pyaudiowpatch is used for loopback when available — it handles Windows
    WASAPI channel-format matching automatically.
    """
    result = []

    # Regular inputs (microphone, line-in) via sounddevice
    if AUDIO_AVAILABLE:
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] > 0:
                    result.append((dev["name"], idx, False, "sd"))
        except Exception:
            pass

    # Loopback devices via pyaudiowpatch (preferred — avoids channel-count errors)
    if _PAW_OK:
        try:
            p = _paw.PyAudio()
            for i in range(p.get_device_count()):
                dev = p.get_device_info_by_index(i)
                if dev.get("isLoopbackDevice"):
                    result.append((f"[Loopback] {dev['name']}", i, True, "paw"))
            p.terminate()
        except Exception:
            pass

    return result

# ── Real-time BPM detector ────────────────────────────────────────────────────

class AudioBPMDetector:
    """Captures audio and fires a callback with (bpm, confidence) on each beat.

    For microphone / line-in:  uses sounddevice (pip install sounddevice numpy)
    For Windows loopback:      uses pyaudiowpatch (pip install pyaudiowpatch)
    For BPM detection:         uses aubio when available (pip install aubio)
    """

    _HOP  = 512
    _RATE = 44100

    # ── Tier-1 stability wrapper tunables ────────────────────────────────────
    _MAX_BEATS    = 64      # beat-timestamp ring buffer length (~30 s @ 120 BPM)
    _MIN_BEATS    = 6       # min beats before a computed BPM is trusted
    _BPM_FOLD_LO  = 85.0    # octave-fold window [lo, 2*lo) — house/techno range
    _GATE_FREEZE  = 0.10    # enter HOLD when RMS < this * recent peak
    _GATE_RESUME  = 0.20    # candidate-resume when RMS > this * recent peak
    _RESUME_BEATS = 4       # consecutive fresh beats required to leave HOLD

    def __init__(self):
        self._stream      = None
        self._paw_inst    = None   # PyAudio instance for loopback streams
        self._paw_chs     = 1
        self._cb          = None
        self._tempo_obj   = None
        self._onsets      = []
        self._e_prev      = 0.0
        self._rate        = self._RATE
        self._ticks       = 0
        self.level        = 0.0    # RMS — read by UI level meter
        self._lp_y        = 0.0    # IIR low-pass state (bass pre-filter)
        self._lp_alpha    = 0.03   # updated per stream in _init_aubio
        # Tier-1 stability state
        self._beat_times  = []     # perf_counter timestamps of detected beats
        self._locked_bpm  = 0.0    # last confident BPM (held through breaks)
        self._lock_conf   = 0.0    # confidence from inter-beat-interval consistency
        self._frozen      = False  # HOLD state during beatless sections
        self._fresh       = 0      # fresh confident beats since audio returned
        self._rms_peak    = 1e-6   # adaptive loudness reference for the gate

    # ── public ────────────────────────────────────────────────────────────────

    def start(self, device_idx: int, loopback: bool, callback, source: str = "sd"):
        self.stop()
        if source == "paw":
            self._start_paw(device_idx, callback)
        else:
            self._start_sd(device_idx, callback)

    def stop(self):
        if self._stream is not None:
            try:
                if hasattr(self._stream, "stop_stream"):   # pyaudio stream
                    self._stream.stop_stream()
                    self._stream.close()
                else:                                       # sounddevice stream
                    self._stream.stop()
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._paw_inst is not None:
            try:
                self._paw_inst.terminate()
            except Exception:
                pass
            self._paw_inst = None

    @property
    def running(self) -> bool:
        if self._stream is None:
            return False
        if hasattr(self._stream, "is_active"):   # pyaudio
            return bool(self._stream.is_active())
        return bool(self._stream.active)          # sounddevice

    # ── sounddevice path (mic / line-in) ─────────────────────────────────────

    def _start_sd(self, device_idx: int, callback):
        self._cb = callback
        dev  = sd.query_devices(device_idx)
        rate = int(dev.get("default_samplerate", self._RATE))
        log.info("Audio SD: opening device %d '%s' @ %d Hz", device_idx, dev.get("name"), rate)
        self._rate = rate
        self._init_aubio(rate)
        self._stream = sd.InputStream(device=device_idx, channels=1,
                                      samplerate=rate, blocksize=self._HOP,
                                      dtype="float32", callback=self._sd_cb)
        self._stream.start()
        log.info("Audio SD: stream started")

    def _sd_cb(self, indata, frames, _time, _status):
        mono = indata[:, 0].astype(np.float32)
        self._process(mono)

    # ── pyaudiowpatch path (Windows loopback) ─────────────────────────────────

    def _start_paw(self, device_idx: int, callback):
        self._cb = callback
        p   = _paw.PyAudio()
        dev = p.get_device_info_by_index(device_idx)
        rate = int(dev["defaultSampleRate"])
        chs  = int(dev["maxInputChannels"])
        log.info("Audio PAW: opening loopback device %d '%s' @ %d Hz ch=%d",
                 device_idx, dev.get("name"), rate, chs)
        self._rate    = rate
        self._paw_chs = chs
        self._paw_inst = p
        self._init_aubio(rate)
        self._stream = p.open(
            format=_paw.paFloat32,
            channels=chs,
            rate=rate,
            frames_per_buffer=self._HOP,
            input=True,
            input_device_index=device_idx,
            stream_callback=self._paw_cb,
        )
        self._stream.start_stream()

    def _paw_cb(self, in_data, frame_count, _time_info, _status):
        samples = np.frombuffer(in_data, dtype=np.float32).copy()
        if self._paw_chs > 1:
            mono = np.mean(samples.reshape(-1, self._paw_chs), axis=1).astype(np.float32)
        else:
            mono = samples
        self._process(mono)
        return (None, _paw.paContinue)

    # ── shared processing ─────────────────────────────────────────────────────

    def _init_aubio(self, rate: int):
        if _AUBIO_OK:
            # "energy" onset method tracks kick drums / snares better than
            # the default spectral-flux method for EDM/club music.
            # Larger buf_size (×8 instead of ×4) gives aubio more context.
            try:
                self._tempo_obj = _aubio.tempo("energy", self._HOP * 8, self._HOP, rate)
            except Exception:
                self._tempo_obj = _aubio.tempo("default", self._HOP * 4, self._HOP, rate)
        else:
            self._tempo_obj = None
        # Bass pre-filter: first-order IIR low-pass at ~250 Hz
        # Focuses aubio on the kick-drum frequency range where beats live.
        fc = 250.0
        self._lp_alpha = min(0.95, 2.0 * math.pi * fc / rate)
        self._ticks    = 0
        self._onsets   = []
        self._e_prev   = 0.0
        self._lp_y     = 0.0
        # reset stability state
        self._beat_times = []
        self._locked_bpm = 0.0
        self._lock_conf  = 0.0
        self._frozen     = False
        self._fresh      = 0
        self._rms_peak   = 1e-6

    def _bass_filter(self, samples):
        """First-order IIR low-pass — keeps kick/snare range, cuts hi-hats/voice."""
        a   = self._lp_alpha
        b   = 1.0 - a
        out = np.empty_like(samples)
        y   = self._lp_y
        for i in range(len(samples)):
            y = a * float(samples[i]) + b * y
            out[i] = y
        self._lp_y = y
        return out

    def _process(self, mono):
        self.level  = float(np.sqrt(np.mean(mono ** 2)))
        self._ticks += 1
        bass = self._bass_filter(mono)   # focus aubio on kick/snare frequencies
        if _AUBIO_OK and self._tempo_obj is not None:
            self._aubio_tick(bass)
        else:
            self._fallback_tick(bass)

    # ── (renamed from _audio_cb — kept for compatibility) ────────────────────
    def _audio_cb(self, indata, frames, _time, _status):
        mono = (indata[:, 0] if indata.shape[1] == 1
                else np.mean(indata, axis=1)).astype(np.float32)
        self._process(mono)

    def _aubio_tick(self, mono):
        # Advance aubio every hop (keeps its internal tracking state moving).
        beat    = self._tempo_obj(mono)
        is_beat = bool(beat[0])
        now     = time.perf_counter()
        rms     = self.level   # full-band RMS, set in _process before this call

        # ── Adaptive loudness gate (hysteresis) ──────────────────────────────
        # Peak tracks recent loudness while active; held fixed during a break so
        # a multi-minute quiet section keeps the gate referenced to the music.
        if not self._frozen:
            self._rms_peak = max(rms, self._rms_peak * 0.9995)
        else:
            self._rms_peak = max(rms, self._rms_peak)
        peak  = max(self._rms_peak, 1e-6)
        quiet = rms < self._GATE_FREEZE * peak
        loud  = rms > self._GATE_RESUME * peak

        if not self._frozen and quiet:
            # Enter HOLD: keep the last locked BPM, drop stale beats so the gap
            # across the break can't corrupt the next interval estimate.
            self._frozen = True
            self._beat_times = []
            self._fresh = 0

        if is_beat:
            if self._frozen:
                if loud:
                    # Collect fresh evidence; only leave HOLD after enough beats
                    # (a single stray beat after silence won't unfreeze us).
                    self._beat_times.append(now)
                    self._fresh += 1
                    if self._fresh >= self._RESUME_BEATS:
                        self._frozen = False
            else:
                self._beat_times.append(now)
            if len(self._beat_times) > self._MAX_BEATS:
                self._beat_times.pop(0)
            if not self._frozen:
                bpm, conf = self._recompute()
                if bpm is not None:
                    self._locked_bpm = bpm
                    self._lock_conf  = conf

        # Report on beats AND ~2x/sec so the display + clock never stall.
        if (is_beat or self._ticks % 43 == 0) and self._cb and self._locked_bpm > 0:
            status = "hold" if self._frozen else "tracking"
            self._cb(self._locked_bpm, self._lock_conf, status)

    def _octave_fold(self, bpm: float, ref: float = 0.0) -> float:
        """Fold octave (half/double) errors. With a reference (the locked BPM),
        pick the multiple closest to it; otherwise fold into [LO, 2*LO)."""
        if bpm <= 0:
            return bpm
        if ref > 0:
            best = bpm
            for m in (0.25, 0.5, 1.0, 2.0, 4.0):
                if abs(bpm * m - ref) < abs(best - ref):
                    best = bpm * m
            return best
        lo = self._BPM_FOLD_LO
        while bpm < lo:
            bpm *= 2.0
        while bpm >= lo * 2.0:
            bpm /= 2.0
        return bpm

    def _recompute(self):
        """Stable BPM from the beat-time ring buffer: median picks the dominant
        interval cluster, mean of the inliers gives sub-decimal precision, and
        interval consistency yields a meaningful confidence. (None, 0.0) if not
        enough data yet."""
        if len(self._beat_times) < self._MIN_BEATS:
            return None, 0.0
        ibis = np.diff(np.array(self._beat_times))
        ibis = ibis[(ibis > 0.25) & (ibis < 2.0)]      # 30–240 BPM plausible
        if len(ibis) < self._MIN_BEATS - 1:
            return None, 0.0
        med = float(np.median(ibis))
        inliers = ibis[(ibis > med * 0.75) & (ibis < med * 1.25)]
        if len(inliers) < 3:
            inliers = ibis
        period = float(np.mean(inliers))               # averaged → steady decimals
        if period <= 0:
            return None, 0.0
        bpm = self._octave_fold(60.0 / period, self._locked_bpm)
        cv  = float(np.std(inliers) / period)          # interval spread
        conf = max(0.0, min(1.0, 1.0 - cv * 6.0)) * min(1.0, len(inliers) / 16.0)
        return bpm, conf

    def _fallback_tick(self, mono):
        energy = float(np.mean(mono ** 2))
        # Simple flux onset: energy spikes relative to smoothed background
        if energy > self._e_prev * 2.5 and energy > 5e-5:
            now = time.perf_counter()
            self._onsets = [t for t in self._onsets if now - t < 6.0]
            self._onsets.append(now)
            if len(self._onsets) >= 4:
                itvs = np.diff(self._onsets)
                itvs = itvs[(itvs > 0.25) & (itvs < 1.5)]  # 40–240 BPM range
                if len(itvs) >= 2:
                    bpm = 60.0 / float(np.median(itvs))
                    if self._cb:
                        self._cb(bpm, 0.5, "tracking")   # 0.5 = estimated
        self._e_prev = energy * 0.3 + self._e_prev * 0.7

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
]

# Global speed multiplier applied to eff_beat before the per-parameter divisions.
SPEEDS = [("⅛×", 0.125), ("¼×", 0.25), ("½×", 0.5), ("1×", 1.0), ("2×", 2.0)]

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

        # Per-window status from the firmware (updated by the telemetry thread).
        # keys: cal[2], homed[2], max[2], pos[2], seen(bool/timestamp)
        self._status = [{"cal": [0, 0], "homed": [0, 0], "max": [0, 0],
                         "pos": [0, 0], "seen": 0.0} for _ in FRAMES]
        self._stat_lbls: list = []   # per-window status labels — filled in _frame_card

        self._build_ui()
        self._open_preview()                  # preview lives in its own window
        self._bpm_on.trace_add("write", self._on_bpm_toggle)
        threading.Thread(target=self._anim_loop, daemon=True).start()
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        self._heartbeat()
        self._poll_link()
        self.after(300, self._refresh_status_labels)
        self.after(120, self._draw_preview)   # first draw after layout
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ═════════════════════════════════════════════════════════════════════════
    # UI construction
    # ═════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        tk.Label(self, text="BLINDS CONTROLLER",
                 font=("Segoe UI", 15, "bold"), bg=BG, fg=BLUE).pack(pady=(16, 2))
        tk.Label(self, text=f"Art-Net unicast  •  Universe {UNIVERSE}",
                 font=("Segoe UI", 9), bg=BG, fg=DIM).pack()

        _hr(self, top=10, bot=10)
        self._build_frame_cards()
        self._build_artnet_controls()
        _hr(self)
        self._build_gap_section()
        _hr(self)
        self._build_bpm_section()
        _hr(self)
        self._build_audio_section()
        tk.Frame(self, bg=BG, height=14).pack()

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

        # Manual-mode traces: push static values to all frames when sliders move
        for v in (self._gap_pos_var, self._gap_size_var):
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
        pos  = max(0.0, min(1.0, self._gap_pos_var.get()  / 100.0))
        size = max(0.0, min(1.0, self._gap_size_var.get() / 100.0))
        self._suspend_send = True
        for pv in self._pvars:
            pv["t"].set((1.0 - pos) * (1.0 - size) * 100.0)
            pv["b"].set(pos * (1.0 - size) * 100.0)
        self._suspend_send = False
        self._send_frame()

    def _set_size_sync(self, beats: "float | None"):
        self._size_sync_beats = beats
        for btn, (_, v) in zip(self._size_sync_btns, BEAT_OPTIONS):
            btn.config(bg=BTNSEL if v == beats else BTN,
                       fg=BTNSELFG if v == beats else BTNFG)

    def _set_pos_sync(self, beats: "float | None"):
        self._pos_sync_beats = beats
        for btn, (_, v) in zip(self._pos_sync_btns, BEAT_OPTIONS):
            btn.config(bg=BTNSEL if v == beats else BTN,
                       fg=BTNSELFG if v == beats else BTNFG)

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
        for n, b in self._pos_pbts.items():
            b.config(bg=BTNSEL if n == name else BTN,
                     fg=BTNSELFG if n == name else BTNFG)

    def _set_size_pattern(self, fn, name: str):
        self._size_pat_fn = fn
        for n, b in self._size_pbts.items():
            b.config(bg=BTNSEL if n == name else BTN,
                     fg=BTNSELFG if n == name else BTNFG)

    def _on_bpm_toggle(self, *_):
        if self._bpm_on.get():
            # Sync slew tracker to current slider positions so the first tick
            # doesn't see a huge "jump" from 0,0 and rate-limit away from it.
            self._cur_pos = [
                (self._pvars[i]["b"].get(), self._pvars[i]["t"].get())
                for i in range(len(FRAMES))
            ]
            self._last_apply = time.perf_counter()

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
        self.destroy()

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

            # pos=0 → top blind fully extended (gap at bottom)
            # pos=1 → bottom blind fully extended (gap at top)
            t_pct = (1.0 - pos) * (1.0 - size) * 100.0
            b_pct = pos * (1.0 - size) * 100.0
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

# ── Utilities ─────────────────────────────────────────────────────────────────

def _hr(parent, top=8, bot=8):
    tk.Frame(parent, bg=BTN, height=1).pack(fill="x", padx=16,
                                             pady=(top, bot))

def _hov(btn):
    btn.bind("<Enter>", lambda e: btn.config(bg=BTNHOV))
    btn.bind("<Leave>", lambda e: btn.config(bg=BTN))

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = BlindsApp()
    app.mainloop()
