# SPDX-License-Identifier: MIT
"""The synthetic template for the flow tests, so they run the same on a clean clone and here.

SYNTH_TEMPLATE is the 18869-byte template of a fake DLL (real layout, random table bytes;
synaptics0078/tests/fixtures.py). setUpModule() makes calibration.load_template return it
for the tools under test (calibration is the one shared module, so capture, fingerlift and
calibrate all see it), and swaps the two program skeleton pins of calib_model for the ones
of the programs it gives, so the calibrate guard accepts them. A test module imports
setUpModule and tearDownModule from here: the patches must not leak into the test files
that exercise the real load_template and the real pins.
"""

import contextlib
from unittest import mock

import calibration  # puts the repo root on sys.path, so the package imports below work

from synaptics0078 import calib_model as cm
from synaptics0078.tests import fixtures as fx

SYNTH_TEMPLATE = fx.expected_template()
SYNTH_PINS = fx.synthetic_pins(SYNTH_TEMPLATE)

_stacks = []


def patches() -> contextlib.ExitStack:
    """The patches of setUpModule, applied; the caller closes the stack to undo them.

    For a test class of a module that also holds tests of the real load_template or the real pins,
    where a module-wide setUpModule would reach them:
    setUpClass takes this and registers its close with addClassCleanup.
    """
    with contextlib.ExitStack() as stack:  # a patch that fails undoes the ones before it
        stack.enter_context(mock.patch.object(calibration, "load_template", lambda path=None: SYNTH_TEMPLATE))
        sk1, sk2 = SYNTH_PINS.program1_skeleton_sha256, SYNTH_PINS.program2_skeleton_sha256
        stack.enter_context(mock.patch.object(cm, "PROGRAM1_SKELETON_SHA256", sk1))
        stack.enter_context(mock.patch.object(cm, "PROGRAM2_SKELETON_SHA256", sk2))
        stack.enter_context(mock.patch.dict(cm.SKELETON_SHA256, {cm.PROGRAM1_LEN: sk1, cm.PROGRAM2_LEN: sk2}))
        return stack.pop_all()


def setUpModule():
    _stacks.append(patches())


def tearDownModule():
    _stacks.pop().close()
