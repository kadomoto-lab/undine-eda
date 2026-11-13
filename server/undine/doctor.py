"""`python -m undine doctor`: what is installed, what is missing, and the command that fixes it.

Levels: `required` (the server does not start or shows nothing), `recommended` (a feature is off without it),
`optional`. The exit code is 1 when something required is missing.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

from undine.pdks import ciel, pcell
from undine import tools
from undine.pdks.pdk import Pdk

REPO = Path(__file__).resolve().parents[2]
OK, MISS = "ok", "missing"
KLAYOUT_HINT = ("install the KLayout application (>= 0.30) from https://www.klayout.de/build.html "
                "(./setup.sh check prints the command for this system)")


def _module_version(name: str) -> str | None:
    if importlib.util.find_spec(name) is None:
        return None
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "installed"


def _tool(name: str) -> str:
    """A tool's row detail: its path, then its version number and where it comes from ('0.30.12 (pixi)'); the banner
    when no number could be read from it. Empty when it is not installed."""
    found, rec = tools.locate(name), tools.tool_record(name)
    if not found or not rec:
        return ""
    return f"{found[0]}  {rec['version'] or rec.get('text') or '?'} ({rec['from']})"


def checks(pdks: dict[str, Pdk]) -> list[tuple[str, str, str, str]]:
    """(level, what, status / detail, fix) rows."""
    rows = []

    def add(level, what, found, detail, fix):
        rows.append((level, what, f"{OK}  {detail}" if found else MISS, "" if found else fix))

    py = sys.version_info
    add("required", "Python >= 3.10", py >= (3, 10), f"{py.major}.{py.minor}.{py.micro}", "use Python 3.10 or newer")
    kl = _module_version("klayout")
    kl_ok = bool(kl) and tuple(int(x) for x in kl.split(".")[:2] if x.isdigit()) >= (0, 30)
    add("required", "klayout Python module >= 0.30", kl_ok, kl or "", "pixi install")
    try:
        from undine import _core
        core = _core.__version__
    except ImportError:
        core = None
    add("required", "undine._core (Rust core)", bool(core), core or "", "pixi install (builds it from server/src; needs cargo)")
    for m in ("fastapi", "uvicorn"):
        v = _module_version(m)
        add("required", m, bool(v), v or "", "pixi install")
    dist = REPO / "web" / "dist" / "index.html"
    add("required", "web UI build (web/dist)", dist.is_file(), str(dist.parent), "pixi run web")
    add("required", "a PDK", bool(pdks), ", ".join(pdks) or "",
        "pixi run pdk")

    pre = tools.tools_prefix()
    for name, use in (("ngspice", "simulation"), ("klayout", "DRC / LVS"), ("xschem", "Xschem netlister"),
                      ("netgen", "Magic + Netgen LVS")):
        detail = _tool(name)
        add("recommended", f"{name} ({use})", bool(detail), detail,
            KLAYOUT_HINT if name == "klayout" else "pixi run ngspice" if name == "ngspice" else "pixi install")
    ng = tools.find("ngspice")
    if ng:
        add("recommended", "ngspice with OSDI (IHP's Verilog-A models)", tools.ngspice_osdi(ng), tools.version("ngspice") or "",
            "pixi run ngspice (builds ngspice with OSDI into the environment)")
    need = [p for p in pdks.values() if p.profile.osdi]
    if need:
        bad = [p.id for p in need if not all(f.is_file() for f in p.osdi_files())]
        old = [p.id for p in need if p.id not in bad and p.legacy_osdi_cached()]
        detail = ", ".join(p.id for p in need) + (f"; {', '.join(old)} from the old cache: run `pixi run osdi` to move "
                                                  "them into the store" if old else "")
        add("recommended", "compiled Verilog-A models of the PDKs that need them", not bad,
            detail, f"pixi run osdi (OpenVAF; missing for {', '.join(bad)})")
    pm = shutil.which("pmap")
    add("recommended", "pmap (called by the sky130 KLayout LVS deck)", bool(pm), pm or "",
        "install procps-ng (RHEL family) or procps (Debian, Ubuntu)")
    pc = pcell.python_exe()
    add("recommended", "PCell environment (gdsfactory 8 with kfactory; sky130, IHP)", bool(pc), pc or "", "pixi install -e pcell")
    if any(p.family == "gf180mcu" for p in pdks.values()):
        pg = pcell.python_exe("pcell-gf180")
        add("recommended", "GF180MCU PCell environment (gdsfactory 7.9)", bool(pg), pg or "", "pixi install -e pcell-gf180")
    ok = [p.id for p in pdks.values() if pcell.macros_dir(p)]
    add("recommended", "a PDK whose PCells Undine runs", bool(ok), ", ".join(ok), "pixi run pdk")
    old = [p.id for p in pdks.values() if pcell.legacy(p)]
    if old:
        rows.append(("note", "PDKs no longer supported (PCell code before 2025)", ", ".join(old),
                     "projects pinned to them: python -m undine set-pdk PROJECT " + ", ".join(ok[:1] or ["<a supported PDK>"])))

    detail = _tool("magic")
    add("optional", "magic (Magic DRC, Magic + Netgen LVS)", bool(detail), detail, "pixi install")
    v = _module_version("vlsirtools")
    add("optional", "vlsirtools (netlister)", bool(v), v or "", "pixi install (or pip install vlsirtools)")
    git = shutil.which("git")
    add("optional", "git (library dependencies, git-setup)", bool(git), git or "", "install git with the system's package manager")
    found = ciel.exe()                       # the one `pdk fetch` runs ($UNDINE_CIEL first)
    add("optional", "ciel (PDK manager)", bool(found), str(found or ""), "pixi install")
    add("optional", "ANTHROPIC_API_KEY (cloud assistant)", bool(os.environ.get("ANTHROPIC_API_KEY")), "set",
        "put it in ~/.undine/env (see README)")
    rows.append(("info", "Undine", tools.undine_version(), ""))
    rows.append(("info", "pixi.lock", tools.pixi_lock_sha() or "none (not run in this checkout's pixi environment)", ""))
    rows.append(("info", "pixi environment", os.environ.get("PIXI_ENVIRONMENT_NAME") or "none (not run with pixi)", ""))
    rows.append(("info", "tool prefix ($UNDINE_TOOLS)", str(pre) if pre else "none, tools from PATH", ""))
    return rows


def run(pdks: dict[str, Pdk]) -> int:
    rows = checks(pdks)
    w = max(len(r[1]) for r in rows)
    failed = False
    for level, what, status, fix in rows:
        missing = status == MISS
        failed |= missing and level == "required"
        mark = "!" if level == "note" else ("✗" if level == "required" else "-") if missing else "✓"
        print(f" {mark} {what:<{w}}  {status}" + (f"  [{level}]  -> {fix}" if missing or level == "note" else ""))
    print("\nrequired items are missing" if failed else "\nready: python -m undine serve")
    return 1 if failed else 0
