"""
AudioBPMDetector + get_audio_devices().

Optional dependencies (sounddevice, numpy, aubio, pyaudiowpatch) are imported
here with typed Any aliases so pyright does not flag attribute access on
possibly-None imports.  The AUDIO_AVAILABLE / _AUBIO_OK / _PAW_OK flags are
checked at runtime before any of these are used.
"""

import collections
import logging
import math
import threading
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
    _MAX_BEATS    = 128     # beat-timestamp ring buffer length (~60 s @ 120 BPM)
    _FFT_N         = 8192   # FFT window size for spectrum (~186 ms / 5.38 Hz bin width)

    # Default band cutoffs (Hz) — adjustable from the UI
    DEFAULT_KICK_LOW    = 40.0
    DEFAULT_KICK_HIGH   = 180.0
    DEFAULT_HIHAT_LOW   = 5000.0
    DEFAULT_HIHAT_HIGH  = 12000.0
    # Band weights in the signal sent to aubio. Kick dominant prevents tempo
    # lock onto the eighth-note hi-hat grid for house/techno.
    DEFAULT_KICK_WEIGHT  = 3.0
    DEFAULT_HIHAT_WEIGHT = 0.6

    def __init__(self):
        self._stream: Any    = None   # sounddevice OR pyaudio stream (different APIs)
        self._paw_inst: Any  = None   # PyAudio instance for loopback streams
        self._paw_chs        = 1
        self._cb             = None
        # _tempo_obj is permanently None — aubio's tempo tracker was retired
        # in favour of madmom (see attach_madmom).  Kept as an attribute only
        # because _init_aubio still touches it during stream open.
        self._tempo_obj: Any = None
        self._rate        = self._RATE
        self._ticks       = 0
        self.level        = 0.0    # RMS — read by UI level meter
        # Optional madmom (RNN+DBN) deep-learning BPM tracker; runs in its own
        # background thread with a rolling audio buffer.  When attached, the
        # detector takes over public BPM/beat reporting.
        self._madmom: Any = None
        # Measured sample-rate diagnostic.  pyaudiowpatch/WASAPI's reported
        # device rate isn't always what's actually being delivered (especially
        # for loopback devices with internal SRC) — measuring over a few
        # seconds gives us the true rate.  Diagnostic only — we no longer
        # auto-correct from it.
        self._rate_measure_t0:        float = 0.0
        self._rate_measure_samples:   int   = 0
        self._rate_measure_done:      bool  = False

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
        # ~20 ms blocks — see _start_paw for rationale
        bs = max(self._HOP, 1024)
        while bs < int(rate * 0.020):
            bs *= 2
        log.info("Audio SD: opening device %d '%s' @ %d Hz (blocksize=%d → %.1f ms)",
                 device_idx, dev.get("name"), rate, bs, bs * 1000.0 / rate)
        self._rate = rate
        self._init_aubio(rate)
        self._stream = sd.InputStream(device=device_idx, channels=1,
                                      samplerate=rate, blocksize=bs,
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
        # Pick a callback period that gives WASAPI enough headroom.  At 96 kHz
        # a 512-sample HOP fires every 5.3 ms which is brutally tight and
        # causes the audible dropouts at stream open.  Targeting ~20 ms per
        # callback by rounding the HOP up to the nearest power of two ≥ 1024
        # is much friendlier to the audio engine without adding meaningful
        # latency (madmom's window is 8 s).
        target_period_ms = 20.0
        fpb = max(self._HOP, 1024)
        while fpb < int(rate * target_period_ms / 1000.0):
            fpb *= 2
        log.info("Audio PAW: opening loopback device %d '%s' @ %d Hz ch=%d "
                 "(frames_per_buffer=%d → %.1f ms/cb)",
                 device_idx, dev.get("name"), rate, chs, fpb,
                 fpb * 1000.0 / rate)
        self._rate    = rate
        self._paw_chs = chs
        self._paw_inst = p
        self._init_aubio(rate)
        self._stream = p.open(
            format=_paw.paFloat32,
            channels=chs,
            rate=rate,
            frames_per_buffer=fpb,
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
        # Aubio's tempo tracker is permanently disabled — madmom is the sole
        # BPM source.  We keep the rest of this initialisation (band filters,
        # FFT buffer, kick rising-edge state) because the spectrum visualizer
        # and the kick-on-drop auto-resync still need them.
        self._tempo_obj = None
        # Reset the sample-rate self-check whenever a new stream is opened
        self._rate_measure_t0      = 0.0
        self._rate_measure_samples = 0
        self._rate_measure_done    = False
        # Configurable band filters (kick + hi-hat) fed into aubio
        self.kick_low_hz    = self.DEFAULT_KICK_LOW
        self.kick_high_hz   = self.DEFAULT_KICK_HIGH
        self.hihat_low_hz   = self.DEFAULT_HIHAT_LOW
        self.hihat_high_hz  = self.DEFAULT_HIHAT_HIGH
        # Per-band weights — kick dominant prevents half-tempo lock
        self.kick_weight    = self.DEFAULT_KICK_WEIGHT
        self.hihat_weight   = self.DEFAULT_HIHAT_WEIGHT
        # Filter states for 4 first-order IIR low-passes (2 bandpasses)
        self._y_kl = 0.0   # kick low cutoff
        self._y_kh = 0.0   # kick high cutoff
        self._y_hl = 0.0   # hi-hat low cutoff
        self._y_hh = 0.0   # hi-hat high cutoff
        # Per-band RMS, updated each frame — exposed for kick detection + UI
        self.kick_rms   = 0.0
        self.hihat_rms  = 0.0
        # Strong-kick rising-edge detector (for auto-resync on drop)
        self._kick_active     = False
        self._kick_baseline   = 0.0   # slow average for the baseline-ratio gate
        self._kick_rms_prev   = 0.0   # previous-frame RMS for the transient gate
        # Butterworth bandpass state (set up by _build_kick_filter).
        # _kick_filter_lock protects writes from the UI thread (set_bands) against
        # reads from the audio callback thread inside _band_signal.
        self._kick_sos: Any = None
        self._kick_zi:  Any = None
        self._kick_filter_lock = threading.Lock()
        # _fft_buf_lock protects the rolling FFT buffer that's written by the
        # audio callback and read by the UI thread inside get_spectrum().
        self._fft_buf_lock = threading.Lock()
        self._recompute_band_alphas()
        self._ticks    = 0
        # Waveform ring buffer of (timestamp, peak_amplitude).  Stores ~18 s
        # of audio at 512-sample hops — covers 16 beats down to ~55 BPM
        # without forcing the UI to scan a 30 s history every frame.
        hop_rate = max(1, rate // self._HOP)
        self._wave_buf: collections.deque = collections.deque(maxlen=hop_rate * 18)
        # FFT buffer for spectrum analyzer (rolling raw-sample window).
        # FFT is only computed lazily when the UI calls get_spectrum() —
        # never on the audio callback thread.
        self._fft_buf = np.zeros(self._FFT_N, dtype=np.float32)
        self._fft_window = np.hanning(self._FFT_N).astype(np.float32)

    # 4th-order Butterworth bandpass for the kick band gives 24 dB/octave
    # rolloff on each side — sharp enough to reject snare body (200-500 Hz)
    # and vocal lows so `kick_rms` actually means "kick energy" and not
    # "anything loud at all".
    KICK_FILTER_ORDER = 4

    def _build_kick_filter(self):
        """(Re)design the steep kick-band Butterworth bandpass.
        Atomic swap under the lock so the audio thread can't see a sos array
        from one design paired with a zi from another."""
        new_sos: Any = None
        new_zi:  Any = None
        if self._rate > 0:
            try:
                from scipy.signal import butter, sosfilt_zi
                nyq = self._rate * 0.5
                lo  = max(20.0, self.kick_low_hz) / nyq
                hi  = min(0.99, self.kick_high_hz / nyq)
                if hi > lo:
                    sos = butter(self.KICK_FILTER_ORDER, [lo, hi],
                                  btype="bandpass", output="sos")
                    new_sos = sos.astype(np.float64)
                    # sosfilt_zi gives the steady-state response to a unit
                    # input; zero the state so we start from quiescence.
                    new_zi  = sosfilt_zi(new_sos) * 0.0
            except ImportError:
                log.warning("scipy not available — kick filter falls back to 1st-order IIR")
        with self._kick_filter_lock:
            self._kick_sos = new_sos
            self._kick_zi  = new_zi

    def _recompute_band_alphas(self):
        """Update IIR coefficients when band cutoffs change.
        Kept for the hi-hat band (still used by the spectrum overlay).
        The kick band now uses a Butterworth bandpass — see _build_kick_filter."""
        if self._rate <= 0:
            return
        k = 2.0 * math.pi / self._rate
        # Hi-hat band IIR coefficients (still used for the spectrum overlay)
        self._a_hl = min(0.95, k * self.hihat_low_hz)
        self._a_hh = min(0.95, k * self.hihat_high_hz)
        # Kick band IIR coefficients kept as a fallback if scipy is missing
        self._a_kl = min(0.95, k * self.kick_low_hz)
        self._a_kh = min(0.95, k * self.kick_high_hz)
        # Rebuild the steep Butterworth bandpass for actual kick detection
        self._build_kick_filter()

    def set_bands(self, kick_low=None, kick_high=None,
                  hihat_low=None, hihat_high=None,
                  kick_weight=None):
        """Adjust band cutoffs + kick weight at runtime (called from UI).
        Any argument left at None keeps its current value."""
        if kick_low  is not None:
            self.kick_low_hz  = max(10.0, min(800.0, float(kick_low)))
        if kick_high is not None:
            self.kick_high_hz = max(self.kick_low_hz + 10.0,
                                    min(1000.0, float(kick_high)))
        if hihat_low is not None:
            self.hihat_low_hz = max(500.0, min(18000.0, float(hihat_low)))
        if hihat_high is not None:
            self.hihat_high_hz = max(self.hihat_low_hz + 100.0,
                                     min(20000.0, float(hihat_high)))
        if kick_weight is not None:
            self.kick_weight = max(0.5, min(10.0, float(kick_weight)))
        self._recompute_band_alphas()

    def _band_signal(self, samples):
        """Weighted sum of kick + hi-hat bandpasses.
        Kick is heavily weighted so aubio locks to the quarter-note grid
        instead of the eighth-note hi-hat grid.
        Side effect: updates per-band RMS for kick detection + UI metering."""
        a_kl, a_kh = self._a_kl, self._a_kh
        a_hl, a_hh = self._a_hl, self._a_hh
        y_kl, y_kh = self._y_kl, self._y_kh
        y_hl, y_hh = self._y_hl, self._y_hh
        wk, wh = self.kick_weight, self.hihat_weight

        out = np.empty_like(samples)
        sumsq_k = 0.0
        sumsq_h = 0.0
        for i in range(len(samples)):
            x = float(samples[i])
            y_kl = a_kl * x + (1.0 - a_kl) * y_kl
            y_kh = a_kh * x + (1.0 - a_kh) * y_kh
            y_hl = a_hl * x + (1.0 - a_hl) * y_hl
            y_hh = a_hh * x + (1.0 - a_hh) * y_hh
            kv = y_kh - y_kl
            hv = y_hh - y_hl
            sumsq_k += kv * kv
            sumsq_h += hv * hv
            out[i] = wk * kv + wh * hv

        self._y_kl, self._y_kh = y_kl, y_kh
        self._y_hl, self._y_hh = y_hl, y_hh
        n = max(1, len(samples))
        self.hihat_rms = math.sqrt(sumsq_h / n)

        # Kick RMS via the steep Butterworth bandpass — rejects snare/vocal
        # leakage that the 1st-order IIR cascade lets through.  If scipy is
        # missing, fall back to the IIR result.  Lock keeps the UI thread
        # from swapping _kick_sos / _kick_zi out from under us.
        with self._kick_filter_lock:
            sos = self._kick_sos
            zi  = self._kick_zi
        if sos is not None:
            try:
                from scipy.signal import sosfilt
                kick_band, new_zi = sosfilt(
                    sos, samples.astype(np.float64), zi=zi)
                with self._kick_filter_lock:
                    # Only persist the new zi if no one swapped the filter
                    # while we were running — otherwise the new zi is for
                    # a stale sos array.
                    if self._kick_sos is sos:
                        self._kick_zi = new_zi
                self.kick_rms = float(np.sqrt(np.mean(kick_band * kick_band)))
            except Exception:
                self.kick_rms = math.sqrt(sumsq_k / n)
        else:
            self.kick_rms = math.sqrt(sumsq_k / n)
        return out

    # ── Kick-detection tunables ──────────────────────────────────────────
    # A kick must clear ALL three of these gates to count:
    #   1. Above the absolute floor (rules out quiet background music)
    #   2. Significantly above the slow baseline (rules out sustained
    #      basslines that occupy the same frequency band as kicks)
    #   3. Positive derivative — i.e. a transient, not a slow ramp (rules
    #      out bass-synth swells / wubs that build up gradually)
    KICK_FLOOR_RMS         = 0.015
    KICK_BASELINE_RATIO    = 1.6    # current must be ≥ this × slow baseline
    KICK_DELTA_THRESHOLD   = 0.015  # min RMS jump per callback to qualify as a transient
    KICK_RELEASE_RATIO     = 1.1    # release when current drops below 1.1× baseline

    def detect_strong_kick(self) -> bool:
        """Three-stage rising-edge detector — sub-bass transient that is also
        much louder than the recent baseline AND above an absolute floor."""
        # Slow baseline (~10 s time constant at 50 fps).  Sustained bass
        # raises this so it stops triggering, while sporadic kicks barely
        # nudge it because their duty cycle is low.
        self._kick_baseline = (0.999 * self._kick_baseline
                                + 0.001 * self.kick_rms)
        # Positive derivative — transients have fast attack, basslines ramp
        delta = self.kick_rms - self._kick_rms_prev
        self._kick_rms_prev = self.kick_rms

        is_kick = False
        above_floor    = self.kick_rms > self.KICK_FLOOR_RMS
        above_baseline = self.kick_rms > self._kick_baseline * self.KICK_BASELINE_RATIO
        has_transient  = delta > self.KICK_DELTA_THRESHOLD

        if (above_floor and above_baseline and has_transient
                and not self._kick_active):
            is_kick = True
            self._kick_active = True
        elif self.kick_rms < self._kick_baseline * self.KICK_RELEASE_RATIO:
            self._kick_active = False
        return is_kick

    def get_spectrum(self):
        """Return (freqs_hz, magnitudes_db) of the current audio buffer.
        Magnitude is in dB FS clamped to roughly [-90, 0]. None when not running."""
        if not AUDIO_AVAILABLE or self._rate <= 0:
            return None
        # Snapshot under the lock so the audio thread can't roll the buffer
        # while we're reading it.  Multiplying by the window in the same step
        # detaches us from the live buffer for the rest of the FFT path.
        with self._fft_buf_lock:
            windowed = self._fft_buf * self._fft_window
        spec = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(self._FFT_N, 1.0 / self._rate)
        mags_db = 20.0 * np.log10(spec / (self._FFT_N * 0.5) + 1e-9)
        return freqs, mags_db

    def _process(self, mono):
        self.level  = float(np.sqrt(np.mean(mono ** 2)))
        self._ticks += 1
        # Record peak amplitude + timestamp for waveform display
        self._wave_buf.append((time.perf_counter(), float(np.max(np.abs(mono)))))
        # Roll the FFT buffer (newest samples at the right end) — used lazily
        # by the UI thread via get_spectrum().  Lock pairs with get_spectrum's
        # snapshot so the UI never sees a half-shifted buffer.
        n = len(mono)
        with self._fft_buf_lock:
            if n < self._FFT_N:
                self._fft_buf[:-n] = self._fft_buf[n:]
                self._fft_buf[-n:] = mono
            else:
                self._fft_buf[:] = mono[-self._FFT_N:]
        self._band_signal(mono)   # side-effect: updates self.kick_rms / self.hihat_rms

        now = time.perf_counter()

        # ── Diagnostic: dropout-aware rate check ───────────────────────────
        # We INTENTIONALLY do NOT use this to override madmom's source_rate
        # anymore — the audio device drops samples for 5-10 s at stream open,
        # which makes any rate computation during that window read low.  We
        # only log the steady-state ratio once enough audio has settled.
        #   Phase 1: ignore the first 12 s (dropout window)
        #   Phase 2: measure over the next 30 s of continuous capture
        if self._rate_measure_t0 == 0.0:
            self._rate_measure_t0 = now
            self._rate_measure_samples = 0
        elif not self._rate_measure_done:
            elapsed = now - self._rate_measure_t0
            if elapsed < 12.0:
                # Dropout window — reset the counter so it starts fresh after
                self._rate_measure_samples = 0
            else:
                self._rate_measure_samples += len(mono)
                if elapsed >= 12.0 + 30.0:
                    measured = self._rate_measure_samples / 30.0
                    ratio = measured / max(self._rate, 1)
                    log.info("Sample-rate (post-settle) configured=%d Hz, "
                             "measured=%.2f Hz over 30 s (ratio %.4f)",
                             self._rate, measured, ratio)
                    self._rate_measure_done = True

        # Feed the deep-learning BPM tracker (runs inference on its own thread)
        if self._madmom is not None:
            try:
                self._madmom.feed(mono, now)
            except Exception:
                pass

        # Aubio's tempo tracker is gone — only the kick rising-edge survives
        # because the auto-resync needs sub-frame latency that the 1.2 s
        # madmom inference cycle can't match.
        self._kick_tick()

    # ── (renamed from _audio_cb — kept for compatibility) ────────────────────
    def _audio_cb(self, indata, frames, _time, _status):
        mono = (indata[:, 0] if indata.shape[1] == 1
                else np.mean(indata, axis=1)).astype(np.float32)
        self._process(mono)

    def _kick_tick(self):
        """Lightweight per-frame loop: only the kick rising-edge for the
        ``is_kick`` callback flag.  No BPM, no aubio, nothing else."""
        is_kick = self.detect_strong_kick()
        if is_kick and self._cb is not None:
            try:
                # Signature matches the old callback so the app side need not
                # know the BPM path went away.  bpm=0, confidence=0 are ignored
                # by _apply_audio_bpm — it only acts on is_kick now.
                self._cb(0.0, 0.0, "tracking", False, True)
            except Exception:
                pass

    # ── Madmom (deep-learning BPM) attachment ────────────────────────────

    def attach_madmom(self, detector: "Any") -> None:
        """Attach an already-started MadmomBPM detector. The audio callback
        will feed its rolling buffer; its BPM/beat output replaces aubio's."""
        self._madmom = detector

    def detach_madmom(self) -> None:
        self._madmom = None
