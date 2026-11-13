"""Three-way merge and semantic diff of Xschem schematics and symbols (undine.vcs.schmerge, docs/design/merge.md)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.schematics import xschem  # noqa: E402
from undine.vcs import schmerge  # noqa: E402

# a sky130 inverter as Xschem writes it (MOSFET attributes one per line)
INV = """v {xschem version=3.4.5 file_version=1.2
}
G {}
K {}
V {}
S {}
E {}
T {CMOS inverter} -80 -200 0 0 0.4 0.4 {}
N -60 -60 -60 60 {lab=A}
N -100 0 -60 0 {lab=A}
N 0 -30 0 30 {lab=Y}
N 0 0 100 0 {lab=Y}
N 0 -120 0 -90 {lab=VDD}
N 0 90 0 120 {lab=VSS}
C {sky130_fd_pr/pfet_01v8.sym} -20 -60 0 0 {name=M1
L=0.15
W=2
nf=1
mult=1
ad="expr('int((@nf + 1)/2) * @W / @nf * 0.29')"
model=pfet_01v8
spiceprefix=X
}
C {sky130_fd_pr/nfet_01v8.sym} -20 60 0 0 {name=M2
L=0.15
W=1
nf=1
mult=1
ad="expr('int((@nf + 1)/2) * @W / @nf * 0.29')"
model=nfet_01v8
spiceprefix=X
}
C {devices/ipin.sym} -100 0 0 0 {name=p1 lab=A}
C {devices/opin.sym} 100 0 0 0 {name=p2 lab=Y}
C {devices/lab_pin.sym} 0 -120 0 0 {name=l1 sig_type=std_logic lab=VDD}
C {devices/lab_pin.sym} 0 120 0 0 {name=l2 sig_type=std_logic lab=VSS}
"""

INV_SYM = """v {xschem version=3.4.5 file_version=1.2
}
G {}
K {type=subcircuit
format="@name @pinlist @symname"
template="name=x1"
}
V {}
S {}
E {}
L 4 -40 -30 40 -30 {}
L 4 40 -30 40 30 {}
L 4 40 30 -40 30 {}
L 4 -40 30 -40 -30 {}
L 4 -60 0 -40 0 {}
L 4 40 0 60 0 {}
B 5 -62.5 -2.5 -57.5 2.5 {name=A dir=in}
B 5 57.5 -2.5 62.5 2.5 {name=Y dir=out}
T {@symname} -45 -46 0 0 0.3 0.3 {}
T {@name} -40 34 0 0 0.2 0.2 {}
T {A} -35 -6 0 0 0.2 0.2 {}
T {Y} 25 -6 0 1 0.2 0.2 {}
"""


def nfet3(x: int, w: int) -> str:
    """A third transistor, M3, as Xschem places it."""
    return (f"C {{sky130_fd_pr/nfet_01v8.sym}} {x} 60 0 0 {{name=M3\nL=0.15\nW={w}\nnf=1\nmult=1\nmodel=nfet_01v8\n"
            "spiceprefix=X\n}\n")


def edit(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def merge(base: str | None, ours: str, theirs: str, kind: str = "schematic", **kw):
    return schmerge.merge(None if base is None else base.encode(), ours.encode(), theirs.encode(), kind=kind, **kw)


def comps(data: bytes) -> dict:
    f = xschem.parse(data.decode())
    names = [c.props.get("name") for c in f.comps]
    assert len(names) == len(set(names)), names
    return {c.props.get("name"): c for c in f.comps}


def ids(r) -> list[str]:
    return [c.id for c in r.conflicts]


def test_w_on_ours_and_l_on_theirs_of_one_transistor_combine():
    ours = edit(INV, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=4")
    theirs = edit(INV, "name=M1\nL=0.15", "name=M1\nL=0.3")
    r = merge(INV, ours, theirs)
    assert r.clean and r.conflicts == []
    m1 = comps(r.data)["M1"]
    assert (m1.props["W"], m1.props["L"]) == ("4", "0.3")
    assert list(m1.props)[0] == "name" and m1.props["ad"] == "expr('int((@nf + 1)/2) * @W / @nf * 0.29')"
    assert comps(r.data)["M2"].props == comps(INV.encode())["M2"].props
    assert r.combined == ["instance M1: L"]


def test_both_moving_one_instance_is_a_conflict_and_prefer_theirs_takes_theirs():
    ours = edit(INV, "{sky130_fd_pr/nfet_01v8.sym} -20 60 0 0", "{sky130_fd_pr/nfet_01v8.sym} -20 80 0 0")
    theirs = edit(INV, "{sky130_fd_pr/nfet_01v8.sym} -20 60 0 0", "{sky130_fd_pr/nfet_01v8.sym} 20 60 0 1")
    theirs = edit(theirs, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=3")       # elsewhere: combines anyway
    r = merge(INV, ours, theirs)
    assert ids(r) == ["inst:M2:placement"] and not r.clean
    c = r.conflicts[0]
    assert c.what == "instance M2: placement" and c.taken == "ours" and not c.resolved
    assert c.base == {"x": -20.0, "y": 60.0, "rot": 0, "flip": 0}
    assert c.ours == {"x": -20.0, "y": 80.0, "rot": 0, "flip": 0}
    assert c.theirs == {"x": 20.0, "y": 60.0, "rot": 0, "flip": 1}
    got = comps(r.data)
    assert (got["M2"].x, got["M2"].y, got["M2"].flip) == (-20, 80, 0)       # ours kept, not a diagonal move
    assert got["M1"].props["W"] == "3"

    r = merge(INV, ours, theirs, prefer={"inst:M2:placement": "theirs"})
    assert r.clean and r.conflicts[0].resolved and r.conflicts[0].taken == "theirs"
    got = comps(r.data)
    assert (got["M2"].x, got["M2"].y, got["M2"].rot, got["M2"].flip) == (20, 60, 0, 1)
    assert got["M1"].props["W"] == "3"


def test_delete_on_one_side_and_modify_on_the_other_is_a_conflict():
    m2 = INV[INV.index("C {sky130_fd_pr/nfet_01v8.sym}"):INV.index("C {devices/ipin.sym}")]
    deleted = edit(INV, m2, "")
    changed = edit(INV, "name=M2\nL=0.15\nW=1", "name=M2\nL=0.15\nW=2")

    r = merge(INV, deleted, changed)
    assert ids(r) == ["inst:M2"] and r.conflicts[0].ours is None and r.conflicts[0].theirs["props"]["W"] == "2"
    assert "M2" not in comps(r.data)                                            # ours: deleted
    r = merge(INV, deleted, changed, prefer={"inst:M2": "theirs"})
    assert r.clean and comps(r.data)["M2"].props["W"] == "2"

    r = merge(INV, changed, deleted)
    assert ids(r) == ["inst:M2"] and comps(r.data)["M2"].props["W"] == "2"     # ours: changed

    r = merge(INV, INV, deleted)                                                # deleted, untouched: deleted
    assert r.clean and "M2" not in comps(r.data) and r.combined == ["instance M2"]


def test_rename_on_one_side_and_prop_change_on_the_other_follows_the_new_name():
    renamed = edit(INV, "name=M1\n", "name=MP\n")
    changed = edit(INV, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=4")
    for ours, theirs in ((renamed, changed), (changed, renamed)):
        r = merge(INV, ours, theirs)
        assert r.clean, r.to_json()
        got = comps(r.data)
        assert "M1" not in got and got["MP"].props["W"] == "4" and (got["MP"].x, got["MP"].y) == (-20, -60)
        assert list(got)[:2] == ["MP", "M2"]                                    # in its place


def test_both_renaming_one_instance_differently_is_a_conflict():
    r = merge(INV, edit(INV, "name=M1\n", "name=MP\n"), edit(INV, "name=M1\n", "name=MPU\n"))
    assert ids(r) == ["rename:M1"]
    assert (r.conflicts[0].base, r.conflicts[0].ours, r.conflicts[0].theirs) == ("M1", "MP", "MPU")
    assert "MP" in comps(r.data) and "MPU" not in comps(r.data)
    r = merge(INV, edit(INV, "name=M1\n", "name=MP\n"), edit(INV, "name=M1\n", "name=MP\n"))
    assert r.clean and "MP" in comps(r.data)


def test_both_adding_one_name_differently_is_a_conflict_never_a_rename():
    ours = INV + nfet3(x=100, w=1)
    theirs = INV + nfet3(x=200, w=1)
    r = merge(INV, ours, theirs)
    assert ids(r) == ["inst:M3"] and r.conflicts[0].base is None
    got = comps(r.data)                                                         # one M3, ours'
    assert got["M3"].x == 100 and len(got) == len(comps(INV.encode())) + 1
    r = merge(INV, ours, theirs, prefer={"inst:M3": "theirs"})
    assert r.clean and comps(r.data)["M3"].x == 200

    r = merge(INV, ours, INV + nfet3(x=100, w=1))                        # the same addition: once
    assert r.clean and r.data == ours.encode()


def test_a_name_given_to_two_instances_by_the_merge_is_a_conflict():
    ours = edit(INV, "name=M2\n", "name=M3\n")                                  # M2 renamed M3
    theirs = INV + nfet3(x=200, w=4)                                     # a new M3
    r = merge(INV, ours, theirs)
    assert ids(r) == ["name:M3"]
    got = comps(r.data)                                                         # names stay unique
    assert got["M3"].x == -20 and "M2" not in got
    r = merge(INV, ours, theirs, prefer={"name:M3": "theirs"})
    assert r.clean
    got = comps(r.data)
    assert got["M3"].x == 200 and got["M2"].x == -20


def test_wires_added_on_both_sides_combine():
    ours = edit(INV, "N 0 0 100 0 {lab=Y}\n", "N 0 0 100 0 {lab=Y}\nN 100 0 140 0 {lab=Y}\n")
    theirs = edit(INV, "N -100 0 -60 0 {lab=A}\n", "N -100 0 -60 0 {lab=A}\nN -140 0 -100 0 {lab=A}\n")
    r = merge(INV, ours, theirs)
    assert r.clean
    wires = [(xschem.wire_id(*w[:4]), w[4]) for w in xschem.parse(r.data.decode()).wires]
    assert ("100,0,140,0", {"lab": "Y"}) in wires and ("-140,0,-100,0", {"lab": "A"}) in wires
    assert len(wires) == 8
    assert wires.index(("-140,0,-100,0", {"lab": "A"})) == wires.index(("-100,0,-60,0", {"lab": "A"})) + 1
    assert r.combined == ["wires: wires from theirs, 1 added"]

    # the same wire on both sides, one drawn the other way round: one wire
    r = merge(INV, ours, edit(INV, "N 0 0 100 0 {lab=Y}\n", "N 0 0 100 0 {lab=Y}\nN 140 0 100 0 {lab=Y}\n"))
    assert r.clean and r.data == ours.encode()


def test_wire_label_changed_on_one_side_and_deleted_on_the_other_is_a_conflict():
    ours = edit(INV, "N 0 90 0 120 {lab=VSS}\n", "")
    theirs = edit(INV, "N 0 90 0 120 {lab=VSS}", "N 0 90 0 120 {lab=GND}")
    r = merge(INV, ours, theirs)
    assert ids(r) == ["wire:0,90,0,120 lab=VSS"] and "ours removed, theirs changed" in r.conflicts[0].what
    r = merge(INV, INV, theirs)
    assert r.clean and r.data == theirs.encode()                                # only theirs changed: their bytes
    r = merge(INV, edit(INV, "N 0 90 0 120 {lab=VSS}", "N 0 90 0 120 {lab=VGND}"), theirs)
    assert ids(r) == ["wire:0,90,0,120 lab=VSS"] and "both sides changed" in r.conflicts[0].what


def test_symbol_pin_boxes_changed_on_both_sides_is_a_conflict():
    ours = edit(INV_SYM, "B 5 57.5", "B 5 -62.5 17.5 -57.5 22.5 {name=B dir=in}\nB 5 57.5")
    theirs = edit(INV_SYM, "B 5 57.5", "B 5 -62.5 -22.5 -57.5 -17.5 {name=EN dir=in}\nB 5 57.5")
    r = merge(INV_SYM, ours, theirs, kind="symbol")
    assert r.kind == "symbol" and ids(r) == ["drawing:boxes"]
    pins = [b[5]["name"] for b in xschem.parse(r.data.decode()).boxes if b[0] == 5]
    assert pins == ["A", "B", "Y"]                                              # ours' pins, in ours' order
    r = merge(INV_SYM, ours, theirs, kind="symbol", prefer={"drawing:boxes": "theirs"})
    assert r.clean and r.data == theirs.encode()

    # one side's pin change and the other side's label change combine
    theirs = edit(INV_SYM, "T {Y} 25 -6 0 1 0.2 0.2 {}", "T {Y} 25 -6 0 1 0.25 0.25 {}")
    r = merge(INV_SYM, ours, theirs, kind="symbol")
    f = xschem.parse(r.data.decode())
    assert r.clean and len(f.boxes) == 3 and [t.xs for t in f.texts if t.s == "Y"] == [0.25]


def test_k_record_merges_per_attribute():
    ours = edit(INV_SYM, 'format="@name @pinlist @symname"', 'format="@name @pinlist @symname m=@m"')
    theirs = edit(INV_SYM, 'template="name=x1"', 'template="name=x1 m=1"')
    r = merge(INV_SYM, ours, theirs, kind="symbol")
    assert r.clean and r.combined == ["attribute template"]
    k = xschem.parse(r.data.decode()).global_props
    assert k == {"type": "subcircuit", "format": "@name @pinlist @symname m=@m", "template": "name=x1 m=1"}

    both = edit(INV_SYM, "type=subcircuit", "type=primitive")
    r = merge(INV_SYM, both, edit(INV_SYM, "type=subcircuit", "type=label"), kind="symbol")
    assert ids(r) == ["K:type"]


def test_a_symbol_with_its_attributes_in_g_keeps_them_there():
    old = ('v {xschem version=3.4.4 file_version=1.2\n}\nG {type=delay\nverilog_ignore=true\n'
           'format="@name [ @@s ] [ @@d ] @adc_bridge_model"\ntemplate="name=A1 adc_bridge_model=adc_buff"}\nV {}\n'
           'S {}\nE {}\nL 4 -20 -10 20 -10 {}\nL 4 -20 10 20 10 {}\nB 5 -42.5 -2.5 -37.5 2.5 {name=s dir=in}\n'
           'B 5 37.5 -2.5 42.5 2.5 {name=d dir=out}\nT {@name} -20 -25 0 0 0.2 0.2 {}\n')
    assert merge(old, old, old, kind="symbol").data == old.encode()
    ours = edit(old, "L 4 -20 10 20 10 {}", "L 4 -20 10 20 10 {}\nL 4 -20 -10 -20 10 {}")
    theirs = edit(old, "T {@name} -20 -25 0 0 0.2 0.2 {}", "T {@name} -20 -30 0 0 0.2 0.2 {}")
    r = merge(old, ours, theirs, kind="symbol")
    f = xschem.parse(r.data.decode())
    assert r.clean and "\nK {" not in r.data.decode() and f.global_props == xschem.parse(old).global_props
    assert len(f.lines) == 3 and [t.y for t in f.texts] == [-30]


def test_an_unchanged_or_reformatted_model_keeps_ours_bytes():
    assert merge(INV, INV, INV).data == INV.encode()
    reformatted = xschem.dump(xschem.parse(INV))                                # attributes on one line, no K {}
    assert reformatted != INV
    for b, o, t in ((INV, INV, reformatted), (reformatted, INV, reformatted), (INV, INV, INV)):
        r = merge(b, o, t)
        assert r.clean and r.data == INV.encode() and r.combined == []
    # only theirs changed: theirs' bytes, not ours' formatting nor the writer's
    theirs = edit(INV, "name=M2\nL=0.15", "name=M2\nL=0.18")
    assert merge(INV, INV, theirs).data == theirs.encode()
    assert merge(reformatted, reformatted, theirs).data == theirs.encode()


def test_both_adding_the_file():
    r = merge(None, INV, INV)
    assert r.clean and r.data == INV.encode()
    r = merge(None, INV, xschem.dump(xschem.parse(INV)))
    assert r.clean and r.data == INV.encode()
    r = merge(None, INV, edit(INV, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=4"))
    assert ids(r) == ["inst:M1"]


def test_theirs_addition_goes_after_its_predecessor():
    ours = INV + "C {devices/lab_pin.sym} 100 -120 0 0 {name=l3 sig_type=std_logic lab=EN}\n"
    theirs = edit(INV, "C {devices/ipin.sym}", nfet3(x=60, w=1) + "C {devices/ipin.sym}")
    r = merge(INV, ours, theirs)
    assert r.clean and list(comps(r.data)) == ["M1", "M2", "M3", "p1", "p2", "l1", "l2", "l3"]


def test_theirs_reordering_is_kept_when_ours_kept_the_order():
    p1 = "C {devices/ipin.sym} -100 0 0 0 {name=p1 lab=A}\n"
    theirs = edit(edit(INV, p1, ""), "C {sky130_fd_pr/pfet_01v8.sym}", p1 + "C {sky130_fd_pr/pfet_01v8.sym}")
    ours = edit(INV, "name=M2\nL=0.15", "name=M2\nL=0.18")
    r = merge(INV, ours, theirs)
    assert r.clean and list(comps(r.data))[:3] == ["p1", "M1", "M2"] and comps(r.data)["M2"].props["L"] == "0.18"


P1 = "C {devices/ipin.sym} -100 0 0 0 {name=p1 lab=A}\n"
P2 = "C {devices/opin.sym} 100 0 0 0 {name=p2 lab=Y}\n"
P3 = "C {devices/iopin.sym} 100 60 0 0 {name=p3 lab=C}\n"


def pins(data: bytes) -> list[str]:
    """The port labels in file order: the subcircuit's pin order."""
    return [c.props["lab"] for c in xschem.parse(data.decode()).comps
            if Path(c.sym).stem in ("ipin", "opin", "iopin")]


def test_port_order_is_one_value():
    swapped = edit(INV, P1 + P2, P2 + P1)                                        # ours: pins Y, A
    appended = edit(INV, P2, P2 + P3)                                            # theirs: pins A, Y, C
    assert pins(swapped.encode()) == ["Y", "A"] and pins(appended.encode()) == ["A", "Y", "C"]
    r = merge(INV, swapped, appended)       # interleaved, it was Y, C, A clean: a pin order neither side wrote
    assert ids(r) == ["ports"] and not r.clean
    c = r.conflicts[0]
    assert (c.what, c.base, c.ours, c.theirs) == ("port order (the subcircuit's pin order)", ["p1", "p2"],
                                                  ["p2", "p1"], ["p1", "p2", "p3"])
    got = pins(r.data)
    assert sorted(got) == ["A", "C", "Y"] and got.index("Y") < got.index("A")  # ours' order of ours' pins
    r = merge(INV, swapped, appended, prefer={"ports": "theirs"})
    assert r.clean and pins(r.data) == ["A", "Y", "C"]
    r = merge(INV, appended, swapped)
    assert ids(r) == ["ports"] and pins(r.data) == ["A", "Y", "C"]

    # one side reordering the pins while the other reorders other instances: the pin order of the side that changed it
    m1 = INV[INV.index("C {sky130_fd_pr/pfet_01v8.sym}"):INV.index("C {sky130_fd_pr/nfet_01v8.sym}")]
    devices = edit(edit(INV, m1, ""), "C {devices/ipin.sym}", m1 + "C {devices/ipin.sym}")   # M2 before M1
    r = merge(INV, devices, swapped)
    assert r.clean and pins(r.data) == ["Y", "A"] and list(comps(r.data))[:2] == ["M2", "M1"]
    assert r.combined == ["port order (the subcircuit's pin order)"]
    r = merge(INV, swapped, devices)
    assert r.clean and pins(r.data) == ["Y", "A"]

    # a port renamed or relabelled is no reordering; the same reordering on both sides is one
    relabelled = edit(edit(INV, "name=p1 lab=A", "name=pa lab=IN"), "name=M2\nL=0.15", "name=M2\nL=0.18")
    r = merge(INV, swapped, relabelled)
    assert r.clean and pins(r.data) == ["Y", "IN"] and comps(r.data)["M2"].props["L"] == "0.18"
    r = merge(INV, swapped, swapped)
    assert r.clean and r.data == swapped.encode()


def test_a_wire_rerouted_on_both_sides_is_a_conflict():
    y = "N 0 0 100 0 {lab=Y}"
    yid = "wire:0,0,100,0 lab=Y"
    stretched = edit(INV, y, "N 0 0 140 0 {lab=Y}")                            # its end moved right
    rerouted = edit(INV, y, "N 0 0 0 40 {lab=Y}")                               # its end moved down
    deleted = edit(INV, y + "\n", "")

    def wires(data: bytes) -> set[str]:
        return {xschem.wire_id(*w[:4]) for w in xschem.parse(data.decode()).wires}

    # one region (the new wires touch the old one's end): never both, never net Y reaching both ends
    r = merge(INV, stretched, rerouted)
    assert ids(r) == [yid] and not r.clean
    c = r.conflicts[0]
    assert c.what.startswith("wires: both sides changed wire 0,0,100,0 lab=Y")
    assert c.base[0] == "0,0,100,0 lab=Y"
    assert c.ours["added"] == ["0,0,140,0 lab=Y"] and c.theirs["added"] == ["0,0,0,40 lab=Y"]
    assert "0,0,140,0" in wires(r.data) and "0,0,0,40" not in wires(r.data) and r.combined == []
    r = merge(INV, stretched, rerouted, prefer={yid: "theirs"})
    assert r.clean and r.data == rerouted.encode()

    # removed on one side and rerouted on the other: the deletion is not lost
    r = merge(INV, deleted, rerouted)
    assert ids(r) == [yid] and "ours removed, theirs changed" in r.conflicts[0].what
    assert r.data == deleted.encode()
    r = merge(INV, deleted, rerouted, prefer={yid: "theirs"})
    assert r.clean and r.data == rerouted.encode()
    r = merge(INV, rerouted, deleted)
    assert ids(r) == [yid] and r.data == rerouted.encode()

    # moved by both sides differently (copies, nothing shared): each copy goes with the wire it moved
    vss = "N 0 90 0 120 {lab=VSS}"
    r = merge(INV, edit(INV, vss, "N 10 90 10 120 {lab=VSS}"), edit(INV, vss, "N 20 90 20 120 {lab=GND}"))
    assert ids(r) == ["wire:0,90,0,120 lab=VSS"] and "20,90,20,120" not in wires(r.data)

    # rerouted on one side only, the same way on both, or removed on both: no conflict
    for ours, theirs, want in ((INV, rerouted, rerouted), (rerouted, INV, rerouted), (rerouted, rerouted, rerouted),
                               (deleted, deleted, deleted)):
        r = merge(INV, ours, theirs)
        assert r.clean and r.conflicts == [] and r.data == want.encode()
    # rerouted the same way but labelled differently: one region, a conflict
    r = merge(INV, rerouted, edit(rerouted, "N 0 0 0 40 {lab=Y}", "N 0 0 0 40 {lab=OUT}"))
    assert ids(r) == [yid] and r.data == rerouted.encode()
    # a rerouted wire and a wire added elsewhere combine
    theirs = edit(INV, "N -100 0 -60 0 {lab=A}\n", "N -100 0 -60 0 {lab=A}\nN -140 0 -100 0 {lab=A}\n")
    r = merge(INV, stretched, theirs)
    assert r.clean and {"0,0,140,0", "-140,0,-100,0"} <= wires(r.data) and "0,0,100,0" not in wires(r.data)


def wire_labels(data: bytes) -> dict[str, str]:
    return {xschem.wire_id(*w[:4]): w[4].get("lab") for w in xschem.parse(data.decode()).wires}


def test_a_wire_relabelled_on_one_side_and_rerouted_on_the_other_is_one_decision():
    y = "N 0 0 100 0 {lab=Y}"
    yid = "wire:0,0,100,0 lab=Y"
    rerouted = edit(INV, y, "N 0 0 0 40 {lab=Y}")
    relabelled = edit(INV, y, "N 0 0 100 0 {lab=OUT}")
    r = merge(INV, relabelled, rerouted)
    assert ids(r) == [yid] and r.data == relabelled.encode() and r.combined == []
    c = r.conflicts[0]
    assert c.ours["added"] == ["0,0,100,0 lab=OUT"] and c.theirs["added"] == ["0,0,0,40 lab=Y"]
    r = merge(INV, relabelled, rerouted, prefer={yid: "theirs"})
    assert r.clean and r.data == rerouted.encode()
    r = merge(INV, rerouted, relabelled)
    assert ids(r) == [yid] and r.data == rerouted.encode()
    r = merge(INV, rerouted, relabelled, prefer={yid: "theirs"})
    assert r.clean and r.data == relabelled.encode()
    # kept unchanged on one side: the reroute is taken, no conflict
    r = merge(INV, INV, rerouted)
    assert r.clean and r.data == rerouted.encode()
    # removed (not rerouted) on one side: the same conflict, nothing in its place on that side
    r = merge(INV, edit(INV, y + "\n", ""), relabelled)
    assert ids(r) == [yid] and r.conflicts[0].ours["added"] == [] and "0,0,100,0" not in wire_labels(r.data)
    # an unlabelled L-route: ours labels one leg, theirs reroutes both legs; taking ours keeps the whole old route
    base = edit(INV, "N 0 0 100 0 {lab=Y}\n", "N 0 0 100 0 {}\nN 100 0 100 50 {}\n")
    ours = edit(base, "N 0 0 100 0 {}", "N 0 0 100 0 {lab=OUT}")
    theirs = edit(base, "N 0 0 100 0 {}\nN 100 0 100 50 {}", "N 0 0 0 50 {}\nN 0 50 100 50 {}")
    r = merge(base, ours, theirs)
    assert len(r.conflicts) == 1 and r.data == ours.encode()
    assert merge(base, ours, theirs, prefer={r.conflicts[0].id: "theirs"}).data == theirs.encode()


def test_a_wire_removed_and_drawn_again_elsewhere_is_a_move():
    # the same direction and length drawn by the side that removed a wire is that wire moved, however far: against
    # the other side's removal it is a conflict, which keeps ours by default
    vss = "N 0 90 0 120 {lab=VSS}\n"
    removed = edit(INV, vss, "")
    far = edit(INV, vss, "N 200 90 200 120 {lab=VSS}\n")
    r = merge(INV, far, removed)
    assert ids(r) == ["wire:0,90,0,120 lab=VSS"] and r.data == far.encode()
    # a wire of another direction or length drawn elsewhere is another wire
    r = merge(INV, edit(INV, vss, "N 200 90 260 90 {lab=VSS}\n"), removed)
    assert r.clean and "200,90,260,90" in wire_labels(r.data)
    # removed on both sides, ours also drawing a wire of that length elsewhere: taken for its move, reported
    r = merge(INV, edit(removed, "N 0 0 100 0 {lab=Y}", "N 0 0 100 0 {lab=Y}\nN 300 0 300 30 {lab=Z}"), removed)
    assert ids(r) == ["wire:0,90,0,120 lab=VSS"]                 # taken for its move: reported, never silent


def test_many_shifted_parallel_wires_merge_in_seconds():
    # 4000 wires over one x range, all shifted by ours: which wires touch is found on a grid, not wire by wire
    n = 4000

    def sch(dy: int, extra: str = "") -> str:
        head = INV[:INV.index("T {CMOS")]
        return head + "".join(f"N 0 {i * 20 + dy} 100 {i * 20 + dy} {{lab=n{i}}}\n" for i in range(n)) + extra
    base, ours = sch(0), sch(5)
    t0 = time.monotonic()
    r = merge(base, ours, sch(0, "N -500 -500 -400 -500 {lab=x}\n"))             # a wire elsewhere: combined
    assert r.clean and len(xschem.parse(r.data.decode()).wires) == n + 1
    assert wire_labels(r.data)["-500,-500,-400,-500"] == "x" and wire_labels(r.data)["0,5,100,5"] == "n0"
    r = merge(base, ours, sch(0, f"N 50 -10 50 {n * 20} {{lab=v}}\n"))           # a wire across all: one region
    assert ids(r) == ["wire:0,0,100,0 lab=n0"] and r.data == ours.encode()
    assert r.conflicts[0].what.endswith(f"(and {2 * n} more wires in its region)")
    assert time.monotonic() - t0 < 5
    base = INV + "C {devices/lab_pin.sym} 100 60 0 0 {sig_type=std_logic lab=VSS}\n"
    ours = base + "C {devices/lab_pin.sym} 140 60 0 0 {sig_type=std_logic lab=VSS}\n"
    theirs = base + "C {devices/lab_pin.sym} 180 60 0 0 {sig_type=std_logic lab=VSS}\n"
    r = merge(base, ours, theirs)
    assert r.clean
    xs = sorted(c.x for c in xschem.parse(r.data.decode()).comps if not c.props.get("name"))
    assert xs == [100, 140, 180]
    r = merge(base, ours, ours)                                                 # the same addition: once
    assert r.clean and r.data == ours.encode()

    # both sides change one unnamed label: one instance changed twice, not two labels
    vpwr = edit(base, "100 60 0 0 {sig_type=std_logic lab=VSS}", "100 60 0 0 {sig_type=std_logic lab=VGND}")
    vgnd = edit(base, "100 60 0 0 {sig_type=std_logic lab=VSS}", "100 60 0 0 {sig_type=std_logic lab=GND}")
    r = merge(base, vpwr, vgnd)
    assert len(r.conflicts) == 1 and r.conflicts[0].id.startswith("inst:@devices/lab_pin.sym@100,60,0,0#")
    labels = [c.props["lab"] for c in xschem.parse(r.data.decode()).comps if not c.props.get("name")]
    assert labels == ["VGND"]
    r = merge(base, vpwr, vgnd, prefer={r.conflicts[0].id: "theirs"})
    assert r.clean and r.data == vgnd.encode()
    r = merge(base, base, vgnd)                                                 # changed on one side only
    assert r.clean and r.data == vgnd.encode()


def test_instances_placed_at_one_place_on_both_sides_warn():
    ours = INV + "C {sky130_fd_pr/cap_mim_m3_1.sym} 200 0 0 0 {name=C1 model=cap_mim_m3_1 W=1 L=1 MF=1}\n"
    theirs = INV + "C {sky130_fd_pr/cap_mim_m3_1.sym} 200 0 0 0 {name=C2 model=cap_mim_m3_1 W=2 L=2 MF=1}\n"
    r = merge(INV, ours, theirs)
    assert r.clean and {"C1", "C2"} <= set(comps(r.data))
    assert r.warnings == ["instances of sky130_fd_pr/cap_mim_m3_1.sym placed at 200,0 on both sides: ours C1, "
                          "theirs C2: check they are not one device placed twice"]

    # moved there on one side, added there on the other: the same
    moved = edit(INV, "{sky130_fd_pr/nfet_01v8.sym} -20 60 0 0", "{sky130_fd_pr/nfet_01v8.sym} 200 60 0 0")
    r = merge(INV, moved, INV + nfet3(x=200, w=1))
    assert r.clean and (comps(r.data)["M2"].x, comps(r.data)["M3"].x) == (200, 200)
    assert r.warnings == ["instances of sky130_fd_pr/nfet_01v8.sym placed at 200,60 on both sides: ours M2, "
                          "theirs M3: check they are not one device placed twice"]
    r = merge(INV, INV + nfet3(x=200, w=1), moved)
    assert r.warnings == ["instances of sky130_fd_pr/nfet_01v8.sym placed at 200,60 on both sides: ours M3, "
                          "theirs M2: check they are not one device placed twice"]

    # two symbols at one position, or one move both sides made: no device twice
    res = INV + "C {sky130_fd_pr/res_xhigh_po.sym} 200 60 0 0 {name=R1\nL=1\nmodel=res_xhigh_po\n}\n"
    r = merge(INV, res, INV + nfet3(x=200, w=1))
    assert r.clean and r.warnings == []
    r = merge(INV, moved, edit(moved, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=4"))
    assert r.clean and r.warnings == []


def test_texts_merge_by_region():
    title = "T {CMOS inverter} -80 -200 0 0 0.4 0.4 {}"
    tid = "text:{CMOS inverter} -80 -200 0 0 0.4 0.4 {}"
    ours = edit(INV, title, title + "\nT {W/L sized for 1:2} -80 -170 0 0 0.2 0.2 {}")
    theirs = edit(INV, title, "T {CMOS inverter} -80 -200 0 0 0.5 0.5 {layer=8}")
    r = merge(INV, ours, theirs)                                                # different texts: combined
    assert r.clean and r.combined == ["texts: texts from theirs, 1 added, 1 removed"]
    t = {x.s: x for x in xschem.parse(r.data.decode()).texts}
    assert (t["CMOS inverter"].xs, t["CMOS inverter"].props) == (0.5, {"layer": "8"}) and "W/L sized for 1:2" in t

    # one text changed on both sides (in any of its fields): one conflict, never two versions of it
    for o, t in ((title.replace("0.4 0.4", "0.3 0.4"), title.replace("0.4 0.4", "0.5 0.4")),
                 (title.replace("0.4 0.4", "0.3 0.3"), title.replace("{}", "{layer=8}"))):
        r = merge(INV, edit(INV, title, o), edit(INV, title, t))
        assert ids(r) == [tid] and "texts: both sides changed text" in r.conflicts[0].what
        assert [x.s for x in xschem.parse(r.data.decode()).texts].count("CMOS inverter") == 1
        assert r.data == edit(INV, title, o).encode()
        assert merge(INV, edit(INV, title, o), edit(INV, title, t), prefer={tid: "theirs"}).data == \
            edit(INV, title, t).encode()

    # a symbol's text moved on one side and rewritten on the other (it was kept twice): one region, a conflict
    sym = "T {@symname} -45 -46 0 0 0.3 0.3 {}"
    moved = edit(INV_SYM, sym, "T {@symname} -45 -60 0 0 0.3 0.3 {}")
    rewritten = edit(INV_SYM, sym, "T {@symname @model} -45 -46 0 0 0.3 0.3 {}")
    r = merge(INV_SYM, moved, rewritten, kind="symbol")
    assert ids(r) == ["text:{@symname} -45 -46 0 0 0.3 0.3 {}"] and r.data == moved.encode()
    c = r.conflicts[0]
    assert c.ours["added"] == ["{@symname} -45 -60 0 0 0.3 0.3 {}"]
    assert c.theirs["added"] == ["{@symname @model} -45 -46 0 0 0.3 0.3 {}"]
    r = merge(INV_SYM, moved, rewritten, kind="symbol", prefer={c.id: "theirs"})
    assert r.clean and r.data == rewritten.encode()
    # moved far on one side and removed on the other: the deletion is not lost
    far = edit(INV_SYM, sym, "T {@symname} 300 300 0 0 0.3 0.3 {}")
    r = merge(INV_SYM, edit(INV_SYM, sym + "\n", ""), far, kind="symbol")
    assert ids(r) == [c.id] and "ours removed, theirs changed" in r.conflicts[0].what
    assert "@symname" not in [x.s for x in xschem.parse(r.data.decode()).texts]
    # moved on one side only: taken
    r = merge(INV_SYM, INV_SYM, far, kind="symbol")
    assert r.clean and r.data == far.encode()


def test_embedded_symbol_changed_on_both_sides_is_a_conflict():
    emb = ("C {inv.sym} 300 0 0 0 {name=x1} [\n" + INV_SYM.split("}\n", 1)[1].replace("V {}\nS {}\nE {}\n", "")
           + "]\n")
    base = INV + emb
    ours = edit(base, "T {A} -35 -6 0 0 0.2 0.2 {}", "T {IN} -35 -6 0 0 0.2 0.2 {}")
    theirs = edit(base, "T {A} -35 -6 0 0 0.2 0.2 {}", "T {in} -35 -6 0 0 0.2 0.2 {}")
    assert comps(base.encode())["x1"].embedded is not None
    r = merge(base, ours, theirs)
    assert ids(r) == ["inst:x1:embedded"] and r.data == ours.encode()
    r = merge(base, ours, edit(base, "{name=x1}", "{name=x1 net=n1}"))
    assert r.clean and comps(r.data)["x1"].props["net"] == "n1"
    assert [t.s for t in comps(r.data)["x1"].embedded.texts if t.s in ("A", "IN")] == ["IN"]


def test_a_canonical_schematic_stays_canonical():
    base = xschem.dump(xschem.canonical(xschem.parse(INV)))
    ours = xschem.dump(xschem.canonical(xschem.parse(base + "N -200 0 -140 0 {lab=A}\n")))
    theirs = xschem.dump(xschem.canonical(xschem.parse(base + "N 0 -200 0 -150 {lab=VDD}\n")))
    r = merge(base, ours, theirs)
    f = xschem.parse(r.data.decode())
    assert r.clean and len(f.wires) == 8
    assert r.data.decode() == xschem.dump(xschem.canonical(xschem.parse(r.data.decode())))


def test_conflict_ids_are_stable():
    ours = edit(edit(INV, "-20 60 0 0", "-20 80 0 0"), "N 0 90 0 120 {lab=VSS}\n", "")
    theirs = edit(edit(INV, "-20 60 0 0", "20 60 0 0"), "N 0 90 0 120 {lab=VSS}", "N 0 90 0 120 {lab=GND}")
    a, b = merge(INV, ours, theirs), merge(INV, ours, theirs)
    assert ids(a) == ids(b) == ["inst:M2:placement", "wire:0,90,0,120 lab=VSS"]
    assert a.data == b.data


def test_diff_of_a_moved_instance_and_a_changed_prop():
    new = edit(INV, "{sky130_fd_pr/nfet_01v8.sym} -20 60 0 0", "{sky130_fd_pr/nfet_01v8.sym} -20 80 1 0")
    new = edit(new, "name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=4")
    d = schmerge.diff(INV.encode(), new.encode(), kind="schematic")
    assert d == {"kind": "schematic", "changes": [
        {"do": "mod", "id": "inst:M1:prop:W", "what": "instance M1: W", "before": "2", "after": "4"},
        {"do": "mod", "id": "inst:M2:placement", "what": "instance M2: placement",
         "before": {"x": -20.0, "y": 60.0, "rot": 0, "flip": 0}, "after": {"x": -20.0, "y": 80.0, "rot": 1, "flip": 0}},
    ]}
    # reordering (but of the ports) and formatting are no change
    shuffled = xschem.parse(INV)
    shuffled.comps.reverse()
    shuffled.wires = [(w[2], w[3], w[0], w[1], w[4]) for w in reversed(shuffled.wires)]
    assert [c.props["name"] for c in shuffled.comps] == ["l2", "l1", "p2", "p1", "M2", "M1"]
    ch = schmerge.diff(INV.encode(), xschem.dump(shuffled).encode(), path="libs/l/inv.sch")["changes"]
    assert ch == [{"do": "mod", "id": "ports", "what": "port order (the subcircuit's pin order)",
                   "before": ["p1", "p2"], "after": ["p2", "p1"]}]
    shuffled.comps[2:4] = shuffled.comps[3:1:-1]                                # the pins back in their order
    assert schmerge.diff(INV.encode(), xschem.dump(shuffled).encode(), path="libs/l/inv.sch")["changes"] == []


def test_diff_names_renames_additions_and_pins():
    new = edit(INV, "name=M1\n", "name=MP\n") + nfet3(x=100, w=1)
    new = edit(new, "N 0 90 0 120 {lab=VSS}\n", "")
    ch = schmerge.diff(INV.encode(), new.encode(), kind="schematic")["changes"]
    assert [(c["do"], c["id"]) for c in ch] == [("mod", "rename:M1"), ("add", "inst:M3"), ("del", "wire:0,90,0,120")]
    assert ch[0]["before"] == "M1" and ch[0]["after"] == "MP" and ch[1]["after"]["props"]["W"] == "1"

    sym = edit(INV_SYM, "B 5 57.5", "B 5 -62.5 17.5 -57.5 22.5 {name=B dir=in}\nB 5 57.5")
    ch = schmerge.diff(INV_SYM.encode(), sym.encode(), path="inv.sym")["changes"]
    assert [c["id"] for c in ch] == ["drawing:boxes"] and ch[0]["what"].endswith("pins A, Y -> A, B, Y")

    added = schmerge.diff(None, INV.encode(), kind="schematic")["changes"]
    assert {c["do"] for c in added} == {"add"} and sum(c["id"].startswith("inst:") for c in added) == 6


def test_a_text_moved_and_restyled_against_a_rewrite_or_a_deletion_is_one_conflict():
    title = "T {CMOS inverter} -80 -200 0 0 0.4 0.4 {}"
    moved = edit(INV, title, "T {CMOS inverter} -80 -260 0 0 0.6 0.6 {}")          # moved and enlarged
    for theirs in (edit(INV, title + "\n", ""), edit(INV, title, "T {CMOS inverter (sky130)} -60 -200 0 0 0.4 0.4 {}")):
        r = merge(INV, moved, theirs)
        assert ids(r) == ["text:{CMOS inverter} -80 -200 0 0 0.4 0.4 {}"]
        assert [x.s for x in xschem.parse(r.data.decode()).texts if "CMOS" in x.s] == ["CMOS inverter"]   # once
