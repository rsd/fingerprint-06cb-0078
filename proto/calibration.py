# SPDX-License-Identifier: MIT
"""Alias of synaptics0078.calibration (the module moved into the shared package in B5a).

`import calibration` from proto/ or tools/ gets the package module itself, not a copy:
sys.modules["calibration"] is synaptics0078.calibration, so every import and every monkeypatch
of the old name acts on the one shared module.

tools/dev/b2_program.py runs as root and loads this file by path from its source text, with
no import loader (so no .pyc is ever read). In that case (no __spec__) synaptics0078/pins.py
and synaptics0078/calibration.py are always compiled from their sources the same way,
whatever sys.modules holds already, and their names are copied into this module's
namespace. They are not left in sys.modules (nothing imported later picks them up in place
of the real modules): each one is there only while its own source runs, because dataclasses
look a class's module up there, and whatever entry was there before is put back. Their one
relative import (`from . import pins`) is answered from the modules compiled here, never by
the import system, and sys.path is left alone.
"""

import builtins
import importlib
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_DEPS = ("pins", "calibration")  # synaptics0078.calibration and what it imports, in order
_PKG = "synaptics0078"
_ABSENT = object()


def _from_source(names):
    """synaptics0078.<names[-1]>, each module compiled from its .py file; no cache, no sys.modules."""
    pkg_dir = _ROOT / _PKG
    pkg = types.ModuleType(_PKG)  # private: the parent of the modules below, never registered
    pkg.__path__, pkg.__package__ = [str(pkg_dir)], _PKG
    done = {}
    real_import = builtins.__import__

    def source_import(name, scope=None, local_scope=None, fromlist=(), level=0):  # __import__'s arguments
        if level == 0 and name != _PKG and not name.startswith(_PKG + "."):
            return real_import(name, scope, local_scope, fromlist, level)  # the standard library
        if level == 1 and (scope or {}).get("__package__") == _PKG:
            if name in done:  # from .pins import x
                return done[name]
            missing = [n for n in fromlist if n not in done] if name == "" else [name]
            if not missing:  # from . import pins
                return pkg
        else:
            missing = [name]
        raise ImportError(f"{', '.join(missing)}: not one of the synaptics0078 modules compiled from source here")

    module_builtins = dict(vars(builtins), __import__=source_import)
    for name in names:
        path = pkg_dir / f"{name}.py"
        mod = types.ModuleType(f"{_PKG}.{name}")
        mod.__file__, mod.__package__, mod.__builtins__ = str(path), _PKG, module_builtins
        code = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        before = sys.modules.get(mod.__name__, _ABSENT)
        sys.modules[mod.__name__] = mod  # only while it runs: @dataclass looks the module up there
        try:
            exec(code, mod.__dict__)  # noqa: S102 - the project's own source, by explicit path
        finally:
            if before is _ABSENT:
                sys.modules.pop(mod.__name__, None)
            else:
                sys.modules[mod.__name__] = before
        setattr(pkg, name, mod)
        done[name] = mod
    return done[names[-1]]


if globals().get("__spec__") is None:  # loaded by path from source (tools/dev/b2_program.py)
    globals().update({k: v for k, v in vars(_from_source(_SOURCE_DEPS)).items() if not k.startswith("__")})
else:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    sys.modules[__name__] = importlib.import_module("synaptics0078.calibration")
