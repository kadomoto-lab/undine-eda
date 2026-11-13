"""PDK registry.

PDKs live only on the server. The API exposes what a designer needs to see
(name, version, layer display names and colours, primitive device list);
models, rule decks and PCell implementations are never sent to the client.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Where IHP's own script compiles its Verilog-A models inside the PDK tree (installs set up by an earlier Undine have
# them).
LEGACY_OSDI = "libs.tech/ngspice/osdi"

# ciel's PDK families and the variants each installs (`ciel ls-pdks`): `ciel fetch --pdk-family` takes the family, and
# one fetch installs every variant of it.
FAMILY_OF_VARIANT = {
    "sky130A": "sky130", "sky130B": "sky130",
    "gf180mcuA": "gf180mcu", "gf180mcuB": "gf180mcu", "gf180mcuC": "gf180mcu", "gf180mcuD": "gf180mcu",
    "ihp-sg13g2": "ihp-sg13", "ihp-sg13cmos5l": "ihp-sg13",
}


def family_of(variant: str) -> str:
    """The ciel family of a PDK variant (sky130A -> sky130, ihp-sg13g2 -> ihp-sg13). A variant ciel does not list
    keeps the old rule (a trailing capital letter dropped), except that every ihp-sg13* is in ihp-sg13."""
    if variant in FAMILY_OF_VARIANT:
        return FAMILY_OF_VARIANT[variant]
    if variant.startswith("ihp-sg13"):
        return "ihp-sg13"
    return re.sub(r"[A-Z]$", "", variant)


@dataclass
class Pdk:
    name: str           # e.g. sky130A
    family: str         # e.g. sky130
    version: str        # open_pdks commit hash (ciel / volare version)
    root: Path          # <pdk>/ (contains libs.tech, libs.ref)
    lyp: Path | None = None
    xschem_dir: Path | None = None
    klayout_tech: Path | None = None
    extra: dict = field(default_factory=dict)
    source: str | None = None   # how it was installed: "ciel", "volare", or None for a plain root

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version[:12]}" if self.version else self.name

    @property
    def commit(self) -> str | None:
        """The full commit of the PDK's source, None when the install does not say (a plain root)."""
        return self.version or None

    @property
    def variant(self) -> str:
        return self.name

    def libraries(self) -> list[str]:
        """The libraries installed with it (the directories of libs.ref), which `ciel fetch -l` selects."""
        ref = self.root / "libs.ref"
        return sorted(d.name for d in ref.iterdir() if d.is_dir()) if ref.is_dir() else []

    @property
    def profile(self):
        from undine.pdks import pdkprofile
        return pdkprofile.for_pdk(self.name, self.family)

    @property
    def decks_dir(self) -> Path | None:
        """Where the KLayout DRC / LVS decks are (drc/, lvs/): libs.tech/klayout, or a subdirectory of it."""
        if self.klayout_tech is None:
            return None
        return self.klayout_tech / self.profile.klayout_dir if self.profile.klayout_dir else self.klayout_tech

    def _local_key(self) -> str:
        """What tells apart PDKs that have no version: where they are (the first 12 hex of the real root's SHA-256)."""
        return hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:12]

    @property
    def osdi_dir(self) -> Path:
        """Where `pixi run osdi` (scripts/osdi.sh) writes this PDK's compiled Verilog-A models: the store's
        build/<commit>/<variant>/osdi, so that the PDK stays as installed and every project of the user shares them.
        A PDK without a version is told apart by where it is: build/local-<hash of its root>/<variant>/osdi."""
        from undine.home import store_root
        key = self.commit or f"local-{self._local_key()}"
        return store_root() / "build" / key / self.variant / "osdi"

    @property
    def old_osdi_cache(self) -> Path:
        """Where an earlier Undine compiled the models (Undine's cache, osdi/<id>). Still read until osdi.sh moves them
        into osdi_dir; nothing writes there any more."""
        from undine import tools
        return tools.cache_root() / "osdi" / (self.id if self.version else f"{self.name}-{self._local_key()}")

    def _osdi_found(self) -> list[tuple[Path, bool]]:
        """For each model of the profile, in its order: the file to load and whether it comes from the old cache.
        The first of osdi_dir, the PDK's own libs.tech/ngspice/osdi (compiled there by an older setup, or by hand)
        and the old cache that has it; osdi_dir when none has it."""
        out = []
        for m in self.profile.osdi:
            name = f"{m}.osdi"
            where = [self.osdi_dir / name, self.root / LEGACY_OSDI / name, self.old_osdi_cache / name]
            i = next((i for i, f in enumerate(where) if f.is_file()), 0)
            out.append((where[i], i == 2))
        return out

    def osdi_files(self) -> list[Path]:
        """The compiled models ngspice must load (profile.osdi), in profile order; a missing one is named by where it
        should be (osdi_dir)."""
        return [f for f, _ in self._osdi_found()]

    def legacy_osdi_cached(self) -> bool:
        """Whether a model is found only in the old cache: `pixi run osdi` moves it into the store."""
        return any(old for _, old in self._osdi_found())

    def public(self) -> dict:
        p = self.profile
        return {
            "profile": {"label": p.label, "verified": p.verified, "max_metal": p.max_metal,
                        "magic": bool(p.magic_rc), "pcells": p.pcells, "limits": list(p.limits)},
            "id": self.id,
            "name": self.name,
            "family": self.family,
            "version": self.version,
            "has_layer_props": self.lyp is not None,
            "has_symbols": self.xschem_dir is not None,
        }


def _scan_pdk(root: Path, family: str, version: str) -> Pdk:
    tech = root / "libs.tech"
    lyps = sorted((tech / "klayout").glob("**/*.lyp")) if (tech / "klayout").is_dir() else []
    lyp = None
    for cand in lyps:  # prefer <name>.lyp, then <family>.lyp
        if cand.stem in (root.name, family):
            lyp = cand
            break
    if lyp is None and lyps:
        lyp = lyps[0]
    xs = tech / "xschem"
    return Pdk(
        name=root.name, family=family, version=version, root=root, lyp=lyp,
        xschem_dir=xs if xs.is_dir() else None,
        klayout_tech=(tech / "klayout") if (tech / "klayout").is_dir() else None,
    )


def _walk(pdk_roots: list[Path]):
    """Every install in PDK_ROOT style directories (open_pdks, ciel or volare layout), in scan order. A directory
    reached twice (the plain symlink `ciel enable` makes, or a root listed twice) is one install."""
    seen: set[Path] = set()

    def new(root: Path) -> bool:
        real = root.resolve()
        if real in seen:
            return False
        seen.add(real)
        return True

    for base in pdk_roots:
        base = Path(base).expanduser()
        if not base.is_dir():
            continue
        # ciel (and its predecessor volare): <root>/{ciel,volare}/<family>/versions/<hash>/<name>
        for source in ("ciel", "volare"):
            for vdir in sorted(base.glob(f"{source}/*/versions/*")):
                if not vdir.is_dir():
                    continue
                family, version = vdir.parent.parent.name, vdir.name
                for root in sorted(vdir.iterdir()):
                    if (root / "libs.tech").is_dir() and new(root):
                        pdk = _scan_pdk(root, family, version)
                        pdk.source = source
                        yield pdk
        # plain: <root>/<name>/libs.tech (symlinks to the enabled ciel / volare version)
        for root in sorted(base.iterdir()):
            if (root / "libs.tech").is_dir() and new(root):
                real = root.resolve()
                version = real.parent.name if real.parent.parent.name == "versions" else ""
                pdk = _scan_pdk(root, family_of(root.name), version)
                pdk.name = root.name
                yield pdk


def discover(pdk_roots: list[Path]) -> dict[str, Pdk]:
    """Finds PDKs in PDK_ROOT style directories, by id; the first install of an id wins."""
    found: dict[str, Pdk] = {}
    for pdk in _walk(pdk_roots):
        found.setdefault(pdk.id, pdk)
    return found


def installs(pdk_roots: list[Path]) -> list[Pdk]:
    """Every install, also those discover() hides behind one id: two commits sharing a 12-character prefix, or one
    commit under two roots (resolving a prefix and hashing need to see each)."""
    return list(_walk(pdk_roots))


def default_pdk_roots() -> list[Path]:
    roots = []
    if os.environ.get("PDK_ROOT"):
        roots.append(Path(os.environ["PDK_ROOT"]))
    roots += [Path.home() / ".ciel", Path.home() / ".volare"]
    return roots
