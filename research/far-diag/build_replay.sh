#!/bin/sh
# Build research/far-diag/far_replay.c against a fork build dir's private
# libfprint (libfprint-private.a + libsigfm.a), with the exact compile and
# link flags meson uses for the fork's own tests/test-fpi-print.
#
# Usage: build_replay.sh FORK_BUILDDIR OUT_BINARY
#   FORK_BUILDDIR  e.g. ~/Devel/Linux/Synaptics/libfprint/build (the build the
#                  installed fprintd lib came from)
#   OUT_BINARY     where to put the binary (outside git, e.g. local/far-diag/bin)
set -eu
build=$(cd "${1:?usage: build_replay.sh FORK_BUILDDIR OUT_BINARY}" && pwd)
out=${2:?}
here=$(cd "$(dirname "$0")" && pwd)
ninja="$build/build.ninja"
[ -f "$build/libfprint/libfprint-private.a" ] || { echo "no libfprint-private.a in $build" >&2; exit 1; }

# Compile ARGS of test-fpi-print.c.o and LINK_ARGS of tests/test-fpi-print.
cargs=$(awk '/^build tests\/test-fpi-print.p\/test-fpi-print.c.o:/{f=1} f&&/^ ARGS = /{sub(/^ ARGS = /,"");print;exit}' "$ninja")
largs=$(awk '/^build tests\/test-fpi-print:/{f=1} f&&/^ LINK_ARGS = /{sub(/^ LINK_ARGS = /,"");print;exit}' "$ninja")
[ -n "$cargs" ] && [ -n "$largs" ] || { echo "test-fpi-print not found in $ninja" >&2; exit 1; }
# Drop the test-utils whole-archive (it has its own main helpers); keep the rest.
largs=$(printf '%s' "$largs" | sed -e 's#-Wl,--whole-archive -Wl,--start-group tests/libfprint-test-utils.a -Wl,--no-whole-archive#-Wl,--start-group#' -e "s#'-Wl,-rpath,\$\$ORIGIN/../libfprint'##")

mkdir -p "$(dirname "$out")"
out=$(cd "$(dirname "$out")" && pwd)/$(basename "$out")
cd "$build"
# Relative -I paths in ARGS are relative to the build dir, hence the cd.
# shellcheck disable=SC2086
eval cc $cargs -Wno-missing-prototypes -c "$here/far_replay.c" -o "$out.o"
# shellcheck disable=SC2086
eval c++ -o "$out" "$out.o" $largs -Wl,-rpath,"$build/libfprint"
rm -f "$out.o"
echo "built $out against $build"
