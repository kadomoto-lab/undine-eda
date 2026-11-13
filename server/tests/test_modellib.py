"""Fast loading of the PDK's model library (simulation/modellib.py): a section reduced to what the netlist can reach,
made once and shared; anything the reducer does not understand reads the library whole, and says why. The reduced copy
gives the same results to the last bit: on a small library here (with the netlist's includes as ngspice reads them,
and two sections that need each other), on GF180MCU's sections read together as its own test benches read them, and on
SKY130 for every device family it has, at tt and with mismatch (tt_mm, one seed); with UNDINE_SLOW_TESTS=1 also at
every other corner, its mismatch section, and at -40 / 125 C (some ten minutes: each run of the whole library takes
20 s or more)."""
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
import pytest  # noqa: E402

from undine import tools  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.simulation import modellib, sim  # noqa: E402

LIB = """* a small model library: sections that include files and a section of their own file
.lib tt
.param mc_mm_switch=0
.lib "lib.spice" common
.endl tt

.lib tt_mm
.param mc_mm_switch=1
.lib "lib.spice" common
.endl tt_mm

.lib common
.option scale=1u
.param global_draw = agauss(0, 1, 1)
.param vth_n = 0.5
+ kp_n = 200u
* p-channel
.param vth_p = -0.5 kp_p = '80u*1'
.param unused_big = {1 + 2}
.func twice(x) = 2*x
.include "models/nmos.spice"
.include "models/pmos.spice"
.model dio.0 d is=1e-14 area_min=0
.model dio.1 d is=2e-14
.model other d is=3e-14
.endl common
"""
NMOS = """* n-channel
.param nslope = 0.02
.subckt nfet d g s b w=1 l=1
.param vt = {vth_n + mc_mm_switch*agauss(0, 1, 1)*nslope/sqrt(w*l)}
M1 d g s b nmod w={w} l={l}
.model nmod nmos level=1 vto={vt} kp={kp_n}
.ends
"""
PMOS = """* p-channel
.param pslope = 0.02
.subckt pfet d g s b w=1 l=1
.param vt = {vth_p - mc_mm_switch*agauss(0, 1, 1)*pslope/sqrt(w*l)}
M1 d g s b pmod w={w} l={l}
.model pmod pmos level=1 vto={vt} kp={kp_p}
.ends
"""


def _lib(tmp_path, lib=LIB, nmos=NMOS) -> Path:
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "lib.spice").write_text(lib)
    (tmp_path / "models" / "nmos.spice").write_text(nmos)
    (tmp_path / "models" / "pmos.spice").write_text(PMOS)
    return tmp_path / "lib.spice"


def _tb(lib: Path, section="tt", body="X1 d g 0 0 nfet w=2 l=1\nX2 d g 0 0 nfet w=2 l=1\n") -> str:
    return (f"* tb\n.lib {lib} {section}\n.option seed=7\nV1 d 0 1.8\nV2 g 0 0.8\n{body}"
            ".control\nop\nwrite out.raw\nprint all\nquit\n.endc\n.end\n")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("UNDINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv(modellib.FULL_ENV, raising=False)
    return tmp_path / "cache" / "models"


def _copy(text: str) -> str:
    """The reduced copy a netlist reads."""
    path = next(ln.split()[1] for ln in text.splitlines() if ln.startswith(".lib "))
    return Path(path).read_text()


def test_a_section_is_reduced_to_what_the_netlist_reaches(tmp_path, cache):
    lib = _lib(tmp_path)
    text, rec = modellib.reduce(_tb(lib), lib, "pdk@1")
    assert rec == [{"library": str(lib.resolve()), "section": "tt", "reduced": rec[0]["reduced"], "cards": [7, 14]}]
    assert Path(rec[0]["reduced"]).parent == cache and Path(rec[0]["reduced"]).name.startswith("pdk-1-lib.spice-tt-")
    assert f"* Undine: the models of {lib.resolve()} tt, from a copy" in text
    assert f".lib {rec[0]['reduced']} tt\n" in text
    copy = _copy(text)
    body = [ln for ln in copy.splitlines()[1:] if not ln.startswith("*")]
    # in the library's order, as written: the .option, the random draw and the .func (kept whatever the netlist uses),
    # the subcircuit and the parameters it names (the card of vth_n with its continuation line), not the p-channel's
    assert body == [".lib tt", ".param mc_mm_switch=0", ".option scale=1u", ".param global_draw = agauss(0, 1, 1)",
                    ".param vth_n = 0.5", "+ kp_n = 200u", ".func twice(x) = 2*x", ".param nslope = 0.02",
                    *NMOS.splitlines()[2:], ".endl tt"]
    # a model's bins are found by its name; a netlist's include (a standard-cell library) counts as the netlist
    (tmp_path / "cells.spice").write_text(".subckt inv a y vdd vss\nXP y a vdd vdd pfet w=2 l=1\n.ends\n")
    text, rec = modellib.reduce(_tb(lib, body=f".include {tmp_path / 'cells.spice'}\nD1 g 0 dio\n"), lib, "pdk@1")
    copy = _copy(text)
    assert ".model dio.0 d is=1e-14 area_min=0\n.model dio.1 d is=2e-14\n" in copy and "other" not in copy
    assert ".subckt pfet" in copy and ".param vth_p = -0.5 kp_p = '80u*1'" in copy and ".subckt nfet" not in copy
    assert "unused_big" not in copy and rec[0]["cards"] == [9, 14]


def test_the_copy_is_made_once_per_library_version_and_definitions(tmp_path, cache):
    lib = _lib(tmp_path)
    a = modellib.reduce(_tb(lib), lib, "pdk@1")[1][0]["reduced"]
    made = Path(a).stat().st_mtime_ns
    # another testbench with the same devices: the same copy, not made again
    b = modellib.reduce(_tb(lib, body="Xa d g 0 0 nfet w=5 l=2\n"), lib, "pdk@1")[1][0]["reduced"]
    assert a == b and Path(a).stat().st_mtime_ns == made
    # other devices, another section, another PDK version: other copies
    assert modellib.reduce(_tb(lib, body="Xa d g d d pfet\n"), lib, "pdk@1")[1][0]["reduced"] != a
    assert modellib.reduce(_tb(lib, "tt_mm"), lib, "pdk@1")[1][0]["reduced"] != a
    assert modellib.reduce(_tb(lib), lib, "pdk@2")[1][0]["reduced"] != a
    # an included file of the library changes: a new copy with the new content
    (tmp_path / "models" / "nmos.spice").write_text(NMOS.replace("0.02", "0.025"))
    c = modellib.reduce(_tb(lib), lib, "pdk@1")[1][0]["reduced"]
    assert c != a and ".param nslope = 0.025" in Path(c).read_text()
    # jobs starting together make one copy, whole, and leave nothing behind
    shutil.rmtree(cache)
    with ThreadPoolExecutor(8) as ex:
        paths = set(ex.map(lambda _: modellib.reduce(_tb(lib), lib, "pdk@1")[1][0]["reduced"], range(16)))
    assert paths == {c} and sorted(p.name for p in cache.iterdir()) == [Path(c).name]
    assert Path(c).read_text().rstrip().endswith(".endl tt")


@pytest.mark.parametrize("change, why", [
    (("lib", ".lib common\n", ".lib common\n.if (1)\n.endif\n"), "a .if card"),
    (("lib", ".lib common\n", ".lib common\nR1 a 0 1k\n"), "an element line outside a subcircuit"),
    (("lib", ".lib common\n", ".lib common\n+ x=1\n"), "a continuation line (+) without a card"),
    (("lib", '.include "models/pmos.spice"', '.include "models/gone.spice"'), "cannot read"),
    (("lib", ".param unused_big = {1 + 2}", ".param unused_big = {1 + 2"), "cannot list the assignments"),
    (("lib", ".param unused_big = {1 + 2}", ".param 1x = 2"), "cannot list the assignments"),
    (("lib", ".lib common\n", ".lib common\n.end\n"), "a .end card"),
    (("nmos", "M1 d g s b", ".include x.spice\nM1 d g s b"), ".include inside a subcircuit"),
    (("nmos", ".ends\n", ""), "has no .ends"),
    (("tb", " tt\n", " ff\n"), "no section ff"),
    (("tb", "V1 d 0 1.8", ".include missing.spice\nV1 d 0 1.8"), "which the netlist includes"),
    (("tb", "V1 d 0 1.8", ".include 'my cells.spice\nV1 d 0 1.8"), "cannot tell the file"),
    (("tb", "V1 d 0 1.8", ".include cells.spice more.spice\nV1 d 0 1.8"), "cannot tell the file"),
    # a line that reads the library itself other than as reduce rewrites one: that section, read whole, may need
    # any definition of the other
    (("tb", "V1 d 0 1.8", ".lib lib.spice tt_mm ; mismatch\nV1 d 0 1.8"), "reads lib.spice other than in a line"),
    (("tb", "op\n", "source more.cir\nop\n"), "sources a file"),
])
def test_what_it_does_not_understand_reads_the_library_whole(tmp_path, cache, change, why):
    where, old, new = change
    lib = _lib(tmp_path, LIB.replace(old, new) if where == "lib" else LIB,
               NMOS.replace(old, new) if where == "nmos" else NMOS)
    tb = _tb(lib).replace(old, new) if where == "tb" else _tb(lib)
    assert old in (LIB if where == "lib" else NMOS if where == "nmos" else _tb(lib))
    for _ in range(2):                                   # the second time from what the first one found
        text, rec = modellib.reduce(tb, lib, "pdk@1", cwd=tmp_path)
        assert text == tb and len(rec) == 1 and why in rec[0]["full"] and "reduced" not in rec[0], rec
        assert modellib.describe(rec[0]).startswith(f"models: lib.spice section {rec[0]['section']} read whole (")
    assert not cache.exists() or not list(cache.iterdir())


def test_the_switch_turns_it_off(tmp_path, cache, monkeypatch):
    lib = _lib(tmp_path)
    monkeypatch.setenv(modellib.FULL_ENV, "1")
    text, rec = modellib.reduce(_tb(lib), lib, "pdk@1")
    assert text == _tb(lib)
    assert rec == [{"library": str(lib.resolve()), "section": "tt", "full": f"{modellib.FULL_ENV} is set"}]
    monkeypatch.setenv(modellib.FULL_ENV, "0")
    assert "reduced" in modellib.reduce(_tb(lib), lib, "pdk@1")[1][0]
    # a netlist without the library is left alone; another library's .lib line too
    assert modellib.reduce("* x\nR1 a 0 1\n.end\n", lib) == ("* x\nR1 a 0 1\n.end\n", [])
    other = f"* x\n.lib {tmp_path / 'models' / 'nmos.spice'} tt\n.end\n"
    assert modellib.reduce(other, lib) == (other, [])


def test_its_own_errors_read_the_library_whole(tmp_path, cache, monkeypatch):
    lib = _lib(tmp_path)

    def broken(*_):
        raise KeyError("x")
    monkeypatch.setattr(modellib, "closure", broken)
    text, rec = modellib.reduce(_tb(lib), lib, "pdk@1")
    assert text == _tb(lib) and rec[0]["full"] == "the reducer failed (KeyError: 'x')"


def test_the_copies_are_in_an_absolute_place_and_made_again_when_cut_short(tmp_path, monkeypatch):
    lib = _lib(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(modellib.FULL_ENV, raising=False)
    # the netlist names its copy, and ngspice reads it from the job's directory: a relative place is made absolute
    monkeypatch.setenv("UNDINE_CACHE", "cache")
    assert modellib.cache_dir() == Path.cwd() / "cache" / "models"
    path = Path(modellib.reduce(_tb(lib), lib, "pdk@1")[1][0]["reduced"])
    assert path.is_absolute() and path.parent == Path.cwd() / "cache" / "models"
    monkeypatch.delenv("UNDINE_CACHE")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", "xdg")                  # relative: ignored, as the XDG spec says
    assert modellib.cache_dir() == tmp_path / "home" / ".cache" / "undine" / "models"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert modellib.cache_dir() == tmp_path / "xdg" / "undine" / "models"
    # a copy cut short (a crash before it was on disk, an edit by hand) is made again, not read by every job after it
    monkeypatch.setenv("UNDINE_CACHE", "cache")
    whole = path.read_bytes()
    for cut in (whole[:len(whole) // 2], b""):
        path.write_bytes(cut)
        assert modellib.reduce(_tb(lib), lib, "pdk@1")[1][0]["reduced"] == str(path) and path.read_bytes() == whole


def test_a_library_on_a_path_of_other_characters(tmp_path, cache):
    (tmp_path / "設計pdk").mkdir()
    lib = _lib(tmp_path / "設計pdk")
    red, rec = modellib.reduce(_tb(lib), lib, "pdk@1")
    assert rec[0]["cards"] == [7, 14] and f"of section tt of {lib.resolve()} (pdk@1)" in _copy(red)
    _same(_run(tmp_path / "full", _tb(lib)), _run(tmp_path / "reduced", red))


# the netlist's includes as ngspice reads them: a quoted path with a blank, a trailing comment, a file with CR LF lines
CELL = ".subckt cell d g\nXP d g d d pfet w=4 l=1\n.ends\n"
INCLUDES = [
    ({"my cells/cells.spice": CELL}, '.include "{d}/my cells/cells.spice"'),
    ({"cells.spice": CELL}, ".include {d}/cells.spice $ the cells"),
    ({"outer.spice": ".include inner.spice\r\n", "inner.spice": CELL.replace("\n", "\r\n")}, ".INC {d}/outer.spice"),
]


@pytest.mark.parametrize("files, line", INCLUDES)
def test_the_files_the_netlist_includes_as_ngspice_reads_them(tmp_path, cache, files, line):
    lib = _lib(tmp_path)
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_bytes(text.encode())
    tb = _tb(lib, body=f"{line.format(d=tmp_path)}\nX1 d g 0 0 nfet w=2 l=1\nXC d g cell\n")
    red, rec = modellib.reduce(tb, lib, "pdk@1")
    assert rec[0]["cards"] == [10, 14] and ".subckt pfet" in _copy(red)      # the p-channel only the include uses
    _same(_run(tmp_path / "full", tb), _run(tmp_path / "reduced", red))


TWO = """* sections a netlist reads together: A's subcircuit reads y, which only B defines, and x, which B defines again
.lib A
.param x = 1
.subckt r1 a b
R1 a b {x*1k + y}
.ends
.endl A

.lib B
.param x = 2
.param y = 500 z = 7
.param unused = 3
.endl B
"""


def test_sections_read_together_keep_what_each_needs_of_the_other(tmp_path, cache):
    (tmp_path / "two.spice").write_text(TWO)
    lib = tmp_path / "two.spice"
    tb = f"* tb\n.lib {lib} A\n.lib {lib} B\nV1 n 0 1\nX1 n 0 r1\n" \
         ".control\nop\nwrite out.raw\nprint all\nquit\n.endc\n.end\n"
    red, rec = modellib.reduce(tb, lib, "pdk@1")
    assert [r["cards"] for r in rec] == [[2, 2], [2, 3]]
    assert ".param x = 2\n.param y = 500 z = 7\n.endl B" in Path(rec[1]["reduced"]).read_text()
    _same(_run(tmp_path / "full", tb), _run(tmp_path / "reduced", red))
    assert sim.read_raw(tmp_path / "reduced" / "out.raw")["data"]["i(v1)"][0] == pytest.approx(-1 / 2500)
    # one of them read whole: the other too (what it needs of the other is not known)
    (tmp_path / "two.spice").write_text(TWO.replace(".lib B\n", ".lib B\n.if (1)\n.endif\n"))
    lib = lib.resolve()
    assert modellib.reduce(tb, lib, "pdk@1") == (tb, [
        {"library": str(lib), "section": "A", "full": f"with section B, which is read whole: a .if card ({lib})"},
        {"library": str(lib), "section": "B", "full": f"a .if card ({lib})"}])


def _ngspice():
    if not tools.find("ngspice"):
        pytest.skip("ngspice not installed")
    return tools.find("ngspice")


def _run(d: Path, text: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "netlist.cir").write_text(text)
    (d / ".spiceinit").write_text("set num_threads=1\n")
    subprocess.run([_ngspice(), "-b", "-o", "log.txt", "netlist.cir"], cwd=d, stdout=subprocess.DEVNULL,
                   stderr=subprocess.STDOUT, timeout=900)
    return d


def _same(a: Path, b: Path, raws=("out",)) -> None:
    """The runs in a and b wrote the same vectors, to the bit, and printed the same values."""
    for raw in raws:
        ra, rb = sim.read_raw(a / f"{raw}.raw"), sim.read_raw(b / f"{raw}.raw")
        assert ra["vars"] == rb["vars"] and len(ra["vars"]) > 1, (a, raw)
        for name, _ in ra["vars"]:
            assert np.array_equal(ra["data"][name], rb["data"][name]), (a, raw, name)

    def printed(d):
        lines = (d / "log.txt").read_text().splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("No. of Data Rows"))
        return [ln for ln in lines[start:] if " = " in ln]
    assert printed(a) == printed(b) and printed(a)


def test_the_same_results_and_the_same_mismatch_draws(tmp_path, cache):
    lib = _lib(tmp_path)
    for section in ("tt", "tt_mm"):
        tb = _tb(lib, section, "X1 d g 0 0 nfet w=2 l=1\nX2 d g 0 0 nfet w=2 l=1\nXP d g d d pfet w=4 l=1\n")
        red, rec = modellib.reduce(tb, lib, "pdk@1")
        assert "reduced" in rec[0]
        _same(_run(tmp_path / f"{section}-full", tb), _run(tmp_path / f"{section}-reduced", red))
    full, mm = (sim.read_raw(tmp_path / f"{s}-full" / "out.raw")["data"]["i(v1)"][0] for s in ("tt", "tt_mm"))
    assert full != mm                                    # mismatch on: other currents, drawn alike from seed 7


def test_a_job_reads_the_reduced_copy_and_says_how(tmp_path, cache, monkeypatch):
    _ngspice()
    lib = _lib(tmp_path)
    jobs = sim.Jobs(limit=2)

    def run(text, **kw):
        job = jobs.submit(tmp_path / "prj", "tb", text, None, models=(lib, "pdk@1"), **kw)
        t0 = time.time()
        while job.status()["state"] in ("queued", "running") and time.time() - t0 < 60:
            time.sleep(0.05)
        assert job.status()["state"] == "done", (job.status(), job.log())
        return job
    (tmp_path / "prj").mkdir()
    job = run(_tb(lib, "tt_mm"))
    st = job.status()
    assert st["models"] == [{"library": str(lib.resolve()), "section": "tt_mm", "reduced": st["models"][0]["reduced"],
                             "cards": [7, 14]}]
    assert "notes" not in st and f".lib {st['models'][0]['reduced']} tt_mm\n" in (job.path / "netlist.cir").read_text()
    assert job.log().startswith("Undine: models: lib.spice section tt_mm as a copy reduced to the 7 of its 14 cards")
    assert "v1#branch" in job.log()                      # ngspice's own log follows
    # the switch: the library itself, the same numbers
    monkeypatch.setenv(modellib.FULL_ENV, "1")
    whole = run(_tb(lib, "tt_mm"))
    assert whole.status()["models"][0]["full"] == f"{modellib.FULL_ENV} is set" and "notes" not in whole.status()
    assert whole.log().startswith(f"Undine: models: lib.spice section tt_mm read whole ({modellib.FULL_ENV} is set)")
    _same(job.path, whole.path)
    # a library it does not understand: read whole, and the job's notes say why
    monkeypatch.delenv(modellib.FULL_ENV)
    (tmp_path / "lib.spice").write_text(LIB.replace(".lib common\n", ".lib common\n.if (1)\n.endif\n"))
    odd = run(_tb(lib, "tt_mm"))
    assert "a .if card" in odd.status()["models"][0]["full"] and "notes" not in odd.status()
    assert odd.log().startswith("Undine: models: lib.spice section tt_mm read whole (a .if card")


# ------------------------------------------------------------------ SKY130: every device family, the same to the bit

MOS = [("nfet_01v8", "n", 1.8, 0.15), ("pfet_01v8", "p", 1.8, 0.15), ("nfet_01v8_lvt", "n", 1.8, 0.15),
       ("pfet_01v8_lvt", "p", 1.8, 0.35), ("pfet_01v8_hvt", "p", 1.8, 0.15), ("nfet_g5v0d10v5", "n", 3.3, 0.5),
       ("pfet_g5v0d10v5", "p", 3.3, 0.5), ("nfet_03v3_nvt", "n", 3.3, 0.5), ("nfet_05v0_nvt", "n", 3.3, 0.9),
       ("special_nfet_01v8", "n", 1.8, 0.15)]


def sky130_devices(lib: Path, section: str, temp: float = 27) -> str:
    """Every device family of sky130_fd_pr Undine's symbols place: the MOSFETs (one each, on its own supply), an
    inverter driving MIM capacitors (m3_1, m3_2) and a varactor through the precision and generic poly resistors,
    diodes and both bipolars; an operating point, a DC sweep, a temperature sweep and a short transient."""
    lines = ["* every device family", f".lib {lib} {section}", ".option seed=4242", f".temp {temp}",
             "Vg g 0 0.9", "Vgh gh 0 2.0", "Vin in 0 pulse(0 1.8 0.1n 0.1n 0.1n 1n 2.4n)"]
    for k, (model, kind, vdd, length) in enumerate(MOS):
        geo = "ad=0.29 as=0.29 pd=2.58 ps=2.58 nrd=0.29 nrs=0.29 sa=0 sb=0 sd=0 mult=1 m=1"
        gate = "gh" if vdd > 2 else "g"
        if kind == "n":
            lines += [f"XM{k} d{k} {gate} 0 0 sky130_fd_pr__{model} L={length} W=1 nf=1 {geo}", f"Vd{k} d{k} 0 {vdd}"]
        else:
            lines += [f"XM{k} d{k} {gate} s{k} s{k} sky130_fd_pr__{model} L={length} W=1 nf=1 {geo}",
                      f"Vs{k} s{k} 0 {vdd}", f"Vd{k} d{k} 0 0"]
    lines += ["Vdd vdd 0 1.8",
              "XI1 out in vdd vdd sky130_fd_pr__pfet_01v8 L=0.15 W=2 nf=1 mult=1 m=1",
              "XI2 out in 0 0 sky130_fd_pr__nfet_01v8 L=0.15 W=1 nf=1 mult=1 m=1",
              "XC1 out 0 sky130_fd_pr__cap_mim_m3_1 W=10 L=10 MF=1 m=1",
              "XR1 out r1 0 sky130_fd_pr__res_high_po_0p35 L=3.5 mult=1 m=1",
              "XC2 r1 0 sky130_fd_pr__cap_mim_m3_2 W=8 L=8 MF=1 m=1",
              "XR2 out r2 0 sky130_fd_pr__res_xhigh_po_0p35 L=7 mult=1 m=1",
              "XC3 r2 0 vdd sky130_fd_pr__cap_var_lvt W=5 L=0.5 VM=1 m=1",
              "XR3 out r3 0 sky130_fd_pr__res_high_po W=1 L=5 mult=1 m=1",
              "XR4 r3 r4 0 sky130_fd_pr__res_xhigh_po W=1 L=5 mult=1 m=1",
              "XR5 r4 0 sky130_fd_pr__res_generic_po W=1 L=5 m=1",
              "Vdio vdio 0 0.7",
              "D1 vdio 0 sky130_fd_pr__diode_pw2nd_05v5 area=1 pj=4",
              "D2 vdio vdd sky130_fd_pr__diode_pd2nw_05v5 area=1 pj=4",
              "XQ1 vdd vdio 0 0 sky130_fd_pr__npn_05v5_W1p00L1p00 m=1 mult=1",
              "XQ2 0 vdio 0 sky130_fd_pr__pnp_05v5_W0p68L0p68 m=1 mult=1",
              ".control", "op", "write op.raw", "print all", "dc Vg 0 1.8 0.3", "write dc.raw",
              "dc temp -40 125 55", "write temp.raw", "tran 20p 3n", "write tran.raw", "quit", ".endc", ".end"]
    return "\n".join(lines) + "\n"


def _sky130() -> Path:
    _ngspice()
    pdk = next((p for k, p in discover(default_pdk_roots()).items() if k.startswith("sky130A")), None)
    if pdk is None:
        pytest.skip("sky130A not installed")
    return pdk.root / pdk.profile.model_lib


def _compare(tmp_path, runs: list[tuple[str, float]]) -> None:
    """Each (section, temperature): the whole library and its reduced copy, four runs at a time."""
    lib = _sky130()
    todo = []
    for section, temp in runs:
        text = sky130_devices(lib, section, temp)
        red, rec = modellib.reduce(text, lib, "sky130A@test")
        assert "reduced" in rec[0] and rec[0]["cards"][0] < rec[0]["cards"][1] / 2, rec
        todo += [(tmp_path / f"{section}-{temp:g}-full", text), (tmp_path / f"{section}-{temp:g}-reduced", red)]
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda t: _run(*t), todo))
    for section, temp in runs:
        run = tmp_path / f"{section}-{temp:g}"
        _same(run.with_name(run.name + "-full"), run.with_name(run.name + "-reduced"), ("op", "dc", "temp", "tran"))


def test_sky130_every_device_family_at_tt_and_with_mismatch(tmp_path):
    _compare(tmp_path, [("tt", 27), ("tt_mm", 27)])
    op = {s: sim.read_raw(tmp_path / f"{s}-27-reduced" / "op.raw")["data"]["i(vd0)"][0] for s in ("tt", "tt_mm")}
    assert op["tt"] != op["tt_mm"]                      # the mismatch section draws: other currents, the same in both


@pytest.mark.skipif(not os.environ.get("UNDINE_SLOW_TESTS"), reason="ten minutes; UNDINE_SLOW_TESTS=1 runs it")
def test_sky130_every_corner_mismatch_section_and_temperature(tmp_path):
    corners = ("ss", "ff", "sf", "fs")
    _compare(tmp_path, [(c, 27) for c in corners] + [(f"{c}_mm", 27) for c in corners] + [("tt", -40), ("tt_mm", 125)])


# ------------------------------------------------------------------ GF180MCU: several sections of the library together

GF_MOS = [("nfet_03v3", "v3", "g", 0.28, 1), ("pfet_03v3", "v3", "g", 0.28, 1), ("nfet_03v3", "v3", "g", 2, 10),
          ("pfet_03v3", "v3", "g", 1, 5), ("nfet_05v0", "v6", "gh", 0.6, 2), ("pfet_05v0", "v6", "gh", 0.5, 2),
          ("nfet_06v0", "v6", "gh", 0.7, 2), ("pfet_06v0", "v6", "gh", 0.55, 2), ("nfet_06v0_nvt", "v6", "gh", 1.8, 10),
          ("nfet_03v3_dss", "v3", "g", 0.28, 10), ("pfet_03v3_dss", "v3", "g", 0.28, 10),
          ("nfet_06v0_dss", "v6", "gh", 0.6, 10), ("pfet_06v0_dss", "v6", "gh", 0.5, 10)]


def gf180_devices(pdk, corner: str, temp: float, mismatch: bool, mim_new: bool = True) -> str:
    """The device families of gf180mcu_fd_pr, their models read as the PDK's own test benches read them
    (libs.tech/xschem/tests): design.ngspice and a section per family, cap_mim's subcircuits reading the parameters
    of mimcap_<corner> (and with `mim_new`, a capacitor of mimcap_<corner> itself, which reads them too); mismatch on
    (design.ngspice's sw_stat_mismatch) with a seed."""
    lib, rc = pdk.root / pdk.profile.model_lib, corner if corner in ("typical", "ss", "ff") else "typical"
    lines = ["* gf180 device families", *(f".include {pdk.root / f}" for f in pdk.profile.model_includes),
             *(f".lib {lib} {s}" for s in (corner, "cap_mim", f"res_{rc}", f"moscap_{rc}", f"bjt_{rc}", f"diode_{rc}",
                                            f"mimcap_{rc}")),
             f".temp {temp:g}", "Vg g 0 1.5", "Vgh gh 0 3", "V3 v3 0 3.3", "V6 v6 0 5",
             "Vin in 0 pulse(0 3.3 0.1n 0.1n 0.1n 1n 2.4n)"]
    for k, (model, vdd, gate, length, width) in enumerate(GF_MOS):
        s, d = ("0", vdd) if model[0] == "n" else (vdd, "0")
        lines += [f"XM{k} d{k} {gate} {s} {s} {model} L={length}u W={width}u nf=1 m=1", f"Vd{k} d{k} {d} 0"]
    lines += ["XI1 out in v3 v3 pfet_03v3 L=0.28u W=2u", "XI2 out in 0 0 nfet_03v3 L=0.28u W=1u",
              "XR1 out r1 0 nplus_u r_length=10e-6 r_width=1e-6", "XR2 r1 r2 0 pplus_u r_length=10e-6 r_width=1e-6",
              "XR3 r2 r3 0 ppolyf_u_1k r_length=10e-6 r_width=1e-6", "XR4 r3 r4 0 npolyf_u r_length=10e-6 r_width=1e-6",
              "XR5 r4 r5 0 nwell r_length=10e-6 r_width=2e-6", "XR6 r5 r6 rm1 r_length=10e-6 r_width=1e-6",
              "XR7 r6 0 tm30k r_length=10e-6 r_width=1e-6",
              "XC1 out 0 cap_mim_2f0fF c_width=10e-6 c_length=10e-6 m=1",
              *(["XC2 r2 0 cap_mim_2f0_m4m5_noshield c_width=10e-6 c_length=10e-6"] if mim_new else []),
              "XC3 r4 0 cap_nmos_03v3 c_width=10e-6 c_length=10e-6",
              "Vdio vdio 0 0.6", "D1 0 vdio diode_nd2ps_03v3 area=1e-12 pj=4e-6",
              "D2 vdio v3 diode_pd2nw_03v3 area=1e-12 pj=4e-6",
              "XQ1 v3 vdio 0 0 npn_05p00x05p00", "XQ2 0 vdio 0 pnp_05p00x05p00",
              *([f".param {pdk.profile.mismatch_param}", ".option seed=13996"] if mismatch else []),
              ".control", "op", "write op.raw", "print all", "dc Vg 0 3.3 0.3", "write dc.raw",
              "dc temp -40 125 55", "write temp.raw", "tran 20p 3n", "write tran.raw", "quit", ".endc", ".end"]
    return "\n".join(lines) + "\n"


def test_gf180_sections_read_together_at_corners_and_with_mismatch(tmp_path):
    _ngspice()
    pdk = next((p for k, p in discover(default_pdk_roots()).items() if k.startswith("gf180mcuD")), None)
    if pdk is None:
        pytest.skip("gf180mcuD not installed")
    # at typical without mismatch, cap_mim_2f0fF is the only capacitor, as in the PDK's test_cap_mim_2f0fF: nothing
    # the netlist names is in mimcap_typical, only what cap_mim's subcircuits read
    runs, todo = [("typical", 27, False), ("typical", 27, True), ("ss", 125, True)], []
    for corner, temp, mismatch in runs:
        text = gf180_devices(pdk, corner, temp, mismatch, mim_new=mismatch)
        red, rec = modellib.reduce(text, pdk.root / pdk.profile.model_lib, pdk.id)
        assert len(rec) == 7 and all("reduced" in r for r in rec), rec
        # cap_mim's subcircuits need mim_corner_2p0ff ..., which only mimcap_<corner> defines
        assert rec[-1]["cards"][0] > 0 and "mim_corner_2p0ff" in Path(rec[-1]["reduced"]).read_text()
        run = f"{corner}-{temp}-{int(mismatch)}"
        todo += [(tmp_path / f"{run}-full", text), (tmp_path / f"{run}-reduced", red)]
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda t: _run(*t), todo))
    for corner, temp, mismatch in runs:
        run = tmp_path / f"{corner}-{temp}-{int(mismatch)}"
        _same(run.with_name(run.name + "-full"), run.with_name(run.name + "-reduced"), ("op", "dc", "temp", "tran"))
    op = {m: sim.read_raw(tmp_path / f"typical-27-{m}-reduced" / "op.raw")["data"]["i(vd0)"][0] for m in (0, 1)}
    assert op[0] != op[1]                                # mismatch on: other currents, drawn alike from the seed
