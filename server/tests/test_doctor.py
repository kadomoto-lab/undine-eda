"""`python -m undine doctor`: tool rows with their number and origin, the ciel `pdk fetch` runs, Undine's version and
pixi.lock, and the hint for OSDI models still in the old cache. Fake executables only (UNDINE_TOOLS, UNDINE_CIEL)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import doctor, tools  # noqa: E402
from undine.pdks.pdk import discover  # noqa: E402

BANNERS = {"klayout": "KLayout 0.30.12", "xschem": "XSCHEM V3.4.5",
           "ngspice": "******\n** ngspice-47 : Circuit level simulation program", "magic": "magic build 7",
           "netgen": "Netgen 1.5.272 compiled on Mon Jan 1"}


@pytest.fixture(autouse=True)
def _store(fresh_store, tmp_path, monkeypatch):
    """Fake tools under UNDINE_TOOLS (found ahead of the environment's and PATH), and a cache of this test's own."""
    bin_ = tmp_path / "tools" / "bin"
    bin_.mkdir(parents=True)
    for name, banner in BANNERS.items():
        exe = bin_ / name
        exe.write_text("#!/bin/sh\ncat <<'EOF'\n" + banner + "\nEOF\n")
        exe.chmod(0o755)
    monkeypatch.setenv("UNDINE_TOOLS", str(tmp_path / "tools"))
    monkeypatch.setenv("UNDINE_CACHE", str(tmp_path / "cache"))
    return fresh_store


def _rows(pdks=None) -> dict[str, tuple[str, str, str]]:
    return {what: (level, status, fix) for level, what, status, fix in doctor.checks(pdks or {})}


def test_tool_rows_show_number_and_origin(tmp_path):
    rows = _rows()
    bin_ = tmp_path / "tools" / "bin"
    assert rows["klayout (DRC / LVS)"][1] == f"ok  {bin_ / 'klayout'}  0.30.12 (UNDINE_TOOLS)"
    assert rows["ngspice (simulation)"][1] == f"ok  {bin_ / 'ngspice'}  47 (UNDINE_TOOLS)"
    assert rows["netgen (Magic + Netgen LVS)"][1] == f"ok  {bin_ / 'netgen'}  1.5.272 (UNDINE_TOOLS)"
    # a banner no rule reads: shown as it is
    assert rows["magic (Magic DRC, Magic + Netgen LVS)"][1] == f"ok  {bin_ / 'magic'}  magic build 7 (UNDINE_TOOLS)"


def test_info_rows_give_undine_version_and_pixi_lock():
    rows = _rows()
    assert rows["Undine"] == ("info", tools.undine_version(), "")
    assert rows["pixi.lock"][1] == (tools.pixi_lock_sha() or "none (not run in this checkout's pixi environment)")


def test_ciel_row_runs_what_pdk_fetch_runs(tmp_path, monkeypatch):
    fake = tmp_path / "ciel"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("UNDINE_CIEL", str(fake))
    assert _rows()["ciel (PDK manager)"][1] == f"ok  {fake}"
    monkeypatch.setenv("UNDINE_CIEL", str(tmp_path / "none"))      # set but not there: no other ciel is taken
    assert _rows()["ciel (PDK manager)"][1] == doctor.MISS


def test_osdi_row_hints_at_moving_the_old_cache(tmp_path):
    h = "89d42ee6d2fd9b50b86a1aada6fee6673c3e6008"
    (tmp_path / f"pdks/ciel/ihp-sg13/versions/{h}/ihp-sg13g2/libs.tech").mkdir(parents=True)
    pdks = discover([tmp_path / "pdks"])
    pdk = next(iter(pdks.values()))
    what = "compiled Verilog-A models of the PDKs that need them"
    level, status, fix = _rows(pdks)[what]
    assert status == doctor.MISS and "pixi run osdi" in fix
    pdk.old_osdi_cache.mkdir(parents=True)
    for m in pdk.profile.osdi:
        (pdk.old_osdi_cache / f"{m}.osdi").write_bytes(b"")
    level, status, fix = _rows(pdks)[what]
    assert status.startswith("ok  ") and "run `pixi run osdi` to move them into the store" in status and fix == ""
    for m in pdk.profile.osdi:                         # in the store: no hint
        pdk.osdi_dir.mkdir(parents=True, exist_ok=True)
        (pdk.osdi_dir / f"{m}.osdi").write_bytes(b"")
    level, status, fix = _rows(pdks)[what]
    assert status == f"ok  {pdk.id}"
