"""The PDK profiles as golden data: every value Undine reads from a Profile, in a JSON form that keeps types.

data/profiles_golden.json was written from the Python profiles of pdkprofile.py before they became profile.toml
(packages phase 6), and test_profile_data.py compares the profiles loaded from the TOML files with it. A deliberate
change of a profile changes this file too: run

    pixi run python server/tests/profile_golden.py

and review its diff. The form: a dataclass is {"$": class name, field: value, ...} with every field, a tuple a JSON
array, a dict {"dict": [[key, value], ...]} (its order kept), a list {"list": [...]}, a formula {"formula": name};
JSON keeps 1 and 1.0 apart, so a float written as an integer (a deck's -rd text) is a difference.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GOLDEN = Path(__file__).with_name("data") / "profiles_golden.json"
VARIANTS = ("sky130A", "sky130B", "sky130a", "sky130C", "gf180mcuA", "gf180mcuB", "gf180mcuC", "gf180mcuD",
            "ihp-sg13g2", "ihp-sg13cmos5l")
FAMILIES = ("sky130", "gf180mcu", "ihp-sg13", "ihp-sg13g2", "mystery", None)


def canon(v, formulas: dict | None = None):
    """`v` in the golden form. `formulas`: {function: registry name} for functions (formulas.FORMULAS by default)."""
    if formulas is None:
        from undine.pdks import formulas as f
        formulas = {fn: name for name, fn in f.FORMULAS.items()}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {"$": type(v).__name__, **{f.name: canon(getattr(v, f.name), formulas) for f in dataclasses.fields(v)}}
    if isinstance(v, tuple):
        return [canon(x, formulas) for x in v]
    if isinstance(v, list):
        return {"list": [canon(x, formulas) for x in v]}
    if isinstance(v, dict):
        return {"dict": [[k, canon(x, formulas)] for k, x in v.items()]}
    if callable(v):
        return {"formula": formulas[v]}
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    raise TypeError(f"{type(v).__name__} in a profile: {v!r}")


def golden(pp, formulas: dict | None = None) -> dict:
    """The golden data of the pdkprofile module `pp`."""
    variants = {}
    for v in VARIANTS:
        p = pp.for_pdk(v, None)
        decks = [d.replace("?", v[-1]) for d in p.drc_klayout]
        variants[v] = {"profile": p.family, "stack": canon(p.stack, formulas),
                       "same_but_stack": canon(dataclasses.replace(p, stack=None), formulas)
                       == canon(dataclasses.replace(pp.PROFILES.get(p.family, p), stack=None), formulas),
                       "drc_rd": {d: [p.drc_rd(d, {}, v), p.drc_rd(d, {"feol": False, "offgrid": True}, v)]
                                  for d in decks},
                       "lvs_rd": p.lvs_rd({}, v), "magic_file": p.magic_file(v), "netgen_file": p.netgen_file(v)}
    mim = next(e.params for e in pp.SKY130.lvs_elements if e.params)
    return {"profiles": {k: canon(p, formulas) for k, p in [*pp.PROFILES.items(), ("generic", pp.GENERIC)]},
            "variants": variants,
            "for_family": {str(f): pp.for_family(f).family for f in FAMILIES},
            "formulas": {"mim_area_perimeter": mim({"w": 1.17, "l": 2.5})}}


def write(data: dict, path: Path = GOLDEN) -> None:
    path.write_text(json.dumps(data, indent=1) + "\n")


def diff(a, b, path: str = "") -> list[str]:
    """Where two golden forms differ (key paths), at most a few."""
    if type(a) is not type(b):
        return [f"{path or '.'}: {a!r} != {b!r}"]
    if isinstance(a, dict):
        out = [f"{path}.{k}: only on one side" for k in a.keys() ^ b.keys()]
        for k in a.keys() & b.keys():
            out += diff(a[k], b[k], f"{path}.{k}")
        if not out and list(a) != list(b):
            out.append(f"{path}: key order {list(a)} != {list(b)}")
        return out[:20]
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items != {len(b)}"]
        return [x for i, (p, q) in enumerate(zip(a, b)) for x in diff(p, q, f"{path}[{i}]")][:20]
    return [] if a == b else [f"{path or '.'}: {a!r} != {b!r}"]


if __name__ == "__main__":
    from undine.pdks import pdkprofile

    write(golden(pdkprofile))
    print(f"wrote {GOLDEN}")
