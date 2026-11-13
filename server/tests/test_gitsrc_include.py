"""store/gitsrc.py, phase 2: blob_tree's include file set (files='include': only what the top-level manifest's [files]
include selects, plus the manifest, is checked and read into the store) and read_file (one file at a commit)."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from undine.store import cas, gitsrc, manifest
from undine.store.gitsrc import GitSourceError
from undine.store.manifest import MANIFEST, ManifestError

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")

HEAD = '[package]\nname = "ip"\nversion = "1.0.0"\n'
INCLUDE = HEAD + '[files]\ninclude = ["*.sch", "*.sym", "rtl/**"]\n'


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _g(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(d: Path, files: dict[str, bytes | str], links: dict[str, str] | None = None,
          submodules: tuple[str, ...] = ()) -> tuple[str, str]:
    """A repository with one commit of `files`, symbolic `links` and gitlink `submodules`; (url, kept commit)."""
    d.mkdir(parents=True)
    _g(d, "init", "-q", "-b", "main")
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
    for rel, target in (links or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, d / rel)
    _g(d, "add", "-A")
    _g(d, "commit", "-qm", "1")
    for rel in submodules:
        _g(d, "update-index", "--add", "--cacheinfo", f"160000,{_g(d, 'rev-parse', 'HEAD')},{rel}")
    if submodules:
        _g(d, "commit", "-qm", "2")
    url = str(d)
    return url, gitsrc.fetch(url, None, "main")


def _objects(store: Path) -> set[str]:
    root = store / "objects"
    return {p.parent.name + p.name for p in root.rglob("*") if p.is_file()} if root.is_dir() else set()


def _hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _no_blob_read(monkeypatch):
    def never(repo):
        raise AssertionError("cat-file --batch started")
    monkeypatch.setattr(gitsrc, "_cat_file", never)


# ------------------------------------------------------------------ the include set


def test_the_include_tree_is_the_selected_files_plus_the_manifest_and_nothing_else_is_read(tmp_path, fresh_store):
    url, c = _repo(tmp_path / "src", {
        "lib/" + MANIFEST: INCLUDE, "lib/amp.sch": "sch\n", "lib/amp.sym": "sym\n", "lib/rtl/top.sv": "module t;\n",
        "lib/sim/tb.spice": "outside include\n", "lib/README.md": "outside too\n", "lib/sub/amp.sch": "not top\n"})
    got = gitsrc.blob_tree(url, c, "lib", files="include")
    assert sorted(got) == sorted([MANIFEST, "amp.sch", "amp.sym", "rtl/top.sv"])
    expect = {MANIFEST: ("100644", _hex(INCLUDE)), "amp.sch": ("100644", _hex("sch\n")),
              "amp.sym": ("100644", _hex("sym\n")), "rtl/top.sv": ("100644", _hex("module t;\n"))}
    assert cas.tree_hash(got) == cas.tree_hash(expect)
    # the store holds the selected files only: nothing outside include was ever read into it
    assert _objects(fresh_store) == {h for _, h in expect.values()}
    assert cas.tree_hash(gitsrc.blob_tree(url, c, "lib")) != cas.tree_hash(got)
    assert _hex("outside include\n") in _objects(fresh_store)           # files='all' reads it, as phase 1 did


def test_without_files_or_without_a_manifest_include_gives_the_all_tree(tmp_path):
    url, c = _repo(tmp_path / "src", {"a/" + MANIFEST: HEAD, "a/amp.sch": "s\n", "a/doc/x.md": "x\n",
                                      "b/amp.sch": "s\n", "b/notes.txt": "n\n"})
    for sub in ("a", "b"):
        assert gitsrc.blob_tree(url, c, sub, files="include") == gitsrc.blob_tree(url, c, sub, files="all")


def test_files_must_be_all_or_include(tmp_path):
    url, c = _repo(tmp_path / "src", {"amp.sch": "s\n"})
    with pytest.raises(ValueError, match="'all' or 'include'"):
        gitsrc.blob_tree(url, c, "", files="every")


def test_a_link_or_submodule_is_refused_only_inside_the_include_set(tmp_path):
    url, c = _repo(tmp_path / "src", {MANIFEST: INCLUDE, "amp.sch": "s\n", "docs/a.txt": "a\n"},
                   links={"docs/link.txt": "a.txt"}, submodules=("vendor/sub",))
    assert sorted(gitsrc.blob_tree(url, c, "", files="include")) == sorted([MANIFEST, "amp.sch"])
    with pytest.raises(GitSourceError, match=r"link\.txt is a symbolic link"):
        gitsrc.blob_tree(url, c, "", files="all")
    url, c = _repo(tmp_path / "in", {MANIFEST: INCLUDE, "amp.sch": "s\n", "rtl/a.sv": "a\n"},
                   links={"amp.sym": "amp.sch"}, submodules=("rtl/sub",))
    with pytest.raises(GitSourceError, match=r"amp\.sym is a symbolic link"):
        gitsrc.blob_tree(url, c, "", files="include")
    url, c = _repo(tmp_path / "sm", {MANIFEST: INCLUDE, "amp.sch": "s\n"}, submodules=("rtl/sub",))
    with pytest.raises(GitSourceError, match=r"rtl/sub is a submodule"):
        gitsrc.blob_tree(url, c, "", files="include")


def test_an_lfs_pointer_or_bad_path_outside_include_is_not_read(tmp_path):
    lfs = "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 12345\n"
    url, c = _repo(tmp_path / "src", {MANIFEST: INCLUDE, "amp.sch": "s\n", "gds/big.gds": lfs,
                                      "x/" + cas.MARKER: "{}\n"})
    assert sorted(gitsrc.blob_tree(url, c, "", files="include")) == sorted([MANIFEST, "amp.sch"])
    with pytest.raises(GitSourceError, match="is named"):
        gitsrc.blob_tree(url, c, "", files="all")


def test_limits_apply_to_the_selected_entries_only(tmp_path, monkeypatch):
    url, c = _repo(tmp_path / "src", {MANIFEST: INCLUDE, "amp.sch": "s\n",
                                      **{f"big/{i}.bin": "x" * 100 for i in range(5)}})
    monkeypatch.setattr(gitsrc, "MAX_FILES", 2)
    monkeypatch.setattr(gitsrc, "MAX_BYTES", len(INCLUDE) + 2)
    assert sorted(gitsrc.blob_tree(url, c, "", files="include")) == sorted([MANIFEST, "amp.sch"])
    _no_blob_read(monkeypatch)
    with pytest.raises(GitSourceError, match="7 files under the top, more than the 2"):
        gitsrc.blob_tree(url, c, "", files="all")
    monkeypatch.setattr(gitsrc, "MAX_FILES", 100)
    with pytest.raises(GitSourceError, match="bytes under the top, more than the"):
        gitsrc.blob_tree(url, c, "", files="all")
    monkeypatch.setattr(gitsrc, "MAX_BYTES", len(INCLUDE) + 1)            # the selection is over now
    with pytest.raises(GitSourceError, match=rf"{len(INCLUDE) + 2} bytes under the top"):
        gitsrc.blob_tree(url, c, "", files="include")


def test_the_listing_is_bounded_at_ten_times_max_files(tmp_path, monkeypatch):
    url, c = _repo(tmp_path / "src", {MANIFEST: INCLUDE, "amp.sch": "s\n",
                                      **{f"big/{i}.bin": str(i) for i in range(30)}})
    monkeypatch.setattr(gitsrc, "MAX_FILES", 2)
    _no_blob_read(monkeypatch)
    for files in ("all", "include"):
        with pytest.raises(GitSourceError, match="more than 20 entries under the top"):
            gitsrc.blob_tree(url, c, "", files=files)


def test_an_oversized_or_irregular_manifest_is_refused_before_any_other_blob_is_read(tmp_path, monkeypatch):
    _no_blob_read(monkeypatch)
    big = HEAD + "# " + "x" * manifest.MAX_BYTES + "\n"
    url, c = _repo(tmp_path / "big", {"lib/" + MANIFEST: big, "lib/amp.sch": "s\n"})
    with pytest.raises(ManifestError, match=rf"@ {c[:12]}:lib: {MANIFEST}: \d+ bytes, more than {manifest.MAX_BYTES}"):
        gitsrc.blob_tree(url, c, "lib", files="include")
    url, c = _repo(tmp_path / "link", {"real.toml": HEAD, "amp.sch": "s\n"}, links={MANIFEST: "real.toml"})
    with pytest.raises(ManifestError, match=f"{MANIFEST}: is a symbolic link: a manifest is a regular file"):
        gitsrc.blob_tree(url, c, "", files="include")
    url, c = _repo(tmp_path / "sm", {"amp.sch": "s\n"}, submodules=(MANIFEST,))
    with pytest.raises(ManifestError, match=f"{MANIFEST}: is a submodule"):
        gitsrc.blob_tree(url, c, "", files="include")
    url, c = _repo(tmp_path / "dir", {MANIFEST + "/x": "x\n", "amp.sch": "s\n"})
    with pytest.raises(ManifestError, match=f"{MANIFEST}: is a directory"):
        gitsrc.blob_tree(url, c, "", files="include")
    url, c = _repo(tmp_path / "bad", {MANIFEST: "[package]\nname = 1\n", "amp.sch": "s\n"})
    with pytest.raises(ManifestError, match="package.name"):
        gitsrc.blob_tree(url, c, "", files="include")


def test_a_manifest_below_the_top_is_not_the_package_manifest(tmp_path):
    url, c = _repo(tmp_path / "src", {"amp.sch": "s\n", "docs/" + MANIFEST: "not toml at all [[["})
    assert sorted(gitsrc.blob_tree(url, c, "", files="include")) == ["amp.sch", "docs/" + MANIFEST]


def test_the_manifest_checks_the_selected_files(tmp_path):
    text = HEAD + '[files]\ninclude = ["rtl/**"]\n'
    url, c = _repo(tmp_path / "src", {MANIFEST: text, "amp.sch": "s\n", "rtl/a.sv": "a\n"})
    with pytest.raises(ManifestError, match="files.include: selects no top-level cell"):
        gitsrc.blob_tree(url, c, "", files="include")
    text = INCLUDE + '[exports]\ncells = ["amp", "ldo"]\n'
    url, c = _repo(tmp_path / "ex", {MANIFEST: text, "amp.sch": "s\n", "ldo.oas": "l\n"})
    with pytest.raises(ManifestError, match="exports.cells: .* for ldo"):
        gitsrc.blob_tree(url, c, "", files="include")


# ------------------------------------------------------------------ read_file


def test_read_file_reads_one_file_without_the_tree(tmp_path, fresh_store):
    url, c = _repo(tmp_path / "src", {"lib/" + MANIFEST: INCLUDE, "lib/amp.sch": "s\n", "a*b": "glob\n",
                                      "lib/sub/x.txt": "x\n"},
                   links={"lib/link.toml": MANIFEST})
    assert gitsrc.read_file(url, c, "lib", MANIFEST, manifest.MAX_BYTES) == INCLUDE.encode()
    assert gitsrc.read_file(url, c, "./lib/", MANIFEST, manifest.MAX_BYTES) == INCLUDE.encode()
    assert gitsrc.read_file(url, c, "", MANIFEST, manifest.MAX_BYTES) is None
    assert gitsrc.read_file(url, c, "absent", MANIFEST, manifest.MAX_BYTES) is None
    assert gitsrc.read_file(url, c, "", "a*b", 100) == b"glob\n"
    assert gitsrc.read_file(url, c, "", "a?b", 100) is None                # a name, not a pattern
    assert _objects(fresh_store) == set()                                  # nothing went into the store
    with pytest.raises(GitSourceError, match=r"lib/link\.toml is a symbolic link"):
        gitsrc.read_file(url, c, "lib", "link.toml", 100)
    with pytest.raises(GitSourceError, match=r"lib/sub is a directory"):
        gitsrc.read_file(url, c, "lib", "sub", 100)
    with pytest.raises(GitSourceError, match=rf"lib/{MANIFEST} is {len(INCLUDE)} bytes, more than the 10"):
        gitsrc.read_file(url, c, "lib", MANIFEST, 10)


def test_read_file_checks_its_arguments_before_git_runs(tmp_path, monkeypatch):
    url, c = _repo(tmp_path / "src", {MANIFEST: HEAD, "amp.sch": "s\n"})

    def no_git(*a, **k):
        raise AssertionError("git ran")
    monkeypatch.setattr(gitsrc, "_run", no_git)
    for name in ("../x", "/etc/passwd", "a/../b", ""):
        with pytest.raises(GitSourceError, match="would leave"):
            gitsrc.read_file(url, c, "", name, 100)
    with pytest.raises(GitSourceError, match="subdir must be"):
        gitsrc.read_file(url, c, "../up", MANIFEST, 100)
    with pytest.raises(GitSourceError, match="not a full commit"):
        gitsrc.read_file(url, "--upload-pack=x", "", MANIFEST, 100)
    with pytest.raises(GitSourceError, match="not in the store's copy"):
        gitsrc.read_file(url, "0" * 40, "", MANIFEST, 100)
