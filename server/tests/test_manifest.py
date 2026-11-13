"""store/manifest.py: undine-package.toml parsed and checked, the [files] include set, and reading a manifest from the
store or a directory. Every refusal names its key path and says what to do."""
import importlib.util
import os
import sys
import types

import pytest

from undine.store import cas, manifest
from undine.store.manifest import MANIFEST, GitDep, IndexDep, ManifestError, check_files, parse, select
from undine.store.semver import Req, Version

COMMIT = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"

# the design's example (docs/design/packages.md, Packages), index forms and all
DESIGN = f'''
[package]
name = "analog_ip"
version = "1.2.0"
description = "Bandgap, LDO and comparator for sky130"
license = "Apache-2.0"
authors = ["Kadomoto <k@example.org>"]
repository = "https://github.com/me/analog_ip.git"

[pdk]
family = "sky130"
variants = ["sky130A"]
compat = ["sky130/2025"]
built_with = "{COMMIT}"

[dependencies]
sar_common = "^1.2"
bias = {{ package = "bias_gen", version = "~0.3" }}
tech_cells = {{ version = "^2", index = "acme" }}

[exports]
cells = ["bandgap", "ldo", "comparator"]

[files]
include = ["*.sch", "*.sym", "*.oas", "*.netlist.yaml", "*.intent.yaml", "rtl/**", "README.md", "LICENSE"]

[checks]
drc = {{ deck = "sky130A_mr.drc", options = {{ feol = true, beol = true, density = false }} }}
waive = [{{ cell = "unit_cap", rules = ["m1.2"], reason = "abutment cell: clean once arrayed" }}]

[renames]
"1.2.0" = {{ comp = "comparator" }}
'''

MINIMAL = '[package]\nname = "ip"\nversion = "0.1.0"\n'


def P(text, where="analog_ip"):
    return parse(text.encode(), where)


def refused(text, *parts):
    with pytest.raises(ManifestError) as e:
        P(text, where="app -> ip")
    msg = str(e.value)
    assert msg.startswith(f"app -> ip: {MANIFEST}: "), msg
    for p in parts:
        assert p in msg, msg
    return msg


def test_the_design_example_parses():
    m = P(DESIGN)
    assert (m.name, m.version, m.license, m.authors) == ("analog_ip", Version(1, 2, 0), "Apache-2.0",
                                                        ("Kadomoto <k@example.org>",))
    assert m.repository == "https://github.com/me/analog_ip.git"
    assert (m.pdk.family, m.pdk.variants, m.pdk.compat, m.pdk.commits, m.pdk.built_with) == \
        ("sky130", ("sky130A",), ("sky130/2025",), (), COMMIT)
    assert m.dependencies == {
        "sar_common": IndexDep("sar_common", "sar_common", "^1.2"),
        "bias": IndexDep("bias", "bias_gen", "~0.3"),
        "tech_cells": IndexDep("tech_cells", "tech_cells", "^2", "acme"),
    }
    assert m.exports == ("bandgap", "ldo", "comparator")
    assert "rtl/**" in m.include
    assert m.later["checks"]["drc"]["deck"] == "sky130A_mr.drc"
    assert m.later["renames"] == {"1.2.0": {"comp": "comparator"}}


def test_git_and_sibling_requirements():
    m = P(MINIMAL + '''
[dependencies]
sar = { git = "https://example.org/sar.git", rev = "v1.3.1", version = "^1.3" }
bias = { git = "git@example.org:me/bias.git", rev = "main", subdir = "lib/bias", package = "bias_gen" }
common = { subdir = "common", version = "=0.2.0" }
''')
    sar, bias, common = (m.dependencies[k] for k in ("sar", "bias", "common"))
    assert sar == GitDep("sar", "sar", "https://example.org/sar.git", "v1.3.1", None, Req.parse("^1.3"))
    assert (bias.package, bias.subdir, bias.version) == ("bias_gen", "lib/bias", None)
    assert (common.git, common.rev, common.subdir, str(common.version)) == (None, None, "common", "=0.2.0")
    assert m.pdk is None and m.exports is None and m.include is None and m.later == {}


def test_a_minimal_manifest_has_defaults():
    m = P(MINIMAL)
    assert (m.description, m.license, m.authors, m.repository, m.dependencies) == (None, None, (), None, {})


@pytest.mark.parametrize("text, parts", [
    (MINIMAL + "[pakage]\nx = 1\n", ["pakage: unknown table (made for a newer Undine?)", "package, pdk, dependencies"]),
    (MINIMAL + '[dependencies]\nsar = { git = "https://e.org/s.git", revision = "v1" }\n',
     ["dependencies.sar.revision: unknown key (made for a newer Undine?)", "git, rev, subdir"]),
    (MINIMAL.replace('version = "0.1.0"', 'version = "0.1.0"\nhomepage = "x"'),
     ["package.homepage: unknown key", "name, version"]),
    (MINIMAL + "[files]\nincludes = []\n", ["files.includes: unknown key"]),
    (MINIMAL + '[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\ncommit = "x"\n', ["pdk.commit: unknown key"]),
    (MINIMAL + "[exports]\ncell = []\n", ["exports.cell: unknown key"]),
    ('[package]\nname = "Analog-IP"\nversion = "1.0.0"\n', ["package.name: 'Analog-IP' is not a package name",
                                                             "lower-case letters"]),
    ('[package]\nversion = "1.0.0"\n', ["package.name: missing"]),
    ('[package]\nname = "ip"\nversion = "1.2"\n',
     ["package.version: '1.2' is not semver: MAJOR.MINOR.PATCH, e.g. 1.2.0"]),
    ('[package]\nname = "ip"\n', ["package.version: missing"]),
    ('name = "ip"\n', ["name: unknown table"]),
    ("", ["package: missing", "add a [package] table"]),
    (MINIMAL.replace('version = "0.1.0"', 'version = "0.1.0"\nrepository = "-uhttps://x"'),
     ["package.repository", "not a git URL"]),
    (MINIMAL + '[dependencies]\nsar = { rev = "v1", subdir = "sar" }\n', ["dependencies.sar.rev: rev without git",
                                                                           "drop rev for a sibling"]),
    (MINIMAL + '[dependencies]\nsar = { git = "--upload-pack=touch x", rev = "v1" }\n',
     ["dependencies.sar.git: '--upload-pack=touch x' is not a git URL"]),
    (MINIMAL + '[dependencies]\nsar = { git = "https://e.org/s.git" }\n', ["dependencies.sar.rev: missing",
                                                                           'rev = "v1.2.0"']),
    (MINIMAL + '[dependencies]\nsar = { git = "https://e.org/s.git", rev = "--output=x" }\n',
     ["dependencies.sar.rev: '--output=x' is not a branch, tag or commit"]),
    (MINIMAL + '[dependencies]\nsar = { git = "https://e.org/s.git", rev = "v1", subdir = "../x" }\n',
     ["dependencies.sar.subdir: '../x' is not a relative path inside the repository", "no .."]),
    (MINIMAL + '[dependencies]\nsar = { subdir = "/abs" }\n', ["dependencies.sar.subdir", "no leading /"]),
    (MINIMAL + '[dependencies]\nsar = { subdir = "a/-b" }\n', ["dependencies.sar.subdir", "starting with -"]),
    (MINIMAL + '[dependencies]\nsar = { git = "https://e.org/s.git", rev = "v1", version = ">=1.2" }\n',
     ["dependencies.sar.version", "version ranges come with the package index (phase 3)"]),
    (MINIMAL + '[dependencies]\nsar = { git = "https://e.org/s.git", rev = "v1", index = "acme" }\n',
     ["dependencies.sar.index: index together with git or subdir"]),
    (MINIMAL + '[dependencies]\nip = { subdir = "x" }\n', ["dependencies.ip: the package depends on itself"]),
    (MINIMAL + '[dependencies]\nme = { subdir = "x", package = "ip" }\n',
     ["dependencies.me.package: the package depends on itself"]),
    (MINIMAL + '[dependencies]\nBad = { subdir = "x" }\n', ["dependencies.Bad: 'Bad' is not a local name"]),
    (MINIMAL + '[dependencies]\nb = { subdir = "x", package = "B-x" }\n', ["dependencies.b.package: 'B-x'"]),
    (MINIMAL + "[dependencies]\nb = 3\n", ["dependencies.b: 3 is neither a version nor a table"]),
    (MINIMAL + f'[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\ncommits = ["{COMMIT[:12]}"]\n',
     [f"pdk.commits: '{COMMIT[:12]}' is a prefix", "40 or 64 lower-case hex"]),
    (MINIMAL + f'[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\nbuilt_with = "{COMMIT.upper()}"\n',
     ["pdk.built_with", "is not a full commit"]),
    (MINIMAL + '[pdk]\nfamily = "sky130"\nvariants = ["gf180mcuD"]\n',
     ["pdk.variants: 'gf180mcuD' is not a variant of sky130"]),
    (MINIMAL + '[pdk]\nfamily = "sky130"\nvariants = []\n', ["pdk.variants: missing or empty"]),
    (MINIMAL + '[pdk]\nvariants = ["sky130A"]\n', ["pdk.family: missing"]),
    (MINIMAL + '[pdk]\nfamily = "Sky130"\nvariants = ["sky130A"]\n', ["pdk.family: 'Sky130' is not a PDK family"]),
    (MINIMAL + '[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\ncompat = ["gf180mcu/2025"]\n',
     ["pdk.compat: 'gf180mcu/2025' is not a compatibility tag of sky130"]),
    (MINIMAL + f'[pdk]\nfamily = "sky130"\nvariants = ["sky130A"]\ncommits = ["{COMMIT}"]\n'
               f'built_with = "{"1" * 40}"\n', ["pdk.built_with: not one of pdk.commits"]),
    (MINIMAL + '[exports]\ncells = ["ok", "../x"]\n', ["exports.cells: '../x' is not a cell name"]),
    (MINIMAL + '[files]\ninclude = ["../x/*.sch"]\n', ["files.include: '../x/*.sch' has a .. part"]),
    (MINIMAL + '[files]\ninclude = ["/abs/*.sch"]\n', ["files.include: '/abs/*.sch'", "relative"]),
    (MINIMAL + '[files]\ninclude = ["rtl\\\\*.v"]\n', ["files.include", "relative / path"]),
    (MINIMAL + '[files]\ninclude = ["rtl**/x"]\n', ["files.include: 'rtl**/x' has ** inside a part"]),
    (MINIMAL + "[files]\n", ["files.include: missing"]),
    (MINIMAL + 'checks = "deck"\n', ["package.checks: unknown key"]),
    ('checks = "deck"\n' + MINIMAL, ["checks: not a table"]),
    (MINIMAL + "[package.extra]\n", ["package.extra: unknown key"]),
])
def test_each_refusal_names_its_key_path_and_what_to_do(text, parts):
    refused(text, *parts)


def test_a_toml_error_keeps_line_and_column():
    msg = refused(MINIMAL + "[dependencies]\nsar = {\n", "not valid TOML", "line 5")
    assert "column" in msg


def test_size_and_encoding_are_checked_before_toml():
    big = (MINIMAL + "# " + "x" * manifest.MAX_BYTES + "\n").encode()
    with pytest.raises(ManifestError, match=f"more than {manifest.MAX_BYTES}"):
        parse(big, "ip")
    byte = len(MINIMAL) + 2
    with pytest.raises(ManifestError, match=rf"ip: undine-package.toml: not UTF-8 \(byte {byte}\): save it as UTF-8"):
        parse(MINIMAL.encode() + b"# \xff\n", "ip")


def test_select_matches_whole_paths_segment_by_segment():
    paths = ["inv.sch", "inv.sym", "sub/inv.sch", "rtl/a.v", "rtl/x/y/b.sv", "rtlx/c.v", "README.md", MANIFEST,
             "docs/undine-package.toml", "inv.oas"]
    assert select(["*.sch"], paths) == {"inv.sch", MANIFEST}                        # top-level cells only
    assert select(["rtl/**"], paths) == {"rtl/a.v", "rtl/x/y/b.sv", MANIFEST}
    assert select(["**/*.sch"], paths) == {"inv.sch", "sub/inv.sch", MANIFEST}      # ** matches zero parts too
    assert select(["rtl/*"], paths) == {"rtl/a.v", MANIFEST}                        # * never crosses /
    assert select(["inv.s[c]?"], paths) == {"inv.sch", MANIFEST}
    assert select([], paths) == {MANIFEST}
    assert select(None, paths) == set(paths)
    for bad in ("../x", "/x", "a//b", "a**", ""):
        with pytest.raises(ManifestError, match="files.include"):
            select([bad], paths)


def test_check_files_wants_a_top_level_cell_and_every_export():
    m = P(MINIMAL + '[files]\ninclude = ["cells/*.sch"]\n')
    with pytest.raises(ManifestError, match=r"ip: undine-package.toml: files.include: selects no top-level cell.*"
                                            r"\*\.sch matches top-level cells only"):
        check_files(m, ["cells/inv.sch", MANIFEST], "ip")
    with pytest.raises(ManifestError, match="no top-level cell"):
        check_files(P(MINIMAL), ["README.md", MANIFEST], "ip")
    m = P(MINIMAL + '[exports]\ncells = ["inv", "buf", "nand"]\n[files]\ninclude = ["*.sym", "*.oas"]\n')
    with pytest.raises(ManifestError, match=r"exports.cells: no .sym, .sch or .oas among the package's files for "
                                            r"buf, nand: add the cell's files to \[files\] include"):
        check_files(m, ["inv.sym", "buf.sch", "nand.v", MANIFEST], "ip")
    check_files(m, ["inv.sym", "buf.oas", "nand.sym", MANIFEST], "ip")
    check_files(P(MINIMAL + '[files]\ninclude = ["*.v"]\n'), ["top.v"], "ip")


def _stored(fresh_store, text: str) -> dict:
    return {MANIFEST: ("100644", cas.put_bytes(text.encode())), "inv.sch": ("100644", cas.put_bytes(b"v {}\n"))}


def test_from_files_reads_the_store_after_rehashing(fresh_store, monkeypatch):
    files = _stored(fresh_store, MINIMAL)
    checked = []
    real = cas.check_object
    monkeypatch.setattr(cas, "check_object", lambda h, *a, **k: checked.append(h) or real(h, *a, **k))
    assert manifest.from_files(files, "ip").name == "ip"
    assert checked == [files[MANIFEST][1]]
    assert manifest.from_files({"inv.sch": files["inv.sch"]}, "ip") is None


def test_from_files_refuses_a_modified_store_object(fresh_store):
    files = _stored(fresh_store, MINIMAL)
    p = cas.object_path(files[MANIFEST][1])
    os.chmod(p, 0o644)
    p.write_text(MINIMAL.replace("0.1.0", "9.9.9"))
    with pytest.raises(ManifestError, match="ip: undine-package.toml: the store's copy is missing or damaged"):
        manifest.from_files(files, "ip")


def test_from_tree_and_from_archive(fresh_store):
    files = _stored(fresh_store, MINIMAL)
    assert manifest.from_tree(cas.write_tree(files), "ip").version == Version(0, 1, 0)
    assert manifest.from_archive(cas.write_archive_tree(files), "ip").name == "ip"
    assert manifest.from_tree("sha256:" + "0" * 64, "ip") is None
    assert manifest.from_archive("0" * 64, "ip") is None


def test_from_dir(tmp_path):
    assert manifest.from_dir(tmp_path, "ip") is None
    (tmp_path / MANIFEST).write_text(MINIMAL)
    assert manifest.from_dir(tmp_path, "ip").name == "ip"
    other = tmp_path / "other"
    other.mkdir()
    (other / MANIFEST).symlink_to(tmp_path / MANIFEST)
    with pytest.raises(ManifestError, match="ip: undine-package.toml: is a symbolic link"):
        manifest.from_dir(other, "ip")


def test_parse_falls_back_to_tomli_on_python_3_10(monkeypatch):
    """Python 3.10 has no tomllib: the module imports tomli instead (the same parser, packaged on PyPI)."""
    import tomllib as real
    tomli = types.ModuleType("tomli")
    tomli.loads, tomli.TOMLDecodeError = real.loads, real.TOMLDecodeError
    monkeypatch.setitem(sys.modules, "tomllib", None)                 # `import tomllib` raises ModuleNotFoundError
    monkeypatch.setitem(sys.modules, "tomli", tomli)
    spec = importlib.util.spec_from_file_location("manifest_on_310", manifest.__file__)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "manifest_on_310", mod)          # dataclasses look their module up
    spec.loader.exec_module(mod)
    assert mod.tomllib is tomli
    assert mod.parse(DESIGN.encode(), "analog_ip").name == "analog_ip"
    with pytest.raises(mod.ManifestError, match="not valid TOML"):
        mod.parse(b"[package\n", "ip")
