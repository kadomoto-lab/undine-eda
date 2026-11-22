"""Phase 3 of docs/design/packages.md: package indexes. store/index.py reads an index (a git repository of JSON lines,
one per version) and chooses one version per package (solve); store/deps.py turns the choice into git requirements
pinned at the commits the index names, so that add, update, lib lock and sync work for packages from an index as for
git ones. Every index and repository here is local, under the test's temporary directory."""
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402
from undine.store import cas, deps, gitsrc  # noqa: E402
from undine.store import index as ix  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402
from undine.store.semver import Req, Version  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
SYM = "v {}\nK {type=subcircuit}\n"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str, tag: str | None = None) -> str:
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    if tag:
        _git(repo, "tag", tag)
    return _git(repo, "rev-parse", "HEAD")


class World:
    """Package repositories and an index (the default one, through $UNDINE_HOME/config.toml) in tmp_path."""

    def __init__(self, tmp_path: Path, name: str = "index"):
        self.root = tmp_path
        self.repos = tmp_path / f"repos-{name}"
        self.index = tmp_path / name
        _commit(self.index, {"config.json": json.dumps({"name": name, "format": 1})}, "index")

    def default(self) -> None:
        cfg = Path(ix.home()) / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(f'[indexes]\nundine = "{self.index}"\n')

    def package(self, name: str, version: str, deps_: dict[str, str] | None = None, cells=None, pdk: str = "",
                repo: Path | None = None) -> tuple[Path, str]:
        """A release of package `name` in its repository, tagged v<version>; returns (repository, commit)."""
        repo = repo or self.repos / name
        lines = ["[package]", f'name = "{name}"', f'version = "{version}"']
        if deps_:
            lines += ["", "[dependencies]", *(f"{k} = {v}" for k, v in deps_.items())]
        files = {f"{c}.sym": SYM for c in (cells or (f"{name}_cell",))}
        files["undine-package.toml"] = "\n".join(lines) + "\n" + pdk
        return repo, _commit(repo, files, f"{name} {version}", f"v{version}")

    def publish(self, name: str, version: str, deps_: list[dict] | None = None, *, yanked: bool = False,
                tree: str | None = None, manifest_deps: dict[str, str] | None = None, cells=None, pdk_line=None,
                **more) -> dict:
        """Releases `name` `version` and appends its line to the index (deps_ as index lines give them; the
        manifest's [dependencies] follow unless `manifest_deps` says otherwise)."""
        if manifest_deps is None:
            manifest_deps = {}
            for d in deps_ or []:
                body = f'version = "{d.get("req", "*")}"'
                if d.get("package"):
                    body = f'package = "{d["package"]}", ' + body
                if d.get("index"):
                    body += f', index = "{d["index"]}"'
                manifest_deps[d["name"]] = "{ " + body + " }"
        repo, commit = self.package(name, version, manifest_deps, cells)
        url = str(repo)
        with cas.store_lock(shared=True):
            gitsrc.fetch(url, None, f"v{version}")
            files = gitsrc.blob_tree(url, commit, None, "include")
        line = {"name": name, "vers": version, "git": url, "commit": commit, "tree": tree or cas.tree_hash(files),
                "deps": deps_ or [], "license": "Apache-2.0", "yanked": yanked, **more}
        if pdk_line is not None:
            line["pdk"] = pdk_line
        self.append(line)
        return line

    def append(self, line: dict) -> None:
        rel = ix.rel_path(line["name"])
        p = self.index / rel
        text = p.read_text() if p.exists() else ""
        _commit(self.index, {rel: text + json.dumps(line) + "\n"}, f"{line['name']} {line['vers']}")

    def yank(self, name: str, version: str) -> None:
        rel = ix.rel_path(name)
        p = self.index / rel
        out = []
        for raw in p.read_text().splitlines():
            d = json.loads(raw)
            if d["vers"] == version:
                d["yanked"] = True
            out.append(json.dumps(d))
        _commit(self.index, {rel: "\n".join(out) + "\n"}, f"yank {name} {version}")


def _project(tmp_path: Path, meta: dict | None = None):
    store = ProjectStore(tmp_path / "projects", {})
    prj = store.create("p", None)
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    if meta:
        prj.meta.update(meta)
        prj.save()
    return store, prj


def _lock(prj) -> dict:
    return json.loads((prj.path / deps.LOCK).read_text())


def _refused(call, kind=deps.DepError) -> deps.DepError:
    with pytest.raises(kind) as e:
        call()
    return e.value


# ------------------------------------------------------------------ the format


def test_package_files_follow_crates_io_index_paths():
    assert [ix.rel_path(n) for n in ("a", "op", "inv", "analog_ip")] == \
        ["1/a.jsonl", "2/op.jsonl", "3/i/inv.jsonl", "an/al/analog_ip.jsonl"]
    with pytest.raises(ix.IndexReadError, match="not a package name"):
        ix.rel_path("Bad-Name")


GOOD = {"name": "analog_ip", "vers": "1.2.0", "git": "https://example.com/a.git", "commit": "a" * 40,
        "tree": "sha256:" + "b" * 64, "deps": [{"name": "bias", "package": "bias_gen", "req": "~0.3"}]}


def test_a_line_is_read_and_its_dependencies_parsed():
    line = ix.parse_line(json.dumps(GOOD), "x", "https://i")
    assert (line.label, line.index, line.deps[0].text()) == ("analog_ip 1.2.0", "https://i", "bias_gen ~0.3 as bias")
    assert not line.yanked


@pytest.mark.parametrize("change, msg", [
    ({"commit": "abc"}, "not a full commit id"), ({"tree": "sha256:xyz"}, "not a tree hash"),
    ({"git": "-uupload-pack=x"}, "not a git URL"), ({"subdir": "../x"}, "subdir must be"),
    ({"vers": "1.2"}, "not semver"), ({"name": "Analog"}, "not a package name"), ({"extra": 1}, "unknown key"),
    ({"deps": [{"name": "x", "req": ">>1"}]}, "req:"), ({"yanked": "yes"}, "yanked is not"),
])
def test_a_bad_line_is_refused_naming_what(change, msg):
    with pytest.raises(ix.IndexReadError, match=msg):
        ix.parse_line(json.dumps({**GOOD, **change}), "x")


def test_a_package_file_lists_each_version_once_newest_first():
    a, b = json.dumps(GOOD), json.dumps({**GOOD, "vers": "1.10.0"})
    lines = ix.parse_file(f"{a}\n\n{b}\n".encode(), "analog_ip", "f")
    assert [str(x.vers) for x in lines] == ["1.10.0", "1.2.0"]
    with pytest.raises(ix.IndexReadError, match="listed twice"):
        ix.parse_file(f"{a}\n{a}\n".encode(), "analog_ip", "f")
    with pytest.raises(ix.IndexReadError, match="a line for analog_ip in the file of other"):
        ix.parse_file(a.encode(), "other", "f")


CHECK = {"url": "https://github.com/me/analog_ip/releases/download/v1.2.0/undine-check.json", "sha256": "5f" * 32}
FULL = {"name": "analog_ip", "vers": "1.2.0", "git": "https://github.com/me/analog_ip.git", "subdir": "lib",
        "commit": "2" * 40, "tree": "sha256:" + "e" * 64,
        "pdk": {"family": "sky130", "variants": ["sky130A"], "compat": ["sky130/2025"], "built_with": "8" * 40},
        "deps": [{"name": "sar_common", "req": "^1.2"}, {"name": "bias", "package": "bias_gen", "req": "~0.3"},
                 {"name": "tech_cells", "req": "^2", "index": "git@github.com:acme/undine-index.git"}],
        "exports": ["bandgap", "ldo", "comparator"], "license": "Apache-2.0", "description": "Bandgap, LDO — für",
        "size": 1843200, "check": CHECK, "yanked": False}


def test_line_text_round_trips_through_parse_line_and_parse_file():
    line = ix.parse_line(json.dumps(FULL), "x", "https://i")
    text = ix.line_text(line)
    assert ix.parse_line(text, "y", "https://i") == line
    assert ix.parse_file((text + "\n").encode(), "analog_ip", "f", "https://i") == [line]
    assert text == json.dumps(FULL, ensure_ascii=False, separators=(",", ":"))     # the design's key order, compact
    assert "\n" not in text and "für" in text
    bare = ix.parse_line(json.dumps(GOOD), "x")
    assert ix.line_text(bare) == ('{"name":"analog_ip","vers":"1.2.0","git":"https://example.com/a.git",'
                                  f'"commit":"{"a" * 40}","tree":"sha256:{"b" * 64}",'
                                  '"deps":[{"name":"bias","package":"bias_gen","req":"~0.3"}],"yanked":false}')
    with pytest.raises(ix.IndexReadError, match="analog_ip 1.0.0: the line written: tree 'sha256:xyz' is not a tree"):
        ix.line_text(ix.Line("analog_ip", Version(1, 0, 0), "https://e/a.git", "a" * 40, "sha256:xyz"))


@pytest.mark.parametrize("check, msg", [
    ({"url": CHECK["url"]}, "exactly url and sha256"), ({**CHECK, "size": 1}, "exactly url and sha256"),
    ({**CHECK, "url": "http://example.com/r.json"}, "not an https URL"), ({**CHECK, "url": "https:///r.json"}, "https"),
    ({**CHECK, "url": "https://u:p@example.com/r.json"}, "no user or password"),
    ({**CHECK, "url": "https://example.com/a b"}, "url is not a URL"), ({**CHECK, "url": 3}, "url is not a URL"),
    ({**CHECK, "sha256": "5F" * 32}, "sha256 is not 64 lower-case hex"), ({**CHECK, "sha256": "ab"}, "sha256 is not"),
    ("https://example.com/r.json", "exactly url and sha256"),
])
def test_check_needs_an_https_url_and_a_sha256(check, msg):
    with pytest.raises(ix.IndexReadError, match=f"x: check .*{msg}"):
        ix.parse_line(json.dumps({**GOOD, "check": check}), "x")
    assert ix.parse_line(json.dumps({**GOOD, "check": CHECK}), "x").check == CHECK


def test_yank_rewrites_the_one_line_and_never_appends():
    a = ix.line_text(ix.parse_line(json.dumps(FULL), "x"))
    b = json.dumps({**GOOD, "vers": "1.3.0"})                         # written by hand: spaces, no yanked key
    data = f"{a}\r\n{b}\n".encode()
    once = ix.yank_rewrite(data, "analog_ip", "1.2.0")
    marked = a.replace('"yanked":false', '"yanked":true')
    assert once == f"{marked}\r\n{b}\n".encode()                     # one value changed, CRLF kept
    assert [x.yanked for x in ix.parse_file(once, "analog_ip", "f")] == [False, True]
    twice = ix.yank_rewrite(once, "analog_ip", Version(1, 3, 0))
    assert twice.count(b"\n") == 2 and twice.startswith(once.split(b"\n")[0])
    assert json.loads(twice.decode().splitlines()[1]) == {**GOOD, "vers": "1.3.0", "yanked": True}
    assert ix.yank_rewrite(once, "analog_ip", "1.2.0", yanked=False) == data
    with pytest.raises(ix.IndexReadError, match="analog_ip 1.2.0 is yanked already"):
        ix.yank_rewrite(once, "analog_ip", "1.2.0")
    with pytest.raises(ix.IndexReadError, match="no line for analog_ip 9.9.9"):
        ix.yank_rewrite(data, "analog_ip", "9.9.9")
    with pytest.raises(ix.IndexReadError, match="listed twice"):
        ix.yank_rewrite(f"{a}\n{a}\n".encode(), "analog_ip", "1.2.0")


def test_a_description_with_unicode_line_breaks_keeps_the_file_readable():
    odd = "one two three\x85four"
    line = ix.parse_line(json.dumps({**FULL, "description": odd}), "x")
    text = ix.line_text(line)
    assert " " not in text and "\x85" not in text and "\\u2028" in text and "\\u0085" in text
    other = json.dumps({**GOOD, "vers": "1.3.0", "description": odd}, ensure_ascii=False)    # raw, by hand
    data = f"{text}\n{other}\n".encode()
    assert [x.description for x in ix.parse_file(data, "analog_ip", "f")] == [odd, odd]
    once = ix.yank_rewrite(data, "analog_ip", "1.3.0")
    assert once.count(b"\n") == 2 and once.startswith(text.encode() + b"\n")
    assert [x.yanked for x in ix.parse_file(once, "analog_ip", "f")] == [True, False]
    assert ix.parse_file(once, "analog_ip", "f")[0].description == odd


def test_yank_refuses_a_file_that_is_not_utf8_and_splits_lines_as_parse_file_does():
    a = json.dumps({**GOOD, "description": "x"})
    bad = (a.replace('"x"', '"\xff"').encode("latin-1") + b"\n")
    assert ix.parse_file(bad, "analog_ip", "f")                     # read with replacement characters
    with pytest.raises(ix.IndexReadError, match="not UTF-8"):
        ix.yank_rewrite(bad, "analog_ip", "1.2.0")
    for sep in ("\x0b", "\x0c", "\x1c", "\x1e"):                   # str.splitlines would split there; JSON refuses it
        with pytest.raises(ix.IndexReadError, match="not JSON"):
            ix.parse_file(f"{a}{sep}{a}\n".encode(), "analog_ip", "f")
        with pytest.raises(ix.IndexReadError, match="not JSON"):
            ix.yank_rewrite(f"{a}{sep}{a}\n".encode(), "analog_ip", "1.2.0")


def test_nicknames_come_from_the_machine_and_the_project(tmp_path):
    assert ix.url_of(None) == ix.DEFAULT_URL
    (Path(ix.home()) / "config.toml").parent.mkdir(parents=True, exist_ok=True)
    (Path(ix.home()) / "config.toml").write_text('[indexes]\nundine = "/mirror"\nlab = "git@host:lab/idx.git"\n')
    assert ix.url_of(None) == "/mirror" and ix.url_of("lab") == "git@host:lab/idx.git"
    meta = {"indexes": {"lab": "https://lab.example/idx.git"}}
    assert ix.url_of("lab", meta) == "https://lab.example/idx.git"        # the project's wins: a clone finds it
    with pytest.raises(ix.IndexReadError, match="no index is named 'acme'"):
        ix.url_of("acme", meta)


# ------------------------------------------------------------------ the choice


def _line(name, vers, deps_=(), yanked=False, index="I", pdk=None):
    return ix.Line(name, Version.parse(vers), f"/{name}", "c" * 40, "sha256:" + "0" * 64, index, None, pdk,
                   tuple(ix.Dep(*d) if isinstance(d, tuple) else d for d in deps_), yanked=yanked)


def _lines(*lines):
    table: dict = {}
    for x in lines:
        table.setdefault((x.index, x.name), []).append(x)
    for v in table.values():
        v.sort(key=lambda x: x.vers, reverse=True)
    return lambda url, name: table.get((url, name), [])


def _want(package, req, index="I", local=None, chain=("app",)):
    return ix.Want(local or package, package, Req.parse(req), index, chain)


def _picked(got):
    return {k[1]: str(v.vers) for k, v in got.items()}


def test_solve_takes_the_newest_version_every_requirement_accepts():
    lines = _lines(_line("a", "1.0.0", [("c", "c", Req.parse("^1"))]), _line("b", "1.0.0", [("c", "c", Req.parse("~1.1"))]),
                   _line("c", "1.0.0"), _line("c", "1.1.3"), _line("c", "1.2.0"), _line("c", "2.0.0"))
    assert _picked(ix.solve([_want("a", "*"), _want("b", "*")], lines)) == {"a": "1.0.0", "b": "1.0.0", "c": "1.1.3"}


def test_solve_backtracks_to_an_older_version_whose_dependencies_fit():
    lines = _lines(_line("a", "2.0.0", [("c", "c", Req.parse("^2"))]), _line("a", "1.5.0", [("c", "c", Req.parse("^1"))]),
                   _line("c", "1.0.0"), _line("c", "2.0.0"))
    got = ix.solve([_want("a", "*"), _want("c", "^1")], lines)
    assert _picked(got) == {"a": "1.5.0", "c": "1.0.0"}


def test_conflicting_requirements_name_both_chains():
    lines = _lines(_line("a", "1.0.0", [("c", "c", Req.parse("^2"))]), _line("c", "1.0.0"), _line("c", "2.0.0"))
    with pytest.raises(ix.SolveError) as e:
        ix.solve([_want("a", "^1"), _want("c", "~1.0")], lines)
    text = str(e.value)
    assert "c: no version meets all of them" in text and "app -> a 1.0.0 -> c ^2" in text and "app -> c ~1.0" in text


def test_yanked_versions_are_skipped_unless_locked():
    lines = _lines(_line("c", "1.0.0"), _line("c", "1.1.0", yanked=True))
    assert _picked(ix.solve([_want("c", "^1")], lines)) == {"c": "1.0.0"}
    locked = {("I", "c", Version.parse("1.1.0"))}
    assert _picked(ix.solve([_want("c", "^1")], lines, locked=locked, prefer={("I", "c"): Version.parse("1.1.0")})) \
        == {"c": "1.1.0"}
    with pytest.raises(ix.SolveError, match="every c version that matches is yanked"):
        ix.solve([_want("c", "=1.1.0")], lines)


def test_held_packages_stay_and_a_requirement_beyond_them_says_how_to_move_them():
    lines = _lines(_line("c", "1.0.0"), _line("c", "1.4.0"))
    held = {("I", "c"): Version.parse("1.0.0")}
    assert _picked(ix.solve([_want("c", "^1")], lines, held=held)) == {"c": "1.0.0"}
    with pytest.raises(ix.SolveError, match=r"c is locked at 1.0.0: `pkg update c` moves it"):
        ix.solve([_want("c", "^1.2")], lines, held=held)


def test_one_package_name_from_two_indexes_is_refused():
    lines = _lines(_line("c", "1.0.0", index="I"), _line("c", "1.0.0", index="J"))
    with pytest.raises(ix.SolveError, match="c: from two indexes, I .* and J"):
        ix.solve([_want("c", "*", "I"), _want("c", "*", "J", local="c2")], lines)


def test_a_dependency_without_an_index_comes_from_the_index_of_the_package_naming_it():
    lines = _lines(_line("a", "1.0.0", [("c", "c", Req.parse("*"))], index="ACME"), _line("c", "1.0.0", index="ACME"),
                   _line("c", "9.0.0", index="I"))
    assert {k: str(v.vers) for k, v in ix.solve([_want("a", "*", "ACME")], lines).items()} == \
        {("ACME", "a"): "1.0.0", ("ACME", "c"): "1.0.0"}


def test_versions_that_do_not_fit_the_pdk_are_skipped():
    lines = _lines(_line("c", "1.0.0", pdk={"family": "sky130"}), _line("c", "2.0.0", pdk={"family": "gf180mcu"}))
    fits = (lambda x: None if x.pdk["family"] == "sky130" else "family gf180mcu, this project is sky130")
    assert _picked(ix.solve([_want("c", "*")], lines, pdk_check=fits)) == {"c": "1.0.0"}
    with pytest.raises(ix.SolveError, match="no c version that matches fits this project's PDK"):
        ix.solve([_want("c", "^2")], lines, pdk_check=fits)


def test_a_package_missing_from_the_index_is_named():
    with pytest.raises(ix.SolveError, match="index I lists no package zz"):
        ix.solve([_want("zz", "*")], _lines())


# ------------------------------------------------------------------ add, sync, update, lock


def test_add_from_the_index_installs_the_newest_fitting_versions_and_sync_reinstalls_them(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.0.0")
    w.publish("sar_common", "1.1.0")
    w.publish("sar_common", "2.0.0")
    w.publish("analog_ip", "1.2.0", [{"name": "sar_common", "req": "^1.0"}])
    store, prj = _project(tmp_path)
    r = deps.add(prj, "analog_ip", None)
    assert (r["package"], r["version"]) == ("analog_ip", "1.2.0")
    assert prj.meta["libraries"]["analog_ip"] == {"version": "^1.2.0"}            # cargo add: ^ the version taken
    lock = _lock(prj)
    a, c = lock["libraries"]["analog_ip"], lock["packages"]["sar_common"]
    assert (a["index"], a["version"], a["package"], a["deps"]) == (str(w.index), "1.2.0", "analog_ip",
                                                                    {"sar_common": "sar_common"})
    assert (c["index"], c["version"], "rev" in c) == (str(w.index), "1.1.0", False)
    assert (prj.path / deps.DEPS / "sar_common" / "sar_common_cell.sym").is_file()
    shutil.rmtree(prj.path / deps.DEPS)                                          # a fresh clone
    rows = {x["name"]: x["action"] for x in deps.sync(store.get("p"))}
    assert rows == {"analog_ip": "installed", "sar_common": "installed"}
    assert deps.status(store.get("p"))["ok"]


def test_update_fetches_the_index_again_and_moves_to_the_newest_accepted_version(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("bias_gen", "0.3.0")
    store, prj = _project(tmp_path)
    deps.add(prj, "bias", None, package="bias_gen", version="~0.3")
    assert prj.meta["libraries"]["bias"] == {"package": "bias_gen", "version": "~0.3"}
    w.publish("bias_gen", "0.3.4")
    w.publish("bias_gen", "0.4.0")
    deps.update(prj, "bias")
    assert _lock(prj)["libraries"]["bias"]["version"] == "0.3.4"
    deps.update(prj, "bias", version="^0.4")
    assert (_lock(prj)["libraries"]["bias"]["version"], prj.meta["libraries"]["bias"]["version"]) == ("0.4.0", "^0.4")


def test_a_yanked_version_stays_locked_but_a_new_add_skips_it(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.0.0")
    w.publish("sar_common", "1.1.0")
    _, prj = _project(tmp_path)
    deps.add(prj, "sar_common", None)
    w.yank("sar_common", "1.1.0")
    ix.Index(str(w.index)).update()
    r = deps.relock(prj)
    assert r["resolved"] == [] and _lock(prj)["libraries"]["sar_common"]["version"] == "1.1.0"
    assert any("sar_common 1.1.0 is yanked in undine: it stays locked" in x for x in deps.status(prj)["warnings"])
    _, other = _project(tmp_path / "other")
    deps.add(other, "sar_common", None)
    assert _lock(other)["libraries"]["sar_common"]["version"] == "1.0.0"


def test_lib_lock_resolves_a_requirement_changed_in_project_json(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.0.0")
    w.publish("sar_common", "2.0.0")
    _, prj = _project(tmp_path)
    deps.add(prj, "sar_common", None, version="^1")
    prj.meta["libraries"]["sar_common"]["version"] = "^2"                        # a hand edit, or a pull
    prj.save()
    assert "locks 1.0.0" in str(_refused(lambda: deps.sync(prj)))
    deps.relock(prj)
    assert _lock(prj)["libraries"]["sar_common"]["version"] == "2.0.0"


def test_an_index_line_whose_tree_differs_from_the_repository_is_refused(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.0.0", tree="sha256:" + "1" * 64)
    _, prj = _project(tmp_path)
    before = (prj.path / "project.json").read_text()
    e = _refused(lambda: deps.add(prj, "sar_common", None))
    assert "the index line and the repository disagree" in str(e) and "nothing was changed" in str(e)
    assert (prj.path / "project.json").read_text() == before


def test_a_manifest_that_disagrees_with_its_index_line_is_refused(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.0.0")
    w.publish("analog_ip", "1.0.0", [{"name": "sar_common", "req": "^1"}], manifest_deps={})
    _, prj = _project(tmp_path)
    assert "[dependencies] differ from the index line's deps" in str(_refused(lambda: deps.add(prj, "analog_ip", None)))


def test_a_private_index_named_in_project_json_and_cargo_s_index_rule(tmp_path):
    public, acme = World(tmp_path, "public"), World(tmp_path, "acme")
    public.default()
    public.publish("sar_common", "9.0.0")                                       # same name, someone else's
    acme.publish("sar_common", "1.0.0")
    acme.publish("tech_cells", "1.0.0", [{"name": "sar_common", "req": "^1"}])
    _, prj = _project(tmp_path, {"indexes": {"acme": str(acme.index)}})
    deps.add(prj, "tech_cells", None, index="acme")
    lock = _lock(prj)
    assert (lock["packages"]["sar_common"]["version"], lock["packages"]["sar_common"]["index"]) == \
        ("1.0.0", str(acme.index))
    e = _refused(lambda: deps.add(prj, "sar_common", None))                      # the public one too: refused
    assert "sar_common: from two indexes" in str(e)


def test_a_git_package_may_name_an_index_dependency(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.3.0")
    repo, _ = w.package("adc_top", "0.1.0", {"sar_common": '"^1.2"'})
    _, prj = _project(tmp_path)
    deps.add(prj, "adc_top", str(repo), rev="v0.1.0")
    c = _lock(prj)["packages"]["sar_common"]
    assert (c["version"], c["index"]) == ("1.3.0", str(w.index))


def test_a_format_1_lock_refuses_a_package_from_an_index(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.0.0")
    store = ProjectStore(tmp_path / "projects", {})
    prj = store.create("p", None)
    assert "needs lock format 2" in str(_refused(lambda: deps.add(prj, "sar_common", None)))


def test_index_search_and_versions(tmp_path):
    w = World(tmp_path)
    w.publish("sar_common", "1.0.0", description="common SAR blocks")
    w.publish("bias_gen", "0.3.0")
    w.publish("bias_gen", "0.4.0", yanked=True)
    i = ix.Index(str(w.index))
    assert i.names() == ["bias_gen", "sar_common"]
    assert [x.label for x in i.search("bias")] == ["bias_gen 0.3.0"]           # the newest that is not yanked
    assert [x.label for x in i.search("SAR blocks")] == ["sar_common 1.0.0"]
    assert [str(x.vers) for x in i.versions("bias_gen")] == ["0.4.0", "0.3.0"] and i.versions("zz") == []


def test_an_index_of_another_format_is_refused(tmp_path):
    bad = tmp_path / "bad"
    _commit(bad, {"config.json": json.dumps({"format": 7})}, "x")
    with pytest.raises(ix.IndexReadError, match="format 7, this Undine reads format 1"):
        ix.Index(str(bad)).update()


# ------------------------------------------------------------------ the command line


def _run_cli(root: Path, *argv: str) -> tuple[int, str]:
    import contextlib
    import io
    from undine import __main__ as cli
    out, argv0 = io.StringIO(), sys.argv
    sys.argv = ["undine", "--root", str(root / "projects"), "--pdk-root", str(root / "no-pdks"), *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.cli()
                code = 0
            except SystemExit as e:
                code = e.code or 0
    finally:
        sys.argv = argv0
    return code, out.getvalue()


def test_pkg_commands_search_add_tree_why_outdated_and_update(tmp_path):
    w = World(tmp_path)
    w.default()
    w.publish("sar_common", "1.0.0", description="common SAR blocks")
    w.publish("analog_ip", "1.2.0", [{"name": "sar_common", "req": "^1.0"}], description="an ADC and a bandgap")
    _project(tmp_path)
    code, out = _run_cli(tmp_path, "pkg", "search", "sar")
    assert code == 0 and "sar_common 1.0.0  common SAR blocks (Apache-2.0)" in out
    code, out = _run_cli(tmp_path, "pkg", "info", "analog_ip")
    assert code == 0 and "needs    sar_common ^1.0" in out and "versions 1.2.0" in out
    code, out = _run_cli(tmp_path, "pkg", "add", "p", "analog_ip@^1.2")
    assert code == 0, out
    assert "analog_ip 1.2.0 at" in out and "requirement ^1.2" in out and "+ sar_common 1.0.0 as sar_common" in out
    code, out = _run_cli(tmp_path, "pkg", "tree", "p")
    assert out.splitlines()[1:] == ["└── analog_ip 1.2.0", "    └── sar_common 1.0.0"]
    code, out = _run_cli(tmp_path, "pkg", "why", "p", "sar_common")
    assert out.strip() == "p -> analog_ip 1.2.0 (" + _lock_commit(tmp_path, "analog_ip") + ") -> sar_common 1.0.0 (" \
        + _lock_commit(tmp_path, "sar_common") + ")"
    w.publish("sar_common", "1.4.0")
    code, out = _run_cli(tmp_path, "pkg", "outdated", "p")
    assert "sar_common           1.0.0      -          1.4.0      via analog_ip" in out
    code, out = _run_cli(tmp_path, "pkg", "update", "p")
    assert code == 0 and "sar_common: 1.0.0 -> 1.4.0" in out                 # what only analog_ip reaches moves too
    code, out = _run_cli(tmp_path, "pkg", "index", "list")
    assert f"undine       {w.index}  fetched at" in out
    code, out = _run_cli(tmp_path, "pkg", "add", "p", "nothing_here")
    assert code == 1 and "lists no package nothing_here" in out


def _lock_commit(tmp_path: Path, package: str) -> str:
    lock = json.loads((tmp_path / "projects" / "p" / deps.LOCK).read_text())
    e = (lock.get("libraries") or {}).get(package) or lock["packages"][package]
    return e["commit"][:12]
