"""Projects and libraries on the server.

    <root>/<project>/project.json   name, format, pinned PDK, libraries, symbol paths
    <root>/<project>/libs/<lib>/    one library
        <cell>.sch / <cell>.sym     schematic / symbol views (Xschem format,
                                    so Xschem can open the same directory)
        <cell>.oas                  layout view, one file per cell (layoutstore.py)

`format` in project.json is the version of this layout. Opening a project of an older format converts it:
    1 -> 2   layout.oas (all cells of a library) -> one <cell>.oas per cell
    2 -> 3   every schematic gets its netlist view, <cell>.netlist.yaml (netlist.py)
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import klayout.db as kdb

from undine.store import deps
from undine.store import manifest as mf
from undine.layouts import layoutstore
from undine.pdks import compat, pcell, pin
from undine import tools
from undine.pdks.pdk import Pdk, family_of

FORMAT = 3
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
# cells generated for schematic devices: pcell.cell_name() (type, parameters, a 6-digit hash), then the wrapper's
# suffixes (sdl.DEVICE_CELL): "_par" when its fingers are strapped, "_tie" its butted tap, "_left" mirrored
DEVICE_CELL = re.compile(r"^[A-Za-z0-9_]+_[0-9a-f]{6}(_par)?(_tie)?(_left)?$")


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"invalid name: {name!r}")
    return name


@dataclass
class Library:
    name: str
    path: Path
    readonly: bool = False
    origin: str = "project"  # project | dep (a pinned dependency, deps.py) | pdk
    version: str | None = None  # a dependency's rev @ commit, or 'analog_ip 1.2.0 (v1.2.0 @ 2299146)'
    layout_file: str | None = None  # one file with all cells (a PDK's GDS); None: a file per cell (layoutstore)
    package: str | None = None  # a dependency's [package] name, from its install marker
    direct: bool = True         # False: installed only because a package needs it (not in project.json)
    via: tuple = ()             # for those, the packages that need it
    exports: tuple | None = None  # a package's public cells ([exports]); None: every cell is public
    _layout_cells: tuple[tuple, list[str], list[str]] | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.readonly:                      # a PDK's or a dependency's files are never written, even if the disk allows it
            from undine.schematics import netlist

            netlist.protect(self.path)

    def layout_files(self) -> list[Path]:
        if self.layout_file:
            p = self.path / self.layout_file
            return [p] if p.is_file() else []
        return layoutstore.cell_files(self.path)

    def has_layout(self) -> bool:
        return bool(self.layout_files())

    def read_layout(self, layout: kdb.Layout) -> None:
        for f in self.layout_files():
            layout.read(str(f))

    def layout_cells(self) -> tuple[list[str], list[str]]:
        """(all cells, top cells) of the layout, cached by the files' modification times."""
        files = self.layout_files()
        key = tuple((f.name, f.stat().st_mtime_ns) for f in files)
        if self._layout_cells is None or self._layout_cells[0] != key:
            ly = kdb.Layout()
            self.read_layout(ly)
            cells = sorted(c.name for c in ly.each_cell() if not c.is_ghost_cell())
            tops = sorted(c.name for c in ly.top_cells() if not c.is_ghost_cell())
            self._layout_cells = (key, cells, tops)
        return self._layout_cells[1], self._layout_cells[2]

    def cells(self, layout: tuple[list[str], list[str]] | None = None) -> list[dict]:
        """The library's cells with their views. `layout`: (all cells, top cells) of its layout as the server holds it
        (a deletion or a new cell not saved yet), in place of its cell files' (layout_cells)."""
        views: dict[str, set[str]] = {}
        if self.path.is_dir():
            for f in self.path.iterdir():
                if f.suffix == ".sch":
                    views.setdefault(f.stem, set()).add("schematic")
                elif f.suffix == ".sym":
                    views.setdefault(f.stem, set()).add("symbol")
                elif f.suffix in (".sv", ".v"):
                    views.setdefault(f.stem, set()).add("rtl")
        all_cells, tops = layout if layout is not None else self.layout_cells()
        for c in all_cells:
            views.setdefault(c, set()).add("layout")
        order = {"schematic": 0, "symbol": 1, "rtl": 2, "layout": 3}
        return [
            {"name": c, "views": sorted(v, key=order.get), "top": c in tops,
             "device": v == {"layout"} and bool(DEVICE_CELL.match(c)),         # placed or left over, never a design cell
             **({"internal": True} if self.internal(c) else {})}
            for c, v in sorted(views.items(), key=lambda kv: (kv[0] not in tops, kv[0].lower()))
        ]

    def internal(self, cell: str) -> bool:
        """Whether a dependency's cell is outside its package's [exports]: hidden from the picker, still resolvable,
        netlisted and placeable (Decisions: [exports] in phase 2)."""
        return self.origin == "dep" and self.exports is not None and cell not in self.exports

    def public(self) -> dict:
        return {"name": self.name, "readonly": self.readonly, "origin": self.origin,
                "has_layout": self.has_layout(), **({"version": self.version} if self.version else {}),
                "package": self.package, "direct": self.direct, "via": list(self.via)}


# Install markers, and the [exports] of their manifests, by marker path: libraries() runs on almost every request, and
# a reinstall writes a new marker, so (inode, mtime_ns, size) tells a changed one apart. {path: (key, marker, exports)}
_MARKERS: dict[Path, tuple[tuple, dict | None, tuple | None]] = {}
_MARKERS_LOCK = threading.Lock()


def _installed(d: Path, name: str) -> tuple[dict | None, tuple | None]:
    """(the install marker of dependency directory d, or None; its package's [exports], or None when it has none,
    is not a package, or its manifest cannot be read: status reports that). Cached by the marker's inode, mtime and
    size."""
    p = d / deps.MARKER
    try:
        st = os.stat(p)
    except OSError:
        return None, None
    key = (st.st_ino, st.st_mtime_ns, st.st_size)          # the inode too: a reinstall writes a new file
    with _MARKERS_LOCK:
        hit = _MARKERS.get(p)
    if hit is not None and hit[0] == key:
        return hit[1], hit[2]
    try:
        marker = json.loads(p.read_text())
    except (OSError, ValueError):
        marker = None
    if not isinstance(marker, dict):
        marker = None
    exports = None
    if marker and marker.get("package"):          # a plain or phase 1 dependency: every cell public, as before
        try:
            m = mf.from_dir(d, name)
            exports = m.exports if m is not None else None
        except (ValueError, OSError):            # manifest.ManifestError among them
            exports = None
    with _MARKERS_LOCK:
        _MARKERS[p] = (key, marker, exports)
    return marker, exports


def _label(marker: dict) -> str | None:
    """'analog_ip 1.2.0' when the marker names a package."""
    package = marker.get("package")
    if not isinstance(package, str) or not package:
        return None
    version = marker.get("version")
    return f"{package} {version}" if isinstance(version, str) and version else package


_MIGRATE = threading.Lock()


class PdkMove(tuple):
    """What set_pdk returns: (old id, new id), as it always has, and .created, whether it made undine.lock."""
    created: bool

    def __new__(cls, old: str | None, new: str, created: bool = False):
        t = super().__new__(cls, (old, new))
        t.created = created
        return t


def pick_pdk(pdks: dict[str, Pdk], spec: str) -> Pdk | None:
    """A PDK by id ("sky130A@8afc8346a57f") or by name ("sky130A"); for a name, a version whose PCells Undine runs
    comes first, of those the version Undine is tested with (pcell.SUPPORTED), then the order of discovery."""
    # a copy: the server's rescan adds and removes PDKs while requests read the dict
    match = [x for x in list(pdks.values()) if x.id == spec or x.name == spec]
    match.sort(key=lambda x: (x.id != spec, pcell.macros_dir(x) is None,
                              not x.version or x.version != pcell.SUPPORTED.get(x.family)))
    return match[0] if match else None


class Project:
    def __init__(self, path: Path, pdks: dict[str, Pdk]):
        self.path = path
        self.pdks = pdks
        self.meta = json.loads((path / "project.json").read_text())
        if self.meta.get("format", 1) < FORMAT:
            self._migrate()

    def _migrate(self) -> None:
        with _MIGRATE:
            self.meta = json.loads((self.path / "project.json").read_text())   # another request may have done it
            if self.meta.get("format", 1) < 2:
                for spec in self.library_specs().values():
                    if "path" in spec:
                        layoutstore.split_legacy(self.lib_path(spec["path"]))
            if self.meta.get("format", 1) < 3:
                from undine.schematics import netlist, xschem  # every schematic gets its netlist view, once
                resolver = xschem.SymbolResolver(self.symbol_paths())
                for spec in self.library_specs().values():
                    d = self.lib_path(spec["path"]) if "path" in spec else None
                    for sch in sorted(d.glob("*.sch")) if d and d.is_dir() else []:
                        try:
                            netlist.current(sch, resolver, write=True)
                        except Exception as e:     # a broken schematic keeps its drawing; its netlist comes later
                            import sys
                            print(f"undine: no netlist for {sch}: {e}", file=sys.stderr)
            self.meta["format"] = FORMAT
            self.save()

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def pdk(self) -> Pdk | None:
        pid = self.meta.get("pdk")
        if not pid:
            return None
        return pick_pdk(self.pdks, pid)      # an exact id, or a bare name ("sky130A")

    def _refuse_incompatible(self, lock: dict | None, new: Pdk) -> None:
        """IncompatiblePdk when a package the lock pins does not accept `new` (compat.locked_incompatible, the
        manifests from the store or the install, never git). A lock that pins no dependency has nothing to refuse,
        whatever `new` tells of itself."""
        if not isinstance(lock, dict) or not (lock.get("libraries") or lock.get("packages")):
            return
        variant = getattr(new, "variant", None) or getattr(new, "name", None) or new.id
        commit = getattr(new, "commit", None)
        root = getattr(new, "root", None)
        target = compat.Target(family_of(variant), variant, commit, None, new.id, str(root) if root else None)
        bad = compat.locked_incompatible(self.path, lock, target)
        if bad:
            raise compat.IncompatiblePdk(new.id, bad)

    def set_pdk(self, spec: str) -> PdkMove:
        """Pins the project to another PDK version: project.json's pdk (the requirement) and undine.lock's (the pin),
        always both, so they never disagree after it. A project without a lock gets one, recording the tool versions
        now, as `lib lock` would. A format 1 lock gets the id; a format 2 lock keeps its format and gets the new PDK's
        pin (full commit and part hashes, pin.pin_object), its tools left as they are. Returns (old id, new id), with
        .created telling whether it made the lock. Layout cells generated from the old PDK's PCells stay until
        regenerated (the schematic-driven layout panel lists them as out of date, since generated cell names depend on
        the generator). Refused (compat.IncompatiblePdk, nothing changed) when a locked package's [pdk] does not
        accept the new PDK; checked before hashing and again on the lock as it is under the project lock. Setting the
        PDK the project already names is never refused."""
        new = pick_pdk(self.pdks, spec)
        if new is None:
            raise KeyError(f"no PDK {spec}")
        # The tools answer, and a format 2 pin is hashed (seconds for a PDK not hashed before), outside meta_lock (as
        # in relock); the tools only when there is no lock to keep their versions.
        seen = deps.read_lock(self.path)           # a lock this Undine cannot read: refused before anything runs
        if self.meta.get("pdk") != new.id:
            self._refuse_incompatible(seen, new)   # before seconds of hashing
        env = None if seen is not None else deps.environment(new)
        pinned = pin.pin_object(new) if seen is not None and deps.lock_format(seen) == 2 else None
        with self.editing():
            old = self.meta.get("pdk")
            lock = deps.read_lock(self.path)       # one this Undine cannot write: refused before project.json changes
            if old != new.id:
                self._refuse_incompatible(lock, new)   # the lock as it is now (a lib add meanwhile)
            fmt = deps.lock_format(lock) if lock is not None else deps.FORMAT
            if fmt == 2 and pinned is None:        # made format 2 meanwhile (a git pull): rare, hashed here
                pinned = pin.pin_object(new)
            if old != new.id:
                self.meta["pdk"] = new.id
                self.meta.setdefault("pdk_history", []).append(
                    {"from": old, "to": new.id, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
                self.save()
            created = lock is None
            if fmt == 2:
                was = lock.get("pdk") if isinstance(lock.get("pdk"), dict) else {}
                if (was.get("id"), was.get("variant"), was.get("commit")) != (new.id, new.variant, new.commit):
                    lock["pdk"] = pinned           # the same PDK keeps its pin: a modified tree stays reported
                    deps.write_lock(self.path, lock)
            elif lock is None or deps.lock_pdk_id(lock) != new.id:   # the same PDK again too: a missing or stale pin
                lock = lock or deps.new_lock(env or deps.environment(new))
                lock["pdk"] = new.id
                deps.write_lock(self.path, lock)
        return PdkMove(old, new.id, created)

    @contextlib.contextmanager
    def editing(self):
        """project.json as it is on disk now (self.meta read again), kept from other writers (another request, the
        CLI, a dependency operation) until the block ends: deps.meta_lock. Not reentrant."""
        with deps.meta_lock(self.path):
            self.meta = json.loads((self.path / "project.json").read_text())
            yield self

    def save(self) -> None:
        (self.path / "project.json").write_text(json.dumps(self.meta, indent=2) + "\n")

    def library_specs(self) -> dict[str, dict]:
        """The libraries of project.json: {"path": dir} (the project's own; a bare string means the same) or
        {"git": url, "rev": ..., "subdir": ...} (a dependency, deps.py)."""
        return {n: ({"path": s} if isinstance(s, str) else dict(s)) for n, s in self.meta.get("libraries", {}).items()}

    def lib_path(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        return p if p.is_absolute() else self.path / p

    def _deps_root(self) -> Path | None:
        """.undine/deps when it is a real directory of the project; None when it is absent or a link (lib sync
        refuses that, deps._deps_root)."""
        try:
            root = deps._deps_root(self.path)
        except deps.DepError:
            return None
        return root if root.is_dir() else None

    def _transitive(self, taken: dict) -> dict[str, Library]:
        """The dependencies installed only because a package needs them: the directories of .undine/deps whose
        marker says direct false, from the markers alone (opening a project never reads undine.lock). A name
        project.json already uses is left to it."""
        root = self._deps_root()
        out: dict[str, Library] = {}
        if root is None:
            return out
        used = {n.casefold() for n in taken}
        for d in sorted(root.iterdir()):
            name = d.name
            if name.startswith(".") or not NAME_RE.match(name) or name.casefold() in used or d.is_symlink():
                continue
            marker, exports = _installed(d, name)
            if not marker or marker.get("direct") is not False:
                continue
            commit = marker.get("commit") if isinstance(marker.get("commit"), str) else ""
            label = _label(marker) or (marker.get("rev") if isinstance(marker.get("rev"), str) else None) or "HEAD"
            via = tuple(v for v in marker.get("via") or () if isinstance(v, str)) \
                if isinstance(marker.get("via"), list) else ()
            out[name] = Library(name, d, readonly=True, origin="dep", version=f"{label} @ {commit[:7]}",
                                package=marker.get("package") if isinstance(marker.get("package"), str) else None,
                                direct=False, via=via, exports=exports)
        return out

    def libraries(self) -> dict[str, Library]:
        libs: dict[str, Library] = {}
        for name, spec in self.library_specs().items():
            if "git" in spec:
                d = deps.dep_dir(self.path, name)
                have, exports = _installed(d, name)
                have = have or {}
                commit = have.get("commit") if isinstance(have.get("commit"), str) else ""
                at = f"{spec.get('rev') or 'HEAD'} @ {commit[:7]}"
                label = _label(have)
                ver = (f"{label} ({at})" if label else at) if commit else "not installed"
                package = have.get("package") if isinstance(have.get("package"), str) else None
                libs[name] = Library(name, d, readonly=True, origin="dep", version=ver, package=package,
                                     exports=exports)
            else:
                libs[name] = Library(name, self.lib_path(spec["path"]))
        libs.update(self._transitive(libs))
        pdk = self.pdk
        if pdk and pdk.xschem_dir:
            for d in sorted(pdk.xschem_dir.iterdir()):
                if d.is_dir() and any(d.glob("*.sym")) and d.name not in libs:
                    libs[d.name] = Library(d.name, d, readonly=True, origin="pdk")
        if pdk:
            # layout-only PDK libraries (standard cells etc.)
            for gds in sorted(pdk.root.glob("libs.ref/*/gds/*.gds")):
                lib = gds.parent.parent.name
                if lib not in libs:
                    libs[lib] = Library(lib, gds.parent, readonly=True, origin="pdk", layout_file=gds.name)
        return libs

    def library(self, name: str) -> Library:
        libs = self.libraries()
        if name not in libs:
            raise KeyError(f"no library {name}")
        return libs[name]

    def symbol_paths(self) -> list[Path]:
        """Directories `lib/cell.sym` is looked up under, first hit. A library's parent makes `lib/` resolve, so every
        dependency puts .undine/deps/ there, and with it any directory left in it: one that project.json no longer
        names keeps resolving until `lib sync` removes it, and `lib status` reports it meanwhile. Not one entry per
        declared dependency: the Rust core and Xschem (XSCHEM_LIBRARY_PATH) take the same list of directories, and a
        directory on it cannot hide one child; that would need one parent directory per dependency, another install
        layout."""
        libs = self.libraries()
        paths = [lib.path.parent for lib in libs.values()]
        root = self._deps_root()
        if root is not None:
            paths.append(root)                # also with only transitive installs, or none declared any more
        paths += [lib.path for lib in libs.values()]
        pdk = self.pdk
        if pdk and pdk.xschem_dir:
            paths.append(pdk.xschem_dir)
        paths += [Path(p).expanduser() for p in self.meta.get("symbol_paths", [])]
        xl = tools.xschem_library()
        if xl:
            paths += [xl, xl / "devices"]
        seen, out = set(), []
        for p in paths:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def dependency_cell_names(self) -> dict[str, str]:
        """{casefolded cell name: local name} of the logic cells (top-level .sch, .sym, .sv, .v) of every installed
        dependency, direct ones first: what status compares the project's own cells with."""
        from undine.store import resolve
        out: dict[str, str] = {}
        for lib in self.libraries().values():
            if lib.origin != "dep" or not lib.path.is_dir():
                continue
            logic, _ = resolve.cell_names(p.name for p in lib.path.iterdir())
            for cf in logic:
                out.setdefault(cf, lib.name)
        return out

    def _installed_name(self, name: str) -> str | None:
        """The directory of .undine/deps that is `name` on a case-insensitive file system, if any."""
        root = self._deps_root()
        if root is None:
            return None
        return next((d.name for d in root.iterdir() if not d.name.startswith(".")
                     and d.name.casefold() == name.casefold()), None)

    def create_library(self, name: str) -> Library:
        check_name(name)
        with self.editing():
            libs = self.meta.setdefault("libraries", {})
            if name in libs:
                raise ValueError(f"library {name} exists")
            have = self._installed_name(name)
            if have is not None:
                raise ValueError(f"library {name}: a dependency is installed under the name {have} "
                                 f"(.undine/deps/{have}), and the two would share one symbol path: choose another name")
            d = self.path / "libs" / name
            d.mkdir(parents=True, exist_ok=True)
            libs[name] = f"libs/{name}"
            self.save()
        return self.library(name)

    def public(self) -> dict:
        pdk = self.pdk
        return {
            "name": self.name,
            "pdk": pdk.public() if pdk else None,
            "pdk_requested": self.meta.get("pdk"),
            "pdk_unsupported": pcell.unavailable_reason(pdk) if pcell.legacy(pdk) else None,
            "pdk_upgrade": (lambda u: u.id if u and pcell.macros_dir(u) and u.id != pdk.id else None)(
                pick_pdk(self.pdks, pdk.name)) if pcell.legacy(pdk) else None,
            "created": self.meta.get("created"),
        }


class ProjectStore:
    def __init__(self, root: Path, pdks: dict[str, Pdk]):
        from undine.schematics import netlist

        self.root = root
        self.pdks = pdks
        netlist.protect(*(p.root for p in list(pdks.values()) if getattr(p, "root", None)))   # also PDK schematics reached without a Library (subcells)
        root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[Project]:
        """The projects that can be read; one whose project.json cannot (a git merge left conflict markers in it) is in
        broken()."""
        out = []
        for p in sorted(self.root.glob("*/project.json")):
            try:
                out.append(Project(p.parent, self.pdks))
            except ValueError:            # json.JSONDecodeError
                continue
        return out

    def broken(self) -> dict[str, str]:
        """{name: why} of the projects whose project.json cannot be read."""
        out = {}
        for p in sorted(self.root.glob("*/project.json")):
            try:
                json.loads(p.read_text())
            except ValueError as e:
                out[p.parent.name] = f"project.json cannot be read ({e}): a git merge may have left conflict markers in it"
        return out

    def get(self, name: str) -> Project:
        check_name(name)
        p = self.root / name
        if not (p / "project.json").is_file():
            raise KeyError(f"no project {name}")
        try:
            return Project(p, self.pdks)
        except ValueError as e:           # json.JSONDecodeError: resolving it is git's business, say so
            raise ValueError(f"project {name}: project.json cannot be read ({e}): a git merge may have left conflict "
                             "markers in it; resolve it (the History panel lists the conflict)") from e

    def create(self, name: str, pdk: str | None = None) -> Project:
        check_name(name)
        p = self.root / name
        if (p / "project.json").exists():
            raise ValueError(f"project {name} exists")
        pid = None
        if pdk:  # before creating anything: a failed create must not leave a directory behind
            match = pick_pdk(self.pdks, pdk)
            if match is None:
                raise KeyError(f"no PDK {pdk}")
            pid = match.id  # pin the exact version
        p.mkdir(parents=True, exist_ok=True)
        (p / "project.json").write_text(json.dumps({
            "name": name, "format": FORMAT, "pdk": pid, "libraries": {},
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }, indent=2) + "\n")          # the tool versions are the lock's (undine.lock), not project.json's
        prj = Project(p, self.pdks)
        deps.relock(prj)                      # undine.lock: the PDK and tool versions the project starts with
        return prj


def import_layout(lib: Library, src: Path) -> list[str]:
    """Imports a GDS/OASIS file into the library's layout (cells are merged in); writes the cells that changed."""
    if lib.readonly or lib.layout_file:
        raise ValueError("library is read-only")
    ly = kdb.Layout()
    layoutstore.read(lib.path, ly)
    saved = layoutstore.snapshot(ly)
    ly.read(str(src))
    layoutstore.write(lib.path, ly, saved)
    return sorted(set(layoutstore.snapshot(ly)) - set(saved))
