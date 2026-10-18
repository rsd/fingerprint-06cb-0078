# SPDX-License-Identifier: MIT
"""Shared code of the Synaptics 06cb:0078 project (the proto tools and synaptics0078-setup).

Modules:
  pins         every sha256 pin, length and known vendor archive (pure data)
  calibration  the 18869-byte identify program: template, per-unit regions, checks
  calib_model  the Windows calibration model: frames, check score, calibration programs
  parsers      reply parsers
  template     the template rebuilt from the vendor DLL plus code; the template-pin check
  cfb          a small OLE/CFB (MSI) reader
  vendor       the module and the template out of a vendor archive (cab, zip, exe, MSI, DLL)
  session      the guarded USB session and the calibration sequence (pyusb imported lazily)

Nothing here imports a third-party module at import time. proto/calibration.py,
proto/calib_model.py and proto/parsers.py are aliases of the package modules.
"""
