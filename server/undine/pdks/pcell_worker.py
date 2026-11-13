"""PCell worker. Runs inside a Python environment that can import the PDK's PCell code (see pcell.py: gdsfactory 8 for
sky130, gdsfactory 7.9 for GF180MCU, IHP's PyCell API for SG13G2). Not imported by the server.

    python pcell_worker.py SPEC_JSON list
    python pcell_worker.py SPEC_JSON gen PCELL PARAMS_JSON OUT.oas CELLNAME

SPEC_JSON (pcell.spec): {paths, module, init, library, technology, grid_nm}.
"""
import contextlib
import importlib
import io
import json
import sys
import warnings

warnings.filterwarnings("ignore")


def _call(x):
    return list(x() if callable(x) else x)


def _load(spec: dict):
    sys.dont_write_bytecode = True     # importing the PDK's code must not write __pycache__ into the PDK tree
    for p in spec["paths"]:
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        import pya                    # the full API (IHP's code logs through pya.Logger)
    except ImportError:
        import klayout.db as pya
        sys.modules["pya"] = pya
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        mod = importlib.import_module(spec["module"])
        if spec.get("init"):
            getattr(mod, spec["init"])()
        for sub, attr, value in spec.get("limits") or ():   # read when a PCell coerces its parameters: lowered to the rule
            setattr(importlib.import_module(f"{spec['module']}.{sub}"), attr, value)
    lib = pya.Library.library_by_name(spec["library"], spec["technology"]) if spec.get("technology") else \
        pya.Library.library_by_name(spec["library"])
    if lib is None:
        sys.exit(f"the PDK did not register the PCell library {spec['library']}")
    return pya, lib


def _choices(p) -> list[str]:
    return [str(c) for c in _call(p.choice_values)]


def main() -> None:
    spec, cmd = json.loads(sys.argv[1]), sys.argv[2]
    pya, lib = _load(spec)
    ly = lib.layout()
    names = ly.pcell_names() if hasattr(ly, "pcell_names") else []

    if cmd == "list":
        out = []
        for n in names:
            d = ly.pcell_declaration(n)
            ps = []
            for p in d.get_parameters():
                if p.hidden:
                    continue
                choices = _choices(p)
                default = p.default if isinstance(p.default, (int, float, str, bool, type(None))) else str(p.default)
                if default is None and choices:          # a pick list without a default starts at its first entry
                    default = choices[0]
                ps.append({"name": p.name, "label": p.description, "type": {
                    pya.PCellParameterDeclaration.TypeInt: "int", pya.PCellParameterDeclaration.TypeDouble: "float",
                    pya.PCellParameterDeclaration.TypeBoolean: "bool", pya.PCellParameterDeclaration.TypeString: "string",
                    pya.PCellParameterDeclaration.TypeList: "list"}.get(p.type, "string"),
                    "default": default, "unit": p.unit, "readonly": p.readonly,
                    "choices": _call(p.choice_descriptions) or None, "choice_values": choices or None})
                if ps[-1]["choice_values"]:      # a string with choices (newer PDK code) is a pick list like TypeList
                    ps[-1]["type"] = "list"
            out.append({"name": n, "params": ps})
        print(json.dumps(out))
    elif cmd == "gen":
        name, params, dest, cellname = sys.argv[3], json.loads(sys.argv[4]), sys.argv[5], sys.argv[6]
        d = ly.pcell_declaration(name)
        decl = {p.name: p for p in d.get_parameters()}
        vals = {k: v for k, v in params.items() if k in decl}
        for k, p in decl.items():                # GF180's pick lists have no default: the generator then draws nothing
            if k not in vals and p.default is None and _choices(p):
                vals[k] = _choices(p)[0]
        target = pya.Layout()
        target.dbu = ly.dbu
        if spec.get("technology"):
            target.technology_name = spec["technology"]
        said = io.StringIO()     # KLayout catches a generator's exception and leaves the cell empty: keep its message
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            proxy = target.create_cell(name, spec["library"], vals)
            # flatten the proxy hierarchy into a plain cell
            flat = target.create_cell(cellname)
            flat.insert(pya.CellInstArray(proxy.cell_index(), pya.Trans()))
            flat.flatten(True)
        for ci in [c.cell_index() for c in target.each_cell() if c.cell_index() != flat.cell_index()]:
            if target.is_valid_cell_index(ci):
                target.delete_cell(ci)
        flat.name = cellname
        # the PDK generators may work on a finer grid than the process's manufacturing grid: snap every layer
        g = max(1, round(spec.get("grid_nm", 5) * 0.001 / target.dbu))
        for li in list(target.layer_indexes()):
            r = pya.Region(flat.begin_shapes_rec(li))
            if r.is_empty():
                continue
            r.merge()
            flat.clear(li)
            flat.shapes(li).insert(r.snapped(g, g))
        b = flat.bbox()
        if b.empty():
            lines = [x for x in said.getvalue().splitlines() if x.strip()]
            sys.exit(f"PCell {name} produced no geometry for {params}" + (f": {lines[-1].strip()}" if lines else ""))
        flat.add_meta_info(pya.LayoutMetaInfo("undine.pdks.pcell", json.dumps({"pcell": name, "params": params}), None, True))
        target.write(dest)
        print(json.dumps({"cell": cellname, "bbox": [b.left, b.bottom, b.right, b.top], "dbu": ly.dbu}))


if __name__ == "__main__":
    main()
