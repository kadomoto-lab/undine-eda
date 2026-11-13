"""The ciel wrapper, against a fake ciel (tests/fake_ciel.py): --pdk-family takes the family, never the variant."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fake_ciel import make_fake_ciel, read_log  # noqa: E402
from undine.pdks import ciel  # noqa: E402
from undine.pdks.pdk import discover, family_of, installs  # noqa: E402

SKY = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
IHP = "89d42ee6d2fd9b50b86a1aada6fee6673c3e6008"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    log = tmp_path / "ciel.log"
    exe = make_fake_ciel(tmp_path / "bin", [SKY, IHP], log=log)
    monkeypatch.setenv("UNDINE_CIEL", str(exe))
    return log


def test_fetch_passes_the_family_and_returns_the_variant(tmp_path, fake):
    root = tmp_path / "pdks"
    seen = []
    out = ciel.fetch(root, family_of("sky130A"), "sky130A", SKY, ["sky130_fd_sc_hd"], on_output=seen.append)
    assert out == root / "ciel" / "sky130" / "versions" / SKY / "sky130A" and (out / "libs.tech").is_dir()
    argv = read_log(fake)[-1]
    assert argv == ["fetch", "--pdk-root", str(root), "--pdk-family", "sky130", "--data-source", ciel.DEFAULT_SOURCE,
                    "-l", "sky130_fd_sc_hd", SKY]
    assert any("Fetching" in x for x in seen)                         # ciel's output reaches the caller
    found = {p.name: p for p in installs([root])}                     # one fetch installs the whole family
    assert set(found) == {"sky130A", "sky130B"}
    assert found["sky130A"].libraries() == ["sky130_fd_sc_hd"] and found["sky130A"].source == "ciel"
    assert found["sky130A"].commit == SKY and found["sky130A"].variant == "sky130A"
    assert list(discover([root])) == [f"sky130A@{SKY[:12]}", f"sky130B@{SKY[:12]}"]


def test_ihp_is_fetched_as_its_family(tmp_path, fake):
    out = ciel.fetch(tmp_path / "pdks", family_of("ihp-sg13g2"), "ihp-sg13g2", IHP, None)
    argv = read_log(fake)[-1]
    assert argv[argv.index("--pdk-family") + 1] == "ihp-sg13" and out.parent.parent.parent.name == "ihp-sg13"


def test_no_libraries_means_no_l_and_ciels_default_set(tmp_path, fake):
    out = ciel.fetch(tmp_path / "pdks", "sky130", "sky130A", SKY, None)
    assert "-l" not in read_log(fake)[-1]
    assert (out / "libs.ref" / "sky130_fd_pr").is_dir()
    ciel.fetch(tmp_path / "pdks2", "sky130", "sky130A", SKY, [])
    assert "-l" not in read_log(fake)[-1]


def test_the_fake_refuses_a_variant_as_the_family(tmp_path, fake, monkeypatch):
    # what ciel does when handed a variant: the fake must catch a caller that passes one
    import subprocess
    r = subprocess.run([str(Path(ciel.exe())), "fetch", "--pdk-root", str(tmp_path), "--pdk-family", "sky130A", SKY],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "unknown PDK family" in r.stderr
    with pytest.raises(ciel.CielError, match="not the ciel family"):     # the wrapper refuses before running it
        ciel.fetch(tmp_path, "sky130A", "sky130A", SKY, None)
    assert read_log(fake)[-1][4] == "sky130A"                            # only the direct run above reached the fake


def test_a_failing_fetch_raises(tmp_path, monkeypatch):
    log = tmp_path / "ciel.log"
    monkeypatch.setenv("UNDINE_CIEL", str(make_fake_ciel(tmp_path / "bin", [SKY], fail=True, log=log)))
    with pytest.raises(ciel.CielError, match=r"ciel fetch failed \(exit 1\): fake ciel: told to fail"):
        ciel.fetch(tmp_path / "pdks", "sky130", "sky130A", SKY, None)
    with pytest.raises(ciel.CielError, match="ls-remote failed"):
        ciel.ls_remote("sky130")


def test_an_unknown_commit_and_a_missing_tree_are_errors(tmp_path, fake, monkeypatch):
    with pytest.raises(ciel.CielError, match="not found"):
        ciel.fetch(tmp_path / "pdks", "sky130", "sky130A", "0" * 40, None)
    # ciel exits 0 but the variant is not where it should be (a family without that variant): never trust stdout
    monkeypatch.setenv("UNDINE_CIEL", str(make_fake_ciel(tmp_path / "bin2", [SKY], variants_of={"sky130": ["sky130B"]})))
    with pytest.raises(ciel.CielError, match="libs.tech is not there"):
        ciel.fetch(tmp_path / "pdks", "sky130", "sky130A", SKY, None)


def test_bad_arguments_are_refused_before_ciel_runs(tmp_path, fake):
    for commit in (SKY[:12], "-" + SKY[1:], "x" * 40):
        with pytest.raises(ciel.CielError, match="not a full commit"):
            ciel.fetch(tmp_path, "sky130", "sky130A", commit, None)
    with pytest.raises(ciel.CielError, match="not a valid name"):
        ciel.fetch(tmp_path, "sky130", "sky130A", SKY, ["--pdk-root=/etc"])
    with pytest.raises(ciel.CielError, match="data source"):
        ciel.fetch(tmp_path, "sky130", "sky130A", SKY, None, source="--help")
    assert read_log(fake) == []


def test_ls_remote_passes_the_family_and_lists_commits(fake):
    assert ciel.ls_remote("sky130") == [SKY, IHP]
    assert read_log(fake) == [["ls-remote", "--pdk-family", "sky130"]]


def test_a_ciel_beside_python_does_not_shadow_undine_ciel(tmp_path, fake, monkeypatch):
    bindir = tmp_path / "venv" / "bin"
    real = make_fake_ciel(bindir, ["1" * 40])                         # stands for pixi's own ciel
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    assert ciel.exe() != real and ciel.ls_remote("sky130") == [SKY, IHP]
    monkeypatch.delenv("UNDINE_CIEL")
    assert ciel.exe() == real
    monkeypatch.setenv("UNDINE_CIEL", str(tmp_path / "missing"))     # set but absent: no other ciel is run
    assert ciel.exe() is None
    with pytest.raises(ciel.CielError, match="ciel is not installed"):
        ciel.ls_remote("sky130")


def test_a_hanging_ciel_is_stopped(tmp_path, monkeypatch):
    exe = tmp_path / "ciel"
    exe.write_text("#!/bin/sh\nexec sleep 30\n")
    exe.chmod(0o755)
    monkeypatch.setenv("UNDINE_CIEL", str(exe))
    with pytest.raises(ciel.CielError, match="did not finish"):
        ciel.ls_remote("sky130", timeout=0.5)
