#!/bin/sh
# Build research/a5/sigfm_matrix.cpp against the fork's compiled SIGFM.
#
# Usage: build_harness.sh FORK_BUILDDIR OUT_BINARY
#   FORK_BUILDDIR  a configured and compiled build dir of the libfprint fork
#                  (e.g. ~/Devel/Linux/Synaptics/libfprint/build); its
#                  libfprint/sigfm/libsigfm.a is the object libfprint links
#   OUT_BINARY     where to put the binary (outside git, e.g. a scratch directory)
#
# Links the same OpenCV the fork found (opencv4, else opencv5, as meson.build).
set -eu
build=${1:?usage: build_harness.sh FORK_BUILDDIR OUT_BINARY}
out=${2:?}
here=$(cd "$(dirname "$0")" && pwd)
src=$(sed -n 's/^Source dir: //p' "$build/meson-logs/meson-log.txt" | head -n1)
[ -n "$src" ] || src=$(cd "$build/.." && pwd)
lib="$build/libfprint/sigfm/libsigfm.a"
[ -f "$lib" ] || { echo "no $lib: compile the fork first (meson compile -C $build)" >&2; exit 1; }
if pkg-config --exists opencv4; then cv=opencv4; else cv=opencv5; fi
mkdir -p "$(dirname "$out")"
# shellcheck disable=SC2046
# --as-needed as meson links libfprint: only the OpenCV modules SIGFM uses
c++ -std=c++17 -O2 -Wall -Wextra -Wl,--as-needed -I"$src/libfprint/sigfm" \
    "$here/sigfm_matrix.cpp" "$lib" $(pkg-config --cflags --libs "$cv") -o "$out"
echo "built $out (SIGFM from $lib, $cv $(pkg-config --modversion "$cv"))"
