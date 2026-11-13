"""Parasitic extraction (parasitics.py) and simulation on the extracted view (Server.start_sim extracted): the coupling
capacitor survives the rc mode and spreads along the wires, a net without a label keeps its capacitance there, an
inverter driving a long wire is slower extracted than drawn (and its run stores no node inside the extracted cell), and
an extraction that is outdated, missing, without the schematic's ports or with two of them shorted is refused. Those
with Magic are skipped without it, the sky130A PDK or its PCells (as test_pdk_installed.py does); the simulations also
need ngspice."""
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import klayout.db as kdb
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.checks import parasitics, verify  # noqa: E402
from undine.pdks import pcell  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.schematics import schematic, xschem  # noqa: E402
from undine.schematics.xschem import XComp, XFile  # noqa: E402
from undine.server.api import Server  # noqa: E402
from undine.simulation import sim  # noqa: E402
from undine import tools  # noqa: E402
from undine.store import provenance  # noqa: E402

PDKS = discover(default_pdk_roots())
MEAS = [".meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1"]
TRAN = {"type": "tran", "step": "5p", "stop": "4n"}


def _net(node: str) -> str:
    return re.sub(r"\.[tn]\d+$", "", node)


def _caps(text: str) -> dict[frozenset, float]:
    """Capacitance between nets in an extracted netlist (resistor nodes a.n0, a.t1 taken as net a), fF."""
    out: dict[frozenset, float] = {}
    for a, b, v in parasitics.read_spice_text(text)["caps"]:
        k = frozenset((_net(a), _net(b)))
        out[k] = out.get(k, 0.0) + v * 1e15
    return out


# ------------------------------------------------------------------ without Magic


def test_rc_capacitance_keeps_the_totals_of_the_c_mode():
    """Synthetic Magic output: net a has a resistor network (a, a.n0, a.t0), b one node, f capacitors only; Magic's rc
    netlist counts the coupling a second time to ground. The assembled netlist keeps the c mode's capacitance, spread
    over a's nodes."""
    d = Path(tempfile.mkdtemp())
    (d / "c.spice").write_text(".subckt top a VSS b x\nX0 a g VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15\n"
                               "C0 b a 10f\nC1 a VSUBS 4f $ **FLOATING\nC2 b VSUBS 3f\nC3 g a 0.001f\nC4 f b 1f\n.ends\n")
    (d / "rc.spice").write_text(".subckt top a VSS b x\nX0 a.t0 g VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15\n"
                                "R0 a a.n0 100\nR1 a.n0 a.t0 50\nC0 b a 10f\nC1 a VSUBS 14f\nC2 b VSUBS 13f\n.ends\n")
    (d / "top.ext").write_text('node "a" 150 4000 0 0 m1\nnode "b" 0 3000 500 30 m1\n'
                               'substrate "VSUBS" 0 0 -1073741817 -1073741817 space\n')
    (d / "top.res.ext").write_text('rnode "a" 0 5000 0 0 0\nrnode "a.n0" 0 5000 1000 0 0\nrnode "a.t0" 0 0 1010 0 0\n')
    text, full = parasitics.assemble(d, "top", "rc", ["b", "a", "VSS"], 0.01)
    assert ".subckt top b a VSS\n" in text                                   # the schematic's order; x is an inner net
    assert full["ports"] == {"match": True, "order": ["b", "a", "VSS"], "missing": [], "extra_labels": ["x"],
                             "from": "schematic"}
    caps = {tuple(l.split()[1:3]): float(l.split()[3][:-1]) for l in text.splitlines() if l.startswith("C")}
    assert caps == {("0", "a"): 2.0, ("0", "a.n0"): 2.0, ("0", "b"): 3.0, ("a", "b"): 5.0, ("a.n0", "b"): 5.0, ("b", "f"): 1.0}
    assert full["floating_nets"] == ["f"] and "Rfloat1 f 0 1e12" in text          # capacitors only: a DC path
    assert "R1 a a.n0 100" in text and "R2 a.n0 a.t0 50" in text
    assert not [l for l in text.splitlines() if "VSUBS" in l and not l.startswith("*")]        # the substrate is ground
    net = {r["net"]: r for r in full["c_per_net"]}
    assert net["a"] == {"net": "a", "ground_ff": 4.0, "coupling_ff": 10.0, "total_ff": 14.0}
    assert full["dropped_below_threshold"]["count"] == 1 and full["r_per_net"][0]["r_max_path_ohm"] == 150.0
    _, short = parasitics.assemble(d, "top", "c", ["a", "b", "VDD"], 0.01)
    assert short["ports"]["missing"] == ["VDD"] and not short["ports"]["match"]
    assert parasitics.compact(full, "a")["coupling"] == [{"nets": ["a", "b"], "c_ff": 10.0}]
    shutil.rmtree(d)


@pytest.mark.parametrize("comment", ["$", ";"])
def test_a_floating_comment_is_no_device_in_either_mode(comment):
    """Magic comments the substrate capacitor of a net without a label (8.3.465 with $, 8.3.678 with ;), in mode rc on
    every resistor node of a labelled net too: those lines are capacitors, not devices, and keep their capacitance."""
    d = Path(tempfile.mkdtemp())
    (d / "c.spice").write_text(".subckt top a VSS\nX0 a_0_0# a VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15\n"
                               f"C0 a_0_0# VSS 9f {comment} **FLOATING\nC1 a VSS 3f\n.ends\n")
    (d / "rc.spice").write_text(".subckt top a VSS\nX0 a_0_0.t0 a.t0 VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15\n"
                                f"R0 a a.t0 40\nC0 a_0_0.t0 VSS 9f {comment} **FLOATING\nC1 a.t0 VSS 3f {comment} **FLOATING\n"
                                ".ends\n")
    (d / "top.ext").write_text('port "a" 1 0 0 0 0 m1\nport "VSS" 2 0 0 0 0 m1\nnode "a_0_0#" 0 0 10 0 ndif\n'
                               'node "a" 0 0 0 0 m1\nsubstrate "VSS" 0 0 0 0 m1\n')
    (d / "top.res.ext").write_text('rnode "a" 0 1 0 0 0\nrnode "a.t0" 0 1 100 0 0\nresist "a" "a.t0" 40\n')
    assert parasitics.read_spice_text((d / "rc.spice").read_text())["devices"] == [
        "X0 a_0_0.t0 a.t0 VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15"]
    for mode in parasitics.MODES:
        text, full = parasitics.assemble(d, "top", mode, ["a", "VSS"], 0.01)
        assert full["devices"]["total"] == 1, mode
        assert {r["net"]: r["ground_ff"] for r in full["c_per_net"]} == {"a_0_0#": 9.0, "a": 3.0}, mode
        assert "FLOATING" not in text
    shutil.rmtree(d)


def _cdac(d: Path, nx: int, ny: int) -> None:
    """Synthetic Magic output of a CDAC of nx x ny sky130 MIM units 2 x 2 um, 4 um apart: each with its bottom plate
    (met3, b0 / b1 / b2 by column) and a met4 strap 1.8 x 2.4 um (1.8 x 1.9 um inside the capacitor) tied to the top
    plate t by a via3 inside it, except every 7th unit, under metal of another net (no via); a met4 wire below them all.
    The .ext places each device at its capacitor's corner, in Magic's units of 0.005 um."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("cdac")
    capm, m3, v3, m4 = ly.layer(89, 44), ly.layer(70, 20), ly.layer(70, 44), ly.layer(71, 20)
    ext = []
    for ix in range(nx):
        for iy in range(ny):
            x, y = ix * 4.0, iy * 4.0
            top.shapes(capm).insert(kdb.DBox(x, y, x + 2, y + 2))
            top.shapes(m3).insert(kdb.DBox(x - 0.2, y - 0.2, x + 2.2, y + 2.2))
            if (ix + iy) % 7:
                top.shapes(v3).insert(kdb.DBox(x + 0.9, y + 0.9, x + 1.1, y + 1.1))
            top.shapes(m4).insert(kdb.DBox(x + 0.1, y + 0.1, x + 1.9, y + 2.5))
            ext.append(f"device csubckt sky130_fd_pr__cap_mim_m3_1 {ix * 800} {iy * 800} {ix * 800 + 1} {iy * 800 + 1} "
                       f'w=400 l=400 "None" "t" 9216 0 "b{ix % 3}" 0 0,0')
    top.shapes(m4).insert(kdb.DBox(-1, -1, nx * 4.0, -0.5))
    ly.write(str(d / "input.gds"))
    (d / "cdac.ext").write_text("\n".join(ext) + "\n")
    (d / "units.txt").write_text("0.005\n")


def test_mim_overlap_of_a_cdac_is_found_at_once():
    """A CDAC's 2048 units: each tied strap's 3.42 um2 over its bottom plate is counted for its own device's nets, a
    strap of another net (no via inside the capacitor) is not, and all of it in well under the extraction's wait (each
    capacitor against the whole layout took two minutes)."""
    d = Path(tempfile.mkdtemp())
    _cdac(d, 32, 64)
    mims = [{"model": "sky130_fd_pr__cap_mim_m3_1", "cap": (89, 44), "bottom": (70, 20), "via": (70, 44),
             "top": (71, 20), "af": 84.03}]
    t0 = time.time()
    got = parasitics.mim_overlap(d, "cdac", mims)
    assert time.time() - t0 < 10
    tied = {f"b{b}": sum(1 for ix in range(32) for iy in range(64) if ix % 3 == b and (ix + iy) % 7) for b in range(3)}
    assert {k: v[2] for k, v in got.items()} == {(b, "t"): n for b, n in tied.items()}
    for (b, _), (f, um2, n, models) in got.items():
        assert um2 == pytest.approx(3.42 * n) and f == pytest.approx(3.42 * n * 84.03e-18)
        assert models == {"sky130_fd_pr__cap_mim_m3_1": 84.03}
    shutil.rmtree(d)


_TECH = """tech
 format 35
 sky130A
end

extract
 style ngspice variants (),(hrhc)
 variants *
 planeorder metal2 0
 planeorder metal3 1
 planeorder metal4 2
 planeorder metal5 3
 planeorder cap1 4
 variants ()
 defaultoverlap     allm4 metal4 allm3 metal3 84.03
 defaultsideoverlap allm4 metal4 allm3 metal3 70.52
 defaultsideoverlap allm3 metal3 allm4 metal4 42.64
 defaultoverlap     allm5 metal5 allm3 metal3 19.63
 defaultsideoverlap allm3 metal3 allm5 metal5 27.84
 defaultoverlap     allm4 metal4 allm2 metal2 20.33
 variants (hrhc)
 defaultsideoverlap allm4 metal4 allm3 metal3 92.12
 style other
 planeorder metal3 0
end
"""


def test_the_shielded_tech_puts_the_capacitor_between_its_plates():
    """sky130A.tech orders cap1 above every metal, so its default rules let met4 reach met3 through capm: the second
    extraction's tech orders it right above metal3, and each rule between metal3 and a plane above it gets a copy to
    capm at its value, in its variant block, from the nearest plane when two give the same pair (met3's fringe onto
    met4, not met5); a rule below the capacitor (met4 over met2) and other styles' lines stay as they are."""
    out = parasitics.shielded_tech(_TECH, [("*mimcap", "cap1", "metal3")])
    order = dict(re.findall(r"planeorder (\w+) (\d+)", out.split("style other")[0]))
    assert sorted(order, key=lambda p: int(order[p])) == ["metal2", "metal3", "cap1", "metal4", "metal5"]
    added = [ln.split() for ln in out.splitlines() if "*mimcap" in ln]
    assert added == [["defaultoverlap", "allm4", "metal4", "*mimcap", "cap1", "84.03"],
                     ["defaultsideoverlap", "allm4", "metal4", "*mimcap", "cap1", "70.52"],
                     ["defaultsideoverlap", "allm3", "metal3", "*mimcap", "cap1", "42.64"],
                     ["defaultoverlap", "allm5", "metal5", "*mimcap", "cap1", "19.63"],
                     ["defaultsideoverlap", "allm4", "metal4", "*mimcap", "cap1", "92.12"]]
    assert out.index("70.52\n defaultsideoverlap allm4 metal4 *mimcap cap1 70.52") < out.index("variants (hrhc)")
    assert "planeorder metal3 0\nend" in out                              # the other style's
    assert parasitics.shielded_tech(_TECH, [("*mimcap2", "cap2", "metal4")]) is None       # no cap2 in its planeorder
    assert parasitics.shielded_tech("tech\nend\n", [("*mimcap", "cap1", "metal3")]) is None


def _shielded_cdac(d: Path, shielded: str | None) -> dict:
    """The synthetic CDAC of 3 x 2 units (_cdac) with Magic's netlists: the first extraction couples each bottom plate
    to t (by its columns' straps) and b0 to b1, the second (shielded/, None: it failed) less between the plates, b0-b1
    less and b0-t more (b0's metal over b1's capacitors, coupled to their top plate now)."""
    _cdac(d, 3, 2)
    (d / "c.spice").write_text(".subckt cdac t b0 b1 b2\nC0 t b0 1.5f\nC1 b1 t 1.5f\nC2 b2 t 1.4f\nC3 b0 b1 0.4f\n"
                               "C4 t VSUBS 2f\nC5 b0 VSUBS 1f\n.ends\n")
    (d / "shielded").mkdir()
    (d / "shielded" / "pex.tcl").write_text("")
    if shielded is not None:
        (d / "shielded" / "c.spice").write_text(shielded)
    mims = [{"model": "sky130_fd_pr__cap_mim_m3_1", "cap": (89, 44), "bottom": (70, 20), "via": (70, 44),
             "top": (71, 20), "af": 84.03}]
    return parasitics.assemble(d, "cdac", "c", ["t", "b0", "b1", "b2"], 0.0, mims)[1]


def test_the_shielded_extraction_takes_the_place_of_magics_capacitance(tmp_path):
    """Without Magic: the second extraction's coupling between nets is the extraction's, the capacitance to the
    substrate the first's (the capacitor's fringe rules take a share of what went to it). The pairs that are a MIM's two
    plates say how they changed; removed_ff in all is every pair's change summed (b0's metal over b1's capacitors moved
    0.1 fF from b0-b1 to b0-t, so b0-t lost 0.6 fF and gained 0.1: its change is 0.5), by area as mim_overlap counts
    the straps tied inside the capacitors and the fringe; other pairs that changed are moved; mim_overlap_removed is
    not taken out a second time."""
    full = _shielded_cdac(tmp_path, ".subckt cdac t b0 b1 b2\nC0 t b0 0.9f\nC1 b1 t 1.0f\nC2 b2 t 1.4f\n"
                                    "C3 b0 b1 0.3f\nC4 t VSUBS 1.9f\nC5 b0 VSUBS 0.97f\nC6 b0 t 0.1f\nC7 b1 VSUBS 0.01f\n"
                                    ".ends\n")
    ms = full["mim_shielding"]
    pairs = {tuple(r["nets"]): r for r in ms["pairs"]}
    assert set(pairs) == {("b0", "t"), ("b1", "t")}                        # b2-t: unchanged
    over = parasitics.mim_overlap(tmp_path, "cdac", [{"model": "sky130_fd_pr__cap_mim_m3_1", "cap": (89, 44),
                                                      "bottom": (70, 20), "via": (70, 44), "top": (71, 20), "af": 84.03}])
    assert pairs[("b0", "t")] == {"nets": ["b0", "t"], "magic_ff": 1.5, "kept_ff": 1.0, "removed_ff": 0.5,
                                  "overlap_ff": pytest.approx(over[("b0", "t")][0] * 1e15, rel=1e-3), "capacitors": 2}
    assert pairs[("b1", "t")]["removed_ff"] == 0.5
    assert ms["removed_ff"] == 1.1 and ms["moved_to_plates_ff"] == 0.1
    assert ms["fringe_ff"] == pytest.approx(1.1 - ms["overlap_ff"], abs=1e-3) and ms["overlap_ff"] > 0
    assert ms["moved"] == [{"nets": ["b0", "b1"], "magic_ff": 0.4, "kept_ff": 0.3}]          # no substrate row
    assert {tuple(r["nets"]): r["c_ff"] for r in full["coupling"]} == {("b0", "t"): 1.0, ("b1", "t"): 1.0,
                                                                        ("b2", "t"): 1.4, ("b0", "b1"): 0.3}
    assert {r["net"]: r["ground_ff"] for r in full["c_per_net"]} == {"t": 2.0, "b0": 1.0, "b1": 0.0, "b2": 0.0}
    assert full["c_total_ff"]["ground"] == 3.0
    assert "mim_overlap_removed" not in full


def test_the_shielded_extraction_keeps_the_substrate_of_rc_mode(tmp_path):
    """With resistance on, Magic names the substrate after a point of it (the .ext's substrate line): its capacitance
    is the first extraction's there too."""
    _cdac(tmp_path, 1, 1)
    ext = (tmp_path / "cdac.ext").read_text()
    (tmp_path / "cdac.ext").write_text('substrate "w_n28_n28#" 0 0 -28 -28 pw 0 0\n' + ext)
    (tmp_path / "c.spice").write_text(".subckt cdac t b0\nC0 t b0 1.5f\nC1 t w_n28_n28# 2f\nC2 b0 w_n28_n28# 1f\n.ends\n")
    (tmp_path / "shielded").mkdir()
    (tmp_path / "shielded" / "pex.tcl").write_text("")
    (tmp_path / "shielded" / "cdac.ext").write_text('substrate "w_n28_n28#" 0 0 -28 -28 pw 0 0\n' + ext)
    (tmp_path / "shielded" / "c.spice").write_text(".subckt cdac t b0\nC0 t b0 1.1f\nC1 t w_n28_n28# 1.8f\n"
                                                   "C2 b0 w_n28_n28# 0.9f\n.ends\n")
    ms = parasitics.mim_shielding(tmp_path, "cdac", parasitics.read_spice(tmp_path / "c.spice"),
                                  [{"model": "sky130_fd_pr__cap_mim_m3_1", "cap": (89, 44), "bottom": (70, 20),
                                    "via": (70, 44), "top": (71, 20), "af": 84.03}])
    assert {(a, b): pytest.approx(v * 1e15) for a, b, v in ms["caps"]} == {("t", "b0"): 1.1, ("t", "w_n28_n28#"): 2.0,
                                                                            ("b0", "w_n28_n28#"): 1.0}
    assert ms["removed_ff"] == 0.4 and ms["moved"] == [] and [p["nets"] for p in ms["pairs"]] == [["b0", "t"]]


def test_a_failed_shielded_extraction_takes_out_the_overlap_as_before(tmp_path):
    full = _shielded_cdac(tmp_path, None)
    assert "failed" in full["mim_shielding"]["not_applied"]
    assert full["mim_overlap_removed"]["total_ff"] > 0
    assert {tuple(r["nets"]) for r in full["mim_overlap_removed"]["pairs"]} == {("b0", "t"), ("b1", "t"), ("b2", "t")}
    (tmp_path / "x").mkdir()
    other = _shielded_cdac(tmp_path / "x", ".subckt cdac t b0 b1 b2\nC0 t b9 1f\n.ends\n")     # a net it lacks
    assert "does not have the first's" in other["mim_shielding"]["not_applied"] and "mim_overlap_removed" in other


def test_one_nets_rows_and_no_such_net():
    full = {"mode": "c", "ports": {"match": True, "order": ["out", "VDD"]}, "runtime_s": 1.5,
            "c_per_net": [{"net": "out", "ground_ff": 1.0}, {"net": "mid#", "ground_ff": 2.0}, {"net": "VDD", "ground_ff": 3.0}],
            "coupling": [{"nets": ["mid#", "out"], "c_ff": 0.5}, {"nets": ["VDD", "mid#"], "c_ff": 0.2}],
            "floating_nets": ["mid#"], "mim_shielding": {"removed_ff": 1.0, "pairs": [{"nets": ["VDD", "out"], "kept_ff": 1}],
                                                        "moved": []}}
    one = parasitics.compact(full, "OUT")                     # any case, when only one net has the name
    assert one == {"net": "out", "mode": "c", "runtime_s": 1.5, "port": True,
                   "c_per_net": [{"net": "out", "ground_ff": 1.0}], "coupling": [{"nets": ["mid#", "out"], "c_ff": 0.5}],
                   "floating": False, "mim_shielding": {"pairs": [{"nets": ["VDD", "out"], "kept_ff": 1}]}}
    assert parasitics.compact(full, "mid#")["floating"] is True
    with pytest.raises(KeyError, match=r"no net mid in this extraction \(near: mid#\)"):
        parasitics.compact(full, "mid")
    with pytest.raises(KeyError, match=r"no net zzz in this extraction \(3 nets, e.g. out, VDD, mid#\)"):
        parasitics.compact(full, "zzz")
    assert parasitics.compact(full)["coupling"] == full["coupling"]


def test_an_extraction_of_a_layout_whose_lvs_does_not_match_says_so(tmp_path):
    """Without Magic: names from an LVS run that does not match give a warning, and devices_kept says why (not that the
    cell has no schematic); a run that kept no circuit of the layout says that; no LVS run at all, a warning too."""
    (tmp_path / "c.spice").write_text(".subckt cell a b\nX0 a b a a sky130_fd_pr__nfet_01v8 w=1 l=0.15\n.ends\n")
    def run(named, ports=("a", "b")):
        return parasitics.assemble(tmp_path, "cell", "c", list(ports) if ports else None, 0.0, None, named)[1]
    full = run({"lvs": "j1", "match": False, "labels": [], "unnamed": [], "empty": True})
    assert "LVS of this layout does not match its schematic (job j1" in full["warning"]
    assert full["names"]["devices_kept"].startswith("all: the LVS run j1 kept no circuit of the layout")
    full = run({"lvs": "j1", "match": False, "labels": [], "unnamed": [], "devices": []})
    assert "does not match: get_lvs_report" in full["names"]["devices_kept"] and "warning" in full
    full = run({"lvs": "j1", "match": True, "labels": [], "unnamed": [], "devices": None})
    assert "could not be written" in full["names"]["devices_kept"] and "warning" not in full
    full = run({"lvs": None, "why": "no LVS run of this layout: run_checks (LVS), then extract again"})
    assert full["warning"].startswith("no LVS run of this layout")
    full = run({"lvs": "j1", "match": False, "labels": [], "unnamed": [], "empty": True}, None)
    assert full["names"]["devices_kept"] == "all: the cell has no schematic to name its devices after"
    assert "warning" not in full


def test_a_netgen_lvs_of_the_layout_is_not_said_to_be_missing(tmp_path):
    """lvs_names takes names from a KLayout LVS run only; a layout whose LVS run is Netgen's is not warned about as
    not verified when it matches (the summary says the names could not be taken from it), and is when it does not."""
    pdk = type("P", (), {"name": "sky130A", "profile": type("Pr", (), {"net_labels": [("via3", "met4.label")]})()})()
    inputs = {"design": [{"view": "layout", "sha": "s1"}]}

    def netgen(name: str, match: bool, sha: str = "s1") -> None:
        d = tmp_path / "runs" / name
        d.mkdir(parents=True)
        (d / "lvs.out").write_text("")
        sim.Job(d).set(id=name, kind="lvs", tool="netgen", lib="l", cell="cell", state="done", created=0,
                       inputs={"design": [{"view": "layout", "sha": sha}]}, summary={"match": match})
    assert parasitics.lvs_names(tmp_path / "runs", pdk, "l", "cell", inputs, ["a", "b"])["why"].startswith("no LVS run")
    netgen("20261105-100000-0001", True)
    netgen("20261105-110000-0002", False, "s0")                    # another layout's
    named = parasitics.lvs_names(tmp_path / "runs", pdk, "l", "cell", inputs, ["a", "b"])
    assert named == {"lvs": None, "other_lvs": "20261105-100000-0001", "match": True,
                     "why": "the LVS run of this layout (job 20261105-100000-0001) is Netgen's: the schematic's names "
                            "come from a KLayout LVS run only (run_checks with KLayout's LVS, then extract again)"}
    (tmp_path / "c.spice").write_text(".subckt cell a b\nX0 a b a a sky130_fd_pr__nfet_01v8 w=1 l=0.15\n.ends\n")
    full = parasitics.assemble(tmp_path, "cell", "c", ["a", "b"], 0.0, None, named)[1]
    assert "warning" not in full and full["names"] == {"from_lvs": None, "why": named["why"]}
    netgen("20261105-120000-0003", False)
    named = parasitics.lvs_names(tmp_path / "runs", pdk, "l", "cell", inputs, ["a", "b"])
    full = parasitics.assemble(tmp_path, "cell", "c", ["a", "b"], 0.0, None, named)[1]
    assert "does not match its schematic (job 20261105-120000-0003" in full["warning"]


def test_extracted_subcircuit_takes_the_symbols_pin_order():
    ext = "* from the layout\n.subckt inv in out VDD VSS\nX0 out in VSS VSS nfet\n.ends\n"
    lines = schematic._extracted_subckt(ext, "inv", ["vdd", "IN", "OUT", "vss"])
    assert lines[1] == ".subckt inv VDD in out VSS" and lines[-1] == ".ends" and "X0 out in VSS VSS nfet" in lines
    with pytest.raises(ValueError, match="not the symbol's pins"):
        schematic._extracted_subckt(ext, "inv", ["in", "out", "VDD"])


def test_a_net_without_a_label_keeps_its_capacitance_in_rc_mode():
    """extresist renames a net without a label (killnode a_0_0#; nodes a_0_0.t0, a_0_0.t1): its capacitance goes to
    those nodes, which the drains use, and it is neither floating nor without its resistance."""
    d = Path(tempfile.mkdtemp())
    (d / "c.spice").write_text(".subckt top in VSS\nX0 a_0_0# in VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15\n"
                               "X1 a_0_0# in x x sky130_fd_pr__pfet_01v8 w=1 l=0.15\nC0 a_0_0# VSS 90f\nC1 a_0_0# in 1f\n.ends\n")
    (d / "rc.spice").write_text(".subckt top in VSS\nX0 a_0_0.t0 in VSS VSS sky130_fd_pr__nfet_01v8 w=1 l=0.15\n"
                                "X1 a_0_0.t1 in x x sky130_fd_pr__pfet_01v8 w=1 l=0.15\nR0 a_0_0.t0 a_0_0.t1 500\n"
                                "C0 a_0_0.t0 VSS 65f\n.ends\n")
    (d / "top.ext").write_text('port "in" 1 -180 272 -180 272 m1\nport "VSS" 2 -400 -210 -400 -210 m1\n'
                               'node "a_0_0#" 0 90000 0 0 ndif\nnode "in" 0 600 -180 272 m1\n'
                               'substrate "VSS" 0 0 -400 -210 m1\n')
    (d / "top.res.ext").write_text('killnode "a_0_0#"\nrnode "a_0_0.t1" 0 75 60 900 0\nrnode "a_0_0.t0" 0 225 60 100 0\n'
                                   'resist "a_0_0.t0" "a_0_0.t1" 500\n')
    text, full = parasitics.assemble(d, "top", "rc", ["in", "VSS"], 0.01)
    caps = {tuple(l.split()[1:3]): float(l.split()[3][:-1]) for l in text.splitlines() if l.startswith("C")}
    assert caps == {("VSS", "a_0_0.t0"): 67.5, ("VSS", "a_0_0.t1"): 22.5, ("a_0_0.t0", "in"): 0.75,
                    ("a_0_0.t1", "in"): 0.25}
    assert "a_0_0#" not in text and full["floating_nets"] == []
    assert full["r_per_net"] == [{"net": "a_0_0#", "r_total_ohm": 500.0, "r_max_path_ohm": 500.0, "resistors": 1,
                                  "nodes": 2}]
    shutil.rmtree(d)


def test_two_labels_on_one_net():
    """Magic keeps one name for a net with labels a and a2 (the other an equiv): a schematic port a takes the net when a2
    is not a port of the schematic; when both are, they are shorted."""
    d = Path(tempfile.mkdtemp())
    (d / "c.spice").write_text(".subckt top a2 b\nX0 a2 b VSUBS VSUBS sky130_fd_pr__nfet_01v8 w=1 l=0.15\nC0 a2 b 5f\n"
                               "C1 a2 VSUBS 2f\n.ends\n")
    (d / "top.ext").write_text('port "b" 2 0 0 0 0 m1\nport "a2" 1 0 0 0 0 m1\nport "a" 3 0 0 0 0 m1\nnode "b" 0 0 0 0 m1\n'
                               'node "a2" 0 0 0 0 m1\nsubstrate "VSUBS" 0 0 0 0 space\nequiv "a2" "a"\n')
    text, full = parasitics.assemble(d, "top", "c", ["b", "a"], 0.01)
    assert full["ports"] == {"match": True, "order": ["b", "a"], "missing": [], "extra_labels": ["a2"], "from": "schematic"}
    assert ".subckt top b a\n" in text and "X0 a b 0 0 sky130_fd_pr__nfet_01v8" in text and "C2 a b 5f" in text
    _, full = parasitics.assemble(d, "top", "c", ["a", "a2", "b"], 0.01)
    assert full["ports"]["shorted"] == [{"port": "a", "to": "a2"}] and full["ports"]["missing"] == []
    assert not full["ports"]["match"]
    shutil.rmtree(d)


def test_a_coupling_capacitor_spreads_along_two_parallel_networks():
    """Two 1 mm networks side by side, 40 nodes each at the same distance from the other: the coupling covers their
    whole length, between nodes facing each other, in at most SPREAD pieces per half."""
    n, xs = 40, np.linspace(0, 1000, 40)
    a = ([f"a.n{i}" for i in range(n)], [1 / n] * n, np.array([[x, 0.0] for x in xs]))
    b = ([f"b.n{i}" for i in range(n)], [1 / n] * n, np.array([[x, 0.28] for x in xs]))
    pieces = parasitics._pieces(a, b, 10e-15, 0.01e-15)
    assert sum(f for *_, f in pieces) == pytest.approx(1.0) and len(pieces) <= 2 * parasitics.SPREAD
    assert all(x[3:] == y[3:] for x, y, _ in pieces)
    assert sum(f for x, _, f in pieces if int(x[3:]) >= 16) == pytest.approx(0.6, abs=0.05)    # the last 60 %
    small = parasitics._pieces(a, b, 0.1e-15, 0.01e-15)              # pieces of the threshold or more, still spread
    assert len(small) == 10 and max(int(x[3:]) for x, _, _ in small) - min(int(x[3:]) for x, _, _ in small) > 30


def test_a_run_on_an_extracted_view_saves_the_testbench_nets():
    tb = ("* tb\n.subckt inv in out\nR1 in m 1k\nR2 m out 1k\n.ends\nVin in 0 pulse(0 1 1n)\nx1 in out inv\n"
          "C1 out 0 1p W=2\n+ m=1\nB1 sense 0 V=v(out)*2\n.meas tran d TRIG v(in) VAL=0.5 RISE=1 TARG v(x1.m, out) VAL=0.5\n.end\n")
    assert sim.save_cards(tb, ["x1.q", "i(vin)", "OUT"]).split() == [
        ".save", "in", "i(Vin)", "out", "sense", "v(in)", "v(x1.m)", "v(out)", "x1.q"]
    assert sim.save_cards(tb.replace(".end\n", ".save all\n.end\n")) == ""


# ------------------------------------------------------------------ with Magic and the sky130A PDK


def _need(sim: bool = False):
    pdk = next((p for p in PDKS.values() if p.name == "sky130A" and pcell.available(p)), None)
    if pdk is None or not verify.decks(pdk)["magic"]:
        pytest.skip("sky130A with its PCells, or Magic, is not installed")
    if sim and not tools.find("ngspice"):
        pytest.skip("ngspice not installed")
    return pdk


def _fet(pdk, ly: kdb.Layout, kind: str) -> kdb.Cell:
    path, name, _ = pcell.generate(pdk, kind, {"type": f"sky130_fd_pr__{kind}_01v8", "w": 1.0, "l": 0.15, "nf": 1,
                                               "bulk": "bulk tie"})
    src = kdb.Layout()
    src.read(str(path))
    c = ly.create_cell(name)
    c.copy_tree(src.cell(name))
    return c


def _project(pdk, ly: kdb.Layout, files: dict[str, XFile] | None = None):
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", PDKS, [root])
    lib = srv.store.create("p", pdk.id).create_library("l")
    for name, f in (files or {}).items():
        (lib.path / name).write_text(xschem.dump(f))
    ly.write(str(root / "in.gds"))
    srv.db("p", "l").import_oas(root / "in.gds")
    return root, srv, T.ToolCtx(srv, "p", actor="agent:t")


def _call(ctx, tool: str, args: dict) -> dict:
    res, err = T.call(ctx, tool, args)
    assert not err, res
    return json.loads(res)


def _job(srv, jid: str):
    return srv.jobs.get(srv.project("p").path, jid)


def test_coupling_capacitor_survives_both_modes():
    """Two met1 wires 200 um side by side, 0.14 um apart, the drain and source of an nfet PCell: a resistor network
    each in rc mode, and the a-b coupling capacitor in both modes, with the same total; Magic's own rc netlist counts it
    once more to ground."""
    pdk = _need()
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("cpl")
    top.insert(kdb.DCellInstArray(_fet(pdk, ly, "nfet").cell_index(), kdb.DTrans(kdb.DVector(0, 0))))
    m1, txt = ly.layer(68, 20), ly.layer(68, 5)
    for box in ((2, 0.3, 202, 0.44), (2, 0.58, 202, 0.72),                                   # a, b
                (0.035, -0.5, 0.265, 0.825), (0.035, -0.5, 2.2, -0.3), (2.0, -0.5, 2.2, 0.44),  # drain to a
                (0.485, 0.58, 2.1, 0.72)):                                                  # source to b
        top.shapes(m1).insert(kdb.DBox(*box))
    for t, x, y in (("a", 201, 0.37), ("b", 201, 0.65), ("g", 0.375, 1.36)):
        top.shapes(txt).insert(kdb.DText(t, x, y))
    root, srv, ctx = _project(pdk, ly)
    c = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "cpl", "mode": "c"})
    rc = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "cpl", "mode": "rc", "reason": "with wire resistance"})
    assert c["devices"]["by_model"] == {"sky130_fd_pr__nfet_01v8": 1} == rc["devices"]["by_model"]
    ab = c["coupling"][0]
    assert set(ab["nets"]) == {"a", "b"} and 15 < ab["c_ff"] < 30, c                  # about 0.11 fF / um
    assert rc["coupling"][0] == ab
    # the rc job's own capacitance run (Magic 8.3.678 gives a net's ground a little differently with resistance on)
    assert [r["net"] for r in rc["c_per_net"]] == [r["net"] for r in c["c_per_net"]]
    assert all(x["total_ff"] == pytest.approx(y["total_ff"], rel=0.02) for x, y in zip(rc["c_per_net"], c["c_per_net"]))
    assert {r["net"] for r in rc["r_per_net"]} >= {"a", "b"} and c["ports"]["from"] == "layout labels (no schematic)"
    ground = {r["net"]: r["ground_ff"] for r in rc["c_per_net"]}
    netlist = (_job(srv, rc["job"]).path / "extracted.spice").read_text()
    caps = _caps(netlist)
    assert caps[frozenset("ab")] == pytest.approx(ab["c_ff"], rel=1e-3)               # still a coupling capacitor
    assert caps[frozenset(("a", "0"))] == pytest.approx(ground["a"], rel=1e-3)          # not grown by it
    assert len(re.findall(r"^C\d+ a\S* b\S* ", netlist, re.M)) >= 2                     # spread over a's network
    # what Magic's rc flow writes by itself (8.3.465): the coupling, and the coupling again on b's capacitance to ground
    if "revision 465" in (tools.version("magic") or ""):
        d = _job(srv, rc["job"]).path
        sub = parasitics.read_ext(d / "cpl.ext")[0]
        raw = _caps((d / "rc.spice").read_text())
        assert raw[frozenset("ab")] == pytest.approx(ab["c_ff"], rel=1e-3)
        assert raw[frozenset(("b", sub))] == pytest.approx(ground["b"] + ab["c_ff"], rel=1e-2)
    shutil.rmtree(root)


def test_a_short_thin_signal_wire_gets_its_resistance_in_rc_mode():
    """An nfet's drain on a 40 um met1 wire 0.14 um wide (about 36 ohm and a few fF: a delay well under 1 ps, which
    Magic's own mindelay and threshold would leave one node) gets a resistor network by default; with
    res_threshold_ohm above its resistance, or res_nets naming only the source's net, it stays one node, and
    single_node says why. Mode c takes no resistance options."""
    pdk = _need()
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("thin")
    top.insert(kdb.DCellInstArray(_fet(pdk, ly, "nfet").cell_index(), kdb.DTrans(kdb.DVector(0, 0))))
    m1, txt = ly.layer(68, 20), ly.layer(68, 5)
    for box in ((0.035, -0.5, 0.265, 0.825), (0.035, -0.5, 40.2, -0.36),       # drain, then 40 um to a
                (0.485, 0.58, 2.1, 0.72)):                                       # source to b
        top.shapes(m1).insert(kdb.DBox(*box))
    for t, x, y in (("a", 40.1, -0.43), ("b", 2.0, 0.65), ("g", 0.375, 1.36)):
        top.shapes(txt).insert(kdb.DText(t, x, y))
    root, srv, ctx = _project(pdk, ly)
    rc = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "thin", "mode": "rc"})
    a = next(r for r in rc["r_per_net"] if r["net"] == "a")
    assert 30 < a["r_max_path_ohm"] < 300, a           # 40 / 0.14 squares of 0.125 ohm, and the contacts down to the drain
    assert rc["res_threshold_ohm"] == parasitics.RES_THRESHOLD_OHM and "a" not in [r["net"] for r in rc["single_node"]]
    assert all(r["why"] for r in rc["single_node"])
    sub = next((r for r in rc["single_node"] if "substrate" in r["why"]), None)
    assert sub is None or "ground 0" in sub["why"]
    hi = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "thin", "mode": "rc", "res_threshold_ohm": 500})
    why = {r["net"]: r["why"] for r in hi["single_node"]}
    assert "a" not in [r["net"] for r in hi["r_per_net"]] and "below res_threshold_ohm (500)" in why["a"], why
    only = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "thin", "mode": "rc", "res_nets": ["b", "nope"]})
    why = {r["net"]: r["why"] for r in only["single_node"]}
    assert why["a"] == "not in res_nets" and "no net of the layout has this name" in why["nope"] and only["res_nets"] == ["b", "nope"]
    up = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "thin", "mode": "rc", "res_nets": ["A"]})
    assert "a" in [r["net"] for r in up["r_per_net"]] and "a" not in [r["net"] for r in up["single_node"]], up
    assert "res_options_not_applied" not in up
    res, err = T.call(ctx, "extract_parasitics", {"lib": "l", "cell": "thin", "mode": "c", "res_threshold_ohm": 1})
    assert err and "for mode rc" in res
    res, err = T.call(ctx, "extract_parasitics", {"lib": "l", "cell": "thin", "mode": "rc", "res_threshold_ohm": -1})
    assert err and "0 or more" in res
    shutil.rmtree(root)


def test_single_node_says_when_this_magic_has_no_minres_or_mindelay(tmp_path):
    """A Magic before extresist minres and mindelay (8.3.653; 8.3.465 takes threshold, with an older meaning, and says
    "Unknown option: minres") lumps by its own rules: the reasons do not claim res_threshold_ohm was applied."""
    (tmp_path / "log.txt").write_text("Unknown option: minres\nUnknown option: mindelay\nNot adding b; (maxres = 3.5 ohm)\n")
    assert parasitics.unsupported_options((tmp_path / "log.txt").read_text()) == ["mindelay", "minres"]
    why = {r["net"]: r["why"] for r in parasitics.single_nodes(tmp_path, ["a", "b"], set(), "VSUBS", False, {},
                                                                {"res_threshold_ohm": 0.1})}
    assert "res_threshold_ohm (0.1) was not applied" in why["a"] and "tolerance (10)" in why["a"], why
    assert "3.5 ohm, is below this Magic's own threshold" in why["b"] and "was not applied" in why["b"], why
    (tmp_path / "log.txt").write_text("Not adding b; (maxres = 0.05 ohm)\n")
    why = {r["net"]: r["why"] for r in parasitics.single_nodes(tmp_path, ["a", "b"], set(), "VSUBS", False, {},
                                                                {"res_threshold_ohm": 0.1})}
    assert "below res_threshold_ohm (0.1)" in why["b"] and "every resistor below res_threshold_ohm" in why["a"], why


def C(sym, x, y, **p):
    return XComp(sym, x, y, 0, 0, p)


def _inverter_files() -> dict[str, XFile]:
    """An inverter (W 1 / L 0.15, ports in out VDD VSS), its symbol, and a testbench with a 5 fF load."""
    inv = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    fet = dict(L="0.15", W="1", nf="1", mult="1", spiceprefix="X")
    inv.comps = [C("sky130_fd_pr/pfet_01v8.sym", 0, -60, name="M1", model="pfet_01v8", **fet),
                 C("sky130_fd_pr/nfet_01v8.sym", 0, 60, name="M2", model="nfet_01v8", **fet),
                 C("devices/ipin.sym", -120, 0, name="p1", lab="in"), C("devices/opin.sym", 120, 0, name="p2", lab="out"),
                 C("devices/iopin.sym", -120, -120, name="p3", lab="VDD"),
                 C("devices/iopin.sym", -120, 120, name="p4", lab="VSS")]
    inv.wires = [(-120, 0, -20, 0, {}), (-20, -60, -20, 60, {}), (20, -30, 20, 30, {}), (20, 0, 120, 0, {}),
                 (20, -90, 40, -90, {}), (40, -90, 40, -60, {}), (40, -60, 20, -60, {}), (20, -90, -120, -90, {}),
                 (-120, -90, -120, -120, {}), (20, 90, 40, 90, {}), (40, 90, 40, 60, {}), (40, 60, 20, 60, {}),
                 (20, 90, -120, 90, {}), (-120, 90, -120, 120, {})]
    sym = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    sym.global_props = {"type": "subcircuit", "format": "@name @pinlist @symname", "template": "name=x1"}
    sym.k_raw = 'type=subcircuit\nformat="@name @pinlist @symname"\ntemplate="name=x1"\n'
    sym.boxes = [(4, -40, -40, 40, 40, {}), (5, -2.5, -42.5, 2.5, -37.5, {"name": "VDD", "dir": "inout"}),    # VDD first:
                 (5, -42.5, -2.5, -37.5, 2.5, {"name": "in", "dir": "in"}),                     # another order than
                 (5, 37.5, -2.5, 42.5, 2.5, {"name": "out", "dir": "out"}),                     # the schematic's
                 (5, -2.5, 37.5, 2.5, 42.5, {"name": "VSS", "dir": "inout"})]
    tb = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    tb.comps = [C("invw.sym", 0, 0, name="x1"), C("devices/vsource.sym", -200, -100, name="Vdd", value="1.8"),
                C("devices/vsource.sym", -200, 60, name="Vin", value="pulse(0 1.8 1n 50p 50p 5n 10n)"),
                C("devices/capa.sym", 160, 40, name="CL", value="5f"),
                C("devices/lab_pin.sym", -40, 0, lab="in", name="l1"), C("devices/lab_pin.sym", -200, 30, lab="in", name="l2"),
                C("devices/lab_pin.sym", 40, 0, lab="out", name="l3"), C("devices/lab_pin.sym", 160, 10, lab="out", name="l4"),
                C("devices/lab_pin.sym", -200, -130, lab="vdd", name="l5"), C("devices/lab_pin.sym", 0, -40, lab="vdd", name="l6"),
                C("devices/gnd.sym", -200, 90, name="l7", lab="GND"), C("devices/gnd.sym", -200, -70, name="l8", lab="GND"),
                C("devices/gnd.sym", 160, 70, name="l9", lab="GND"), C("devices/gnd.sym", 0, 40, name="l10", lab="GND")]
    return {"invw.sch": inv, "invw.sym": sym, "tb.sch": tb}


def _inverter(pdk, wire_um: float = 1000.0, drop: tuple[str, ...] = ()):
    """The inverter from nfet / pfet PCells (bulk ties), its output driving a met1 wire of `wire_um` with a victim wire b
    beside it (a label the schematic does not have); without the labels in `drop`."""
    if tools.xschem_library() is None:
        pytest.skip("Xschem's devices library is not installed")
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("invw")
    top.insert(kdb.DCellInstArray(_fet(pdk, ly, "nfet").cell_index(), kdb.DTrans(kdb.DVector(0, 0))))
    top.insert(kdb.DCellInstArray(_fet(pdk, ly, "pfet").cell_index(), kdb.DTrans(kdb.DVector(0, 4))))
    poly, li, m1, txt = ly.layer(66, 20), ly.layer(67, 20), ly.layer(68, 20), ly.layer(68, 5)
    top.shapes(poly).insert(kdb.DBox(0.24, 1.53, 0.51, 3.8))                       # the gates joined
    top.shapes(li).insert(kdb.DBox(0.6, 0.155, 0.95, 0.845))                      # sources to their bulk ties
    top.shapes(li).insert(kdb.DBox(0.6, 4.155, 0.95, 4.845))
    for box in ((-1.0, 1.25, 0.56, 1.48),                                         # in
                (-1.6, 0.175, 0.265, 0.40), (-1.6, 0.175, -1.3, 4.825), (-1.6, 4.6, 0.265, 4.825),   # out: the drains
                (-2.5, -1.2, 2.0, -0.9), (0.485, -1.2, 0.715, 0.825),             # VSS
                (-2.5, 6.3, 2.0, 6.6), (0.485, 4.6, 1.7, 4.825), (1.4, 4.6, 1.7, 6.6),   # VDD
                (-1.6 - wire_um, 2.0, -1.3, 2.3), (-1.6 - wire_um, 2.44, -3.0, 2.58)):   # the long wire, the victim
        top.shapes(m1).insert(kdb.DBox(*box))
    for t, x, y in (("in", -0.9, 1.36), ("VSS", -2.0, -1.05), ("VDD", -2.0, 6.45), ("out", -1.5 - wire_um, 2.15),
                    ("b", -1.5 - wire_um, 2.51)):
        if t not in drop:
            top.shapes(txt).insert(kdb.DText(t, x, y))
    return _project(pdk, ly, _inverter_files())


def test_an_inverter_driving_a_long_wire_is_slower_extracted_than_drawn():
    pdk = _need(sim=True)
    root, srv, ctx = _inverter(pdk)
    run = {"lib": "l", "cell": "tb", "analysis": TRAN, "measure": MEAS, "signals": ["out"]}
    drawn = _call(ctx, "run_simulation", run)
    assert "extracted" not in drawn
    c = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c"})
    assert c["ports"] == {"match": True, "order": ["in", "out", "VDD", "VSS"], "missing": [], "extra_labels": ["b"],
                          "from": "schematic"}
    assert c["devices"]["total"] == 2 and c["c_per_net"][0]["net"] == "out" and c["c_per_net"][0]["total_ff"] > 100
    rc = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "rc"})
    assert next(r for r in rc["r_per_net"] if r["net"] == "out")["r_max_path_ohm"] > 400          # 1 mm of met1
    text = (_job(srv, c["job"]).path / "extracted.spice").read_text()
    assert ".subckt invw in out VDD VSS\n" in text                     # the schematic's order (Magic's: in VSS VDD out b)
    tphl = {"drawn": drawn["measurements"]["tphl"]}
    for mode, ex in (("c", {"l/invw": "c"}), ("rc", ["l/invw"])):              # a list: the newest extraction
        r = _call(ctx, "run_simulation", {**run, "extracted": ex})
        job = _job(srv, r["job"])
        assert r["extracted"] == [{"cell": "l/invw", "mode": mode, "job": (c if mode == "c" else rc)["job"]}]
        assert ".subckt invw VDD in out VSS\n" in (job.path / "netlist.cir").read_text()       # the symbol's pin order
        assert all("reduced" in m for m in job.status()["models"])                          # the PDK's models, reduced
        assert {"view": "extraction", "lib": "l", "cell": "invw", "mode": mode} == \
            {k: v for k, v in job.status()["inputs"]["design"][1].items() if k != "sha"}
        # stored: the testbench's nets and source currents, not the extracted cell's nodes (x1.out.n0 ...)
        vectors = [n for n, _ in sim.read_raw(job.path / "out.raw")["vars"]]
        assert {"v(in)", "v(out)", "v(vdd)"} <= set(vectors) and not [n for n in vectors if "x1." in n], vectors
        tphl[mode] = r["measurements"]["tphl"]
    assert tphl["c"] > 3 * tphl["drawn"] and tphl["rc"] > tphl["c"], tphl                  # the wire's C, then its R
    shutil.rmtree(root)


def test_an_outdated_missing_or_unlabeled_extraction_is_refused():
    pdk = _need()
    root, srv, ctx = _inverter(pdk, wire_um=50.0)
    run = {"lib": "l", "cell": "tb", "analysis": TRAN, "extracted": ["l/invw"]}
    res, err = T.call(ctx, "run_simulation", run)
    assert err and "no finished parasitic extraction" in res and "extract_parasitics(lib='l', cell='invw'" in res
    c = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c"})
    netlist, _ = srv.reference_netlist(srv.project("p"), "l", "tb", "tb", "sim", extracted={"l/invw": (
        srv.project("p").library("l").path / "invw.sch", (_job(srv, c["job"]).path / "extracted.spice").read_text())})
    assert "sky130_fd_pr__pfet_01v8 ad=" in netlist and "M1" not in netlist.split(".subckt invw")[1]   # the layout's devices
    with pytest.raises(ValueError, match="simulated as extracted"):                  # a sweep cannot vary what is not drawn
        srv.start_sim("p", "l", "tb", TRAN, overrides={"invw": {"M1": {"W": "2"}}}, extracted=["l/invw"])
    res, err = T.call(ctx, "run_simulation", {**run, "extracted": ["l/tb"]})
    assert err and "no finished parasitic extraction" in res
    # the layout changes: the extraction (and a simulation on it) is outdated
    db = srv.db("p", "l")
    with db.recording("create", {"cell": "invw"}, "user", "a dummy shape"):
        db.create("invw", "box", db.layer_id("met1", "invw"), [0, 20000, 1000, 21000])
    res, err = T.call(ctx, "run_simulation", run)
    assert err and f"(job {c['job']}, mode c) is outdated: layout of l/invw changed" in res and "again" in res
    got = _call(ctx, "get_parasitics", {"lib": "l", "cell": "invw"})
    assert got["outdated"] is True and got["changed"] == ["layout of l/invw changed"]
    assert got["made_from"]["layout_sha"] == next(e["sha"] for e in _job(srv, c["job"]).status()["inputs"]["design"]
                                                  if e["view"] == "layout")[:16]
    assert srv.extraction_sha("p", "l", "invw") is None
    # extracted again without the VDD label: its ports are not the schematic's
    hits = [h for h in db.select("invw", [-2500, 6000, -1500, 7000], False, None, 0) if h.get("type") == "text"]
    with db.recording("delete", {"cell": "invw"}, "user", "drop the VDD label"):
        db.delete("invw", [h["ref"] for h in hits])
    c2 = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c"})
    assert c2["ports"]["missing"] == ["VDD"] and not c2["ports"]["match"] and "label the missing pins" in c2["use"]
    res, err = T.call(ctx, "run_simulation", run)
    assert err and "does not have the schematic's ports: no layout label for VDD" in res
    # a VDD label on the VSS rail: two ports of the schematic on one net are shorted, not unlabeled
    with db.recording("create", {"cell": "invw"}, "user", "VDD on the VSS rail"):
        db.create("invw", "text", db.layer_id("68/5", "invw"), [1500, -1050], 0, "VDD")
    c3 = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c"})
    assert c3["ports"]["missing"] == [] and len(c3["ports"]["shorted"]) == 1 and not c3["ports"]["match"]
    res, err = T.call(ctx, "run_simulation", run)
    assert err and "is shorted to" in res and "no layout label" not in res
    shutil.rmtree(root)


def test_a_simulation_on_an_extraction_is_outdated_when_the_layout_changes():
    pdk = _need(sim=True)
    root, srv, ctx = _inverter(pdk, wire_um=50.0)
    first = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c"})
    job = srv.start_sim("p", "l", "tb", {"type": "op"}, extracted=["l/invw"])
    assert srv.start_sim("p", "l", "tb", {"type": "op"}, extracted=["l/invw"], reuse=True).id == job.id
    assert provenance.freshness(srv, "p", job.status())["fresh"]
    # a newer extraction of the mode (another threshold): a run on the newest is outdated, one on the job named is not
    _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c", "cthresh_ff": 0})
    pinned = srv.start_sim("p", "l", "tb", {"type": "op"}, extracted={"l/invw": first["job"]})
    assert pinned.status()["extracted"] == [{"cell": "l/invw", "mode": "c", "job": first["job"]}]
    assert provenance.freshness(srv, "p", pinned.status())["fresh"]
    assert provenance.freshness(srv, "p", job.status())["changed"] == ["extracted view of l/invw changed"]
    db = srv.db("p", "l")
    with db.recording("create", {"cell": "invw"}, "user", "a dummy shape"):
        db.create("invw", "box", db.layer_id("met1", "invw"), [0, 20000, 1000, 21000])
    for j in (job, pinned):
        assert provenance.freshness(srv, "p", j.status())["changed"] == ["extracted view of l/invw is gone"]
        while j.status()["state"] in ("queued", "running"):
            time.sleep(0.2)
    shutil.rmtree(root)


def test_nets_without_a_label_in_rc_mode():
    """Without the out and VSS labels, the output is a net a_N_M# and the ground the substrate's own net, which is not a
    port. extresist renames the output's nodes (killnode); its capacitance still goes to the drains' nodes, and the
    nfet's source and bulk are ground 0."""
    pdk = _need()
    root, srv, ctx = _inverter(pdk, wire_um=200.0, drop=("out", "VSS"))
    c = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c"})
    rc = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "rc"})
    out = c["c_per_net"][0]
    assert out["net"].endswith("#") and out["ground_ff"] > 5 and "not a port" in rc["substrate"]
    # Magic comments those capacitors (** FLOATING, after $ or ;): still capacitors, no device more in either mode
    assert re.search(r"[;$] \*\*FLOATING", (_job(srv, rc["job"]).path / "rc.spice").read_text())
    assert c["devices"]["by_model"] == rc["devices"]["by_model"] == {"sky130_fd_pr__nfet_01v8": 1, "sky130_fd_pr__pfet_01v8": 1}
    assert rc["floating_nets"] == c["floating_nets"] == ["b"]                    # the victim wire only
    assert next(r for r in rc["r_per_net"] if r["net"] == out["net"])["r_max_path_ohm"] > 50
    net = parasitics.read_spice_text((_job(srv, rc["job"]).path / "extracted.spice").read_text())
    nodes = {t: x for x in net["devices"] for t in parasitics._terminals(x)}
    drains = {t for t in nodes if t.startswith(out["net"][:-1] + ".")}
    assert drains and out["net"] not in {n for a, b, _ in net["caps"] for n in (a, b)}
    assert sum(v for a, b, v in net["caps"] if {a, b} - {"0"} <= drains and "0" in (a, b)) * 1e15 == \
        pytest.approx(out["ground_ff"], rel=1e-3)
    nfet = next(x for x in net["devices"] if "nfet" in x)
    assert parasitics._terminals(nfet).count("0") == 2                          # source and bulk
    shutil.rmtree(root)


def test_a_mim_strapped_on_met4_is_not_coupled_twice():
    """A 10 x 10 um MIM PCell with its top plate strapped all over in met4. Magic's planes do not let capm shield the
    bottom plate from met4, so it counts the met4 over met3 inside the capacitor (100 um2, 84.03 aF/um2 in
    sky130A.tech) as coupling between the plates' nets, which the device's own capacitance has: the second extraction
    (capm between its plates) takes it out, in both modes, and says how much; the fringe of the strap's edges, all on
    the capacitor's edge, lands outside it and stays."""
    pdk = _need()
    ly = kdb.Layout()
    ly.dbu = 0.001
    path, name, _ = pcell.generate(pdk, "mim_cap", {"type": "sky130_fd_pr__model__cap_mim", "w": 10.0, "l": 10.0})
    src = kdb.Layout()
    src.read(str(path))
    unit = ly.create_cell(name)
    unit.copy_tree(src.cell(name))
    top = ly.create_cell("mim")
    top.insert(kdb.DCellInstArray(unit.cell_index(), kdb.DTrans()))
    top.shapes(ly.layer(71, 20)).insert(kdb.DBox(0, 0, 10, 10))                    # the strap over the whole top plate
    top.shapes(ly.layer(71, 5)).insert(kdb.DText("c0", 1, 1))
    top.shapes(ly.layer(71, 5)).insert(kdb.DText("c1", 11.4, 5))                    # the PCell's bottom-plate contact
    root, srv, ctx = _project(pdk, ly)
    want = 100 * parasitics.tech_overlap(pdk.root / "libs.tech/magic/sky130A.tech", ("metal4", "metal3")) * 1e-3
    assert want == pytest.approx(8.403)
    kept = {}
    for mode in parasitics.MODES:
        r = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "mim", "mode": mode})
        d = _job(srv, r["job"]).path
        raw = _caps((d / "c.spice").read_text())[frozenset(("c0", "c1"))]
        ms = r["mim_shielding"]
        assert [p["nets"] for p in ms["pairs"]] == [["c0", "c1"]] and ms["pairs"][0]["capacitors"] == 1
        p = ms["pairs"][0]
        assert p["magic_ff"] == pytest.approx(raw, abs=2e-3) and p["overlap_ff"] == pytest.approx(want, rel=1e-3)
        # the strap's edges are the capacitor's: their fringe lands on the bottom plate outside it, and stays
        assert p["removed_ff"] == pytest.approx(want, abs=2e-3) and abs(ms["fringe_ff"]) < 2e-3
        assert p["kept_ff"] == pytest.approx(raw - want, abs=2e-3) and p["kept_ff"] > 0
        assert r["coupling"][0] == {"nets": ["c0", "c1"], "c_ff": p["kept_ff"]}
        assert _caps((d / "extracted.spice").read_text())[frozenset(("c0", "c1"))] == pytest.approx(p["kept_ff"], abs=2e-3)
        assert "mim_overlap_removed" not in r and (d / "shielded" / "c.spice").is_file()
        assert r["runtime_s"] > 0
        kept[mode] = p["kept_ff"]
    assert kept["c"] == kept["rc"]
    shutil.rmtree(root)


def test_metal_over_a_bottom_plate_outside_the_capacitor_keeps_its_coupling():
    """A 1.17 um unit (its 0.78 um met4 contact pad, no strap) as Magic extracts it: 191 aF between its plates, of
    which 51 aF is the pad's area and about 40 aF the fringe of its edges inside the capacitor; the coupling outside
    it (the pad's fringe onto the bottom plate around it, the bottom plate's met4 contact beside it) stays. A met4 wire
    of another net over the bottom plate beside the capacitor keeps its coupling to it (its overlap there, 53 aF at
    least); what its fringe gave the bottom plate inside the capacitor goes to the top plate, the wire's total stays."""
    pdk = _need()

    def unit(wire: bool) -> dict:
        ly = kdb.Layout()
        ly.dbu = 0.001
        path, name, _ = pcell.generate(pdk, "mim_cap", {"type": "sky130_fd_pr__model__cap_mim", "w": 1.17, "l": 1.17})
        src = kdb.Layout()
        src.read(str(path))
        u = ly.create_cell(name)
        u.copy_tree(src.cell(name))
        top = ly.create_cell("u")
        top.insert(kdb.DCellInstArray(u.cell_index(), kdb.DTrans()))
        m4, t4 = ly.layer(71, 20), ly.layer(71, 5)
        top.shapes(t4).insert(kdb.DText("t", 0.585, 0.585))
        top.shapes(t4).insert(kdb.DText("b", 2.57, 0.585))
        if wire:                                       # over the bottom plate, 0.23 um beside the capacitor
            top.shapes(m4).insert(kdb.DBox(1.4, -1.5, 1.8, 2.5))
            top.shapes(t4).insert(kdb.DText("w", 1.6, 2.4))
        root, srv, ctx = _project(pdk, ly)
        r = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "u", "mode": "c", "cthresh_ff": 0.0001})
        d = _job(srv, r["job"]).path                   # the substrate's capacitance: the first extraction's
        raw, shielded = _caps((d / "c.spice").read_text()), _caps((d / "shielded" / "c.spice").read_text())
        ground = {x["net"]: x["ground_ff"] for x in r["c_per_net"]}
        assert ground["b"] == pytest.approx(raw[frozenset(("b", "VSUBS"))], abs=1e-4)
        if not wire:                                   # what the capacitor's fringe rules took of it (2.8 aF)
            assert raw[frozenset(("b", "VSUBS"))] - shielded[frozenset(("b", "VSUBS"))] > 1e-3
        assert not any("VSUBS" in m["nets"] for m in r["mim_shielding"]["moved"])
        if wire:                                       # one net's rows, found in any case; a net it lacks: near ones
            one = _call(ctx, "get_parasitics", {"lib": "l", "cell": "u", "net": "W"})
            assert one["net"] == "w" and {r["net"] for r in one["c_per_net"]} == {"w"}
            assert one["coupling"] and all("w" in x["nets"] for x in one["coupling"]) and "ports" not in one
            assert {tuple(m["nets"]) for m in one["mim_shielding"]["moved"]} == {("b", "w"), ("t", "w")}
            res, err = T.call(ctx, "get_parasitics", {"lib": "l", "cell": "u", "net": "ww"})
            assert err and "no net ww in this extraction (near: w" in res, res
        shutil.rmtree(root)
        return r
    alone = unit(False)
    p = alone["mim_shielding"]["pairs"][0]
    assert p["nets"] == ["b", "t"] and p["overlap_ff"] == pytest.approx(0.0511, abs=1e-3)
    assert p["magic_ff"] == pytest.approx(0.191, abs=0.01) and p["removed_ff"] == pytest.approx(0.0895, abs=0.01)
    assert p["kept_ff"] == pytest.approx(0.102, abs=0.01)          # Magic's coupling outside the capacitor
    r = unit(True)
    moved = {tuple(m["nets"]): m for m in r["mim_shielding"]["moved"]}
    bw, tw = moved[("b", "w")], moved[("t", "w")]
    assert bw["kept_ff"] > 0.9 * bw["magic_ff"] and bw["kept_ff"] > 0.628 * 84.03e-3      # 0.4 x 1.57 um over it
    assert tw["kept_ff"] > tw["magic_ff"]
    assert bw["kept_ff"] + tw["kept_ff"] == pytest.approx(bw["magic_ff"] + tw["magic_ff"], abs=1e-3)


def test_an_lvs_that_kept_nothing_of_the_layout_says_why():
    """Two MIM units placed and not wired, no labels: KLayout's netlist simplification purges every net (one connection,
    no pin) and with them the circuit, so the cross-reference is empty. get_lvs_report says that, with the schematic's
    devices and pins the layout lacks, and that the extracted netlist is empty."""
    pdk = _need()
    if not verify.decks(pdk)["lvs"]:
        pytest.skip("no KLayout LVS deck")
    ly = kdb.Layout()
    ly.dbu = 0.001
    path, name, _ = pcell.generate(pdk, "mim_cap", {"type": "sky130_fd_pr__model__cap_mim", "w": 1.17, "l": 1.17})
    src = kdb.Layout()
    src.read(str(path))
    unit = ly.create_cell(name)
    unit.copy_tree(src.cell(name))
    top = ly.create_cell("pair")
    for x in (0, 6):
        top.insert(kdb.DCellInstArray(unit.cell_index(), kdb.DTrans(kdb.DVector(x, 0))))
    root, srv, ctx = _project(pdk, ly)
    (root / "ref.spice").write_text(".SUBCKT pair c0 c1 t\nCC1 t c0 sky130_fd_pr__model__cap_mim A=1.3689 P=4.68 m=1\n"
                                    "CC2 t c1 sky130_fd_pr__model__cap_mim A=1.3689 P=4.68 m=1\n.ENDS\n")
    lvs = srv.start_lvs("p", "l", "pair", schematic_file=str(root / "ref.spice"))
    while lvs.status()["state"] in ("queued", "running"):
        time.sleep(0.3)
    r = _call(ctx, "get_lvs_report", {"lib": "l", "cell": "pair"})
    assert r["match"] is False and r["counts"] == {"nets": 0, "devices": 0, "pins": 0, "subcircuits": 0}
    assert r["why"][0].startswith("KLayout's netlist of the layout is empty (no circuit pair)")
    assert "no labels, so no pins (the schematic has 3)" in r["why"][0]
    assert "SKY130_FD_PR__MODEL__CAP_MIM: 0 in the layout, 2 in the schematic" in r["why"]
    assert any(w.startswith("schematic pins the layout does not have") and "C0, C1, T" in w for w in r["why"])
    assert r["layout"]["circuit"] is None and r["schematic"]["devices"] == {"SKY130_FD_PR__MODEL__CAP_MIM": 2}
    assert r["extracted_from_layout"].rstrip().endswith("(no circuit: KLayout kept nothing of the layout, see why)")
    shutil.rmtree(root)


def test_an_lvs_with_too_few_devices_counts_them_by_one_class():
    """Two MIM units wired and labelled against a schematic of three: KLayout's layout netlist keeps the deck's class
    name in lower case, its SPICE reader upper-cases the schematic's; the counts are of one class, in both."""
    pdk = _need()
    if not verify.decks(pdk)["lvs"]:
        pytest.skip("no KLayout LVS deck")
    ly = kdb.Layout()
    ly.dbu = 0.001
    path, name, _ = pcell.generate(pdk, "mim_cap", {"type": "sky130_fd_pr__model__cap_mim", "w": 1.17, "l": 1.17})
    src = kdb.Layout()
    src.read(str(path))
    unit = ly.create_cell(name)
    unit.copy_tree(src.cell(name))
    top = ly.create_cell("pair")
    for x in (0, 6):
        top.insert(kdb.DCellInstArray(unit.cell_index(), kdb.DTrans(kdb.DVector(x, 0))))
    m4, t4 = ly.layer(71, 20), ly.layer(71, 5)
    for b in (kdb.DBox(0.385, 0.385, 0.785, 3.0), kdb.DBox(6.385, 0.385, 6.785, 3.0), kdb.DBox(0.385, 2.6, 6.785, 3.0)):
        top.shapes(m4).insert(b)                       # the two top plates' pads joined above the units
    for text, x, y in (("t", 3, 2.8), ("c0", 2.57, 0.585), ("c1", 8.57, 0.585)):
        top.shapes(t4).insert(kdb.DText(text, x, y))
    root, srv, ctx = _project(pdk, ly)
    (root / "ref.spice").write_text(".SUBCKT pair c0 c1 t\nCC1 t c0 sky130_fd_pr__model__cap_mim A=1.3689 P=4.68 m=1\n"
                                    "CC2 t c1 sky130_fd_pr__model__cap_mim A=1.3689 P=4.68 m=1\n"
                                    "CC3 t c1 sky130_fd_pr__model__cap_mim A=1.3689 P=4.68 m=1\n.ENDS\n")
    lvs = srv.start_lvs("p", "l", "pair", schematic_file=str(root / "ref.spice"))
    while lvs.status()["state"] in ("queued", "running"):
        time.sleep(0.3)
    r = _call(ctx, "get_lvs_report", {"lib": "l", "cell": "pair"})
    assert r["match"] is False
    assert r["layout"]["devices"] == {"SKY130_FD_PR__MODEL__CAP_MIM": 2}
    assert r["schematic"]["devices"] == {"SKY130_FD_PR__MODEL__CAP_MIM": 3}
    counts = [w for w in r["why"] if " in the layout, " in w]
    assert counts == ["SKY130_FD_PR__MODEL__CAP_MIM: 2 in the layout, 3 in the schematic"], r["why"]
    shutil.rmtree(root)


def _chain_files() -> dict[str, XFile]:
    """Two nfets in series: M1 (mid, a, VSS) and M2 (out, b, mid); mid is an inner net."""
    sch = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    fet = dict(model="nfet_01v8", L="0.15", W="1", nf="1", mult="1", spiceprefix="X")
    sch.comps = [C("sky130_fd_pr/nfet_01v8.sym", 0, 0, name="M1", **fet), C("sky130_fd_pr/nfet_01v8.sym", 200, 0, name="M2", **fet),
                 C("devices/lab_pin.sym", 20, -30, name="l1", lab="mid"), C("devices/iopin.sym", -20, 0, name="p1", lab="a"),
                 C("devices/iopin.sym", 20, 30, name="p2", lab="VSS"), C("devices/lab_pin.sym", 20, 0, name="l2", lab="VSS"),
                 C("devices/iopin.sym", 220, -30, name="p3", lab="out"), C("devices/iopin.sym", 180, 0, name="p4", lab="b"),
                 C("devices/lab_pin.sym", 220, 30, name="l3", lab="mid"), C("devices/lab_pin.sym", 220, 0, name="l4", lab="VSS")]
    return {"chain.sch": sch}


def _chain(pdk):
    """The chain's layout: M1 at the origin (VSS on the source / drain butted against its tap), M2 3 um to the right;
    mid joins M1's left and M2's right source / drain in met1 below them, with no label."""
    if tools.xschem_library() is None:
        pytest.skip("Xschem's devices library is not installed")
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("chain")
    fet = _fet(pdk, ly, "nfet")
    for x in (0, 3):
        top.insert(kdb.DCellInstArray(fet.cell_index(), kdb.DTrans(kdb.DVector(x, 0))))
    top.shapes(ly.layer(67, 20)).insert(kdb.DBox(0.6, 0.155, 0.95, 0.845))
    for box in ((0.035, -0.6, 0.265, 0.825), (0.035, -0.6, 3.715, -0.4), (3.485, -0.6, 3.715, 0.825)):
        top.shapes(ly.layer(68, 20)).insert(kdb.DBox(*box))
    for t, x, y in (("VSS", 0.6, 0.5), ("a", 0.375, 1.36), ("out", 3.15, 0.5), ("b", 3.375, 1.36)):
        top.shapes(ly.layer(68, 5)).insert(kdb.DText(t, x, y))
    return _project(pdk, ly, _chain_files())


def test_nets_and_devices_take_the_schematic_names_through_lvs():
    """The inner net mid has no label: Magic names it after a point on it (a_N_M#), so a testbench's x1.mid would be a
    new, floating node; its devices are X0 and X1, so @m.x1.xm1.msky130_fd_pr__nfet_01v8[id] finds none. With an LVS
    run of the same layout, the extraction labels mid at one of its contacts with the name LVS matched it to, and
    names each device after the element a simulation netlist writes for the instance LVS matched it to (XM1, XM2),
    in both modes; without one it says why it could not."""
    pdk = _need()
    root, srv, ctx = _chain(pdk)
    before = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "chain", "mode": "c"})
    assert before["names"] == {"from_lvs": None, "why": "no LVS run of this layout: run_checks (LVS), then extract again"}
    assert any(r["net"].endswith("#") for r in before["c_per_net"])
    lvs = srv.prepare_lvs("p", "l", "chain").launch()
    while lvs.status()["state"] in ("queued", "running"):
        time.sleep(0.3)
    assert lvs.status()["summary"]["match"], lvs.status()
    for mode in parasitics.MODES:
        r = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "chain", "mode": mode})
        assert r["names"] == {"from_lvs": lvs.id, "labelled": ["mid"], "devices_named": 2, "devices_kept": []}
        assert r["ports"]["match"] and r["ports"]["extra_labels"] == []
        assert not any("#" in x["net"] for x in r["c_per_net"])
        net = parasitics.read_spice_text((_job(srv, r["job"]).path / "extracted.spice").read_text())
        mids = [t for x in net["devices"] for t in parasitics._terminals(x) if t == "mid" or t.startswith("mid.")]
        assert len(mids) == 2, net["devices"]                                # M1's drain and M2's source
        fets = {x.split()[0]: [_net(t) for t in parasitics._terminals(x)] for x in net["devices"]}
        assert {k: sorted(v[0::2]) + v[1::2] for k, v in fets.items()} == \
            {"XM1": ["VSS", "mid", "a", "VSS"], "XM2": ["mid", "out", "b", "VSS"]}, fets          # d / s either way
        text = f".subckt chain {' '.join(net['ports'])}\n" + "\n".join(net["devices"]) + "\n.ends\nx1 a b out 0 chain\n"
        assert parasitics.unknown_nodes(text + f"B1 s 0 V=v(x1.{mids[0]})\n.control\n"
                                        "let k = @m.x1.xm2.msky130_fd_pr__nfet_01v8[l] > 0\n.endc\n", {"chain"}) == {}
        assert parasitics.unknown_nodes(text + ".save @m.x1.xm3.msky130_fd_pr__nfet_01v8[id]\n", {"chain"}) == \
            {"chain": ["@m.x1.xm3.msky130_fd_pr__nfet_01v8"]}
    shutil.rmtree(root)


_GF180_N, _GF180_P = "nfet_05v0 W=8.2e-07 L=6e-07", "pfet_05v0 W=1.22e-06 L=5e-07"
_IHP_N, _IHP_P = "sg13_lv_nmos w=740n l=130n ng=1", "sg13_lv_pmos w=1.12u l=130n ng=1"


@pytest.mark.parametrize("name, inv, pitch, vias, metal2, labels, ref, unnamed", [
    # ZN's metal1 at 1.24-1.62, the next I's at 2.92-3.30; the pfets' n-well is a net of its own (nw, no contact)
    ("gf180mcuD", "gf180mcu_fd_sc_mcu7t5v0__inv_1", 2.24, ((1.30, 1.57, 1.56, 1.83), (2.98, 1.57, 3.24, 1.83)),
     (1.24, 1.51, 3.30, 1.89), (("in", 0.87, 1.7), ("out", 3.67, 1.0), ("VDD", 1.12, 3.92), ("VSS", 1.12, -0.08)),
     f".SUBCKT chain in out VDD VSS SUB\nM0 mid in VSS SUB {_GF180_N}\nM1 mid in VDD nw {_GF180_P}\n"
     f"M2 out mid VSS SUB {_GF180_N}\nM3 out mid VDD nw {_GF180_P}\n.ENDS\n", ["nw"]),
    # Y's metal1 at 0.855-1.085, the next A's at 1.75-2.065; the n-well is on VDD through the cell's own tap
    ("ihp-sg13g2", "sg13g2_inv_1", 1.44, ((0.875, 1.59, 1.065, 1.78), (1.81, 1.59, 2.0, 1.78)),
     (0.825, 1.54, 2.05, 1.83), (("in", 0.48, 1.68), ("out", 2.42, 1.69), ("VDD", 0.705, 3.78), ("VSS", 0.72, -0.02)),
     f".SUBCKT chain in out VDD VSS\nMN0 mid in VSS VSS {_IHP_N}\nMP0 mid in VDD VDD {_IHP_P}\n"
     f"MN1 out mid VSS VSS {_IHP_N}\nMP1 out mid VDD VDD {_IHP_P}\n.ENDS\n", [])])
def test_inner_nets_of_gf180_and_ihp_take_the_schematic_names_through_lvs(name, inv, pitch, vias, metal2, labels, ref,
                                                                            unnamed):
    """GF180 and IHP (net_labels from contact up): two of the PDK's inverters abutted, the first's output to the second's
    input in metal2 without a label (mid). With KLayout's LVS against a netlist, the extraction labels mid at one of
    its contacts in metal1, in both modes, and the label is no port of the cell; a net without a contact (GF180's
    n-well) keeps Magic's name and says so."""
    pdk = next((p for p in PDKS.values() if p.name == name), None)
    if pdk is None or not tools.find("klayout") or not tools.find("magic") or not verify.decks(pdk)["lvs"] or \
            not verify.decks(pdk)["magic"]:
        pytest.skip(f"{name}, KLayout or Magic is not installed")
    L = pdk.profile.layers
    lib, gds = pdk.profile.stdcell(inv)
    src = kdb.Layout()
    src.read(str(pdk.root / gds))
    ly = kdb.Layout()
    ly.dbu = src.dbu
    ic = ly.create_cell(inv)
    ic.copy_tree(src.cell(inv))
    top = ly.create_cell("chain")
    for x in (0, pitch):
        top.insert(kdb.DCellInstArray(ic.cell_index(), kdb.DTrans(kdb.DVector(x, 0))))
    for box in vias:                                                           # via1 on the output and the next input
        top.shapes(ly.layer(*L["via1"])).insert(kdb.DBox(*box))
    top.shapes(ly.layer(*L["metal2"])).insert(kdb.DBox(*metal2))
    for t, x, y in labels:
        top.shapes(ly.layer(*L["metal1.label"])).insert(kdb.DText(t, x, y))
    root, srv, ctx = _project(pdk, ly)
    (root / "ref.spice").write_text(ref)
    lvs = srv.start_lvs("p", "l", "chain", schematic_file=str(root / "ref.spice"))
    while lvs.status()["state"] in ("queued", "running"):
        time.sleep(0.3)
    assert lvs.status()["summary"]["match"], lvs.status()
    for mode in parasitics.MODES:
        r = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "chain", "mode": mode})
        assert {k: r["names"].get(k, []) for k in ("from_lvs", "labelled", "unnamed")} == \
            {"from_lvs": lvs.id, "labelled": ["mid"], "unnamed": unnamed}, r["names"]
        assert "mid" in {x["net"] for x in r["c_per_net"]} and r["ports"]["order"] == ["in", "out", "VDD", "VSS"]
        net = parasitics.read_spice_text((_job(srv, r["job"]).path / "extracted.spice").read_text())
        assert sum(1 for x in net["devices"] for t in parasitics._terminals(x) if _net(t) == "mid") == 4, net["devices"]
    shutil.rmtree(root)


def test_device_names_follow_the_nets_through_unnamed_ones():
    """Without Magic: three of Magic's devices against LVS's two elements. XM1 (nf 2) is two of them, on the net a_5_5#
    without a name that is mid in the layout; XM2 is the third, and the one that names a_5_5# first decides it; a device
    of a model no element has keeps its name, and so does one that fits two elements unalike."""
    lay = [{"name": "XXM1", "models": ["sky130_fd_pr__nfet_01v8"], "l": 0.15,
            "terms": [["S", "vss"], ["G", "a"], ["D", "mid"], ["B", "vss"]]},
           {"name": "XXM2", "models": ["sky130_fd_pr__nfet_01v8"], "l": 0.15,
            "terms": [["S", "mid"], ["G", "b"], ["D", "out"], ["B", "vss"]]},
           {"name": "XXC1", "models": ["sky130_fd_pr__model__cap_mim", r"(sky130_fd_pr__)?cap_mim_m3_1"], "l": None,
            "terms": [["A", "out"], ["B", "top"]]}]
    fet = "sky130_fd_pr__nfet_01v8 w=1 l=0.15"
    lines = [f"X0 out b a_5_5# vss {fet}", f"X1 a_5_5# a vss vss {fet}", f"X2 vss a a_5_5# vss {fet}",
             "X3 top out sky130_fd_pr__cap_mim_m3_1 w=2 l=2", "X4 top out sky130_fd_pr__cap_mim_m3_1 w=2 l=2",
             "X5 a b vss vss sky130_fd_pr__pfet_01v8 w=1 l=0.15"]
    names, kept, split = parasitics.device_names(lines, lay, lambda n: n, lambda n: bool(parasitics._ANON.fullmatch(n)))
    assert names == {"X0": "XXM2", "X1": "XXM1", "X2": "XXM1_2", "X3": "XXC1", "X4": "XXC1_2"} and kept == ["X5"]
    assert split == {"XXM1": 2, "XXC1": 2}
    twins = [dict(lay[0], name="XXD1", terms=[["S", "vss"], ["G", "vss"], ["D", "n1"], ["B", "vss"]]),
             dict(lay[0], name="XXD2", terms=[["S", "vss"], ["G", "vss"], ["D", "n2"], ["B", "vss"]])]
    names, kept, _ = parasitics.device_names(["X0 a_1_1# vss vss vss sky130_fd_pr__nfet_01v8 w=1 l=0.15"], twins,
                                             lambda n: n, lambda n: bool(parasitics._ANON.fullmatch(n)))
    assert names == {} and kept == ["X0"]                                  # n1 or n2: nothing says which


def test_unknown_nodes_of_an_extracted_view():
    net = ("* tb\n.lib /pdk/sky130.lib.spice tt\n.subckt boot vin vout vss\nX0 vout a_31_52# vin vss nfet w=20 l=0.15\n"
           "R1 g g.n0 12\nC3 g.n0 vss 1f\n.ends\n.subckt wrap a b\nx1 a b 0 boot\n.ends\nx1 in out 0 boot\nx2 in out wrap\n"
           "Bs1 s 0 V=v(x1.g)*2+v(x1.vout)+v(x1.X0)\n.save x1.g.n0 x2.x1.nope v(x9.foo) @m.x1.xm1[id] 1.8\n.control\n"
           "plot v(x1.bt) v(x2.x1.vout)\nwrdata x1.txt v(out)\nlet k = @m.x1.x0.msky130_fd_pr__nfet_01v8[l] > 0\n"
           ".endc\n.end\n")
    # x1.g, its rc node, an element and a port are there; x9 is no instance; @m.x1.x0 a device, xm1 none; x1.txt a file
    assert parasitics.unknown_nodes(net, {"boot"}) == {"boot": ["x2.x1.nope", "@m.x1.xm1", "x1.bt"]}
    # wrap extracted: flat, so nothing below it is reached by a path through x1
    assert parasitics.unknown_nodes(net, {"wrap"}) == {"wrap": ["x2.x1.nope", "x2.x1.vout"]}


def test_a_simulation_naming_a_node_the_extracted_view_lacks_is_refused():
    pdk = _need(sim=True)
    root, srv, ctx = _inverter(pdk, wire_um=50.0)
    c = _call(ctx, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c"})
    run = {"lib": "l", "cell": "tb", "analysis": TRAN, "extracted": ["l/invw"]}
    res, err = T.call(ctx, "run_simulation", {**run, "signals": ["out", "x1.mid"],
                                              "measure": [".meas tran vb FIND v(x1.b) AT=1n"]})
    assert err and "x1.mid: no such node in the extracted view of l/invw (job " + c["job"] in res, res
    assert "x1.b" not in res.split("x1.mid")[0] and "run_checks" in res
    # a device quantity: without an LVS run the devices keep Magic's names (X0, X1), and ngspice would evaluate nothing
    res, err = T.call(ctx, "run_simulation", {**run, "signals": ["out"],
                                              "measure": [".meas tran l1 FIND @m.x1.xm1.msky130_fd_pr__nfet_01v8[l] AT=1n"]})
    assert err and "@m.x1.xm1.msky130_fd_pr__nfet_01v8: no such device in the extracted view of l/invw" in res, res
    assert "keep Magic's names (X0, X1 ..)" in res and "no such node" not in res
    ok = _call(ctx, "run_simulation", {**run, "signals": ["out", "x1.b"]})
    assert ok["state"] == "done", ok
    shutil.rmtree(root)
