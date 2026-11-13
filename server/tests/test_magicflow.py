"""Magic DRC / Netgen LVS result parsing and the netlist flavour Netgen needs."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.checks import magicflow  # noqa: E402
from undine.schematics import schematic, xschem
import test_changeset as tc  # noqa: E402


def test_magic_drc_tsv_is_summarised_and_browsable():
    d = Path(tempfile.mkdtemp())
    f = d / "result.tsv"
    f.write_text("Local interconnect spacing < 0.17um (li.3)\t1.0\t2.0\t1.5\t2.5\n"
                 "Local interconnect spacing < 0.17um (li.3)\t3.0\t2.0\t3.5\t2.5\n"
                 "nFET cannot abut P-diffusion (diff/tap.3)\t0.0\t0.0\t0.2\t0.2\n")
    s = magicflow.drc_summary(f)
    assert s["total"] == 3 and s["rules"] == 2 and s["categories"][0]["name"] == "li.3" and s["categories"][0]["count"] == 2
    items = magicflow.drc_items(f, s["categories"][1]["id"])["items"]
    assert len(items) == 1 and items[0]["cat"] == "diff/tap.3" and items[0]["bbox"] == [0.0, 0.0, 0.2, 0.2]
    shutil.rmtree(d)


def test_netgen_verdicts():
    d = Path(tempfile.mkdtemp())
    f = d / "lvs.out"
    f.write_text("Circuit 1 contains 2 devices, Circuit 2 contains 2 devices.\nCircuit 1 contains 12 nets,    Circuit 2 contains 12 nets.\n\nFinal result: \nCircuits match uniquely.\n.\n")
    r = magicflow.lvs_summary(f, "inv")
    assert r["match"] and r["devices"] == [2, 2] and r["nets"] == [12, 12] and r["circuits"][0]["status"] == "Match"
    f.write_text("Final result: Top level cell failed pin matching.\n")
    r = magicflow.lvs_summary(f, "inv")
    assert not r["match"] and "failed pin matching" in r["verdict"]
    f.write_text("Final result: Circuits match with property errors.\n")
    r = magicflow.lvs_summary(f, "inv")
    assert not r["match"] and r["warnings"] and r["circuits"][0]["status"] == "MatchWithWarning"
    shutil.rmtree(d)


def test_netgen_netlist_keeps_subcircuit_instances():
    root, srv, cs = tc.setup()
    prj = srv.project("p")
    sch = prj.library("l").path / "n.sch"
    f = xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")
    (prj.library("l").path / "fet.sym").write_text('v {}\nK {type=nmos\nlvs_format="@spiceprefix@name @pinlist sky130_fd_pr__nfet_01v8 L=@L W=@W"\n'
                                                   'template="name=M1 L=0.15 W=1 spiceprefix=X"}\nB 5 -2.5 -2.5 2.5 2.5 {name=D}\n')
    f.comps = [xschem.XComp("fet.sym", 0, 0, 0, 0, {"name": "M1", "L": "0.15", "W": "1", "spiceprefix": "X"})]
    sch.write_text(xschem.dump(f))
    res = xschem.SymbolResolver(prj.symbol_paths())
    net = schematic.spice(sch, res, {}, mode="netgen", top_subckt=True, top_name="n").text
    klay = schematic.spice(sch, res, {}, mode="lvs", top_subckt=True, top_name="n").text
    assert "XM1 " in net and "\nM1 " in klay or "\nMM1 " in klay or "\nM1 " in klay.replace("\nMM1", "\nM1")
    assert "XM1" in net and not any(l.startswith("M") for l in net.splitlines())
    shutil.rmtree(root)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
