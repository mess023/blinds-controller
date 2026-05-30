"""Blinds-controller package init.

The very first thing this module does is restore the NumPy 1.20-deprecated
type aliases (``np.float``, ``np.int``, …).  Madmom 0.16.1 (last release
2018) was compiled against pre-1.20 NumPy and references those aliases from
its compiled Cython extensions, so we have to make them exist BEFORE any
``import blinds.bpm_madmom`` (or any transitive import that pulls madmom).

Putting this here centralises a shim that was previously duplicated in
``blinds_controller.py``, ``bpm_madmom._install_numpy_aliases``, and the
top of ``MadmomBPM.__init__``.
"""

import numpy as _np

for _name, _t in (("float", float), ("int", int), ("bool", bool),
                   ("complex", complex), ("object", object),
                   ("long", int), ("str", str), ("unicode", str)):
    if not hasattr(_np, _name):
        setattr(_np, _name, _t)

del _np, _name, _t
