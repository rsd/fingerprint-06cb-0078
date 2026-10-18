#!/bin/sh
# Build research/far-diag/sigfm_geometry.cpp against a fork build dir's libsigfm.a
# and the OpenCV it was built with (as research/a5/build_harness.sh does).
# Usage: build_geometry.sh FORK_BUILDDIR OUT_BINARY
set -eu
build=${1:?usage: build_geometry.sh FORK_BUILDDIR OUT_BINARY}
out=${2:?}
here=$(cd "$(dirname "$0")" && pwd)
src=$(cd "$build/.." && pwd)
lib="$build/libfprint/sigfm/libsigfm.a"
[ -f "$lib" ] || { echo "no $lib" >&2; exit 1; }
if pkg-config --exists opencv4; then cv=opencv4; else cv=opencv5; fi
mkdir -p "$(dirname "$out")"
# shellcheck disable=SC2046
c++ -std=c++17 -O2 -Wall -Wextra -Wl,--as-needed -I"$src/libfprint/sigfm" "$here/sigfm_geometry.cpp" "$lib" \
    $(pkg-config --cflags --libs "$cv") -o "$out"
echo "built $out"
