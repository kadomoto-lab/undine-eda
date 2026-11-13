"""OSDI models built into the store: an IHP simulation loads them from store/build/<commit>/<variant>/osdi, not from the
PDK tree; scripts/osdi.sh moves models an earlier Undine compiled into its cache into the store instead of compiling
them again; store gc and verify leave build/ alone. No real PDK, OpenVAF or ngspice is needed: nothing here is skipped."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.pdks.pdk import LEGACY_OSDI, discover  # noqa: E402
from undine.pdks import pdkprofile  # noqa: E402
from undine.server.api import Server  # noqa: E402
from undine.simulation import sim  # noqa: E402
from undine.store import cas  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
COMMIT = "89d42ee6d2fd9b50b86a1aada6fee6673c3e6008"
MODELS = pdkprofile.IHP.osdi


@pytest.fixture(autouse=True)
def _store(fresh_store, tmp_path, monkeypatch):
    monkeypatch.setenv("UNDINE_CACHE", str(tmp_path / "cache"))
    return fresh_store


def _ihp(base: Path) -> Path:
    """A fake IHP SG13G2 install under ciel's layout, with the PDK's compile script; returns the PDK_ROOT."""
    root = base / "pdks"
    v = root / f"ciel/ihp-sg13/versions/{COMMIT}/ihp-sg13g2/libs.tech"
    (v / "klayout/tech/drc").mkdir(parents=True)
    (v / "klayout/tech/drc/ihp-sg13g2.drc").write_text("# deck\n")
    (v / "verilog-a").mkdir()
    (v / "verilog-a/openvaf-compile-va.sh").write_text("echo 'not run by these tests' >&2; exit 1\n")
    return root


def _write(d: Path, text: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for m in MODELS:
        (d / f"{m}.osdi").write_text(text)


def test_profile_lists_model_names():
    assert MODELS == ("psp103", "psp103_nqs", "r3_cmc", "mosvar", "cap_cmomi", "cap_cmomf")
    assert pdkprofile.IHP_CMOS5L.osdi == MODELS


def test_an_ihp_simulation_loads_osdi_from_the_store(fresh_store, tmp_path, monkeypatch):
    # a home of its own: spiceinit puts ~/.spiceinit first, and IHP's setup copies one with osdi lines there
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _ihp(tmp_path)
    pdks = discover([root])
    srv = Server(tmp_path / "projects", pdks, [root])
    prj = srv.store.create("p", next(iter(pdks)))
    build = fresh_store / "build" / COMMIT / "ihp-sg13g2" / "osdi"
    assert prj.pdk.osdi_dir == build
    _write(build, "store")
    _write(prj.pdk.root / LEGACY_OSDI, "in the PDK tree")              # an older setup's copies: the store's win
    init = sim.spiceinit(srv._osdi(prj))
    loads = [ln for ln in init.splitlines() if ln.startswith("osdi ")]
    assert loads == [f"osdi '{build / f'{m}.osdi'}'" for m in MODELS]
    assert str(prj.pdk.root) not in init and not prj.pdk.legacy_osdi_cached()


def test_store_gc_and_verify_leave_build_alone(fresh_store):
    build = fresh_store / "build" / COMMIT / "ihp-sg13g2" / "osdi"
    _write(build, "store")
    cas.put_bytes(b"an object no tree names")
    assert cas.gc(set(), set())["objects"] == 1
    cas.verify()
    assert sorted(p.name for p in build.iterdir()) == sorted(f"{m}.osdi" for m in MODELS)


def _osdi_sh(tmp_path: Path, root: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    """scripts/osdi.sh with this test's store, cache and PDK_ROOT, and a home of its own (no ~/.ciel is scanned)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    e = {k: v for k, v in os.environ.items() if k not in ("FORCE", "DRY_RUN", "PDK_ROOT")}
    e |= {"HOME": str(home), "PDK_ROOT": str(root), "CONDA_PREFIX": str(tmp_path / "env"),
          "PATH": f"{Path(sys.executable).parent}:{e.get('PATH', '')}", "PYTHONPATH": str(REPO / "server"), **env}
    return subprocess.run(["bash", str(REPO / "scripts/osdi.sh"), *args], env=e, capture_output=True, text=True,
                          timeout=120)


def test_osdi_sh_dry_run_reports_a_move_from_the_old_cache(fresh_store, tmp_path):
    root = _ihp(tmp_path)
    pdk = next(iter(discover([root]).values()))
    old = Path(os.environ["UNDINE_CACHE"]) / "osdi" / "ihp-sg13g2@89d42ee6d2fd"
    assert pdk.old_osdi_cache == old
    _write(old, "compiled by an earlier Undine")
    r = _osdi_sh(tmp_path, root, DRY_RUN="1")
    assert r.returncode == 0, r.stderr
    assert f"would move psp103.osdi from {old} into {pdk.osdi_dir}" in r.stdout
    assert "compile" not in r.stdout.replace("already compiled", "") and "OpenVAF" not in r.stdout
    assert not (fresh_store / "build").exists() and len(list(old.iterdir())) == len(MODELS)    # nothing changed


def test_osdi_sh_moves_the_old_cache_into_the_store(fresh_store, tmp_path):
    root = _ihp(tmp_path)
    pdk = next(iter(discover([root]).values()))
    old = pdk.old_osdi_cache
    _write(old, "compiled by an earlier Undine")
    (pdk.osdi_dir).mkdir(parents=True)
    (pdk.osdi_dir / "psp103.osdi").write_text("compiled into the store")           # the store's copy is kept
    assert pdk.legacy_osdi_cached()
    r = _osdi_sh(tmp_path, root)
    assert r.returncode == 0, r.stderr
    assert "moved psp103_nqs.osdi" in r.stdout and "moved psp103.osdi" not in r.stdout
    assert [f.parent for f in pdk.osdi_files()] == [pdk.osdi_dir] * len(MODELS)
    assert (pdk.osdi_dir / "psp103.osdi").read_text() == "compiled into the store"
    assert (pdk.osdi_dir / "r3_cmc.osdi").read_text() == "compiled by an earlier Undine"
    assert not pdk.legacy_osdi_cached() and [p.name for p in old.iterdir()] == ["psp103.osdi"]
    assert not list(pdk.osdi_dir.glob(".*"))                                         # no temporary file left
