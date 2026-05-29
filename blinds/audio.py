"""
AudioBPMDetector + get_audio_devices().

Optional dependencies (sounddevice, numpy, aubio, pyaudiowpatch) are imported
here with typed Any aliases so pyright does not flag attribute access on
possibly-None imports.  The AUDIO_AVAILABLE / _AUBIO_OK / _PAW_OK flags are
checked at runtime before any of these are used.
"""

import logging
import math
import time
from typing import Any

log = logging.getLogger("blinds")

# ── Optional imports ──────────────────────────────────────────────────────────

sd: Any = None
np: Any = None
AUDIO_AVAILABLE = False
try:
    import sounddevice as _sd_mod
    import numpy as _np_mod
    sd = _sd_mod
    np = _np_mod
    AUDIO_AVAILABLE = True
except ImportError:
    pass

_aubio: Any = None
_AUBIO_OK = False
try:
    import aubio as _aubio_mod
    _aubio = _aubio_mod
    _AUBIO_OK = True
except ImportError:
    pass

_paw: Any = None
_PAW_OK = False
try:
    import pyaudiowpatch as _paw_mod   # Windows WASAPI loopback support
    _paw = _paw_mod
    _PAW_OK = True
except ImportError:
    pass

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
        self._stream: Any    = None   # sounddevice OR pyaudio stream (different APIs)
        self._paw_inst: Any  = None   # PyAudio instance for loopback streams
        self._paw_chs        = 1
        self._cb             = None
        self._tempo_obj: Any = None   # aubio.tempo instance
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
