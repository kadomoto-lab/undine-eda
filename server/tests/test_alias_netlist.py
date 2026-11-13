"""A local alias resolves end to end (docs/design/packages.md, Resolution 5): analog_ip's buf places `bias/inv.sym`,
bias being its local name for the package bias_gen, whose own inv places `bias_gen/r.sym`. lib add installs bias_gen
under both names, so a project testbench placing analog_ip/buf.sym (and bias_gen/inv.sym) netlists with Undine's
netlister and with Xschem's, on the project's one symbol path, with one `.subckt inv` and no missing symbol."""
import copy
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402
from undine.schematics import schematic, xschem  # noqa: E402
from undine.store import deps  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
V = "v {xschem version=3.4.5 file_version=1.2}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
R_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=resistor\nformat="@name @pinlist @value"\n'
         'template="name=R1 value=1k"}\nL 4 0 -30 0 30 {}\nB 5 -2.5 -32.5 2.5 -27.5 {name=P dir=inout}\n'
         'B 5 -2.5 27.5 2.5 32.5 {name=M dir=inout}\n')
# a two-pin subcircuit symbol: `a` at the left, `y` at the right
INV_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=subcircuit\nformat="@name @pinlist @symname"\n'
           'template="name=x1"}\nB 5 -22.5 -2.5 -17.5 2.5 {name=a dir=in}\nB 5 17.5 -2.5 22.5 2.5 {name=y dir=out}\n')
INV_SCH = V + "\n".join([
    "C {devices/iopin.sym} -200 0 0 0 {name=p1 lab=a}", "C {devices/iopin.sym} -200 40 0 0 {name=p2 lab=y}",
    "C {bias_gen/r.sym} 0 0 0 0 {name=R1 value=1k}",                  # its own cell, by its [package] name
    "C {devices/lab_pin.sym} 0 -30 0 0 {name=l1 lab=a}", "C {devices/lab_pin.sym} 0 30 0 0 {name=l2 lab=y}"]) + "\n"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: None)
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)


def _devices() -> None:
    if tools.xschem_library() is None:
        pytest.skip("Xschem's devices library is not installed")


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(d: Path, files: dict[str, str], tag: str) -> Path:
    d.mkdir(parents=True)
    for rel, text in files.items():
        (d / rel).write_text(text)
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", tag)
    _git(d, "tag", tag)
    return d


def _places(cell: str, syms: list[str], ports: bool = False) -> str:
    """A schematic placing each symbol (an inv's pins) from `in` to o<k>; `ports`: with pins in and o0."""
    lines = ["C {devices/iopin.sym} -200 0 0 0 {name=p1 lab=in}",
             "C {devices/iopin.sym} -200 40 0 0 {name=p2 lab=o0}"] if ports else []
    for k, sym in enumerate(syms):
        x = 200 * k
        lines += [f"C {{{sym}}} {x} 0 0 0 {{name=x{k}}}",
                  f"C {{devices/lab_pin.sym}} {x - 20} 0 0 0 {{name=la{k} lab=in}}",
                  f"C {{devices/lab_pin.sym}} {x + 20} 0 0 0 {{name=ly{k} lab=o{k}}}"]
    return V + "\n".join(lines) + "\n"


def _project(tmp_path: Path):
    src = tmp_path / "src"
    bias = _repo(src / "bias_gen", {"undine-package.toml": '[package]\nname = "bias_gen"\nversion = "0.3.0"\n',
                                    "inv.sch": INV_SCH, "inv.sym": INV_SYM, "r.sym": R_SYM}, "v0.3.0")
    manifest = ('[package]\nname = "analog_ip"\nversion = "1.2.0"\n\n[dependencies]\n'
                f'bias = {{ git = "{bias}", rev = "v0.3.0", package = "bias_gen" }}\n')
    buf_sym = INV_SYM.replace("{name=a ", "{name=in ").replace("{name=y ", "{name=o0 ")
    ip = _repo(src / "analog_ip", {"undine-package.toml": manifest, "buf.sch": _places("buf", ["bias/inv.sym"], True),
                                   "buf.sym": buf_sym}, "v1.2.0")
    store = ProjectStore(tmp_path / "projects", {})
    prj = store.create("p")
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    deps.add(prj, "analog_ip", str(ip), "v1.2.0")
    prj = store.get("p")
    lib = prj.create_library("mylib")
    tb = lib.path / "tb.sch"
    tb.write_text(_places("tb", ["analog_ip/buf.sym"]))
    both = lib.path / "tb2.sch"
    both.write_text(_places("tb2", ["analog_ip/buf.sym", "bias_gen/inv.sym"]))
    return store.get("p"), tb, both


def _subckt(text: str, name: str) -> str:
    m = re.search(rf"(?ms)^\.subckt {name} .*?^\.ends", text, flags=re.IGNORECASE)
    assert m, text
    return m.group(0)


def test_a_local_alias_resolves_with_undines_netlister(tmp_path):
    _devices()
    prj, tb, both = _project(tmp_path)
    assert {n for n in ("analog_ip", "bias", "bias_gen") if deps.dep_dir(prj.path, n).is_dir()} == \
        {"analog_ip", "bias", "bias_gen"}
    res = xschem.SymbolResolver(prj.symbol_paths())
    for path in (tb, both):
        r = schematic.spice(path, res, top_subckt=False)
        assert not [w for w in r.warnings if "missing symbol" in w], r.warnings
        assert r.text.count(".subckt inv ") == 1 and r.text.count(".subckt buf ") == 1, r.text
        assert re.search(r"(?m)^x0 in o0 inv$", _subckt(r.text, "buf")), r.text       # buf's bias/inv
        assert re.search(r"(?m)^R1 a y 1k$", _subckt(r.text, "inv")), r.text           # inv's bias_gen/r


def test_a_local_alias_resolves_with_the_xschem_netlister(tmp_path):
    _devices()
    prj, tb, both = _project(tmp_path)
    if tools.find("xschem") is None:
        with pytest.raises(RuntimeError, match="xschem not found"):     # past the check, to the missing executable
            schematic.xschem_netlist(tb, prj.symbol_paths(), {})
        return
    for path in (tb, both):
        text = schematic.xschem_netlist(path, prj.symbol_paths(), {})
        assert text.lower().count(".subckt inv ") == 1 and text.lower().count(".subckt buf ") == 1, text
        assert re.search(r"(?mi)^x0 in o0 inv\b", _subckt(text, "buf")), text
        assert re.search(r"(?mi)^R1 a y 1k\b", _subckt(text, "inv")), text
