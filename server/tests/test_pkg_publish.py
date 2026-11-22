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
from undine.store import cas, deps, gitsrc, pkgsource  # noqa: E402
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
