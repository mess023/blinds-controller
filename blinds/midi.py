"""
MIDI optional-import block.

Exports: MIDI_AVAILABLE, mido
"""

from typing import Any

mido: Any = None
MIDI_AVAILABLE = False
try:
    import mido as _mido_mod
    mido = _mido_mod
    MIDI_AVAILABLE = True
except ImportError:
    pass
