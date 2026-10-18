#!/bin/sh
# Rerun the whole matcher evaluation on a capture set.
#
# Usage: run_all.sh CAPTURES OUT WORKDIR
#   CAPTURES  proto/captures (or any folder of finger folders with manifest.json)
#   OUT       results directory; must be outside git (results are biometric-derived)
#   WORKDIR   scratch dir for the venv, the patched libfprint build and the
#             crop-control sets (e.g. a scratch directory)
#
# Needs: libfprint with the virtual_image driver and its FPrint-2.0 typelib,
# python-gobject, meson/ninja/pixman/libgusb (for the patched build), network
# access on the first run (pip, git clone).
#
# Writes OUT/{nbis,sift}-summary.md, per-variant OUT/*.md, OUT/overlap-*.md and
# OUT/control-<size>/ for the crop control on libfprint's NIST example prints.
set -eu
caps=${1:?usage: run_all.sh CAPTURES OUT WORKDIR}
out=${2:?}
work=${3:?}
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$work"
if [ ! -x "$work/venv/bin/python" ]; then
    # system site packages for gi (FPrint); OpenCV 4.x still ships AKAZE
    python3 -m venv --system-site-packages "$work/venv"
    "$work/venv/bin/pip" install -q 'opencv-python-headless==4.14.0.94' numpy
fi
py="$work/venv/bin/python"
[ -f "$work/build-lf/libfprint/libfprint-2.so.2" ] || sh "$here/build_libfprint.sh" "$work"

"$py" "$here/nbis_libfprint.py" "$caps" --out "$out" --libfprint-build "$work/build-lf" --crosscheck 4 --ks 3,5,7
"$py" "$here/sift_matcher.py" "$caps" --out "$out" --overlap pr288-mask25 --ks 3,5,7

prints="$work/src/libfprint/examples/prints"
for sz in 144x56 288x112 288x224; do
    "$py" "$here/make_crop_set.py" "$prints/arch.png" "$prints/loop-right.png" \
        "$prints/tented_arch.png" "$prints/whorl.png" --out "$work/control/crops-$sz" --crop "$sz"
    "$py" "$here/nbis_libfprint.py" "$work/control/crops-$sz" --out "$out/control-$sz" \
        --libfprint-build "$work/build-lf" --variants stock,up2-partial,stock-cubic2
    # SIGFM's score counts pairs of match pairs (O(n^4) in matches): slow on large crops
    kp=pr288-nomask,pr288-mask25
    [ "$sz" = 144x56 ] && kp="$kp,sigfm-1x,sigfm-up2nn"
    "$py" "$here/sift_matcher.py" "$work/control/crops-$sz" --out "$out/control-$sz" --variants "$kp"
done
"$py" "$here/summarize.py" "$out" --detail stock-cubic2,up2-partial,pr288,pr288-mask25,sigfm-up2nn > "$out/digest.md"
echo "digest: $out/digest.md"
