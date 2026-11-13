"""The phase 2 command line (undine/__main__.py): lib add / update / remove / lock / status report the packages a
dependency brings (with the chain that places each, its local names and whether the project trusts it), lib trust asks
a person, lib lock --format 2 converts and resolves in one write, and set-pdk lists the packages that refuse a PDK.

Every run passes --pdk-root (fake PDK trees under tmp_path), so no command scans the PDKs of this machine; the tools
are fixed, so nothing runs a real tool; git is real, every repository local."""
import contextlib
import copy
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_deps_phase2 import _dep, _git, _package  # noqa: E402
from test_pdk_ops import BANNERS, NOW, SKY, SKY2, _tree  # noqa: E402
from undine import tools  # noqa: E402
from undine.store import deps  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

# a schematic whose instance value runs a .control block: what the trust rule refuses in a simulation
CONTROL_SCH = ('v {xschem version=3.4.5 file_version=1.2}\n'
               'C {devices/code_shown.sym} 0 0 0 0 {name=s1 value=".control\nrun\n.endc"}\n')


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


class _Terminal(io.StringIO):
    """stdin of a person at a terminal: what lib trust asks needs one."""

    def isatty(self) -> bool:
        return True


def _cli(root: Path, pdk_root: Path, *argv: str, stdin: str = "", tty: bool = False) -> tuple[int, str]:
    """Runs `python -m undine --root ROOT --pdk-root PDK_ROOT ARGV` in this process; (exit code, everything printed)."""
    from undine import __main__ as cli
    out, argv0, stdin0 = io.StringIO(), sys.argv, sys.stdin
    sys.argv = ["undine", "--root", str(root), "--pdk-root", str(pdk_root), *argv]
    sys.stdin = _Terminal(stdin) if tty else io.StringIO(stdin)
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


def _setup(tmp_path: Path, *pdks: str) -> tuple[Path, Path]:
    base, root = tmp_path / "pdks", tmp_path / "projects"
    base.mkdir()
    for commit in pdks:
        _tree(base, commit)
    return base, root


def _meta(prj: Path) -> dict:
    return json.loads((prj / "project.json").read_text())


def _lock(prj: Path) -> dict:
    return json.loads((prj / deps.LOCK).read_text())


def _c12(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", rev)[:12]


def test_lib_add_prints_the_packages_it_brings_and_what_is_not_trusted(tmp_path):
    base, root = _setup(tmp_path)
    src = tmp_path / "src"
    c = _package(src, "c", extra={"c_cell.sch": CONTROL_SCH})
    ip = _package(src, "analog_ip", "1.2.0", deps_={"c": _dep(c)})
    bias = _package(src, "bias_gen", "0.3.0", cells=("ref",))
    p = root / "p"
    assert _cli(root, base, "new", "p")[0] == 0

    lock = (p / deps.LOCK).read_bytes()                                  # format 1: a package's own dependencies
    code, out = _cli(root, base, "lib", "add", "p", "analog_ip", str(ip), "--rev", "v1.2.0")
    assert code == 1 and out.strip() == ("error: analog_ip: package analog_ip names dependencies (c): needs lock "
                                         "format 2: run python -m undine lib lock p --format 2 first; nothing was "
                                         "changed"), out
    assert (p / deps.LOCK).read_bytes() == lock and "analog_ip" not in _meta(p)["libraries"]

    code, out = _cli(root, base, "lib", "lock", "p", "--format", "2")
    assert code == 0 and "format 2, no PDK" in out, out
    code, out = _cli(root, base, "lib", "add", "p", "analog_ip", str(ip), "--rev", "v1.2.0")
    lines = out.splitlines()
    assert code == 0, out
    assert lines[0] == f"analog_ip: analog_ip 1.2.0 at {_c12(ip, 'v1.2.0')} (1 cells), locked in undine.lock", out
    assert "  + c 1.0.0 as c, via analog_ip" in lines, out
    assert ("~ c 1.0.0: 1 cell with .control (not trusted): a simulation using it needs "
            "`python -m undine lib trust p c`") in lines, out
    assert set(_lock(p)["packages"]) == {"c"}

    # an alias: the package is installed under its own name too
    code, out = _cli(root, base, "lib", "add", "p", "bias", str(bias), "--rev", "v0.3.0", "--package", "bias_gen")
    assert code == 0 and out.splitlines()[:2] == [
        f"bias: bias_gen 0.3.0 at {_c12(bias, 'v0.3.0')} (1 cells), locked in undine.lock",
        "  installed as bias and bias_gen"], out
    assert _meta(p)["libraries"]["bias"]["package"] == "bias_gen"
    assert (p / deps.DEPS / "bias_gen" / "ref.sym").is_file()


def test_lib_status_shows_via_and_alias_rows_and_remove_names_what_stays_placed(tmp_path):
    base, root = _setup(tmp_path)
    src = tmp_path / "src"
    sar = _package(src, "sar_common", "1.3.1", cells=("comparator",))
    bias = _package(src, "bias_gen", "0.3.0", cells=("ref",))
    ip = _package(src, "analog_ip", "1.2.0", deps_={"sar_common": _dep(sar, "v1.3.1"),
                                                     "bias": _dep(bias, "v0.3.0", package="bias_gen")})
    p = root / "p"
    _cli(root, base, "new", "p")
    _cli(root, base, "lib", "lock", "p", "--format", "2")
    assert _cli(root, base, "lib", "add", "p", "sar_common", str(sar), "--rev", "v1.3.1")[0] == 0
    code, out = _cli(root, base, "lib", "add", "p", "analog_ip", str(ip), "--rev", "v1.2.0")
    assert code == 0 and "  + bias_gen 0.3.0 as bias, bias_gen, via analog_ip" in out.splitlines(), out

    code, out = _cli(root, base, "lib", "status", "p")
    assert code == 0 and "as locked" in out, out
    rows = {x.split()[0]: x for x in out.splitlines() if x.startswith("  ") and x.split()[0] != "via"}
    cip, cbias = _c12(ip, "v1.2.0"), _c12(bias, "v0.3.0")
    assert rows["analog_ip"].endswith(f"v1.2.0 @ {cip}  analog_ip 1.2.0, not trusted"), out
    assert rows["sar_common"].endswith("  sar_common 1.3.1, not trusted"), out
    assert rows["bias"].endswith(f"v0.3.0 @ {cbias}  -> bias_gen 0.3.0, not trusted"), out
    assert rows["bias_gen"].endswith("  bias_gen 0.3.0, not trusted"), out
    via = f"via p -> analog_ip 1.2.0 (v1.2.0 @ {cip}) -> bias_gen 0.3.0 (v0.3.0 @ {cbias})"
    lines = out.splitlines()
    for name in ("bias", "bias_gen"):                       # each transitive row says what places it
        assert lines[lines.index(rows[name]) + 1].strip() == via, out
    assert lines[lines.index(rows["sar_common"]) + 1].strip().split()[0] != "via"     # direct: no via line

    # sar_common is direct and placed by analog_ip: removing it from project.json keeps it installed
    code, out = _cli(root, base, "lib", "remove", "p", "sar_common")
    assert code == 0 and "sar_common: still placed by analog_ip" in out.splitlines(), out
    assert (p / deps.DEPS / "sar_common" / "comparator.sym").is_file()
    code, out = _cli(root, base, "lib", "remove", "p", "analog_ip")
    assert code == 0, out
    assert "removed analog_ip 1.2.0 (installed as analog_ip)" in out, out
    assert "removed bias_gen 0.3.0 (installed as bias, bias_gen)" in out, out
    assert "removed sar_common 1.3.1 (installed as sar_common)" in out, out
    assert not (p / deps.DEPS / "bias").exists() and not (p / deps.DEPS / "sar_common").exists()


def test_lib_update_prints_the_packages_it_brings_moves_and_drops(tmp_path):
    base, root = _setup(tmp_path)
    src = tmp_path / "src"
    c = _package(src, "c")
    _package(src, "c", "1.1.0", repo=c)
    d = _package(src, "d")
    a = _package(src, "a", deps_={"c": _dep(c)})
    _package(src, "a", "2.0.0", deps_={"c": _dep(c, "v1.1.0"), "d": _dep(d)}, repo=a)
    _cli(root, base, "new", "p")
    _cli(root, base, "lib", "lock", "p", "--format", "2")
    _cli(root, base, "lib", "add", "p", "a", str(a), "--rev", "v1.0.0")
    c10, c11 = _c12(c, "v1.0.0"), _c12(c, "v1.1.0")

    code, out = _cli(root, base, "lib", "update", "p", "a", "--rev", "v2.0.0")
    lines = out.splitlines()
    assert code == 0 and lines[0] == f"a: {_c12(a, 'v1.0.0')} -> {_c12(a, 'v2.0.0')}", out
    assert f"  c: 1.0.0 @ {c10} -> 1.1.0 @ {c11}" in lines and "  + d 1.0.0 as d" in lines, out
    code, out = _cli(root, base, "lib", "update", "p", "a", "--rev", "v1.0.0")
    lines = out.splitlines()
    assert code == 0 and f"  c: 1.1.0 @ {c11} -> 1.0.0 @ {c10}" in lines, out
    assert "  - d 1.0.0 as d: no longer needed" in lines, out

    code, out = _cli(root, base, "lib", "update", "p", "c")          # placed by a, not in project.json
    assert code == 1 and out.strip() == "error: c: placed by a; update or remove that instead", out


def test_lib_trust_lists_asks_and_writes_trusted(tmp_path):
    base, root = _setup(tmp_path)
    src = tmp_path / "src"
    c = _package(src, "c", extra={"c_cell.sch": CONTROL_SCH})
    plain = src / "plain"
    _package(src, "x", repo=plain)
    (plain / "undine-package.toml").unlink()                            # no manifest: keyed by its commit
    _git(plain, "commit", "-qam", "plain")
    p = root / "p"
    _cli(root, base, "new", "p")
    _cli(root, base, "lib", "add", "p", "c", str(c), "--rev", "v1.0.0")
    _cli(root, base, "lib", "add", "p", "plain", str(plain), "--rev", "HEAD")

    code, out = _cli(root, base, "lib", "trust", "p", "c")              # no terminal, no --yes
    assert code == 1 and "c: c 1.0.0, trust key c@1.0.0" in out, out
    assert "  c_cell.sch s1 value line 1 (.control): .control" in out.splitlines(), out
    assert "error: trusting c@1.0.0 is a person's decision" in out and "trusted" not in _meta(p), out

    code, out = _cli(root, base, "lib", "trust", "p", "c", stdin="n\n", tty=True)
    assert code == 1 and "trust c@1.0.0 in p? [y/N]" in out and "not trusted" in out, out
    assert "trusted" not in _meta(p)
    code, out = _cli(root, base, "lib", "trust", "p", "c", stdin="y\n", tty=True)
    assert code == 0 and "c@1.0.0: trusted in p" in out and _meta(p)["trusted"] == ["c@1.0.0"], out
    code, out = _cli(root, base, "lib", "trust", "p", "c", tty=True)    # nothing asked again
    assert code == 0 and "c@1.0.0 is trusted in p already" in out and "[y/N]" not in out, out
    code, out = _cli(root, base, "lib", "status", "p")
    row = next(x for x in out.splitlines() if x.startswith("  c "))
    assert row.endswith("  c 1.0.0, trusted") and "lib trust p c" not in out, out

    code, out = _cli(root, base, "lib", "trust", "p", "c", "--untrust")
    assert code == 0 and "c@1.0.0: no longer trusted in p" in out and "trusted" not in _meta(p), out
    code, out = _cli(root, base, "lib", "trust", "p", "plain", "--yes")     # nothing found, trusted by --yes
    key = f"plain@{_c12(plain, 'HEAD')}"
    assert code == 0 and "nothing a simulation would refuse was found" in out and f"{key}: trusted in p" in out, out
    assert _meta(p)["trusted"] == [key]

    code, out = _cli(root, base, "lib", "trust", "p", "ghost", "--yes")
    assert code == 1 and "error: ghost: no dependency is installed under that name" in out, out
    code, out = _cli(root, base, "lib", "trust", "p", "../x", "--yes")
    assert code == 1 and "is not a library name" in out, out
    (p / deps.DEPS / "stray").mkdir()                                     # not installed by Undine
    code, out = _cli(root, base, "lib", "trust", "p", "stray", "--yes")
    assert code == 1 and "stray has no install record" in out and _meta(p)["trusted"] == [key], out


def test_lib_lock_format_2_converts_and_resolves_in_one_write_or_leaves_format_1(tmp_path):
    base, root = _setup(tmp_path)
    src = tmp_path / "src"
    c = _package(src, "c", cells=("comparator",))
    a = _package(src, "a", deps_={"c": _dep(c)})
    x = _package(src, "x", cells=("comparator",))
    for name, libs in (("p", {"a": a}), ("q", {"a": a, "x": x})):
        _cli(root, base, "new", name)
        m = _meta(root / name)
        m["libraries"].update({k: {"git": str(r), "rev": "v1.0.0"} for k, r in libs.items()})
        (root / name / "project.json").write_text(json.dumps(m))

    code, out = _cli(root, base, "lib", "lock", "p")                   # without --format 2: the hint
    assert code == 1 and "needs lock format 2: run python -m undine lib lock p --format 2 first" in out, out
    code, out = _cli(root, base, "lib", "lock", "p", "--format", "2")
    lock = _lock(root / "p")
    assert code == 0 and lock["format"] == 2 and set(lock["packages"]) == {"c"}, out
    lines = out.splitlines()
    assert lines[0].startswith(f"a: {_c12(a, 'v1.0.0')} (tree sha256:") and lines[0].endswith(", a 1.0.0"), out
    assert "  + c 1.0.0 as c, via a" in lines and "undine.lock: format 2, no PDK" in lines, out
    assert (root / "p" / deps.DEPS / "c" / "comparator.sym").is_file()

    before = (root / "q" / deps.LOCK).read_bytes()
    code, out = _cli(root, base, "lib", "lock", "q", "--format", "2")
    assert code == 1 and "error: comparator: x 1.0.0 and c 1.0.0; nothing was changed" in out, out
    assert (root / "q" / deps.LOCK).read_bytes() == before and _lock(root / "q")["format"] == 1
    assert not (root / "q" / deps.DEPS).exists() or not any((root / "q" / deps.DEPS).iterdir())


def test_set_pdk_to_a_pdk_locked_packages_refuse_lists_each_and_changes_nothing(tmp_path):
    base, root = _setup(tmp_path, SKY, SKY2)
    req = f'\n[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\ncommits = ["{SKY}"]\n'
    src = tmp_path / "src"
    c = _package(src, "c", body=req)
    ip = _package(src, "analog_ip", "1.2.0", deps_={"c": _dep(c)}, body=req)
    p = root / "p"
    _cli(root, base, "new", "p", "--pdk", f"sky130A@{SKY[:12]}")
    m = _meta(p)
    m["libraries"]["analog_ip"] = {"git": str(ip), "rev": "v1.2.0"}
    (p / "project.json").write_text(json.dumps(m))
    code, out = _cli(root, base, "lib", "lock", "p", "--format", "2")      # converted and resolved in one write
    assert code == 0 and f"format 2, PDK sky130A@{SKY[:12]} pinned" in out and "  + c 1.0.0 as c, via analog_ip" in out
    assert _lock(p)["pdk"]["commit"] == SKY and set(_lock(p)["packages"]) == {"c"}
    before = {n: (p / n).read_bytes() for n in ("project.json", deps.LOCK)}

    code, out = _cli(root, base, "set-pdk", "p", f"sky130A@{SKY2[:12]}")
    cip, cc = _c12(ip, "v1.2.0"), _c12(c, "v1.0.0")
    why = f"sky130A@{SKY2[:12]} is not among commits {SKY[:12]}"
    assert code == 1 and out.splitlines() == [
        f"error: the PDK sky130A@{SKY2[:12]} is not accepted by 2 locked packages:",
        f"- p -> analog_ip 1.2.0 (v1.2.0 @ {cip}): {why}",
        f"- p -> analog_ip 1.2.0 (v1.2.0 @ {cip}) -> c 1.0.0 (v1.0.0 @ {cc}): {why}",
        "update or remove them (lib update, lib remove), or choose a PDK they accept; nothing was changed"], out
    assert {n: (p / n).read_bytes() for n in before} == before
    code, out = _cli(root, base, "set-pdk", "p", f"sky130A@{SKY[:12]}")    # the PDK it names: never refused
    assert code == 0 and "already" in out, out


def _moved_by_hand(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Project p on format 1, with package solo (accepting SKY2 only) added while it asked for SKY2, then moved to SKY
    by hand in project.json and undine.lock (as a merge could): (pdk root, projects root, solo's repository)."""
    base, root = _setup(tmp_path, SKY, SKY2)
    req = f'\n[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\ncommits = ["{SKY2}"]\n'
    solo = _package(tmp_path / "src", "solo", body=req)
    p = root / "p"
    _cli(root, base, "new", "p", "--pdk", f"sky130A@{SKY2[:12]}")
    assert _cli(root, base, "lib", "add", "p", "solo", str(solo), "--rev", "v1.0.0")[0] == 0
    m = _meta(p)
    m["pdk"] = f"sky130A@{SKY[:12]}"
    (p / "project.json").write_text(json.dumps(m))
    deps.write_lock(p, {**_lock(p), "pdk": f"sky130A@{SKY[:12]}"})
    return base, root, solo


def test_a_pdk_pinned_by_lib_lock_format_2_that_a_package_refuses_is_a_warning(tmp_path):
    """The pin records what is installed: a package that does not accept it is a '!' line, not a refusal."""
    base, root, solo = _moved_by_hand(tmp_path)
    p = root / "p"
    code, out = _cli(root, base, "lib", "lock", "p", "--format", "2")
    assert _lock(p)["format"] == 2 and f"format 2, PDK sky130A@{SKY[:12]} pinned" in out, out
    said = (f"! p -> solo (git {_c12(solo, 'v1.0.0')}): does not fit the PDK sky130A@{SKY[:12]}: sky130A@{SKY[:12]} "
            f"is not among commits {SKY2[:12]}: update or remove it (lib update, lib remove), or choose a PDK it "
            "accepts")                              # a format 1 entry: no package keys, so its commit names it
    assert out.splitlines()[2] == said and out.count(said) == 1, out    # status's issue, not said twice
    assert code == 0 and "solo                 dependency" in out, out


def test_lib_sync_shows_a_package_the_locked_pdk_does_not_fit(tmp_path):
    base, root, solo = _moved_by_hand(tmp_path)
    p = root / "p"
    _cli(root, base, "lib", "lock", "p", "--format", "2")
    shutil.rmtree(p / deps.DEPS / "solo")
    code, out = _cli(root, base, "lib", "sync", "p")
    assert "solo: installed" in out and "~ p -> solo (git " in out and "does not fit the PDK" in out, out
    assert (p / deps.DEPS / "solo" / "solo_cell.sym").is_file()


def test_lib_add_prints_a_conflict_with_both_chains_and_every_problem(tmp_path):
    base, root = _setup(tmp_path)
    src = tmp_path / "src"
    c = _package(src, "c")
    _package(src, "c", "2.0.0", repo=c)
    a = _package(src, "a", deps_={"c": _dep(c, "v1.0.0")})
    b = _package(src, "b", deps_={"c": _dep(c, "v2.0.0")})
    b2 = _package(src, "b2", deps_={"c": _dep(c, "v2.0.0")}, cells=("a_cell",))     # and a cell named like a's
    p = root / "p"
    _cli(root, base, "new", "p")
    _cli(root, base, "lib", "lock", "p", "--format", "2")
    assert _cli(root, base, "lib", "add", "p", "a", str(a), "--rev", "v1.0.0")[0] == 0
    before = {n: (p / n).read_bytes() for n in ("project.json", deps.LOCK)}
    ca, cb, c1, c2 = _c12(a, "v1.0.0"), _c12(b, "v1.0.0"), _c12(c, "v1.0.0"), _c12(c, "v2.0.0")

    code, out = _cli(root, base, "lib", "add", "p", "b", str(b), "--rev", "v1.0.0")
    assert code == 1 and out.startswith("error: c: two versions are required, "), out
    assert f"p -> a 1.0.0 (v1.0.0 @ {ca}) -> c 1.0.0 (v1.0.0 @ {c1})" in out, out
    assert f"p -> b 1.0.0 (v1.0.0 @ {cb}) -> c 2.0.0 (v2.0.0 @ {c2})" in out, out
    assert out.rstrip().endswith("nothing was changed"), out
    assert {n: (p / n).read_bytes() for n in before} == before

    code, out = _cli(root, base, "lib", "add", "p", "b2", str(b2), "--rev", "v1.0.0")
    lines = out.splitlines()
    assert code == 1 and lines[0] == "error: 2 problems with the dependencies:", out
    assert lines[1].startswith("- c: two versions are required, ") and "-> c 2.0.0 (v2.0.0 @ " in lines[1], out
    assert lines[2] == "- a_cell: a 1.0.0 and b2 1.0.0" or lines[2] == "- a_cell: b2 1.0.0 and a 1.0.0", out
    assert lines[3] == "nothing was changed" and len(lines) == 4, out
    assert {n: (p / n).read_bytes() for n in before} == before


def test_pdk_verify_pin_prints_the_packages_the_new_pin_does_not_fit(tmp_path):
    base, root, solo = _moved_by_hand(tmp_path)
    p = root / "p"
    code, out = _cli(root, base, "pdk", "verify", "p", "--pin")
    assert _lock(p)["format"] == 2 and _lock(p)["pdk"]["commit"] == SKY, out
    said = (f"! p -> solo (git {_c12(solo, 'v1.0.0')}): does not fit the PDK sky130A@{SKY[:12]}: sky130A@{SKY[:12]} "
            f"is not among commits {SKY2[:12]}: update or remove it (lib update, lib remove), or choose a PDK it "
            "accepts")
    assert said in out.splitlines(), out
    assert out.splitlines()[-1].startswith("PDK "), out                # a warning: the PDK's state still ends it


def test_lib_status_shows_a_direct_aliases_own_name_as_the_same_install(tmp_path):
    base, root = _setup(tmp_path)
    bias = _package(tmp_path / "src", "bias_gen", "0.3.0", cells=("ref",))
    _cli(root, base, "new", "p")
    _cli(root, base, "lib", "lock", "p", "--format", "2")
    assert _cli(root, base, "lib", "add", "p", "bias", str(bias), "--rev", "v0.3.0", "--package", "bias_gen")[0] == 0
    code, out = _cli(root, base, "lib", "status", "p")
    lines = out.splitlines()
    row = next(x for x in lines if x.split() and x.split()[0] == "bias_gen")
    assert code == 0 and lines[lines.index(row) + 1].strip() == "= bias", out
    assert "via" not in out, out
