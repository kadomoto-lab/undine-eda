"""The semantic merge with real git (vcs/gitmerge.py, docs/design/merge.md): git-setup registers the merge driver,
`git merge` of two branches that changed different objects of the same cell files combines them, a conflict is
reported and resolved with `undine resolve`, merge-check lists the cells to verify, the pre-commit hook refuses
unsaved layout edits, and diff --rev reports the semantic changes."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402

from undine.__main__ import GITATTRIBUTES, HOOK_MARK, PRELUDE, git_setup, undine_command  # noqa: E402
from undine.layouts import layoutstore  # noqa: E402
from undine.schematics import xschem  # noqa: E402
from undine.vcs import gitmerge, logmerge  # noqa: E402
from test_vcs_log import jsonl, rec  # noqa: E402
from test_vcs_schematic import INV, INV_SYM, edit  # noqa: E402
from test_vcs_yaml import amp, ib, inverter, item, nl  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

MET1 = (68, 20)
HEADER = "v {xschem version=3.4.5 file_version=1.2\n}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
TOP = HEADER + "C {l/inv.sym} 0 0 0 0 {name=x1}\nC {buf.sym} 200 0 0 0 {name=x2}\n"    # above inv and buf
CHAIN = HEADER + "C {buf.sym} 0 0 0 0 {name=x1}\nC {buf.sym} 200 0 0 0 {name=x2}\n"    # above buf only
BUF = edit(INV, "T {CMOS inverter}", "T {buffer stage}")


def save(seq: int, t: str) -> dict:
    """The record LayoutDB.save writes: the layout edits before it are in the cell files (no journal)."""
    return rec(seq, op="save", cell=None, view="layout", effects=None, t=t)


LOG = [rec(1, reason="size the inverter"), rec(2, cell="inv", view="layout", op="create", t="2026-10-23T10:05:00+0900"),
       save(3, "2026-10-23T10:06:00+0900")]
SCH = "libs/l/inv.sch"


@pytest.fixture(autouse=True)
def git_env(monkeypatch):
    """git (and the driver it runs) without the user's or the system's configuration."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def git(d: Path, *a: str, ok: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
    assert not ok or r.returncode == 0, r.stdout + r.stderr
    return r


def undine(d: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "undine", *a], cwd=d, capture_output=True, text=True)


def write(d: Path, rel: str, data: str | bytes) -> None:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)


def text(d: Path, rel: str) -> str:
    return (d / rel).read_text()


def layout(lib: Path, edit_fn=None) -> None:
    """inv (metal, a via1 instance) and top (inv as X1), one file per cell; or an edit of the saved cells, written the
    way the layout store saves them (only the cells that changed)."""
    ly = kdb.Layout()
    if edit_fn is None:
        ly.dbu = 0.001
        inv, via, top = (ly.create_cell(n) for n in ("inv", "via1", "top"))
        via.shapes(ly.layer(67, 44)).insert(kdb.Box(0, 0, 170, 170))
        inv.shapes(ly.layer(*MET1)).insert(kdb.Box(0, 0, 500, 2000))
        inv.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(100, 100)))
        top.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans(0, 0))).set_property("undine_inst", "X1")
        layoutstore.write(lib, ly, {})
        return
    layoutstore.read(lib, ly)
    saved = layoutstore.snapshot(ly)
    edit_fn(ly)
    layoutstore.write(lib, ly, saved)


def add_box(*box: int):
    return lambda ly: ly.cell("inv").shapes(ly.layer(*MET1)).insert(kdb.Box(*box))


def add_via(name: str):
    """A via1 instance named `name` in inv, always at the same place: one added on each side is a warning (likely one
    device placed twice), not a conflict, where two shapes added that touch would be one."""
    def e(ly: kdb.Layout) -> None:
        inst = ly.cell("inv").insert(kdb.CellInstArray(ly.cell("via1").cell_index(), kdb.Trans(3000, 3000)))
        inst.set_property("undine_inst", name)
    return e


def boxes(path: Path, cell: str = "inv") -> list[str]:
    ly = kdb.Layout()
    ly.read(str(path))
    return sorted(s.to_s() for s in ly.cell(cell).shapes(ly.layer(*MET1)).each())


def comp(data: str, name: str) -> xschem.XComp:
    return next(c for c in xschem.parse(data).comps if c.props.get("name") == name)


@pytest.fixture
def prj(tmp_path) -> Path:
    return project(tmp_path / "prj")


def project(d: Path) -> Path:
    """A project in git: library l with inv (schematic, symbol, netlist, intent, layout with a child cell via1), buf,
    top above inv and buf, chain above buf, and the operation log; committed and tagged `base`."""
    write(d, "project.json", json.dumps({"name": "prj", "format": 3, "pdk": None, "libraries": {"l": "libs/l"}}))
    write(d, SCH, INV)
    write(d, "libs/l/inv.sym", INV_SYM)
    write(d, "libs/l/inv.netlist.yaml", nl(inverter()))
    write(d, "libs/l/inv.intent.yaml", ib(amp()))
    write(d, "libs/l/buf.sch", BUF)
    write(d, "libs/l/buf.sym", INV_SYM)
    write(d, "libs/l/top.sch", TOP)
    write(d, "libs/l/chain.sch", CHAIN)
    write(d, "history/l.jsonl", jsonl(*LOG))
    layout(d / "libs/l")
    git_setup(d)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "tag", "base")
    return d


def branch(d: Path, name: str, message: str, *edits) -> None:
    git(d, "checkout", "-q", "-b", name, "base")
    for e in edits:
        e(d)
    git(d, "commit", "-qam", message)


def test_kind_of():
    assert [gitmerge.kind_of(p) for p in ("libs/l/inv.sch", "libs/l/inv.sym", "libs/l/via%241.oas",
                                          "libs/l/inv.netlist.yaml", "libs/l/inv.intent.yaml", "history/l.jsonl",
                                          "prj/history/l.jsonl")] == \
        ["schematic", "symbol", "layout", "netlist", "intent", "log", "log"]
    assert [gitmerge.kind_of(p) for p in ("project.json", "undine.lock", "rtl/inv.v", "l.jsonl", "libs/l/x.yaml")] == \
        [None] * 5


def test_git_setup_registers_the_driver_and_is_idempotent(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    write(d, ".gitattributes", "*.oas diff=undine-layout\n")                # a project set up before the merge driver
    git_setup(d)
    git_setup(d)
    lines = text(d, ".gitattributes").splitlines()
    assert lines == GITATTRIBUTES and "history/*.jsonl merge=undine" in lines and "*.sch merge=undine" in lines
    driver, textconv = undine_command("merge-driver", "%O", "%A", "%B", "%P"), undine_command("textconv")
    virtual = undine_command("merge-driver", "--virtual-base", "%O", "%A", "%B", "%P")
    assert git(d, "config", "merge.undine.driver").stdout.strip() == driver
    assert git(d, "config", "merge.undine.name").stdout.strip()
    assert git(d, "config", "merge.undine.recursive").stdout.strip() == "undine-base"   # a criss-cross merge's bases
    assert git(d, "config", "merge.undine-base.driver").stdout.strip() == virtual
    assert git(d, "config", "merge.undine-base.name").stdout.strip()
    assert git(d, "config", "diff.undine-layout.textconv").stdout.strip() == textconv
    git(d, "config", "--add", "merge.undine.driver", "python -m undine merge-driver %O %A %B %P")   # old values,
    git(d, "config", "--add", "diff.undine-layout.textconv", "python -m undine textconv")          # twice: replaced
    git(d, "config", "--remove-section", "merge.undine-base")               # set up before the virtual-base driver
    git(d, "config", "--unset", "merge.undine.recursive")
    git_setup(d)
    assert git(d, "config", "--get-all", "merge.undine.driver").stdout.splitlines() == [driver]
    assert git(d, "config", "--get-all", "diff.undine-layout.textconv").stdout.splitlines() == [textconv]
    assert git(d, "config", "--get-all", "merge.undine.recursive").stdout.splitlines() == ["undine-base"]
    assert git(d, "config", "--get-all", "merge.undine-base.driver").stdout.splitlines() == [virtual]
    attrs = git(d, "check-attr", "merge", "--", "history/l.jsonl", "libs/l/inv.oas", "project.json").stdout
    assert attrs.splitlines() == ["history/l.jsonl: merge: undine", "libs/l/inv.oas: merge: undine",
                                  "project.json: merge: unspecified"]


def test_the_commands_git_runs_never_import_from_its_directory(tmp_path):
    """-P from Python 3.11; before, a -c prelude that takes the current directory off the path (-I would also drop the
    user's site-packages and PYTHONPATH, where Undine may be installed). The shapes this Python accepts run here: both
    from 3.11, the prelude alone on 3.10 (which has no -P)."""
    py = shlex.quote(sys.executable)
    assert undine_command("textconv", version=(3, 11)) == f"{py} -P -m undine textconv"
    old = undine_command("merge-driver", "%O", "%A", "%B", "%P", version=(3, 10))
    assert "-I" not in shlex.split(old)
    assert shlex.split(old) == [sys.executable, "-c", PRELUDE, "merge-driver", "%O", "%A", "%B", "%P"]
    assert PRELUDE.startswith("import sys; del sys.path[0]; ")
    write(tmp_path, "json.py", f"open({str(tmp_path / 'ran')!r}, 'w').write('ran')\n")    # what -m would import
    layout(tmp_path / "l")
    for version in [(3, 10), *([(3, 11)] if sys.version_info >= (3, 11) else [])]:
        cmd = undine_command("textconv", "l/inv.oas", version=version)
        r = subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True)   # as git runs it
        assert r.returncode == 0 and "cell inv" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "ran").exists()


def test_git_setup_installs_the_pre_commit_hook_unless_there_is_one(tmp_path, capsys):
    d = tmp_path / "p"
    d.mkdir()
    git_setup(d)
    hook = d / ".git/hooks/pre-commit"
    assert hook.read_text() == f"#!/bin/sh\n{HOOK_MARK}\nexec {undine_command('precommit')}\n"
    assert os.access(hook, os.X_OK)
    hook.write_text(f"#!/bin/sh\n{HOOK_MARK}\nexec /old/python -m undine precommit\n")    # ours, from before
    git_setup(d)
    assert undine_command("precommit") in hook.read_text()
    hook.write_text("#!/bin/sh\nmake lint\n")                                           # the project's own
    capsys.readouterr()
    git_setup(d)
    assert hook.read_text() == "#!/bin/sh\nmake lint\n"
    out = capsys.readouterr().out
    assert "the pre-commit hook is not installed" in out and undine_command("precommit") in out
    hook.unlink()
    git(d, "config", "core.hooksPath", str(tmp_path / "shared-hooks"))                  # hooks of all repositories
    git_setup(d)
    assert not hook.exists() and not (tmp_path / "shared-hooks").exists()
    out = capsys.readouterr().out
    assert "core.hooksPath" in out
    # it commits nothing, and says that the repository has no commit yet (an apply would be left uncommitted)
    assert git(d, "rev-parse", "-q", "--verify", "HEAD", ok=False).returncode != 0
    assert f"{d}: no commit yet: commit the project's files with git, or with Make the first commit" in out
    write(d, "project.json", "{}\n")
    git(d, "add", "project.json")
    git(d, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "first")
    git_setup(d)
    assert "no commit yet" not in capsys.readouterr().out


def test_git_setup_again_keeps_the_lines_added_to_its_hook(tmp_path):
    """Running git-setup again replaces the command of the line that runs `undine precommit` (the Python may have
    moved), whatever is around it, and keeps every other line of the hook; a hook whose line is gone gets it back
    after the mark, where the lines after it still run."""
    d = tmp_path / "p"
    d.mkdir()
    git_setup(d)
    hook, run = d / ".git/hooks/pre-commit", undine_command("precommit")
    hook.write_text(f'#!/bin/sh\n{HOOK_MARK}\nmake lint || exit 1\nexec /old/python -m undine precommit "$@"\n')
    git_setup(d)
    assert hook.read_text() == f'#!/bin/sh\n{HOOK_MARK}\nmake lint || exit 1\nexec {run} "$@"\n'
    py310 = undine_command("precommit", version=(3, 10)).replace(shlex.quote(sys.executable), "'/old dir/python3.10'")
    assert py310.startswith("'/old dir/python3.10' -c ")
    hook.write_text(f"#!/bin/sh\n{HOOK_MARK}\nif ! {py310}; then exit 1; fi\nmake lint\n")
    git_setup(d)
    assert hook.read_text() == f"#!/bin/sh\n{HOOK_MARK}\nif ! {run}; then exit 1; fi\nmake lint\n"
    hook.write_text(f"#!/bin/sh\n{HOOK_MARK}\nmake lint\n# was: exec undine precommit\n")
    git_setup(d)
    assert hook.read_text() == f"#!/bin/sh\n{HOOK_MARK}\n{run} || exit 1\nmake lint\n# was: exec undine precommit\n"
    assert os.access(hook, os.X_OK)
    git_setup(d)                                                            # idempotent
    assert hook.read_text().count(run) == 1


def test_files_of_the_repository_never_run_as_python_modules(tmp_path):
    """git runs the driver and textconv in the repository's top; a yaml.py or json.py committed there is not imported
    in place of PyYAML or json."""
    d = tmp_path / "prj"
    for m in ("yaml", "json"):
        write(d, f"{m}.py", f"open({str(tmp_path / (m + '.ran'))!r}, 'w').write('ran')\n")
    project(d)
    branch(d, "b", "b", edits_b)
    branch(d, "a", "a", edits_a)
    r = git(d, "merge", "--no-commit", "--no-ff", "b")
    assert "undine: libs/l/inv.intent.yaml: intent merged (1 from theirs)" in r.stderr, r.stderr
    assert item(gitmerge.yamlmerge.read_intent((d / "libs/l/inv.intent.yaml").read_bytes()), "c1")["text"] == \
        "M1 and M2 matched"
    assert "68/20  box (0,3000;100,3100)" in git(d, "show", "a", "--", "libs/l/inv.oas").stdout    # textconv ran
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prj"]                                  # neither module


def edits_a(d: Path) -> None:
    """Branch a: M1 wider (schematic and netlist), a metal box in inv, port inp moves north, an operation logged."""
    write(d, SCH, edit(INV, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=3"))
    m = inverter()
    m["instances"]["M1"]["props"]["W"] = "3"
    write(d, "libs/l/inv.netlist.yaml", nl(m))
    layout(d / "libs/l", add_box(0, 3000, 100, 3100))
    i = amp()
    i["ports"]["inp"]["side"] = "N"
    write(d, "libs/l/inv.intent.yaml", ib(i))
    write(d, "history/l.jsonl", jsonl(*LOG, rec(4, reason="widen M1", t="2026-10-23T11:00:00+0900")))


def edits_b(d: Path) -> None:
    """Branch b: M2 longer (schematic and netlist), another metal box in inv, item c1 reworded, buf changed (only here),
    an operation logged."""
    write(d, SCH, edit(INV, "name=M2\nL=0.15", "name=M2\nL=0.18"))
    m = inverter()
    m["instances"]["M2"]["props"]["L"] = "0.18"
    write(d, "libs/l/inv.netlist.yaml", nl(m))
    layout(d / "libs/l", add_box(5000, 0, 5100, 100))
    i = amp()
    item(i, "c1")["text"] = "M1 and M2 matched"
    write(d, "libs/l/inv.intent.yaml", ib(i))
    write(d, "libs/l/buf.sch", edit(BUF, "W=1", "W=2"))
    write(d, "history/l.jsonl", jsonl(*LOG, rec(4, cell="inv", view="layout", op="add_box", actor="agent:route",
                                                t="2026-10-23T10:30:00+0900"), save(5, "2026-10-23T10:31:00+0900")))


def test_merge_combines_both_branches_then_check_and_diff(prj):
    branch(prj, "b", "b", edits_b)
    branch(prj, "a", "a", edits_a)
    r = git(prj, "merge", "--no-commit", "--no-ff", "b")
    said = dict(line.split(": ", 2)[1:] for line in r.stderr.splitlines() if line.startswith("undine: "))
    assert said == {SCH: "schematic merged (1 from theirs)", "libs/l/inv.oas": "layout merged (1 from theirs)",
                    "libs/l/inv.netlist.yaml": "netlist merged (1 from theirs)",
                    "libs/l/inv.intent.yaml": "intent merged (1 from theirs)",
                    "history/l.jsonl": "log merged (1 from theirs)"}, r.stderr

    sch = text(prj, SCH)                                                    # both transistors' changes
    assert comp(sch, "M1").props["W"] == "3" and comp(sch, "M2").props["L"] == "0.18"
    assert comp(sch, "M1").props["L"] == "0.15" and comp(sch, "M2").props["W"] == "1"
    assert boxes(prj / "libs/l/inv.oas") == ["box (0,0;500,2000)", "box (0,3000;100,3100)", "box (5000,0;5100,100)"]
    ly = kdb.Layout()
    ly.read(str(prj / "libs/l/inv.oas"))
    assert [c.name for c in ly.each_cell() if c.is_ghost_cell()] == ["via1"]   # still a cell file with its child
    nlist = gitmerge.yamlmerge.read_netlist((prj / "libs/l/inv.netlist.yaml").read_bytes())
    assert nlist["instances"]["M1"]["props"]["W"] == "3" and nlist["instances"]["M2"]["props"]["L"] == "0.18"
    it = gitmerge.yamlmerge.read_intent((prj / "libs/l/inv.intent.yaml").read_bytes())
    assert it["ports"]["inp"]["side"] == "N" and item(it, "c1")["text"] == "M1 and M2 matched"
    log = [json.loads(x) for x in text(prj, "history/l.jsonl").splitlines()]
    assert [(r["seq"], r["op"], r.get("merged_seq")) for r in log] == \
        [(1, "set_props", None), (2, "create", None), (3, "save", None), (4, "set_props", None), (5, "add_box", 4),
         (6, "save", 5), (7, "merge", None)]
    assert "replay" not in log[-1]                                          # both sides saved: no journal pending
    assert text(prj, "libs/l/buf.sch") == edit(BUF, "W=1", "W=2")           # one side's file: git took it

    report = json.loads((prj / ".git/undine-merge/libs/l/inv.sch.json").read_text())
    assert report["path"] == SCH and report["kind"] == "schematic" and report["clean"] and report["conflicts"] == []
    assert report["combined"] and report["prefer"] == {} and set(report["blobs"]) == {"base", "ours", "theirs"}
    assert report["blobs"]["ours"] == git(prj, "rev-parse", f"HEAD:{SCH}").stdout.strip()

    check = gitmerge.merge_check(prj)                                       # the merge in progress
    assert check["commit"] is None and check["unmerged"] == []
    assert check["theirs"] == git(prj, "rev-parse", "b").stdout.strip()
    assert check["merged"] == ["history/l.jsonl", "libs/l/inv.intent.yaml", "libs/l/inv.netlist.yaml",
                               "libs/l/inv.oas", "libs/l/inv.sch"]
    # inv's own files were merged, top is above it; buf (b's alone), chain (above buf only), via1 (unchanged) not
    assert [(v["lib"], v["cell"]) for v in check["verify"]] == [("l", "inv"), ("l", "top")]
    why = {v["cell"]: v["why"] for v in check["verify"]}
    assert why["inv"] == "its views were merged"
    assert why["top"] == "its views are ours'; changed below it: l/buf (theirs'), l/inv"
    reason = {v["cell"]: v["reason"] for v in check["verify"]}             # the same, for the UI's own words
    assert reason == {"inv": {"kind": "merged"},
                      "top": {"kind": "side", "side": "ours", "below": [{"lib": "l", "cell": "buf", "side": "theirs"},
                                                                         {"lib": "l", "cell": "inv", "side": None}]}}
    assert check["notes"] == [] and check["note_codes"] == []

    git(prj, "commit", "-q", "--no-edit")
    with pytest.raises(ValueError, match="no merge in progress"):
        gitmerge.merge_check(prj)
    after = gitmerge.merge_check(prj, "HEAD")
    assert after["commit"] == git(prj, "rev-parse", "HEAD").stdout.strip()
    assert after["verify"] == check["verify"] and after["merged"] == check["merged"]
    with pytest.raises(ValueError, match="not a merge commit"):
        gitmerge.merge_check(prj, "HEAD^1")
    out = undine(prj, "merge-check", "--commit", "HEAD")
    assert out.returncode == 0 and "l/inv  its views were merged" in out.stdout, out.stdout + out.stderr

    changes = gitmerge.diff_revs(prj, "base", "HEAD")                      # base -> merged, by identity
    assert sorted(changes) == ["history/l.jsonl", "libs/l/buf.sch", "libs/l/inv.intent.yaml", "libs/l/inv.netlist.yaml",
                               "libs/l/inv.oas", "libs/l/inv.sch"]
    sch_ids = {(c["do"], c["id"]) for c in changes[SCH]["changes"]}
    assert sch_ids == {("mod", "inst:M1:prop:W"), ("mod", "inst:M2:prop:L")}
    assert changes["libs/l/inv.oas"]["kind"] == "layout"
    assert [c["id"] for c in changes["libs/l/inv.oas"]["changes"]] == ["shape:inv:68/20"]
    assert [c["id"] for c in changes["history/l.jsonl"]["changes"]] == ["seq:4", "seq:5", "seq:6", "seq:7"]
    assert {c["id"] for c in changes["libs/l/inv.intent.yaml"]["changes"]} == {"port:inp:side", "item:c1:text"}
    assert gitmerge.diff_revs(prj, "HEAD") == {}                             # the working tree is the merge commit
    out = undine(prj, "diff", "--rev", "base", "HEAD")
    assert out.returncode == 0 and "~ instance M1: W: 2 -> 3" in out.stdout, out.stdout + out.stderr
    assert json.loads(undine(prj, "diff", "--rev", "a", "--json").stdout) == gitmerge.diff_revs(prj, "a")


def test_diff_against_the_working_tree_has_its_untracked_files(prj):
    """diff --rev A compares the working tree, files not added yet included (a cell drawn since); not the files git
    ignores (job results), nor a file taken out of the index that is as A has it."""
    write(prj, SCH, edit(INV, "W=2", "W=4"))
    write(prj, "libs/l/new.sch", BUF)                                       # untracked
    write(prj, "runs/sim/out.sch", BUF)                                     # ignored
    git(prj, "rm", "-q", "--cached", "libs/l/buf.sch")                      # untracked, unchanged
    changes = gitmerge.diff_revs(prj, "base")
    assert sorted(changes) == [SCH, "libs/l/new.sch"]
    assert changes["libs/l/new.sch"]["kind"] == "schematic" and changes["libs/l/new.sch"]["changes"]
    assert {c["do"] for c in changes["libs/l/new.sch"]["changes"]} == {"add"}
    out = undine(prj / "libs", "diff", "--rev", "base", "--dir", "..")
    assert out.returncode == 0 and "libs/l/new.sch  (schematic)" in out.stdout.splitlines(), out.stdout + out.stderr
    assert gitmerge.diff_revs(prj, "base", "HEAD") == {}                   # two revisions: not the working tree


def test_a_project_in_a_subdirectory_of_its_repository(tmp_path):
    repo = tmp_path / "repo"
    write(repo, "README", "designs\n")
    git(repo, "init", "-q")
    d = project(repo / "designs" / "prj")                                   # git-setup uses the repository it is in
    config = (repo / ".git/config").read_text()                             # the driver, the virtual-base one
    assert not (d / ".git").exists() and config.count("merge-driver %O") == config.count("--virtual-base %O") == 1
    branch(d, "b", "b", edits_b)
    branch(d, "a", "a", edits_a)
    git(d, "merge", "--no-commit", "--no-ff", "b")
    p = "designs/prj/"
    assert comp(text(d, SCH), "M1").props["W"] == "3" and comp(text(d, SCH), "M2").props["L"] == "0.18"
    check = gitmerge.merge_check(d)
    assert check["merged"] == [p + x for x in ("history/l.jsonl", "libs/l/inv.intent.yaml", "libs/l/inv.netlist.yaml",
                                               "libs/l/inv.oas", "libs/l/inv.sch")]
    assert [(v["lib"], v["cell"]) for v in check["verify"]] == [("l", "inv"), ("l", "top")]
    assert sorted(gitmerge.diff_revs(d, "base", "b")) == [p + x for x in (
        "history/l.jsonl", "libs/l/buf.sch", "libs/l/inv.intent.yaml", "libs/l/inv.netlist.yaml", "libs/l/inv.oas",
        "libs/l/inv.sch")]
    assert gitmerge.diff_revs(repo, "base", "b")[p + SCH]["kind"] == "schematic"
    with pytest.raises(ValueError, match="no project.json"):
        gitmerge.merge_check(repo)


def test_resolve_in_a_project_in_a_subdirectory(tmp_path):
    """The driver prints paths from the repository's top: resolve takes them from the project's directory too, and
    prints the `git add` that works from anywhere."""
    repo = tmp_path / "repo"
    write(repo, "README", "designs\n")
    git(repo, "init", "-q")
    d = project(repo / "designs" / "prj")
    top = f"designs/prj/{SCH}"
    branch(d, "d", "M2 down by 40", move(100, via=False))
    branch(d, "c", "M2 down by 20", move(80, via=False))
    r = git(d, "merge", "--no-edit", "d", ok=False)
    assert f"`undine resolve {top} --theirs ID`" in r.stderr, r.stderr
    assert gitmerge.locate(top, cwd=d) == gitmerge.locate("l/inv.sch", cwd=d / "libs") == top
    assert gitmerge.locate(f":/{top}", cwd=d / "libs") == top
    with pytest.raises(ValueError, match="not a conflicted file"):
        gitmerge.locate(SCH, cwd=repo)                                      # from the top, that is no file
    out = undine(d, "resolve", top, "--theirs", "inst:M2:placement")      # what the driver printed, from the project
    assert out.returncode == 0 and f"`git add :/{top}` marks it resolved" in out.stdout, out.stdout + out.stderr
    assert comp(text(d, SCH), "M2").y == 100
    out = undine(d / "libs", "resolve", f":/{top}", "--ours", "inst:M2:placement")
    assert out.returncode == 0 and comp(text(d, SCH), "M2").y == 80, out.stdout + out.stderr
    git(d / "libs", "add", f":/{top}")                                      # the hint, from yet another directory
    assert gitmerge.merge_check(d)["unmerged"] == []


def move(y: int, via: bool = True):
    """M2 to y in the schematic, and (via) a via1 instance in inv's layout at the place the other y's puts its own."""
    def e(d: Path) -> None:
        write(d, SCH, edit(INV, "nfet_01v8.sym} -20 60 0 0", f"nfet_01v8.sym}} -20 {y} 0 0"))
        if via:
            layout(d / "libs/l", add_via(f"V{y}"))
    return e


def test_conflict_is_reported_resolved_with_theirs_and_checked(prj):
    branch(prj, "d", "M2 down by 40", move(100))
    branch(prj, "c", "M2 down by 20", move(80))
    r = git(prj, "merge", "--no-edit", "d", ok=False)
    assert r.returncode == 1 and f"CONFLICT (content): Merge conflict in {SCH}" in r.stdout, r.stdout + r.stderr
    assert "inst:M2:placement" in r.stderr
    assert f"see `undine merge-check`, then `undine resolve {SCH} --theirs ID`" in r.stderr
    assert comp(text(prj, SCH), "M2").y == 80                               # ours kept for the conflict
    report = json.loads((prj / f".git/undine-merge/{SCH}.json").read_text())
    assert not report["clean"] and [c["id"] for c in report["conflicts"]] == ["inst:M2:placement"]
    c = report["conflicts"][0]
    assert (c["ours"]["y"], c["theirs"]["y"], c["base"]["y"], c["taken"]) == (80, 100, 60, "ours")

    check = gitmerge.merge_check(prj)                                       # vias at one place: merged, a warning
    assert [(u["path"], [x["id"] for x in u["conflicts"]]) for u in check["unmerged"]] == [(SCH, ["inst:M2:placement"])]
    assert [w["path"] for w in check["warnings"]] == ["libs/l/inv.oas"]
    assert "same place" in check["warnings"][0]["warning"]
    assert [(v["lib"], v["cell"]) for v in check["verify"]] == [("l", "inv"), ("l", "top")]
    out = undine(prj, "merge-check", "--json")
    assert out.returncode == 1 and json.loads(out.stdout) == check

    with pytest.raises(ValueError, match="no conflict inst:M9:placement"):
        gitmerge.resolve(SCH, theirs=["inst:M9:placement"], cwd=prj)
    out = undine(prj / "libs", "resolve", "l/inv.sch", "--theirs", "inst:M2:placement")   # a path from where one is
    assert out.returncode == 0 and "no conflict open" in out.stdout, out.stdout + out.stderr
    assert comp(text(prj, SCH), "M2").y == 100
    report = json.loads((prj / f".git/undine-merge/{SCH}.json").read_text())
    assert report["clean"] and report["prefer"] == {"inst:M2:placement": "theirs"}
    assert report["conflicts"][0]["taken"] == "theirs" and report["conflicts"][0]["resolved"]
    assert gitmerge.resolve(SCH, cwd=prj) == [] and comp(text(prj, SCH), "M2").y == 100   # decisions accumulate
    assert gitmerge.merge_check(prj)["unmerged"] == [{"path": SCH, "kind": "schematic", "conflicts": []}]

    git(prj, "add", SCH)
    git(prj, "commit", "-q", "--no-edit")
    after = gitmerge.merge_check(prj, "HEAD")
    assert after["unmerged"] == [] and after["warnings"] == check["warnings"]
    assert [(v["lib"], v["cell"]) for v in after["verify"]] == [("l", "inv"), ("l", "top")]
    with pytest.raises(ValueError, match="not a conflicted file"):
        gitmerge.resolve(SCH, cwd=prj)


def test_a_criss_cross_merge_stops_where_its_merge_bases_conflict(prj):
    """a and b each merged the other's first commit and resolved the same conflict their own way (M2 at 80 on a, at 100
    on b): merging them again has two merge bases, which git first merges into one with the driver
    merge.undine.recursive names. That merge takes the bases' own base for its conflict, so the merge sees both
    resolutions as changes and stops on them; with ours kept there, it would take one of them without a word."""
    def rev(x: str) -> str:
        return git(prj, "rev-parse", x).stdout.strip()

    def conclude(theirs: str) -> None:                  # each keeps its own M2
        assert git(prj, "merge", "--no-edit", theirs, ok=False).returncode == 1
        assert gitmerge.resolve(SCH, ours=["inst:M2:placement"], cwd=prj) == []
        git(prj, "add", SCH)
        git(prj, "commit", "-q", "--no-edit")
    branch(prj, "b", "b1: M2 down by 40", move(100, via=False))
    branch(prj, "a", "a1: M2 down by 20", move(80, via=False))
    a1 = rev("a")
    conclude("b")
    git(prj, "checkout", "-q", "b")
    conclude(a1)
    assert (comp(text(prj, SCH), "M2").y, comp(git(prj, "show", f"a:{SCH}").stdout, "M2").y) == (100, 80)
    git(prj, "checkout", "-q", "a")
    assert len(git(prj, "merge-base", "--all", "a", "b").stdout.split()) == 2
    r = git(prj, "merge", "--no-edit", "b", ok=False)
    assert r.returncode == 1 and f"CONFLICT (content): Merge conflict in {SCH}" in r.stdout, r.stdout + r.stderr
    assert "merged against the base they share" not in r.stderr          # the conflict alone took the base's value
    [c] = json.loads((prj / f".git/undine-merge/{SCH}.json").read_text())["conflicts"]   # the merge's own report
    assert (c["id"], c["ours"]["y"], c["theirs"]["y"], c["base"]["y"]) == ("inst:M2:placement", 80, 100, 60)
    check = gitmerge.merge_check(prj)
    assert [(u["path"], [x["id"] for x in u["conflicts"]]) for u in check["unmerged"]] == [(SCH, ["inst:M2:placement"])]
    assert gitmerge.resolve(SCH, theirs=["inst:M2:placement"], cwd=prj) == [] and comp(text(prj, SCH), "M2").y == 100


def test_the_virtual_base_driver(tmp_path, capsys):
    """`merge-driver --virtual-base` (the merge of a criss-cross merge's bases): the merged file, each conflict taking
    the bases' own base's value; the base whole when a view cannot take it or the merge fails (empty without one);
    exit 0, no report."""
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q")

    def run(kind_path: str, base: str | bytes, ours: str | bytes, theirs: str | bytes) -> bytes:
        for name, data in (("base", base), ("ours", ours), ("theirs", theirs)):
            write(d, name, data)
        assert gitmerge.driver("base", "ours", "theirs", kind_path, cwd=d, virtual_base=True) == 0
        return (d / "ours").read_bytes()
    m2_80 = edit(INV, "nfet_01v8.sym} -20 60 0 0", "nfet_01v8.sym} -20 80 0 0")
    m2_100 = edit(INV, "nfet_01v8.sym} -20 60 0 0", "nfet_01v8.sym} -20 100 0 0")
    merged = run(SCH, INV, edit(INV, "W=2", "W=4"), edit(INV, "name=M2\nL=0.15", "name=M2\nL=0.18")).decode()
    assert comp(merged, "M1").props["W"] == "4" and comp(merged, "M2").props["L"] == "0.18"     # clean: merged
    assert capsys.readouterr().err == ""
    got = run(SCH, INV, edit(m2_80, "W=2", "W=4"), m2_100).decode()          # a conflict: the base's value there,
    assert comp(got, "M2").y == 60 and comp(got, "M1").props["W"] == "4"     # not ours; the clean change kept
    assert capsys.readouterr().err == ""
    two = "C {sky130_fd_pr/nfet_01v8.sym} 200 0 0 0 {name=M9\n"              # one name added twice: no base value
    got = run(SCH, INV, INV + two + "W=1}\n", INV + two + "W=2}\n")
    assert got == INV.encode() and "merged against the base they share" in capsys.readouterr().err
    assert run(SCH, b"", m2_80, m2_100) == b""                              # no base: empty
    assert run("libs/l/x.oas", b"the base", b"not an OASIS file", b"nor this") == b"the base"     # an error
    assert "RuntimeError" in capsys.readouterr().err
    assert run("notes.txt", "a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n") == b"A\nb\nC\n"                # by lines
    assert run("notes.txt", "a\n", "A\n", "B\n") == b"a\n"
    assert not (d / ".git/undine-merge").exists()                           # no report: the merge's is what counts
    for name, data in (("base", INV), ("ours", m2_80), ("theirs", m2_100)):
        write(d, name, data)
    out = undine(d, "merge-driver", "--virtual-base", "base", "ours", "theirs", SCH)   # as git runs it
    assert out.returncode == 0 and comp(text(d, "ours"), "M2").y == 60, out.stdout + out.stderr


def test_the_driver_says_what_its_one_conflict_leaves(prj):
    """With one open conflict the driver prints that conflict's own words rather than "ours kept": the log's
    layout-journal conflict does not keep ours' unsaved edits, the merged log drops them."""
    git(prj, "checkout", "-q", "-b", "x", "base")
    write(prj, "history/l.jsonl", jsonl(*LOG, rec(4, cell="inv", view="layout", op="add_box",
                                                  t="2026-10-24T09:00:00+0900")))
    git(prj, "commit", "--no-verify", "-qam", "x: an unsaved layout edit")
    branch(prj, "y", "y: a saved layout edit", lambda d: write(d, "history/l.jsonl", jsonl(
        *LOG, rec(4, cell="top", view="layout", op="add_box"), save(5, "2026-10-24T10:00:00+0900"))))
    r = git(prj, "merge", "--no-edit", "x", ok=False)
    assert r.returncode == 1, r.stdout + r.stderr
    [c] = json.loads((prj / ".git/undine-merge/history/l.jsonl.json").read_text())["conflicts"]
    assert c["id"] == logmerge.JOURNAL
    said = [x for x in r.stderr.splitlines() if x.startswith("undine: history/l.jsonl: ")]
    assert said == [f"undine: history/l.jsonl: log, 1 conflict (layout-journal): {c['what']}; see `undine "
                    "merge-check`, then `undine resolve history/l.jsonl --theirs ID`"], r.stderr


@pytest.mark.parametrize("renamed_on", ["ours", "theirs"])
def test_a_file_renamed_on_one_side_keeps_its_report(prj, renamed_on):
    """git merges a file renamed on one side under its new name, which the other side's tree lacks: the report still
    belongs to the merge (its warnings are shown), and the merge commit has a report for it."""
    oas, moved = "libs/l/inv.oas", "libs/l/inv_v2.oas"

    def rename(d: Path) -> None:                                            # an edit too: both sides changed it
        layout(d / "libs/l", add_via("V1"))
        git(d, "mv", oas, moved)

    def same_place(d: Path) -> None:                                         # a warning with the other side's via
        layout(d / "libs/l", add_via("V2"))
    branch(prj, "d", "d", rename if renamed_on == "theirs" else same_place)
    branch(prj, "c", "c", rename if renamed_on == "ours" else same_place)
    # a small cell file: an added instance changes much of it, more than git's rename detection takes by default
    r = git(prj, "merge", "--no-commit", "--no-ff", "-X", "find-renames=20%", "d")
    assert f"undine: {moved}: layout merged (1 from theirs, 1 warning)" in r.stderr, r.stdout + r.stderr
    check = gitmerge.merge_check(prj)
    assert check["merged"] == [moved] and [w["path"] for w in check["warnings"]] == [moved]
    git(prj, "commit", "-q", "--no-edit")
    after = gitmerge.merge_check(prj, "HEAD")
    assert (after["merged"], after["no_report"], after["warnings"]) == ([moved], [], check["warnings"])


def test_merge_check_of_an_octopus_merge(prj):
    """Every parent of an octopus merge is a side: a file or a cell as one of them has it needs no report and no
    verification. The octopus strategy runs no merge driver: a file it combined has no report."""
    def rev(x: str) -> str:
        return git(prj, "rev-parse", x).stdout.strip()

    def solo(d: Path) -> None:
        write(d, "libs/l/solo.sch", edit(BUF, "T {buffer stage}", "T {a cell of its own}"))
        git(d, "add", "libs/l/solo.sch")
    branch(prj, "b1", "buf wider", lambda d: write(d, "libs/l/buf.sch", edit(BUF, "W=1", "W=2")))
    branch(prj, "b2", "M1 wider", lambda d: write(d, SCH, edit(INV, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=3")))
    branch(prj, "b3", "a cell of its own", solo)
    b1, b2, b3 = rev("b1"), rev("b2"), rev("b3")
    git(prj, "checkout", "-q", "b1")
    assert "octopus" in git(prj, "merge", "--no-edit", "b2", "b3").stdout
    check = gitmerge.merge_check(prj, "HEAD")
    assert (check["parents"], check["ours"], check["theirs"]) == ([b1, b2, b3], b1, b2)
    assert (check["no_report"], check["notes"], check["warnings"]) == ([], [], [])     # solo.sch is b3's
    assert check["verify"] == [{"lib": "l", "cell": "top", "why": "its views are ours'; changed below it: "
                                                                  "l/inv (parent 2's)",   # not solo: b3's
                                "reason": {"kind": "side", "side": "ours",
                                           "below": [{"lib": "l", "cell": "inv", "side": "parent 2"}]}}]
    assert gitmerge.format_check(check).startswith(f"merge commit {rev('HEAD')[:12]}: ours {b1[:12]}, "
                                                   f"theirs {b2[:12]}, {b3[:12]}\n")

    branch(prj, "b4", "M2 longer", lambda d: write(d, SCH, edit(INV, "name=M2\nL=0.15", "name=M2\nL=0.18")))
    git(prj, "checkout", "-q", "b3")
    git(prj, "merge", "--no-edit", "b2", "b4")                              # inv.sch changed on two: by lines
    check = gitmerge.merge_check(prj, "HEAD")
    assert check["no_report"] == [SCH] and check["notes"] == [gitmerge._OCTOPUS]
    assert check["note_codes"] == [{"code": "octopus"}]
    assert [(v["cell"], v["why"]) for v in check["verify"]] == [
        ("inv", "its views were merged"), ("top", "its views are ours'; changed below it: l/inv")]


M1_W3 = ("name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=3")


def test_a_cell_whose_instances_resolve_to_other_cells(tmp_path):
    """A cell as one side has it, the cells below as that side has them, but its symbol resolves to another cell: theirs
    removed l/x.sym, so top's x.sym is m's now. Listed, the reason says so."""
    d = tmp_path / "p"
    write(d, "project.json", json.dumps({"name": "p", "libraries": {"l": "libs/l", "m": "libs/m"}}))
    write(d, "libs/l/x.sym", INV_SYM)
    write(d, "libs/m/x.sym", INV_SYM)
    write(d, "libs/l/top.sch", HEADER + "C {x.sym} 0 0 0 0 {name=x1}\n")
    git(d, "init", "-q")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "checkout", "-q", "-b", "t")
    git(d, "rm", "-q", "libs/l/x.sym")
    git(d, "commit", "-qm", "no l/x")
    git(d, "checkout", "-q", "-b", "o", "HEAD~1")
    write(d, "libs/l/top.sch", HEADER + "C {x.sym} 100 0 0 0 {name=x1}\n")
    git(d, "commit", "-qam", "x1 moved")
    git(d, "merge", "-q", "--no-edit", "t")
    check = gitmerge.merge_check(d, "HEAD")
    assert check["verify"] == [{"lib": "l", "cell": "top",
                                "why": "its views are ours'; what it instantiates resolves differently",
                                "reason": {"kind": "resolves", "side": "ours"}}]


def test_an_octopus_merge_takes_no_report_of_another_merge(prj):
    """A two-way merge of two of an octopus merge's branches, aborted, leaves a report with the blobs of the octopus
    merge's sides; git's octopus strategy merged the file by lines all the same: no report is the octopus merge's."""
    branch(prj, "b2", "M1 wider", lambda d: write(d, SCH, edit(INV, *M1_W3)))
    branch(prj, "b3", "buf wider", lambda d: write(d, "libs/l/buf.sch", edit(BUF, "W=1", "W=2")))
    branch(prj, "b4", "M2 longer", lambda d: write(d, SCH, edit(INV, "name=M2\nL=0.15", "name=M2\nL=0.18")))
    git(prj, "checkout", "-q", "b2")
    r = git(prj, "merge", "--no-commit", "--no-ff", "b4")
    assert f"undine: {SCH}: schematic merged" in r.stderr, r.stderr
    git(prj, "merge", "--abort")
    assert (prj / f".git/undine-merge/{SCH}.json").is_file()               # left behind
    assert "octopus" in git(prj, "merge", "--no-edit", "b3", "b4").stdout   # inv.sch: b2's and b4's, by lines
    check = gitmerge.merge_check(prj, "HEAD")
    assert (check["merged"], check["warnings"], check["no_report"]) == ([], [], [SCH])
    assert check["notes"] == [gitmerge._OCTOPUS]


def test_merge_check_of_an_octopus_merge_in_progress(prj):
    """git's octopus strategy stops at a conflict of its last branch, with every branch in MERGE_HEAD: each is a side
    (a cell as one of them has it is not to verify), the conflicted file is git's (no driver ran) until `undine
    resolve` merges it, and a note says that files changed on several branches were merged by lines."""
    def rev(x: str) -> str:
        return git(prj, "rev-parse", x).stdout.strip()

    def b5(d: Path) -> None:
        write(d, SCH, edit(INV, M1_W3[0], "name=M1\nL=0.15\nW=4"))
        write(d, "libs/l/solo.sch", edit(BUF, "T {buffer stage}", "T {a cell of its own}"))
        git(d, "add", "libs/l/solo.sch")
    branch(prj, "b1", "buf wider", lambda d: write(d, "libs/l/buf.sch", edit(BUF, "W=1", "W=2")))
    branch(prj, "b2", "M1 wider", lambda d: write(d, SCH, edit(INV, *M1_W3)))
    branch(prj, "b5", "M1 wider still, and a cell of its own", b5)
    b1, b2, b5_ = rev("b1"), rev("b2"), rev("b5")
    git(prj, "checkout", "-q", "b1")
    r = git(prj, "merge", "--no-edit", "b2", "b5", ok=False)
    assert r.returncode == 1 and "Automatic merge failed" in r.stdout, r.stdout + r.stderr
    assert (prj / ".git/MERGE_HEAD").read_text().split() == [b2, b5_]
    check = gitmerge.merge_check(prj)
    assert (check["operation"], check["ours"], check["theirs"], check["parents"]) == ("merge", b1, b2, [b1, b2, b5_])
    assert check["notes"] == [gitmerge._OCTOPUS] and check["note_codes"] == [{"code": "octopus"}]
    assert (check["unmerged"], check["merged"]) == ([{"path": SCH, "kind": "schematic", "conflicts": None}], [])
    assert [(v["cell"], v["why"]) for v in check["verify"]] == [                # not solo: b5's, nor chain: b1's
        ("inv", "its views were merged"), ("top", "its views are ours'; changed below it: l/inv")]
    assert gitmerge.format_check(check).startswith(f"merge in progress: ours {b1[:12]}, theirs {b2[:12]}, "
                                                   f"{b5_[:12]}\nnote: {gitmerge._OCTOPUS}\n")
    assert gitmerge.resolve(SCH, theirs=["inst:M1:prop:W"], cwd=prj) == []  # its report has the stages: this merge's
    assert comp(text(prj, SCH), "M1").props["W"] == "4"
    check = gitmerge.merge_check(prj)
    assert (check["unmerged"], check["merged"]) == ([{"path": SCH, "kind": "schematic", "conflicts": []}], [SCH])


def test_merge_check_of_a_commit_whose_report_is_not_here(prj, tmp_path):
    """Reports are local: a merge commit checked in another clone lists the files it combined without one (their
    warnings unknown); the cells to verify come from the commit alone. A later merge of the same file keeps the
    earlier merge's report."""
    oas = "libs/l/inv.oas"
    branch(prj, "d", "d", lambda d: layout(d / "libs/l", add_via("V1")))
    branch(prj, "c", "c", lambda d: layout(d / "libs/l", add_via("V2")))
    git(prj, "merge", "--no-edit", "d")                                     # clean, vias at one place: a warning
    merge = git(prj, "rev-parse", "HEAD").stdout.strip()
    here = gitmerge.merge_check(prj, merge)
    assert [w["path"] for w in here["warnings"]] == [oas] and here["merged"] == [oas] and here["no_report"] == []
    assert [(v["lib"], v["cell"]) for v in here["verify"]] == [("l", "inv"), ("l", "top")]

    git(tmp_path, "clone", "-q", str(prj), "clone")                         # another clone: no report
    there = gitmerge.merge_check(tmp_path / "clone", merge)
    assert (there["warnings"], there["merged"], there["no_report"]) == ([], [], [oas])
    assert there["verify"] == here["verify"]
    assert f"no merge report for {oas} (merged in another clone or by git's octopus strategy, or the report is " \
           "gone): its warnings are unknown" in gitmerge.format_check(there).splitlines()

    git(prj, "checkout", "-q", "-b", "f", merge)                            # a later merge of the same file
    layout(prj / "libs/l", add_box(9000, 0, 9100, 100))
    git(prj, "commit", "-qam", "f")
    git(prj, "checkout", "-q", "-b", "e", merge)
    layout(prj / "libs/l", add_box(0, 9000, 100, 9100))
    git(prj, "commit", "-qam", "e")
    git(prj, "merge", "--no-edit", "f")
    later = git(prj, "rev-parse", "HEAD").stdout.strip()
    assert gitmerge.merge_check(prj, later)["merged"] == [oas]
    assert gitmerge.merge_check(prj, merge) == here                         # its own report, kept

    # the same ours and theirs, but another base: not this merge's report
    paths = sorted((prj / ".git/undine-merge/libs/l").glob("inv.oas*.json"))
    assert len(paths) == 3                                                  # the latest, and each merge's own
    for path in paths:
        report = json.loads(path.read_text())
        report["blobs"]["base"] = git(prj, "rev-parse", f"base:{oas}").stdout.strip()
        path.write_text(json.dumps(report))
    assert gitmerge.merge_check(prj, later)["no_report"] == [oas]


def test_every_merge_of_a_file_keeps_its_report(prj):
    """The operation log is merged by almost every merge: each merge's report is kept (named by its blobs), so an
    earlier merge commit still has its own, and resolve reads the report of the merge in progress."""
    def logged(*recs):
        return lambda d: write(d, "history/l.jsonl", jsonl(*recs))
    r4 = [rec(4, reason="x: wider", t="2026-10-24T09:00:00+0900"), rec(4, reason="y: longer",
                                                                         t="2026-10-24T09:30:00+0900")]
    branch(prj, "x", "x", logged(*LOG, r4[0]))
    branch(prj, "y", "y", logged(*LOG, r4[1]))
    r = git(prj, "merge", "--no-edit", "x")
    assert "undine: history/l.jsonl: log merged" in r.stderr, r.stderr
    first = git(prj, "rev-parse", "HEAD").stdout.strip()
    merged = text(prj, "history/l.jsonl")
    for name, why in (("z", "z: again"), ("w", "w: again")):              # both log once more from the merge
        git(prj, "checkout", "-q", "-b", name, first)
        write(prj, "history/l.jsonl", merged + json.dumps(rec(9, reason=why)) + "\n")
        git(prj, "commit", "-qam", name)
    r = git(prj, "merge", "--no-edit", "z")
    assert "undine: history/l.jsonl: log merged" in r.stderr, r.stderr
    second = git(prj, "rev-parse", "HEAD").stdout.strip()
    for c in (first, second):
        check = gitmerge.merge_check(prj, c)
        assert (check["merged"], check["no_report"]) == (["history/l.jsonl"], []), c
    reports = sorted(p.name for p in (prj / ".git/undine-merge/history").iterdir())
    assert len(reports) == 3 and "l.jsonl.json" in reports                  # the latest, and each merge's own
    latest = json.loads((prj / ".git/undine-merge/history/l.jsonl.json").read_text())
    assert latest["blobs"]["ours"] == git(prj, "rev-parse", f"{second}^1:history/l.jsonl").stdout.strip()



def test_cherry_pick_rebase_and_stash_pop(prj):
    """merge-check follows a cherry-pick (CHERRY_PICK_HEAD) and a rebase (REBASE_HEAD) like a merge, but not the
    REBASE_HEAD git leaves behind a finished rebase; a stash pop has no commit for theirs: the driver points to resolve
    only, merge-check shows the conflicted files alone."""
    branch(prj, "d", "M2 down by 40", move(100))
    branch(prj, "c", "M2 down by 20", move(80))
    c, d = (git(prj, "rev-parse", x).stdout.strip() for x in ("c", "d"))
    assert git(prj, "cherry-pick", "d", ok=False).returncode == 1
    check = gitmerge.merge_check(prj)
    assert (check["operation"], check["ours"], check["theirs"], check["notes"]) == ("cherry-pick", c, d, [])
    assert [(u["path"], [x["id"] for x in u["conflicts"]]) for u in check["unmerged"]] == [(SCH, ["inst:M2:placement"])]
    assert check["merged"] == ["libs/l/inv.oas", SCH] and [w["path"] for w in check["warnings"]] == ["libs/l/inv.oas"]
    assert gitmerge.format_check(check).startswith("cherry-pick in progress: ")
    git(prj, "cherry-pick", "--abort")

    r = git(prj, "rebase", "d", ok=False)                                   # c onto d
    assert r.returncode == 1 and "see `undine merge-check`" in r.stderr, r.stdout + r.stderr
    check = gitmerge.merge_check(prj)
    assert (check["operation"], check["ours"], check["theirs"], check["notes"]) == ("rebase", d, c, [])
    assert [(u["path"], [x["id"] for x in u["conflicts"]]) for u in check["unmerged"]] == [(SCH, ["inst:M2:placement"])]
    gitmerge.resolve(SCH, theirs=["inst:M2:placement"], cwd=prj)
    git(prj, "add", SCH)
    git(prj, "-c", "core.editor=true", "rebase", "--continue")
    rebased = prj / ".git/REBASE_HEAD"
    if not rebased.exists():                                                # left behind by git 2.34 at least
        rebased.write_text(c + "\n")
    with pytest.raises(ValueError, match="no merge in progress"):           # not a rebase in progress any more
        gitmerge.merge_check(prj)

    write(prj, SCH, edit(INV, "nfet_01v8.sym} -20 60 0 0", "nfet_01v8.sym} -20 120 0 0"))    # M2 to 120 on c
    git(prj, "stash", "-q")
    git(prj, "checkout", "-q", "d")
    r = git(prj, "stash", "pop", ok=False)
    assert r.returncode == 1 and f"`undine resolve {SCH} --theirs ID`" in r.stderr, r.stdout + r.stderr
    assert "merge-check" not in r.stderr
    check = gitmerge.merge_check(prj)                                       # the stale REBASE_HEAD is not theirs
    assert (check["operation"], check["ours"], check["theirs"], check["verify"]) == (None, d, None, [])
    assert [(u["path"], [x["id"] for x in u["conflicts"]]) for u in check["unmerged"]] == [(SCH, ["inst:M2:placement"])]
    assert check["notes"] == [gitmerge._NO_THEIRS]
    assert gitmerge.resolve(SCH, theirs=["inst:M2:placement"], cwd=prj) == [] and comp(text(prj, SCH), "M2").y == 120


def test_merge_squash_and_cherry_pick_no_commit(prj):
    """git merge --squash and cherry-pick -n write no head for theirs: the driver still points to merge-check (a merge
    is told by GITHEAD_*), which shows the conflicted files with their conflicts and warnings, and says that it cannot
    tell the cells to verify."""
    branch(prj, "d", "M2 down by 40", move(100))
    branch(prj, "c", "M2 down by 20", move(80))
    c = git(prj, "rev-parse", "c").stdout.strip()
    for cmd in (["merge", "--squash", "d"], ["cherry-pick", "-n", "d"]):
        r = git(prj, *cmd, ok=False)
        assert r.returncode == 1 and "inst:M2:placement" in r.stderr, r.stdout + r.stderr
        assert "see `undine merge-check`" in r.stderr or cmd[0] == "cherry-pick"    # newer git's pick: no sign
        check = gitmerge.merge_check(prj)
        assert (check["ours"], check["theirs"], check["commit"], check["operation"]) == (c, None, None, None)
        assert [(u["path"], [x["id"] for x in u["conflicts"]]) for u in check["unmerged"]] == \
            [(SCH, ["inst:M2:placement"])]
        assert check["merged"] == [SCH] and check["verify"] == [] and check["notes"] == [gitmerge._NO_THEIRS]
        assert check["note_codes"] == [{"code": "no_theirs"}]
        out = gitmerge.format_check(check).splitlines()
        assert out[0] == f"conflicts in the index: ours {c[:12]}, theirs unknown"
        assert out[1].startswith("note: no commit is known for theirs")
        assert out[-1] == "cells to verify: not known without a commit for theirs"
        cli = undine(prj, "merge-check", "--json")
        assert cli.returncode == 1 and json.loads(cli.stdout) == check, cli.stdout + cli.stderr
        git(prj, "reset", "-q", "--hard")
    with pytest.raises(ValueError, match="no merge in progress"):           # nothing conflicted: nothing to show
        gitmerge.merge_check(prj)


def test_merge_check_says_what_a_rebase_or_cherry_pick_of_several_commits_does_not_cover(prj):
    """merge-check sees the commit git stopped at: the commits applied before it are in ours already."""
    branch(prj, "d", "M2 down by 40", move(100))
    git(prj, "checkout", "-q", "-b", "c", "base")
    write(prj, "libs/l/buf.sch", edit(BUF, "W=1", "W=2"))                   # merges cleanly
    git(prj, "commit", "-qam", "buf wider")
    move(80)(prj)
    git(prj, "commit", "-qam", "M2 down by 20")                             # stops on M2
    c1, c2 = (git(prj, "rev-parse", x).stdout.strip() for x in ("c~1", "c"))
    note = "earlier commits of this {} are not covered: their warnings were printed when they were merged, and " \
           "the cells only they merged are not listed"

    git(prj, "rebase", "d", ok=False)
    check = gitmerge.merge_check(prj)
    assert check["operation"] == "rebase" and check["theirs"] == c2 and check["notes"] == [note.format("rebase")]
    assert check["note_codes"] == [{"code": "earlier", "operation": "rebase"}]
    assert f"note: {note.format('rebase')}" in gitmerge.format_check(check).splitlines()
    git(prj, "rebase", "--abort")

    git(prj, "checkout", "-q", "d")
    git(prj, "cherry-pick", c1, c2, ok=False)
    check = gitmerge.merge_check(prj)
    assert check["operation"] == "cherry-pick" and check["notes"] == [note.format("cherry-pick")]
    assert check["note_codes"] == [{"code": "earlier", "operation": "cherry-pick"}]
    git(prj, "cherry-pick", "--abort")
    git(prj, "cherry-pick", c2, c1, ok=False)                               # stops at its first commit
    assert gitmerge.merge_check(prj)["notes"] == []
    git(prj, "cherry-pick", "--abort")


def test_a_single_cherry_pick_beside_a_sequence_left_behind(prj):
    """A sequence of picks given up without --abort leaves .git/sequencer (its HEAD from before it), and a single
    `git cherry-pick C` then leaves it as it is: its todo does not start with C, so it says nothing about C."""
    branch(prj, "d", "M2 down by 40", move(100, via=False))
    git(prj, "checkout", "-q", "-b", "c", "base")
    write(prj, "libs/l/buf.sch", edit(BUF, "W=1", "W=2"))
    git(prj, "commit", "-qam", "buf wider")
    move(80, via=False)(prj)
    git(prj, "commit", "-qam", "M2 down by 20")
    write(prj, "libs/l/chain.sch", edit(CHAIN, "200 0 0 0 {name=x2}", "400 0 0 0 {name=x2}"))
    git(prj, "commit", "-qam", "chain wider")
    branch(prj, "e", "M2 down by 60", move(120, via=False))
    e = git(prj, "rev-parse", "e").stdout.strip()
    git(prj, "checkout", "-q", "d")
    git(prj, "cherry-pick", "c~2", "c~1", "c", ok=False)                    # stops at M2, one more to go
    git(prj, "reset", "-q", "--hard")                                       # given up: the sequencer stays
    assert (prj / ".git/sequencer/todo").is_file() and not (prj / ".git/CHERRY_PICK_HEAD").exists()
    assert git(prj, "cherry-pick", "e", ok=False).returncode == 1
    check = gitmerge.merge_check(prj)
    assert (check["operation"], check["theirs"], check["notes"]) == ("cherry-pick", e, [])
    assert [(u["path"], [x["id"] for x in u["conflicts"]]) for u in check["unmerged"]] == [(SCH, ["inst:M2:placement"])]


def test_pre_commit_hook_refuses_unsaved_layout_edits(prj, tmp_path):
    """The hook git-setup installs (`undine precommit`) refuses a commit whose staged operation log holds layout edits
    not saved to the cell files (what the index has counts, not the working tree); --no-verify gets them through, and
    the commit that concludes a merge is not checked."""
    def head() -> str:
        return git(prj, "rev-parse", "HEAD").stdout.strip()
    unsaved = jsonl(*LOG, rec(4, cell="inv", view="layout", op="add_box", t="2026-10-24T09:00:00+0900"))
    saved = unsaved + jsonl(save(5, "2026-10-24T09:01:00+0900"))
    git(prj, "checkout", "-q", "-b", "x", "base")
    write(prj, "history/l.jsonl", unsaved)
    git(prj, "add", "history/l.jsonl")
    write(prj, "history/l.jsonl", saved)                                    # saved since, not staged
    r = git(prj, "commit", "-qm", "unsaved", ok=False)
    assert r.returncode == 1 and head() == git(prj, "rev-parse", "base").stdout.strip(), r.stdout + r.stderr
    assert "undine: history/l.jsonl: library l has layout edits not saved to its cell files (cells inv)" in r.stderr
    assert "save the layout in Undine" in r.stderr and "--no-verify" in r.stderr
    assert gitmerge.precommit(prj) == [{"path": "history/l.jsonl", "lib": "l", "cells": ["inv"]}]
    git(prj, "commit", "-qam", "saved")                                     # -a: the saved log is what is committed
    assert undine(prj, "precommit").returncode == 0 and gitmerge.precommit(prj) == []

    write(prj, "history/l.jsonl", saved + jsonl(rec(6, cell="top", view="layout", op="add_box")))
    assert git(prj, "commit", "-qam", "unsaved again", ok=False).returncode == 1
    git(prj, "commit", "--no-verify", "-qam", "unsaved, on purpose")
    git(prj, "checkout", "-q", "-b", "y", "base")
    write(prj, "libs/l/buf.sch", edit(BUF, "W=1", "W=2"))
    git(prj, "commit", "-qam", "y")
    git(prj, "merge", "--no-commit", "--no-ff", "x")                         # the log is x's, with its journal
    assert logmerge.journal((prj / "history/l.jsonl").read_bytes())
    git(prj, "commit", "-q", "--no-edit")                                   # the merge decided: not refused
    assert len(git(prj, "log", "-1", "--format=%P").stdout.split()) == 2

    d = tmp_path / "new"                                                    # the first commit is checked too
    write(d, "history/l.jsonl", unsaved)
    git_setup(d)
    git(d, "add", "-A")
    assert git(d, "commit", "-qm", "first", ok=False).returncode == 1


def test_pre_commit_hook_while_concluding_a_merge(prj):
    """The commit that concludes a merge keeps the unsaved edits the merge left pending (theirs', renumbered by the
    log's merge), but refuses layout edits made since, while concluding it."""
    git(prj, "checkout", "-q", "-b", "x", "base")
    write(prj, "history/l.jsonl", jsonl(*LOG, rec(4, cell="inv", view="layout", op="add_box",
                                                  t="2026-10-24T09:00:00+0900")))
    git(prj, "commit", "--no-verify", "-qam", "x: an unsaved layout edit")
    branch(prj, "y", "y", lambda d: write(d, "history/l.jsonl", jsonl(*LOG, rec(4, reason="y only"))))
    r = git(prj, "merge", "--no-commit", "--no-ff", "x")                    # y did not change the layout
    assert "undine: history/l.jsonl: log merged" in r.stderr, r.stderr
    merged = (prj / "history/l.jsonl").read_bytes()
    assert [(x["seq"], x.get("merged_seq")) for x in logmerge.journal(merged)] == [(5, 4)]   # x's, pending
    assert gitmerge.precommit(prj) == []

    write(prj, "history/l.jsonl", merged + jsonl(rec(7, cell="top", view="layout", op="add_box")))   # made since
    git(prj, "add", "history/l.jsonl")
    assert gitmerge.precommit(prj) == [{"path": "history/l.jsonl", "lib": "l", "cells": ["top"]}]
    r = git(prj, "commit", "-q", "--no-edit", ok=False)
    assert r.returncode == 1 and "library l has layout edits not saved to its cell files (cells top)" in r.stderr
    assert (prj / ".git/MERGE_HEAD").is_file()                              # still concluding it
    write(prj, "history/l.jsonl", merged)
    git(prj, "add", "history/l.jsonl")
    git(prj, "commit", "-q", "--no-edit")
    assert len(git(prj, "log", "-1", "--format=%P").stdout.split()) == 2


def test_pre_commit_hook_concluding_a_squash_merge_or_a_cherry_pick_no_commit(prj):
    """git merge --squash and cherry-pick -n write neither MERGE_HEAD nor CHERRY_PICK_HEAD, and the commit that
    concludes them keeps the unsaved edits the merge left pending all the same: a squash names theirs in SQUASH_MSG
    (here git took the other branch's log whole, its own records), and the copies a log merge made (merged_seq, not in
    HEAD's log) are the driver's decision whatever the operation. Layout edits made since are refused."""
    log = prj / "history/l.jsonl"

    def journal() -> list[tuple[int, int | None]]:
        return [(r["seq"], r.get("merged_seq")) for r in logmerge.journal(log.read_bytes())]

    def refuses_edits_made_since() -> None:
        merged = log.read_bytes()
        nxt = max(r["seq"] for _, r in logmerge.records(merged)) + 1
        write(prj, "history/l.jsonl", merged + jsonl(rec(nxt, cell="top", view="layout", op="add_box")))
        git(prj, "add", "history/l.jsonl")
        assert gitmerge.precommit(prj) == [{"path": "history/l.jsonl", "lib": "l", "cells": ["top"]}]
        write(prj, "history/l.jsonl", merged)
        git(prj, "add", "history/l.jsonl")
    git(prj, "checkout", "-q", "-b", "x", "base")
    write(prj, "history/l.jsonl", jsonl(*LOG, rec(4, cell="inv", view="layout", op="add_box",
                                                  t="2026-10-24T09:00:00+0900")))
    git(prj, "commit", "--no-verify", "-qam", "x: an unsaved layout edit")

    branch(prj, "y", "y: buf wider", lambda d: write(d, "libs/l/buf.sch", edit(BUF, "W=1", "W=2")))
    git(prj, "merge", "--squash", "x")                                      # y left the log as it was: x's whole
    assert (prj / ".git/SQUASH_MSG").is_file() and not (prj / ".git/MERGE_HEAD").exists()
    assert journal() == [(4, None)] and gitmerge.precommit(prj) == []
    refuses_edits_made_since()
    git(prj, "commit", "-qm", "x, squashed")                                # the hook lets it through
    assert git(prj, "log", "-1", "--format=%s").stdout.strip() == "x, squashed"

    branch(prj, "z", "z: an operation, no layout edit", lambda d: write(d, "history/l.jsonl", jsonl(
        *LOG, rec(4, reason="z only"))))
    r = git(prj, "cherry-pick", "-n", "x")                                  # z did not change the layout: x's pending
    assert "undine: history/l.jsonl: log merged" in r.stderr, r.stderr
    assert not any((prj / ".git" / f).exists() for f in ("CHERRY_PICK_HEAD", "MERGE_HEAD", "SQUASH_MSG"))
    assert journal() == [(5, 4)] and gitmerge.precommit(prj) == []
    refuses_edits_made_since()
    git(prj, "commit", "-qm", "x, picked")
    assert git(prj, "log", "-1", "--format=%s").stdout.strip() == "x, picked"


def test_resolve_all_ours_and_all_theirs(prj):
    branch(prj, "y", "y", lambda d: write(d, SCH, edit(edit(INV, "W=2", "W=5"), "-20 -60 0 0", "-20 -40 0 0")))
    branch(prj, "x", "x", lambda d: write(d, SCH, edit(edit(INV, "W=2", "W=4"), "-20 -60 0 0", "-20 -20 0 0")))
    r = git(prj, "merge", "--no-edit", "y", ok=False)
    assert r.returncode == 1 and f"undine: {SCH}: schematic, 2 conflicts (" in r.stderr, r.stdout + r.stderr
    assert "), ours kept; see `undine merge-check`" in r.stderr                # more than one: no words of each
    report = json.loads((prj / f".git/undine-merge/{SCH}.json").read_text())
    assert sorted(c["id"] for c in report["conflicts"]) == ["inst:M1:placement", "inst:M1:prop:W"]
    out = undine(prj, "resolve", SCH, "--all-theirs")
    assert out.returncode == 0, out.stdout + out.stderr
    assert (comp(text(prj, SCH), "M1").props["W"], comp(text(prj, SCH), "M1").y) == ("5", -40)
    left = gitmerge.resolve(SCH, ours=["inst:M1:prop:W"], cwd=prj)         # one back to ours, the other stays theirs
    assert left == [] and (comp(text(prj, SCH), "M1").props["W"], comp(text(prj, SCH), "M1").y) == ("4", -40)
    out = undine(prj, "resolve", SCH, "--ours", "inst:M1:placement", "--theirs", "inst:M1:placement")
    assert out.returncode == 1 and "both sides named" in out.stderr


def test_driver_never_leaves_a_partial_file(tmp_path, capsys):
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q")
    write(d, "o.oas", b"ours, kept whole")
    write(d, "t.oas", b"not an OASIS file")
    write(d, "b.oas", b"")
    assert gitmerge.driver("b.oas", "o.oas", "t.oas", "libs/l/x.oas", cwd=d) == 1
    assert (d / "o.oas").read_bytes() == b"ours, kept whole"
    assert "libs/l/x.oas" in capsys.readouterr().err
    assert sorted(p.name for p in d.iterdir()) == [".git", "b.oas", "o.oas", "t.oas"]       # no temporary file left
    report = json.loads((d / ".git/undine-merge/libs/l/x.oas.json").read_text())         # the error, for merge-check
    assert report["error"].startswith("RuntimeError: ") and report["kind"] == "layout" and not report["clean"]
    assert (report["conflicts"], report["warnings"], report["combined"], report["prefer"]) == ([], [], [], {})
    assert report["blobs"] == {"base": None, "ours": git(d, "hash-object", "o.oas").stdout.strip(),
                               "theirs": git(d, "hash-object", "t.oas").stdout.strip()}


def test_driver_writes_the_file_before_the_report(tmp_path, monkeypatch, capsys):
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q")
    write_ = gitmerge._write

    def setup(fail) -> None:
        write(d, "base", INV)
        write(d, "ours", edit(INV, "W=2", "W=4"))
        write(d, "theirs", edit(INV, "name=M2\nL=0.15", "name=M2\nL=0.18"))
        shutil.rmtree(d / ".git/undine-merge", ignore_errors=True)

        def failing(path: Path, data: bytes) -> None:
            if fail(Path(path)):
                raise OSError("disk full")
            write_(path, data)
        monkeypatch.setattr(gitmerge, "_write", failing)

    setup(lambda p: p.name == "ours")       # the file cannot be written: no report says it was merged
    assert gitmerge.driver("base", "ours", "theirs", SCH, cwd=d) == 1
    assert text(d, "ours") == edit(INV, "W=2", "W=4")
    report = json.loads((d / f".git/undine-merge/{SCH}.json").read_text())
    assert report["error"] == "OSError: disk full" and report["combined"] == []
    setup(lambda p: p.suffix == ".json")    # the report cannot be written: the file is merged all the same
    assert gitmerge.driver("base", "ours", "theirs", SCH, cwd=d) == 0
    assert comp(text(d, "ours"), "M1").props["W"] == "4" and comp(text(d, "ours"), "M2").props["L"] == "0.18"
    assert "merged, but no report (OSError: disk full)" in capsys.readouterr().err


def test_a_file_the_driver_cannot_merge(prj):
    """The driver fails on ours' broken layout: the file stays ours, conflicted, and merge-check says why."""
    oas = "libs/l/inv.oas"
    branch(prj, "b", "b", edits_b)
    branch(prj, "a", "a broken layout", lambda d: write(d, oas, b"not an OASIS file"))
    r = git(prj, "merge", "--no-commit", "--no-ff", "b", ok=False)
    assert r.returncode == 1 and f"undine: {oas}: RuntimeError" in r.stderr, r.stdout + r.stderr
    assert (prj / oas).read_bytes() == b"not an OASIS file"
    check = gitmerge.merge_check(prj)
    error = json.loads((prj / f".git/undine-merge/{oas}.json").read_text())["error"]
    assert check["unmerged"] == [{"path": oas, "kind": "layout", "conflicts": [], "error": error}]
    assert error.startswith("RuntimeError: ") and oas not in check["merged"]
    assert f"  {oas}  merge failed: {error}" in gitmerge.format_check(check).splitlines()
    git(prj, "checkout", "--theirs", oas)                                   # resolved without Undine
    git(prj, "add", oas)
    check = gitmerge.merge_check(prj)
    assert check["unmerged"] == [] and check["warnings"] == \
        [{"path": oas, "warning": f"not merged by Undine ({error}): its warnings are unknown"}]


def test_driver_without_base_and_for_other_files(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q")
    # both sides added the cell's schematic (git passes an empty base): instances added differently on the two sides
    # are whole conflicts, ours kept; the rest is the same on both
    m3 = "C {sky130_fd_pr/nfet_01v8.sym} 40 60 0 0 {name=M3 W=1}\n"
    write(d, "base", b"")
    write(d, "ours", edit(INV, "W=2", "W=4") + m3)
    write(d, "theirs", INV + m3.replace("W=1", "W=2"))
    assert gitmerge.driver("base", "ours", "theirs", "libs/l/new.sch", cwd=d) == 1
    report = json.loads((d / ".git/undine-merge/libs/l/new.sch.json").read_text())
    assert sorted(c["id"] for c in report["conflicts"]) == ["inst:M1", "inst:M3"]
    assert report["blobs"]["base"] is None and text(d, "ours") == edit(INV, "W=2", "W=4") + m3
    # a file of no view: git's line merge
    write(d, "base", "a\nb\nc\n")
    write(d, "ours", "A\nb\nc\n")
    write(d, "theirs", "a\nb\nC\n")
    assert gitmerge.driver("base", "ours", "theirs", "notes.txt", cwd=d) == 0
    assert text(d, "ours") == "A\nb\nC\n"


def test_diff_of_two_files(tmp_path):
    write(tmp_path, "a.sch", INV)
    write(tmp_path, "b.sch", edit(INV, "W=2", "W=4"))
    out = undine(tmp_path, "diff", "a.sch", "b.sch")
    assert out.returncode == 0 and out.stdout.splitlines() == ["b.sch  (schematic)", "  ~ instance M1: W: 2 -> 4"]
    out = undine(tmp_path, "diff", "a.sch", "a.sch", "--json")
    assert json.loads(out.stdout) == {"a.sch": {"kind": "schematic", "changes": []}}
    write(tmp_path, "x.jsonl", jsonl(*LOG))
    assert gitmerge.diff_files(os.devnull, tmp_path / "x.jsonl", "log")["changes"][0]["id"] == "seq:1"
    with pytest.raises(ValueError, match="which view"):
        gitmerge.diff_files(tmp_path / "x.jsonl", tmp_path / "x.jsonl")
