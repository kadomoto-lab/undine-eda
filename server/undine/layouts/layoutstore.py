"""Layout storage of a project library: one OASIS file per cell, next to the cell's schematic and symbol.

    libs/<lib>/inv.sch  inv.sym  inv.oas        the views of cell `inv`

A cell file holds that cell's own shapes, instances and meta information (e.g. the parameters of a generated PCell).
The cells it instantiates are in it as ghost cells, by name only, so each cell's geometry is stored once; reading
all files of the library into one layout resolves them (in any order). Saving writes only the cells whose content
changed and removes the files of deleted cells, so a version-control system sees exactly the cells that changed.

File names are the cell names; characters outside [A-Za-z0-9_.+$-] (and a leading dot) are %-encoded.

Before this layout (project format 1) all cells of a library were in one `layout.oas`; `split_legacy` converts it
(Project does this once, see project.py).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

import klayout.db as kdb

SUFFIX = ".oas"
LEGACY = "layout.oas"
_SAFE = re.compile(r"[A-Za-z0-9_.+$-]+")


def file_name(cell: str) -> str:
    name = cell if _SAFE.fullmatch(cell) else quote(cell, safe="+$")
    if name.startswith("."):
        name = "%2E" + name[1:]
    return name + SUFFIX


def cell_name(path: Path) -> str:
    return unquote(path.name[:-len(SUFFIX)])


def cell_files(lib_dir: Path) -> list[Path]:
    return sorted(p for p in lib_dir.glob(f"*{SUFFIX}") if p.is_file() and not p.name.startswith(".")) \
        if lib_dir.is_dir() else []


def read(lib_dir: Path, layout: kdb.Layout) -> None:
    """Reads every cell file of the library into `layout` (ghost cells are replaced by their definitions)."""
    files = cell_files(lib_dir)
    for f in files:
        layout.read(str(f))


def cell_sha(layout: kdb.Layout, cell: kdb.Cell) -> str:
    """Hash of what the cell's own file holds: shapes (with properties), instances (by cell name, with properties),
    persisted meta information. Order does not count."""
    h = hashlib.sha256(f"{cell.name}\n{layout.dbu:.12g}\n".encode())
    for info, li in sorted((str(layout.get_info(li)), li) for li in layout.layer_indexes()):
        shapes = cell.shapes(li)
        if not shapes.is_empty():
            h.update(f"L {info}\n".encode() + "\n".join(sorted(s.to_s() for s in shapes.each())).encode())
    insts = []
    for i in cell.each_inst():
        a = f" {i.a} {i.b} {i.na} {i.nb}" if i.is_regular_array() else ""
        props = dict(layout.properties(i.prop_id)) if i.has_prop_id() else {}
        p = f" {sorted(props.items(), key=lambda item: (type(item[0]).__name__, str(item[0])))}" if props else ""
        insts.append(f"{i.cell.name} {i.cplx_trans}{a}{p}")
    h.update(("\nI " + "\nI ".join(sorted(insts)) if insts else "").encode())
    meta = sorted(f"{m.name}={m.value!r}" for m in cell.each_meta_info() if m.is_persisted())
    h.update(("\nM " + "\nM ".join(meta) if meta else "").encode())
    return h.hexdigest()


def snapshot(layout: kdb.Layout) -> dict[str, str]:
    """{cell name: cell_sha} of every (non-ghost) cell: what is on disk right after a read or a write."""
    return {c.name: cell_sha(layout, c) for c in layout.each_cell() if not c.is_ghost_cell()}


def cell_layout(layout: kdb.Layout, cell: kdb.Cell) -> kdb.Layout:
    """A layout holding `cell` alone, the cells it instantiates as ghost cells."""
    out = kdb.Layout()
    out.dbu = layout.dbu
    nc = out.create_cell(cell.name)
    nc.copy_shapes(cell)              # maps layers by layer info, translates properties
    ghosts: dict[int, int] = {}
    for i in cell.each_inst():
        ci = i.cell_index
        if ci not in ghosts:
            g = out.create_cell(layout.cell(ci).name)
            g.ghost_cell = True
            ghosts[ci] = g.cell_index()
        arr = i.cell_inst.dup()
        arr.cell_index = ghosts[ci]
        ni = nc.insert(arr)
        if i.has_prop_id():
            ni.prop_id = out.properties_id(layout.properties(i.prop_id))
    for m in cell.each_meta_info():
        nc.add_meta_info(m)
    return out


def write(lib_dir: Path, layout: kdb.Layout, saved: dict[str, str]) -> dict[str, str]:
    """Writes the cells whose content differs from `saved` (a snapshot of the files), removes the files of cells
    that are gone, and returns the new snapshot. New files are written next to their targets first and renamed
    at the end, so a failure leaves the previous files in place."""
    lib_dir.mkdir(parents=True, exist_ok=True)
    now = snapshot(layout)
    opt = kdb.SaveLayoutOptions()
    opt.format = "OASIS"
    staged: list[tuple[Path, Path]] = []
    try:
        for name, sha in now.items():
            if saved.get(name) != sha or not (lib_dir / file_name(name)).is_file():
                dest = lib_dir / file_name(name)
                tmp = dest.with_name(f".{dest.name}.tmp")
                cell_layout(layout, layout.cell(name)).write(str(tmp), opt)
                staged.append((tmp, dest))
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, dest in staged:
        os.replace(tmp, dest)
    for name in set(saved) - set(now):
        (lib_dir / file_name(name)).unlink(missing_ok=True)
    return now


def split_legacy(lib_dir: Path) -> list[str]:
    """Converts a format-1 library (all cells in layout.oas) into cell files. layout.oas is kept as
    layout.oas.pre-split (safe to delete once the cell files are checked). Returns the cells written."""
    src = lib_dir / LEGACY
    if not src.is_file():
        return []
    ly = kdb.Layout()
    ly.read(str(src))
    cells = sorted(write(lib_dir, ly, {}))
    os.replace(src, lib_dir / (LEGACY + ".pre-split"))
    return cells


def textconv(path: Path) -> str:
    """A cell file as sorted text lines, for `git diff` (see `python -m undine git-setup`)."""
    ly = kdb.Layout()
    ly.read(str(path))
    out = []
    for c in sorted(ly.each_cell(), key=lambda c: (c.is_ghost_cell(), c.name)):
        if c.is_ghost_cell():
            out.append(f"uses {c.name}")
            continue
        out.append(f"cell {c.name}  (dbu {ly.dbu:g} um)")
        out += [f"  meta {m.name} = {m.value}" for m in sorted(c.each_meta_info(), key=lambda m: m.name)]
        for info, li in sorted((str(ly.get_info(li)), li) for li in ly.layer_indexes()):
            shapes = sorted(s.to_s() for s in c.shapes(li).each())
            out += [f"  {info}  {s}" for s in shapes]
        insts = []
        for i in c.each_inst():
            a = f"  array {i.na}x{i.nb} a={i.a} b={i.b}" if i.is_regular_array() else ""
            p = f"  {dict(ly.properties(i.prop_id))}" if i.has_prop_id() else ""
            insts.append(f"  inst {i.cell.name}  {i.cplx_trans}{a}{p}")
        out += sorted(insts)
    return "\n".join(out) + "\n"
