"""Xschem property values that refer to other parameters and compute with expr(...), as the sky130 MOS symbols do
since open_pdks 2024: they are netlisted as numbers, or as {...} for the simulator when an operand is not a number."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest  # noqa: E402

from undine.schematics import schematic, xschem  # noqa: E402
from undine.schematics.schematic import _value  # noqa: E402

NFET = r'''v {xschem version=3.4.5 file_version=1.2}
K {type=nmos
format="@spiceprefix@name @pinlist sky130_fd_pr__@model L=@L W=@W
+ nf=@nf ad=@ad as=@as pd=@pd ps=@ps
+ nrd=@nrd nrs=@nrs m=@mult"
template="name=M1
W=1
L=0.15
nf=1
mult=1
ad=\\"expr('int((@nf + 1)/2) * @W / @nf * 0.29')\\"
pd=\\"expr('2*int((@nf + 1)/2) * (@W / @nf + 0.29)')\\"
as=\\"expr('int((@nf + 2)/2) * @W / @nf * 0.29')\\"
ps=\\"expr('2*int((@nf + 2)/2) * (@W / @nf + 0.29)')\\"
nrd=\\"expr('0.29 / @W ')\\" nrs=\\"expr('0.29 / @W ')\\"
model=nfet_01v8
spiceprefix=X
"}
B 5 17.5 -32.5 22.5 -27.5 {name=D dir=inout}
B 5 -22.5 -2.5 -17.5 2.5 {name=G dir=in}
B 5 17.5 27.5 22.5 32.5 {name=S dir=inout}
B 5 19.921875 -0.078125 20.078125 0.078125 {name=B dir=in}
'''   # the header of sky130_fd_pr/nfet_01v8.sym in open_pdks 2024+ (8afc834)


@pytest.mark.parametrize("value, props, want", [
    ("expr('int((@nf + 1)/2) * @W / @nf * 0.29')", {"W": "2", "nf": "1"}, "0.58"),
    ("expr('int((@nf + 1)/2) * @W / @nf * 0.29')", {"W": "8", "nf": "4"}, "1.16"),        # floating point, like Xschem
    ("expr('2*int((@nf + 2)/2) * (@W / @nf + 0.29)')", {"W": "2", "nf": "2"}, "5.16"),
    ("expr( @W * 2u )", {"W": "2"}, "4e-06"),                                             # SI suffixes
    ("expr(12u * 2)", {}, "2.4e-05"),
    ("expr('int((@nf + 1)/2) * @W')", {"W": "Wn", "nf": "1"}, "{int((1 + 1)/2) * Wn}"),   # a .param: the simulator's job
    ("expr(1/0)", {}, "{1/0}"),
    ("@W", {"W": "3"}, "3"),
    ("plain", {}, "plain"),
])
def test_values(value, props, want):
    assert _value(value, props, {}) == want


def test_a_new_sky130_style_mosfet_netlists_with_numbers(tmp_path):
    (tmp_path / "nfet.sym").write_text(NFET)
    sch = xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")
    sch.comps = [xschem.XComp("nfet.sym", 0, 0, 0, 0, {"name": "M1", "W": "2", "L": "1", "nf": "2"})]
    (tmp_path / "top.sch").write_text(xschem.dump(sch))
    text = schematic.spice(tmp_path / "top.sch", xschem.SymbolResolver([tmp_path])).text
    lines = text.splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("XM1"))
    line = " ".join([lines[i], *[l[1:] for l in lines[i + 1:] if l.startswith("+")][:3]]) + " "   # with its + lines
    assert "ad=0.29 " in line and "as=0.58 " in line and "nrd=0.145 " in line and "expr" not in line, line
