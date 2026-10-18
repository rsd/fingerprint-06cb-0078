# SPDX-License-Identifier: MIT
"""Alias of synaptics0078.calib_model (the module moved into the shared package in B5a).

`import calib_model` from proto/ or tools/ gets the package module itself, not a copy:
sys.modules["calib_model"] is synaptics0078.calib_model, so every import and every monkeypatch
of the old name acts on the one shared module.
"""

import importlib
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_m = importlib.import_module("synaptics0078.calib_model")

sys.modules[__name__] = _m
