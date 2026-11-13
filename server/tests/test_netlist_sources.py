"""Netlist attribution (schematic.spice -> NetlistResult.sources / files) and owner naming: the trust gate checks every
group of lines by the cell body it is written into (home) and the symbol it was formed from, so a dependency's text
is found whichever of the two it came through."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402
from undine.schematics import netlist, schematic, xschem  # noqa: E402
from undine.store import cas  # noqa: E402

V = "v {xschem version=3.4.5 file_version=1.2}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
R_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=resistor\nformat="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
         'L 4 0 -30 0 30 {}\nB 5 -2.5 -32.5 2.5 -27.5 {name=P dir=inout}\nB 5 -2.5 27.5 2.5 32.5 {name=M dir=inout}\n')
# a two-pin subcircuit symbol: a at the left, y at the right
SUB_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=subcircuit\nformat="@name @pinlist @symname"\n'
           'template="name=x1"}\nB 5 -22.5 -2.5 -17.5 2.5 {name=a dir=in}\nB 5 17.5 -2.5 22.5 2.5 {name=y dir=out}\n')
# a PDK-style corner block: its lines are built in Tcl, which Undine replaces by the model library's section
CORNER_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=netlist_commands\n'
              'format="tcleval([corner_lines @corner])"\ntemplate="name=c1 corner=tt"}\n')
CONTROL = ".control\nshell echo hi\n.endc"
ENV = {"UNDINE_MODEL_LIB": "/pdk/models.lib", "UNDINE_CORNERS": "tt ss"}


def _devices() -> list[Path]:
    xl = tools.xschem_library()
    if xl is None:
        pytest.skip("Xschem's devices library is not installed")
    return [xl, xl / "devices"]


def _cell(d: Path, cell: str, comps: list[str], ports: bool = True) -> Path:
    """Schematic `cell` in `d` (with pins a and y), its subcircuit symbol beside it, placing `comps` (C records)."""
    d.mkdir(parents=True, exist_ok=True)
    pins = ["C {devices/iopin.sym} -200 0 0 0 {name=p1 lab=a}", "C {devices/iopin.sym} -200 40 0 0 {name=p2 lab=y}"]
    (d / f"{cell}.sch").write_text(V + "\n".join([*(pins if ports else []), *comps]) + "\n")
    (d / f"{cell}.sym").write_text(SUB_SYM)
    return d / f"{cell}.sch"


def _r(sym: str, name: str, value: str | None = None, x: int = 0) -> list[str]:
    """A two-pin instance of `sym` from a to y."""
    val = f' value="{value}"' if value is not None else ""
    return [f"C {{{sym}}} {x} 0 0 0 {{name={name}{val}}}", f"C {{devices/lab_pin.sym}} {x} -30 0 0 {{name=l{name}a lab=a}}",
            f"C {{devices/lab_pin.sym}} {x} 30 0 0 {{name=l{name}y lab=y}}"]


def _sub(sym: str, name: str, x: int = 0) -> list[str]:
    return [f"C {{{sym}}} {x} 0 0 0 {{name={name}}}", f"C {{devices/lab_pin.sym}} {x - 20} 0 0 0 {{name=l{name}a lab=a}}",
            f"C {{devices/lab_pin.sym}} {x + 20} 0 0 0 {{name=l{name}y lab=y}}"]


def _code(name: str, value: str | None = None, place: str = "", sym: str = "devices/code_shown.sym", y: int = 300) -> str:
    props = f"name={name}" + (f' value="{value}"' if value is not None else "") + (f" place={place}" if place else "")
    return f"C {{{sym}}} 0 {y} 0 0 {{{props}}}"


def _marker(d: Path, **info) -> None:
    (d / cas.MARKER).write_text(json.dumps({"git": "https://example.invalid/ip.git", "rev": "v1.2.0",
                                            "commit": "229914698b1f" + "0" * 28, **info}))


def _of(r: schematic.NetlistResult, **match) -> list[schematic.Source]:
    return [s for s in r.sources if all(getattr(s, k) == v for k, v in match.items())]


def test_code_blocks_are_attributed_to_their_cell_body_with_the_exact_text(tmp_path):
    """A code block in a child cell is a Source of kind 'code' whose home is that cell's schematic and whose text is
    what the netlist holds; the top's header blocks are 'header'; the corner .lib Undine writes is 'generated', with
    neither home nor symbol; a child's header block is not written, so it is not a Source either."""
    lib = tmp_path / "l"
    lib.mkdir()
    (lib / "r.sym").write_text(R_SYM)
    (lib / "corner.sym").write_text(CORNER_SYM)
    sub = _cell(lib, "sub", [*_r("r.sym", "R1", "2k"), _code("s1", ".option   savecurrents  \n\n.param a=$ROOT/x"),
                             _code("s2", ".param hidden=1", place="header", y=400), _code("s3", y=500)])
    tb = _cell(lib, "tb", [*_sub("sub.sym", "x1"), _code("h1", ".param top=1", place="header"),
                           _code("c1", sym="corner.sym", y=400)], ports=False)
    res = xschem.SymbolResolver([lib, *_devices()])
    r = schematic.spice(tb, res, env={**ENV, "ROOT": "/r"}, top_subckt=False)

    s1, = _of(r, name="s1")
    assert (s1.home, s1.symbol, s1.cell, s1.kind) == (sub.resolve(), None, "sub", "code")
    assert s1.text == ".option   savecurrents\n.param a=/r/x" and f"\n{s1.text}\n" in r.text
    # a code block without a value of its own writes the symbol's template default: the symbol formed it
    s3, = _of(r, name="s3")
    assert s3.text == "blabla" and s3.symbol == (_devices()[1] / "code_shown.sym").resolve() and s3.home == sub.resolve()
    h1, = _of(r, name="h1")
    assert (h1.home, h1.cell, h1.kind, h1.text) == (tb.resolve(), "tb", "header", ".param top=1")
    assert r.text.index(".param top=1") < r.text.index(".subckt sub")
    gen, = _of(r, kind="generated")
    assert (gen.home, gen.symbol, gen.cell, gen.text) == (None, None, "tb", ".lib /pdk/models.lib tt")
    assert gen.text in r.text
    assert ".param hidden=1" not in r.text and not _of(r, name="s2")
    for s in r.sources:                                  # every Source is text of the netlist
        assert s.text and s.text in r.text, s


def test_device_lines_name_the_symbol_they_were_formed_from(tmp_path):
    """A device line from a symbol of another directory names that .sym; one from the cell's own directory is recorded
    too, with home and symbol both set. files holds every schematic, netlist view and symbol read."""
    lib = tmp_path / "l"
    lib.mkdir()
    (lib / "r.sym").write_text(R_SYM)
    sub = _cell(lib, "sub", _r("r.sym", "R1", "2k"))
    netlist.sync(sub, xschem.SymbolResolver([lib, *_devices()]))     # sub has a netlist view on disk
    tb = _cell(lib, "tb", [*_sub("sub.sym", "x1"), *_r("devices/res.sym", "R5", "5k", x=300)], ports=False)
    res = xschem.SymbolResolver([lib, *_devices()])
    r = schematic.spice(tb, res, top_subckt=False)
    devres = (_devices()[0] / "devices" / "res.sym").resolve()

    r5, = _of(r, name="R5")
    assert (r5.home, r5.symbol, r5.cell, r5.kind) == (tb.resolve(), devres, "tb", "device")
    assert r5.text.startswith("R5 a y 5k") and r5.text in r.text
    r1, = _of(r, name="R1")
    assert (r1.home, r1.symbol, r1.cell, r1.text) == (sub.resolve(), (lib / "r.sym").resolve(), "sub", "R1 a y 2k")
    x1, = _of(r, name="x1")
    assert (x1.home, x1.symbol, x1.text) == (tb.resolve(), (lib / "sub.sym").resolve(), "x1 a y sub")
    assert {tb.resolve(), sub.resolve(), netlist.path_of(sub).resolve(), devres, (lib / "r.sym").resolve(),
            (lib / "sub.sym").resolve()} <= r.files
    assert all(p.is_absolute() for p in r.files)


def test_a_trusted_symbol_placed_in_a_dependency_cell_is_attributed_to_that_cell(tmp_path):
    """devices/res.sym (Xschem's, trusted) placed in a dependency's schematic with a value that carries a .control
    block: the line's home is the dependency's schematic, so the gate finds it through home; a dependency's symbol
    placed in a project cell is found through symbol."""
    prj = tmp_path / "p"
    deps = prj / ".undine" / "deps"
    ip = deps / "ip"
    div = _cell(ip, "div", _r("devices/res.sym", "R1", "1k\n" + CONTROL))
    _marker(ip, name="ip", package="analog_ip", version="1.2.0")
    (ip / "bad.sym").write_text(R_SYM.replace('"@name @pinlist @value"', '"@name @pinlist @value\n.control\nshell id\n.endc"'))
    tb = _cell(prj / "lib", "tb", [*_sub("ip/div.sym", "x1"), *_r("ip/bad.sym", "R7", x=300)], ports=False)
    res = xschem.SymbolResolver([prj / "lib", deps, *_devices()])
    r = schematic.spice(tb, res, top_subckt=False)

    r1, = _of(r, name="R1")
    assert r1.home == div.resolve() and r1.cell == "div" and r1.kind == "device"
    assert r1.symbol == (_devices()[0] / "devices" / "res.sym").resolve()
    assert ".control" in r1.text and "shell echo hi" in r1.text and r1.text in r.text
    r7, = _of(r, name="R7")
    assert r7.home == tb.resolve() and r7.symbol == (ip / "bad.sym").resolve() and "shell id" in r7.text
    assert schematic.owner(r1.home) == "analog_ip 1.2.0 (ip)"


def test_stand_in_lines_name_the_stand_in_symbol(tmp_path):
    """A cell netlisted as its stand-in: the line is formed from the stand-in's symbol, its body is the stand-in's."""
    lib = tmp_path / "l"
    lib.mkdir()
    (lib / "r.sym").write_text(R_SYM)
    _cell(lib, "sub", _r("r.sym", "R1", "2k"))
    model = _cell(lib, "model", _r("r.sym", "R2", "3k"))
    tb = _cell(lib, "tb", _sub("sub.sym", "x1"), ports=False)
    res = xschem.SymbolResolver([lib, *_devices()])
    st = schematic.Standin("l/sub", "l/model", "model", res.load(str(lib / "model.sym"), None), model, model)
    r = schematic.spice(tb, res, top_subckt=False, substitute={lib / "sub.sym": st})
    x1, = _of(r, name="x1")
    assert x1.symbol == (lib / "model.sym").resolve() and x1.text == "x1 a y model"
    r2, = _of(r, name="R2")
    assert r2.home == model.resolve() and not _of(r, name="R1")


def test_owner_names_packages_plain_dependencies_and_directories(tmp_path):
    ip, plain, lib = tmp_path / "ip", tmp_path / "plain", tmp_path / "mylib"
    for d in (ip, plain, lib):
        d.mkdir()
    _marker(ip, name="ip", package="analog_ip", version="1.2.0")
    _marker(plain)
    assert schematic.owner(ip / "inv.sch") == "analog_ip 1.2.0 (ip)"
    assert schematic.owner(plain / "inv.sch") == "plain (git 229914698b1f)"
    assert schematic.owner(lib / "inv.sch") == "mylib"
    alias = tmp_path / "alias"                              # a package installed under another local name too
    alias.mkdir()
    _marker(alias, name="alias", package="analog_ip", version="1.2.0", direct=False, via=["sar"])
    assert schematic.owner(alias / "inv.sch") == "analog_ip 1.2.0 (alias)"
    (lib / cas.MARKER).write_text("not json")                 # a broken marker is no marker: messages never fail
    assert schematic.owner(lib / "inv.sch") == "mylib"


def test_same_name_error_names_installed_sides_by_owner(tmp_path):
    """Two cells named inv of other content: a side installed with a marker is '<cell> in <owner>', a plain directory
    keeps 'lib/cell'."""
    prj = tmp_path / "p"
    deps = prj / ".undine" / "deps"
    for d, v in ((prj / "a", "1k"), (deps / "ip", "2k"), (deps / "old", "3k")):
        d.mkdir(parents=True)
        (d / "r.sym").write_text(R_SYM)
        _cell(d, "inv", _r("r.sym", "R1", v))
    _marker(deps / "ip", name="ip", package="analog_ip", version="1.2.0")
    _marker(deps / "old", name="old")
    res = xschem.SymbolResolver([prj, deps, *_devices()])
    tb = _cell(prj / "a", "tb", [*_sub("a/inv.sym", "x0"), *_sub("ip/inv.sym", "x1", x=200)], ports=False)
    with pytest.raises(schematic.SameNameError,
                       match=r"tb: two cells named inv are netlisted, a/inv and inv in analog_ip 1\.2\.0 \(ip\) "
                             r"\(instance x1 in tb\), and they differ"):
        schematic.spice(tb, res, top_subckt=False)
    tb = _cell(prj / "a", "tb2", [*_sub("old/inv.sym", "x0"), *_sub("ip/inv.sym", "x1", x=200)], ports=False)
    with pytest.raises(schematic.SameNameError,
                       match=r"two cells named inv are netlisted, inv in old \(git 229914698b1f\) and inv in "
                             r"analog_ip 1\.2\.0 \(ip\) \(instance x1 in tb2\)"):
        schematic.spice(tb, res, top_subckt=False)


def test_subckt_lines_are_sources_and_a_pin_name_with_a_line_break_is_refused(tmp_path):
    """The .subckt line is formed from the placed symbol's pin names: a Source of kind 'subckt' whose home is the
    child's schematic and whose symbol is that .sym (the top's from its own port labels). A pin name that holds a
    space or a line break would write cards of its own, so it is refused when the netlist is formed."""
    from undine.store import trust
    lib = tmp_path / "l"
    lib.mkdir()
    (lib / "r.sym").write_text(R_SYM)
    sub = _cell(lib, "sub", _r("r.sym", "R1", "2k"))
    tb = _cell(lib, "tb", _sub("sub.sym", "x1"))
    res = xschem.SymbolResolver([lib, *_devices()])
    r = schematic.spice(tb, res)
    head, = _of(r, kind="subckt", cell="sub")
    assert (head.home, head.symbol, head.text) == (sub.resolve(), (lib / "sub.sym").resolve(), ".subckt sub a y")
    top, = _of(r, kind="subckt", cell="tb")
    assert (top.home, top.symbol, top.text) == (tb.resolve(), None, ".subckt tb a y")
    assert head.text in r.text.splitlines() and top.text in r.text.splitlines()

    # the gate reads such a line like any other: one from an untrusted directory with cards in it is refused
    bad = schematic.NetlistResult("", [], [], [], sources=[
        schematic.Source(sub.resolve(), (lib / "sub.sym").resolve(), "sub", "subckt", "sub",
                         ".subckt sub a y\n.control\nshell x\n.endc")])
    with pytest.raises(trust.UntrustedContent, match=r"its \.subckt line of sub line 2: \.control|\.subckt line of sub"):
        trust.check_netlist(bad, untrusted={lib: ("l", "l@229914698b1f")}, pdk_root=None, env={}, rules="sim")
    bad.sources[0].text = ".subckt sub a y\n.include /etc/x"
    with pytest.raises(trust.UntrustedContent, match="/etc/x"):
        trust.check_netlist(bad, untrusted={lib: ("l", "l@229914698b1f")}, pdk_root=None, env={}, rules="paths")

    sym = (lib / "sub.sym").read_text()
    (lib / "sub.sym").write_text(sym.replace("{name=y dir=out}", '{name="y\n.control\nshell x\n.endc" dir=out}'))
    with pytest.raises(ValueError, match=r"pin name 'y\\n\.control.*holds a space or a line break"):
        schematic.spice(tb, xschem.SymbolResolver([lib, *_devices()]))
