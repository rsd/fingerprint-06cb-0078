#!/bin/sh
# Build a private libfprint with the evaluation patch, for nbis_libfprint.py's
# driver-side variants (FPI_IMAGE_PARTIAL / COLORS_INVERTED flags and
# fpi_image_resize, as egis0570 and elanspi apply them).
#
# Usage: build_libfprint.sh WORKDIR [TAG]
#   WORKDIR  scratch directory; gets src/libfprint and build-lf/
#   TAG      libfprint tag matching the installed library (default v1.94.100)
#
# Then: nbis_libfprint.py ... --libfprint-build WORKDIR/build-lf
#
# Only the library is built (no introspection; the installed FPrint-2.0 typelib
# is reused and LD_LIBRARY_PATH selects this library). Needs meson, ninja,
# glib2, libgusb, pixman. The patch also fixes tests/meson.build for meson >= 1.12
# when introspection is off.
set -eu
work=${1:?usage: build_libfprint.sh WORKDIR [TAG]}
tag=${2:-v1.94.100}
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$work/src"
if [ ! -d "$work/src/libfprint" ]; then
    git clone -q --depth 1 --branch "$tag" https://gitlab.freedesktop.org/libfprint/libfprint.git "$work/src/libfprint"
    git -C "$work/src/libfprint" apply "$here/libfprint-virtual-image-eval.patch"
fi
meson setup --wipe "$work/build-lf" "$work/src/libfprint" \
    -Ddrivers=virtual_image -Dintrospection=false -Ddoc=false -Dgtk-examples=false \
    -Dudev_rules=disabled -Dudev_hwdb=disabled -Dinstalled-tests=false --buildtype=release >/dev/null
ninja -C "$work/build-lf" libfprint/libfprint-2.so.2.0.0
echo "built $work/build-lf/libfprint/libfprint-2.so.2"
