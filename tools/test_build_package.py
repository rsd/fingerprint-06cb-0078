# SPDX-License-Identifier: MIT
"""Offline tests of the in-tree PKGBUILD and of tools/dev/build-package.sh.

The in-tree PKGBUILD is the AUR package's except its b2sums: it must name only public
https sources (the fork by commit, this project by release tag), carry a static pkgver
and no pkgver(), and no local path.

build-package.sh runs here with --prepare-only only: no makepkg build. Its fork is a tiny
git repo made in tmp_path and its build root is in tmp_path; the project snapshot is
taken from this tree (read only), or from a minimal copy of the project made in tmp_path
where a test needs a path with spaces, a dirty index or its own pinned commit. Git runs
with a fixed identity and no global or system config.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "dev" / "build-package.sh"
PKGNAME = "libfprint-sigfm-0078"
PKGBUILD = REPO / "packaging" / "arch" / PKGNAME / "PKGBUILD"
HEX40 = re.compile(r"[0-9a-f]{40}")
DEV_PKGREL = re.compile(r"0\.[0-9]{14}")
MESON_BUILD = "project('libfprint', [ 'c', 'cpp' ],\n    version: '1.94.100',\n)\n"


def git_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(tmp_path / "home"),
        LC_ALL="C",
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def var(text: str, name: str) -> str:
    m = re.search(rf"^{name}=(.*)$", text, re.MULTILINE)
    assert m, f"no {name}= in the PKGBUILD"
    return m.group(1)


def b2sums(text: str) -> list[str]:
    m = re.search(r"^b2sums=\(([^)]*)\)", text, re.MULTILINE)
    assert m, "no b2sums=(...) in the PKGBUILD"
    return re.findall(r"'([^']*)'", m.group(1))


def sources(text: str) -> list[str]:
    m = re.search(r"^source=\(([^)]*)\)", text, re.MULTILINE)
    assert m, "no source=(...) in the PKGBUILD"
    return re.findall(r'"([^"]*)"', m.group(1))


# --- the in-tree PKGBUILD -------------------------------------------------------------


def test_pkgbuild_names_no_local_path():
    text = PKGBUILD.read_text()
    assert "file://" not in text
    assert "/home/" not in text


def test_pkgbuild_sources_are_public_and_pinned():
    text = PKGBUILD.read_text()
    assert var(text, "_repo").startswith("https://")
    assert var(text, "_toolrepo").startswith("https://")
    assert var(text, "_tooltag") == "v0.1.0"
    assert re.search(r"^_commit=[0-9a-f]{40}$", text, re.MULTILINE)
    assert sources(text) == [
        "libfprint::git+${_repo}#commit=${_commit}",
        "fp0078::git+${_toolrepo}#tag=${_tooltag}",
    ]


def test_pkgbuild_version_is_static():
    text = PKGBUILD.read_text()
    assert not re.search(r"^\s*pkgver\s*\(\s*\)", text, re.MULTILINE)
    assert re.fullmatch(r"\d+(\.\d+)*\.r\d+\.g[0-9a-f]{7,}", var(text, "pkgver"))
    assert re.fullmatch(r"\d+", var(text, "pkgrel"))
    # the pkgver names the pinned fork commit
    assert var(text, "_commit").startswith(var(text, "pkgver").rsplit(".g", 1)[1])


def test_pkgbuild_sums_are_skip():
    assert b2sums(PKGBUILD.read_text()) == ["SKIP", "SKIP"]


# --- build-package.sh --prepare-only ---------------------------------------------------


def make_fork(tmp_path: Path, name: str = "fork", tag: str | None = "v1.94.100") -> Path:
    """A one-commit fork with the real meson.build layout, tagged like upstream."""
    env = git_env(tmp_path)
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    (path / "meson.build").write_text(MESON_BUILD)
    subprocess.run(["git", "-C", str(path), "add", "meson.build"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fork"], check=True, env=env)
    if tag:
        subprocess.run(["git", "-C", str(path), "tag", tag], check=True, env=env)
    return path


@pytest.fixture
def fork(tmp_path):
    return make_fork(tmp_path)


def make_project(tmp_path: Path, name: str = "project", pin: str | None = None) -> Path:
    """A minimal copy of this project: the script, the PKGBUILD, a package file, an ignored
    local/. pin replaces the PKGBUILD's _commit. Left with a modified tracked file and an
    untracked one, so a stray `git status` or `git add` would rewrite its index."""
    env = git_env(tmp_path)
    proj = tmp_path / name
    pkg = proj / "packaging" / "arch" / PKGNAME
    (proj / "tools" / "dev").mkdir(parents=True)
    pkg.mkdir(parents=True)
    (proj / "synaptics0078").mkdir()
    shutil.copy2(SCRIPT, proj / "tools" / "dev" / SCRIPT.name)
    text = PKGBUILD.read_text()
    if pin:
        text = re.sub(r"^_commit=.*$", f"_commit={pin}", text, flags=re.MULTILINE)
    (pkg / "PKGBUILD").write_text(text)
    shutil.copy2(PKGBUILD.parent / f"{PKGNAME}.install", pkg / f"{PKGNAME}.install")
    (proj / "synaptics0078" / "__init__.py").write_text("")
    (proj / ".gitignore").write_text("local/\n")
    subprocess.run(["git", "init", "-q", str(proj)], check=True, env=env)
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "p"], check=True, env=env)
    (proj / "synaptics0078" / "__init__.py").write_text("# modified\n")
    (proj / "synaptics0078" / "new.py").write_text("")
    return proj


def run_script(tmp_path, *args, script: Path = SCRIPT, **env_extra):
    env = git_env(tmp_path)
    env.update(env_extra)
    return subprocess.run(
        [str(script), *args], check=False, env=env, capture_output=True, text=True, timeout=300
    )


def project_script(proj: Path) -> Path:
    return proj / "tools" / "dev" / SCRIPT.name


def index_state(proj: Path) -> tuple[int, str]:
    index = proj / ".git" / "index"
    return index.stat().st_mtime_ns, hashlib.sha256(index.read_bytes()).hexdigest()


def srcinfo(pkgbuild: Path, env: dict[str, str]) -> str:
    info = subprocess.run(
        ["makepkg", "--printsrcinfo"], check=False, cwd=pkgbuild.parent,
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert info.returncode == 0, info.stderr
    return info.stdout


def rev_of(repo: Path, rev: str, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{rev}^{{commit}}"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_prepare_only_rewrites_the_sources(tmp_path, fork):
    root = tmp_path / "build"
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode == 0, r.stderr
    pkgbuild = Path(r.stdout.strip())
    assert pkgbuild == root.resolve() / "pkgbuild" / "PKGBUILD"
    assert (pkgbuild.parent / f"{PKGNAME}.install").is_file()
    text = pkgbuild.read_text()
    env = git_env(tmp_path)

    snap = root.resolve() / "snap"
    assert var(text, "_repo") == f"'file://{fork.resolve()}'"
    assert var(text, "_toolrepo") == f"'file://{snap}'"
    fork_commit = var(text, "_commit")
    assert HEX40.fullmatch(fork_commit)
    assert rev_of(fork, fork_commit, env) == fork_commit == rev_of(fork, "HEAD", env)
    # the dev version: the fork commit, by git describe; a pkgrel below the release's
    assert var(text, "pkgver") == f"1.94.100.r0.g{fork_commit[:7]}"
    assert DEV_PKGREL.fullmatch(var(text, "pkgrel"))
    src = sources(text)
    assert src[0] == "libfprint::git+${_repo}#commit=${_commit}"
    m = re.fullmatch(r"fp0078::git\+\$\{_toolrepo\}#commit=([0-9a-f]+)", src[1])
    assert m and HEX40.fullmatch(m.group(1)), src[1]
    assert rev_of(snap, m.group(1), env) == m.group(1)
    assert "#tag=" not in text
    assert b2sums(text) == ["SKIP", "SKIP"]

    # the snapshot holds this tree's files, and nothing of the build root
    assert (snap / "synaptics0078" / "__init__.py").is_file()
    assert (snap / "packaging" / "arch" / PKGNAME / "synaptics0078-setup").is_file()
    assert not (snap / "local").exists()

    subprocess.run(["bash", "-n", str(pkgbuild)], check=True)

    if shutil.which("makepkg") is None:
        pytest.skip("makepkg is not on PATH: the .SRCINFO check needs it")
    info = srcinfo(pkgbuild, env)
    assert f"source = libfprint::git+file://{fork.resolve()}#commit={fork_commit}" in info
    assert f"source = fp0078::git+file://{snap}#commit={m.group(1)}" in info
    assert f"pkgver = {var(text, 'pkgver')}\n" in info
    assert f"pkgrel = {var(text, 'pkgrel')}\n" in info


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_prepare_only_resolves_fork_rev(tmp_path, fork):
    env = git_env(tmp_path)
    first = rev_of(fork, "HEAD", env)
    subprocess.run(
        ["git", "-C", str(fork), "commit", "-q", "--allow-empty", "-m", "second"],
        check=True, env=env,
    )
    root = tmp_path / "build"
    r = run_script(
        tmp_path, "--prepare-only",
        LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root), FORK_REV="HEAD~1",
    )
    assert r.returncode == 0, r.stderr
    text = Path(r.stdout.strip()).read_text()
    assert var(text, "_commit") == first
    assert var(text, "pkgver") == f"1.94.100.r0.g{first[:7]}"

    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode == 0, r.stderr
    second = rev_of(fork, "HEAD", env)
    assert var(Path(r.stdout.strip()).read_text(), "pkgver") == f"1.94.100.r1.g{second[:7]}"


def test_refuses_a_missing_fork(tmp_path):
    r = run_script(
        tmp_path, "--prepare-only",
        LIBFPRINT_FORK=str(tmp_path / "no-such-fork"), BUILD_ROOT=str(tmp_path / "build"),
    )
    assert r.returncode != 0
    assert "fork" in r.stderr
    assert not (tmp_path / "build" / "pkgbuild").exists()


def test_refuses_a_fork_that_is_not_a_git_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    r = run_script(
        tmp_path, "--prepare-only",
        LIBFPRINT_FORK=str(tmp_path / "plain"), BUILD_ROOT=str(tmp_path / "build"),
    )
    assert r.returncode != 0
    assert "not a git repo" in r.stderr


def test_refuses_an_unknown_fork_rev(tmp_path, fork):
    r = run_script(
        tmp_path, "--prepare-only",
        LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(tmp_path / "build"), FORK_REV="no-such-rev",
    )
    assert r.returncode != 0
    assert "no-such-rev" in r.stderr


def test_refuses_an_unknown_argument(tmp_path):
    r = run_script(tmp_path, "--bogus")
    assert r.returncode != 0
    assert "--bogus" in r.stderr


def test_refuses_a_build_root_in_the_tree_that_git_does_not_ignore(tmp_path, fork):
    root = REPO / "tools" / "build-package-test-root-not-ignored"
    assert not root.exists()
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode != 0
    assert "not ignored" in r.stderr
    assert not root.exists()


# --- paths, the project's git state, hooks --------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_paths_with_spaces_and_sed_metacharacters(tmp_path):
    proj = make_project(tmp_path, "my project")
    fork = make_fork(tmp_path, "the fork & a|b\\c")
    r = run_script(tmp_path, "--prepare-only", script=project_script(proj),
                   LIBFPRINT_FORK=str(fork))
    assert r.returncode == 0, r.stderr
    pkgbuild = Path(r.stdout.strip())
    root = proj.resolve() / "local" / "pkg-dev"
    assert pkgbuild == root / "pkgbuild" / "PKGBUILD"
    text = pkgbuild.read_text()
    assert var(text, "_repo") == f"'file://{fork.resolve()}'"
    assert var(text, "_toolrepo") == f"'file://{root / 'snap'}'"
    # sourcing the PKGBUILD runs nothing and sets the paths whole
    out = subprocess.run(
        ["bash", "-c", 'source "$1" && printf "%s\\n" "$_repo" "$_toolrepo"', "-", str(pkgbuild)],
        check=True, capture_output=True, text=True, cwd=tmp_path,
    ).stdout.splitlines()
    assert out == [f"file://{fork.resolve()}", f"file://{root / 'snap'}"]

    if shutil.which("makepkg") is None:
        pytest.skip("makepkg is not on PATH: the .SRCINFO check needs it")
    info = srcinfo(pkgbuild, git_env(tmp_path))
    fork_commit = var(text, "_commit")
    snap_commit = sources(text)[1].rsplit("=", 1)[1]
    assert f"source = libfprint::git+file://{fork.resolve()}#commit={fork_commit}\n" in info
    assert f"source = fp0078::git+file://{root / 'snap'}#commit={snap_commit}\n" in info


@pytest.mark.parametrize("name", ["it's", "a#b"])
def test_refuses_a_path_the_pkgbuild_cannot_carry(tmp_path, name):
    fork = make_fork(tmp_path, name)
    root = tmp_path / "build"
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode != 0
    assert "cannot go into the PKGBUILD" in r.stderr
    assert not root.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_prepare_only_leaves_the_project_index_alone(tmp_path, fork):
    proj = make_project(tmp_path)
    before = index_state(proj)
    r = run_script(tmp_path, "--prepare-only", script=project_script(proj),
                   LIBFPRINT_FORK=str(fork))
    assert r.returncode == 0, r.stderr
    assert index_state(proj) == before
    # the snapshot has the working tree, dirty and untracked files included
    snap = proj / "local" / "pkg-dev" / "snap"
    assert (snap / "synaptics0078" / "__init__.py").read_text() == "# modified\n"
    assert (snap / "synaptics0078" / "new.py").is_file()


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_ignores_the_callers_git_dir_and_index_file(tmp_path, fork):
    proj = make_project(tmp_path)
    env = git_env(tmp_path)
    head = rev_of(proj, "HEAD", env)
    before = index_state(proj)
    decoy_index = tmp_path / "decoy-index"
    # passed through to the script, unlike git_env's own GIT_* stripping
    r = run_script(
        tmp_path, "--prepare-only", script=project_script(proj),
        LIBFPRINT_FORK=str(fork), GIT_DIR=str(proj / ".git"),
        GIT_INDEX_FILE=str(decoy_index), GIT_WORK_TREE=str(proj),
    )
    assert r.returncode == 0, r.stderr
    assert index_state(proj) == before
    assert rev_of(proj, "HEAD", env) == head
    assert not decoy_index.exists()
    snap = proj / "local" / "pkg-dev" / "snap"
    assert (snap / ".git").is_dir()
    m = re.search(r"#commit=([0-9a-f]{40})", sources(Path(r.stdout.strip()).read_text())[1])
    assert m and rev_of(snap, "HEAD", env) == m.group(1)


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_the_snapshot_runs_none_of_the_users_hooks(tmp_path, fork):
    hooks = tmp_path / "hooks"
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    hooks.mkdir()
    ran = tmp_path / "hook-ran"
    for d, name in ((hooks, "pre-commit"), (hooks, "post-commit"),
                    (template / "hooks", "pre-commit"), (template / "hooks", "post-commit")):
        hook = d / name
        hook.write_text(f"#!/bin/sh\necho {d.name}/{name} >> '{ran}'\nexit 1\n")
        hook.chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\thooksPath = {hooks}\n[init]\n\ttemplateDir = {template}\n")
    root = tmp_path / "build"
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root),
                   GIT_CONFIG_GLOBAL=str(config))
    assert r.returncode == 0, r.stderr
    assert not ran.exists()
    assert not (root / "snap" / ".git" / "hooks").exists()


# --- the dev version -------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_dev_pkgrel_is_below_the_release_and_increases(tmp_path, fork):
    root = tmp_path / "build"
    rels = []
    for _ in range(2):
        r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork),
                       BUILD_ROOT=str(root))
        assert r.returncode == 0, r.stderr
        rels.append(var(Path(r.stdout.strip()).read_text(), "pkgrel"))
        if len(rels) == 1:
            subprocess.run(["sleep", "1.1"], check=True)
    assert all(DEV_PKGREL.fullmatch(rel) for rel in rels), rels
    assert rels[0] < rels[1]
    if shutil.which("vercmp") is None:
        return
    ver = f"1.94.100.r0.g{rev_of(fork, 'HEAD', git_env(tmp_path))[:7]}"
    pairs = ((f"{ver}-{rels[0]}", f"{ver}-{rels[1]}"), (f"{ver}-{rels[1]}", f"{ver}-1"))
    for older, newer in pairs:
        cmp = subprocess.run(["vercmp", older, newer], check=True, capture_output=True, text=True)
        assert cmp.stdout.strip() == "-1", (older, newer)


def test_refuses_an_untagged_fork_commit_that_is_not_the_pinned_one(tmp_path):
    fork = make_fork(tmp_path, tag=None)
    root = tmp_path / "build"
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode != 0
    assert "fetch the fork's tags" in r.stderr
    assert not root.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_an_untagged_pinned_fork_commit_keeps_the_static_pkgver(tmp_path):
    fork = make_fork(tmp_path, tag=None)
    pin = rev_of(fork, "HEAD", git_env(tmp_path))
    proj = make_project(tmp_path, pin=pin)
    r = run_script(tmp_path, "--prepare-only", script=project_script(proj),
                   LIBFPRINT_FORK=str(fork))
    assert r.returncode == 0, r.stderr
    text = Path(r.stdout.strip()).read_text()
    assert var(text, "pkgver") == var(PKGBUILD.read_text(), "pkgver")
    assert var(text, "_commit") == pin
    assert DEV_PKGREL.fullmatch(var(text, "pkgrel"))


# --- the build root --------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_marks_a_new_build_root_and_reuses_it(tmp_path, fork):
    root = tmp_path / "build"
    for _ in range(2):
        r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork),
                       BUILD_ROOT=str(root))
        assert r.returncode == 0, r.stderr
        assert (root / ".build-package-root").is_file()


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses root")
def test_accepts_an_empty_build_root(tmp_path, fork):
    root = tmp_path / "build"
    root.mkdir()
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode == 0, r.stderr
    assert (root / ".build-package-root").is_file()


def test_refuses_a_non_empty_build_root_without_the_mark(tmp_path, fork):
    root = tmp_path / "home"
    (root / "snap").mkdir(parents=True, exist_ok=True)
    keep = root / "snap" / "keep.txt"
    keep.write_text("mine\n")
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode != 0
    assert ".build-package-root" in r.stderr
    assert keep.read_text() == "mine\n"
    assert not (root / ".build-package-root").exists()
    assert not (root / "pkgbuild").exists()


def test_refuses_a_build_root_that_is_a_file(tmp_path, fork):
    root = tmp_path / "build"
    root.write_text("")
    r = run_script(tmp_path, "--prepare-only", LIBFPRINT_FORK=str(fork), BUILD_ROOT=str(root))
    assert r.returncode != 0
    assert "not a directory" in r.stderr
