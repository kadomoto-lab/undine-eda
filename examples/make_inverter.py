"""Creates a transistor-level CMOS inverter (inv) and a testbench (tb_inv) in a project library.

    python examples/make_inverter.py PROJECT LIB

Writes the files directly into the project under $UNDINE_ROOT (default ~/undine-projects), creating the library if needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from undine.schematics import xschem  # noqa: E402
from undine.__main__ import DEFAULT_ROOT  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402
from undine.schematics.xschem import XComp, XFile, XText  # noqa: E402


def C(sym, x, y, rot=0, flip=0, **p):
    return XComp(sym, x, y, rot, flip, p)


def inv() -> XFile:
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    fet = dict(L="0.15", nf="1", mult="1", spiceprefix="X")
    f.comps = [
        C("sky130_fd_pr/pfet_01v8.sym", 0, -60, name="M1", W="2", model="pfet_01v8", **fet),
        C("sky130_fd_pr/nfet_01v8.sym", 0, 60, name="M2", W="1", model="nfet_01v8", **fet),
        C("devices/ipin.sym", -120, 0, name="p1", lab="A"),
        C("devices/opin.sym", 120, 0, name="p2", lab="Y"),
        C("devices/iopin.sym", -120, -120, name="p3", lab="VDD"),
        C("devices/iopin.sym", -120, 120, name="p4", lab="VSS"),
    ]
    f.wires = [(-120, 0, -20, 0, {}), (-20, -60, -20, 60, {}), (20, -30, 20, 30, {}), (20, 0, 120, 0, {}),
               (20, -90, 40, -90, {}), (40, -90, 40, -60, {}), (40, -60, 20, -60, {}),
               (20, -90, -120, -90, {}), (-120, -90, -120, -120, {}),
               (20, 90, 40, 90, {}), (40, 90, 40, 60, {}), (40, 60, 20, 60, {}),
               (20, 90, -120, 90, {}), (-120, 90, -120, 120, {})]
    return f


def inv_sym() -> XFile:
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    f.global_props = {"type": "subcircuit", "format": "@name @pinlist @symname", "template": "name=x1"}
    f.k_raw = 'type=subcircuit\nformat="@name @pinlist @symname"\ntemplate="name=x1"\n'
    f.boxes = [(4, -40, -40, 40, 40, {}),
               (5, -42.5, -2.5, -37.5, 2.5, {"name": "A", "dir": "in"}),
               (5, 37.5, -2.5, 42.5, 2.5, {"name": "Y", "dir": "out"}),
               (5, -2.5, -42.5, 2.5, -37.5, {"name": "VDD", "dir": "inout"}),
               (5, -2.5, 37.5, 2.5, 42.5, {"name": "VSS", "dir": "inout"})]
    f.lines = [(4, -40, -40, 40, -40, {}), (4, 40, -40, 40, 40, {}), (4, 40, 40, -40, 40, {}), (4, -40, 40, -40, -40, {})]
    f.texts = [XText("@symname", -20, -10, 0, 0, 0.3, 0.3, {}), XText("@name", 0, 44, 0, 0, 0.2, 0.2, {})]
    return f


def tb() -> XFile:
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    f.comps = [
        C("inv.sym", 0, 0, name="x1"),
        C("devices/vsource.sym", -200, -100, name="Vdd", value="1.8"),
        C("devices/vsource.sym", -200, 60, name="Vin", value="pulse(0 1.8 1n 100p 100p 5n 10n)"),
        C("devices/capa.sym", 160, 40, name="CL", value="20f"),
        C("devices/lab_pin.sym", -40, 0, lab="in", name="l1"),
        C("devices/lab_pin.sym", -200, 30, lab="in", name="l2"),
        C("devices/lab_pin.sym", 40, 0, lab="out", name="l3"),
        C("devices/lab_pin.sym", 160, 10, lab="out", name="l4"),
        C("devices/lab_pin.sym", -200, -130, lab="vdd", name="l5"),
        C("devices/lab_pin.sym", 0, -40, lab="vdd", name="l6"),
        C("devices/gnd.sym", -200, 90, name="l7", lab="GND"),
        C("devices/gnd.sym", -200, -70, name="l8", lab="GND"),
        C("devices/gnd.sym", 160, 70, name="l9", lab="GND"),
        C("devices/gnd.sym", 0, 40, name="l10", lab="GND"),
        C("devices/code_shown.sym", -300, 160, name="s1", only_toplevel="false",
          value='.lib $::SKYWATER_MODELS/sky130.lib.spice tt\n.save all\n'
                '.control\nset wr_singlescale\ntran 20p 20n\nwrite tb_inv.raw\n.endc'),
    ]
    return f


if __name__ == "__main__":
    project, lib = sys.argv[1:3]
    prj = ProjectStore(DEFAULT_ROOT, discover(default_pdk_roots())).get(project)
    d = (prj.libraries().get(lib) or prj.create_library(lib)).path
    for name, f in (("inv.sch", inv()), ("inv.sym", inv_sym()), ("tb_inv.sch", tb())):
        (d / name).write_text(xschem.dump(f))
        print("wrote", d / name)
