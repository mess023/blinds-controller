#!/usr/bin/env python3
"""
Blinds Controller — thin entry point.
All logic lives in the blinds/ package.

Run:
  python blinds_controller.py
"""

import logging
import os

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

from blinds.app import BlindsApp

if __name__ == "__main__":
    app = BlindsApp()
    app.mainloop()
