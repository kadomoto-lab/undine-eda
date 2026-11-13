"""The phase 1 command line (undine/__main__.py): store verify and gc, pdk list / verify / fetch, lib sync --offline,
lib lock --format 2, and the 'created undine.lock (format N)' line.

Every run passes --pdk-root (fake PDK trees under tmp_path), so no command scans the PDKs of this machine; ciel is
fake_ciel.py (UNDINE_CIEL); the tools are fixed, so nothing runs a real tool; git is real."""
import contextlib
import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_ciel import make_fake_ciel, read_log  # noqa: E402
from test_pdk_ops import BANNERS, NOW, SKY, SKY2, _tree  # noqa: E402
from undine import tools  # noqa: E402
from undine.pdks import ciel  # noqa: E402
from undine.store import cas, deps  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)
    monkeypatch.delenv("UNDINE_STORE_LINK", raising=False)


@pytest.fixture
def fake_ciel(tmp_path, monkeypatch):
    log = tmp_path / "ciel.log"
    monkeypatch.setenv("UNDINE_CIEL", str(make_fake_ciel(tmp_path / "bin", [SKY, SKY2], log=log)))
    return log


def _cli(root: Path, pdk_root: Path, *argv: str, stdin: str = "") -> tuple[int, str]:
    """Runs `python -m undine --root ROOT --pdk-root PDK_ROOT ARGV` in this process; (exit code, everything printed)."""
    from undine import __main__ as cli
    out, argv0, stdin0 = io.StringIO(), sys.argv, sys.stdin
    sys.argv = ["undine", "--root", str(root), "--pdk-root", str(pdk_root), *argv]
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.cli()
                code = 0
            except SystemExit as e:
                code = e.code or 0
    finally:
        sys.argv, sys.stdin = argv0, stdin0
    return code, out.getvalue()


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(d: Path, text: str = "v {}\nK {type=subcircuit}\n") -> Path:
    """lib/cell.sym holding `text` (one object of its own), tagged v1; v2 changes it."""
    (d / "lib").mkdir(parents=True)
    (d / "lib" / "cell.sym").write_text(text)
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "v1")
    _git(d, "tag", "v1")
    (d / "lib" / "cell.sym").write_text(text + "* v2\n")
    _git(d, "commit", "-qam", "v2")
    _git(d, "tag", "v2")
    return d


def _object(text: str) -> Path:
    return cas.object_path(hashlib.sha256(text.encode()).hexdigest())


def _lock(prj: Path) -> dict:
    return json.loads((prj / deps.LOCK).read_text())


def test_lock_format_2_only_on_an_explicit_command(tmp_path):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    _tree(base, SKY)
    _tree(base, SKY2)
    repo = _repo(tmp_path / "src")
    p = root / "p"
    code, out = _cli(root, base, "new", "p", "--pdk", f"sky130A@{SKY[:12]}")
    assert code == 0 and "created undine.lock (format 1)" in out, out
    for argv in [["lib", "add", "p", "ip", str(repo), "--rev", "v1", "--subdir", "lib"],
                 ["lib", "update", "p", "ip", "--rev", "v2"],
                 ["lib", "add", "p", "ip2", str(repo), "--rev", "v1", "--subdir", "lib"],
                 ["lib", "remove", "p", "ip2"], ["lib", "sync", "p"], ["lib", "lock", "p"], ["lib", "status", "p"],
                 ["set-pdk", "p", f"sky130A@{SKY2[:12]}"]]:
        code, out = _cli(root, base, *argv)
        lock = _lock(p)
        assert code == 0, (argv, out)
        assert lock["format"] == 1 and isinstance(lock["pdk"], str), (argv, lock)
        assert all("sha" in e and "tree" not in e for e in lock["libraries"].values()), (argv, lock)
        assert "created undine.lock" not in out, (argv, out)
    assert list(_lock(p)["libraries"]) == ["ip"]

    code, out = _cli(root, base, "pdk", "verify", "p")                     # without --pin: nothing written
    assert code == 0 and "PDK not pinned" in out and _lock(p)["format"] == 1, out
    code, out = _cli(root, base, "pdk", "verify", "p", "--pin")
    lock = _lock(p)
    assert code == 0 and "PDK unverified" in out and "pinned as installed here (format 2)" in out, out
    assert lock["format"] == 2 and lock["pdk"]["commit"] == SKY2 and "sha" in lock["libraries"]["ip"], lock
    code, out = _cli(root, base, "lib", "update", "p", "ip")
    entry = _lock(p)["libraries"]["ip"]
    assert code == 0 and entry["tree"].startswith("sha256:") and "sha" not in entry, (out, entry)

    _cli(root, base, "new", "q")                                           # no PDK, and no lock at all
    (root / "q" / deps.LOCK).unlink()
    code, out = _cli(root, base, "lib", "lock", "q", "--format", "2")
    lock = _lock(root / "q")
    assert code == 0 and "created undine.lock (format 2)" in out and "format 2, no PDK" in out, out
    assert lock["format"] == 2 and lock["pdk"] is None and lock["tools"] == NOW, lock
    code, out = _cli(root, base, "pdk", "verify", "q", "--pin")
    assert code == 1 and "has no PDK" in out and "lib lock q --format 2" in out, out


def test_lib_lock_format_2_prints_what_it_resolved_and_leaves_a_format_2_pin_alone(tmp_path):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    _tree(base, SKY)
    repo = _repo(tmp_path / "src")
    _cli(root, base, "new", "p", "--pdk", f"sky130A@{SKY[:12]}")
    m = json.loads((root / "p" / "project.json").read_text())
    m["libraries"]["ip"] = {"git": str(repo), "rev": "v1", "subdir": "lib"}           # named, not locked yet
    (root / "p" / "project.json").write_text(json.dumps(m))
    code, out = _cli(root, base, "lib", "lock", "p", "--format", "2")
    lock = _lock(root / "p")
    assert code == 0 and f"ip: {_git(repo, 'rev-parse', 'v1')[:12]} (sha " in out, out
    assert f"format 2, PDK sky130A@{SKY[:12]} pinned" in out and lock["format"] == 2, out
    pin = copy.deepcopy(lock["pdk"])
    pin["tree"]["libs.tech"] = "sha256:" + "0" * 64                         # pinned elsewhere, differently
    deps.write_lock(root / "p", {**lock, "pdk": pin})
    code, out = _cli(root, base, "lib", "lock", "p", "--format", "2")
    assert code == 0 and "format 2 already" in out and _lock(root / "p")["pdk"] == pin, out    # not re-pinned
    assert "! PDK sky130A@" in out and "libs.tech differs from the pin" in out, out
    code, out = _cli(root, base, "pdk", "verify", "p")
    assert code == 1 and "PDK modified" in out and f"ciel rm --pdk-family sky130 {SKY}" in out, out


def test_pdk_fetch_is_refused_when_only_a_volare_root_is_scanned(tmp_path, fake_ciel):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    _tree(base, SKY)
    _cli(root, base, "new", "p", "--pdk", f"sky130A@{SKY[:12]}")
    vol = tmp_path / "volare-root"
    (vol / "volare").mkdir(parents=True)
    code, out = _cli(root, vol, "pdk", "fetch", "p", "--yes")
    assert code == 1 and f"scanned: {vol}" in out and "--pdk-root" in out, out
    assert read_log(fake_ciel) == []                                       # ciel never ran


def test_pdk_fetch_asks_then_installs_with_ciel_and_reports_verify(tmp_path, fake_ciel):
    first, root = tmp_path / "first", tmp_path / "projects"
    ciel.fetch(first, "sky130", "sky130A", SKY, ["sky130_fd_pr"])          # the machine that pinned it
    _cli(root, first, "new", "p", "--pdk", f"sky130A@{SKY[:12]}")
    code, out = _cli(root, first, "pdk", "verify", "p", "--pin")
    assert code == 0 and _lock(root / "p")["pdk"]["libraries"] == ["sky130_fd_pr"], out
    pinned = (root / "p" / deps.LOCK).read_bytes()
    runs = len(read_log(fake_ciel))

    here = tmp_path / "here"                                               # this machine: an empty ciel root
    code, out = _cli(root, here, "pdk", "fetch", "p", stdin="n\n")
    assert code == 1 and "not fetched" in out and len(read_log(fake_ciel)) == runs, out
    for shown in ("sky130", "sky130A", SKY, "sky130_fd_pr", ciel.DEFAULT_SOURCE, str(here.absolute()), "[y/N]"):
        assert shown in out, (shown, out)
    assert not here.exists()

    code, out = _cli(root, here, "pdk", "fetch", "p", "--yes")
    assert code == 0, out
    assert f"installed {ciel.variant_dir(here.absolute(), 'sky130', 'sky130A', SKY)}" in out, out
    assert "PDK unverified" in out and "POST /api/pdks/rescan" in out, out
    assert read_log(fake_ciel)[-1][-1] == SKY and (root / "p" / deps.LOCK).read_bytes() == pinned
    code, out = _cli(root, here, "pdk", "list")
    line = next(x for x in out.splitlines() if x.startswith(f"sky130A@{SKY[:12]}"))
    assert SKY in line and " ciel " in line and "libraries=sky130_fd_pr" in line, out
    assert _cli(root, here, "pdks")[1] == out                              # the alias


def test_pdk_fetch_without_a_commit_installed_needs_network(tmp_path, fake_ciel):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    _tree(base, SKY)
    _cli(root, base, "new", "p", "--pdk", f"sky130A@{SKY[:12]}")
    (root / "p" / deps.LOCK).unlink()
    m = json.loads((root / "p" / "project.json").read_text())
    m["pdk"] = f"sky130A@{SKY2[:12]}"                                     # known only to ciel's source
    (root / "p" / "project.json").write_text(json.dumps(m))
    here = tmp_path / "here"
    code, out = _cli(root, here, "pdk", "fetch", "p", "--yes")
    assert code == 1 and "--network" in out and read_log(fake_ciel) == [], out
    code, out = _cli(root, here, "pdk", "fetch", "p", "--yes", "--network")
    assert code == 0 and SKY2 in out and "PDK not pinned" in out, out
    assert not (root / "p" / deps.LOCK).exists()                           # a fetch never writes the lock


def test_store_verify_finds_an_edited_object_and_removes_it(tmp_path):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    text = "v {}\nK {type=subcircuit}\n* verify\n"
    repo = _repo(tmp_path / "src", text)
    _cli(root, base, "new", "p")
    assert _cli(root, base, "lib", "add", "p", "ip", str(repo), "--rev", "v1", "--subdir", "lib")[0] == 0
    code, out = _cli(root, base, "store", "verify")
    assert code == 0 and "objects checked" in out and "!" not in out, out
    obj = _object(text)
    (root / "p" / deps.DEPS / "ip" / "cell.sym").unlink()                 # the install's link: the object alone
    obj.chmod(0o644)
    obj.write_text("edited\n")
    code, out = _cli(root, base, "store", "verify", "--dry-run")
    assert code == 1 and f"! object {obj.parent.name}{obj.name}" in out and "would be removed" in out, out
    assert obj.exists()
    code, out = _cli(root, base, "store", "verify")
    assert code == 1 and f"! object {obj.parent.name}{obj.name}" in out and "removed" in out, out
    assert not obj.exists() and "lib sync PROJECT" in out


def test_store_gc_keeps_what_projects_here_lock_or_install_and_removes_the_rest(tmp_path):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    texts = {k: f"v {{}}\n* {k}\n" for k in ("root", "elsewhere", "changeset", "by_hand", "unused")}
    repos = {k: _repo(tmp_path / "src" / k, t) for k, t in texts.items()}

    def project(where: Path, name: str, key: str) -> Path:
        _cli(where, base, "new", name)
        code, out = _cli(where, base, "lib", "add", name, key.replace("_", ""), str(repos[key]), "--rev", "v1",
                         "--subdir", "lib")
        assert code == 0, out
        return where / name
    p = project(root, "p", "root")                                          # under --root
    project(tmp_path / "elsewhere", "e", "elsewhere")                      # registered by its add
    c = project(tmp_path / "gone", "c", "changeset")                       # its lock lives on in a changeset worktree
    work = root / ".changesets" / "p" / "c1" / "work" / "prefix"
    work.mkdir(parents=True)
    shutil.copy(c / deps.LOCK, work / deps.LOCK)
    (root / ".changesets" / "p" / "c1" / "work" / ".git").mkdir()
    (root / ".changesets" / "p" / "c1" / "work" / ".git" / deps.LOCK).write_text('{"format": 3}')   # never read
    shutil.rmtree(c)
    _cli(root, base, "lib", "add", "p", "byhand", str(repos["by_hand"]), "--rev", "v1", "--subdir", "lib")
    lock = _lock(p)
    del lock["libraries"]["byhand"]                                        # still installed
    deps.write_lock(p, lock)
    shutil.rmtree(project(tmp_path / "gone", "u", "unused"))               # nothing refers to it any more

    code, out = _cli(root, base, "store", "gc", "--dry-run")
    assert code == 0 and "would remove 1 tree records and 1 objects" in out, out
    assert all(_object(t).exists() for t in texts.values())
    code, out = _cli(root, base, "store", "gc")
    assert code == 0 and "removed 1 tree records and 1 objects" in out, out
    assert [k for k, t in texts.items() if not _object(t).exists()] == ["unused"]


def test_store_gc_with_an_unreadable_lock_removes_nothing(tmp_path):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    text = "v {}\n* unused\n"
    _cli(tmp_path / "gone", base, "new", "u")
    _cli(tmp_path / "gone", base, "lib", "add", "u", "ip", str(_repo(tmp_path / "src", text)), "--rev", "v1")
    shutil.rmtree(tmp_path / "gone" / "u")
    newer = tmp_path / "elsewhere" / "n"
    _cli(tmp_path / "elsewhere", base, "new", "n")
    cas.register_project(newer)
    (newer / deps.LOCK).write_text('{"format": 3, "libraries": {}}')
    code, out = _cli(root, base, "store", "gc")
    assert code == 1 and f"! {newer.resolve() / deps.LOCK}" in out and "format 3" in out, out
    assert "nothing was removed" in out and _object(text).exists()
    (newer / deps.LOCK).write_text("<<<<<<< ours\n")                       # a merge's conflict markers
    code, out = _cli(root, base, "store", "gc")
    assert code == 1 and "nothing was removed" in out and _object(text).exists(), out
    (newer / deps.LOCK).unlink()
    assert _cli(root, base, "store", "gc")[0] == 0 and not _object(text).exists()


def test_lib_sync_offline_and_status_notes_issues_and_warnings(tmp_path):
    base, root = tmp_path / "pdks", tmp_path / "projects"
    _tree(base, SKY)
    _tree(base, SKY2)
    repo = _repo(tmp_path / "src")
    p = root / "p"
    _cli(root, base, "new", "p", "--pdk", f"sky130A@{SKY[:12]}")
    _cli(root, base, "lib", "add", "p", "ip", str(repo), "--rev", "v1", "--subdir", "lib")
    shutil.rmtree(p / deps.DEPS / "ip")
    shutil.rmtree(cas.store_root() / "git")                                # no repository to read: the store alone
    code, out = _cli(root, base, "lib", "sync", "p", "--offline")
    assert code == 0 and "ip: installed" in out and (p / deps.DEPS / "ip" / "cell.sym").is_file(), out

    assert _cli(root, base, "pdk", "verify", "p", "--pin")[0] == 0
    code, out = _cli(root, base, "lib", "status", "p")
    assert code == 0 and "note: PDK sky130A@" in out and "unverified (trust on first use)" in out, out
    assert "unverified" in next(x for x in out.splitlines() if x.strip().startswith("PDK")) and "as locked" in out
    lock = _lock(p)
    lock["tools"]["klayout"] = {"version": "0.28.17", "from": "pixi"}      # another machine's KLayout
    deps.write_lock(p, lock)
    code, out = _cli(root, base, "lib", "status", "p")
    assert code == 0 and "~ klayout: locked 0.28.17 from pixi, here 0.30.12 from pixi" in out, out
    code, out = _cli(root, base, "lib", "sync", "p")
    assert code == 0 and "~ klayout" in out, out                           # warnings after a command, exit 0
    m = json.loads((p / "project.json").read_text())
    m["pdk"] = f"sky130A@{SKY2[:12]}"                                     # the requirement moved, the pin did not
    (p / "project.json").write_text(json.dumps(m))
    code, out = _cli(root, base, "lib", "status", "p")
    assert code == 1 and f"! project.json asks for sky130A@{SKY2[:12]}" in out, out

    shutil.rmtree(p / deps.DEPS / "ip")
    for d in ("trees", "objects", "git"):
        shutil.rmtree(cas.store_root() / d, ignore_errors=True)
    code, out = _cli(root, base, "lib", "sync", "p", "--offline")
    assert code == 1 and "offline, and not in the store" in out and not (p / deps.DEPS / "ip").exists(), out


def test_serve_gives_the_server_the_pdk_roots_it_scanned(tmp_path, monkeypatch):
    """Rescan and `pdk fetch` over HTTP look where the PDKs were found: --pdk-root, else default_pdk_roots()."""
    import undine.server.api as api
    seen = {}

    class Stop(Exception):
        pass

    def fake_server(root, pdks, import_roots, llm_policy=None, mcp=False, dep_roots=None, pdk_roots=None):
        seen["pdk_roots"] = pdk_roots
        raise Stop
    monkeypatch.setattr(api, "Server", fake_server)
    with pytest.raises(Stop):
        _cli(tmp_path / "projects", tmp_path / "pdks", "serve", "--port", "0")
    assert seen["pdk_roots"] == [tmp_path / "pdks"]
