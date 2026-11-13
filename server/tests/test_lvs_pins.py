"""KLayout LVS: a top-cell pin the layout does not carry is a mismatch, not a match.

The databases are real results of the demo Tiny Tapeout tile (examples/make_tt_inverter.py), compared flat with
VGND as the substrate: lvs_tt_vdpwr_open has the via stack from the inverter's VDD to the VDPWR stripe missing its
met3-met4 via (an undo took it back), lvs_tt_ok is the same tile with the via. KLayout itself reports both as matching:
it pairs the schematic pin VDPWR with no layout pin and its net with the unnamed supply net of the inverter.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.checks import verify  # noqa: E402

DATA = Path(__file__).with_name("data")


def test_a_supply_cut_from_its_pin_is_a_mismatch():
    s = verify.lvs_summary(DATA / "lvs_tt_vdpwr_open.lvsdb")
    assert s["circuits"][0]["status"] == "Match"                       # what KLayout says
    assert s["match"] is False
    assert s["pin_errors"] == ["pin VDPWR: no VDPWR label on the layout net it matched ($12): "
                               "not connected to the VDPWR pin in the layout?"]
    pins = {p["schematic"]: p["layout"] for p in verify.lvs_detail(DATA / "lvs_tt_vdpwr_open.lvsdb")["pins"]}
    assert pins["VDPWR"] is None                                       # the detail shows it too


def test_the_connected_tile_still_matches():
    s = verify.lvs_summary(DATA / "lvs_tt_ok.lvsdb")
    assert s["match"] is True and "pin_errors" not in s
