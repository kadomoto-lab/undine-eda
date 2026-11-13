"""Git sources in the store (store/gitsrc.py): kept bare repositories, the blob tree read from git's objects (lock
format 2) and the archive route (lock format 1) run in a repository without refs."""
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.store import cas, gitsrc  # noqa: E402
from undine.store.gitsrc import GitSourceError  # noqa: E402

SERVER = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _g(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _write(d: Path, files: dict[str, bytes | str]) -> None:
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)


def _repo(d: Path, files: dict[str, bytes | str]) -> Path:
    d.mkdir(parents=True)
    _g(d, "init", "-q", "-b", "main")
    _write(d, files)
    _g(d, "add", "-A")
    _g(d, "commit", "-qm", "1")
    return d


def _commit(d: Path, files: dict[str, bytes | str], msg: str) -> str:
    _write(d, files)
    _g(d, "add", "-A")
    _g(d, "commit", "-qm", msg)
    return _g(d, "rev-parse", "HEAD")


def _global_config(tmp: Path, monkeypatch, text: str) -> None:
    cfg = tmp / "gitconfig"
    cfg.write_text(text)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


def _kept(url: str, *a: str) -> str:
    return subprocess.run(["git", f"--git-dir={gitsrc.repo_path(url)}", *a], check=True, capture_output=True,
                          text=True).stdout.strip()


# phase 0's deps.tree_sha and the depth-1 path of deps._fetch, frozen here as the reference the format 1 `sha` of a
# lock written by phase 0 was computed with


def _phase0_tree_sha(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file() and p.name != cas.MARKER):
        h.update(f"{f.relative_to(d).as_posix()}\0{hashlib.sha256(f.read_bytes()).hexdigest()}\n".encode())
    return h.hexdigest()


def _phase0_sha(src: str, rev: str, sub: str, tmp: Path) -> str:
    repo, staging = tmp / "p0.git", tmp / "p0"
    shutil.rmtree(repo, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*a, cwd=None):
        return subprocess.run(["git", "-c", "protocol.ext.allow=never", *a], cwd=cwd, env=env, check=True,
                              capture_output=True).stdout
    git("init", "-q", "--bare", str(repo))
    git("fetch", "-q", "--depth", "1", "--end-of-options", src, rev, cwd=repo)
    got = git("rev-parse", "FETCH_HEAD^{commit}", cwd=repo).decode().strip()
    data = git("archive", "--format=tar", got, "--", *([sub] if sub else []), cwd=repo)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = []
        for m in tar.getmembers():
            if not (m.isfile() or m.isdir()):
                continue
            rel = Path(m.name)
            if sub:
                rel = rel.relative_to(sub) if rel.is_relative_to(sub) else None
            if rel is None or rel.is_absolute() or ".." in rel.parts or str(rel) == ".":
                continue
            m.name = rel.as_posix()
            members.append(m)
        tar.extractall(staging, members=members, filter="data")
    return _phase0_tree_sha(staging)


# ------------------------------------------------------------------ tree hash


def test_one_commit_hashes_the_same_with_autocrlf_and_export_subst(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src", {
        "lib/crlf.txt": b"one\r\ntwo\r\n", "lib/lf.txt": b"one\ntwo\n", "lib/subst.txt": "id $Format:%H$\n",
        ".gitattributes": "lib/subst.txt export-subst\n"})
    url = str(src)
    c = gitsrc.fetch(url, None, "main")
    assert c == _g(src, "rev-parse", "HEAD")
    plain = gitsrc.blob_tree(url, c, "lib")
    archived = gitsrc.archive_tree(url, c, "lib")
    _global_config(tmp_path, monkeypatch, "[core]\n\tautocrlf = true\n")
    crlf = gitsrc.blob_tree(url, c, "lib")
    archived_crlf = gitsrc.archive_tree(url, c, "lib")

    assert cas.tree_hash(plain) == cas.tree_hash(crlf)                   # the commit's own bytes, on every machine
    assert cas.object_path(plain["subst.txt"][1]).read_bytes() == b"id $Format:%H$\n"
    assert cas.object_path(plain["crlf.txt"][1]).read_bytes() == b"one\r\ntwo\r\n"
    # git archive applies export-subst and the machine's core.autocrlf: why format 2 hashes blobs instead
    assert cas.object_path(archived["subst.txt"][1]).read_bytes() == f"id {c}\n".encode()
    assert cas.archive_sha(archived) != cas.archive_sha(archived_crlf)
    assert cas.object_path(archived_crlf["lf.txt"][1]).read_bytes() == b"one\r\ntwo\r\n"
    assert {m for m, _ in plain.values()} == {"100644"}


def test_modes_enter_the_tree(tmp_path):
    src = _repo(tmp_path / "src", {"run.sh": "#!/bin/sh\n", "data": "x\n"})
    (src / "run.sh").chmod(0o755)
    _g(src, "add", "-A")
    _g(src, "commit", "-qm", "exec")
    c = gitsrc.fetch(str(src), None, "main")
    files = gitsrc.blob_tree(str(src), c, "")
    assert files["run.sh"][0] == "100755" and files["data"][0] == "100644"
    assert all(os.stat(cas.object_path(h)).st_mode & 0o777 == 0o444 for _, h in files.values())


def test_two_versions_share_unchanged_objects(tmp_path, fresh_store):
    src = _repo(tmp_path / "src", {"lib/same.oas": b"\x00cell" * 100, "lib/b.sch": "v1\n", "README": "top\n"})
    _g(src, "tag", "v1")
    _commit(src, {"lib/b.sch": "v2\n"}, "v2")
    _g(src, "tag", "v2")
    url = str(src)
    v1 = gitsrc.blob_tree(url, gitsrc.fetch(url, None, "v1"), "lib")
    v2 = gitsrc.blob_tree(url, gitsrc.fetch(url, None, "v2"), "lib")
    assert sorted(v1) == sorted(v2) == ["b.sch", "same.oas"]                # the subdir only
    assert v1["same.oas"] == v2["same.oas"] and v1["b.sch"] != v2["b.sch"]
    objects = [p for p in (fresh_store / "objects").rglob("*") if p.is_file()]
    assert len(objects) == 3                                                 # same.oas once, b.sch twice
    assert gitsrc.repo_path(url).parent == fresh_store / "git"
    assert len(list((fresh_store / "git").glob("*.git"))) == 1               # one kept repository for both


# ------------------------------------------------------------------ archive route (format 1)


def _history(tmp_path: Path) -> tuple[Path, str, str, str]:
    """c1 (annotated tag v1), c2 (no ref), c3 (main); lib/ has an export-subst file whose expansion depends on the
    refs and history the archiving repository has, a nested install-marker name and a symbolic link."""
    src = _repo(tmp_path / "src", {
        "lib/subst.txt": "d=$Format:%d$\nD=$Format:%D$\ndescribe=$Format:%(describe)$\nH=$Format:%H$\n",
        "lib/cell.sch": "v1\n", "lib/deep/.undine-dep.json": "{}\n", "README": "top\n",
        ".gitattributes": "lib/subst.txt export-subst\n"})
    os.symlink("cell.sch", src / "lib" / "link.sch")
    _g(src, "add", "-A")
    _g(src, "commit", "-qm", "link")
    c1 = _g(src, "rev-parse", "HEAD")
    _g(src, "tag", "-a", "v1", "-m", "v1")
    c2 = _commit(src, {"lib/cell.sch": "v2\n"}, "2")
    c3 = _commit(src, {"lib/cell.sch": "v3\n"}, "3")
    return src, c1, c2, c3


def test_archive_sha_equals_phase0_in_a_kept_repository_with_refs(tmp_path, monkeypatch):
    src, c1, c2, c3 = _history(tmp_path)
    url = str(src)
    assert gitsrc.fetch(url, None, "main") == c3
    # a server that does not serve an unadvertised commit (protocol v0): the all-refs fallback brings every head and
    # tag into the kept repository, and refs/undine/* are there too
    _global_config(tmp_path, monkeypatch, "[protocol]\n\tversion = 0\n")
    assert gitsrc.fetch(url, None, c2, c2) == c2
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    refs = _kept(url, "for-each-ref", "--format=%(refname)")
    assert "refs/tags/v1" in refs and f"refs/undine/{c2}" in refs and f"refs/undine/{c3}" in refs
    # in the kept repository itself, export-subst would see them
    direct = subprocess.run(["git", f"--git-dir={gitsrc.repo_path(url)}", "archive", "--format=tar", c1, "--",
                             "lib/subst.txt"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(direct)) as t:
        assert b"describe=v1" in t.extractfile("lib/subst.txt").read()

    for commit, rev in ((c1, "v1"), (c3, "main")):
        files = gitsrc.archive_tree(url, commit, "lib")
        assert sorted(files) == ["cell.sch", "subst.txt"]           # no link, no nested marker, subdir stripped
        assert cas.archive_sha(files) == _phase0_sha(url, rev, "lib", tmp_path)
        text = cas.object_path(files["subst.txt"][1]).read_bytes()
        assert b"describe=\n" in text and b"v1" not in text
    assert cas.archive_sha(gitsrc.archive_tree(url, c1, "")) == _phase0_sha(url, "v1", "", tmp_path)
    assert not list((cas.tmp_dir()).iterdir())                       # the throwaway repository is gone


def test_archive_refuses_an_empty_result(tmp_path):
    src = _repo(tmp_path / "src", {"lib/a": "a\n", "empty/.undine-dep.json": "{}\n"})
    c = gitsrc.fetch(str(src), None, "main")
    with pytest.raises(GitSourceError, match="nothing at empty"):
        gitsrc.archive_tree(str(src), c, "empty")
    with pytest.raises(GitSourceError, match="did not match"):
        gitsrc.archive_tree(str(src), c, "absent")


# ------------------------------------------------------------------ what blob_tree refuses


@pytest.fixture
def odd_repo(tmp_path):
    lfs = "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 12345\n"
    src = _repo(tmp_path / "src", {
        "ok/a.txt": "a\n", "ok/b.txt": "bb\n", "sym/a.txt": "a\n", "lfs/big.gds": lfs,
        "nested/x/.undine-dep.json": "{}\n", "nested/x/cell.sch": "c\n", "mod/keep.txt": "k\n"})
    os.symlink("a.txt", src / "sym" / "link.txt")
    _g(src, "add", "-A")
    _g(src, "update-index", "--add", "--cacheinfo", f"160000,{_g(src, 'rev-parse', 'HEAD')},mod/sub")
    _g(src, "commit", "-qm", "odd")
    url = str(src)
    return url, gitsrc.fetch(url, None, "main")


def test_blob_tree_refuses_what_cannot_be_installed(odd_repo, monkeypatch):
    url, c = odd_repo
    with pytest.raises(GitSourceError, match=r"link\.txt is a symbolic link"):
        gitsrc.blob_tree(url, c, "sym")
    with pytest.raises(GitSourceError, match=r"sub is a submodule"):
        gitsrc.blob_tree(url, c, "mod")
    with pytest.raises(GitSourceError, match=r"big\.gds is a Git LFS pointer"):
        gitsrc.blob_tree(url, c, "lfs")
    with pytest.raises(GitSourceError, match=r"x/\.undine-dep\.json is named"):
        gitsrc.blob_tree(url, c, "nested")
    with pytest.raises(GitSourceError, match="has no directory 'absent'"):
        gitsrc.blob_tree(url, c, "absent")
    # the archive route skips links and submodules silently, as phase 0 did
    assert sorted(gitsrc.archive_tree(url, c, "sym")) == ["a.txt"]
    assert sorted(gitsrc.archive_tree(url, c, "mod")) == ["keep.txt"]
    assert sorted(gitsrc.blob_tree(url, c, "ok")) == ["a.txt", "b.txt"]


def test_limits_are_checked_before_any_blob_is_read(odd_repo, monkeypatch):
    url, c = odd_repo

    def never(repo):
        raise AssertionError("cat-file started")
    monkeypatch.setattr(gitsrc, "_cat_file", never)
    monkeypatch.setattr(gitsrc, "MAX_FILES", 1)
    with pytest.raises(GitSourceError, match="2 files under ok, more than the 1"):
        gitsrc.blob_tree(url, c, "ok")
    monkeypatch.setattr(gitsrc, "MAX_FILES", 100)
    monkeypatch.setattr(gitsrc, "MAX_BYTES", 4)
    with pytest.raises(GitSourceError, match="5 bytes under ok, more than the 4"):
        gitsrc.blob_tree(url, c, "ok")
    with pytest.raises(GitSourceError, match="symbolic link"):           # modes are checked before blobs too
        gitsrc.blob_tree(url, c, "sym")


def test_bad_paths_are_refused(tmp_path):
    src = _repo(tmp_path / "src", {"lib/a": "a\n"})
    # a tree entry git itself would never check out: a name with a control character
    blob = _g(src, "rev-parse", "HEAD:lib/a")
    tree = subprocess.run(["git", "-C", str(src), "mktree", "-z"], input=f"100644 blob {blob}\tbad\x01name\0".encode(),
                          check=True, capture_output=True).stdout.decode().strip()
    top = subprocess.run(["git", "-C", str(src), "mktree"], input=f"040000 tree {tree}\tlib\n".encode(),
                         check=True, capture_output=True).stdout.decode().strip()
    c = _g(src, "commit-tree", top, "-m", "bad")
    _g(src, "update-ref", "refs/heads/bad", c)
    assert gitsrc.fetch(str(src), None, "bad", c) == c
    with pytest.raises(GitSourceError, match="control character"):
        gitsrc.blob_tree(str(src), c, "lib")
    for sub in ("/lib", "../lib", "lib/../..", "-x"):
        with pytest.raises(GitSourceError, match="subdir must be"):
            gitsrc.blob_tree(str(src), c, sub)


# ------------------------------------------------------------------ kept repositories


def test_a_kept_commit_needs_no_network_and_survives_gc(tmp_path):
    src = _repo(tmp_path / "src", {"lib/a": "a\n"})
    url = str(src)
    c = gitsrc.fetch(url, None, "main")
    assert gitsrc.has_commit(url, c)
    shutil.rmtree(src)                                               # the remote is gone
    repo = gitsrc.repo_path(url)
    (repo / "FETCH_HEAD").unlink(missing_ok=True)
    subprocess.run(["git", f"--git-dir={repo}", "gc", "-q", "--prune=now"], check=True, capture_output=True)
    assert _kept(url, "rev-parse", f"refs/undine/{c}") == c
    assert gitsrc.fetch(url, None, c, c) == c                         # no network: the remote would fail
    assert sorted(gitsrc.blob_tree(url, c, "lib")) == ["a"]
    with pytest.raises(GitSourceError):
        gitsrc.fetch(url, None, "main")                              # a rev always asks the remote


def test_the_fallback_reaches_an_older_commit_in_a_shallow_kept_repo(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src", {"lib/a": "1\n"})
    old = _g(src, "rev-parse", "HEAD")
    _commit(src, {"lib/a": "2\n"}, "2")
    _commit(src, {"lib/a": "3\n"}, "3")
    url = str(src)
    gitsrc.fetch(url, None, "main")
    assert _kept(url, "rev-parse", "--is-shallow-repository") == "true"
    assert not gitsrc.has_commit(url, old)
    _global_config(tmp_path, monkeypatch, "[protocol]\n\tversion = 0\n")   # the depth-1 fetch of `old` is refused
    assert gitsrc.fetch(url, None, old, old) == old
    assert _kept(url, "rev-parse", "--is-shallow-repository") == "false"      # --unshallow brought the history
    assert cas.object_path(gitsrc.blob_tree(url, old, "lib")["a"][1]).read_bytes() == b"1\n"
    with pytest.raises(GitSourceError, match="has no branch, tag or commit 'nope'"):
        gitsrc.fetch(url, None, "nope")


def test_a_commit_other_than_the_locked_one_is_refused(tmp_path):
    src = _repo(tmp_path / "src", {"a": "1\n"})
    first = _g(src, "rev-parse", "HEAD")
    _commit(src, {"a": "2\n"}, "2")
    with pytest.raises(GitSourceError, match=f"not the locked {first[:12]}"):
        gitsrc.fetch(str(src), None, "main", first)


def test_two_processes_fetch_one_url_at_once(tmp_path):
    src = _repo(tmp_path / "src", {"lib/a": "a\n"})
    _commit(src, {"lib/b": "b\n"}, "2")
    code = ("import sys; sys.path.insert(0, sys.argv[2]); from undine.store import gitsrc; "
            "c = gitsrc.fetch(sys.argv[1], None, 'main'); print(c, sorted(gitsrc.blob_tree(sys.argv[1], c, 'lib')))")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(src), str(SERVER)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    head = _g(src, "rev-parse", "HEAD")
    assert [o.strip() for o, _ in outs] == [f"{head} ['a', 'b']"] * 2


def test_ext_protocol_is_refused(tmp_path):
    pwned = tmp_path / "pwned"
    url = f"ext::sh -c touch% {pwned}"
    with pytest.raises(GitSourceError):
        gitsrc.fetch(url, None, "main")
    assert not pwned.exists()


def test_option_looking_arguments_are_refused_before_git_runs(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src", {"a": "1\n"})
    url = str(src)
    c = gitsrc.fetch(url, None, "main")

    def no_git(*a, **k):
        raise AssertionError(f"git ran: {a}")
    monkeypatch.setattr(subprocess, "run", no_git)
    monkeypatch.setattr(subprocess, "Popen", no_git)
    for bad in ("--output=x", "-x", c[:12], c.upper(), c + "0", None):
        if bad is not None:                                           # fetch without a commit fetches `want`
            with pytest.raises(GitSourceError, match="not a full commit id"):
                gitsrc.fetch(url, None, "main", bad)
        with pytest.raises(GitSourceError, match="not a full commit id"):
            gitsrc.has_commit(url, bad)
        with pytest.raises(GitSourceError, match="not a full commit id"):
            gitsrc.blob_tree(url, bad, "")
        with pytest.raises(GitSourceError, match="not a full commit id"):
            gitsrc.archive_tree(url, bad, "")
    for want in ("--upload-pack=touch x", "-q", ""):
        with pytest.raises(GitSourceError, match="not a branch, tag or commit"):
            gitsrc.fetch(url, None, want)
    with pytest.raises(GitSourceError, match="not a git URL"):
        gitsrc.fetch(url, "--upload-pack=touch x", "main")
    assert not (tmp_path / "x").exists() and not Path("x").exists()


def test_a_git_part_in_a_path_is_refused_as_git_refuses_it(tmp_path):
    src = _repo(tmp_path / "src", {"lib/a": "a\n"})
    blob = _g(src, "rev-parse", "HEAD:lib/a")

    def tree(entries: str) -> str:
        return subprocess.run(["git", "-C", str(src), "mktree"], input=entries.encode(), check=True,
                              capture_output=True).stdout.decode().strip()
    for name in (".git", ".GIT", ".git.", "git~1"):
        dotgit = tree(f"100644 blob {blob}\tconfig\n100644 blob {blob}\tHEAD\n")
        lib = tree(f"100644 blob {blob}\ta\n040000 tree {dotgit}\t{name}\n")
        c = _g(src, "commit-tree", tree(f"040000 tree {lib}\tlib\n"), "-m", name)
        _g(src, "update-ref", "refs/heads/dotgit", c)
        assert gitsrc.fetch(str(src), None, "dotgit", c) == c
        with pytest.raises(GitSourceError, match=rf"{re.escape(name)}/HEAD has a \.git part"):
            gitsrc.blob_tree(str(src), c, "lib")
    # a tree read back from a record or a lock cannot install one either
    files = {"a": ("100644", cas.put_bytes(b"a\n")), ".git/config": ("100644", cas.put_bytes(b"[core]\n"))}
    with pytest.raises(cas.StoreError, match=r"\.git part"):
        cas.install(files, tmp_path / "st")
    assert not (tmp_path / "st" / ".git").exists()
    assert not gitsrc.cas.git_part(".github") and not gitsrc.cas.git_part(".gitignore")


def test_a_lock_file_left_by_a_killed_fetch_does_not_stop_the_next_one(tmp_path):
    src = _repo(tmp_path / "src", {"lib/a": "1\n"})
    url = str(src)
    gitsrc.fetch(url, None, "main")
    repo = gitsrc.repo_path(url)
    assert _kept(url, "rev-parse", "--is-shallow-repository") == "true"
    for name in ("shallow.lock", "packed-refs.lock", "refs/undine/x.lock"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("")                               # as a SIGKILLed git leaves them
    c2 = _commit(src, {"lib/a": "2\n"}, "2")
    assert gitsrc.fetch(url, None, "main") == c2
    assert not (repo / "shallow.lock").exists() and not (repo / "refs/undine/x.lock").exists()
    # git's own complaint names the lock file, not only "remove the file manually to continue"
    err = (b"fatal: Unable to create '/s/git/x.git/shallow.lock': File exists.\n\nAnother git process seems to be "
           b"running in this repository...\nremove the file manually to continue.\n")
    assert "/s/git/x.git/shallow.lock" in gitsrc._last_line(err)


def test_a_timed_out_git_is_asked_to_stop_before_it_is_killed(tmp_path, monkeypatch):
    calls = []

    class Slow:
        returncode = None

        def __init__(self, *a, **k):
            pass

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            return b"", b""

        def terminate(self):
            calls.append(("terminate",))

        def kill(self):
            calls.append(("kill",))
    monkeypatch.setattr(subprocess, "Popen", Slow)
    with pytest.raises(GitSourceError, match="took longer than 1 s"):
        gitsrc._run(None, "fetch", timeout=1)
    assert ("terminate",) in calls and ("kill",) not in calls


def test_export_subst_h_is_abbreviated_as_phase_0_did_in_a_large_kept_repository(tmp_path):
    src = _repo(tmp_path / "src", {"lib/v.txt": "$Format:%h$\n", "lib/.gitattributes": "v.txt export-subst\n"})
    url = str(src)
    c = gitsrc.fetch(url, None, "main")
    # the kept repository grows past 2^14 objects (an older version, a fallback fetch of every head and tag): git
    # would abbreviate %h to 8 characters there, and to 7 in phase 0's depth-1 repository
    stream = b"".join(b"blob\ndata %d\n%s\n" % (len(b := f"filler {i}\n".encode()), b) for i in range(17000))
    subprocess.run(["git", f"--git-dir={gitsrc.repo_path(url)}", "fast-import", "--quiet"], input=stream, check=True,
                   capture_output=True)
    count = _kept(url, "count-objects", "-v")
    assert int(next(x.split()[1] for x in count.splitlines() if x.startswith("in-pack"))) > 16384
    files = gitsrc.archive_tree(url, c, "lib")
    assert cas.object_path(files["v.txt"][1]).read_bytes() == (c[:7] + "\n").encode()
    assert cas.archive_sha(files) == _phase0_sha(url, "main", "lib", tmp_path)
