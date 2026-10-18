# A1 report: libfprint fork

Repo: the libfprint fork (`../libfprint`)
Branch: sigfm-0078, head a3f3fe63 (base 6f9479c3). Remotes: origin, ssubbotin, gotenks. Working tree clean (build/ is ignored).

git log --oneline 6f9479c3..HEAD
a3f3fe63 device: Validate gallery equality only for raw prints
35220b7f sigfm: document the benchmark workflow and threshold policy
e2906988 ci: install opencv-devel in the test image
836d1e0e tests: run the matching benchmark smoke in the virtual-driver suite
602ee19e stats: replace Wilson score intervals with exact Clopper-Pearson
e10fbca8 tests: add benchmark-matching, a virtual-driver FPR benchmark
c2d1194d sigfm-bench: reproducible trials, exportable subset, honest statistics
9198c225 virtual-image: benchmark configuration via environment
247b6d3f Add SIGFM (SIFT-based) matching algorithm for small-area sensors

Range check: d258eb51^..7263fb2c = 8 commits; 19f4bb5b not in range; no file path in the range matches fpc/moh. All 9 cherry-picks applied with -x, no conflicts. 406c10e1 not taken.

Comparator fix (libfprint/sigfm/sigfm.cpp):
 L55  constexpr auto max_matches = std::size_t{176};
 L67-68  return std::tie(this->p1.y, this->p1.x, this->p2.y, this->p2.x) <
                std::tie(right.p1.y, right.p1.x, right.p2.y, right.p2.x);
 (std::set<match> matches_unique dedups on this key; L~176 `if (candidates.size() > max_matches) candidates.resize(max_matches);`, preceded by stable_sort.)

Build: meson setup build -Ddrivers=all -Dintrospection=false -Dinstalled-tests=false ; meson compile -C build -> 159/159 OK, 0 warnings in a clean rebuild. build/config.h:10 `#define HAVE_SIGFM 1`.

meson test -C build: Ok 8, Fail 0, Skipped 33 (of 41).
 OK: fpi-print, udev-hwdb, fpi-assembling, fp-context, fpi-ssm, fp-device, metainfo-validate, fpi-device.
 Skips: all 33 are driver/virtual-image tests that meson.build replaces with `sh -c 'exit 77'` stubs because the umockdev/virtual-driver tests are inside `if get_option('introspection')` (tests/meson.build:113). So -Dintrospection=false (prescribed by the brief) means the virtual-image suite and the SIGFM benchmark-matching smoke are NOT run. umockdev-1.0 pkg-config is also absent (not needed for this config).

## Addendum: full suite with introspection on

Build dir build-gi (separate, ignored): meson setup build-gi -Ddrivers=all -Dintrospection=true -Dinstalled-tests=false ; meson compile -C build-gi (161/161). No commits, no code changes; the untracked tests/__pycache__ the tests created was removed.

meson test -C build-gi: 133 tests, Ok 132, Fail 1, Skipped 0 (reproduced in two full runs; no skips remain).

Failure: drivers+custom - libfprint:egis_etu905 (upstream egismoc driver, unrelated to SIGFM; 6.1 s).
 libfprint-egis_etu905-WARNING: Cancel command failed: transfer timed out
 umockdev-pcap.vala: Replay may be stuck: Reaping discard URB of type INTERRUPT, endpoint 0x83 length 64 without corresponding submit
 UMockdev: Queued URB 0 has a metadata mismatch! (BULK ep 0x02 len 18 NOT submitted; "Stuck for 2001 ms")
 -> umockdev-run exit 134 (abort via G_DEBUG=fatal-warnings), CalledProcessError in umockdev-test.py:121.
 It is timing-dependent in the cancel/identify part of the test: run alone it passed in 7 of 8 attempts and in 3 of 3 repeats, but it failed 8 of 9 in one --repeat 10 batch and in both full runs. Not compared against an unmodified 6f9479c3 build (not done; would need a second build tree).

SIGFM benchmark smoke (virtual-driver - libfprint:benchmark-matching-smoke): RAN and PASSED (1.64 s): "equivalence: OK (4 trials, pixels and scores identical)", 4 trials locked, 12 payload hashes identical, 8 score comparisons identical, 4 decisions @ 24 agree with the manifest, galleries enrolled 2 (sigfm) / 2 (nbis, 0 failed). All VirtualImage/VirtualDevice* tests also passed.

## Addendum 2: is egis_etu905 failure pre-existing?
Baseline: a separate clone of the fork at detached 6f9479c3, same options, built. `meson test --repeat 10 egis_etu905`, run back to back (base then fork), twice:
 round 1: base 1 pass / 8 fail ; fork 0 pass / 8 fail
 round 2: base 2 pass / 8 fail ; fork 2 pass / 8 fail
(each batch counts ~9 of 10 iterations). Same flaky failure rate on the unmodified base: PRE-EXISTING upstream flake, not introduced by the cherry-picks.
Diff 6f9479c3..HEAD: no egismoc paths, no fpi-usb-transfer. Touches libfprint/fpi-device.c (4 lines, the 094b65d0 gallery-equality change), fpi-print.c/h (SIGFM matching), drivers/*.c one line each in 19 other drivers (not egismoc), egis0570 (different driver from egis_etu905).
