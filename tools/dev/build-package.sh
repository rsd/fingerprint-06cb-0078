#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Local development build of libfprint-sigfm-0078 from this working tree and a local fork.
#
#   tools/dev/build-package.sh                  snapshot, rewrite the PKGBUILD, run makepkg
#   tools/dev/build-package.sh --prepare-only   stop before makepkg; print the PKGBUILD path
#
# Run it as yourself, not as root. The project is the git repo that holds this script.
#
# The in-tree PKGBUILD builds from the public repos (the fork by commit, this project by
# release tag). This script builds the code on disk instead:
# - it copies the project's working tree (tracked files plus untracked files that are not
#   ignored, minus tracked files that are now ignored, deleted files skipped) into a
#   throwaway git repo, BUILD_ROOT/snap, and commits it there. The project's own index and
#   tree are never written;
# - it writes a copy of the PKGBUILD and the .install to BUILD_ROOT/pkgbuild, with both
#   sources on git+file:// and pinned by commit (the fork at FORK_REV, the snapshot at its
#   commit) and both b2sums SKIP. Its pkgver names the fork commit
#   (<meson version>.r<count>.g<hash>, from `git describe --tags`; the fork's tags must be
#   fetched) and its pkgrel is 0.<UTC timestamp>: successive dev builds upgrade each other,
#   and the release (pkgrel 1 or more) upgrades a dev build of the same pkgver;
# - it runs `makepkg -f -C -L` there, with BUILDDIR, PKGDEST, SRCDEST and LOGDEST under
#   BUILD_ROOT, and prints the built package path(s), without the debug package.
#
# Environment:
#   LIBFPRINT_FORK     the libfprint fork checkout (default: ../libfprint next to the project)
#   FORK_REV           the fork revision to build (default HEAD), resolved to its full hash
#   BUILD_ROOT         where everything is written (default: <project>/local/pkg-dev).
#                      The script marks it with a .build-package-root file and refuses a
#                      non-empty directory without that mark: it deletes and recreates
#                      directories inside it.
#   MAKEPKG_FLAGS      extra makepkg flags, e.g. "-s" (install missing deps) or "-d"
#   MESON_TESTTHREADS  parallel meson tests (default 2: the umockdev replays have 15 s
#                      timeouts that a busy machine overruns)
set -euo pipefail
export LC_ALL=C
# A caller's git environment (a hook, a worktree) must not redirect any git call below.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR

PKGNAME=libfprint-sigfm-0078

die() {
  echo "build-package: $*" >&2
  exit 1
}

usage() {
  sed -n '3,9s/^# \{0,1\}//p' "$0"
}

prepare_only=0
while (($#)); do
  case $1 in
    --prepare-only) prepare_only=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
  shift
done

[[ $EUID -ne 0 ]] || die "run this as your own user, not as root (makepkg refuses root)"

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT=$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null) ||
  die "$script_dir is not inside a git repo"
PKGSRC=$PROJECT/packaging/arch/$PKGNAME
[[ -f $PKGSRC/PKGBUILD && -f $PKGSRC/$PKGNAME.install ]] || die "no PKGBUILD in $PKGSRC"

# The fork, at a full commit hash.
fork_in=${LIBFPRINT_FORK:-$(dirname "$PROJECT")/libfprint}
[[ -d $fork_in ]] || die "libfprint fork not found: $fork_in (set LIBFPRINT_FORK)"
FORK=$(cd "$fork_in" && pwd -P)
git -C "$FORK" rev-parse --git-dir >/dev/null 2>&1 ||
  die "libfprint fork is not a git repo: $FORK (set LIBFPRINT_FORK)"
fork_rev=${FORK_REV:-HEAD}
fork_commit=$(git -C "$FORK" rev-parse --verify --quiet "$fork_rev^{commit}") ||
  die "no commit $fork_rev in the fork $FORK (set FORK_REV)"
[[ $fork_commit =~ ^[0-9a-f]{40}$ ]] || die "unexpected hash for $fork_rev: $fork_commit"

# The dev version: pkgver from the fork commit (<meson version>.r<count>.g<hash>, as the
# fork's git history names it), pkgrel 0.<UTC timestamp>. Without a reachable tag only the
# PKGBUILD's own pinned commit keeps the PKGBUILD's static pkgver.
meson_build=$(git -C "$FORK" show "$fork_commit:meson.build") ||
  die "the fork has no meson.build at $fork_commit"
base=$(sed -n "/^ *version: '\([^']*\)',\$/{s//\1/p;q}" <<<"$meson_build")
[[ -n $base ]] || die "no version: line in the fork's meson.build at $fork_commit"
if desc=$(git -C "$FORK" describe --long --tags --abbrev=7 "$fork_commit" 2>/dev/null); then
  hash=${desc##*-g}
  count=${desc%-g*}
  count=${count##*-}
  pkgver=$base.r$count.g$hash
else
  static_commit=$(sed -n 's/^_commit=//p' "$PKGSRC/PKGBUILD")
  [[ $fork_commit == "$static_commit" ]] ||
    die "git describe finds no tag before $fork_commit in $FORK, so the dev pkgver" \
      "cannot name it; fetch the fork's tags (e.g. git -C $FORK fetch --tags upstream)"
  pkgver=$(sed -n 's/^pkgver=//p' "$PKGSRC/PKGBUILD")
fi
[[ $pkgver =~ ^[[:alnum:]._+]+$ ]] || die "unexpected pkgver: $pkgver"
pkgrel=0.$(date -u +%Y%m%d%H%M%S)

# The build root. Inside the project it must be ignored, or the snapshot would copy it.
ROOT=$(realpath -m -- "${BUILD_ROOT:-$PROJECT/local/pkg-dev}")
case $ROOT/ in
  "$PROJECT"/*)
    [[ $ROOT != "$PROJECT" ]] && git -C "$PROJECT" check-ignore -q "${ROOT#"$PROJECT"/}/" ||
      die "BUILD_ROOT $ROOT is inside the project but not ignored by git"
    ;;
esac
SNAP=$ROOT/snap
PKGB=$ROOT/pkgbuild

# The two file:// URLs go into the PKGBUILD single-quoted, and through sed: refuse what
# neither can carry (a quote, a newline, makepkg's URL fragment mark #), escape sed's
# replacement metacharacters.
for path in "$FORK" "$SNAP"; do
  [[ $path != *[\'#$'\n']* ]] ||
    die "a path with a single quote, # or a newline cannot go into the PKGBUILD: $path"
done
sed_repl() {
  local s=${1//\\/\\\\}
  s=${s//&/\\&}
  printf '%s' "${s//|/\\|}"
}

# Directories inside the build root are deleted below: only in a root this script made.
MARK=$ROOT/.build-package-root
if [[ -e $ROOT ]]; then
  [[ -d $ROOT ]] || die "BUILD_ROOT $ROOT is not a directory"
  [[ -f $MARK || -z $(ls -A -- "$ROOT") ]] ||
    die "BUILD_ROOT $ROOT is not empty and has no .build-package-root: not a build" \
      "root of this script; pick an empty or new directory"
fi
mkdir -p "$ROOT"
echo "build root of tools/dev/build-package.sh: its contents are deleted on each run" >"$MARK"

rm -rf "$SNAP" "$PKGB"
mkdir -p "$SNAP" "$PKGB" "$ROOT/build" "$ROOT/pkgdest" "$ROOT/srcdest" "$ROOT/logdest"

# The snapshot: tracked plus untracked-not-ignored files, minus tracked files now ignored
# (they are being untracked), skipping files deleted on disk.
n=0
while IFS= read -r -d '' rel; do
  [[ -f $PROJECT/$rel || -L $PROJECT/$rel ]] || continue
  mkdir -p "$SNAP/$(dirname "$rel")"
  cp -a "$PROJECT/$rel" "$SNAP/$rel"
  n=$((n + 1))
done < <(comm -z -23 \
  <(git -C "$PROJECT" ls-files -z -co --exclude-standard | sort -zu) \
  <(git -C "$PROJECT" ls-files -z -ci --exclude-standard | sort -zu))
((n > 0)) || die "the snapshot of $PROJECT is empty"

export GIT_AUTHOR_NAME=snapshot GIT_AUTHOR_EMAIL=snapshot@example.invalid
export GIT_COMMITTER_NAME=snapshot GIT_COMMITTER_EMAIL=snapshot@example.invalid
# No template and no hooks of the caller's: the snapshot is a plain copy.
git -C "$SNAP" init -q --template=
git -C "$SNAP" add -A
git -C "$SNAP" -c core.hooksPath=/dev/null -c commit.gpgsign=false \
  commit -q --no-verify -m "snapshot of the working tree"
snap_commit=$(git -C "$SNAP" rev-parse HEAD)

# The PKGBUILD copy: the dev version, both sources on file://, both pinned by commit,
# both sums SKIP.
cp "$PKGSRC/PKGBUILD" "$PKGSRC/$PKGNAME.install" "$PKGB/"
# shellcheck disable=SC2016  # ${_tooltag} is the PKGBUILD's literal text
sed -i \
  -e "s|^pkgver=.*|pkgver=$pkgver|" \
  -e "s|^pkgrel=.*|pkgrel=$pkgrel|" \
  -e "s|^_repo=.*|_repo='file://$(sed_repl "$FORK")'|" \
  -e "s|^_commit=.*|_commit=$fork_commit|" \
  -e "s|^_toolrepo=.*|_toolrepo='file://$(sed_repl "$SNAP")'|" \
  -e 's|#tag=${_tooltag}"|#commit='"$snap_commit"'"|' \
  -e "s|'[0-9a-f]\{128\}'|'SKIP'|g" \
  "$PKGB/PKGBUILD"
check() {
  grep -qxF -- "$1" "$PKGB/PKGBUILD" || die "rewriting the PKGBUILD failed: no line $1"
}
check "pkgver=$pkgver"
check "pkgrel=$pkgrel"
check "_repo='file://$FORK'"
check "_commit=$fork_commit"
check "_toolrepo='file://$SNAP'"
# shellcheck disable=SC2016  # the PKGBUILD's literal text
check '  "libfprint::git+${_repo}#commit=${_commit}"'
# shellcheck disable=SC2016
check '  "fp0078::git+${_toolrepo}#commit='"$snap_commit"'"'
check "b2sums=('SKIP'"
! grep -qE "[0-9a-f]{128}|#tag=" "$PKGB/PKGBUILD" ||
  die "rewriting the PKGBUILD failed: a tag pin or a checksum is left"

echo "snapshot: $n files, commit $snap_commit; fork $FORK at $fork_commit;" \
  "version $pkgver-$pkgrel" >&2

if ((prepare_only)); then
  echo "$PKGB/PKGBUILD"
  exit 0
fi

# The snapshot is new on each run, so its bare clone in SRCDEST would be stale.
rm -rf "$ROOT/srcdest/fp0078"

read -ra extra <<<"${MAKEPKG_FLAGS:-}"
export MESON_TESTTHREADS=${MESON_TESTTHREADS:-2}
export BUILDDIR=$ROOT/build PKGDEST=$ROOT/pkgdest SRCDEST=$ROOT/srcdest LOGDEST=$ROOT/logdest
cd "$PKGB"
makepkg -f -C -L "${extra[@]}" >&2
makepkg --packagelist | grep -v -- '-debug-' || die "makepkg lists no package"
