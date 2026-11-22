"""The rest of phase 3 of docs/design/packages.md: `pkg remove`, `pkg index add` (config.toml or project.json) and
`pkg migrate` (a git dependency becomes a package from an index when the index version's files equal the installed
ones). Every index and repository is local, under the test's temporary directory."""
import argparse
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_pkg_index import SYM, World, _commit, _git, _project  # noqa: E402
from undine import pkgcli  # noqa: E402
from undine.store import cas, deps, gitsrc  # noqa: E402
from undine.store import index as ix  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _cli(store, *argv) -> int:
    parser = argparse.ArgumentParser()
    pkgcli.add_parser(parser.add_subparsers(dest="cmd"))
    return pkgcli.run(parser.parse_args(["pkg", *argv]), ProjectStore(store.root, {}))


# ------------------------------------------------------------------ pkg index add


def _cfg() -> Path:
    return Path(ix.home()) / "config.toml"


def test_index_add_writes_config_toml_and_keeps_the_rest():
    assert ix.add_configured("acme", "git@git.acme.example:idx.git")
    assert ix.configured()["acme"] == "git@git.acme.example:idx.git"
    assert _cfg().read_text() == '[indexes]\nacme = "git@git.acme.example:idx.git"\n'
    assert not ix.add_configured("acme", "git@git.acme.example:idx.git")          # there already
    with pytest.raises(ix.IndexReadError, match="is git@git.acme.example:idx.git already"):
        ix.add_configured("acme", "https://other.example/idx.git")
    with pytest.raises(ix.IndexReadError, match="not a nickname"):
        ix.add_configured("Acme!", "https://x.example/i.git")
    # a file with comments, another table after [indexes] and a blank line: the entry goes into [indexes]
    _cfg().write_text('# my settings\n[indexes]\nacme = "git@git.acme.example:idx.git"  # work\n\n[other]\nk = 1\n')
    assert ix.add_configured("lab", "https://lab.example/idx.git")
    text = _cfg().read_text()
    assert text.startswith('# my settings\n[indexes]\nacme = "git@git.acme.example:idx.git"  # work\n'
                           'lab = "https://lab.example/idx.git"\n\n[other]\nk = 1\n')
    assert ix.configured() == {"undine": ix.DEFAULT_URL, "acme": "git@git.acme.example:idx.git",
                               "lab": "https://lab.example/idx.git"}
    # a file with other tables only: [indexes] is appended
    _cfg().write_text("[other]\nk = 1")
    assert ix.add_configured("lab", "https://lab.example/idx.git")
    assert _cfg().read_text() == '[other]\nk = 1\n\n[indexes]\nlab = "https://lab.example/idx.git"\n'


def test_index_add_into_a_project_and_from_the_cli(tmp_path, capsys):
    store, prj = _project(tmp_path)
    assert _cli(store, "index", "add", "acme", "https://idx.acme.example/i.git", "--project", "p") == 0
    assert json.loads((prj.path / "project.json").read_text())["indexes"] == {"acme": "https://idx.acme.example/i.git"}
    assert "added to p's project.json" in capsys.readouterr().out
    assert _cli(store, "index", "add", "acme", "https://other.example/i.git", "--project", "p") == 1
    assert "already" in capsys.readouterr().out
    assert _cli(store, "index", "add", "lab", "https://lab.example/idx.git") == 0
    assert ix.configured()["lab"] == "https://lab.example/idx.git"


# ------------------------------------------------------------------ pkg remove


def test_pkg_remove_is_lib_remove(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    w.publish("amp", "1.0.0")
    store, prj = _project(tmp_path)
    deps.add(prj, "amp", None, package="amp")
    assert _cli(store, "remove", "p", "amp") == 0
    assert "amp" not in json.loads((prj.path / "project.json").read_text())["libraries"]
    assert not deps.dep_dir(prj.path, "amp").exists()


# ------------------------------------------------------------------ pkg migrate


def _release(w: World, name: str, version: str, extra: dict[str, str] | None = None) -> tuple[Path, str]:
    """A release whose manifest includes the symbols only; `extra` files lie outside the include set."""
    repo = w.repos / name
    manifest = f'[package]\nname = "{name}"\nversion = "{version}"\n\n[files]\ninclude = ["*.sym"]\n'
    return repo, _commit(repo, {f"{name}_cell.sym": SYM, "undine-package.toml": manifest, **(extra or {})},
                         f"{name} {version}", f"v{version}")


def _list(w: World, repo: Path, commit: str, name: str, version: str) -> None:
    url = str(repo)
    with cas.store_lock(shared=True):
        gitsrc.fetch(url, None, commit, commit)
        files = gitsrc.blob_tree(url, commit, None, "include")
    w.append({"name": name, "vers": version, "git": url, "commit": commit, "tree": cas.tree_hash(files), "deps": [],
              "license": "MIT", "yanked": False})


def test_migrate_turns_a_git_dependency_into_a_package_from_the_index(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    repo, commit = _release(w, "amp", "1.0.0", {"tests/run.sh": "#!/bin/sh\n", "README.md": "amp\n"})
    _list(w, repo, commit, "amp", "1.0.0")
    store, prj = _project(tmp_path)
    lk = deps.read_lock(prj.path)                                   # locked in format 1: every file of the subdir
    deps.write_lock(prj.path, {**lk, "format": 1, "pdk": None, "tools": {}})
    deps.add(prj, "amp", str(repo), rev="v1.0.0")
    lk = deps.read_lock(prj.path)
    assert "sha" in lk["libraries"]["amp"]
    deps.write_lock(prj.path, {**lk, "format": 2})                  # as lib lock --format 2: entries as they are
    d = deps.dep_dir(prj.path, "amp")
    assert (d / "tests" / "run.sh").is_file()
    assert _cli(store, "migrate", "p", "amp") == 0
    out = capsys.readouterr().out
    assert "now amp 1.0.0 from" in out and "README.md, tests/run.sh" in out
    meta = json.loads((prj.path / "project.json").read_text())
    assert meta["libraries"]["amp"] == {"version": "=1.0.0"}
    e = deps.read_lock(prj.path)["libraries"]["amp"]
    assert e["index"] == str(w.index) and e["version"] == "1.0.0" and e["commit"] == commit and "rev" not in e
    assert e.get("files") == "include"
    assert (d / "amp_cell.sym").is_file() and not (d / "tests").exists() and not (d / "README.md").exists()
    prj = ProjectStore(store.root, {}).get("p")                     # as the CLI left it
    assert deps.status(prj)["ok"], deps.status(prj)["issues"]
    with pytest.raises(deps.DepError, match="package from an index already"):
        deps.migrate(prj, "amp")


def test_migrate_refuses_another_release_a_missing_version_no_manifest_and_format_1(tmp_path):
    w = World(tmp_path)
    w.default()
    repo, commit = _release(w, "amp", "1.0.0")
    store, prj = _project(tmp_path)
    deps.add(prj, "amp", str(repo), rev="v1.0.0")
    with pytest.raises(deps.DepError, match="lists no amp 1.0.0, the version installed \\(it lists none\\)"):
        deps.migrate(prj, "amp")
    # the index's 1.0.0 is another commit whose symbol differs: not the same release
    other = tmp_path / "fork"
    _commit(other, {"amp_cell.sym": SYM + "* changed\n", "undine-package.toml":
                    '[package]\nname = "amp"\nversion = "1.0.0"\n\n[files]\ninclude = ["*.sym"]\n'}, "fork", "v1.0.0")
    _list(w, other, _git(other, "rev-parse", "HEAD"), "amp", "1.0.0")
    before = ((prj.path / "project.json").read_text(), (prj.path / deps.LOCK).read_text())
    with pytest.raises(deps.DepError, match="differ from the installed ones: amp_cell.sym"):
        deps.migrate(prj, "amp")
    assert ((prj.path / "project.json").read_text(), (prj.path / deps.LOCK).read_text()) == before
    # no manifest at all
    plain = tmp_path / "plain"
    _commit(plain, {"x.sym": SYM}, "plain", "v1")
    deps.add(prj, "plain", str(plain), rev="v1")
    with pytest.raises(deps.DepError, match="have no undine-package.toml"):
        deps.migrate(prj, "plain")
    # a format 1 lock
    lk = deps.read_lock(prj.path)
    lk["format"] = 1
    deps.write_lock(prj.path, lk)
    with pytest.raises(deps.NeedsFormat2):
        deps.migrate(prj, "amp")


# ------------------------------------------------------------------ regressions from the review


def test_index_add_keeps_a_link_the_mode_and_crlf(tmp_path):
    import os
    import stat as st
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_bytes(b'[other]\r\nk = 1\r\n[indexes]\r\nacme = "https://acme.example/i.git"\r\n')
    os.chmod(real, 0o600)
    _cfg().parent.mkdir(parents=True, exist_ok=True)
    _cfg().symlink_to(real)
    assert ix.add_configured("lab", "https://lab.example/idx.git")
    assert _cfg().is_symlink()                                       # the link stays, its target gets the entry
    assert real.read_bytes() == (b'[other]\r\nk = 1\r\n[indexes]\r\nacme = "https://acme.example/i.git"\r\n'
                                 b'lab = "https://lab.example/idx.git"\r\n')
    assert st.S_IMODE(real.stat().st_mode) == 0o600
    assert not list(real.parent.glob(".config.toml.*"))


def test_index_add_from_two_threads_keeps_both():
    import threading
    errors = []

    def add(n: str) -> None:
        try:
            ix.add_configured(n, f"https://{n}.example/idx.git")
        except Exception as e:                                         # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=add, args=(f"i{k}",)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and {f"i{k}" for k in range(8)} <= set(ix.configured())


def test_a_refused_migrate_leaves_a_format_1_lock_as_it_was(tmp_path, capsys):
    w = World(tmp_path)
    w.default()
    repo, _ = _release(w, "amp", "1.0.0")
    store, prj = _project(tmp_path)
    lk = deps.read_lock(prj.path)
    deps.write_lock(prj.path, {**lk, "format": 1, "pdk": None, "tools": {}})
    deps.add(prj, "amp", str(repo), rev="v1.0.0")
    before = (prj.path / deps.LOCK).read_text()
    for name in ("amp", "ampp"):                                      # not listed; not a dependency
        assert _cli(store, "migrate", "p", name) == 1
        assert "error:" in capsys.readouterr().out
        assert (prj.path / deps.LOCK).read_text() == before


def test_migrate_refuses_a_dependency_changed_after_its_checks(tmp_path, monkeypatch):
    """The checks run without the dependency lock; update() refuses when what they compared changed meanwhile."""
    w = World(tmp_path)
    w.default()
    repo, commit = _release(w, "amp", "1.0.0")
    _list(w, repo, commit, "amp", "1.0.0")
    _commit(repo, {"amp_cell.sym": SYM + "* v2\n"}, "v2", "v2.0.0")
    store, prj = _project(tmp_path)
    deps.add(prj, "amp", str(repo), rev="v1.0.0")
    real = deps.update

    def racing(prj_, name, *a, **k):
        if k.get("replace") is not None:                              # just before migrate's update takes the lock
            real(ProjectStore(store.root, {}).get("p"), "amp", "v2.0.0")
        return real(prj_, name, *a, **k)
    monkeypatch.setattr(deps, "update", racing)
    with pytest.raises(deps.DepError, match="changed meanwhile"):
        deps.migrate(prj, "amp")
    assert json.loads((prj.path / "project.json").read_text())["libraries"]["amp"]["rev"] == "v2.0.0"
