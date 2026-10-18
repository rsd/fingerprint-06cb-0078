"""Offline tests of tools/dev/a5_session.py and the a5-enroll.sh / a5-verify.sh wrappers.

No fprintd, no D-Bus, no USB: every fprintd client, journalctl and systemctl is a fake script, and every
journal line is synthetic (the formats of the driver log-line contract), with a made-up serial.
"""

import hashlib
import io
import os
import re
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "dev"))

import a5_session as a5

SESSION_PY = HERE / "dev" / "a5_session.py"
ENROLL_SH = HERE / "dev" / "a5-enroll.sh"
VERIFY_SH = HERE / "dev" / "a5-verify.sh"
LIB_SH = HERE / "dev" / "a5-session-lib.sh"
FORK_DRIVER = HERE.parent.parent / "libfprint" / "libfprint" / "drivers" / "synaptics0078.c"

# Journal lines (bare messages, as `journalctl -o cat` shows them).
ACT = "Activation: capture module already loaded (RomInfo byte 17 = 0x1e)"
L6 = "Image captured (diff vs last capture -)"
L6D = "Image captured (diff vs last capture 54.2)"
L1 = "Finger off after 1 probe(s), 512 ms: no finger event"
L2 = "Finger off after 3 probe(s), 1800 ms: new touch (diff 41.0 >= 30)"
L3 = "Same touch as the last capture (diff 4.2 < 30), not submitted; waiting for finger off"
E2 = "Finger-off probe 4: finger still down"
L4 = "Finger-off probe 5: same touch (diff 3.1 < 30)"
L5 = "Finger still down for 5 s, probing every 1000 ms from now on"
E3 = "Capture failed: event code 3 (failed/aborted), asking for a retry"
HOT = "Device reported an error during enroll: Device disabled to prevent overheating."
# The driver's guard-wait lines (synaptics0078.c), log-only for the prompts.
GUARD_NEW = "Finger-off probe 3: new touch (diff 41.0 >= 30), taking this image as the capture"
GUARD_LIFT = "Lift seen during the guard wait, accepting the next touch without the same-touch check"
SKIP_LIFT = "Same-touch check skipped: the lift was seen during the guard wait"
SKIP_STALE = "Same-touch check skipped: the reference image is stale (a retry since)"
PROBE_STALE = "Finger-off probe 2: reference image is stale (a retry since), image not compared"
SERIAL = "Serial 0123456789ab"  # fake
NOISE = ["Sending command 0x02 (1 bytes)", "EP 0x83 event 0200401000: finger (+97 ms after 02)", SERIAL]


def ER(name):
    return f"Enroll result: enroll-{name}"


def VR(name, done=True):
    return f"Verify result: verify-{name} ({'done' if done else 'not done'})"


FIRST = a5.EnrollMachine().touch_prompt()  # the duplicate-check touch


def touch(n, total=a5.ENROLL_STAGES, finger="RIGHT INDEX finger"):
    return f"Touch {n} of {total} with your {finger} (vary the position a little)"


SKIP_NOTE = "(fprintd skipped its check: the first touch counted as touch 1 of 18)"


def drive(machine, events):
    """events: (time, 'j'|'c'|'t', line). Returns every prompt line, in order."""
    out = []
    for now, src, line in events:
        if src == "j":
            out += machine.feed_journal(line, now)
        elif src == "c":
            out += machine.feed_client(line, now)
        if hasattr(machine, "tick"):
            out += machine.tick(now)
    return out


def check_events(t=0.0):
    """The duplicate-check identify: armed, touched, lifted (nothing on stdout)."""
    return [(t, "j", ACT), (t + 1, "j", L6), (t + 2, "j", L1)]


def check_prompts():
    return [FIRST, a5.MSG_LIFT, a5.MSG_LIFTED, touch(1)]


# ------------------------------------------------------------------ helpers


def test_enroll_stages_match_the_driver():
    if not FORK_DRIVER.is_file():
        pytest.skip(f"fork source {FORK_DRIVER} not readable")
    m = re.search(r"#define SYNAPTICS0078_ENROLL_STAGES (\d+)", FORK_DRIVER.read_text())
    assert m and int(m.group(1)) == a5.ENROLL_STAGES


@pytest.mark.parametrize(
    "line, kind",
    [
        (L6, "taken"),
        (L6D, "taken"),
        ("Image captured", "taken"),  # the driver before A5b
        (L1, "off"),
        (L2, "off"),
        ("Finger off after 32 probe(s), 16400 ms", "off"),
        (L3, "stale"),
        (E2, "still"),
        (L4, "still"),
        (L5, "still"),
        (E3, "failed"),
        ("Capture failed: the image reply carries an error, asking for a retry", "failed"),
        (ACT, "activation"),
        ("sigfm score 87/105", "score"),
        (HOT, "overheat"),
        ("Device reported an error during identify for enroll: Device disabled to prevent overheating.", "overheat"),
        ("Finger-off probe 1: arming", "other"),
        (GUARD_NEW, "other"),
        (GUARD_LIFT, "other"),
        (SKIP_LIFT, "other"),
        (SKIP_STALE, "other"),
        (PROBE_STALE, "other"),
        (SERIAL, "other"),
        ("", "other"),
    ],
)
def test_journal_kind(line, kind):
    assert a5.journal_kind(line) == kind


def test_finger_names_and_display():
    assert a5.parse_finger_used("right-middle") == "right-middle-finger"
    assert a5.parse_finger_used("RIGHT-INDEX-FINGER") == "right-index-finger"
    assert a5.parse_finger_used("left-thumb") == "left-thumb"
    assert a5.parse_finger_used("right-pinky") is None
    assert a5.finger_display("right-middle-finger") == "RIGHT MIDDLE finger"
    assert a5.finger_display("left-thumb") == "LEFT THUMB"
    assert a5.finger_label("right-index-finger") == "right-index"
    assert len(a5.FINGERS) == 10


# ------------------------------------------------------------------ enroll


def full_enroll_events(result_first_every=2):
    """Check touch, then 18 stages; every `result_first_every`-th stage result arrives before its L6."""
    ev = check_events()
    t = 10.0
    for stage in range(1, a5.ENROLL_STAGES + 1):
        result = ER("completed") if stage == a5.ENROLL_STAGES else ER("stage-passed")
        pair = [(t, "j", L6D), (t + 0.2, "c", result)]
        if stage % result_first_every == 0:
            pair = [(t, "c", result), (t + 0.2, "j", L6D)]
        ev += pair
        ev += [(t + 0.3, "j", n) for n in NOISE]
        if stage < a5.ENROLL_STAGES:
            ev.append((t + 1.0, "j", L1 if stage % 3 else L2))
        t += 3.0
    return ev


def test_enroll_full_sequence_exact():
    m = a5.EnrollMachine()
    out = m.start(0.0) + drive(m, full_enroll_events())
    want = check_prompts()
    for stage in range(1, a5.ENROLL_STAGES):
        want += [a5.MSG_LIFT, a5.MSG_LIFTED, touch(stage + 1)]
    want.append(a5.MSG_LIFT)  # the last touch: enroll-completed
    assert out == want
    assert m.check_done is True and m.stages == a5.ENROLL_STAGES and m.retries == 0
    lines, code = m.finish(0)
    assert code == 0
    done = "Done: your RIGHT INDEX finger is enrolled as right-index-finger (18 of 18 touches). "
    nxt = "Next: tools/dev/a5-verify.sh --finger-used right-index --expect match --finger right-index-finger"
    assert lines == [done + nxt]


def test_enroll_left_index_every_touch_prompt_names_it():
    m = a5.EnrollMachine(finger="left-index-finger")
    out = m.start(0.0) + drive(m, full_enroll_events())
    assert out[0] == (
        "First touch with your LEFT INDEX finger. fprintd first checks that this finger is not enrolled yet "
        "(this touch is not one of the 18)"
    )
    prompts = [line for line in out if line.startswith("Touch ")]
    assert prompts == [touch(n, finger="LEFT INDEX finger") for n in range(1, a5.ENROLL_STAGES + 1)]
    assert prompts[2] == "Touch 3 of 18 with your LEFT INDEX finger (vary the position a little)"
    assert "RIGHT" not in " ".join(out)
    (line,), code = m.finish(0)
    assert code == 0
    assert line.startswith("Done: your LEFT INDEX finger is enrolled as left-index-finger (18 of 18 touches).")
    # The suggested command checks the finger just enrolled, touching with that same finger.
    assert line.endswith(
        "Next: tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger"
    )


@pytest.mark.parametrize("finger", a5.FINGERS)
def test_enroll_next_step_is_a_valid_verify_command(finger):
    m = a5.EnrollMachine(finger=finger)
    m.outcome = "enroll-completed"
    nxt = m.finish(0)[0][0].split("Next: ", 1)[1].split()
    assert nxt[:2] == ["tools/dev/a5-verify.sh", "--finger-used"]
    assert a5.parse_finger_used(nxt[2]) == finger
    assert nxt[3:] == ["--expect", "match", "--finger", finger]


def test_enroll_no_prompt_before_the_sensor_is_armed_then_wait_note_once():
    m = a5.EnrollMachine()
    assert m.start(100.0) == []
    assert drive(m, [(110.0, "c", "Using device /net/reactivated/Fprint/Device/0"), (120.0, "t", "")]) == []
    assert drive(m, [(131.0, "t", ""), (140.0, "t", "")]) == [a5.MSG_WAIT_SENSOR]
    assert drive(m, [(141.0, "j", ACT)]) == [FIRST]


def test_enroll_touch_without_activation_line_still_prompts_once():
    m = a5.EnrollMachine()
    assert drive(m, [(1.0, "j", L6), (1.1, "j", L6), (2.0, "j", L1)]) == [FIRST, a5.MSG_LIFT, a5.MSG_LIFTED, touch(1)]


def test_enroll_finger_off_before_the_result_waits_for_the_count():
    m = a5.EnrollMachine()
    drive(m, check_events())
    out = drive(m, [(10.0, "j", L6), (10.6, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED]  # no "Touch N" yet: its count is not known
    assert drive(m, [(10.9, "c", ER("stage-passed"))]) == [touch(2)]


def test_enroll_result_never_comes_after_lift_times_out_with_the_known_count():
    m = a5.EnrollMachine()
    drive(m, check_events())
    assert drive(m, [(10.0, "j", L6), (10.5, "j", L1), (12.0, "t", "")]) == [a5.MSG_LIFT, a5.MSG_LIFTED]
    assert drive(m, [(12.6, "t", "")]) == [touch(1)]


def test_enroll_without_duplicate_check_counts_the_first_touch():
    m = a5.EnrollMachine()
    out = drive(m, [(0, "j", ACT), (1, "j", L6), (1.2, "c", ER("stage-passed")), (2, "j", L1)])
    assert out == [FIRST, a5.MSG_LIFT, SKIP_NOTE, a5.MSG_LIFTED, touch(2)]
    assert m.check_done is False and m.stages == 1


def test_enroll_without_duplicate_check_result_before_l6():
    m = a5.EnrollMachine()
    out = drive(m, [(0, "j", ACT), (1, "c", ER("stage-passed")), (1.1, "j", L6), (2, "j", L1)])
    assert out == [
        FIRST,
        a5.MSG_LIFT,
        SKIP_NOTE,
        a5.MSG_LIFTED,
        touch(2),
    ]
    # The next touch pairs normally: L6 -> LIFT, result, off -> touch 3.
    out = drive(m, [(5, "j", L6), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(3)]


def test_enroll_without_duplicate_check_late_result_corrects_the_prompt():
    m = a5.EnrollMachine()
    out = drive(m, check_events())  # looks like the check: touch 1 is announced
    assert out[-1] == touch(1)
    # ...but it was stage 1, its result late. Doubtful until the journal shows no enroll Activation.
    assert drive(m, [(2.6, "c", ER("stage-passed")), (4.0, "t", "")]) == []
    assert drive(m, [(4.2, "t", "")]) == [SKIP_NOTE, touch(2)]
    assert m.check_done is False and m.stages == 1
    out = drive(m, [(5, "j", L6), (5.3, "c", ER("stage-passed")), (6, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(3)]


def test_enroll_without_duplicate_check_late_result_then_next_l6_decides():
    m = a5.EnrollMachine()
    drive(m, check_events())
    assert drive(m, [(2.6, "c", ER("stage-passed"))]) == []
    # The user touches again before the doubt times out: an L6 without an Activation, so no check.
    assert drive(m, [(3.0, "j", L6D)]) == [SKIP_NOTE, a5.MSG_LIFT]
    assert drive(m, [(3.2, "c", ER("stage-passed")), (4.0, "j", L1)]) == [a5.MSG_LIFTED, touch(3)]
    assert m.check_done is False and m.stages == 2 and m.touches == m.results == 1


def test_enroll_without_duplicate_check_late_result_then_next_result_decides():
    m = a5.EnrollMachine()
    drive(m, check_events())
    assert drive(m, [(2.6, "c", ER("stage-passed"))]) == []
    # The next touch's result beats its L6: a second result without an Activation, so no check.
    assert drive(m, [(3.0, "c", ER("stage-passed"))]) == [SKIP_NOTE, a5.MSG_LIFT]
    assert drive(m, [(3.1, "j", L6D), (4.0, "j", L1)]) == [a5.MSG_LIFTED, touch(3)]
    assert m.check_done is False and m.stages == 2


def test_enroll_quick_first_touch_after_a_real_check_journal_lags_behind_the_result():
    """Late review, should-fix 1: a real check, then a quick enroll touch 1 whose result arrives before the
    enroll's Activation and L6 (the journal lags). No skipped-check note, no "Touch 2" while the finger is
    down: the Activation proves the check, and the result gets its LIFT."""
    m = a5.EnrollMachine()
    assert drive(m, check_events()) == check_prompts()
    assert drive(m, [(2.6, "c", ER("stage-passed"))]) == []
    assert drive(m, [(3.4, "j", ACT)]) == [a5.MSG_LIFT]
    assert drive(m, [(3.5, "j", L6D), (4.5, "t", "")]) == []  # the doubt is settled: no timeout note
    assert drive(m, [(5.0, "j", L1)]) == [a5.MSG_LIFTED, touch(2)]
    assert m.check_done is True and m.stages == 1 and m.touches == m.results == 1
    out = drive(m, [(7, "j", L6D), (7.2, "c", ER("stage-passed")), (8, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(3)]


def test_enroll_quick_first_touch_after_a_real_check_result_before_its_l6():
    m = a5.EnrollMachine()
    assert drive(m, check_events()) == check_prompts()
    # The enroll re-activates the sensor; touch 1's result beats its L6 within the check window.
    out = drive(m, [(2.2, "j", ACT), (2.6, "c", ER("stage-passed"))])
    assert out == [a5.MSG_LIFT]  # not the skipped-check note, not "Touch 2" while the finger is down
    assert drive(m, [(2.8, "j", L6D), (3.5, "j", L1)]) == [a5.MSG_LIFTED, touch(2)]
    assert m.check_done is True and m.stages == 1
    out = drive(m, [(5, "j", L6D), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(3)]


def test_enroll_late_result_after_the_forced_settle_shows_no_lift():
    m = a5.EnrollMachine()
    drive(m, check_events())
    drive(m, [(5, "j", L6), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    assert drive(m, [(10.0, "j", L6), (10.5, "j", L1), (12.6, "t", "")]) == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(2)]
    # Its result after all: the count moves on, no LIFT while no finger is down.
    assert drive(m, [(13.0, "c", ER("stage-passed"))]) == [touch(3)]
    assert m.stages == 2
    out = drive(m, [(15, "j", L6), (15.2, "c", ER("stage-passed")), (16, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(4)]


def test_enroll_late_retry_after_the_forced_settle_is_log_only():
    m = a5.EnrollMachine()
    drive(m, check_events())
    drive(m, [(10.0, "j", L6), (10.5, "j", L1), (12.6, "t", "")])
    assert drive(m, [(13.0, "c", ER("retry-scan"))]) == []
    assert m.retries == 1 and m.stages == 0
    out = drive(m, [(15, "j", L6), (15.2, "c", ER("stage-passed")), (16, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(2)]


@pytest.mark.parametrize("kind", sorted(a5.RETRY_TEXT))
def test_enroll_retry_kinds_repeat_the_same_touch(kind):
    m = a5.EnrollMachine()
    drive(m, check_events())
    drive(m, [(5, "j", L6), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    out = drive(m, [(10, "j", L6), (10.2, "c", ER(kind)), (11, "j", L1)])
    assert out == [a5.MSG_LIFT, a5.retry_message(kind), a5.MSG_LIFTED, touch(2)]
    assert m.retries == 1 and m.stages == 1


def test_enroll_failed_capture_then_retry_result():
    m = a5.EnrollMachine()
    drive(m, check_events())
    out = drive(m, [(10, "j", E3), (10.1, "c", ER("retry-scan")), (11, "j", L1)])
    assert out == [
        a5.MSG_LIFT_FAILED,
        "That touch did not count: the sensor could not read it",
        a5.MSG_LIFTED,
        touch(1),
    ]


def test_enroll_failed_check_touch_repeats_the_check():
    m = a5.EnrollMachine()
    out = drive(m, [(0, "j", ACT), (1, "j", E3), (2, "j", L1)])
    assert out == [FIRST, a5.MSG_LIFT_FAILED, a5.MSG_LIFTED, FIRST]
    assert m.phase == "check"
    assert drive(m, [(3, "j", L6), (4, "j", L1)]) == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(1)]


def test_enroll_check_cut_short_without_finger_off_restarts_at_next_activation():
    m = a5.EnrollMachine()
    out = drive(m, [(0, "j", ACT), (1, "j", E3), (1.1, "c", ER("retry-scan")), (1.2, "j", ACT)])
    assert out == [FIRST, a5.MSG_LIFT_FAILED, a5.retry_message("retry-scan"), a5.MSG_AGAIN, FIRST]


def test_enroll_l3_guard_once_then_lift_and_same_prompt():
    m = a5.EnrollMachine()
    drive(m, check_events())
    drive(m, [(5, "j", L6), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    out = drive(m, [(8, "j", L3), (8.5, "j", L4), (9, "j", L3), (10, "j", L1)])
    assert out == [a5.MSG_STALE, a5.MSG_LIFTED, touch(2)]
    assert drive(m, [(12, "j", L6), (12.2, "c", ER("stage-passed")), (13, "j", L2)]) == [
        a5.MSG_LIFT,
        a5.MSG_LIFTED,
        touch(3),
    ]


def test_enroll_still_down_note_after_5_s_once_per_touch():
    m = a5.EnrollMachine()
    drive(m, check_events())
    out = drive(m, [(10, "j", L6), (10.2, "c", ER("stage-passed")), (12, "j", E2), (15.0, "j", E2)])
    assert out == [a5.MSG_LIFT]  # 5.0 s is not more than 5 s
    assert drive(m, [(15.2, "j", L4)]) == [a5.MSG_STILL]
    assert drive(m, [(16, "j", E2), (17, "j", L5)]) == []
    assert drive(m, [(20, "j", L1)]) == [a5.MSG_LIFTED, touch(2)]
    assert drive(m, [(25, "j", L6), (25.1, "c", ER("stage-passed")), (31, "j", L5)]) == [a5.MSG_LIFT, a5.MSG_STILL]


def test_enroll_guard_wait_new_touch_is_the_capture():
    m = a5.EnrollMachine()
    drive(m, check_events())
    drive(m, [(5, "j", L6), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    # L3, then the guard's probe sees a new touch and takes it: L6 without any finger-off line.
    out = drive(
        m, [(8, "j", L3), (8.5, "j", E2), (9, "j", GUARD_NEW), (9.01, "j", L6D), (9.2, "c", ER("stage-passed"))]
    )
    assert out == [a5.MSG_STALE, a5.MSG_LIFT]
    assert drive(m, [(10, "j", L1)]) == [a5.MSG_LIFTED, touch(3)]
    assert m.stages == 2 and m.touches == m.results == 2


def test_enroll_guard_wait_lift_then_unchecked_touch():
    m = a5.EnrollMachine()
    drive(m, check_events())
    drive(m, [(5, "j", L6), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    out = drive(m, [(8, "j", L3), (9, "j", L1), (9, "j", GUARD_LIFT)])
    assert out == [a5.MSG_STALE, a5.MSG_LIFTED, touch(2)]
    out = drive(m, [(11, "j", SKIP_LIFT), (11, "j", L6D), (11.2, "c", ER("stage-passed")), (12, "j", L2)])
    assert out == [a5.MSG_LIFT, a5.MSG_LIFTED, touch(3)]


def test_enroll_still_down_note_after_l3_once():
    m = a5.EnrollMachine()
    drive(m, check_events())
    drive(m, [(5, "j", L6), (5.2, "c", ER("stage-passed")), (6, "j", L1)])
    assert drive(m, [(8, "j", L3), (10, "j", E2), (13.0, "j", L4)]) == [a5.MSG_STALE]
    assert drive(m, [(13.1, "j", E2)]) == [a5.MSG_STILL]
    assert drive(m, [(14, "j", L3), (15, "j", L5), (20, "j", E2)]) == []
    assert drive(m, [(21, "j", L1)]) == [a5.MSG_LIFTED, touch(2)]


def test_enroll_still_down_evidence_without_a_touch_is_ignored():
    m = a5.EnrollMachine()
    drive(m, check_events())
    assert drive(m, [(30, "j", E2), (40, "j", L4)]) == []


@pytest.mark.parametrize("order", ["journal first", "client first"])
def test_enroll_overheat(order):
    m = a5.EnrollMachine()
    drive(m, check_events())
    pair = [(10, "j", HOT), (10.1, "c", ER("unknown-error"))]
    if order == "client first":
        pair.reverse()
    assert drive(m, pair) == []
    lines, code = m.finish(1)
    assert code == 1
    assert lines == [
        (
            "Your RIGHT INDEX finger is NOT enrolled. The sensor's overheat protection paused it; "
            "let it rest about 5 minutes, then: tools/dev/a5-enroll.sh right-index-finger"
        )
    ]


@pytest.mark.parametrize("name", sorted(a5.ENROLL_FINAL))
def test_enroll_final_results(name):
    m = a5.EnrollMachine()
    drive(m, check_events())
    assert drive(m, [(10, "c", f"Enroll result: {name}"), (11, "j", L6), (12, "j", L1)]) == []
    lines, code = m.finish(1)
    want = "Your RIGHT INDEX finger is NOT enrolled. " + a5.ENROLL_FINAL[name].format(again=m.again())
    assert (lines, code) == ([want], 1)
    assert lines[0].endswith(": tools/dev/a5-enroll.sh right-index-finger")


def test_enroll_unknown_lines_and_results_are_log_only():
    m = a5.EnrollMachine()
    drive(m, check_events())
    out = drive(
        m,
        [
            (5, "c", "Enroll result: enroll-something-new"),
            (5, "c", "Enrolling right-index-finger finger."),
            (5, "j", "Sensor already armed, listening on EP 0x83"),
            *[(5, "j", n) for n in NOISE],
        ],
    )
    assert out == []
    assert m.unknown_results == ["enroll-something-new"]


def test_enroll_client_error_without_result():
    m = a5.EnrollMachine()
    m.start(0)
    drive(
        m,
        [
            (1, "c", "Using device /net/reactivated/Fprint/Device/0"),
            (2, "c", "EnrollStart failed: GDBus.Error:net.reactivated.Fprint.Error.PermissionDenied: Not Authorized"),
        ],
    )
    lines, code = m.finish(1)
    assert code == 1
    assert lines[0].startswith("Your RIGHT INDEX finger is NOT enrolled. fprintd-enroll stopped before finishing: ")
    assert a5.EnrollMachine(finger="left-index-finger").finish(1) == (
        [
            (
                "Your LEFT INDEX finger is NOT enrolled. fprintd-enroll stopped without a result (exit 1). "
                "See the log, then: tools/dev/a5-enroll.sh left-index-finger"
            )
        ],
        1,
    )


# ------------------------------------------------------------------ verify


def test_verify_rounds_tally_scores_and_prompts():
    m = a5.VerifyMachine(finger="right-index-finger", used="right-middle-finger", expect="no-match", rounds=3)
    out = m.start_round(1)
    out += drive(m, [(1, "j", L6), (1.1, "j", "sigfm score 12/105"), (1.2, "c", VR("no-match"))])
    out += m.end_round(0)
    out += m.start_round(2)
    out += drive(
        m,
        [
            (5, "j", E3),
            (5.1, "c", VR("retry-scan", done=False)),
            (6, "j", L6),
            (6.1, "j", "sigfm score 130/105"),
            (6.2, "c", VR("match")),
            (6.9, "j", L1),
        ],
    )
    assert m.settled()
    out += m.end_round(0)
    out += m.start_round(3)
    out += drive(
        m,
        [
            (9, "j", L3),
            (9.5, "j", L3),
            (10, "j", L1),
            (11, "j", L6),
            (11.1, "j", "sigfm score 3/105"),
            (11.2, "j", "sigfm score 5/105"),
            (11.3, "c", VR("no-match")),
        ],
    )
    assert not m.settled()  # LIFT shown, no finger-off yet: the runner waits a little
    out += m.end_round(0)
    p = "touch with your RIGHT MIDDLE finger, then lift"
    assert out == [
        f"Round 1/3: {p}",
        a5.MSG_LIFT,
        "Round 1/3: no match (as expected)",
        f"Round 2/3: {p}",
        a5.MSG_LIFT_FAILED,
        (
            "That touch did not count: the sensor could not read it. Lift your finger, then touch again with your "
            "RIGHT MIDDLE finger"
        ),
        a5.MSG_LIFT,
        "Round 2/3: match (NOT as expected)",
        f"Round 3/3: {p}",
        a5.MSG_STALE,
        a5.MSG_LIFTED,
        f"Round 3/3: {p}",
        a5.MSG_LIFT,
        "Round 3/3: no match (as expected)",
    ]
    lines, code = m.summary()
    assert code == 1
    assert lines == [
        "Summary: RIGHT MIDDLE finger against the enrolled right-index-finger, expected no match",
        "  rounds     3/3",
        "  match      1",
        "  no match   2",
        "  retries    1",
        "  errors     0",
        "as expected: 2/3",
    ]
    assert m.score_log() == [
        "round 1: no-match (verify-no-match), retries 0, sigfm scores 12/105",
        "round 2: match (verify-match), retries 1, sigfm scores 130/105",
        "round 3: no-match (verify-no-match), retries 0, sigfm scores 3/105 5/105",
    ]


@pytest.mark.parametrize("kind", sorted(a5.RETRY_TEXT))
def test_verify_every_retry_kind_is_tallied(kind):
    m = a5.VerifyMachine(rounds=1)
    m.start_round(1)
    out = drive(m, [(1, "j", L6), (1.1, "c", VR(kind, done=False)), (3, "j", L6), (3.1, "c", VR("match"))])
    assert out == [
        a5.MSG_LIFT,
        a5.verify_retry_message(kind),
        a5.MSG_LIFT,
        "Round 1/1: match (as expected)",
    ]
    m.end_round(0)
    assert m.counts()["retries"] == 1
    assert m.summary() == (m.summary()[0], 0)


def test_verify_result_before_l6_shows_lift_once():
    m = a5.VerifyMachine(rounds=1)
    m.start_round(1)
    assert drive(m, [(1, "c", VR("match")), (1.1, "j", L6)]) == [a5.MSG_LIFT, "Round 1/1: match (as expected)"]


def test_verify_still_down_note():
    m = a5.VerifyMachine(rounds=1)
    m.start_round(1)
    assert drive(m, [(1, "j", L6), (3, "j", E2), (6.5, "j", E2), (7, "j", L4)]) == [a5.MSG_LIFT, a5.MSG_STILL]


def test_verify_still_down_note_after_l3_once():
    m = a5.VerifyMachine(rounds=1)
    m.start_round(1)
    assert drive(m, [(1, "j", L3), (3, "j", E2), (6.5, "j", L4), (7, "j", E2), (8, "j", L5)]) == [
        a5.MSG_STALE,
        a5.MSG_STILL,
    ]


def test_verify_guard_wait_new_touch_is_the_capture():
    m = a5.VerifyMachine(rounds=1)
    m.start_round(1)
    out = drive(
        m,
        [
            (1, "j", L3),
            (1.5, "j", E2),
            (2, "j", GUARD_NEW),
            (2.01, "j", L6D),
            (2.1, "j", "sigfm score 120/105"),
            (2.2, "c", VR("match")),
        ],
    )
    assert out == [a5.MSG_STALE, a5.MSG_LIFT, "Round 1/1: match (as expected)"]
    assert not m.settled()
    drive(m, [(3, "j", L1)])
    assert m.settled()
    assert m.end_round(0) == [] and m.counts()["match"] == 1


def test_verify_capture_failed_shows_lift_at_once_in_either_order():
    m = a5.VerifyMachine(rounds=1)
    m.start_round(1)
    retry = a5.verify_retry_message("retry-scan")
    assert drive(m, [(1, "j", E3)]) == [a5.MSG_LIFT_FAILED]
    assert drive(m, [(1.1, "c", VR("retry-scan", done=False))]) == [retry]
    # The retry result first: its late E3 adds nothing, and the next touch still gets its LIFT.
    assert drive(m, [(3, "c", VR("retry-scan", done=False)), (3.1, "j", E3)]) == [retry]
    assert drive(m, [(5, "j", L6), (5.1, "c", VR("match"))]) == [a5.MSG_LIFT, "Round 1/1: match (as expected)"]
    m.end_round(0)
    assert m.counts()["retries"] == 2


def test_verify_remove_and_retry_is_one_sentence():
    text = a5.verify_retry_message("remove-and-retry", "left-index-finger")
    assert text == "That touch did not count: lift your finger completely, then touch again with your LEFT INDEX finger"
    assert text.lower().count("lift") == 1


@pytest.mark.parametrize("kind", sorted(a5.RETRY_TEXT))
def test_verify_retry_names_the_finger_to_touch_with(kind):
    m = a5.VerifyMachine(finger="left-index-finger", used="left-index-finger", rounds=2)
    assert m.start_round(1) == ["Round 1/2: touch with your LEFT INDEX finger, then lift"]
    (line,) = drive(m, [(1, "c", VR(kind, done=False))])
    assert line.endswith("touch again with your LEFT INDEX finger")


def test_verify_overheat_keeps_a_result_already_given():
    m = a5.VerifyMachine(rounds=3, expect="match")
    m.start_round(1)
    hot = "Device reported an error during verify: Device disabled to prevent overheating."
    drive(m, [(1, "j", L6), (1.1, "c", VR("match")), (1.2, "j", hot)])
    assert m.end_round(0) == []
    assert m.abort and "overheat" in m.abort
    assert m.done[0].outcome == "match" and m.done[0].detail == "verify-match"
    lines, code = m.summary()
    assert code == 1 and "  match      1" in lines and "  errors     0" in lines


def test_verify_errors_and_abort_on_client_failure():
    m = a5.VerifyMachine(rounds=3)
    m.start_round(1)
    assert drive(m, [(1, "c", VR("disconnected"))]) == ["Round 1/3: error (the sensor stopped answering)"]
    assert m.end_round(0) == [] and m.abort is None
    m.start_round(2)
    drive(m, [(2, "c", "Finger 'right-index-finger' not enrolled for user someone.")])
    assert m.end_round(1) == ["Round 2/3: error (Finger 'right-index-finger' not enrolled for user someone.)"]
    assert m.abort.startswith("fprintd-verify failed: Finger")
    lines, code = m.summary()
    assert code == 1
    assert (
        lines[0]
        == "Stopped after round 2: fprintd-verify failed: Finger 'right-index-finger' not enrolled for user someone."
    )
    assert "  errors     2" in lines and lines[-1] == "as expected: 0/3"
    m2 = a5.VerifyMachine(rounds=1)
    m2.start_round(1)
    assert m2.end_round(1) == ["Round 1/1: error (no result (exit 1))"] and m2.abort is None


def test_verify_overheat_stops_the_rounds():
    m = a5.VerifyMachine(rounds=10, expect="match")
    m.start_round(1)
    drive(m, [(1, "j", L6), (1.1, "c", VR("match"))])
    m.end_round(0)
    m.start_round(2)
    hot = "Device reported an error during verify: Device disabled to prevent overheating."
    drive(m, [(2, "c", VR("unknown-error")), (2.1, "j", hot)])
    m.end_round(1)
    assert m.abort and "overheat" in m.abort
    lines, code = m.summary()
    assert code == 1
    assert lines[0].startswith("Stopped after round 2: the sensor's overheat protection paused it")
    assert lines[-1] == "as expected: 1/10"
    m3 = a5.VerifyMachine(rounds=2)
    m3.start_round(1)
    drive(m3, [(1, "j", HOT)])
    assert m3.end_round(1) == ["Round 1/2: error (overheat protection)"]


def test_verify_unknown_lines_are_log_only():
    m = a5.VerifyMachine(rounds=1)
    m.start_round(1)
    out = drive(
        m,
        [
            (1, "c", "Using device /x"),
            (1, "c", "Listing enrolled fingers:"),
            (1, "c", " - #0: right-index-finger"),
            (1, "c", "Verify started!"),
            (1, "c", "Verifying: right-index-finger"),
            (1, "c", VR("brand-new-thing", done=False)),
            *[(1, "j", n) for n in NOISE],
        ],
    )
    assert out == []


# ------------------------------------------------------------------ runner with fake commands


def fake(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


def fake_journal(tmp: Path, lines: list[str], delay: float = 0.05) -> str:
    """journalctl: with -f, print `lines` slowly and then follow forever; else print one line."""
    return fake(
        tmp / "journalctl",
        f"""
        import sys, time
        if "-f" not in sys.argv:
            print("Started Fingerprint Authentication Daemon.")
            sys.exit(0)
        for line in {lines!r}:
            time.sleep({delay})
            print(line, flush=True)
        time.sleep(60)
        """,
    )


ENROLL_JOURNAL = [ACT, SERIAL, L6, L1] + [x for _ in range(a5.ENROLL_STAGES) for x in (L6D, *NOISE, L1)]
ENROLL_STDOUT = (
    ["Using device /net/reactivated/Fprint/Device/0", "Enrolling right-index-finger finger."]
    + [ER("stage-passed")] * (a5.ENROLL_STAGES - 1)
    + [ER("completed")]
)


def allowed_prompt(line: str) -> bool:
    fixed = {
        a5.MSG_LIFT,
        a5.MSG_LIFT_FAILED,
        a5.MSG_LIFTED,
        a5.MSG_AGAIN,
        a5.MSG_STALE,
        a5.MSG_STILL,
        FIRST,
        a5.MSG_WAIT_SENSOR,
    }
    return (
        line in fixed
        or re.fullmatch(r"Touch \d+ of 18 with your [A-Z]+ [A-Z]+( finger)? \(vary the position a little\)", line)
        is not None
        or line.startswith(
            (
                "(fprintd skipped its check",
                "Done: ",
                "Round ",
                "That touch did not count",
                "Summary",
                "  ",
                "as expected: ",
            )
        )
    )


def test_run_enroll_terminal_gets_only_prompts_and_log_gets_everything(tmp_path):
    client = fake(
        tmp_path / "fprintd-enroll",
        f"""
        import sys, time
        assert sys.argv[1:] == ["-f", "right-index-finger"], sys.argv
        for line in {ENROLL_STDOUT!r}:
            time.sleep(0.04)
            print(line, flush=True)
        print("some stderr noise", file=sys.stderr, flush=True)
        """,
    )
    journal = fake_journal(tmp_path, ENROLL_JOURNAL, delay=0.01)
    term = io.StringIO()
    log_path = tmp_path / "a5.log"
    with a5.open_log(str(log_path)) as log:
        code = a5.run_enroll("right-index-finger", client, journal, a5.Output(term, log), settle_s=0.2)
    shown = term.getvalue().splitlines()
    assert code == 0
    assert shown[-1].startswith("Done: your RIGHT INDEX finger is enrolled as right-index-finger (18 of 18 touches).")
    bad = [line for line in shown if not allowed_prompt(line)]
    assert bad == []
    assert SERIAL not in term.getvalue() and "Using device" not in term.getvalue()
    text = log_path.read_text()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    for raw in (
        "[enroll] Using device",
        "[enroll] some stderr noise",
        f"[journal] {SERIAL}",
        "[journal] " + L6D,
        "[prompt] Done: ",
    ):
        assert raw in text
    assert re.search(r"^\d\d:\d\d:\d\d\.\d{3} \[enroll\] Enroll result: enroll-completed$", text, re.MULTILINE)


def test_run_verify_two_rounds_with_fakes(tmp_path):
    counter = tmp_path / "count"
    client = fake(
        tmp_path / "fprintd-verify",
        f"""
        import pathlib, sys, time
        assert sys.argv[1:] == ["-f", "right-index-finger"], sys.argv
        c = pathlib.Path({str(counter)!r})
        n = int(c.read_text()) + 1 if c.exists() else 1
        c.write_text(str(n))
        print("Using device /net/reactivated/Fprint/Device/0", flush=True)
        print("Verify started!", flush=True)
        time.sleep(0.1)
        if n == 1:
            print("Verify result: verify-retry-scan (not done)", flush=True)
            time.sleep(0.1)
        print("Verify result: verify-no-match (done)", flush=True)
        """,
    )
    journal = fake_journal(tmp_path, [L6, "sigfm score 12/105", L1, SERIAL], delay=0.02)
    term = io.StringIO()
    log_path = tmp_path / "v.log"
    with a5.open_log(str(log_path)) as log:
        code = a5.run_verify(
            "right-index-finger", "right-middle-finger", "no-match", 2, client, journal, a5.Output(term, log), 0.2
        )
    shown = term.getvalue().splitlines()
    assert code == 0, shown
    assert shown[-1] == "as expected: 2/2"
    assert "  retries    1" in shown
    assert [line for line in shown if not allowed_prompt(line)] == []
    text = log_path.read_text()
    assert "[score] round 1: no-match (verify-no-match), retries 1, sigfm scores 12/105" in text
    assert f"[journal] {SERIAL}" in text and SERIAL not in term.getvalue()


def test_run_enroll_journal_reader_ending_is_told_once(tmp_path):
    client = fake(
        tmp_path / "fprintd-enroll",
        f"""
        import time
        time.sleep(0.3)
        for line in {ENROLL_STDOUT!r}:
            time.sleep(0.01)
            print(line, flush=True)
        """,
    )
    journal = fake(tmp_path / "journalctl", f"print({ACT!r}, flush=True)\n")  # exits at once
    term = io.StringIO()
    log_path = tmp_path / "a5.log"
    with a5.open_log(str(log_path)) as log:
        code = a5.run_enroll("right-index-finger", client, journal, a5.Output(term, log), settle_s=0.2)
    shown = term.getvalue().splitlines()
    assert code == 0, shown
    assert shown.count(a5.MSG_JOURNAL_GONE) == 1
    assert "[note] journal output ended" in log_path.read_text()


@pytest.mark.parametrize("missing", ["client", "journal"])
@pytest.mark.parametrize("cmd", ["enroll", "verify"])
def test_run_missing_command_is_one_plain_line(tmp_path, monkeypatch, cmd, missing):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # neither fprintd-* nor journalctl can be found
    journal = "journalctl" if missing == "journal" else fake_journal(tmp_path, [ACT])
    client = f"fprintd-{cmd}"
    if missing == "journal":
        client = fake(tmp_path / client, "print('x')\n")
    term = io.StringIO()
    log_path = tmp_path / "a5.log"
    with a5.open_log(str(log_path)) as log:
        out = a5.Output(term, log)
        if cmd == "enroll":
            code = a5.run_enroll("right-index-finger", client, journal, out, settle_s=0.05)
        else:
            code = a5.run_verify("right-index-finger", "right-index-finger", "match", 2, client, journal, out, 0.05)
    shown = term.getvalue().splitlines()
    want = "journalctl not found: install systemd (journalctl)" if missing == "journal" else f"{client} not found: "
    assert code == 1
    assert shown[-1].startswith(want) and "Traceback" not in term.getvalue()
    if missing == "client":
        assert shown[-1] == f"fprintd-{cmd} not found: install fprintd"
    assert "cannot start [" in log_path.read_text() and "FileNotFoundError" in log_path.read_text()


@pytest.mark.parametrize("cmd", ["enroll", "verify"])
def test_ctrl_c_stops_both_children_and_exits_130(tmp_path, cmd):
    pids = {name: tmp_path / f"{name}.pid" for name in ("journal", "client")}
    body = """
        import os, pathlib, sys, time
        pathlib.Path({path!r}).write_text(str(os.getpid()))
        print("started", flush=True)
        time.sleep(60)
        """
    journal = fake(tmp_path / "journalctl", body.format(path=str(pids["journal"])))
    client = fake(tmp_path / f"fprintd-{cmd}", body.format(path=str(pids["client"])))
    log_path = tmp_path / "a5.log"
    argv = [sys.executable, str(SESSION_PY), cmd, "--log", str(log_path), "--journalctl", journal]
    argv += ["--finger", "left-index-finger"] if cmd == "enroll" else []
    argv += ["--settle-s", "0.05", f"--fprintd-{cmd}", client]
    if cmd == "verify":
        argv += ["--finger-used", "right-index", "--expect", "match"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not all(p.exists() and p.read_text() for p in pids.values()):
            assert time.monotonic() < deadline, "the fake children did not start"
            time.sleep(0.05)
        time.sleep(0.3)  # into pump()
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, stdout + stderr
    assert "Traceback" not in stderr
    assert "Interrupted" in stdout
    if cmd == "enroll":
        assert stdout.splitlines()[-1] == (
            "Interrupted: your LEFT INDEX finger was not enrolled. When ready: tools/dev/a5-enroll.sh left-index-finger"
        )
    for p in pids.values():
        pid = int(p.read_text())
        deadline = time.monotonic() + 3
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                pytest.fail(f"{p.stem} (pid {pid}) outlived the session")
            time.sleep(0.05)


def test_main_refuses_bad_verify_arguments(tmp_path, capsys):
    log = tmp_path / "x.log"
    args = ["verify", "--log", str(log), "--expect", "match", "--journalctl", "/bin/false"]
    assert a5.main([*args, "--finger-used", "right-pinky"]) == 2
    assert a5.main([*args, "--finger-used", "right-index", "--rounds", "0"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_main_enroll_requires_the_finger(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        a5.main(["enroll", "--log", str(tmp_path / "x.log"), "--journalctl", "/bin/false"])
    assert e.value.code == 2 and "--finger" in capsys.readouterr().err
    assert not (tmp_path / "x.log").exists()


def test_open_log_refuses_a_symlink(tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "log"
    link.symlink_to(target)
    with pytest.raises(OSError):
        a5.open_log(str(link))
    assert target.read_text() == "keep"


# ------------------------------------------------------------------ wrappers


@pytest.mark.parametrize("script", [ENROLL_SH, VERIFY_SH, LIB_SH])
def test_wrapper_syntax(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_wrapper_modes():
    assert os.access(ENROLL_SH, os.X_OK) and os.access(VERIFY_SH, os.X_OK)
    assert not os.access(LIB_SH, os.X_OK)  # sourced only


DEBUG_ENV = 'Environment="G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print"'


class Env:
    """A fake system under A5_TEST_ROOT plus a fake fork, and fake commands that record their calls."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        self.fork = tmp / "fork"
        self.calls = tmp / "calls"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        lib = b"\x7fELF fake libfprint"
        sys_lib = self.root / "usr/local/lib/libfprint-0078-dev/libfprint-2.so.2.0.0"
        sys_lib.parent.mkdir(parents=True)
        sys_lib.write_bytes(lib)
        self.sys_lib = sys_lib
        self.build_lib = self.fork / "build/libfprint/libfprint-2.so.2.0.0"
        self.build_lib.parent.mkdir(parents=True)
        self.build_lib.write_bytes(lib)
        self.driver = self.fork / "libfprint/drivers/synaptics0078.c"
        self.driver.parent.mkdir(parents=True)
        self.driver.write_text("#define SYNAPTICS0078_ENROLL_STAGES 18\n")
        os.utime(self.driver, (1_000_000, 1_000_000))  # older than the build
        self.dropin = self.root / "etc/systemd/system/fprintd.service.d/libfprint-0078-dev.conf"
        self.dropin.parent.mkdir(parents=True)
        self.dropin.write_text('[Service]\nEnvironment="G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print"\n')
        self.enrolled = tmp / "enrolled"
        self.enrolled.write_text("")
        rec = f"""
        import pathlib, sys
        with open({str(self.calls)!r}, "a") as f:
            f.write(pathlib.Path(sys.argv[0]).name + " " + " ".join(sys.argv[1:]) + "\\n")
        """
        fake(
            self.bin / "systemctl",
            rec
            + """
        import os
        if "ActiveState" in sys.argv:
            print(os.environ.get("FAKE_ACTIVE", "inactive"))
        elif "ExecMainStartTimestamp" in sys.argv:
            print(os.environ.get("FAKE_START", ""))
        elif "Environment" in sys.argv:
            print(os.environ.get("FAKE_ENV", "G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print"))
        """,
        )
        fake(
            self.bin / "pacman",
            rec
            + """
        print("libfprint-sigfm-0078 1.94.100.r23.g40cda34-1")
        """,
        )
        fake(
            self.bin / "fprintd-list",
            rec
            + f"""
        fingers = pathlib.Path({str(self.enrolled)!r}).read_text().split()
        print("Using device /net/reactivated/Fprint/Device/0")
        print("Fingerprints for user " + sys.argv[1] + " on Synaptics Sensors (press):")
        for i, f in enumerate(fingers):
            print(f" - #{{i}}: {{f}}")
        """,
        )
        fake(
            self.bin / "fprintd-delete",
            rec
            + f"""
        p = pathlib.Path({str(self.enrolled)!r})
        p.write_text(" ".join(f for f in p.read_text().split() if f != sys.argv[3]))
        print("Fingerprint " + sys.argv[3] + " of user " + sys.argv[1] + " deleted on Synaptics Sensors")
        """,
        )
        fake(
            self.bin / "fprintd-enroll",
            rec
            + f"""
        import time
        for line in {ENROLL_STDOUT!r}:
            time.sleep(0.02)
            print(line, flush=True)
        """,
        )
        fake(
            self.bin / "fprintd-verify",
            rec
            + """
        import time
        time.sleep(0.1)
        print("Verify result: verify-match (done)", flush=True)
        """,
        )
        journal_lines = [ACT, SERIAL, L6, L1, *[x for _ in range(a5.ENROLL_STAGES) for x in (L6D, L1)]]
        fake(
            self.bin / "journalctl",
            rec
            + f"""
        import os, time
        if os.environ.get("FAKE_JOURNAL_HIDDEN"):
            print("Hint: You are currently not seeing messages from other users and the system.", file=sys.stderr)
            sys.exit(0)
        if "-f" not in sys.argv:
            sys.exit(0)
        for line in {journal_lines!r}:
            time.sleep(0.01)
            print(line, flush=True)
        time.sleep(60)
        """,
        )

    def to_package(self, debug: str = DEBUG_ENV) -> None:
        """As after the A6 hand-over plus `a5-fprintd-dev.sh log on`: no dev lib or dev drop-in, the
        packaged lib has the driver, and the debug drop-in carries DEBUG (one Environment= line)."""
        self.sys_lib.unlink()
        self.dropin.unlink()
        self.pkg_lib = self.root / "usr/lib/libfprint-2.so.2"
        self.pkg_lib.parent.mkdir(parents=True, exist_ok=True)
        self.pkg_lib.write_bytes(b"\x7fELF packaged libfprint libfprint-synaptics0078 ")
        os.utime(self.pkg_lib, (1_000, 1_000))  # pacman keeps the build time
        self.debug_dropin = self.dropin.parent / "synaptics0078-debug.conf"
        self.debug_dropin.write_text(f"# log on\n[Service]\n{debug}\n")

    def env(self, **extra) -> dict:
        e = {k: v for k, v in os.environ.items() if not k.startswith(("A5_", "LIBFPRINT_FORK", "FAKE_"))}
        e.update(
            A5_TEST_ROOT=str(self.root),
            LIBFPRINT_FORK=str(self.fork),
            A5_SYSTEMCTL=str(self.bin / "systemctl"),
            A5_JOURNALCTL=str(self.bin / "journalctl"),
            A5_FPRINTD_LIST=str(self.bin / "fprintd-list"),
            A5_FPRINTD_DELETE=str(self.bin / "fprintd-delete"),
            A5_FPRINTD_ENROLL=str(self.bin / "fprintd-enroll"),
            A5_FPRINTD_VERIFY=str(self.bin / "fprintd-verify"),
            A5_PACMAN=str(self.bin / "pacman"),
        )
        e.update(extra)
        return e

    def run(self, script: Path, *args: str, stdin: str = "", **extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=self.env(**extra),
        )

    def called(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def logs(self) -> list[Path]:
        d = self.root / "logs"
        return sorted(d.iterdir()) if d.exists() else []


@pytest.fixture
def fakes(tmp_path):
    return Env(tmp_path)


@pytest.mark.parametrize("script", [ENROLL_SH, VERIFY_SH])
def test_wrapper_help(script):
    r = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, check=False)
    assert r.returncode == 0
    assert "never with sudo" in r.stdout and "set -euo" not in r.stdout


@pytest.mark.parametrize("script", [ENROLL_SH, VERIFY_SH])
def test_wrapper_refuses_test_variables_as_root(fakes, script):
    args = ["right-index-finger"] if script == ENROLL_SH else ["--finger-used", "right-index", "--expect", "match"]
    r = fakes.run(script, *args, A5_FAKE_EUID="0")
    assert r.returncode == 2
    assert "test-only variables are refused as root" in r.stderr
    assert fakes.called() == []


@pytest.mark.parametrize("script", [ENROLL_SH, VERIFY_SH])
def test_dry_run_checks_and_starts_no_client(fakes, script):
    args = ["right-index-finger"] if script == ENROLL_SH else ["--finger-used", "right-index", "--expect", "match"]
    r = fakes.run(script, *args, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Checking the setup... ok" in r.stdout
    assert [c.split()[0] for c in fakes.called()] == ["systemctl", "journalctl"]
    assert fakes.logs() == []


@pytest.mark.parametrize(
    "breakage, message",
    [
        ("no lib", "the dev libfprint is not installed"),
        ("rebuilt", "the installed libfprint differs from the fork build"),
        ("no build", "no fork build at"),
        ("no dropin", "no fprintd drop-in"),
        ("no debug", "G_MESSAGES_DEBUG lacks libfprint-synaptics0078"),
        ("old fprintd", "it still runs the old libfprint"),
        ("journal hidden", "cannot read fprintd's journal"),
    ],
)
def test_preflight_failures(fakes, breakage, message):
    extra = {}
    if breakage == "no lib":
        fakes.sys_lib.unlink()
    elif breakage == "rebuilt":
        fakes.build_lib.write_bytes(b"\x7fELF rebuilt")
    elif breakage == "no build":
        fakes.build_lib.unlink()
    elif breakage == "no dropin":
        fakes.dropin.unlink()
    elif breakage == "no debug":
        fakes.dropin.write_text("[Service]\nEnvironment=LD_LIBRARY_PATH=/usr/local/lib/libfprint-0078-dev\n")
    elif breakage == "old fprintd":
        extra = {"FAKE_ACTIVE": "active", "FAKE_START": f"@{int(fakes.sys_lib.stat().st_mtime) - 60}"}
    elif breakage == "journal hidden":
        extra = {"FAKE_JOURNAL_HIDDEN": "1"}
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run", **extra)
    assert r.returncode == 2
    assert message in r.stderr
    if breakage in ("no lib", "rebuilt", "no dropin", "no debug", "old fprintd"):
        assert "sudo tools/dev/a5-fprintd-dev.sh install" in r.stderr


def test_preflight_fprintd_started_after_install_and_stale_build_warning(fakes):
    os.utime(fakes.driver, None)  # the driver source is now newer than the build
    os.utime(fakes.build_lib, (1_000, 1_000))
    start = f"@{int(fakes.sys_lib.stat().st_mtime) + 5}"
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run", FAKE_ACTIVE="active", FAKE_START=start)
    assert r.returncode == 0, r.stderr
    assert "WARN  the fork build is older than synaptics0078.c" in r.stdout
    assert "meson compile -C" in r.stdout


# ------------------------------------------------------------------ preflight: the packaged install (A6)

LOG_ON = "sudo tools/dev/a5-fprintd-dev.sh log on"


@pytest.mark.parametrize("script", [ENROLL_SH, VERIFY_SH])
def test_preflight_accepts_the_package_with_the_debug_dropin(fakes, script):
    fakes.to_package()
    fakes.build_lib.unlink()  # no fork build: the sha comparison is the dev install's only
    args = ["right-index-finger"] if script == ENROLL_SH else ["--finger-used", "right-index", "--expect", "match"]
    r = fakes.run(script, *args, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Checking the setup... ok" in r.stdout and "WARN" not in r.stdout + r.stderr


def test_preflight_logs_the_mode_package(fakes):
    fakes.to_package()
    fakes.enrolled.write_text("right-index-finger")
    r = fakes.run(VERIFY_SH, "--finger-used", "right-index", "--expect", "match", "--rounds", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "mode:" not in r.stdout + r.stderr  # the log only
    (log,) = fakes.logs()
    text = log.read_text()
    assert f"# preflight: mode: package ({fakes.pkg_lib} has the synaptics0078 driver" in text
    assert f"# preflight: drop-in {fakes.debug_dropin} has G_MESSAGES_DEBUG" in text
    assert f"# libfprint: package {fakes.pkg_lib}" in text and "# fork " not in text
    assert "# preflight: package: libfprint-sigfm-0078 1.94.100.r23.g40cda34-1" in text
    sha = hashlib.sha256(fakes.pkg_lib.read_bytes()).hexdigest()
    assert f"# preflight: package lib {fakes.pkg_lib} -> {fakes.pkg_lib} (sha256 {sha})" in text
    assert "# preflight: fprintd.service Environment has G_MESSAGES_DEBUG" in text


def test_preflight_logs_the_mode_dev(fakes):
    fakes.enrolled.write_text("right-index-finger")
    r = fakes.run(VERIFY_SH, "--finger-used", "right-index", "--expect", "match", "--rounds", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "mode:" not in r.stdout + r.stderr
    (log,) = fakes.logs()
    text = log.read_text()
    assert f"# preflight: mode: dev ({fakes.sys_lib})" in text and "# fork " in text


@pytest.mark.parametrize(
    "breakage, message, hint",
    [
        ("no debug dropin", "no debug drop-in at", LOG_ON),
        ("debug lacks driver", "G_MESSAGES_DEBUG lacks libfprint-synaptics0078", LOG_ON),
        ("old fprintd", "since before the debug drop-in was written: it runs without the debug lines", LOG_ON),
        ("dev dropin left", "the hand-over to the package is incomplete", "a5-fprintd-dev.sh undo --keep-data"),
        ("no driver in package", "the libfprint-sigfm-0078 package is not installed", "a5-fprintd-dev.sh install"),
        ("no package lib", "the dev libfprint is not installed", "sudo pacman -U libfprint-sigfm-0078-[0-9]*"),
        ("not loaded", "systemd has not loaded the debug drop-in", LOG_ON + " (it reloads systemd"),
        ("dangling dev dropin", "the hand-over to the package is incomplete", "a5-fprintd-dev.sh undo --keep-data"),
    ],
)
@pytest.mark.parametrize("script", [ENROLL_SH, VERIFY_SH])
def test_preflight_package_failures(fakes, script, breakage, message, hint):
    fakes.to_package()
    extra = {}
    if breakage == "no debug dropin":
        fakes.debug_dropin.unlink()
    elif breakage == "debug lacks driver":
        fakes.debug_dropin.write_text('[Service]\nEnvironment="G_MESSAGES_DEBUG=libfprint-print"\n')
    elif breakage == "old fprintd":
        # Started before the drop-in was written, though after the lib's (build-time) mtime.
        extra = {"FAKE_ACTIVE": "active", "FAKE_START": f"@{int(fakes.debug_dropin.stat().st_mtime) - 60}"}
    elif breakage == "dev dropin left":
        fakes.dropin.write_text('[Service]\nEnvironment="G_MESSAGES_DEBUG=libfprint-synaptics0078"\n')
    elif breakage == "no driver in package":
        fakes.pkg_lib.write_bytes(b"\x7fELF stock libfprint")
    elif breakage == "no package lib":
        fakes.pkg_lib.unlink()
    elif breakage == "not loaded":
        extra = {"FAKE_ENV": ""}  # written, but no daemon-reload since
    elif breakage == "dangling dev dropin":
        fakes.dropin.symlink_to(fakes.tmp / "gone.conf")
    args = ["right-index-finger"] if script == ENROLL_SH else ["--finger-used", "right-index", "--expect", "match"]
    r = fakes.run(script, *args, "--dry-run", **extra)
    assert r.returncode == 2, r.stdout
    assert message in r.stderr and hint in r.stderr
    assert len([ln for ln in r.stderr.splitlines() if ln.startswith("FAILED")]) == 1


def test_preflight_package_staleness_uses_the_dropin_not_the_lib(fakes):
    fakes.to_package()
    dropin_mtime = int(fakes.debug_dropin.stat().st_mtime)
    # The lib is newer than fprintd's start (a reinstall keeps pacman's build time anyway): no matter.
    os.utime(fakes.pkg_lib, (dropin_mtime + 100, dropin_mtime + 100))
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run", FAKE_ACTIVE="active", FAKE_START=f"@{dropin_mtime + 5}")
    assert r.returncode == 0, r.stderr
    # fprintd from before the drop-in: refused, though it started long after the lib's mtime.
    os.utime(fakes.pkg_lib, (1_000, 1_000))
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run", FAKE_ACTIVE="active", FAKE_START=f"@{dropin_mtime - 5}")
    assert r.returncode == 2 and "it restarts fprintd" in r.stderr and LOG_ON in r.stderr


def mtime_ns(p: Path, ns: int) -> str:
    """Set P's mtime to NS (ns since the epoch); returns it as @SECONDS.MICROSECONDS (a start time)."""
    os.utime(p, ns=(ns, ns))
    return f"@{ns // 1_000_000_000}.{ns // 1000 % 1_000_000:06d}"


@pytest.mark.parametrize("package", [True, False])
def test_preflight_staleness_is_sub_second(fakes, package):
    if package:
        fakes.to_package()
    ref = fakes.debug_dropin if package else fakes.sys_lib
    at = mtime_ns(ref, 1_800_000_000_500_000_000)
    # Started at the very microsecond of the write: fine.
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run", FAKE_ACTIVE="active", FAKE_START=at)
    assert r.returncode == 0, r.stderr
    # In the same second, but 0.4 s before the write: stale.
    early = "@1800000000.100000"
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run", FAKE_ACTIVE="active", FAKE_START=early)
    assert r.returncode == 2 and "it restarts fprintd" in r.stderr
    assert (LOG_ON if package else "a5-fprintd-dev.sh install") in r.stderr


def test_preflight_package_verify_warns_without_libfprint_print(fakes):
    fakes.to_package('Environment="G_MESSAGES_DEBUG=libfprint-synaptics0078"')
    r = fakes.run(VERIFY_SH, "--finger-used", "right-index", "--expect", "match", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "WARN  the drop-in's G_MESSAGES_DEBUG lacks libfprint-print: no sigfm scores in the log" in r.stdout
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run")
    assert r.returncode == 0 and "WARN" not in r.stdout


def test_preflight_dev_install_wins_over_the_package(fakes):
    """Both libs present (the package installed, the hand-over not run yet): the dev checks apply."""
    pkg = fakes.root / "usr/lib/libfprint-2.so.2"
    pkg.parent.mkdir(parents=True)
    pkg.write_bytes(b"\x7fELF packaged libfprint libfprint-synaptics0078 ")
    fakes.build_lib.write_bytes(b"\x7fELF rebuilt")
    r = fakes.run(ENROLL_SH, "right-index-finger", "--dry-run")
    assert r.returncode == 2 and "the installed libfprint differs from the fork build" in r.stderr


@pytest.mark.parametrize(
    "args, message",
    [
        (["--finger-used", "right-pinky", "--expect", "match"], "is not a finger"),
        (["--finger-used", "right-index"], "--expect is required"),
        (["--expect", "match"], "--finger-used is required"),
        (["--finger-used", "right-index", "--expect", "maybe"], "--expect must be match or no-match"),
        (["--finger-used", "right-index", "--expect", "match", "--rounds", "51"], "--rounds must be"),
        (["--finger-used", "right-index", "--expect", "match", "--finger", "right-pinky"], "unknown finger"),
        # A mix-up between the finger touched and the enrolled one fprintd checks against:
        (["--finger-used", "left-index", "--expect", "match"], "fprintd checks against right-index-finger"),
        (["--finger-used", "left-index", "--expect", "no-match", "--finger", "left-index"], "the enrolled finger"),
    ],
)
def test_verify_wrapper_rejects_bad_arguments(fakes, args, message):
    r = fakes.run(VERIFY_SH, *args)
    assert r.returncode == 2
    assert message in r.stderr
    assert fakes.called() == []


def test_enroll_wrapper_rejects_an_unknown_finger(fakes):
    r = fakes.run(ENROLL_SH, "right-pinky")
    assert r.returncode == 2 and "unknown finger 'right-pinky'" in r.stderr


@pytest.mark.parametrize("args", [[], ["--replace"], ["--dry-run"]])
def test_enroll_wrapper_requires_the_finger(fakes, args):
    r = fakes.run(ENROLL_SH, *args)
    assert r.returncode == 2
    assert "name the finger to enroll, e.g. tools/dev/a5-enroll.sh left-index-finger" in r.stderr
    assert fakes.called() == [] and fakes.logs() == []


def test_enroll_wrapper_refuses_two_fingers(fakes):
    r = fakes.run(ENROLL_SH, "left-index-finger", "right-index-finger")
    assert r.returncode == 2 and "more than one finger" in r.stderr and fakes.called() == []


def lib_call(snippet: str) -> str:
    """Run `snippet` with tools/dev/a5-session-lib.sh sourced; its stdout."""
    r = subprocess.run(
        ["bash", "-c", f'. "$1"; {snippet}', "lib", str(LIB_SH)], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_lib_finger_names_and_display_match_the_session():
    assert lib_call('echo "$A5S_ENROLL_STAGES"').strip() == str(a5.ENROLL_STAGES)
    assert lib_call('printf "%s\\n" "${A5S_FINGERS[@]}"').split() == list(a5.FINGERS)
    for finger in a5.FINGERS:
        label = a5.finger_label(finger)
        for given in (finger, label, label.upper()):
            assert lib_call(f"a5s_finger_name {given}").strip() == finger
        assert lib_call(f"a5s_finger_display {finger}").strip() == a5.finger_display(finger)
    assert lib_call("a5s_finger_name right-pinky || echo refused").strip() == "refused"


@pytest.mark.parametrize("client", ["A5_FPRINTD_LIST", "A5_FPRINTD_ENROLL", "A5_FPRINTD_DELETE"])
def test_enroll_preflight_missing_fprintd_client_is_one_plain_line(fakes, client):
    r = fakes.run(ENROLL_SH, "left-index-finger", "--dry-run", **{client: str(fakes.tmp / "nowhere" / "fprintd-x")})
    assert r.returncode == 2
    assert "FAILED: fprintd-x not found" in r.stderr and "install fprintd" in r.stderr
    assert "Traceback" not in r.stderr + r.stdout and "command not found" not in r.stderr


def test_verify_preflight_missing_fprintd_verify(fakes):
    missing = str(fakes.tmp / "nowhere" / "fprintd-verify")
    r = fakes.run(
        VERIFY_SH, "--finger-used", "right-index", "--expect", "match", "--dry-run", A5_FPRINTD_VERIFY=missing
    )
    assert r.returncode == 2 and "FAILED: fprintd-verify not found" in r.stderr


def wrapper_terminal_ok(stdout: str) -> list[str]:
    """The wrapper's own short lines plus the session prompts; returns the lines that are neither."""
    own = (
        "Enrolling your ",
        "Enrolled now: ",
        "Your ",
        "A5 verify: ",
        "Checking the setup... ok",
        "A password dialog may appear",
        "Deleted ",
        "Log (private, it holds the sensor serial): ",
        "Touch the sensor only when asked",
        "Nothing changed",
    )
    return [ln for ln in stdout.splitlines() if not (ln.startswith(own) or allowed_prompt(ln) or "[y/N]" in ln)]


def test_enroll_wrapper_full_run_not_enrolled_yet(fakes):
    r = fakes.run(ENROLL_SH, "right-index-finger")
    assert r.returncode == 0, r.stdout + r.stderr
    assert wrapper_terminal_ok(r.stdout) == []
    assert r.stdout.count("A password dialog may appear") == 1
    assert r.stdout.splitlines()[-1].startswith(
        "Done: your RIGHT INDEX finger is enrolled as right-index-finger (18 of 18 touches)"
    )
    assert SERIAL not in r.stdout
    names = [c.split()[0] for c in fakes.called()]
    assert "fprintd-delete" not in names and "fprintd-enroll" in names
    assert f"fprintd-enroll -f {a5.DEFAULT_FINGER}" in fakes.called()
    (log,) = fakes.logs()
    assert log.name.startswith("a5-enroll-") and stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    text = log.read_text()
    assert "[fprintd-list] Fingerprints for user" in text and f"[journal] {SERIAL}" in text
    assert "# preflight: installed" in text


def test_enroll_wrapper_left_index_names_it_from_the_banner_to_the_end(fakes):
    fakes.enrolled.write_text("right-index-finger")
    r = fakes.run(ENROLL_SH, "left-index")  # the short form works too
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "Enrolling your LEFT INDEX finger: use only that finger for all 18 touches"
    assert "Enrolled now: right-index-finger" in lines
    assert "Touch the sensor only when asked, only with your LEFT INDEX finger, and lift when told." in lines
    touches = [line for line in lines if line.startswith("Touch ") and "vary the position" in line]
    assert touches and all("with your LEFT INDEX finger" in line for line in touches)
    assert "RIGHT" not in r.stdout and "[y/N]" not in r.stdout
    assert lines[-1] == (
        "Done: your LEFT INDEX finger is enrolled as left-index-finger (18 of 18 touches). "
        "Next: tools/dev/a5-verify.sh --finger-used left-index --expect match --finger left-index-finger"
    )
    calls = fakes.called()
    assert "fprintd-enroll -f left-index-finger" in calls and not any(c.startswith("fprintd-delete") for c in calls)


def test_enroll_wrapper_nothing_enrolled_says_none(fakes):
    r = fakes.run(ENROLL_SH, "right-index-finger", "--replace")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Enrolled now: none" in r.stdout.splitlines()
    assert "deleting" not in r.stdout and not any(c.startswith("fprintd-delete") for c in fakes.called())


def test_enroll_wrapper_replace_deletes_without_asking(fakes):
    fakes.enrolled.write_text("right-index-finger left-thumb")
    r = fakes.run(ENROLL_SH, "--replace", "right-index-finger", stdin="n\n")  # stdin is not read
    assert r.returncode == 0, r.stdout + r.stderr
    assert wrapper_terminal_ok(r.stdout) == []
    assert "Enrolled now: right-index-finger left-thumb" in r.stdout
    assert "[y/N]" not in r.stdout
    assert (
        "Your RIGHT INDEX finger is already enrolled (right-index-finger): deleting that print first (--replace)."
        in (r.stdout)
    )
    assert "Deleted right-index-finger." in r.stdout
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    calls = fakes.called()
    assert calls.index(f"fprintd-delete {user} -f right-index-finger") < calls.index(
        "fprintd-enroll -f right-index-finger"
    )
    assert fakes.enrolled.read_text().split() == ["left-thumb"]


def test_enroll_wrapper_already_enrolled_answer_yes_deletes_first(fakes):
    fakes.enrolled.write_text("right-index-finger left-thumb")
    r = fakes.run(ENROLL_SH, "right-index-finger", stdin="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    question = (
        "Your RIGHT INDEX finger is already enrolled (right-index-finger). Delete that print and enroll it again?"
    )
    assert question + " [y/N]" in r.stdout
    assert "Deleted right-index-finger." in r.stdout
    assert r.stdout.count("A password dialog may appear") == 1
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    calls = fakes.called()
    assert f"fprintd-delete {user} -f right-index-finger" in calls
    assert calls.index(f"fprintd-delete {user} -f right-index-finger") < calls.index(
        "fprintd-enroll -f right-index-finger"
    )
    assert fakes.enrolled.read_text().split() == ["left-thumb"]


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_enroll_wrapper_already_enrolled_default_no_changes_nothing(fakes, answer):
    fakes.enrolled.write_text("right-index-finger")
    r = fakes.run(ENROLL_SH, "right-index-finger", stdin=answer)
    assert r.returncode == 1
    assert "Nothing changed: right-index-finger stays enrolled. (--replace deletes it without asking.)" in r.stdout
    names = [c.split()[0] for c in fakes.called()]
    assert "fprintd-delete" not in names and "fprintd-enroll" not in names


def test_verify_wrapper_full_run(fakes):
    fakes.enrolled.write_text("right-index-finger")
    r = fakes.run(VERIFY_SH, "--finger-used", "right-index", "--expect", "match", "--rounds", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert wrapper_terminal_ok(r.stdout) == []
    assert r.stdout.splitlines()[-1] == "as expected: 2/2"
    assert "Round 1/2: touch with your RIGHT INDEX finger, then lift" in r.stdout
    assert [c for c in fakes.called() if c.startswith("fprintd-verify")] == ["fprintd-verify -f right-index-finger"] * 2
    (log,) = fakes.logs()
    assert log.name.startswith("a5-verify-")


def test_verify_wrapper_left_index_against_its_own_print(fakes):
    fakes.enrolled.write_text("right-index-finger left-index-finger")
    r = fakes.run(
        VERIFY_SH, "--finger-used", "left-index", "--expect", "match", "--finger", "left-index", "--rounds", "1"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "A5 verify: 1 round(s) with your LEFT INDEX finger against the enrolled left-index-finger, " + (
        "expecting match"
    )
    assert "Round 1/1: touch with your LEFT INDEX finger, then lift" in lines
    assert [c for c in fakes.called() if c.startswith("fprintd-verify")] == ["fprintd-verify -f left-index-finger"]


def test_verify_wrapper_finger_not_enrolled(fakes):
    r = fakes.run(VERIFY_SH, "--finger-used", "right-middle", "--expect", "no-match")
    assert r.returncode == 2
    assert "right-index-finger is not enrolled" in r.stderr and "tools/dev/a5-enroll.sh right-index-finger" in r.stderr
    assert not any(c.startswith("fprintd-verify") for c in fakes.called())
