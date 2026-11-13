"""A changeset's worktrees hard-link the project's installed dependencies (.undine/deps) instead of copying them: the
same inodes, still read-only, the marker a file of its own; copies where linking is refused (UNDINE_STORE_LINK=copy,
another file system); a dependency's staging and old directories left out."""
import errno
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.store import cas, deps  # noqa: E402
import test_changeset as tc  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

SYM = "v {}\nK {type=subcircuit\nformat=\"@name @pinlist @symname\"\ntemplate=\"name=x1\"}\n"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, check=True,
                          capture_output=True, text=True).stdout.strip()


def ip_repo(d: Path) -> Path:
    """A git repository with a one-symbol library in lib/."""
    (d / "lib").mkdir(parents=True)
    (d / "lib" / "buf.sym").write_text(SYM)
    (d / "lib" / "sub").mkdir()
    (d / "lib" / "sub" / "notes.txt").write_text("a file one level down\n")
    git(d, "init", "-q")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "v1")
    return d


def with_dep(tmp_path: Path, in_repo: bool = False):
    """The changeset test project with the dependency `ip` installed; with in_repo, the store's root is a git
    repository holding the project in its subdirectory p/ (repo.prefix)."""
    root, srv, cs = tc.setup()
    deps.add(srv.project("p"), "ip", str(ip_repo(tmp_path / "ip")), None, "lib")
    if in_repo:
        from undine.__main__ import git_setup
        git(root, "init", "-q")
        git_setup(root / "p", say=None)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "projects")
    return root, srv, cs


def dep_files(prj_path: Path) -> dict[str, os.stat_result]:
    d = deps.dep_dir(prj_path, "ip")
    return {f.relative_to(d).as_posix(): f.stat() for f in sorted(d.rglob("*")) if f.is_file()}


def opened(srv, cs, root):
    meta = cs.create("p", "t", "r", "agent:t")
    return meta, srv.project(meta["preview"]).path, srv.project(meta["baseline"]).path


@pytest.mark.parametrize("in_repo", [False, True], ids=["own-repo", "subdirectory"])
def test_worktrees_link_the_dependency(tmp_path, in_repo):
    root, srv, cs = with_dep(tmp_path, in_repo)
    have = dep_files(srv.project("p").path)
    assert set(have) == {deps.MARKER, "buf.sym", "sub/notes.txt"}
    meta, work, base = opened(srv, cs, root)
    if in_repo:
        assert meta["git"]["prefix"] == "p" and work.name == "p" and base.name == "p"
    for side in (work, base):
        got = dep_files(side)
        assert set(got) == set(have)
        for rel in ("buf.sym", "sub/notes.txt"):
            assert got[rel].st_ino == have[rel].st_ino                # the same file: no disk
            assert not stat.S_IMODE(got[rel].st_mode) & 0o222        # still read-only
        assert got[deps.MARKER].st_ino != have[deps.MARKER].st_ino    # the marker: a file of its own
    assert "ip" in srv.project(meta["preview"]).libraries()
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


@pytest.mark.parametrize("how", ["UNDINE_STORE_LINK", "EXDEV"])
def test_copies_where_linking_is_refused(tmp_path, monkeypatch, how):
    root, srv, cs = with_dep(tmp_path)
    have = dep_files(srv.project("p").path)
    if how == "UNDINE_STORE_LINK":
        monkeypatch.setenv("UNDINE_STORE_LINK", "copy")
    else:
        def no_link(*a, **kw):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        monkeypatch.setattr(cas.os, "link", no_link)
    meta, work, _ = opened(srv, cs, root)
    got = dep_files(work)
    assert set(got) == set(have)
    for rel in ("buf.sym", "sub/notes.txt"):
        assert got[rel].st_ino != have[rel].st_ino                    # a copy
        assert not stat.S_IMODE(got[rel].st_mode) & 0o222            # read-only too
    lib = srv.project(meta["preview"]).libraries()["ip"]
    assert (lib.path / "buf.sym").read_text() == SYM                  # the preview still sees the dependency
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


def test_staging_and_old_directories_are_not_copied(tmp_path):
    root, srv, cs = with_dep(tmp_path)
    top = srv.project("p").path / deps.DEPS
    for leftover in (".ip.new", ".ip.0123456789ab.new", "ip.old"):  # an install cut short, or under way
        (top / leftover).mkdir()
        (top / leftover / "buf.sym").write_text(SYM)
    _, work, base = opened(srv, cs, root)
    for side in (work, base):
        assert sorted(p.name for p in (side / deps.DEPS).iterdir()) == ["ip"]
    shutil.rmtree(root)
