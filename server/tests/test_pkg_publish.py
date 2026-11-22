"""Phase 5 of docs/design/packages.md, publishing: `pkg init` (a manifest drafted from a project library), `pkg pack`
(the index line of a tagged release) and `pkg check --no-run` (the check report's checks that need no PDK). Every
repository and index is local, under the test's temporary directory; nothing touches GitHub or the network."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_pkg_index import SYM, World, _commit, _git, _project, _tools_here  # noqa: E402,F401
from undine import pkgcli  # noqa: E402
from undine.store import cas, checkreport, deps, gitsrc, pkgsource  # noqa: E402
from undine.store import index as ix  # noqa: E402
from undine.store import manifest as mf  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

PDK_COMMIT = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
HEAD = "v {xschem version=3.4.5 file_version=1.2}\nG {}\nK {}\nV {}\nS {}\nE {}\n"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _cli(store, *argv) -> int:
    parser = argparse.ArgumentParser()
    pkgcli.add_parser(parser.add_subparsers(dest="cmd"))
    return pkgcli.run(parser.parse_args(["pkg", *argv]), ProjectStore(store.root, {}))


def _sch(*placed: str) -> str:
    return HEAD + "".join(f"C {{{s}}} {i * 100} 0 0 0 {{name=x{i}}}\n" for i, s in enumerate(placed))


def _pin_pdk(prj) -> None:
    lock = deps.read_lock(prj.path)
    lock["pdk"] = {"id": "sky130A@8afc8346a57f", "family": "sky130", "variant": "sky130A", "commit": PDK_COMMIT}
    deps.write_lock(prj.path, lock)


def _library(tmp_path: Path, cells: dict[str, str] | None = None, lib: str = "analog_ip"):
    """A project p whose library `lib` (libs/<lib>) holds cells, the project a git repository with a remote origin;
    returns (store, project, library directory)."""
    store, prj = _project(tmp_path)
    _pin_pdk(prj)
    d = prj.create_library(lib).path
    for name, text in (cells or {"inv.sch": _sch("devices/res.sym"), "inv.sym": SYM, "unit.sch": _sch()}).items():
        (d / name).write_text(text)
    _git(prj.path, "init", "-q")
    _git(prj.path, "remote", "add", "origin", "https://example.org/me/analog_ip.git")
    (prj.path / ".gitignore").write_text(".undine/\n")
    return store, prj, d


def _release(prj, version: str | None = None, tag: str | None = None) -> str:
    _git(prj.path, "add", "-A")
    _git(prj.path, "commit", "-qm", f"release {version or ''}")
    if tag:
        _git(prj.path, "tag", tag)
    return _git(prj.path, "rev-parse", "HEAD")


def _manifest(d: Path, text: str) -> None:
    (d / mf.MANIFEST).write_text(text)


def _basic(name="analog_ip", version="1.0.0", repository=None, more="") -> str:
    repo = f'repository = "{repository}"\n' if repository else ""
    return (f'[package]\nname = "{name}"\nversion = "{version}"\nlicense = "Apache-2.0"\n{repo}'
            f'[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\ncommits = ["{PDK_COMMIT}"]\n'
            f'built_with = "{PDK_COMMIT}"\n[exports]\ncells = ["inv"]\n[files]\ninclude = ["*.sch", "*.sym"]\n{more}')


# ------------------------------------------------------------------ pkg init


def test_init_writes_a_manifest_that_parses_and_names_the_project_pdk_commit(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    w.publish("bias_gen", "0.3.2", cells=["bias_gen_cell"])
    store, prj, d = _library(tmp_path, {"inv.sym": SYM, "comp.sym": SYM, "unit.sch": _sch(),
                                        "inv.sch": _sch("bias/bias_gen_cell.sym", "devices/res.sym",
                                                        "sar/sar_cell.sym", "analog_ip/unit.sym")})
    deps.add(prj, "bias", None, package="bias_gen", version="~0.3")
    _pin_pdk(prj)
    prj = store.get("p")
    prj.meta["libraries"]["sar"] = {"git": "https://example.org/sar.git", "rev": "v1.0.0"}
    prj.save()
    assert _cli(store, "init", str(d)) == 0
    out = capsys.readouterr().out
    m = mf.from_dir(d, "t")
    assert (m.name, str(m.version), m.license, m.repository) == ("analog_ip", "0.1.0", None,
                                                                 "https://example.org/me/analog_ip.git")
    assert m.exports == ("comp", "inv") and m.include == mf.DEFAULT_INCLUDE
    assert (m.pdk.family, m.pdk.variants, m.pdk.commits, m.pdk.built_with) == ("sky130", ("sky130A",), (PDK_COMMIT,),
                                                                              PDK_COMMIT)
    assert m.dependencies == {"bias": mf.IndexDep("bias", "bias_gen", "^0.3.2")}
    text = (d / mf.MANIFEST).read_text()
    assert "# sar: a git dependency (https://example.org/sar.git @ v1.0.0): a published package depends only" in text
    for said in ("wrote ", "name             analog_ip (the library's name)", "version          0.1.0",
                 "repository       https://example.org/me/analog_ip.git (git remote origin)",
                 "exports.cells    comp, inv (the cells that have a symbol)",
                 f"pdk              sky130 sky130A, built_with and commits {PDK_COMMIT}",
                 "dependencies.bias bias_gen ^0.3.2 (placed: bias_gen_cell)", "~ no license: publishing needs one"):
        assert said in out, out
    assert _cli(store, "init", "p/analog_ip") == 1
    assert "undine-package.toml exists: edit it, or give --force" in capsys.readouterr().out
    assert _cli(store, "init", "p/analog_ip", "--force", "--name", "ip2", "--version", "2.0.0", "--license",
                "MIT OR Apache-2.0", "--repository", "https://example.org/x.git") == 0
    out = capsys.readouterr().out
    m = mf.from_dir(d, "t")
    assert (m.name, str(m.version), m.license, m.repository) == ("ip2", "2.0.0", "MIT OR Apache-2.0",
                                                                 "https://example.org/x.git")
    assert "~ the schematics place the library's own cells as analog_ip/... (unit)" in out
    assert _cli(store, "init", str(d), "--force", "--license", "MIT and ISC") == 1
    assert "--license: 'MIT and ISC' is not an SPDX expression" in capsys.readouterr().out
    assert _cli(store, "init", str(d), "--force", "--name", "Bad-Name") == 1
    assert "'Bad-Name' is not a package name" in capsys.readouterr().out


def test_init_refuses_a_dependency_or_a_directory_that_is_no_library(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    w.publish("bias_gen", "0.3.2")
    store, prj, d = _library(tmp_path)
    deps.add(prj, "bias", None, package="bias_gen")
    assert _cli(store, "init", str(prj.path / deps.DEPS / "bias")) == 1
    assert "p/bias is a dependency installed from elsewhere" in capsys.readouterr().out
    assert _cli(store, "init", str(prj.path / "libs")) == 1
    assert "not a library of project p (its own libraries: analog_ip)" in capsys.readouterr().out
    assert _cli(store, "init", str(tmp_path)) == 1
    assert "no project.json in it or above it" in capsys.readouterr().out
    assert _cli(store, "init", "p/nope") == 1
    assert "project p has no library nope" in capsys.readouterr().out


# ------------------------------------------------------------------ tag and pack


def test_tag_name_v_version_in_a_repository_of_two_packages(tmp_path, capsys):
    store, prj, d = _library(tmp_path)
    _manifest(d, _basic(repository=str(prj.path)))
    _release(prj, tag="v1.0.0")
    src = pkgsource.locate(str(d), store)
    assert (src.top.resolve(), src.subdir) == (prj.path.resolve(), "libs/analog_ip")
    assert pkgsource.tag_of(src) == "v1.0.0"
    assert pkgsource.at_tag(src)[0] == "v1.0.0"
    other = prj.create_library("bias_gen").path
    (other / "b.sym").write_text(SYM)
    _manifest(other, '[package]\nname = "bias_gen"\nversion = "0.3.0"\n')
    _release(prj)
    src = pkgsource.locate(str(d), store)
    assert pkgsource.tag_of(src) == "analog_ip-v1.0.0"
    assert _cli(store, "pack", str(d)) == 1
    assert "p/analog_ip: no tag analog_ip-v1.0.0: tag the release commit (`git tag analog_ip-v1.0.0`)" in \
        capsys.readouterr().out
    _git(prj.path, "tag", "-a", "-m", "release", "analog_ip-v1.0.0")              # an annotated tag works too
    assert _cli(store, "pack", str(d)) == 0
    assert json.loads(capsys.readouterr().out)["vers"] == "1.0.0"
    assert pkgsource.tag_of(pkgsource.locate(str(other), store)) == "bias_gen-v0.3.0"


def test_pack_refuses_a_dirty_package_or_a_head_that_is_not_the_tag(tmp_path, capsys):
    store, prj, d = _library(tmp_path)
    _manifest(d, _basic(repository=str(prj.path)))
    assert _cli(store, "pack", str(d)) == 1
    assert "p/analog_ip: the repository has no commit yet" in capsys.readouterr().out
    _release(prj, tag="v1.0.0")
    (prj.path / "project.json").write_text((prj.path / "project.json").read_text() + "\n")   # outside: not looked at
    (prj.path / "notes.txt").write_text("x")
    assert _cli(store, "pack", str(d)) == 0
    capsys.readouterr()
    (d / "inv.sym").write_text(SYM + "* edited\n")
    assert _cli(store, "pack", str(d)) == 1
    assert "uncommitted changes under libs/analog_ip: libs/analog_ip/inv.sym: commit them" in capsys.readouterr().out
    _git(prj.path, "checkout", "--", "libs/analog_ip/inv.sym")
    (d / "scratch.txt").write_text("x")                                      # untracked, not even in the include set
    assert _cli(store, "pack", str(d)) == 1
    assert "libs/analog_ip/scratch.txt" in capsys.readouterr().out
    (d / "scratch.txt").unlink()
    (d / "inv.sym").write_text(SYM + "* fixed\n")
    _release(prj)
    assert _cli(store, "pack", str(d)) == 1
    assert "tag v1.0.0 is " in capsys.readouterr().out
    _manifest(d, _basic(version="1.0.1", repository=None))
    _release(prj, tag="v1.0.1")
    assert _cli(store, "pack", str(d)) == 1
    assert "package.repository: missing" in capsys.readouterr().out
    shutil.rmtree(prj.path / ".git")
    assert _cli(store, "pack", str(d)) == 1
    assert "is not in a git repository" in capsys.readouterr().out


def test_pack_prints_a_line_parse_line_accepts_with_the_tree_blob_tree_gives(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    other = World(tmp_path, "acme")
    store, prj, d = _library(tmp_path, {"inv.sch": _sch(), "inv.sym": SYM, "big.oas": "x" * 10, "README.md": "r"})
    prj.meta["indexes"] = {"acme": str(other.index)}
    prj.save()
    _manifest(d, _basic(repository=str(prj.path), more='[dependencies]\nsar = "^1.2"\n'
                                                       'tech = { version = "^2", index = "acme" }\n'))
    commit = _release(prj, tag="v1.0.0")
    report = tmp_path / "undine-check.json"
    report.write_bytes(b'{"format": 1}')
    url = "https://example.org/releases/download/v1.0.0/undine-check.json"
    assert _cli(store, "pack", str(d), "--check", str(report), "--check-url", url) == 0
    text = capsys.readouterr().out.strip()
    line = ix.parse_line(text, "pack", str(w.index))
    with cas.store_lock(shared=True):
        files = gitsrc.blob_tree(str(prj.path), commit, "libs/analog_ip", "include")
    assert (line.name, str(line.vers), line.git, line.subdir, line.commit) == ("analog_ip", "1.0.0", str(prj.path),
                                                                               "libs/analog_ip", commit)
    assert line.tree == cas.tree_hash(files) and set(files) == {"inv.sch", "inv.sym", mf.MANIFEST}
    assert line.size == sum((d / p).stat().st_size for p in files)
    assert line.pdk == {"family": "sky130", "variants": ["sky130A"], "commits": [PDK_COMMIT], "built_with": PDK_COMMIT}
    assert [(x.local, str(x.req), x.index) for x in line.deps] == [("sar", "^1.2", None),
                                                                     ("tech", "^2", str(other.index))]
    assert line.check == {"url": url, "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
    assert (line.exports, line.license, line.yanked) == (("inv",), "Apache-2.0", False)
    assert text == ix.line_text(line)
    assert _cli(store, "pack", str(d), "--index", "acme") == 0     # for acme: tech is acme's own, sar is acme's too
    line = ix.parse_line(capsys.readouterr().out.strip(), "pack")
    assert [(x.local, x.index) for x in line.deps] == [("sar", None), ("tech", None)]
    assert line.check is None
    assert _cli(store, "pack", str(d), "--check", str(report)) == 1
    assert "--check FILE and --check-url URL go together" in capsys.readouterr().out
    assert _cli(store, "pack", str(d), "--check", str(report), "--check-url", "http://x/r.json") == 1
    assert "not an https URL" in capsys.readouterr().out


# ------------------------------------------------------------------ the check report


def _report_of(prj, name="analog_ip", version="1.0.0") -> dict:
    return json.loads((prj.path / ".undine" / "check" / f"{name}-{version}" / checkreport.FILE).read_text())


def _problems(rep: dict, check: str) -> list[str]:
    return [p["message"] for p in rep["problems"] if p["check"] == check]


def test_report_verify_refuses_a_changed_byte_a_wrong_commit_or_tree_or_a_failing_report():
    line = ix.parse_line(json.dumps({"name": "analog_ip", "vers": "1.0.0", "git": "https://e.org/a.git",
                                     "commit": "c" * 40, "tree": "sha256:" + "d" * 64}), "x")
    rep = {"format": 1, "kind": "undine-check", "package": {"name": "analog_ip", "version": "1.0.0",
                                                            "commit": "c" * 40, "tree": "sha256:" + "d" * 64},
           "warnings": [], "problems": [], "passed": True}
    data = checkreport.dumps(rep)
    sha = hashlib.sha256(data).hexdigest()
    assert checkreport.verify(data, line, sha) == [] and checkreport.verify(data, line, None) == []
    changed = data.replace(b'"analog_ip"', b'"analog_iq"', 1)
    got = checkreport.verify(changed, line, sha)
    assert got[0].startswith("the report's SHA-256 is ") and "the report is for name 'analog_iq'" in got[1]
    for key, value, want in (("commit", "e" * 40, "c" * 40), ("tree", "sha256:" + "0" * 64, "sha256:" + "d" * 64),
                             ("version", "1.0.1", "1.0.0")):
        other = checkreport.dumps({**rep, "package": {**rep["package"], key: value}})
        assert checkreport.verify(other, line, None) == [f"the report is for {key} {value!r}, the line names "
                                                          f"{want!r}"]
    failing = checkreport.dumps({**rep, "passed": False, "problems": [{"check": "runs", "message": "not run"}]})
    assert checkreport.verify(failing, line, None) == ["the report does not pass: 1 problem(s), the first: not run"]
    lying = checkreport.dumps({**rep, "problems": [{"check": "x", "message": "y"}]})
    assert checkreport.verify(lying, line, None) == ["the report says it passes but lists problems"]
    assert "format is 2" in checkreport.verify(checkreport.dumps({**rep, "format": 2}), line, None)[0]
    assert "kind is 'x'" in checkreport.verify(checkreport.dumps({**rep, "kind": "x"}), line, None)[0]
    assert checkreport.verify(b"not json", line, None)[0].startswith("the report is not JSON")


def test_check_no_run_writes_the_report_under_the_project_bound_to_the_commit_and_tree(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    w.publish("bias_gen", "0.3.2", cells=["bias_gen_cell"])
    store, prj, d = _library(tmp_path, {"inv.sch": _sch("bias/bias_gen_cell.sym", "devices/res.sym"),
                                        "inv.sym": SYM, "inv.oas": "oas", "unit_cap.oas": "oas"})
    deps.add(prj, "bias", None, package="bias_gen", version="~0.3")
    _pin_pdk(prj)
    _manifest(d, _basic(repository=str(prj.path), more=(
        '[dependencies]\nbias = { package = "bias_gen", version = "^0.3" }\n'
        '[checks]\ndrc = { deck = "sky130A_mr.drc", options = { density = false } }\n'
        'waive = [{ cell = "unit_cap", rules = ["m1.2"], reason = "abutment cell: clean once arrayed" }]\n'))
              .replace('include = ["*.sch", "*.sym"]', 'include = ["*.sch", "*.sym", "*.oas"]'))
    commit = _release(prj, tag="v1.0.0")
    assert _cli(store, "check", str(d)) == 1
    assert "for now give --no-run" in capsys.readouterr().out
    assert _cli(store, "check", str(d), "--no-run") == 1                     # not run: it cannot pass
    out = capsys.readouterr().out
    rep = _report_of(prj)
    with cas.store_lock(shared=True):
        tree = cas.tree_hash(gitsrc.blob_tree(str(prj.path), commit, "libs/analog_ip", "include"))
    assert (rep["format"], rep["kind"], rep["passed"]) == (1, "undine-check", False)
    assert rep["package"] == {"name": "analog_ip", "version": "1.0.0", "repository": "<repository>",
                              "subdir": "libs/analog_ip", "tag": "v1.0.0", "commit": commit, "tree": tree}
    assert rep["runs"] == {"state": "not_run", "why": "--no-run"} and rep["semver"]["state"] == "not_run"
    assert [p["check"] for p in rep["problems"]] == ["runs"]
    assert rep["pdk"] == {"family": "sky130", "variants": ["sky130A"], "built_with": PDK_COMMIT,
                          "project": PDK_COMMIT}
    assert rep["license"] == {"expression": "Apache-2.0", "ok": True, "ids": ["Apache-2.0"]}
    assert rep["dependencies"] == [{"name": "bias", "package": "bias_gen", "req": "^0.3", "index": str(w.index),
                                    "resolved": "0.3.2"}]
    assert rep["publisher_project"] == {"name": "p", "lib": "analog_ip"}
    assert set(rep["cells"]) == {"inv", "unit_cap"}
    inv, cap = rep["cells"]["inv"], rep["cells"]["unit_cap"]
    assert (inv["exported"], inv["views"], inv["lvs"]["applies"], cap["exported"]) == (
        True, ["schematic", "symbol", "layout"], True, False)
    assert cap["drc"] == {"state": "not_run", "applies": True, "deck": "sky130A_mr.drc", "options": {"density": False},
                          "job": None, "total": None, "counted": None, "passed": False,
                          "waived": [{"rule": "m1.2", "count": None, "reason": "abutment cell: clean once arrayed"}]}
    assert cap["lvs"]["why"] == "no schematic" and inv["drc"]["waived"] == []
    assert rep["files"]["count"] == 5 and rep["files"]["over"] == []
    data = (prj.path / ".undine" / "check" / "analog_ip-1.0.0" / checkreport.FILE).read_bytes()
    assert str(prj.path).encode() not in data and str(Path.home()).encode() + b"/" not in data
    assert f"wrote {prj.path / '.undine' / 'check' / 'analog_ip-1.0.0' / checkreport.FILE} (sha256 " \
           f"{hashlib.sha256(data).hexdigest()}): does not pass" in out
    assert "! runs: DRC, LVS, the symbol and netlist checks and the specs were not run (--no-run)" in out
    assert pkgsource.dirty(pkgsource.locate(str(d), store)) == []               # the report is outside the package
    line = ix.parse_line(json.dumps({"name": "analog_ip", "vers": "1.0.0", "git": str(prj.path), "commit": commit,
                                     "tree": tree}), "x")
    assert checkreport.verify(data, line, hashlib.sha256(data).hexdigest()) == [
        "the report does not pass: 1 problem(s), the first: DRC, LVS, the symbol and netlist checks and the specs "
        "were not run (--no-run): a report passes only with them"]
    out_file = tmp_path / "elsewhere.json"
    assert _cli(store, "check", str(d), "--no-run", "--offline", "--out", str(out_file)) == 1
    assert json.loads(out_file.read_text())["warnings"][0]["message"].startswith(
        "not checked (--offline) that <repository> serves v1.0.0")


def test_a_file_over_the_limit_and_a_git_dependency_are_problems(tmp_path, monkeypatch, capsys):
    store, prj, d = _library(tmp_path, {"inv.sch": _sch(), "inv.sym": SYM, "big.sch": _sch() + "* " + "x" * 2000})
    _manifest(d, _basic(repository=str(prj.path), more='[dependencies]\n'
                                                       'sar = { git = "https://example.org/sar.git", rev = "v1" }\n')
              .replace('license = "Apache-2.0"\n', 'license = "Apache 2.0"\n'))
    _release(prj, tag="v1.0.0")
    monkeypatch.setenv("UNDINE_PKG_FILE_MAX", "1000")
    assert _cli(store, "check", str(d), "--no-run") == 1
    rep = _report_of(prj)
    assert _problems(rep, "files") == [f"big.sch: {(d / 'big.sch').stat().st_size} bytes, more than the 1000 a package "
                                       "file may have (UNDINE_PKG_FILE_MAX)"]
    assert rep["files"]["largest"]["path"] == "big.sch" and rep["files"]["limit"] == 1000
    assert _problems(rep, "dependencies") == ["sar is a git dependency: a published package depends only on packages "
                                              "from an index: publish it to one and name it by version"]
    assert _problems(rep, "license")[0].startswith("'Apache 2.0' is not an SPDX expression")
    assert rep["license"]["ok"] is False
    _manifest(d, _basic(version="1.0.1", repository=str(prj.path)).replace(PDK_COMMIT + '"\n[exports]',
                                                                           "1" * 40 + '"\n[exports]', 1)
              .replace(f'commits = ["{PDK_COMMIT}"]', f'commits = ["{"1" * 40}"]'))
    (d / "big.sch").unlink()
    (d / "lfs.sch").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 9\n")
    _release(prj, tag="v1.0.1")
    monkeypatch.delenv("UNDINE_PKG_FILE_MAX")
    assert _cli(store, "check", str(d), "--no-run") == 1
    rep = _report_of(prj, version="1.0.1")
    assert "lfs.sch is a Git LFS pointer, not the file" in _problems(rep, "files")[0]
    assert rep["package"]["tree"] is None and str(tmp_path) not in json.dumps(rep)
    assert _problems(rep, "pdk") == [f"the project's PDK is {PDK_COMMIT[:12]}, pdk.built_with is {'1' * 12}: check "
                                     "on the PDK the package names (set-pdk), or change built_with"]


def test_a_package_placing_an_undeclared_local_name_is_refused(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    repo, commit = w.package("bias_gen", "0.3.2", None, ["bias_gen_cell", "inner"],
                             pdk='[exports]\ncells = ["bias_gen_cell"]\n')
    with cas.store_lock(shared=True):
        gitsrc.fetch(str(repo), None, "v0.3.2")
        files = gitsrc.blob_tree(str(repo), commit, None, "include")
    w.append({"name": "bias_gen", "vers": "0.3.2", "git": str(repo), "commit": commit, "tree": cas.tree_hash(files),
              "exports": ["bias_gen_cell"]})
    store, prj, d = _library(tmp_path, {"inv.sym": SYM, "unit.sym": SYM,
                                        "inv.sch": _sch("bias/inner.sym", "foo/x.sym", "analog_ip/unit.sym",
                                                        "plain.sym", "ip/unit.sym")})
    deps.add(prj, "bias", None, package="bias_gen")
    _pin_pdk(prj)
    _manifest(d, _basic(name="ip", repository=str(prj.path),
                        more='[dependencies]\nbias = { package = "bias_gen", version = "^0.3" }\n'))
    _release(prj, tag="v1.0.0")
    assert _cli(store, "check", str(d), "--no-run") == 1
    rep = _report_of(prj, name="ip")
    names = _problems(rep, "local_names")
    assert names[0].startswith("the schematics place the library's own cells as analog_ip/... (unit in inv): a "
                               "consumer installs the package as ip")
    assert names[1].startswith("foo is placed (x in inv) but is neither the package, a declared dependency nor a "
                               "PDK library: add it to [dependencies]")
    assert "PDK is not installed here" in names[1] and len(names) == 2
    warned = {x["check"]: x["message"] for x in rep["warnings"]}
    assert warned["internal_cells"].startswith("places internal cells of bias: inner")
    assert warned["local_names"].startswith("symbols placed with no library part (plain in inv)")


def test_listing_lists_every_code_block_and_rtl_file_and_flags_the_refused(tmp_path):
    from undine.store import trust
    d = tmp_path / "lib"
    (d / "rtl" / "sub").mkdir(parents=True)
    code = ('.include models.spice\n.lib /etc/passwd tt\n.control\nshell ls\npre_osdi x.osdi\nrun\n.endc\n'
            '*# echo hi\n.include $PDK_ROOT/$PDK/libs.tech/ngspice/corners/tt.spice\n')
    (d / "tb.sch").write_text(HEAD + "C {devices/code_shown.sym} 0 0 0 0 {name=s1 value=\"" + code + "\"}\n")
    (d / "evil.sym").write_text("v {}\nK {type=subcircuit format=\"tcleval(@x)\"}\n")
    (d / "rtl" / "a.v").write_text('module a; initial $system("ls"); endmodule\n')
    (d / "rtl" / "sub" / "b.sv").write_text("module b; endmodule\n")
    pdk = tmp_path / "pdks" / "sky130A"
    pdk.mkdir(parents=True)
    got = trust.listing(d, allowed=[pdk], env={"PDK_ROOT": str(pdk.parent), "PDK": "sky130A"})
    rows = [(b["file"], b["kind"], b.get("path"), b["refused"]) for b in got["blocks"]]
    assert rows == [("evil.sym", "tcl", None, True),
                    ("tb.sch", "include", "models.spice", False), ("tb.sch", "lib", "/etc/passwd", True),
                    ("tb.sch", "control", None, True), ("tb.sch", "control_line", None, True),
                    ("tb.sch", "include", "$PDK_ROOT/$PDK/libs.tech/ngspice/corners/tt.spice", False)]
    control = next(b for b in got["blocks"] if b["kind"] == "control")
    assert control["commands"] == ["shell", "pre_osdi", "run"] and control["cell"] == "tb"
    assert control["where"] == "tb.sch s1 value"
    assert got["rtl"] == [{"path": "rtl/a.v", "refused": True, "findings": [
                              {"kind": "rtl_system", "line": 1, "text": 'module a; initial $system("ls"); endmodule'}]},
                          {"path": "rtl/sub/b.sv", "refused": False, "findings": []}]
    bare = trust.listing(d)                                                   # no PDK: its path cannot be told
    assert [b["refused"] for b in bare["blocks"] if b.get("path", "").startswith("$PDK_ROOT")] == [True]
