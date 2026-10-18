#!/usr/bin/env bash
# Step A5 dev setup: make the system fprintd (its unit and sandbox unchanged) load the
# libfprint fork's build, and install the synaptics0078 data files where it reads them.
#
#   sudo tools/dev/a5-fprintd-dev.sh install   copy the fork's lib, the data files and a drop-in
#   tools/dev/a5-fprintd-dev.sh status         what fprintd loads and sees (more with sudo)
#   sudo tools/dev/a5-fprintd-dev.sh undo      remove exactly what install created
#
# Options: --dry-run (print every action, change nothing), --data-dir DIR (install: data
# source, default local/synaptics0078), --yes-delete-prints / --keep-prints (undo: answer
# the prompt about enrolled prints of this driver; the default answer is no), -h/--help.
# Env: LIBFPRINT_FORK (default: ../libfprint next to this project). sudo drops it, so
# pass it on the sudo line: sudo LIBFPRINT_FORK=DIR tools/dev/a5-fprintd-dev.sh install
#
# install copies FORK/build/libfprint/libfprint-2.so.2.0.0 to /usr/local/lib/libfprint-0078-dev/,
# module.bin and program-<serial>.bin to /var/lib/fprint/synaptics0078/ (0700, files 0600),
# and adds /etc/systemd/system/fprintd.service.d/libfprint-0078-dev.conf (LD_LIBRARY_PATH,
# OPENCV_OPENCL_RUNTIME=disabled, G_MESSAGES_DEBUG for the driver's journal lines). It
# records what it created in a manifest; undo removes only that. Nothing under a
# package-owned path is written. Dev only: A6 ships a package.
#
# TEST ONLY (honoured only while A5_TEST_ROOT is set; never set them on a real system;
# as root the script refuses to run while any of them is set, e.g. passed by sudo -E):
#   A5_TEST_ROOT     prefix for every system path; no root needed, no chown
#   A5_SYSTEMCTL, A5_FPRINTD_LIST, A5_PACMAN   commands to run instead of the real ones
#   A5_MODULE_SHA256 expected module.bin sha256 (synthetic test data)
#   A5_FAKE_EUID     (not root only) the euid the root check above sees, to test it
set -euo pipefail
umask 022

# The test hooks redirect what is written and run: never as root.
TEST_VARS=(A5_TEST_ROOT A5_SYSTEMCTL A5_FPRINTD_LIST A5_PACMAN A5_MODULE_SHA256 A5_FAKE_EUID)
guard_euid=$(id -u)
[ "$guard_euid" -eq 0 ] || guard_euid=${A5_FAKE_EUID:-$guard_euid}
if [ "$guard_euid" = 0 ]; then
	for v in "${TEST_VARS[@]}"; do
		if [ -n "${!v+set}" ]; then
			printf 'FAILED: %s is set; the test-only variables are refused as root\n' "$v" >&2
			printf '  hint: unset it (sudo -E and env_keep pass it on): sudo env -u %s ...\n' "$v" >&2
			exit 2
		fi
	done
fi

SELF=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
FORK_DEFAULT=$(cd -- "$ROOT/.." && pwd)/libfprint
FORK=${LIBFPRINT_FORK:-$FORK_DEFAULT}
DATA_SRC=$ROOT/local/synaptics0078

# The path fprintd sees (the drop-in names it), and the data dir compiled into the driver.
SYS_LIBDIR=/usr/local/lib/libfprint-0078-dev
SYS_DATA_DIR=/var/lib/fprint/synaptics0078
DRIVER=synaptics0078
SONAME=libfprint-2.so.2
LIBFILE=libfprint-2.so.2.0.0
MODULE_SIZE=10500
MODULE_SHA256=67f4a332d89f76fe12c57fa7f67b43a076f689e78267b888c694e26b05caacc1
PROGRAM_SIZE=18869
UNIT=fprintd.service

TR=${A5_TEST_ROOT:-}
SYSTEMCTL=(systemctl)
FPRINTD_LIST=(fprintd-list)
PACMAN=(pacman)
if [ -n "$TR" ]; then
	case $TR in /*) ;; *) echo "A5_TEST_ROOT must be an absolute path" >&2 && exit 2 ;; esac
	# Canonical, so the manifest's canonical-path check holds under it.
	TR=$(realpath -e -- "$TR") || exit 2
	[ -z "${A5_SYSTEMCTL:-}" ] || SYSTEMCTL=("$A5_SYSTEMCTL")
	[ -z "${A5_FPRINTD_LIST:-}" ] || FPRINTD_LIST=("$A5_FPRINTD_LIST")
	[ -z "${A5_PACMAN:-}" ] || PACMAN=("$A5_PACMAN")
	MODULE_SHA256=${A5_MODULE_SHA256:-$MODULE_SHA256}
fi

LIBDIR=$TR$SYS_LIBDIR
LIB=$LIBDIR/$LIBFILE
LINK=$LIBDIR/$SONAME
MANIFEST=$LIBDIR/a5-install.manifest
STOCK_LIB=$TR/usr/lib/$SONAME
STATE_DIR=$TR/var/lib/fprint
DATA_DIR=$TR$SYS_DATA_DIR
DROPIN_DIR=$TR/etc/systemd/system/fprintd.service.d
DROPIN=$DROPIN_DIR/libfprint-0078-dev.conf
PROC=$TR/proc
DROPIN_TEXT="# Written by fingerprint-06cb-0078/tools/dev/a5-fprintd-dev.sh (A5, dev only).
# Remove with: sudo tools/dev/a5-fprintd-dev.sh undo
# Loads the libfprint fork instead of the packaged libfprint. The sandbox is unchanged.
[Service]
Environment=LD_LIBRARY_PATH=$SYS_LIBDIR
# SIGFM needs no OpenCL; keep OpenCV from loading an OpenCL runtime inside fprintd.
Environment=OPENCV_OPENCL_RUNTIME=disabled
# The driver's info lines (ROM, serial, module upload, finger off) and the SIGFM scores in
# the journal, for the A5 checks. The serial makes the journal private.
Environment=\"G_MESSAGES_DEBUG=libfprint-synaptics0078 libfprint-print\"
"

usage() { sed -n '2,/^set /s/^# \{0,1\}//p' "$SELF"; }
fail() {
	printf 'FAILED: %s\n' "$1" >&2
	[ $# -lt 2 ] || printf '  hint: %s\n' "$2" >&2
	exit 2
}
ok() { printf '  ok    %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }
# To stderr: some functions' stdout is data (prints_dirs), a warning must never become a path.
warn() { printf '  WARN  %s\n' "$*" >&2; }
already() { printf '  already %s\n' "$*"; }

dry_run=0
prints_answer=""
cmd=""
data_given=0
orig_args=("$@")
while [ $# -gt 0 ]; do
	case $1 in
	install | status | undo)
		[ -z "$cmd" ] || fail "more than one subcommand: $cmd, $1" "see $0 --help"
		cmd=$1
		;;
	--dry-run) dry_run=1 ;;
	--data-dir)
		[ $# -ge 2 ] && [ -n "$2" ] || fail "--data-dir needs a directory"
		DATA_SRC=$2
		data_given=1
		shift
		;;
	--data-dir=*)
		DATA_SRC=${1#--data-dir=}
		[ -n "$DATA_SRC" ] || fail "--data-dir needs a directory"
		data_given=1
		;;
	--yes-delete-prints) prints_answer=yes ;;
	--keep-prints) prints_answer=no ;;
	-h | --help)
		usage
		exit 0
		;;
	*) fail "unknown argument: $1" "see $0 --help" ;;
	esac
	shift
done
[ -n "$cmd" ] || fail "no subcommand" "use install, status or undo; see $0 --help"
[ "$data_given" -eq 0 ] || [ "$cmd" = install ] || fail "--data-dir only applies to install"
[ -z "$prints_answer" ] || [ "$cmd" = undo ] || fail "--yes-delete-prints/--keep-prints only apply to undo"

is_root() { [ "$(id -u)" -eq 0 ]; }
# install/undo write system paths: root, or a test root. A dry run may preview as a user.
need_root() {
	[ -z "$TR" ] || return 0
	is_root && return 0
	if [ "$dry_run" -eq 1 ]; then
		note "not root: dry run only; paths under /var/lib/fprint (mode 0700) cannot be inspected, so the plan assumes they are absent"
		return 0
	fi
	local line=(sudo)
	[ -z "${LIBFPRINT_FORK:-}" ] || line+=("LIBFPRINT_FORK=$LIBFPRINT_FORK")
	line+=("$SELF" "${orig_args[@]}")
	printf -v line_q '%q ' "${line[@]}"
	printf '%s needs root; nothing was changed. Run:\n  %s\n' "$cmd" "${line_q% }" >&2
	exit 2
}

# Owner of what install writes: root:root, or the test user under a test root.
OWN=()
expect_uid=0
if [ -n "$TR" ]; then expect_uid=$(id -u); else OWN=(-o root -g root); fi

sha() { sha256sum -- "$1" | cut -d' ' -f1; }
fsize() { stat -c %s -- "$1"; }
# Print an action, then do it (unless --dry-run).
act() {
	local q
	printf -v q '%q ' "$@"
	printf '+ %s\n' "${q% }"
	[ "$dry_run" -eq 1 ] || "$@"
}
# A real directory (not a symlink) owned by the expected user, or absent.
check_dir() {
	local d=$1
	[ ! -L "$d" ] || fail "$d is a symlink; refusing to write through it"
	[ -e "$d" ] || return 0
	[ -d "$d" ] || fail "$d exists and is not a directory"
	[ "$(stat -c %u -- "$d")" = "$expect_uid" ] || fail "$d is not owned by uid $expect_uid; refusing to use it"
}
# A regular file (not a symlink), or absent.
check_file() {
	local f=$1
	[ ! -L "$f" ] || fail "$f is a symlink; refusing to replace or follow it"
	[ ! -e "$f" ] || [ -f "$f" ] || fail "$f exists and is not a regular file"
}

# ---------------------------------------------------------------- manifest
# One line per thing install created: "dir PATH", "file PATH SHA256", "link PATH TARGET".
declare -A MTYPE=() MVAL=()
MORDER=()
# A path install writes, of that type, spelled canonically (no //, ., .., trailing /) and
# with no symlink in its parent directories: undo deletes what the manifest names, so an
# entry must never lead it outside these exact targets.
manifest_path_ok() {
	local t=$1 p=$2 parent
	case $p in /*) ;; *) return 1 ;; esac
	case $p in *//* | */./* | */../* | */. | */.. | */) return 1 ;; esac
	parent=$(realpath -m -- "${p%/*}" 2>/dev/null) || return 1
	[ "$parent/${p##*/}" = "$p" ] || return 1
	case $t:$p in
	"dir:$LIBDIR" | "dir:$STATE_DIR" | "dir:$DATA_DIR" | "dir:$DROPIN_DIR") return 0 ;;
	"file:$LIB" | "file:$DROPIN" | "file:$DATA_DIR/module.bin" | "link:$LINK") return 0 ;;
	esac
	[ "$t" = file ] && [ "${p%/*}" = "$DATA_DIR" ] && [[ ${p##*/} =~ ^program-[0-9a-f]{12}\.bin$ ]]
}
manifest_load() {
	check_file "$MANIFEST"
	[ -f "$MANIFEST" ] || return 0
	# Only a manifest its owner alone can write is trusted: root's, or the test user's.
	[ "$(stat -c %u -- "$MANIFEST")" = "$expect_uid" ] ||
		fail "$MANIFEST is not owned by uid $expect_uid; refusing to act on it" "inspect it; remove it by hand if it is not ours"
	! (($(stat -c '%#a' -- "$MANIFEST") & 8#022)) ||
		fail "$MANIFEST is group- or world-writable; refusing to act on it" "inspect it; remove it by hand if it is not ours"
	local t p v
	while IFS=$'\t' read -r t p v; do
		case $t in '' | '#'*) continue ;; esac
		manifest_path_ok "$t" "$p" ||
			fail "$MANIFEST names '$t $p', which install never writes; refusing to act on it" \
				"inspect it; nothing was changed"
		[ -n "${MTYPE[$p]:-}" ] || MORDER+=("$p")
		MTYPE[$p]=$t
		MVAL[$p]=$v
	done <"$MANIFEST"
}
manifest_add() {
	[ -n "${MTYPE[$2]:-}" ] || MORDER+=("$2")
	MTYPE[$2]=$1
	MVAL[$2]=${3:-}
	manifest_save
}
manifest_drop() {
	unset "MTYPE[$1]" "MVAL[$1]"
	local p rest=()
	for p in "${MORDER[@]}"; do [ "$p" = "$1" ] || rest+=("$p"); done
	MORDER=("${rest[@]}")
}
manifest_save() {
	[ "$dry_run" -eq 0 ] || return 0
	local tmp p
	tmp=$(mktemp "$LIBDIR/.a5-install.manifest.XXXXXX")
	{
		printf '# a5-fprintd-dev.sh install manifest: what undo removes. Do not edit.\n'
		for p in "${MORDER[@]}"; do printf '%s\t%s\t%s\n' "${MTYPE[$p]}" "$p" "${MVAL[$p]}"; done
	} >"$tmp"
	chmod 0644 "$tmp"
	mv -f -- "$tmp" "$MANIFEST"
}

# ---------------------------------------------------------------- staged copies
# Install SRC as DST: copy it to a stage that mktemp creates 0600 in DST's root-owned dir,
# run CHECK on that copy (nobody else can change it, unlike SRC), then chmod, chown and
# rename it into place (atomic). Sets STAGED_SHA to the sha256 of what was installed.
STAGE=""
STAGED_SHA=""
trap '[ -z "$STAGE" ] || rm -f -- "$STAGE"' EXIT
install_staged() {
	local src=$1 dst=$2 mode=$3
	shift 3
	if [ "$dry_run" -eq 1 ]; then
		printf '+ copy %q to a 0600 stage in %q, check the copy, chmod %s, rename it to %q\n' "$src" "${dst%/*}" "$mode" "$dst"
		return 0
	fi
	STAGE=$(mktemp -- "${dst%/*}/.${dst##*/}.a5-new.XXXXXX")
	printf '+ stage %q (0600)\n' "$STAGE"
	act cp -- "$src" "$STAGE"
	"$@" "$STAGE"
	STAGED_SHA=$(sha "$STAGE")
	[ -n "$TR" ] || act chown root:root -- "$STAGE"
	act chmod "$mode" -- "$STAGE"
	act mv -f -T -- "$STAGE" "$dst"
	STAGE=""
}

# ---------------------------------------------------------------- systemd
systemd_show() { "${SYSTEMCTL[@]}" show "$UNIT" -p Environment,MainPID,ActiveState 2>&1 || true; }
main_pid() { "${SYSTEMCTL[@]}" show "$UNIT" -p MainPID --value 2>/dev/null || echo 0; }

# Paths of libfprint-2.so* that a process maps, " (deleted)" kept when the file was replaced.
mapped_libfprint() {
	local maps=$PROC/$1/maps
	[ -r "$maps" ] || return 1
	awk '$6 ~ /\/libfprint-2\.so/ { p = $6; for (i = 7; i <= NF; i++) p = p " " $i; print p }' "$maps" | sort -u
}

# ---------------------------------------------------------------- install
# The checks each installed file must pass: run on the source in the preflight (early,
# clear failures), and again on the root-owned staged copy that is actually installed.
BUILD_HINT=""
check_lib() {
	local f=$1 soname missing
	# Not an ELF file: readelf fails, and under pipefail that must not end the script silently.
	soname=$(readelf -dW -- "$f" 2>/dev/null | sed -n 's/.*(SONAME).*\[\(.*\)\].*/\1/p') || soname=""
	[ "$soname" = "$SONAME" ] || fail "$f has SONAME '${soname:-none}', expected $SONAME"
	# No `grep -q` at the end of these pipes: it exits at the first match, the writer then
	# dies of SIGPIPE on a large file, and pipefail turns a match into a failure.
	! readelf -dW -- "$f" | grep -E '\((RPATH|RUNPATH)\)' >/dev/null ||
		fail "$f carries an RPATH/RUNPATH; a library fprintd loads as root must not search other paths" "$BUILD_HINT"
	LC_ALL=C strings -a -- "$f" | grep -xF "$SYS_DATA_DIR" >/dev/null ||
		fail "$f was not configured with synaptics0078_data_dir=$SYS_DATA_DIR (fprintd only reads its StateDirectory)" \
			"cd $FORK && meson configure build -Dsynaptics0078_data_dir=$SYS_DATA_DIR && meson compile -C build"
	LC_ALL=C grep -qaF "libfprint-$DRIVER" -- "$f" ||
		fail "driver $DRIVER is not built into $f" "$BUILD_HINT"
	if [ -f "$STOCK_LIB" ]; then
		missing=$(comm -23 <(nm -D --defined-only -- "$STOCK_LIB" | awk '{print $3}' | sort -u) \
			<(nm -D --defined-only -- "$f" | awk '{print $3}' | sort -u))
		[ -z "$missing" ] || fail "$f lacks symbols of the packaged $SONAME: $(echo "$missing" | paste -sd' ')"
	fi
}
check_module() {
	local f=$1 got
	[ "$(fsize "$f")" = "$MODULE_SIZE" ] || fail "$f is $(fsize "$f") bytes, expected $MODULE_SIZE"
	got=$(sha "$f")
	[ "$got" = "$MODULE_SHA256" ] || fail "$f has sha256 $got, expected $MODULE_SHA256"
}
PROGRAM_SHA=""
check_program() {
	local f=$1
	[ "$(fsize "$f")" = "$PROGRAM_SIZE" ] || fail "$f is $(fsize "$f") bytes, expected $PROGRAM_SIZE"
	[ "$(head -c1 -- "$f" | od -An -tx1 | tr -d ' \n')" = 02 ] || fail "$f does not start with the 0x02 opcode"
	# The copy must be the program the preflight checked (and compared with an installed one).
	[ -z "$PROGRAM_SHA" ] || [ "$(sha "$f")" = "$PROGRAM_SHA" ] ||
		fail "$f differs from the program checked in the preflight (changed while copied?)" "run install again"
}

cmd_install() {
	need_root
	echo "A5 fprintd dev install: fork $FORK, data from $DATA_SRC${TR:+, TEST ROOT $TR}"
	[ "$dry_run" -eq 0 ] || echo "dry run: nothing is changed"
	echo "preflight:"
	local tool
	for tool in readelf nm strings sha256sum install; do
		command -v "$tool" >/dev/null || fail "$tool not found" "install binutils/coreutils"
	done

	# 1. The fork's library: a regular file, SONAME libfprint-2.so.2, the driver and the data dir
	#    fprintd needs compiled in, no RPATH/RUNPATH, and the stock ABI (fprintd stays unchanged).
	local src=$FORK/build/libfprint/$LIBFILE
	BUILD_HINT="build the fork: cd $FORK && meson setup build -Dprefix=/usr/local && meson compile -C build (or set LIBFPRINT_FORK)"
	[ -e "$src" ] || [ -L "$src" ] || fail "no fork build at $src" "$BUILD_HINT"
	[ ! -L "$src" ] || fail "$src is a symlink; expected the real library file"
	[ -f "$src" ] || fail "$src is not a regular file"
	check_lib "$src"
	if [ -f "$STOCK_LIB" ]; then
		ok "exports every symbol of the packaged $STOCK_LIB"
	else
		note "no $STOCK_LIB to compare exported symbols with"
	fi
	local src_sha
	src_sha=$(sha "$src")
	ok "fork lib: $src ($SONAME, driver $DRIVER, data dir $SYS_DATA_DIR, sha256 ${src_sha:0:16}...)"

	# 2. The data files: module.bin (size, pinned sha256) and exactly one program-<serial>.bin.
	local data_hint="create them (from $ROOT): proto/.venv/bin/python tools/synaptics0078_data.py"
	[ -d "$DATA_SRC" ] || fail "no data directory $DATA_SRC" "$data_hint"
	local module=$DATA_SRC/module.bin
	[ -e "$module" ] || fail "no $module" "$data_hint"
	[ ! -L "$module" ] && [ -f "$module" ] || fail "$module is not a regular file"
	check_module "$module"
	local module_sha=$MODULE_SHA256
	local programs
	shopt -s nullglob
	programs=("$DATA_SRC"/program-*.bin)
	shopt -u nullglob
	[ ${#programs[@]} -eq 1 ] || fail "expected exactly one program-<serial>.bin in $DATA_SRC, found ${#programs[@]}" "$data_hint"
	local program=${programs[0]} pname
	pname=$(basename -- "$program")
	[[ $pname =~ ^program-[0-9a-f]{12}\.bin$ ]] || fail "$pname is not named program-<12 lowercase hex>.bin"
	[ ! -L "$program" ] && [ -f "$program" ] || fail "$program is not a regular file"
	check_program "$program"
	local program_sha
	program_sha=$(sha "$program")
	PROGRAM_SHA=$program_sha
	ok "data: module.bin ($MODULE_SIZE B, sha256 ok), $pname ($PROGRAM_SIZE B, opcode 0x02)"

	# 3. The targets: real directories of root, files we created or identical ones, no package paths.
	[ -d "$TR/usr/local/lib" ] || fail "$TR/usr/local/lib does not exist"
	[ -d "$TR/etc/systemd/system" ] || fail "$TR/etc/systemd/system does not exist"
	[ -d "$TR/var/lib" ] || fail "$TR/var/lib does not exist"
	local d
	for d in "$LIBDIR" "$STATE_DIR" "$DATA_DIR" "$DROPIN_DIR"; do check_dir "$d"; done
	if [ -d "$LIBDIR" ]; then
		[ -f "$MANIFEST" ] || fail "$LIBDIR exists but has no manifest: not created by this script, refusing to touch it" \
			"look at it and remove it yourself if it is a leftover"
	fi
	manifest_load
	check_file "$LIB"
	if [ -e "$LIB" ] && [ "${MTYPE[$LIB]:-}" != file ]; then fail "$LIB exists and is not recorded in the manifest"; fi
	if [ -L "$LINK" ]; then
		[ "$(readlink -- "$LINK")" = "$LIBFILE" ] || fail "$LINK points to $(readlink -- "$LINK"), not $LIBFILE"
	elif [ -e "$LINK" ]; then
		fail "$LINK exists and is not a symlink"
	fi
	local f
	for f in "$DATA_DIR/module.bin:$module_sha" "$DATA_DIR/$pname:$program_sha"; do
		local path=${f%:*} want=${f##*:}
		check_file "$path"
		if [ -e "$path" ] && [ "$(sha "$path")" != "$want" ]; then
			fail "$path exists and differs from $DATA_SRC/$(basename -- "$path"); refusing to overwrite it" \
				"compare them; if the installed one is stale, remove it (or run undo if install created it) and install again"
		fi
	done
	if [ -d "$DATA_DIR" ] && (($(stat -c '%#a' -- "$DATA_DIR") & 8#077)); then
		warn "$DATA_DIR is mode $(stat -c %a -- "$DATA_DIR"), not 0700 (left as is: install did not create it)"
	fi
	check_file "$DROPIN"
	if [ -e "$DROPIN" ] && [ "$(cat -- "$DROPIN")" != "${DROPIN_TEXT%$'\n'}" ] && [ "${MTYPE[$DROPIN]:-}" != file ]; then
		fail "$DROPIN exists with other content and was not written by this script"
	fi
	if command -v "${PACMAN[0]}" >/dev/null; then
		for f in "$LIBDIR" "$LIB" "$LINK" "$MANIFEST" "$DATA_DIR" "$DATA_DIR/module.bin" "$DATA_DIR/$pname" "$DROPIN_DIR" "$DROPIN"; do
			if [ -e "$f" ] || [ -L "$f" ]; then
				! "${PACMAN[@]}" -Qo -- "$f" >/dev/null 2>&1 || fail "$f belongs to a package; refusing to write it"
			fi
		done
		ok "no target path belongs to a package"
	else
		note "pacman not found: package-ownership guard skipped"
	fi
	ok "targets: $LIBDIR, $DATA_DIR, $DROPIN"

	echo "install:"
	local changed=0 dropin_changed=0
	# The lib dir first: it holds the manifest that records everything after it.
	if [ -d "$LIBDIR" ]; then
		already "present: $LIBDIR"
	else
		act install -d -m 0755 "${OWN[@]}" -- "$LIBDIR"
		manifest_add dir "$LIBDIR"
		changed=1
	fi
	# The library: the checks run again on the staged copy (the source is the user's).
	if [ -f "$LIB" ] && [ "$(sha "$LIB")" = "$src_sha" ]; then
		already "installed: $LIB (same as the fork build)"
		[ "${MTYPE[$LIB]:-}" = file ] || manifest_add file "$LIB" "$src_sha"
	else
		install_staged "$src" "$LIB" 0644 check_lib
		[ "$dry_run" -eq 1 ] || manifest_add file "$LIB" "$STAGED_SHA"
		changed=1
	fi
	if [ -L "$LINK" ]; then
		already "linked: $LINK -> $LIBFILE"
		[ "${MTYPE[$LINK]:-}" = link ] || manifest_add link "$LINK" "$LIBFILE"
	else
		act ln -s -T -- "$LIBFILE" "$LINK"
		manifest_add link "$LINK" "$LIBFILE"
		changed=1
	fi

	# The data files: the driver's directory under fprintd's StateDirectory, root only.
	if [ -d "$STATE_DIR" ]; then
		already "present: $STATE_DIR"
	else
		act install -d -m 0700 "${OWN[@]}" -- "$STATE_DIR"
		manifest_add dir "$STATE_DIR"
	fi
	if [ -d "$DATA_DIR" ]; then
		already "present: $DATA_DIR"
	else
		act install -d -m 0700 "${OWN[@]}" -- "$DATA_DIR"
		manifest_add dir "$DATA_DIR"
		changed=1
	fi
	for f in "$module:check_module" "$program:check_program"; do
		local s=${f%:*} check=${f##*:}
		local dst
		dst=$DATA_DIR/$(basename -- "$s")
		if [ -f "$dst" ]; then
			already "present: $dst (identical)"
			continue
		fi
		install_staged "$s" "$dst" 0600 "$check"
		[ "$dry_run" -eq 1 ] || manifest_add file "$dst" "$STAGED_SHA"
		changed=1
	done

	# The drop-in last, so fprintd never points at a lib that is not there yet.
	if [ -d "$DROPIN_DIR" ]; then
		already "present: $DROPIN_DIR"
	else
		act install -d -m 0755 "${OWN[@]}" -- "$DROPIN_DIR"
		manifest_add dir "$DROPIN_DIR"
	fi
	if [ -f "$DROPIN" ] && [ "$(cat -- "$DROPIN")" = "${DROPIN_TEXT%$'\n'}" ]; then
		already "written: $DROPIN"
	else
		echo "+ write $DROPIN (mode 0644, through a 0600 stage in $DROPIN_DIR):"
		printf '%s' "$DROPIN_TEXT" | sed 's/^/    | /'
		if [ "$dry_run" -eq 0 ]; then
			STAGE=$(mktemp -- "$DROPIN_DIR/.libfprint-0078-dev.conf.a5-new.XXXXXX")
			printf '%s' "$DROPIN_TEXT" >"$STAGE"
			[ -n "$TR" ] || chown root:root -- "$STAGE"
			chmod 0644 -- "$STAGE"
			act mv -f -T -- "$STAGE" "$DROPIN"
			STAGE=""
			manifest_add file "$DROPIN" "$(sha "$DROPIN")"
		fi
		changed=1
		dropin_changed=1
	fi

	echo "fprintd:"
	if [ "$changed" -eq 0 ]; then
		already "installed: nothing changed, fprintd not restarted"
	else
		[ "$dropin_changed" -eq 0 ] || act "${SYSTEMCTL[@]}" daemon-reload
		# Restarts a running fprintd only; otherwise D-Bus starts it on demand with the drop-in.
		act "${SYSTEMCTL[@]}" try-restart "$UNIT"
		note "fprintd is D-Bus activated: if it was not running, the next client (fprintd-list, a lock screen) starts it with the fork"
	fi
	[ "$dry_run" -eq 1 ] || systemd_show | sed 's/^/  /'
	echo "next: $(basename -- "$SELF") status"
}

# ---------------------------------------------------------------- undo
prints_dirs() {
	# Enrolled prints of THIS driver: STATE_DIR/<user>/synaptics0078. Never the data dir
	# (STATE_DIR/synaptics0078, which fprintd lists as a "user"), never other drivers.
	local d user
	shopt -s nullglob
	for d in "$STATE_DIR"/*/"$DRIVER"; do
		user=$(basename -- "$(dirname -- "$d")")
		[ "$user" != "$DRIVER" ] || continue
		if [ -L "$d" ] || [ -L "$(dirname -- "$d")" ] || [ ! -d "$d" ]; then
			warn "skipping $d: not a real directory"
			continue
		fi
		printf '%s\n' "$d"
	done
	shopt -u nullglob
}

remove_recorded() {
	# Remove one manifest entry if it is still what install created. Returns 0 when it is
	# gone, 1 when it was kept (changed since install, or cannot be removed), 2 when a
	# directory is left in place because it is not empty. Only 0 drops the entry: the
	# manifest keeps the rest, so undo can be run again.
	# Called in an `||` context, where set -e is off: every step checks its own status.
	local p=$1 t=${MTYPE[$1]} v=${MVAL[$1]}
	if ! manifest_path_ok "$t" "$p"; then
		warn "kept $p: not a path install writes"
		return 1
	fi
	case $t in
	file)
		if [ ! -e "$p" ] && [ ! -L "$p" ]; then
			already "removed: $p"
		elif [ -L "$p" ] || [ ! -f "$p" ] || [ "$(sha "$p")" != "$v" ]; then
			warn "kept $p: it changed since install (sha256 differs); remove it yourself if you want it gone"
			return 1
		else
			act rm -f -- "$p" || { warn "kept $p: rm failed" && return 1; }
		fi
		;;
	link)
		if [ ! -L "$p" ]; then
			if [ -e "$p" ]; then
				warn "kept $p: no longer a symlink"
				return 1
			fi
			already "removed: $p"
		elif [ "$(readlink -- "$p")" != "$v" ]; then
			warn "kept $p: it points elsewhere now"
			return 1
		else
			act rm -f -- "$p" || { warn "kept $p: rm failed" && return 1; }
		fi
		;;
	dir)
		if [ ! -e "$p" ] && [ ! -L "$p" ]; then
			already "removed: $p"
		elif [ -L "$p" ] || [ ! -d "$p" ]; then
			warn "kept $p: no longer a directory"
			return 1
		elif [ "$dry_run" -eq 1 ]; then
			# What a real run removed before this point is still there: show the plan.
			[ "$p" != "$LIBDIR" ] || act rm -f -- "$MANIFEST"
			act rmdir -- "$p"
		elif [ -n "$(find "$p" -mindepth 1 -maxdepth 1 ! -path "$MANIFEST" -print -quit)" ]; then
			note "left $p in place: it holds files install did not create ($(find "$p" -mindepth 1 -maxdepth 1 ! -path "$MANIFEST" -printf '%f ' | head -c 200))"
			return 2
		else
			if [ "$p" = "$LIBDIR" ] && [ -e "$MANIFEST" ]; then
				act rm -f -- "$MANIFEST" || { warn "kept $MANIFEST: rm failed" && return 1; }
			fi
			act rmdir -- "$p" || { warn "kept $p: rmdir failed" && return 1; }
		fi
		;;
	esac
	return 0
}

# Undo one manifest entry; keep its record unless it is gone. Sets kept/left of cmd_undo.
undo_entry() {
	local rc=0
	remove_recorded "$1" || rc=$?
	case $rc in
	0) manifest_drop "$1" ;;
	2) left=1 ;;
	*) kept=1 ;;
	esac
}

cmd_undo() {
	need_root
	echo "A5 fprintd dev undo${TR:+ (TEST ROOT $TR)}"
	[ "$dry_run" -eq 0 ] || echo "dry run: nothing is changed"
	check_dir "$LIBDIR"
	manifest_load
	if [ ${#MORDER[@]} -eq 0 ]; then
		already "undone: no manifest at $MANIFEST"
		[ ! -e "$DROPIN" ] || warn "$DROPIN exists but no manifest records it: not removed (remove it yourself if it is ours)"
	fi

	# 1. The drop-in first: from here on, a D-Bus activated fprintd loads the packaged lib.
	local reload=0 kept=0 left=0
	if [ "${MTYPE[$DROPIN]:-}" = file ]; then
		undo_entry "$DROPIN"
		reload=1
	fi
	[ "$reload" -eq 0 ] || act "${SYSTEMCTL[@]}" daemon-reload

	# 2. Enrolled prints of this driver: listed, deleted only on an explicit yes.
	local dirs=() d files=() answer=no
	while IFS= read -r d; do [ -z "$d" ] || dirs+=("$d"); done < <(prints_dirs)
	if [ ${#dirs[@]} -gt 0 ]; then
		for d in "${dirs[@]}"; do
			while IFS= read -r -d '' f; do files+=("$f"); done < <(find "$d" -type f -print0)
		done
		echo "enrolled prints of the $DRIVER driver (${#files[@]} file(s)):"
		for d in "${dirs[@]}"; do echo "  $d/"; done
		for f in "${files[@]}"; do echo "    $f"; done
		if [ -n "$prints_answer" ]; then
			answer=$prints_answer
			echo "prints: $answer (from the command line)"
		elif [ "$dry_run" -eq 1 ]; then
			echo "dry run: would ask 'Delete them? [y/N]' (default no)"
		else
			local reply=""
			printf 'Delete them? They are useless with the packaged libfprint. [y/N] '
			read -r reply || {
				reply=""
				echo
			}
			case $reply in [yY] | [yY][eE][sS]) answer=yes ;; *) answer=no ;; esac
		fi
		[ "$answer" = yes ] || echo "prints kept"
	else
		already "none: no enrolled prints of the $DRIVER driver"
	fi

	# 3. Stop fprintd before deleting anything it may have mapped or be writing.
	local stopped=0
	if [ ${#MORDER[@]} -gt 0 ] || [ "$answer" = yes ]; then
		act "${SYSTEMCTL[@]}" stop "$UNIT"
		stopped=1
	fi
	if [ "$answer" = yes ]; then
		for d in "${dirs[@]}"; do act rm -r --one-file-system -- "$d"; done
	fi

	# 4. Everything else install created: files and links newest first (the drop-in was
	#    step 1), then directories, deepest first. The lib dir and its manifest go last.
	local i p
	for ((i = ${#MORDER[@]} - 1; i >= 0; i--)); do
		p=${MORDER[$i]}
		[ "${MTYPE[$p]}" != dir ] && [ "$p" != "$DROPIN" ] || continue
		undo_entry "$p"
	done
	while IFS= read -r p; do
		[ -n "$p" ] && [ "$p" != "$LIBDIR" ] || continue
		undo_entry "$p"
	done < <(for p in "${MORDER[@]}"; do [ "${MTYPE[$p]}" != dir ] || printf '%d\t%s\n' "${#p}" "$p"; done | sort -rn | cut -f2-)
	if [ -n "${MTYPE[$LIBDIR]:-}" ] && [ "$kept" -eq 0 ] && [ "$left" -eq 0 ]; then
		undo_entry "$LIBDIR"
	fi
	if [ ${#MORDER[@]} -gt 0 ]; then
		# Something is left: the manifest lists exactly that, for the next undo.
		note "kept $LIBDIR and its manifest: it lists what is left ($(printf '%s ' "${MORDER[@]}" | head -c 300))"
		manifest_save
	elif [ "$dry_run" -eq 0 ] && [ -e "$MANIFEST" ]; then
		act rm -f -- "$MANIFEST"
	fi
	if [ "$stopped" -eq 1 ]; then
		note "fprintd is stopped; D-Bus starts it on demand with the packaged libfprint (e.g. fprintd-list \"\$USER\")"
	fi
	if [ "$kept" -eq 1 ] || [ "$left" -eq 1 ]; then
		echo "undo incomplete: see the WARN and note lines above; once that is sorted out, run undo again"
		exit 1
	fi
	[ "$stopped" -eq 0 ] || echo "undo done"
}

# ---------------------------------------------------------------- status
cmd_status() {
	local user=${SUDO_USER:-${USER:-$(id -un)}}
	echo "A5 fprintd dev status (as $(id -un))${TR:+ (TEST ROOT $TR)}"
	echo "drop-in: $DROPIN"
	if [ -f "$DROPIN" ] && [ -r "$DROPIN" ]; then
		sed 's/^/    | /' -- "$DROPIN"
	elif [ -e "$DROPIN" ]; then
		echo "  present but not readable as $(id -un)"
	else
		echo "  absent: fprintd uses the packaged libfprint"
	fi

	echo "fprintd ($UNIT):"
	systemd_show | sed 's/^/  /'

	echo "fprintd-list $user:"
	local out rc=0
	out=$("${FPRINTD_LIST[@]}" "$user" 2>&1) || rc=$?
	printf '%s\n' "$out" | sed 's/^/    /'
	[ "$rc" -eq 0 ] || echo "  fprintd-list exited $rc"

	echo "libfprint mapped by fprintd:"
	local pid
	pid=$(main_pid)
	if ! [[ $pid =~ ^[0-9]+$ ]] || [ "$pid" -eq 0 ]; then
		echo "  fprintd is not running; D-Bus starts it on demand: run 'fprintd-list $user', then status again"
	elif ! mapped_libfprint "$pid" >/dev/null 2>&1; then
		echo "  cannot read $PROC/$pid/maps as $(id -un): run status with sudo for this part"
	else
		local maps
		maps=$(mapped_libfprint "$pid")
		if [ -z "$maps" ]; then
			echo "  pid $pid maps no libfprint-2.so"
		fi
		while IFS= read -r p; do
			[ -n "$p" ] || continue
			case $p in
			*" (deleted)") echo "  $p  <- replaced since fprintd started: restart it (sudo systemctl restart fprintd)" ;;
			"$SYS_LIBDIR"/*) echo "  $p  (the fork, from this dev install)" ;;
			*) echo "  $p  (not the dev install)" ;;
			esac
		done <<<"$maps"
	fi

	echo "installed lib: $LIB"
	if [ -f "$LIB" ]; then
		local lsha fsha="" src=$FORK/build/libfprint/$LIBFILE
		lsha=$(sha "$LIB")
		[ ! -r "$src" ] || fsha=$(sha "$src")
		echo "  sha256 $lsha"
		if [ -z "$fsha" ]; then
			echo "  fork build $src not readable: cannot compare"
		elif [ "$fsha" = "$lsha" ]; then
			echo "  same as the fork build $src"
		else
			echo "  differs from the fork build $src (rebuilt since install?): run install again"
		fi
		if [ -L "$LINK" ]; then echo "  $SONAME -> $(readlink -- "$LINK")"; else echo "  WARN $LINK missing"; fi
		if [ -f "$MANIFEST" ]; then echo "  manifest: $MANIFEST"; else echo "  WARN no manifest"; fi
	else
		echo "  absent"
	fi

	echo "data files: $DATA_DIR"
	if [ ! -x "$STATE_DIR" ] && [ -e "$STATE_DIR" ]; then
		echo "  cannot look into $STATE_DIR as $(id -un) (mode 0700, root only): run status with sudo for this part"
	elif [ ! -d "$DATA_DIR" ]; then
		echo "  absent"
	else
		echo "  dir mode $(stat -c '%a %U:%G' -- "$DATA_DIR")"
		local f found=0
		shopt -s nullglob
		for f in "$DATA_DIR"/module.bin "$DATA_DIR"/program-*.bin; do
			found=1
			local line
			line="$(basename -- "$f"): $(stat -c '%s B, mode %a %U:%G' -- "$f")"
			if [ "$(basename -- "$f")" = module.bin ]; then
				if [ -r "$f" ] && [ "$(sha "$f")" = "$MODULE_SHA256" ]; then
					line+=", sha256 ok"
				elif [ -r "$f" ]; then
					line+=", sha256 MISMATCH"
				fi
			fi
			echo "  $line"
		done
		shopt -u nullglob
		[ "$found" -eq 1 ] || echo "  no module.bin or program-*.bin"
	fi
}

case $cmd in
install) cmd_install ;;
undo) cmd_undo ;;
status) cmd_status ;;
esac
