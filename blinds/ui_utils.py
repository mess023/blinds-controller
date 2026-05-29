"""
Tiny shared UI helpers — no circular imports.
"""

import tkinter as tk
from tkinter import ttk  # noqa: F401 — re-exported for app.py convenience
from typing import Any   # noqa: F401 — re-exported for app.py convenience

from .constants import BTN, BTNHOV


def _hr(parent, top=8, bot=8):
    tk.Frame(parent, bg=BTN, height=1).pack(fill="x", padx=16,
                                             pady=(top, bot))

def _hov(btn):
    btn.bind("<Enter>", lambda e: btn.config(bg=BTNHOV))
    btn.bind("<Leave>", lambda e: btn.config(bg=BTN))
