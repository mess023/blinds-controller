"""
Madmom-based BPM + beat detector.

Runs a deep-learning beat tracker (RNN beat activation + Dynamic Bayesian
Network decoding) on a rolling audio buffer in a background thread.  The
detector exposes:

    detector.bpm           — current decimal BPM (e.g. 124.73)
    detector.confidence    — 0..1 quality estimate
    detector.last_beat_t   — perf_counter timestamp of the most recently
                              detected beat (used for phase locking)
    detector.beat_times    — list of recent beat times (perf_counter clock)

Why madmom: it's the de-facto state-of-the-art ML beat tracker.  Unlike
TempoEstimationProcessor (which rounds to integers), beat-interval-derived
BPM gives sub-decimal precision (median over many beats averages out
detection jitter).

Heavy imports (madmom, scipy) happen inside ``start()`` so the renderer
keeps starting up fast when this detector isn't enabled.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional

import numpy as np

log = logging.getLogger("blinds")


def is_available() -> bool:
    """Probe whether madmom is importable.  The NumPy alias shim that madmom
    requires is applied by ``blinds.__init__`` before any module here runs."""
    try:
        from madmom.features.beats import (RNNBeatProcessor,         # noqa
                                            DBNBeatTrackingProcessor)  # noqa
        return True
    except Exception:
        return False


class MadmomBPM:
    """Rolling-buffer beat tracker.

    Audio is pushed in via ``feed()`` from the audio thread.  A worker
    thread periodically runs RNN + DBN on the most recent ``window_s``
    seconds and updates the public state atomically.
    """

    # The pre-trained RNN model is fixed at 44.1 kHz.  ``source_rate`` is the
    # rate of the audio captured upstream (loopback devices like the Roland
    # Rubix run at 48 kHz on Windows).  We resample at inference time so the
    # buffer can stay at the source rate.
    MODEL_RATE       = 44100
    INFERENCE_RATE_S = 1.00
    MAX_BEATS_HIST   = 64
    BPM_EMA_ALPHA    = 0.35

    # Back-compat: some code still reads .SAMPLE_RATE — keep it as the model rate
    SAMPLE_RATE      = MODEL_RATE

    def __init__(self, window_s: float = 8.0,
                  source_rate: int = MODEL_RATE) -> None:
        # NumPy alias shim is applied by blinds/__init__.py at import time;
        # by the time we get here, np.float / np.int / np.bool already exist.
        from madmom.features.beats import (RNNBeatProcessor,
                                            DBNBeatTrackingProcessor)
        # Models are cached on disk after first download; instantiation is fast
        self._rnn = RNNBeatProcessor()
        # CRITICAL: num_tempi=None in madmom's default uses INTEGER frame
        # intervals at fps=100, which gives ~1 BPM resolution and *quantises*
        # the detected BPM to weird values (124 BPM → 125.000 because the
        # nearest integer-frame interval is 48 frames = exactly 60·100/48).
        # Bumping the log-spaced state-space to 10 000 tempi brings the
        # discretisation down to ~0.03 BPM at 120 BPM — far below natural
        # tempo jitter in real music.
        self._dbn = DBNBeatTrackingProcessor(fps=100, num_tempi=10000,
                                              min_bpm=55, max_bpm=215)

        # Rolling audio ring buffer — stored at the SOURCE rate; resampled
        # to MODEL_RATE only when an inference cycle starts.
        self._source_rate = int(source_rate)
        self._buf_size = int(window_s * self._source_rate)
        self._buf      = np.zeros(self._buf_size, dtype=np.float32)
        self._buf_lock = threading.Lock()
        log.info("MadmomBPM: source_rate=%d, model_rate=%d, buffer=%.1fs",
                 self._source_rate, self.MODEL_RATE,
                 self._buf_size / self._source_rate)
        # perf_counter wall-clock at which the LAST sample in self._buf was captured
        self._buf_last_t = 0.0
        # Wall-clock at which the FIRST sample in self._buf was captured
        # (computed when we need it; this avoids carrying state)

        # Public state — written by worker, read from any thread.
        # _beat_times_lock protects beat_times against torn reads during the
        # worker's clear() + extend() pair on every inference cycle.  Float
        # scalars (bpm, confidence, last_beat_t) are written atomically thanks
        # to the GIL so they don't need the same protection.
        self.bpm: float                = 0.0
        self.confidence: float         = 0.0
        self.last_beat_t: float        = 0.0
        self.beat_times: deque         = deque(maxlen=self.MAX_BEATS_HIST)
        self._beat_times_lock          = threading.Lock()
        self.running: bool             = False
        self._stop                     = threading.Event()
        self._worker: Optional[threading.Thread] = None
        # HOLD / arm-for-resync state.  When confidence drops we enter HOLD;
        # once we exit HOLD after a sustained period the *next* kick on the
        # audio thread triggers an auto-resync (i.e. "snap to the drop").
        self._in_hold: bool            = False
        self._hold_started_at: float   = 0.0
        self.armed_for_resync: bool    = False

    # ── public API ───────────────────────────────────────────────────────

    def snapshot_beat_times(self) -> list:
        """Atomic snapshot of beat_times for cross-thread readers.
        Returns a plain list so callers can iterate without holding the lock."""
        with self._beat_times_lock:
            return list(self.beat_times)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                         name="madmom-bpm")
        self._worker.start()
        log.info("MadmomBPM started (window=%.1fs)",
                 self._buf_size / self.SAMPLE_RATE)

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None

    def feed(self, mono: np.ndarray, sample_t: float) -> None:
        """Append a mono audio chunk to the rolling buffer.
        ``sample_t`` is the perf_counter time of the LAST sample in ``mono``."""
        n = len(mono)
        if n == 0:
            return
        with self._buf_lock:
            if n >= self._buf_size:
                self._buf[:] = mono[-self._buf_size:]
            else:
                self._buf[:-n] = self._buf[n:]
                self._buf[-n:] = mono
            self._buf_last_t = sample_t

    # ── worker ───────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # First inference can take ~1s as the RNN warms up the CPU caches.
        # Wait for some real audio to land before bothering.
        time.sleep(1.0)
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception as exc:
                log.warning("MadmomBPM tick failed: %s", exc)
            # Pace ourselves so we don't pin a core
            dt = time.perf_counter() - t0
            sleep = max(0.05, self.INFERENCE_RATE_S - dt)
            self._stop.wait(sleep)

    def _tick(self) -> None:
        with self._buf_lock:
            snapshot   = self._buf.copy()
            buf_end_t  = self._buf_last_t
        # If the buffer is mostly silent, skip the (expensive) inference but
        # ENTER HOLD so phase-arming still works during silent breakdowns —
        # otherwise we'd return here without ever flagging the silence.
        rms = float(np.sqrt(np.mean(snapshot * snapshot)))
        if rms < 0.001:
            if self.bpm > 0 and not self._in_hold:
                self._in_hold = True
                self._hold_started_at = time.perf_counter()
                log.info("MadmomBPM: entering HOLD (silent buffer)")
                self.confidence = 0.0
            return

        # Resample from source rate → model rate (44.1 kHz) so the RNN sees
        # the audio at the rate it was trained on.  Without this the BPM is
        # off by exactly source_rate / 44100 — e.g. 48 kHz loopback reports
        # 91.875 % of the true tempo, so 125 BPM → 114.84.
        if self._source_rate != self.MODEL_RATE:
            from scipy.signal import resample_poly
            snapshot = resample_poly(
                snapshot, self.MODEL_RATE, self._source_rate
            ).astype(np.float32)

        try:
            activations = self._rnn(snapshot)
            beats_s     = self._dbn(activations)  # seconds, relative to buffer start
        except Exception as exc:
            log.warning("madmom inference failed: %s", exc)
            return
        if len(beats_s) < 4:
            return

        # Map beat times (in seconds, relative to buffer start) into the wall
        # clock.  Buffer length in real time is determined by the SOURCE rate
        # (not the model rate) since that's how it was captured.
        buf_start_t = buf_end_t - self._buf_size / self._source_rate
        beats_pc    = beats_s + buf_start_t

        # ── BPM via least-squares regression on beat times ────────────────
        # We fit b[i] ≈ phi + i·T over the visible beats and report
        # bpm = 60/T.  This averages out per-beat jitter and is therefore
        # more accurate than 60/median(intervals), which is bounded by the
        # DBN's discretised tempo states.
        # First reject outliers (skipped/extra beats) via inter-beat sanity
        intervals = np.diff(beats_s)
        valid_int = intervals[(intervals > 0.25) & (intervals < 1.5)]
        if len(valid_int) < 3:
            return
        med_int = float(np.median(valid_int))
        # Mask beats whose preceding interval is within ±25 % of the median
        keep = np.r_[True, (intervals > med_int * 0.75)
                              & (intervals < med_int * 1.25)]
        clean_beats = beats_s[keep]
        if len(clean_beats) < 4:
            return
        idx = np.arange(len(clean_beats), dtype=np.float64)
        slope, _ = np.polyfit(idx, clean_beats, 1)
        if slope <= 0:
            return
        new_bpm = 60.0 / float(slope)

        # Confidence: residual scatter around the fitted line, normalised
        # to the period.  Tight residuals → high confidence.
        residuals = clean_beats - np.polyval((slope, _), idx)
        cv = float(np.std(residuals) / slope) if slope > 0 else 1.0
        conf = max(0.0, min(1.0, 1.0 - cv * 12.0))

        # ── HOLD during low-confidence sections (breakdowns, transitions) ──
        # Otherwise madmom flaps to half-time / random low BPMs when the
        # rhythm thins out, and the gap timer for auto-resync gets reset
        # because last_beat_t still updates.  Hold the previous BPM,
        # last_beat_t and beat_times — but DO publish the confidence so the
        # UI still shows that we're in a degraded state.
        MIN_HOLD_CONF      = 0.40
        EXIT_HOLD_CONF     = 0.55     # hysteresis — looser exit threshold
        MIN_HOLD_TIME_S    = 3.0      # don't arm resync for sub-3s flickers
        now_pc = time.perf_counter()

        if conf < MIN_HOLD_CONF and self.bpm > 0:
            # Entering / staying in HOLD
            if not self._in_hold:
                self._in_hold = True
                self._hold_started_at = now_pc
                log.info("MadmomBPM: entering HOLD (conf=%.2f)", conf)
            self.confidence = conf
            return

        if self._in_hold and conf >= EXIT_HOLD_CONF:
            # Exiting HOLD.  If the breakdown was long enough to be a real
            # one (not just a confidence flicker mid-track), arm the kick
            # path so the next strong kick re-anchors the phase to the drop.
            held_for = now_pc - self._hold_started_at
            self._in_hold = False
            if held_for >= MIN_HOLD_TIME_S:
                self.armed_for_resync = True
                log.info("MadmomBPM: exiting HOLD after %.1f s — armed for "
                         "auto-resync on next kick", held_for)
            else:
                log.debug("MadmomBPM: brief HOLD (%.2f s), not arming", held_for)

        # Publish (with a touch of cross-cycle EMA smoothing on BPM so the
        # natural ±0.1 BPM regression jitter doesn't propagate into the clock)
        a = self.BPM_EMA_ALPHA
        self.bpm = (a * new_bpm + (1 - a) * self.bpm) if self.bpm > 0 else new_bpm
        self.confidence  = conf
        self.last_beat_t = float(beats_pc[-1])
        # Atomic clear+extend under the lock — UI readers (snapshot_beat_times)
        # never observe an empty/half-filled deque mid-publish.
        new_beats = [float(t) for t in beats_pc[-self.MAX_BEATS_HIST:]]
        with self._beat_times_lock:
            self.beat_times.clear()
            self.beat_times.extend(new_beats)
