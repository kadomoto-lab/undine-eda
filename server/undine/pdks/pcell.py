"""PCells from the PDK, evaluated server-side.

The PDK's PCell code runs in a separate Python environment through `pcell_worker.py`; the result is a plain cell
with the parameters stored as layout meta information. The browser only sees the parameter schema and the
generated geometry (as tiles).

Each PDK profile names its KLayout PCell library (pdkprofile.PcellLib) and the pixi environment that runs it:
- sky130 from open_pdks 2025 and later (e.g. 8afc834): the package `cells` in libs.tech/klayout/python, library
  "skywater130", gdsfactory 8 on kfactory 0.2x (environment `pcell`). The older generation (libs.tech/klayout/pymacros/
  cells, gdsfactory 6, open_pdks before 2025) is no longer run: such a PDK reports its PCells as unavailable, with the
  version to move the project to (`python -m undine set-pdk`).
- GF180MCU: `cells` in libs.tech/klayout/tech/pymacros, library "gf180mcu", gdsfactory 7.9 (environment `pcell-gf180`).
- IHP SG13G2: `sg13g2_pycell_lib` on IHP's PyCell API, library "SG13_dev" of the technology sg13g2 (environment `pcell`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from undine.pdks.pdk import Pdk

WORKER = Path(__file__).with_name("pcell_worker.py")
GEN_VERSION = 3  # bump when the generated geometry changes (cell names embed it); 3: open_pdks 2025 PCell code
MIM_CLAMP_UM = 2.0   # the side the open_pdks mim_cap drew at least before PcellLib.limits lowered its clamp
SUPPORTED = {"sky130": "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"}   # the open_pdks version Undine is tested with


def _profile(pdk):
    from undine.pdks import pdkprofile
    return pdkprofile.for_pdk(getattr(pdk, "name", None), getattr(pdk, "family", None))


def python_exe(env: str = "pcell") -> str | None:
    """The Python of a PCell environment: $UNDINE_PCELL_PYTHON (pcell) / $UNDINE_PCELL_GF180_PYTHON (pcell-gf180), else
    the pixi environment of that name."""
    repo = Path(__file__).resolve().parents[3]
    var = "UNDINE_" + env.upper().replace("-", "_") + "_PYTHON"
    for c in (os.environ.get(var), str(repo / ".pixi" / "envs" / env / "bin" / "python")):
        if c and Path(c).is_file():
            return c
    return None


def spec(pdk: Pdk | None) -> dict | None:
    """How the worker loads the PDK's PCell library, when the PDK has one Undine runs."""
    lib = _profile(pdk).pcell_lib if pdk else None
    if lib is None or not pdk.klayout_tech:
        return None
    paths = [pdk.klayout_tech / p for p in lib.paths]
    if not (paths[0] / lib.module / "__init__.py").is_file():
        return None
    return {"paths": [str(p) for p in paths], "module": lib.module, "init": lib.init, "library": lib.library,
            "technology": lib.technology, "grid_nm": _profile(pdk).grid_nm, "env": lib.env,
            **({"limits": [list(x) for x in lib.limits]} if lib.limits else {})}


def macros_dir(pdk: Pdk | None) -> Path | None:
    """The directory holding the PDK's PCell package, when it is one Undine runs."""
    s = spec(pdk)
    return Path(s["paths"][0]) if s else None


def legacy(pdk: Pdk | None) -> bool:
    """A PDK with the older PCell code (open_pdks before 2025), which Undine no longer runs."""
    return bool(pdk and pdk.klayout_tech and macros_dir(pdk) is None
                and (pdk.klayout_tech / "pymacros" / "cells" / "__init__.py").is_file())


def unavailable_reason(pdk: Pdk | None) -> str | None:
    """Why the PDK's PCells cannot run here (None when they can)."""
    if legacy(pdk):
        fam = getattr(pdk, "family", "") or "sky130"
        return (f"{pdk.id} has the PCell code of open_pdks before 2025, which Undine no longer supports: move the project "
                f"to {pdk.name}@{SUPPORTED.get(fam, '')[:12]} (python -m undine set-pdk PROJECT {pdk.name}@{SUPPORTED.get(fam, '')[:12]})")
    s = spec(pdk)
    if s is None:
        return "the PDK has no PCell generators Undine can run"
    if python_exe(s["env"]) is None:
        return (f"the PCell environment is not installed: pixi install -e {s['env']}"
                + (" (gdsfactory 8 with kfactory; or set UNDINE_PCELL_PYTHON)" if s["env"] == "pcell" else ""))
    return None


_cache: dict[str, list[dict]] = {}


def _run(sp: dict, args: list[str], timeout: int = 180) -> str:
    exe = python_exe(sp["env"])
    if not exe:
        raise RuntimeError(f"no PCell environment (pixi install -e {sp['env']})")
    # the PDK generators drop scratch .gds files in the cwd; no __pycache__ may land in the PDK tree, whose hash is
    # pinned in the lock
    r = subprocess.run([exe, str(WORKER), json.dumps(sp), *args], capture_output=True, text=True, timeout=timeout,
                       cwd=tempfile.gettempdir(), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"worker failed ({r.returncode})")
    return r.stdout


def available(pdk: Pdk | None) -> bool:
    return unavailable_reason(pdk) is None


def catalog(pdk: Pdk | None) -> list[dict]:
    sp = spec(pdk)
    if not sp or not python_exe(sp["env"]):
        return []
    key = json.dumps(sp, sort_keys=True)
    if key not in _cache:
        out = _run(sp, ["list"]).strip().splitlines()
        _cache[key] = json.loads(out[-1])
    return _cache[key]


def _slug(v) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "p", str(v)).strip("p")[:12]


def gen_version(pcell: str, params: dict) -> int:
    """The generation a variant's geometry is of (cell_name embeds it): GEN_VERSION, or 4 for a mim_cap with a side
    below MIM_CLAMP_UM, drawn as asked since the clamp was lowered (the larger ones keep their names: unchanged)."""
    if pcell == "mim_cap":
        try:
            if min(float(params.get(k, MIM_CLAMP_UM)) for k in ("w", "l")) < MIM_CLAMP_UM:
                return 4
        except (TypeError, ValueError):
            pass
    return GEN_VERSION


def cell_name(pcell: str, params: dict) -> str:
    h = hashlib.sha1(json.dumps([gen_version(pcell, params), params], sort_keys=True).encode()).hexdigest()[:6]
    base = str(params.get("type") or pcell)
    keys = [k for k in ("w", "l", "nf", "len", "w_gate", "l_gate", "ng") if k in params]
    return "_".join([base] + [f"{k}{_slug(params[k])}" for k in keys] + [h])


def generate(pdk: Pdk, pcell: str, params: dict) -> tuple[Path, str, dict]:
    """Returns (OASIS file, cell name, {bbox, dbu})."""
    why = unavailable_reason(pdk)
    if why:
        raise RuntimeError(why)
    if pcell not in {p["name"] for p in catalog(pdk)}:
        raise KeyError(f"no PCell {pcell}")
    name = cell_name(pcell, params)
    tmp = Path(tempfile.mkdtemp(prefix="undine-pcell-")) / f"{name}.oas"
    out = _run(spec(pdk), ["gen", pcell, json.dumps(params), str(tmp), name]).strip().splitlines()
    info = json.loads(out[-1])
    if info["bbox"][0] > info["bbox"][2]:
        raise RuntimeError(f"PCell {pcell} produced no geometry for {params}")
    return tmp, name, info
