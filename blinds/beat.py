"""
BeatClock — internal beat counter used when Ableton Link is absent.
"""

import time


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
