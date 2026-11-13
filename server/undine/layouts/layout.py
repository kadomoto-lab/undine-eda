"""Server-side layout database and rendering (KLayout).

One LayoutDB per library. The browser never receives the layout database:
it gets raster tiles drawn by KLayout's LayoutView, the layer list (names,
colours, swatches) and, for selected user shapes, their outlines. PCell
instances are opaque (cell name, parameters, bounding box only).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import klayout.db as kdb
import klayout.lay as klay

from undine.layouts import layoutstore
from undine.store import oplog
from undine.store.oplog import OpLog
from undine.store.project import Library
from undine.pdks.pdk import Pdk

TILE_PX = 512
CACHE_BYTES = 256 << 20
BACKGROUND = "#071217"  # = --bg in web/src/styles.css
DISK_CHECK_S = 1.0      # a LayoutDB looks at its files on disk at most this often (check_disk), a save always
RELOAD_REASON = "reload: the unsaved layout edits were dropped and the cell files read again"
# instance property: a device's own layout parameters (JSON: fingers, tap ...), kept with the layout, not the schematic
LAYOUT_PROP = "undine_layout"
# instance property: the tool a designer placed a bound piece of a device with (place_pcell, place_array): schematic-
# driven layout leaves it as it is (sdl.Sdl._own_layout)
OWN_PROP = "undine_own"


def um_text(v: int, dbu: float) -> str:
    """A coordinate (dbu) in um, exactly as the grid has it: no exponent, no trailing zeros (an instance id's origin)."""
    s = f"{v * dbu:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


ID_RE = re.compile(r"(.+)@([-+]?[0-9.]+),([-+]?[0-9.]+)(#\d+)?")   # an id as written: any decimals of the origin


def instance_ids(layout: kdb.Layout, cell: kdb.Cell) -> list[tuple[kdb.Instance, str]]:
    """The cell's top-level instances with their ids: child@x,y (the origin in um exactly, um_text), #2 ... for more
    of that child at one origin, in the cell's order (LayoutDB.instances, connectivity.Nets)."""
    out, seen = [], {}
    for inst in cell.each_inst():
        ci = inst.cell_inst
        iid = f"{layout.cell(inst.cell_index).name}@{um_text(ci.trans.disp.x, layout.dbu)},{um_text(ci.trans.disp.y, layout.dbu)}"
        seen[iid] = seen.get(iid, 0) + 1
        out.append((inst, iid if seen[iid] == 1 else f"{iid}#{seen[iid]}"))
    return out


class DiskConflict(ValueError):
    """A save would write over cell files that changed on disk (git) while the layout had unsaved edits."""


class CellClash(ValueError):
    """An import would overwrite cells the library has with other content (LayoutDB.import_cells): nothing imported."""

    def __init__(self, lib: str, cells: list[str]):
        super().__init__(f"{lib} already has {', '.join(cells)} with other content than the file's")
        self.cells = cells


class _Versions:
    """Layout versions are never given twice, also not across a database loaded again (a server restart, a changeset
    step put back, an import): the browser caches tiles under `?v=<version>` for an hour (api.py: tile) and the editor
    keys its bitmaps by it, so a repeated number would show what an earlier database drew under it. A database starts
    above every version of this process and at the time in microseconds (above those of an earlier process), and
    counts up by one per change."""

    def __init__(self):
        self.last = 0
        self.lock = threading.Lock()

    def first(self) -> int:
        with self.lock:
            self.last = max(self.last + 1, time.time_ns() // 1000)
            return self.last

    def after(self, version: int) -> int:
        with self.lock:
            self.last = max(self.last, version + 1)
            return version + 1


VERSIONS = _Versions()


def _hexcolor(c: int) -> str:
    return f"#{c & 0xffffff:06x}"


class LayoutDB:
    """The layout of a library in memory: the cell files as read (or last saved), with the edits since, which are a
    journal in the operation log until a save writes them (see _replay_pending).

    Files on disk change under it: a git checkout, merge, pull or rebase, or another program. check_disk notices it
    from the files' fingerprints (modification time, size, inode) and the log's journal: without unsaved edits the
    layout is read again (as when the library is opened); with unsaved edits it is a conflict (disk_conflict,
    disk_cells), and save refuses, since it would write the cells as they were before over the changed files, until
    reload() drops the edits and reads the files."""

    def __init__(self, lib: Library, pdk: Pdk | None, log: OpLog | None = None):
        self.lib = lib
        self.pdk = pdk
        self.log = log
        self.lock = threading.RLock()
        self.version = VERSIONS.first()
        self.dirty = False
        self.lib_resolver = None  # name -> LayoutDB, set by the server (copying cells between libraries)
        self._meta: dict = {"op": "edit", "args": {}, "actor": "user"}
        self.cache: OrderedDict[tuple, bytes] = OrderedDict()
        self.cache_bytes = 0
        self._disk_files: dict[str, tuple] = {}   # cell -> fingerprint of its file, as read or saved
        self._disk_log: tuple | None = None       # fingerprint of the log when its journal was last compared
        self._journal: tuple | None = None        # the log's journal this layout holds (_mark); None: not journaled
        self._dropped_upto = 0                    # seq of the latest reload record: undo never goes behind it
        self._conflict: list[str] | None = None   # cells changed on disk under unsaved edits (save refuses)
        self._seen: tuple = ({}, None)            # the fingerprints of the last look (files, log): nothing new since
        self._held: dict[str, tuple] = {}         # cell -> a fingerprint its file had while holding what was read
        self.reads = 0                            # how often the files were read again since opening (editors redraw)
        self._disk_checked = time.monotonic()
        self._read_files()
        self.reads = 0

    @contextmanager
    def recording(self, op: str, args: dict, actor: str = "user", reason: str | None = None,
                  changeset: str | None = None):
        """Names the operation the following edits belong to (written to the operation log)."""
        with self.lock:
            self._meta = {"op": op, "args": args, "actor": actor, "reason": reason, "changeset": changeset}
            try:
                yield
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    # ------------------------------------------------------------------ setup

    def _load(self) -> None:
        """Reads the files into a new view; the database changes only once that worked (a file that cannot be read
        leaves the layout as it was)."""
        lv = klay.LayoutView(True)
        for k, v in {
            "background-color": BACKGROUND,
            "grid-visible": "false",
            "text-visible": "true",
            "inst-color": "#8fa9b1",
            "text-color": "#c8dde2",
            "bitmap-oversampling": "1",
            "drop-small-cells": "false",
        }.items():
            lv.set_config(k, v)
        cv = lv.create_layout(True)
        ly = lv.cellview(cv).layout()
        ly.dbu = 0.001
        self.lib.read_layout(ly)              # a PDK's GDS, or the library's cell files (layoutstore.py)
        saved = layoutstore.snapshot(ly) if not self.lib.layout_file else {}   # what the cell files hold
        if self.pdk and self.pdk.lyp:
            lv.load_layer_props(str(self.pdk.lyp), cv, True)
        else:
            lv.add_missing_layers()
        lv.max_hier_levels = 99
        self.lv, self.cv, self.saved = lv, cv, saved
        self.cur_cell = None
        self.cur_mask = None
        self.cur_depth = None
        self._icons: dict[int, str] = {}
        self._layer_names: list[tuple[str, str, int, int]] | None = None   # _layer_table

    def _read_files(self, drop: bool = False) -> None:
        """Reads the files and replays the log's journal (_replay_pending), as opening the library does, under a new
        version, so tiles and editors draw again. `drop`: the journal is not replayed but ended (reload)."""
        files, logfp = self._fingerprint(), self._log_stat()   # before reading: a change meanwhile shows next time
        self._load()
        self.version = VERSIONS.after(self.version)
        self.dirty = False
        self.cache.clear()
        self.cache_bytes = 0
        if drop:
            self._log(None, None, op="save", args={"reload": True}, reason=RELOAD_REASON,
                      sha=oplog.sha(json.dumps(sorted(self.saved.items()))))
        self._replay_pending()
        self._disk_files, self._disk_log, self._conflict = files, logfp, None
        self._seen, self._held = (files, logfp), {}
        self.reads += 1

    @property
    def layout(self) -> kdb.Layout:
        return self.lv.cellview(self.cv).layout()

    def cell(self, name: str) -> kdb.Cell:
        c = self.layout.cell(name)
        if c is None:
            raise KeyError(f"no cell {name}")
        return c

    def _nodes(self):
        it = self.lv.begin_layers()
        while not it.at_end():
            node = it.current()
            if not node.has_children():
                yield it.dup(), node
            it.next()

    # ----------------------------------------------------------------- layers

    def cell_exists(self, name: str) -> bool:
        return self.layout.cell(name) is not None

    def regions(self, cell_name: str) -> dict[tuple[int, int], kdb.Region]:
        """The cell's geometry flattened and merged, per (layer, datatype)."""
        with self.lock:
            c = self.cell(cell_name)
            out = {}
            for li in self.layout.layer_indexes():
                r = kdb.Region(c.begin_shapes_rec(li))
                if not r.is_empty():
                    r.merge()
                    i = self.layout.get_info(li)
                    out[(i.layer, i.datatype)] = r
            return out

    def layers(self, cell_name: str | None = None) -> list[dict]:
        with self.lock:
            used = set()
            if cell_name:
                c = self.cell(cell_name)
                for li in self.layout.layer_indexes():
                    if not c.bbox_per_layer(li).empty():
                        used.add(li)
            out = []
            for i, (it, node) in enumerate(self._nodes()):
                li = node.layer_index()
                if i not in self._icons:
                    pb = self.lv.icon_for_layer(it, 28, 16, 2, 0, True)
                    self._icons[i] = base64.b64encode(pb.to_png_data()).decode()
                out.append({
                    "id": i,
                    "name": node.name or node.source_name or f"{node.source_layer}/{node.source_datatype}",
                    "source": f"{node.source_layer}/{node.source_datatype}",
                    "layer": node.source_layer, "datatype": node.source_datatype,
                    "fill": _hexcolor(node.eff_fill_color(True)),
                    "frame": _hexcolor(node.eff_frame_color(True)),
                    "visible": node.visible,
                    "valid": node.valid,
                    "used": li >= 0 and li in used,
                    "icon": self._icons[i],
                })
            return out

    def _layer_table(self, fresh: bool = False) -> list[tuple[str, str, int, int]]:
        """(name, source, layer, datatype) of each layer view, by id, as layers() names them. Read once per load: the
        layer list is set when the files are read, and walking it (as layers() does, colours and all) for every shape
        an agent draws made a PDK's few hundred layers the cost of each edit."""
        with self.lock:
            if self._layer_names is None or fresh:
                self._layer_names = [(node.name or node.source_name or f"{node.source_layer}/{node.source_datatype}",
                                      f"{node.source_layer}/{node.source_datatype}", node.source_layer, node.source_datatype)
                                     for _, node in self._nodes()]
                self._layer_found: dict[str, int] = {}               # layer_id's answers for this table
            return self._layer_names

    def layer_source(self, lid: int) -> str:
        """The "layer/datatype" of a layer view id (layer_id)."""
        return self._layer_table()[lid][1]

    def layer_id(self, spec: str, cell_name: str | None = None) -> int:
        """LayerView id for "68/20" or a layer name ("met1", "met1.drawing")."""
        spec = spec.strip()
        if cell_name:
            self.cell(cell_name)
        low = spec.lower()
        for fresh in (False, True):                  # not found: a layer view added since it was read, look again
            ls = self._layer_table(fresh)
            if spec in self._layer_found:
                return self._layer_found[spec]
            hits = [i for i, (_, source, _, _) in enumerate(ls) if source == spec][:1] or \
                [i for i, (name, *_) in enumerate(ls)
                 if name.lower().split(" - ")[0] in (low, low + ".drawing") or name.lower().startswith(low + ".drawing")]
            if len(hits) == 1:
                self._layer_found[spec] = hits[0]
                return hits[0]
        raise KeyError(f"unknown layer {spec!r}: use 'layer/datatype' (e.g. 68/20) or a name like met1")

    def foreign_overlaps(self, cell_name: str, inst_name: str) -> list[dict]:
        """Shapes drawn directly in the cell that change a bound device's transistor: poly of the cell over the device's
        diffusion (makes the gate wider / longer than the PCell drew it, so LVS extracts a different L and W), or
        diffusion of the cell under the device's poly. Boxes in dbu."""
        with self.lock:
            top = self.cell(cell_name)
            inst = next((i for i in top.each_inst() if i.property("undine_inst") == inst_name), None)
            if inst is None:
                raise KeyError(f"no layout instance bound to {inst_name}")
            child = self.layout.cell(inst.cell_index)
            tr = inst.cell_inst.trans

            def dev(l, d):
                li = self.layout.find_layer(l, d)
                return kdb.Region(child.begin_shapes_rec(li)).transformed(tr) if li is not None else kdb.Region()

            def own(l, d):
                li = self.layout.find_layer(l, d)
                return kdb.Region(top.shapes(li)) if li is not None else kdb.Region()

            out = []
            prof = self.pdk.profile if self.pdk else None
            gl, al = (prof.layers.get(prof.gate_layers[0]), prof.layers.get(prof.gate_layers[1])) if prof and prof.layers else (None, None)
            gl, al = gl or (66, 20), al or (65, 20)
            for what, mine, theirs in (("poly of this cell over the device's diffusion", own(*gl), dev(*al)),
                                       ("diffusion of this cell under the device's poly", own(*al), dev(*gl))):
                hit = (mine & theirs)
                for p in hit.each():
                    b = p.bbox()
                    # a poly that merely continues the device's own poly (same width) only counts where it adds area
                    out.append({"kind": what, "where": [b.left, b.bottom, b.right, b.top]})
            # keep only overlaps that are not already inside the device's own poly+diffusion channel
            own_channel = dev(*gl) & dev(*al)
            extra = []
            for o in out:
                box = kdb.Region(kdb.Box(*o["where"]))
                if not (box - own_channel).is_empty():
                    extra.append(o)
            return extra

    def integrity(self, cell_name: str) -> list[dict]:
        """Foreign poly / diffusion over the transistor area of every device bound to a schematic instance."""
        with self.lock:
            out = []
            for i in self.named_instances(cell_name):
                for o in self.foreign_overlaps(cell_name, i["name"]):
                    out.append({"instance": i["name"], "problem": o["kind"], "where": o["where"]})
            return out

    def _layer_index_for(self, lid: int) -> int:
        ls = self._layer_table()
        if 0 <= lid < len(ls) and ls[lid][2] >= 0 and ls[lid][3] >= 0:      # a view of layer / datatype: as the node maps it
            li = self.layout.find_layer(ls[lid][2], ls[lid][3])
            return li if li is not None else self.layout.layer(ls[lid][2], ls[lid][3])
        for i, (_, node) in enumerate(self._nodes()):
            if i == lid:
                li = node.layer_index()
                if li < 0:
                    li = self.layout.layer(node.source_layer, node.source_datatype)
                return li
        raise KeyError(f"no layer {lid}")

    def _visible_layer_indexes(self, mask: str | None) -> set[int]:
        vis = set()
        for i, (_, node) in enumerate(self._nodes()):
            if node.layer_index() >= 0 and (mask is None or _bit(mask, i)):
                vis.add(node.layer_index())
        return vis

    # -------------------------------------------------------------- rendering

    def info(self, cell_name: str) -> dict:
        with self.lock:
            c = self.cell(cell_name)
            b = c.bbox()
            return {
                "cell": cell_name,
                "dbu": self.layout.dbu,
                "bbox": [b.left, b.bottom, b.right, b.top] if not b.empty() else [0, 0, 0, 0],
                "version": self.version,
                "dirty": self.dirty,
                "tile_px": TILE_PX,
                "children": sorted({self.layout.cell(i.cell_index).name for i in c.each_inst()}),
                "is_pcell": c.is_pcell_variant(),
            }

    def _prepare(self, cell_name: str, mask: str | None, depth: int) -> None:
        if cell_name != self.cur_cell:
            self.lv.select_cell(self.cell(cell_name).cell_index(), self.cv)
            self.cur_cell = cell_name
        if mask != self.cur_mask:
            for i, (it, node) in enumerate(self._nodes()):
                v = True if mask is None else _bit(mask, i)
                if node.visible != v:
                    node.visible = v
            self.cur_mask = mask
        if depth != self.cur_depth:
            self.lv.min_hier_levels = 0
            self.lv.max_hier_levels = depth
            self.cur_depth = depth

    def tile(self, cell_name: str, level: int, ix: int, iy: int, mask: str | None, depth: int) -> bytes:
        with self.lock:
            key = (self.version, cell_name, level, ix, iy, mask, depth)   # under the lock: an edit may be running
            png = self.cache.get(key)
            if png is not None:
                self.cache.move_to_end(key)
                return png
            self._prepare(cell_name, mask, depth)
            dbu = self.layout.dbu
            w = TILE_PX * math.ldexp(1.0, level)  # dbu
            box = kdb.DBox(ix * w * dbu, iy * w * dbu, (ix + 1) * w * dbu, (iy + 1) * w * dbu)
            pb = self.lv.get_pixels_with_options(TILE_PX, TILE_PX, 1, 1, 0, box)
            png = pb.to_png_data()
            self.cache[key] = png
            self.cache_bytes += len(png)
            while self.cache_bytes > CACHE_BYTES and self.cache:
                _, old = self.cache.popitem(last=False)
                self.cache_bytes -= len(old)
            return png

    def render(self, cell_name: str, box: list[float] | None = None, size: int = 1024, layers: list[str] | None = None,
               depth: int = 99, solid: bool = False, overlays: list[tuple[int, list, bool]] = (),
               dim: bool = False) -> tuple[bytes, kdb.DBox, int, int]:
        """One picture of a cell for an agent: the whole cell (or a box in um), optionally only some layers (names like
        met1, or 68/20), with a scale ruler. solid: every layer filled solid instead of its stipple; overlays: (colour,
        shapes in um (DBox, DPolygon, DEdgePair), filled) drawn on top as markers (highlighted nets, DRC markers), with
        the layers dimmed under them when `dim`. The view's styles are put back after. Returns (PNG, the box drawn in
        um, width, height)."""
        with self.lock:
            c = self.cell(cell_name)
            mask = None
            if layers:
                ids = {self.layer_id(spec, cell_name) for spec in layers}
                n = sum(1 for _ in self._nodes())
                mask = "".join(f"{sum(1 << b for b in range(4) if k * 4 + b in ids):x}" for k in range((n + 3) // 4))
            bb = kdb.DBox(*box) if box else c.dbbox()
            if bb.empty() or bb.width() <= 0 or bb.height() <= 0:
                raise KeyError(f"nothing to draw in {cell_name}" + (" inside that box" if box else ": the cell is empty"))
            if not box:
                pad = max(bb.width(), bb.height()) * 0.04
                bb = bb.enlarged(pad, pad)
            size = max(64, min(int(size), 2048))
            w, h = (size, max(32, round(size * bb.height() / bb.width()))) if bb.width() >= bb.height() else \
                   (max(32, round(size * bb.width() / bb.height())), size)
            self._prepare(cell_name, mask, depth)
            for k, v in (("grid-visible", "true"), ("grid-show-ruler", "true"), ("grid-style0", "invisible"),
                         ("grid-style1", "invisible"), ("grid-style2", "invisible"), ("grid-ruler-color", "#b6cbd1")):
                self.lv.set_config(k, v)
            styles = [(node.dither_pattern, node.fill_brightness, node.frame_brightness) for _, node in self._nodes()]
            marks = []
            try:
                for _, node in self._nodes():
                    if solid:
                        node.dither_pattern = 0
                    if dim:
                        node.fill_brightness, node.frame_brightness = -110, -90
                for rgb, shapes, filled in overlays:
                    for sh in shapes:
                        m = klay.Marker(self.lv)
                        m.set(sh)
                        m.color = m.frame_color = rgb
                        m.dither_pattern = 0 if filled else 1
                        m.line_width = 1 if filled else 2
                        m.halo = 0 if filled else 1
                        marks.append(m)
                png = self.lv.get_pixels_with_options(w, h, 1, 1, 0, bb).to_png_data()
            finally:
                for m in marks:
                    m._destroy()
                for (_, node), (dp, fb, rb) in zip(self._nodes(), styles):
                    if (node.dither_pattern, node.fill_brightness, node.frame_brightness) != (dp, fb, rb):
                        node.dither_pattern, node.fill_brightness, node.frame_brightness = dp, fb, rb
                self.lv.set_config("grid-visible", "false")
            return png, bb, w, h

    # -------------------------------------------------------------- selection

    def _shape_obj(self, s: kdb.Shape):
        if s.is_box():
            return "box", s.box
        if s.is_path():
            return "path", s.path
        if s.is_text():
            return "text", s.text
        if s.is_polygon() or s.is_simple_polygon():
            return "polygon", s.polygon
        return None, None

    def _shape_desc(self, li: int, s: kdb.Shape) -> dict | None:
        kind, obj = self._shape_obj(s)
        if kind is None:
            return None
        b = s.bbox()
        info = self.layout.get_info(li)
        d = {
            "kind": "shape", "type": kind,
            "ref": {"k": "s", "l": li, "t": kind, "g": str(obj), "b": [b.left, b.bottom, b.right, b.top]},
            "layer": f"{info.layer}/{info.datatype}", "layer_name": info.name,
            "bbox": [b.left, b.bottom, b.right, b.top],
        }
        if s.prop_id:
            d["ref"]["p"] = s.prop_id
        if kind == "text":
            d["text"] = obj.string
            d["outline"] = [[b.left, b.bottom, b.right, b.bottom, b.right, b.top, b.left, b.top]]
        else:
            poly = s.polygon
            d["outline"] = [_pts(poly.each_point_hull())] + [
                _pts(poly.each_point_hole(h)) for h in range(poly.holes())]
            if kind == "path":
                d["width"] = obj.width
                d["spine"] = _pts(obj.each_point())
        return d

    def _inst_desc(self, inst: kdb.Instance) -> dict:
        b = inst.bbox()
        ci = inst.cell_inst
        cell = self.layout.cell(inst.cell_index)
        d = {
            "kind": "inst", "type": "instance",
            "ref": {"k": "i", "g": str(ci), "c": cell.name, "b": [b.left, b.bottom, b.right, b.top]},
            "cell": cell.basic_name() if cell.is_pcell_variant() else cell.name,
            "trans": str(ci.trans),
            "bbox": [b.left, b.bottom, b.right, b.top],
            "outline": [[b.left, b.bottom, b.right, b.bottom, b.right, b.top, b.left, b.top]],
        }
        if ci.is_regular_array():
            d["array"] = {"na": ci.na, "nb": ci.nb, "a": str(ci.a), "b": str(ci.b)}
        if inst.prop_id:
            d["ref"]["p"] = inst.prop_id
        nm = inst.property("undine_inst")
        if nm:
            d["inst_name"] = nm
        if cell.is_pcell_variant():
            # parameters only: the PCell implementation stays on the server
            d["pcell"] = {"library": cell.library().name() if cell.library() else "",
                          "params": {k: _jsonable(v) for k, v in inst.pcell_parameters_by_name().items()}}
        return d

    def select(self, cell_name: str, box: list[float], point: bool, mask: str | None,
               tol: float) -> list[dict]:
        """Top-level shapes and instances of the cell under a point or inside a box."""
        with self.lock:
            c = self.cell(cell_name)
            vis = self._visible_layer_indexes(mask)
            x1, y1, x2, y2 = box
            if point:
                px, py = x1, y1
                t = max(1, int(tol))
                qb = kdb.Box(int(px - t), int(py - t), int(px + t), int(py + t))
                pt = kdb.Point(int(px), int(py))
                hits: list[tuple[float, dict]] = []
                for li in sorted(vis):
                    for s in c.shapes(li).each_touching(qb):
                        kind, obj = self._shape_obj(s)
                        if kind is None:
                            continue
                        if kind == "text":
                            score = 0.5
                        else:
                            poly = s.polygon
                            score = 0.0 if poly.inside(pt) else 1.0
                        d = self._shape_desc(li, s)
                        if d:
                            area = max(1.0, float(s.bbox().width()) * s.bbox().height())
                            hits.append((score * 1e30 + area, d))
                for inst in c.each_touching_inst(qb):
                    b = inst.bbox()
                    area = max(1.0, float(b.width()) * b.height())
                    hits.append((1e29 + area, self._inst_desc(inst)))
                hits.sort(key=lambda h: h[0])
                return [hits[0][1]] if hits else []
            qb = kdb.Box(int(min(x1, x2)), int(min(y1, y2)), int(max(x1, x2)), int(max(y1, y2)))
            out = []
            for li in sorted(vis):
                for s in c.shapes(li).each_overlapping(qb):
                    if qb.contains(s.bbox().p1) and qb.contains(s.bbox().p2):
                        d = self._shape_desc(li, s)
                        if d:
                            out.append(d)
                    if len(out) > 5000:
                        return out
            for inst in c.each_overlapping_inst(qb):
                b = inst.bbox()
                if qb.contains(b.p1) and qb.contains(b.p2):
                    out.append(self._inst_desc(inst))
            return out

    def _find(self, c: kdb.Cell, ref: dict):
        qb = kdb.Box(*[int(v) for v in ref["b"]]) if "b" in ref else None
        if ref["k"] == "s":
            li = int(ref["l"])
            for s in (c.shapes(li).each_touching(qb) if qb else c.shapes(li).each()):
                kind, obj = self._shape_obj(s)
                if kind == ref["t"] and str(obj) == ref["g"] and ("p" not in ref or s.prop_id == ref["p"]):
                    return s
        else:
            key = ref.get("key")
            for inst in (c.each_touching_inst(qb) if qb else c.each_inst()):
                if ((str(inst.cell_inst) == ref["g"] or (key is not None and _inst_key(inst.cell_inst) == key))
                        and self.layout.cell(inst.cell_index).name == ref["c"]
                        and ("p" not in ref or inst.prop_id == ref["p"])):
                    return inst
        return None

    # ---------------------------------------------------------------- editing

    def _apply(self, c: kdb.Cell, op: tuple):
        kind = op[0]
        if kind == "ins":
            _, li, obj = op[:3]
            shape = c.shapes(li).insert(obj)
            if len(op) > 3 and op[3]:
                shape.prop_id = op[3]
            return self._shape_desc(li, shape)
        if kind == "del":
            _, li, obj = op[:3]
            b = obj.bbox()
            ref = {"k": "s", "l": li, "t": _objtype(obj), "g": str(obj), "b": [b.left, b.bottom, b.right, b.top]}
            if len(op) > 3:
                ref["p"] = op[3]
            s = self._find(c, ref)
            if s is not None:
                c.shapes(li).erase(s)
            return None
        if kind == "iins":
            pid = op[2] if len(op) > 2 else 0
            return self._inst_desc(c.insert(op[1], pid) if pid else c.insert(op[1]))
        if kind == "idel":
            ci = op[1]
            b = ci.bbox(self.layout)
            ref = {"k": "i", "g": str(ci), "key": _inst_key(ci), "c": self.layout.cell(ci.cell_index).name}
            if len(op) > 2:
                ref["p"] = op[2]
            if not b.empty():  # an empty child has no box to search in: scan all instances instead
                ref["b"] = [b.left, b.bottom, b.right, b.top]
            inst = self._find(c, ref)
            if inst is not None:
                inst.delete()
            return None
        raise ValueError(kind)

    @staticmethod
    def _inverse(op: tuple) -> tuple:
        return {"ins": "del", "del": "ins", "iins": "idel", "idel": "iins"}[op[0]], *op[1:]

    def _writable(self) -> None:
        """Every edit refuses a read-only library (a PDK's, a dependency's, a changeset's baseline) before it changes
        anything, whatever route it comes by."""
        if self.lib.readonly:
            raise PermissionError("library is read-only")

    def _commit(self, cell_name: str, ops: list[tuple]) -> list[dict]:
        self._writable()
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        c = self.cell(cell_name)
        created = []
        for op in ops:
            d = self._apply(c, op)
            if d:
                created.append(d)
        if ops:
            self._changed()
            self._log(cell_name, [self._op_effect(cell_name, op) for op in ops])
        return created

    # ------------------------------------------------- operation log (see oplog.py)

    def _log(self, cell_name: str | None, effects: list[dict] | None, **extra) -> None:
        if self.log is not None:
            before = self._log_stat()
            rec = self.log.append({**self._meta, "cell": cell_name, "view": "layout", "sha": None, "effects": effects,
                                   **extra})
            if self._journal is not None:              # the journal this layout holds goes on with the record
                self._journal = (_rid(rec), ()) if rec.get("op") == "save" else \
                    (self._journal[0], self._journal[1] + (_rid(rec),))
                if before == self._disk_log:           # nobody wrote the log since it was compared: still the same
                    self._disk_log = self._log_stat()

    def _layer_key(self, li: int) -> str:
        i = self.layout.get_info(li)
        return f"{i.layer}/{i.datatype}"

    def _op_effect(self, cell_name: str, op: tuple) -> dict:
        """A (shape|instance) insert/delete as a JSON effect: portable and invertible."""
        kind = op[0]
        do = "add" if kind in ("ins", "iins") else "del"
        if kind in ("ins", "del"):
            e = {"k": "s", "do": do, "cell": cell_name, "layer": self._layer_key(op[1]),
                 "t": _objtype(op[2]), "s": str(op[2])}
            if len(op) > 3 and op[3]:
                e["props"] = [[k, _jsonable(v)] for k, v in _props(self.layout.properties(op[3])).items()]
            return e
        ci = op[1]
        pid = op[2] if len(op) > 2 else 0
        e = {"k": "i", "do": do, "cell": cell_name, "child": self.layout.cell(ci.cell_index).name,
             "trans": str(ci.trans)}
        if ci.is_complex():
            e["cplx_trans"] = str(ci.cplx_trans)
        if ci.is_regular_array():
            e["array"] = {"na": ci.na, "nb": ci.nb, "a": str(ci.a), "b": str(ci.b)}
        if pid:
            props = _props(self.layout.properties(pid))
            nm = props.get("undine_inst")
            if nm:
                e["inst"] = nm
            e["props"] = [[k, _jsonable(v)] for k, v in props.items()]
        return e

    def _effect_op(self, e: dict, backwards: bool) -> tuple:
        do = e["do"] if not backwards else {"add": "del", "del": "add"}[e["do"]]
        if e["k"] == "s":
            l, d = (int(x) for x in e["layer"].split("/"))
            li = self.layout.layer(l, d)
            cls = {"box": kdb.Box, "path": kdb.Path, "text": kdb.Text, "polygon": kdb.Polygon}[e["t"]]
            pid = self.layout.properties_id(dict(e["props"])) if e.get("props") else 0
            return ("ins" if do == "add" else "del", li, cls.from_s(e["s"]), pid)
        child = self.layout.cell(e["child"])
        if child is None:
            raise KeyError(f"cell {e['child']} is missing")
        tr = kdb.ICplxTrans.from_s(e["cplx_trans"]) if "cplx_trans" in e else kdb.Trans.from_s(e["trans"])
        if "array" in e:
            a = e["array"]
            ci = kdb.CellInstArray(child.cell_index(), tr, kdb.Vector.from_s(a["a"]), kdb.Vector.from_s(a["b"]),
                                   a["na"], a["nb"])
        else:
            ci = kdb.CellInstArray(child.cell_index(), tr)
        props = dict(e.get("props", []))
        if e.get("inst"):
            props["undine_inst"] = e["inst"]
        pid = self.layout.properties_id(props) if props else 0
        return ("iins" if do == "add" else "idel", ci, pid)

    def apply_effects(self, effects: list[dict], backwards: bool = False, strict: bool = False) -> None:
        """Applies logged effects (without logging them again). strict: fail if something to delete is missing."""
        self._writable()
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        seq = list(reversed(effects)) if backwards else list(effects)
        for e in seq:
            if e["k"] == "c":
                if (e["do"] == "add") != backwards and self.layout.cell(e["cell"]) is None:
                    self._make_cell(e)
                elif e["do"] == "del" and e.get("drop") and not backwards and (c := self.layout.cell(e["cell"])) is not None:
                    if strict and e.get("kept") and (c.parent_cells() or (
                            _content(self.layout, c) != e["sha"] if e.get("sha") else not c.is_empty())):
                        # a drop with its content recorded comes after the deletion of that content (drop_cells), or
                        # of what was drawn after the cell was made or brought in (a restore's remade cell: empty, or
                        # as brought in, sha): anything else in it, or placing it, was not recorded
                        raise ValueError(f"layout cell {e['cell']} is not as recorded or is placed (changed outside "
                                         "the recorded operations?)")
                    self.layout.delete_cell(c.cell_index())   # only a checkpoint restore removes a cell (changeset.py)
                continue  # otherwise removing a cell again is never done automatically
            op = self._effect_op(e, backwards)
            if strict and op[0] in ("del", "idel") and not self._exists(self.cell(e["cell"]), op):
                raise KeyError(f"cannot apply: the {e['k'] == 's' and 'shape' or 'instance'} to remove is not in "
                               f"{e['cell']} (the design changed since?)")
            self._apply(self.cell(e["cell"]), op)
        self._changed()

    def _exists(self, c: kdb.Cell, op: tuple) -> bool:
        if op[0] == "del":
            obj = op[2]
            b = obj.bbox()
            return self._find(c, {"k": "s", "l": op[1], "t": _objtype(obj), "g": str(obj),
                                  "b": [b.left, b.bottom, b.right, b.top]}) is not None
        ci = op[1]
        b = ci.bbox(self.layout)
        ref = {"k": "i", "g": str(ci), "key": _inst_key(ci), "c": self.layout.cell(ci.cell_index).name}
        if not b.empty():
            ref["b"] = [b.left, b.bottom, b.right, b.top]
        return self._find(c, ref) is not None

    def replay(self, effects: list[dict], meta: dict) -> None:
        """Applies effects recorded elsewhere (a changeset, or the inverse of one) and logs them as `meta`.
        All-or-nothing: if one effect cannot be applied the ones before it are taken back."""
        self._writable()
        with self.lock:
            done: list[dict] = []
            try:
                for e in effects:
                    self.apply_effects([e], False, strict=True)
                    done.append(e)
            except Exception:
                self.apply_effects(done, True)
                raise
            cells = [e.get("cell") for e in effects if e.get("cell")]
            self._meta = meta
            try:
                self._log(cells[0] if cells else None, effects)
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    def _make_cell(self, e: dict) -> None:
        src = e.get("src") or {}
        if src.get("pcell"):
            from undine.pdks import pcell
            path, name, _ = pcell.generate(self.pdk, src["pcell"], src["params"])
            if name != e["cell"]:
                raise ValueError(f"PCell {src['pcell']} now generates {name}, not {e['cell']}")
            self._import_file(path)
        elif src.get("lib") and self.lib_resolver:
            self._copy_cell_from(self.lib_resolver(src["lib"]).layout, e["cell"])
        elif src.get("stdcell"):
            if (hit := stdcell_layout(self.pdk, e["cell"])) is None:
                raise KeyError(f"no standard cell {e['cell']} in the PDK")
            self._copy_cell_from(hit[1], e["cell"])
        else:
            c = self.layout.create_cell(e["cell"])
            for name, value in e.get("meta") or []:        # a cell drop_cells took out: its meta comes back with it
                c.add_meta_info(kdb.LayoutMetaInfo(name, value, None, True))

    def _replay_pending(self) -> None:
        """Re-applies the journal, the layout operations the cell files lack, so unsaved edits survive a restart: those
        after the last layout save or git merge record. A merge record counts as a save (the cell files are then the
        merge of both branches' saved files); when it carried a branch's journal over, it lists those records, which
        come first, in "replay" (vcs/logmerge.py, whose journal() reads the same records). A reload is recorded as a
        layout save ({"args": {"reload": true}}, reload()): the edits before it were dropped, not written. In a
        changeset's working copy the log is read from its floor (oplog.py): the project's unsaved edits when the
        changeset was opened are a journal of the project's, not of the working copy, which starts from the files."""
        if not self._journaled():
            self._journal = None
            return
        recs = self.log.read(limit=10**9)
        last, pending = _journal(recs)
        self._journal = _mark(recs, last, pending)
        self._dropped_upto = max((r["seq"] for r in recs if _is_reload(r)), default=0)
        byseq = {r["seq"]: r for r in recs if r.get("view") == "layout"}
        n = 0
        for seq, r in pending:
            try:
                if r is None:
                    raise KeyError("the merge record lists it, but there is no layout operation of that seq before it")
                if r["op"] == "undo":
                    self.apply_effects(byseq[r["of"]]["effects"], True)
                elif r["op"] == "redo":
                    self.apply_effects(byseq[r["of"]]["effects"], False)
                elif r.get("effects"):
                    self.apply_effects(r["effects"], False)
                n += 1
            except Exception as ex:  # a damaged journal entry must not make the library unusable
                print(f"undine: cannot replay layout operation {seq} of {self.lib.name}: {ex}", file=sys.stderr)
        if n:
            self.dirty = True

    def _changed(self) -> None:
        """Every change of the layout goes through here: caches keyed on `version` (the drawing tools' extracted nets,
        agent_tools) are right only while no edit bypasses it."""
        self.version = VERSIONS.after(self.version)
        self.dirty = True
        self.cache.clear()
        self.cache_bytes = 0

    # ------------------------------------------------- the files on disk changed (git checkout, merge, pull ...)

    def _journaled(self) -> bool:
        """Whether unsaved edits are a journal in the log, replayed on opening (not in a read-only library)."""
        return self.log is not None and not self.lib.readonly

    def _log_stat(self) -> tuple | None:
        return _stat(self.log.path) if self._journaled() else None

    def _fingerprint(self) -> dict[str, tuple]:
        """{cell: (mtime_ns, size, inode)} of the cell files ("": the one file of a PDK's layout)."""
        if self.lib.layout_file:
            st = _stat(self.lib.path / self.lib.layout_file)
            return {"": st} if st else {}
        return {layoutstore.cell_name(p): st for p in layoutstore.cell_files(self.lib.path) if (st := _stat(p))}

    def _holds(self, cell: str) -> bool:
        """Whether a cell file that was touched still holds what was read or saved (git stash and pop, a copy)."""
        if cell not in self.saved:
            return False
        try:
            ly = kdb.Layout()
            ly.read(str(self.lib.path / layoutstore.file_name(cell)))
            c = ly.cell(cell)
            return c is not None and not c.is_ghost_cell() and layoutstore.cell_sha(ly, c) == self.saved[cell]
        except Exception:
            return False

    def _journal_mark(self) -> tuple | None:
        """The journal of the log as it is on disk now (_mark)."""
        if self._journal is None:
            return None
        recs = self.log.read(limit=10**9)
        return _mark(recs, *_journal(recs))

    def check_disk(self, force: bool = False) -> None:
        """Compares the cell files (and the log's journal) with what this layout read or saved, at most once a second
        unless forced. Changed, without unsaved edits: reads them again (_read_files), and undo stops there (the log's
        edits before it describe other files). With unsaved edits: the conflict that save refuses (disk_conflict,
        disk_cells), always against what was read or saved, so it ends by itself when the files go back to that (git
        stash, then pop; a checkout away and back); edits go on. The fingerprints are compared without the lock, so
        tiles and edits are not held up while nothing changed. Raises when a changed file cannot be read (the layout
        stays as it was)."""
        now = time.monotonic()
        if not force and now - self._disk_checked < DISK_CHECK_S:
            return
        self._disk_checked = now
        if not force and (self._fingerprint(), self._log_stat()) == self._seen:
            return
        with self.lock:
            files, logfp = self._fingerprint(), self._log_stat()
            self._seen = (files, logfp)
            cells: set[str] = set()
            for name in sorted(set(files) | set(self._disk_files)):
                fp = files.get(name)
                if fp == self._disk_files.get(name) or (fp is not None and self._held.get(name) == fp):
                    continue
                if self.dirty and fp is not None and self._holds(name):
                    self._held[name] = fp           # touched, the same content: not looked at again
                else:
                    cells.add(name)
            log_moved = False
            if logfp != self._disk_log:
                mark = self._journal_mark()
                if mark == self._journal:              # other views' records, or this layout's own
                    self._disk_log = logfp
                else:                                  # git brought another journal (a merge record, another branch)
                    log_moved = True
                    ours, theirs = self._journal[1], mark[1] if mark else ()
                    cells |= {r[3] for r in set(ours) ^ set(theirs) if r[3]}
            if not self.dirty:
                if cells or log_moved:
                    self._read_files()
                    if cells and not log_moved and self.log is not None:   # the log's edits are not these files'
                        recs = self.log.read(limit=1)
                        self._dropped_upto = max(self._dropped_upto, recs[-1]["seq"] if recs else 0)
                return
            self._conflict = sorted(cells - {""}) if cells or log_moved else None   # [] : only the log's journal

    def poll_disk(self) -> None:
        """check_disk (at most once a second) for the server's requests and status: never fails. A file that cannot
        be read now (another program is writing it) is read on a later look; a save looks first and fails then."""
        try:
            self.check_disk()
        except Exception as e:
            print(f"undine: cannot read the layout of {self.lib.name} again: {e}", file=sys.stderr)

    @property
    def disk_conflict(self) -> bool:
        """Whether cell files (or the log's journal) changed on disk while this layout had unsaved edits: save refuses
        until reload(). Reading it looks at the disk (poll_disk), so a status poll notices a git command."""
        self.poll_disk()
        return self._conflict is not None

    @property
    def disk_cells(self) -> list[str]:
        """The cells whose files changed on disk under the unsaved edits (disk_conflict)."""
        return list(self._conflict or [])

    def _refuse_stale(self) -> None:
        """Before the cell files are written: they are still the ones this layout read or saved (check_disk)."""
        self.check_disk(force=True)
        if self._conflict is not None:
            raise DiskConflict(f"the layout files of {self.lib.name} changed on disk (git?) while edits were unsaved: "
                               "reload to take the files, which drops the unsaved edits")

    def reload(self) -> dict:
        """Drops the unsaved edits and reads the files again: after git changed them under unsaved edits
        (disk_conflict), or to throw the edits away. Returns {"dropped": whether there were unsaved edits}.

        The log keeps the dropped edits (it is history), but they must never be replayed again, neither now nor
        when the library is opened next. While the log's journal is still the one this layout holds, a layout save
        record ends it, marked {"args": {"reload": true}}: every reader of the journal already stops at a layout
        save (_replay_pending, vcs/logmerge.journal, the pre-commit hook's check for unsaved edits), where a record
        of its own would have had to be taught to each, and until then would have kept the dropped edits pending
        (the hook refusing every commit). It also says what is true: the files hold the layout from there on. Undo
        stops at it (_stacks): the edits behind it are not in the layout. When git brought another journal (a merge
        record listing a branch's unsaved edits), that one is the files' and is replayed, as opening the library
        would."""
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        with self.lock:
            dropped = self.dirty
            mark = self._journal_mark()
            self._read_files(drop=bool(mark and mark == self._journal and mark[1]))
            return {"dropped": dropped}

    def create(self, cell_name: str, kind: str, lid: int, pts: list[float], width: float = 0,
               text: str = "") -> list[dict]:
        if self.lib.readonly:
            raise PermissionError("library is read-only")
        with self.lock:
            li = self._layer_index_for(lid)
            p = [kdb.Point(int(round(pts[i])), int(round(pts[i + 1]))) for i in range(0, len(pts), 2)]
            if kind == "box":
                obj = kdb.Box(p[0], p[1])
                if obj.empty() or obj.width() == 0 or obj.height() == 0:
                    return []
            elif kind == "polygon":
                if len(p) < 3:
                    return []
                obj = kdb.Polygon(p)
            elif kind == "path":
                if len(p) < 2 or width <= 0:
                    return []
                obj = kdb.Path(p, int(round(width)))
            elif kind == "text":
                obj = kdb.Text(text, kdb.Trans(p[0]))
            else:
                raise ValueError(kind)
            return self._commit(cell_name, [("ins", li, obj)])

    def draw(self, cell_name: str, shapes: list[tuple[tuple[int, int], kdb.Box | kdb.Polygon]]) -> list[dict]:
        """Shapes on layers given as (layer, datatype), dbu, as one operation: one record of the log, one undo step (a
        via stack, a route, a guard ring)."""
        self._writable()
        with self.lock:
            return self._commit(cell_name, [("ins", self.layout.layer(l, d), obj) for (l, d), obj in shapes])

    def _ops_for(self, c: kdb.Cell, refs: list[dict]) -> list[tuple]:
        """Resolves refs to (delete-op, object) pairs."""
        out = []
        for ref in refs:
            item = self._find(c, ref)
            if item is None:
                continue
            if ref["k"] == "s":
                _, obj = self._shape_obj(item)
                out.append(("del", int(ref["l"]), obj, item.prop_id))
            else:
                out.append(("idel", item.cell_inst, item.prop_id))
        if refs and not out:  # e.g. refs from before a move: say so instead of silently doing nothing
            raise KeyError("none of the selected objects exist any more (stale selection, select again)")
        return out

    def delete(self, cell_name: str, refs: list[dict]) -> list[dict]:
        if self.lib.readonly:
            raise PermissionError("library is read-only")
        with self.lock:
            return self._commit(cell_name, self._ops_for(self.cell(cell_name), refs))

    def move(self, cell_name: str, refs: list[dict], dx: float, dy: float, copy: bool = False) -> list[dict]:
        if self.lib.readonly:
            raise PermissionError("library is read-only")
        with self.lock:
            t = kdb.Trans(kdb.Vector(int(round(dx)), int(round(dy))))
            ops = []
            for op in self._ops_for(self.cell(cell_name), refs):
                if op[0] == "del":
                    if not copy:
                        ops.append(op)
                    ops.append(("ins", op[1], op[2].transformed(t), op[3] if len(op) > 3 else 0))
                else:
                    ci = op[1].dup()
                    ci.transform(t)
                    if not copy:
                        ops.append(op)
                    ops.append(("iins", ci, 0 if copy else op[2]))
            return self._commit(cell_name, ops)

    def copy_items(self, cell_name: str, refs: list[dict]) -> dict:
        """Snapshot shapes and instances; later source edits cannot change the clipboard."""
        with self.lock:
            c = self.cell(cell_name)
            effects, selection, seen = [], [], set()
            for ref in refs:
                key = json.dumps(ref, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                if ref.get("k") not in ("s", "i"):
                    raise ValueError("invalid clipboard selection")
                item = self._find(c, ref)
                if item is None:
                    raise KeyError("a selected object no longer exists (select again)")
                if ref["k"] == "s":
                    kind, obj = self._shape_obj(item)
                    if kind is None:
                        raise ValueError("unsupported clipboard shape")
                    op = ("ins", int(ref["l"]), obj, item.prop_id)
                    selection.append(self._shape_desc(int(ref["l"]), item))
                else:
                    op = ("iins", item.cell_inst, item.prop_id)
                    selection.append(self._inst_desc(item))
                effects.append(self._op_effect(cell_name, op))
            if not effects or len(effects) > 5000:
                raise ValueError("select objects to copy")
            boxes = [s["bbox"] for s in selection]
            bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes)]
            return {"payload": {"kind": "undine-layout", "version": 1,
                                "dbu": self.layout.dbu, "effects": effects},
                    "bbox": bbox, "selection": selection}

    def paste_items(self, cell_name: str, payload: dict, dx: float, dy: float) -> list[dict]:
        """Validate the entire clipboard before inserting it as one undoable edit."""
        if self.lib.readonly:
            raise PermissionError("library is read-only")
        with self.lock:
            c = self.cell(cell_name)
            if (not isinstance(payload, dict) or payload.get("kind") != "undine-layout"
                    or payload.get("version") != 1 or not isinstance(payload.get("effects"), list)
                    or not 0 < len(payload["effects"]) <= 5000
                    or len(json.dumps(payload)) > 2_000_000):
                raise ValueError("invalid layout clipboard")
            dbu = payload.get("dbu")
            if (not isinstance(dbu, (int, float)) or not math.isfinite(dbu)
                    or not math.isclose(dbu, self.layout.dbu, rel_tol=1e-12, abs_tol=0)):
                raise ValueError("clipboard layout units differ from the destination")
            if not math.isfinite(dx) or not math.isfinite(dy) or max(abs(dx), abs(dy)) > 2**31 - 1:
                raise ValueError("invalid paste position")
            dx, dy = int(round(dx)), int(round(dy))
            validated = []
            used_names = {i.property("undine_inst") for i in c.each_inst() if i.property("undine_inst")}

            def parse_value(cls, value):
                try:
                    parsed = cls.from_s(value)
                except (RuntimeError, ValueError, TypeError) as ex:
                    raise ValueError("invalid clipboard geometry") from ex
                if str(parsed) != value:
                    raise ValueError("invalid clipboard geometry")
                return parsed

            def properties(value):
                if not isinstance(value, list):
                    raise ValueError("invalid clipboard properties")
                result = {}
                for pair in value:
                    if (not isinstance(pair, list) or len(pair) != 2
                            or type(pair[0]) not in (str, int) or pair[0] in result):
                        raise ValueError("invalid clipboard properties")
                    result[pair[0]] = pair[1]
                def valid(v):
                    return (v is None or isinstance(v, (str, bool, int))
                            or isinstance(v, float) and math.isfinite(v)
                            or isinstance(v, list) and all(valid(x) for x in v))
                if not all(valid(v) for v in result.values()):
                    raise ValueError("invalid clipboard properties")
                return result

            for source in payload["effects"]:
                if not isinstance(source, dict) or source.get("do") != "add" or source.get("k") not in ("s", "i"):
                    raise ValueError("invalid layout clipboard object")
                props = properties(source.get("props", []))
                e = {**source, "cell": cell_name}
                if e["k"] == "s":
                    if e.get("t") not in ("box", "polygon", "path", "text"):
                        raise ValueError("unsupported clipboard shape")
                    layer = e.get("layer")
                    if not isinstance(layer, str) or not re.fullmatch(r"\d+/\d+", layer):
                        raise ValueError("invalid clipboard layer")
                    li = self.layout.find_layer(*(int(v) for v in layer.split("/")))
                    if li is None:
                        raise ValueError("clipboard layer is missing from the destination library")
                    cls = {"box": kdb.Box, "path": kdb.Path, "text": kdb.Text, "polygon": kdb.Polygon}[e["t"]]
                    if not isinstance(e.get("s"), str):
                        raise ValueError("invalid clipboard shape")
                    obj = parse_value(cls, e["s"])
                    bbox = obj.bbox()
                else:
                    if not isinstance(e.get("child"), str):
                        raise ValueError("invalid clipboard child cell")
                    child = self.cell(e["child"])
                    if child.cell_index() == c.cell_index() or c.cell_index() in _all_children(self.layout, child):
                        raise ValueError("recursive placement")
                    trans_key = "cplx_trans" if "cplx_trans" in e else "trans"
                    if not isinstance(e.get(trans_key), str):
                        raise ValueError("invalid clipboard transform")
                    cls = kdb.ICplxTrans if trans_key == "cplx_trans" else kdb.Trans
                    trans = parse_value(cls, e[trans_key])
                    if trans_key == "cplx_trans" and (not math.isfinite(trans.mag) or trans.mag <= 0
                                                     or not math.isfinite(trans.angle)):
                        raise ValueError("invalid clipboard transform")
                    if "array" in e:
                        a = e["array"]
                        if (not isinstance(a, dict) or any(type(a.get(k)) is not int or not 1 <= a[k] <= 1_000_000
                                                          for k in ("na", "nb"))
                                or any(not isinstance(a.get(k), str) for k in ("a", "b"))):
                            raise ValueError("invalid clipboard array")
                        va, vb = parse_value(kdb.Vector, a["a"]), parse_value(kdb.Vector, a["b"])
                        obj = kdb.CellInstArray(child.cell_index(), trans, va, vb, a["na"], a["nb"])
                    else:
                        obj = kdb.CellInstArray(child.cell_index(), trans)
                    bbox = obj.bbox(self.layout)
                    name = e.get("inst", props.get("undine_inst"))
                    if name is not None:
                        if not isinstance(name, str):
                            raise ValueError("invalid clipboard instance name")
                        if name in used_names:
                            stem = name.rstrip("0123456789") or "i"
                            n = 1
                            while f"{stem}{n}" in used_names:
                                n += 1
                            name = f"{stem}{n}"
                        used_names.add(name)
                        props["undine_inst"] = e["inst"] = name
                if not bbox.empty() and any(abs(v) > 2**31 - 1 for v in
                                           (bbox.left + dx, bbox.right + dx, bbox.bottom + dy, bbox.top + dy)):
                    raise ValueError("paste position exceeds layout coordinate range")
                e["props"] = [[k, v] for k, v in props.items()]
                validated.append(e)
            t = kdb.Trans(kdb.Vector(dx, dy))
            ops = []
            for e in validated:
                op = self._effect_op(e, False)
                if op[0] == "ins":
                    ops.append(("ins", op[1], op[2].transformed(t), op[3]))
                else:
                    ci = op[1].dup()
                    ci.transform(t)
                    ops.append(("iins", ci, op[2]))
            return self._commit(cell_name, ops)

    def place(self, cell_name: str, child: str, x: float, y: float, rot: int = 0, mirror: bool = False,
              inst_name: str | None = None, columns: int = 1, rows: int = 1, pitch: tuple[float, float] = (0, 0),
              layout: dict | None = None, turned: bool = False, own: str | None = None) -> list[dict]:
        """One instance of `child` (rot in 90 degree steps, mirrored at the x axis first); with columns / rows, one
        regular array instance, `pitch` (dbu) apart along x and y, or with `turned` along the instance's own turned
        axes (the array as one block in that orientation, as retransform keeps one). `inst_name` binds it to a
        schematic instance; `layout`: the device's own layout parameters, kept with it (LAYOUT_PROP, schematic-driven
        layout); `own`: the tool a designer placed a bound piece with (OWN_PROP)."""
        if self.lib.readonly:
            raise PermissionError("library is read-only")
        with self.lock:
            ch = self.cell(child)
            if ch.cell_index() == self.cell(cell_name).cell_index() or \
                    self.cell(cell_name).cell_index() in set(ch.each_child_cell()) | _all_children(self.layout, ch):
                raise ValueError("recursive placement")
            tr = kdb.Trans(rot & 3, mirror, int(round(x)), int(round(y)))
            if columns > 1 or rows > 1:
                axes = kdb.Trans(rot & 3, mirror, 0, 0) if turned else kdb.Trans()
                ci = kdb.CellInstArray(ch.cell_index(), tr, axes.trans(kdb.Vector(int(round(pitch[0])), 0)),
                                       axes.trans(kdb.Vector(0, int(round(pitch[1])))), columns, rows)
            else:
                ci = kdb.CellInstArray(ch.cell_index(), tr)
            props = {**({"undine_inst": inst_name} if inst_name else {}),
                     **({LAYOUT_PROP: json.dumps(layout, sort_keys=True)} if layout else {}),
                     **({OWN_PROP: own} if own and inst_name else {})}
            return self._commit(cell_name, [("iins", ci, self.layout.properties_id(props) if props else 0)])

    def place_many(self, cell_name: str, items: list[tuple[str, kdb.Trans, str | None]],
                   boxes: list[tuple[tuple[int, int], kdb.Box | kdb.Polygon]] = (), own: str | None = None) -> list[dict]:
        """Instances of several cells as one recorded change: (child, transformation, schematic instance it is bound
        to or None) each (place_array's units), with shapes ((layer, datatype), box or polygon) of the cell's own;
        `own`: the tool that placed them, kept with the bound ones (OWN_PROP)."""
        self._writable()
        with self.lock:
            top = self.cell(cell_name)
            ops = [("ins", self.layout.layer(*ld), b if isinstance(b, (kdb.Box, kdb.Polygon)) else kdb.Polygon(b))
                   for ld, b in boxes]
            for child, tr, name in items:
                ch = self.cell(child)
                if ch.cell_index() == top.cell_index() or top.cell_index() in _all_children(self.layout, ch):
                    raise ValueError("recursive placement")
                ops.append(("iins", kdb.CellInstArray(ch.cell_index(), tr),
                            self.layout.properties_id({"undine_inst": name, **({OWN_PROP: own} if own else {})})
                            if name else 0))
            return self._commit(cell_name, ops)

    def retransform(self, cell_name: str, ref: dict, trans: kdb.Trans, props: dict | None = None) -> list[dict]:
        """The top-level instance `ref` with another transformation (an array keeps its steps, turned with it), and,
        given `props`, those properties in place of its own: one undoable change, the instance out and back in."""
        self._writable()
        with self.lock:
            c = self.cell(cell_name)
            ops = self._ops_for(c, [ref])
            ci, pid = ops[0][1], ops[0][2]
            new = ci.dup()
            new.trans = trans
            if ci.is_regular_array():                  # the steps turn with the instance
                was, now = kdb.Trans(ci.trans), kdb.Trans(trans)
                was.disp = now.disp = kdb.Vector()
                turn = now * was.inverted()
                new.a, new.b = turn.trans(ci.a), turn.trans(ci.b)
            if props is not None:
                pid = self.layout.properties_id(props) if props else 0
            return self._commit(cell_name, [ops[0], ("iins", new, pid)])

    def swap(self, cell_name: str, a: dict, b: dict, binding: bool = False) -> list[dict]:
        """Two top-level instances (refs) exchanged as one undoable change: each takes the other's transformation (its
        origin and orientation; an array's steps turn with it), or with `binding` each keeps its place and takes the
        other's properties (the schematic instance it is bound to, its layout parameters, who placed it)."""
        self._writable()
        with self.lock:
            c = self.cell(cell_name)
            ops = self._ops_for(c, [a, b])
            if len(ops) != 2 or ops[0][1] == ops[1][1] and ops[0][2] == ops[1][2]:
                raise KeyError("two different instances are needed (one of them is gone: get_layout_info again)")
            (_, ca, pa), (_, cb, pb) = ops

            def moved(ci: kdb.CellInstArray, other: kdb.CellInstArray) -> kdb.CellInstArray:
                new = ci.dup()
                new.cplx_trans = other.cplx_trans      # a simple one stays simple
                if ci.is_regular_array():              # the steps turn with the instance
                    was, now = kdb.Trans(ci.trans), kdb.Trans(new.trans)
                    was.disp = now.disp = kdb.Vector()
                    turn = now * was.inverted()
                    new.a, new.b = turn.trans(ci.a), turn.trans(ci.b)
                return new
            if binding:
                new_a, new_b = (ca.dup(), pb), (cb.dup(), pa)
            else:
                new_a, new_b = (moved(ca, cb), pa), (moved(cb, ca), pb)
            # each out and back in in turn: the record reads as two moves (llm_context.summarize)
            return self._commit(cell_name, [ops[0], ("iins", *new_a), ops[1], ("iins", *new_b)])

    # ------------------------------------------------------- cells from elsewhere

    def _import_file(self, path: Path) -> list[str]:
        other = kdb.Layout()
        other.read(str(path))
        added = []
        for oc in other.top_cells():
            if self.layout.cell(oc.name) is None:
                self._copy_cell(other, oc)
                added.append(oc.name)
        self._changed()
        return added

    def import_oas(self, path: Path, src: dict | None = None) -> list[str]:
        """Merges the cells of another layout file into this layout (no-op for existing cell names).

        `src` says where the cells come from ({"pcell": name, "params": {...}}) so that the operation can be
        replayed elsewhere. The library is saved right away: cells cannot be rebuilt from the journal.
        """
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        if self.lib.readonly:
            raise PermissionError("library is read-only")
        with self.lock:
            self._refuse_stale()   # before the cells come in: they are saved right away
            added = self._import_file(path)
            if added:
                self._log(added[0], self._brought(added, src or {}))     # with its content: a restore can drop it
                self.save()
            return added

    def _layer_map(self, other: kdb.Layout, cell: kdb.Cell | None = None) -> kdb.LayerMapping:
        """Another layout's layers (those `cell` and the cells under it use) to this one's, by layer / datatype."""
        lm = kdb.LayerMapping()
        for li in other.layer_indexes():
            if cell is None or not cell.bbox_per_layer(li).empty():
                i = other.get_info(li)
                lm.map(li, self.layout.layer(i.layer, i.datatype))
        return lm

    def _fill(self, nc: kdb.Cell, other: kdb.Layout, oc: kdb.Cell, lm: kdb.LayerMapping, cells: dict[int, kdb.Cell]) -> None:
        """Gives cell `nc` what `oc` of the other layout holds: shapes, instances (of `cells`, by the other's index), meta."""
        nc.ghost_cell = False
        nc.copy_shapes(oc, lm)
        for inst in oc.each_inst():
            arr = inst.cell_inst.dup()
            arr.cell_index = cells[inst.cell_index].cell_index()
            ni = nc.insert(arr)
            if inst.has_prop_id():
                ni.prop_id = self.layout.properties_id(other.properties(inst.prop_id))
        for m in oc.each_meta_info():
            nc.add_meta_info(m)

    def _copy_cell(self, other: kdb.Layout, oc: kdb.Cell) -> dict[str, str]:
        """Copies a cell and the cells under it from another layout; returns the cells it made, {name there: name here},
        `oc`'s first. A cell under it that this layout has by that name, with the same content, is used as it is (a
        device or standard cell placed before; copy_tree made cell$1 of it); one with other content is copied under a
        new name, as copy_tree does."""
        lm = self._layer_map(other, oc)
        done: dict[int, kdb.Cell] = {}
        made: dict[str, str] = {}

        def copy(c: kdb.Cell, top: bool) -> kdb.Cell:
            if c.cell_index() in done:
                return done[c.cell_index()]
            for ci in c.each_child_cell():                 # the cells under it first: their names in this layout count
                copy(other.cell(ci), False)
            have = self.layout.cell(c.name)
            if have is not None and have.is_ghost_cell():
                nc = have                                  # placed by name before its own file came: filled now
            elif not top and have is not None and \
                    _content(self.layout, have) == _content(other, c, {i: x.name for i, x in done.items()}):
                done[c.cell_index()] = have
                return have
            else:
                nc = self.layout.create_cell(c.name)       # cell$1 when the name is taken
            self._fill(nc, other, c, lm, done)
            done[c.cell_index()] = nc
            made[c.name] = nc.name
            return nc
        copy(oc, True)
        return dict(reversed(made.items()))

    def _copy_cell_from(self, src: kdb.Layout, name: str) -> dict[str, str]:
        """The cells copied (_copy_cell), none when this layout has `name`."""
        sc = src.cell(name)
        if sc is None or sc.is_ghost_cell():
            raise KeyError(f"no cell {name}")
        if (have := self.layout.cell(name)) is not None and not have.is_ghost_cell():
            return {}
        made = self._copy_cell(src, sc)
        self._changed()
        return made

    def copy_cell_from(self, src: kdb.Layout, name: str, src_lib: str | None = None,
                       origin: dict | None = None) -> dict[str, str]:
        """Copies a cell tree from another layout (e.g. a read-only PDK library) into this layout. `origin` is where it
        comes from in the record ({"lib": src_lib} by default; {"stdcell": lib}: the PDK's standard cell). Each cell it
        makes is an effect with its content (_brought), so a changeset's restore can take it out again. Returns the
        cells under it that were copied under another name, {name there: name here} (this layout has one of that name
        with other content: the copy places via$1, not via)."""
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        if self.lib.readonly:
            raise PermissionError("library is read-only")
        with self.lock:
            if (have := self.layout.cell(name)) is None or have.is_ghost_cell():
                self._refuse_stale()   # before the cell comes in: it is saved right away
            if (made := self._copy_cell_from(src, name)):
                self._log(name, self._brought(list(made.values()), origin or ({"lib": src_lib} if src_lib else {})))
                self.save()
            return {a: b for a, b in made.items() if a != b}

    def _brought(self, cells: list[str], src: dict) -> list[dict]:
        """The effects of cells brought in with their content (a copy, an import), parents first: "sha" is the content
        they came with (_content), by which a changeset's restore knows them unchanged (changeset.py, _drop_made)."""
        return [{"k": "c", "do": "add", "cell": n, "src": src, "sha": _content(self.layout, self.cell(n))} for n in cells]

    def add_stdcell(self, name: str) -> str | None:
        """Copies the PDK's standard cell `name` in (as copy_cell_from), unless the layout has it; returns its library,
        None when `name` is not a standard cell of the PDK."""
        hit = stdcell_layout(self.pdk, name)
        if hit is None:
            return None
        self.copy_cell_from(hit[1], name, origin={"stdcell": hit[0]})
        return hit[0]

    def import_cells(self, path: Path, src: dict, replace: bool | list[str] = ()) -> dict:
        """Brings the cells of a layout file in as one operation. Cells this layout lacks are added ({"k": "c"} effects
        with `src`; their content from the file, saved right away as import_oas does), parents first; cells it has with
        the same content stay as they are; a cell it has with other content is refused (CellClash, nothing imported)
        unless `replace` names it (True: all): then its shapes and instances become the file's, recorded one by one
        (a record per cell), so the review shows them and undo takes them back. A cell the file places without
        defining it must be in the layout, or a standard cell of the PDK (copied in). The file's dbu must be this one's.
        Returns {"top", "added", "unchanged", "replaced", "stdcells"}."""
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        self._writable()
        if self.lib.layout_file:
            raise PermissionError("library is read-only")
        other = kdb.Layout()
        other.read(str(path))
        if not math.isclose(other.dbu, self.layout.dbu, rel_tol=1e-9):
            raise ValueError(f"the file's database unit is {other.dbu:g} um, the library's {self.layout.dbu:g} um")
        with self.lock:
            order = [other.cell(ci) for ci in other.each_cell_top_down()]
            tops = [c.name for c in other.top_cells() if not c.is_ghost_cell()]
            new, same, differ, std = [], [], [], []
            for c in order:
                have = self.layout.cell(c.name)
                if c.is_ghost_cell():
                    if have is None or have.is_ghost_cell():
                        if stdcell_layout(self.pdk, c.name) is None:
                            raise KeyError(f"the file places {c.name} without defining it, and {self.lib.name} has no such cell")
                        std.append(c.name)
                elif have is None or have.is_ghost_cell():
                    new.append(c)
                elif _content(self.layout, have) == _content(other, c):
                    same.append(c.name)
                else:
                    differ.append(c)
            if (clash := [c.name for c in differ if replace is not True and c.name not in (replace or ())]):
                raise CellClash(self.lib.name, clash)
            for name in std:
                self.add_stdcell(name)
            if new or differ:
                self._refuse_stale()   # before the cells come in: they are saved right away
            lm = self._layer_map(other)
            fresh = {c.cell_index() for c in new}
            cells: dict[int, kdb.Cell] = {}
            for c in order:
                have = self.layout.cell(c.name)
                cells[c.cell_index()] = have if have is not None or c.cell_index() not in fresh else self.layout.create_cell(c.name)
            for c in new:
                self._fill(cells[c.cell_index()], other, c, lm, cells)
            if new:
                self._changed()
                self._log(next((n for n in tops if n in {c.name for c in new}), new[0].name),
                          self._brought([c.name for c in new], src))
            for c in differ:                               # the old content out, the file's in: recorded, not searched for
                mine = self.cell(c.name)
                old = [("del", li, obj, s.prop_id) for li in self.layout.layer_indexes() for s in mine.shapes(li).each()
                       for _, obj in [self._shape_obj(s)] if obj is not None]
                old += [("idel", i.cell_inst, i.prop_id) for i in mine.each_inst()]
                ops = []
                for li in other.layer_indexes():
                    info = other.get_info(li)
                    tli = self.layout.layer(info.layer, info.datatype)
                    for s in c.shapes(li).each():
                        _, obj = self._shape_obj(s)
                        if obj is not None:
                            ops.append(("ins", tli, obj, self.layout.properties_id(other.properties(s.prop_id)) if s.prop_id else 0))
                for i in c.each_inst():
                    arr = i.cell_inst.dup()
                    arr.cell_index = cells[i.cell_index].cell_index()
                    ops.append(("iins", arr, self.layout.properties_id(other.properties(i.prop_id)) if i.has_prop_id() else 0))
                effects = [self._op_effect(c.name, op) for op in old + ops]
                mine.clear_shapes()
                mine.clear_insts()
                for op in ops:
                    self._apply(mine, op)
                self._changed()
                self._log(c.name, effects)
            if new or differ:
                self.save()
            return {"top": tops, "added": [c.name for c in new], "unchanged": same, "replaced": [c.name for c in differ],
                    "stdcells": std}

    def drop_cells(self, names: list[str]) -> list[str]:
        """Takes cells nothing places out of the layout, parents first, as one record: for each cell the deletion of
        every shape and instance it held, then of the cell ({"k": "c", "do": "del", "drop": true, "kept": true, "meta":
        its persisted meta information}), so one undo (which re-creates the children before the parents' instances of
        them), redo and a changeset's restore bring them all back as they were (oplog.invert) and take them out again.
        ValueError when one is placed somewhere (by a cell not dropped before it): nothing is dropped then."""
        self._writable()
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        names = list(dict.fromkeys(names))
        with self.lock:
            gone = set()
            for n in names:                              # all of them can go: none is placed but by those before it
                c = self.cell(n)
                if c.is_ghost_cell():
                    raise ValueError(f"{n} is not a cell of {self.lib.name}")
                users = sorted({self.layout.cell(p).name for p in c.each_parent_cell()} - gone)
                if users:
                    raise ValueError(f"{n} is placed in {', '.join(users[:5])}: delete those instances first")
                gone.add(n)
            effects: list[dict] = []
            for n in names:
                c = self.cell(n)
                ops = [("del", li, obj, s.prop_id) for li in self.layout.layer_indexes() for s in c.shapes(li).each()
                       for _, obj in [self._shape_obj(s)] if obj is not None]
                ops += [("idel", i.cell_inst, i.prop_id) for i in c.each_inst()]
                effects += [self._op_effect(n, op) for op in ops]
                effects.append({"k": "c", "do": "del", "cell": n, "drop": True, "kept": True, "src": {},
                                "meta": [[m.name, _jsonable(m.value)] for m in c.each_meta_info() if m.is_persisted()]})
                self.layout.delete_cell(c.cell_index())
            if names:
                self._changed()
                self._log(names[0], effects)
            return list(names)

    def named_instances(self, cell_name: str) -> list[dict]:
        """Top-level instances that are bound to schematic instances (property undine_inst), as instances() lists them."""
        return [i for i in self.instances(cell_name) if i.get("name")]

    def instances(self, cell_name: str) -> list[dict]:
        """The cell's top-level instances: "id" (child@x,y, its origin in um exactly (um_text); #2 ... for more of
        that child at one origin), "cell", "bbox", "trans", "ref", the schematic instance it is bound to ("name"), the
        device's layout parameters ("layout", LAYOUT_PROP), the tool a designer placed it with ("own", OWN_PROP) and
        an array's columns, rows and steps."""
        with self.lock:
            out = []
            for inst, iid in instance_ids(self.layout, self.cell(cell_name)):
                b = inst.bbox()
                ci = inst.cell_inst
                child = self.layout.cell(inst.cell_index).name
                d = {"id": iid, "cell": child,
                     "bbox": [b.left, b.bottom, b.right, b.top], "trans": str(ci.trans), "ref": self._inst_desc(inst)["ref"]}
                if (nm := inst.property("undine_inst")):
                    d["name"] = nm
                    if (own := inst.property(OWN_PROP)):
                        d["own"] = own
                if (lp := inst.property(LAYOUT_PROP)):
                    try:
                        d["layout"] = json.loads(lp)
                    except ValueError:
                        pass
                if ci.is_regular_array():
                    d["array"] = {"columns": ci.na, "rows": ci.nb, "a": [ci.a.x, ci.a.y], "b": [ci.b.x, ci.b.y]}
                out.append(d)
            return out

    def _step(self, backwards: bool, cell: str | None = None) -> str | None:
        """Undoes (redoes) the latest operation of `cell`, or of the whole library when None. An editor passes its
        cell: an undo there must never take back an edit made in another cell of the library, unseen."""
        if self.log is None or self.lib.readonly:
            return None                     # nothing to undo in a read-only library (can_undo_redo: (0, 0))
        self.log.guard()
        done, redo = self._stacks(cell)
        rec = (done[-1] if backwards else redo[-1]) if (done if backwards else redo) else None
        if rec is None:
            return None
        self.apply_effects(rec["effects"], backwards)
        self._log(rec.get("cell"), None, **{"op": "undo" if backwards else "redo", "of": rec["seq"]})
        return rec.get("cell")

    def undo(self, cell: str | None = None) -> str | None:
        with self.lock:
            return self._step(True, cell)

    def redo(self, cell: str | None = None) -> str | None:
        with self.lock:
            return self._step(False, cell)

    def can_undo_redo(self, cell: str | None = None) -> tuple[int, int]:
        done, redo = self._stacks(cell) if self.log and not self.lib.readonly else ([], [])
        return len(done), len(redo)

    def _stacks(self, cell: str | None) -> tuple[list[dict], list[dict]]:
        """The log's undo / redo stacks, without the operations a reload dropped (they are not in the layout)."""
        done, redo = self.log.stacks(cell, "layout")
        if self._dropped_upto:
            done = [r for r in done if r["seq"] > self._dropped_upto]
            redo = [r for r in redo if r["seq"] > self._dropped_upto]
        return done, redo

    def new_cell(self, name: str) -> None:
        self._writable()
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        with self.lock:
            if self.layout.cell(name) is not None:
                raise ValueError(f"cell {name} exists")
            self.layout.create_cell(name)
            self._changed()
            self._log(name, [{"k": "c", "do": "add", "cell": name, "src": {}}])

    def save(self) -> dict[str, list[str]]:
        """Writes the cells that changed since the last read / save (one file per cell) and removes deleted ones.
        Returns the cells written and removed. Refuses (DiskConflict) when the files changed on disk under unsaved
        edits: it would write the cells as they were before over them (check_disk)."""
        if self.lib.readonly or self.lib.layout_file:
            raise PermissionError("library is read-only")
        with self.lock:
            self._refuse_stale()
            before, files = self.saved, self._disk_files
            self.saved = layoutstore.write(self.lib.path, self.layout, self.saved)
            # the files written or removed get their new fingerprints; another file that changed meanwhile keeps
            # its old one, so the next look notices it
            now, fp = self._fingerprint(), dict(files)
            written = {n for n, sha in self.saved.items() if before.get(n) != sha or n not in files}
            for n in written | (set(before) - set(self.saved)):
                if n in now:
                    fp[n] = now[n]
                else:
                    fp.pop(n, None)
            self._disk_files = fp
            self.dirty = False
            self._log(None, None, **{"op": "save", "sha": oplog.sha(json.dumps(sorted(self.saved.items())))})
            return {"written": sorted(n for n, sha in self.saved.items() if before.get(n) != sha),
                    "removed": sorted(set(before) - set(self.saved))}


_STDCELLS: dict[Path, tuple[tuple | None, kdb.Layout]] = {}
_STDCELLS_LOCK = threading.Lock()


def stdcell_layout(pdk: Pdk | None, name: str) -> tuple[str, kdb.Layout] | None:
    """(library, its layout) of the PDK's standard cell `name`, by the profile's naming (Profile.stdcell): the library's
    GDS in the PDK, read at run time and kept while the file is unchanged. None when `name` is not a standard cell of
    the PDK or its GDS does not have it."""
    hit = pdk.profile.stdcell(name) if pdk is not None else None
    if hit is None:
        return None
    lib, rel = hit
    path = pdk.root / rel
    st = _stat(path)
    if st is None:
        return None
    with _STDCELLS_LOCK:
        have = _STDCELLS.get(path)
        if have is None or have[0] != st:
            ly = kdb.Layout()
            ly.read(str(path))
            _STDCELLS[path] = have = (st, ly)
    c = have[1].cell(name)
    return (lib, have[1]) if c is not None and not c.is_ghost_cell() else None


def _shape_key(s: kdb.Shape) -> str:
    """A shape as _content compares it: a box as the polygon it is, a text by its string and position (an OASIS file, as
    a library's cell files are, keeps no text size, alignment or rotation)."""
    if s.is_text():
        return f"text {s.text.string!r} {s.text.trans.disp}"
    if s.is_path():
        return f"path {s.path}"
    return f"polygon {s.polygon}"


def _steps(i: kdb.Instance) -> str:
    """An array instance's steps as _content compares them: the axes that repeat, in order. A GDS AREF and the cell
    files' OASIS give one array with its axes the other way round, and a row as [step*n; 0,0*1] or [0,0*1; step*n]."""
    if not i.is_regular_array():
        return ""
    axes = sorted((str(v), n) for v, n in ((i.a, i.na), (i.b, i.nb)) if n > 1 and (v.x or v.y))
    return "".join(f" [{v}*{n}]" for v, n in axes)


def _content(layout: kdb.Layout, cell: kdb.Cell, names: dict[int, str] | None = None) -> str:
    """What a cell holds, comparable between layouts and file formats (layoutstore.cell_sha is within one layout): its
    shapes per layer / datatype with their properties, its instances by the name of the cell they place (`names`: the
    cell's name in the other layout, by index), and its persisted meta information. A cell read from a GDS (a PDK's
    standard cells, a generator's output) and its copy in a library's cell files are the same: duplicate shapes and
    instances, which the cell files do not keep, count once, and arrays compare by their steps (_steps)."""
    h = hashlib.sha256()
    for key, li in sorted((f"{layout.get_info(li).layer}/{layout.get_info(li).datatype}", li) for li in layout.layer_indexes()):
        shapes = cell.shapes(li)
        if not shapes.is_empty():
            rows = sorted({_shape_key(s) + (f" {sorted(_props(layout.properties(s.prop_id)).items(), key=str)}" if s.prop_id else "")
                           for s in shapes.each()})
            h.update(f"L {key}\n".encode() + "\n".join(rows).encode())
    insts = set()
    for i in cell.each_inst():
        child = names.get(i.cell_index) if names else None
        p = f" {sorted(_props(layout.properties(i.prop_id)).items(), key=str)}" if i.has_prop_id() else ""
        insts.add(f"{child or layout.cell(i.cell_index).name} {i.cplx_trans}{_steps(i)}{p}")
    h.update(("\nI " + "\nI ".join(sorted(insts)) if insts else "").encode())
    meta = sorted(f"{m.name}={m.value!r}" for m in cell.each_meta_info() if m.is_persisted())
    h.update(("\nM " + "\nM ".join(meta) if meta else "").encode())
    return h.hexdigest()


def tree_content(layout: kdb.Layout, cell: kdb.Cell, memo: dict[int, str] | None = None) -> str:
    """_content of a cell with the cells under it: each child counts by its own tree, not by its name, so two cells that
    place differently drawn cells of the same name differ. A ghost cell (placed by name, not defined) by its name."""
    memo = {} if memo is None else memo
    if cell.cell_index() not in memo:
        if cell.is_ghost_cell():
            memo[cell.cell_index()] = f"ghost {cell.name}"
        else:
            kids = {ci: tree_content(layout, layout.cell(ci), memo) for ci in cell.each_child_cell()}
            memo[cell.cell_index()] = _content(layout, cell, kids)
    return memo[cell.cell_index()]


def _stat(p: Path) -> tuple | None:
    """A file's fingerprint: (mtime_ns, size, inode). git writes a file anew (another inode) even within the same
    tick of a coarse clock."""
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def _is_boundary(r: dict) -> bool:
    """A record the journal starts after: a layout save (a reload too), or a git merge (vcs/logmerge.py)."""
    return r.get("op") == "merge" or (r.get("op") == "save" and r.get("view") == "layout")


def _is_reload(r: dict) -> bool:
    return r.get("op") == "save" and r.get("view") == "layout" and bool((r.get("args") or {}).get("reload"))


def _journal(recs: list[dict]) -> tuple[int, list[tuple[object, dict | None]]]:
    """(index of the last boundary, the journal [(seq, record or None)]) of a log's records: its layout operations
    the cell files lack, which LayoutDB._replay_pending replays (as vcs/logmerge.journal reads them)."""
    last = max((i for i, r in enumerate(recs) if _is_boundary(r)), default=-1)
    pending: list[tuple[object, dict | None]] = []
    if last >= 0 and recs[last].get("op") == "merge":
        before = {r["seq"]: r for r in recs[:last] if r.get("view") == "layout"}
        listed = recs[last].get("replay")
        pending = [(s, before.get(s) if isinstance(s, int) else None) for s in
                   (listed if isinstance(listed, list) else [])]
    pending += [(r["seq"], r) for r in recs[last + 1:] if r.get("view") == "layout"]
    return last, pending


def _rid(r: dict | None, seq: object = None) -> tuple:
    """A log record as a journal mark names it: (seq, time, op, cell)."""
    return (r.get("seq"), r.get("t"), r.get("op"), r.get("cell")) if r is not None else (seq, None, None, None)


def _mark(recs: list[dict], last: int, pending: list[tuple[object, dict | None]]) -> tuple:
    """What a layout holds of a log: its last boundary and its journal (_journal), as record names (_rid). The layout
    adds its own records to it as it writes them (LayoutDB._log); a log git rewrote has another."""
    return (_rid(recs[last]) if last >= 0 else None, tuple(_rid(r, s) for s, r in pending))


def _props(p) -> dict:
    """Instance properties as a dict (the KLayout binding returns a dict or a list of pairs)."""
    return dict(p) if not isinstance(p, dict) else p


def _bit(mask: str, i: int) -> bool:
    k = i // 4
    if k >= len(mask):
        return True
    return bool(int(mask[k], 16) >> (i % 4) & 1)


def _pts(points) -> list[int]:
    out = []
    for p in points:
        out += [p.x, p.y]
    return out


def _inst_key(ci: kdb.CellInstArray) -> tuple:
    """An instance as it is after the cell file is written and read again (a logged effect may hold it as placed): a
    regular array's steps of a count above 1 in a fixed order (the OASIS and GDS readers zero a step of count 1 and
    may swap a one-row array's two), a 1 x 1 array as one instance."""
    tr = str(ci.cplx_trans) if ci.is_complex() else str(ci.trans)
    steps = sorted((v.x, v.y, n) for v, n in ((ci.a, ci.na), (ci.b, ci.nb)) if n > 1) if ci.is_regular_array() else []
    return ci.cell_index, tr, tuple(steps)


def _objtype(obj) -> str:
    if isinstance(obj, kdb.Box):
        return "box"
    if isinstance(obj, kdb.Path):
        return "path"
    if isinstance(obj, kdb.Text):
        return "text"
    return "polygon"


def _jsonable(v):
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


def _all_children(layout: kdb.Layout, cell: kdb.Cell) -> set[int]:
    out: set[int] = set()
    stack = [cell.cell_index()]
    while stack:
        for ch in layout.cell(stack.pop()).each_child_cell():
            if ch not in out:
                out.add(ch)
                stack.append(ch)
    return out
