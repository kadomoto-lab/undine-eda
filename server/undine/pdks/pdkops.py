"""A project's PDK as undine.lock pins it: verify, pin (lock format 2), the fetch plan and the fetch through ciel.

project.json's `pdk` is the requirement (an id, `sky130A@8afc8346a57f`, or a bare name); undine.lock's is the pin. A
format 1 lock pins the id only; format 2 pins the PDK as an object (pdkpin.pin_object: full commit, ciel source,
libraries and one tree hash per part). Format 2 is written only by an explicit command, through upgrade():
`pdk verify PROJECT --pin` and `lib lock PROJECT --format 2` (docs/design/packages.md, Lockfile).

Hashing a PDK reads up to a gigabyte the first time, and ciel fetches for minutes: both run with no project lock held
(deps.meta_lock is taken only to re-read and write the lock) and no server lock (callers keep it free). Refusals are
pin.PinError; a lock this Undine cannot read is deps.DepError and a failed ciel run ciel.CielError, as raised.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import shlex
from pathlib import Path
from typing import Callable

from undine import tools
from undine.pdks import ciel
from undine.pdks import pin as pdkpin
from undine.pdks.pdk import Pdk, family_of, installs
from undine.pdks.pin import PinError
from undine.store import deps

_CIEL = "ciel "                          # a pin's source: "ciel <data source>" (pdkpin.pin_object)


def _cmd(args: str) -> str:
    return f"`python -m undine {args}`"


def _meta(prj) -> dict:
    """project.json as it is on disk now (another writer may have changed it since prj was loaded)."""
    return json.loads((prj.path / "project.json").read_text())


def _no_result(state: str, warnings: list[str] | None = None) -> dict:
    return {"state": state, "changed": [], "missing_libraries": [], "extra_libraries": [], "warnings": warnings or []}


# ------------------------------------------------------------------ which install a lock or a requirement means


def _target(meta: dict, lock: dict | None) -> tuple | None:
    """What upgrade pins, without looking at the disk: None (project.json asks for no PDK), ('pin', variant, commit)
    for a format 2 pin, or ('id', spec) for a format 1 id or project.json's requirement. The lock's pin wins over
    project.json's: the lock is the pin, and a requirement that disagrees with it is reported (pdk_block), not
    silently followed (set-pdk moves both)."""
    if not meta.get("pdk"):
        return None
    locked = (lock or {}).get("pdk")
    if isinstance(locked, dict) and locked.get("variant"):
        return ("pin", locked["variant"], locked.get("commit") or None)
    return ("id", locked if isinstance(locked, str) and locked else meta["pdk"])


def _find(pdks: list[Pdk], variant: str, commit: str | None) -> Pdk | None:
    """The install of exactly this variant and commit (commit None: a plain directory with no version)."""
    return next((p for p in pdks if p.variant == variant and p.commit == commit), None)


def _install(prj, target: tuple, allow_network: bool) -> Pdk:
    """The installed PDK `target` (_target) stands for, a 12-character id expanded to its full commit
    (pdkpin.resolve_commit: an installed commit, else ciel ls-remote when the network is allowed). Refused when it is
    not installed here."""
    pdks = list(prj.pdks.values())
    if target[0] == "pin":
        variant, commit = target[1], target[2]
    else:
        spec = target[1]
        if "@" not in spec:                   # a bare name, or a PDK with no version: as the project opens it
            from undine.store.project import pick_pdk
            found = pick_pdk(prj.pdks, spec)
            if found is None:
                raise PinError(f"the PDK {spec} is not installed here: install it, or run "
                               f"{_cmd('pdk fetch ' + prj.name)} when it has a commit to fetch")
            return found
        variant, prefix = spec.split("@", 1)
        commit = pdkpin.resolve_commit(prefix, variant, pdks, allow_network)
    found = _find(pdks, variant, commit)
    if found is None:
        what = f"{variant}@{commit}" if commit else variant
        raise PinError(f"the PDK {what} is not installed here: run {_cmd('pdk fetch ' + prj.name)} first"
                       if commit else f"the PDK {what} (a plain directory, no version) is not installed here: "
                       f"install it under a PDK root")
    return found


# ------------------------------------------------------------------ format 2


def _prepare(prj, allow_network: bool) -> tuple[tuple | None, Pdk | None, dict | None, dict]:
    """What a format 2 lock needs that takes time, with no lock held: (target, the install, its pin, the tools)."""
    lock = deps.read_lock(prj.path)          # a lock this Undine cannot read: refused before anything is hashed
    target = _target(_meta(prj), lock)
    pdk = _install(prj, target, allow_network) if target else None
    pinned = pdkpin.pin_object(pdk) if pdk else None        # the hash: outside meta_lock
    record = tools.tools_record()                        # the tools answer outside it too
    return target, pdk, pinned, record


def _converter(prj, target: tuple | None, pinned: dict | None, record: dict) -> Callable[[dict | None], dict]:
    def convert(lock: dict | None) -> dict:
        """lock as format 2 (a new dict): the PDK pinned and the tools recorded, the libraries as they are ('sha'
        kept). Refused when the lock or project.json now asks for another PDK than the one hashed."""
        if _target(_meta(prj), lock) != target:
            raise PinError(f"{deps.LOCK} or project.json changed while the PDK was hashed: run the command again; "
                           "nothing was changed")
        new = dict(lock or {"libraries": {}})
        new.update({"format": 2, "pdk": pinned, "tools": record})
        return new
    return convert


def upgrader(prj, allow_network: bool = False) -> Callable[[dict | None], dict]:
    """upgrade() in two halves, for a command that writes the lock itself (deps.relock(prj, upgrade=...), lib lock
    --format 2): the PDK is hashed and the tools asked now, with no lock held; the function returned converts a lock
    in memory, under the caller's lock, and writes nothing. Refusals as upgrade's."""
    target, _, pinned, record = _prepare(prj, allow_network)
    return _converter(prj, target, pinned, record)


def _incompatible(prj, lock: dict | None, pinned: dict | None, pdk: Pdk | None) -> list[dict]:
    """The locked packages that do not accept the PDK as now pinned (compat.locked_incompatible)."""
    from undine.pdks import compat
    try:
        return compat.locked_incompatible(prj.path, lock, compat.project_target(pinned, pdk))
    except (deps.DepError, OSError):     # a linked .undine/deps: status reports it; the pin is written already
        return []


def _upgrade(prj, allow_network: bool) -> tuple[dict, Pdk | None]:
    target, pdk, pinned, record = _prepare(prj, allow_network)
    convert = _converter(prj, target, pinned, record)
    with deps.meta_lock(prj.path):
        lock = deps.read_lock(prj.path)
        created = lock is None
        new = convert(lock)
        wrote = new != lock
        if wrote:
            deps.write_lock(prj.path, new)
    out = {"wrote": wrote, "created": created, "format": 2, "pdk": pinned}
    bad = _incompatible(prj, new, pinned, pdk)    # reads the store: outside meta_lock
    return (out | {"incompatible": bad} if bad else out), pdk


def upgrade(prj, allow_network: bool = False) -> dict:
    """Writes undine.lock as format 2: the PDK pinned (pdkpin.pin_object, a format 1 id's 12 characters expanded to the
    full commit; null when project.json asks for none), the tools as numbers with their origin, the libraries as they
    are. The one way to format 2 (`pdk verify --pin`, `lib lock --format 2`). A lock that is format 2 already gets its
    PDK pinned again as installed now. Refused when the PDK is not installed here. Returns {wrote, created, format,
    pdk}, and `incompatible` (compat.locked_incompatible) when locked packages do not accept the PDK now pinned: a
    warning, never a refusal, since the pin records what is installed, which a format 1 bare or prefix id, or a hand
    edit, may have moved."""
    return _upgrade(prj, allow_network)[0]


def verify(prj, pin: bool = False, allow_network: bool = False) -> dict:
    """Compares the installed PDK with the lock's pin (hashing it in full, from the stamp cache where unchanged).
    Without pin it never writes the lock: a format 1 lock (an id only) or no lock gets state 'not pinned', a pin not
    installed here 'missing'. With pin it writes format 2 (upgrade): on a format 2 lock `changed` and `previous` are
    what differed from the old pin, `state` the PDK against the new one. Returns {state, changed, missing_libraries,
    extra_libraries, warnings, previous, wrote, created, format}, and with pin upgrade's `incompatible` when there is
    one."""
    lock = deps.read_lock(prj.path)
    if not _meta(prj).get("pdk"):
        raise PinError(f"project {prj.name} has no PDK, so there is no PDK to verify or pin: "
                       f"{_cmd('lib lock ' + prj.name + ' --format 2')} writes a format 2 lock without one")
    fmt = deps.lock_format(lock) if lock is not None else None
    locked = (lock or {}).get("pdk")
    before = None
    if isinstance(locked, dict) and isinstance(locked.get("tree"), dict):
        try:
            pdk = _install(prj, ("pin", locked.get("variant"), locked.get("commit") or None), False)
        except PinError as e:
            if pin:
                raise
            return {**_no_result("missing", [str(e)]), "previous": None, "wrote": False, "created": False,
                    "format": fmt}
        before = _verified(pdk, locked)
        if before["state"] == "modified":
            before["warnings"].append(_reinstall(pdk))
    if not pin:
        if before is not None:
            return {**before, "previous": None, "wrote": False, "created": False, "format": fmt}
        try:
            pdk = _install(prj, _target(_meta(prj), lock), allow_network)
        except PinError as e:
            return {**_no_result("missing", [str(e)]), "previous": None, "wrote": False, "created": False,
                    "format": fmt}
        pdkpin.part_hashes(pdk)                  # read now, so that --pin and status find it in the stamp cache
        said = f"{deps.LOCK} pins the PDK by id only (format 1)" if lock else f"the project has no {deps.LOCK}"
        return {**_no_result("not pinned", [f"{said}: {_cmd('pdk verify ' + prj.name + ' --pin')} pins its tree"]),
                "previous": None, "wrote": False, "created": False, "format": fmt}
    up, pdk = _upgrade(prj, allow_network)
    after = _verified(pdk, up["pdk"])
    out = {**after, "previous": before["state"] if before else None}
    if before:
        out["changed"] = before["changed"]
        out["warnings"] = before["warnings"] + [w for w in after["warnings"] if w not in before["warnings"]]
    if up.get("incompatible"):
        out["incompatible"] = up["incompatible"]
    return out | {"wrote": up["wrote"], "created": up["created"], "format": 2}


def _reinstall(pdk: Pdk) -> str:
    """The way back for a modified PDK. The command is built from the install found here, never from the lock's
    strings: a pulled lock could put shell text in its family, and the person pastes this into a shell."""
    if not pdk.commit:
        return ("the PDK files differ from the pin: install it again under its PDK root, or pin it as it is now with "
                "pdk verify --pin")
    rm = f"ciel rm --pdk-family {shlex.quote(family_of(pdk.variant))} {shlex.quote(pdk.commit)}"
    return f"the PDK files differ from the pin: reinstall it (`{rm}`, then pdk fetch), or pin it as it is now with pdk verify --pin"


def _verified(pdk: Pdk, locked: dict, only_cached: bool = False) -> dict:
    """pdkpin.verify, with a file that vanished or cannot be read while the PDK is walked (a `ciel rm` or a fetch into
    it meanwhile) refused as a PinError, as a link in the PDK is."""
    try:
        return pdkpin.verify(pdk, locked, only_cached=only_cached)
    except OSError as e:
        raise PinError(f"its files changed or could not be read while they were checked ({e}): check again when "
                       "nothing is installing or removing it") from None


# ------------------------------------------------------------------ status


def pdk_block(prj, lock: dict | None, only_cached: bool = True) -> tuple[dict, list[str], list[str]]:
    """The PDK part of `lib status`: (block, issues, notes). block = {requested (project.json), locked (the lock's
    id), commit (the pin's full commit), installed (the id found here), state, changed, verified_by, ok}. With
    only_cached (status) the PDK is never read: a stamp cache that is not up to date is 'unchecked', a note.
    Issues: the requirement and the pin disagreeing, the PDK missing, a part modified, a locked library missing.
    Notes: unverified (trust on first use), unchecked, not pinned (format 1), a PDK with no version."""
    requested = prj.meta.get("pdk") or None
    locked = (lock or {}).get("pdk")
    block = {"requested": requested, "locked": deps.lock_pdk_id(lock) if lock else None,
             "commit": locked.get("commit") if isinstance(locked, dict) else None, "installed": None,
             "state": "none", "changed": [], "verified_by": None, "ok": True}
    issues: list[str] = []
    notes: list[str] = []
    here = prj.pdk
    if lock is None or deps.lock_format(lock) == 1:
        # ids compared as today; no lock is status's own issue
        block["installed"] = here.id if here else None
        if requested and here is None:
            block["state"] = "missing"
            issues.append(f"PDK {requested} is not installed here: run {_cmd('pdk fetch ' + prj.name)}")
        elif lock is None:
            block["state"] = "not locked"
        elif block["locked"] != block["installed"]:
            block["state"] = "other version"
            issues.append(f"PDK: locked {block['locked']}, here {block['installed']}")
        elif block["locked"]:
            block["state"] = "not pinned"
            notes.append(f"PDK {block['locked']}: not pinned (format 1): "
                         f"{_cmd('pdk verify ' + prj.name + ' --pin')} pins it")
        block["ok"] = not issues and lock is not None
        return block, issues, notes
    pid = block["locked"]
    if not _satisfies(requested, locked):
        issues.append(f"project.json asks for {requested or 'no PDK'}, {deps.LOCK} pins {pid or 'none'}: "
                      f"{_cmd('set-pdk ' + prj.name + ' ' + (requested or 'PDK'))} to move both")
        block["state"] = "mismatch"
    if not isinstance(locked, dict):
        block["ok"] = not issues
        return block, issues, notes
    if not isinstance(locked.get("tree"), dict):
        issues.append(f"{deps.LOCK} pins the PDK {pid} without its tree hashes: run "
                      f"{_cmd('pdk verify ' + prj.name + ' --pin')}")
        block.update(state="error", ok=False)
        return block, issues, notes
    pdk = _find(list(prj.pdks.values()), locked.get("variant"), locked.get("commit") or None)
    if pdk is None:
        block["state"] = "missing"
        issues.append(f"PDK {pid} is not installed here: run {_cmd('pdk fetch ' + prj.name)}"
                      if locked.get("commit") else
                      f"PDK {pid} (a plain directory, no version) is not installed here: install it under a PDK root")
        block["ok"] = False
        return block, issues, notes
    block["installed"] = pdk.id
    try:
        r = _verified(pdk, locked, only_cached=only_cached)
    except PinError as e:
        issues.append(f"PDK {pid}: {e}")
        block.update(state="error", ok=False)
        return block, issues, notes
    block["changed"] = r["changed"]
    if block["state"] != "mismatch":
        block["state"] = r["state"]
    if r["state"] == "modified":
        for c in r["changed"]:
            files = [f"{c['part']}/{f}" for k in ("modified", "added", "removed") for f in c[k]]
            issues.append(f"PDK {pid}: {c['part']} differs from the pin"
                          + (f" ({_names(files)})" if files else "") + f": {_reinstall(pdk)}")
    elif r["state"] == "unchecked":
        notes.append(f"PDK {pid}: unchecked: run {_cmd('pdk verify ' + prj.name)}")
    elif r["state"] == "unverified":
        notes.append(f"PDK {pid}: unverified (trust on first use): it matches the hashes pinned in {deps.LOCK}, "
                     "which no PDK profile publishes yet")
    for lib in r["missing_libraries"]:
        issues.append(f"PDK {pid}: library {lib} is locked but not installed: run {_cmd('pdk fetch ' + prj.name)}")
    notes += r["warnings"]
    block["ok"] = not issues
    return block, issues, notes


def _satisfies(requested: str | None, locked) -> bool:
    """Whether the lock's pin is what project.json asks for: the same id, or for a bare name that variant."""
    if not isinstance(locked, dict):
        return not requested and not locked
    if not requested:
        return False
    return requested == locked.get("id") or ("@" not in requested and requested == locked.get("variant"))


def _names(files: list[str], most: int = 5) -> str:
    return ", ".join(files[:most]) + (f" and {len(files) - most} more" if len(files) > most else "")


# ------------------------------------------------------------------ fetch


def fetch_plan(prj, roots: list[Path], allow_network: bool = False) -> dict:
    """What `pdk fetch` would run, for the person to confirm: {family, variant, commit, libraries (None: ciel's
    default set), source (ciel's data source), root (str)}. A format 2 pin gives its own family, commit, libraries
    and source; a format 1 id, or project.json's when there is no lock, its full commit (resolve_commit), ciel's
    default libraries and source. Refused for a pin from another installer (volare) and a PDK with no commit. The
    root is the first of `roots` that ciel can install into (pdkpin.fetch_root)."""
    lock = deps.read_lock(prj.path)
    root = pdkpin.fetch_root(roots)           # first: no ciel run (ls-remote) for a fetch that is refused anyway
    locked = (lock or {}).get("pdk")
    if isinstance(locked, dict):
        variant, commit = locked.get("variant"), locked.get("commit")
        if not variant or not commit:
            raise PinError(f"{deps.LOCK} pins the PDK {locked.get('id') or variant} with no commit to fetch: a "
                           f"plain directory is installed by hand")
        source = locked.get("source")
        if not isinstance(source, str) or not source.startswith(_CIEL) or not source[len(_CIEL):].strip():
            raise PinError(f"{deps.LOCK} pins the PDK {locked.get('id')} from {source!r}, not from ciel: Undine "
                           f"fetches PDKs with ciel only; install it with that tool, or pin a ciel install "
                           f"(pdk verify --pin)")
        family = locked.get("family") or family_of(variant)
        libraries = list(locked["libraries"]) if isinstance(locked.get("libraries"), list) else None
        source = source[len(_CIEL):].strip()
    else:
        spec = locked if isinstance(locked, str) and locked else _meta(prj).get("pdk")
        if not spec:
            raise PinError(f"project {prj.name} has no PDK to fetch: set one with "
                           f"{_cmd('set-pdk ' + prj.name + ' PDK')}")
        if "@" not in spec:
            raise PinError(f"the PDK {spec} names no commit, so there is no commit to fetch: set-pdk to an id "
                           f"(variant@commit)")
        variant, prefix = spec.split("@", 1)
        # the installs the project sees and those under the roots (a server started with other roots)
        commit = pdkpin.resolve_commit(prefix, variant, list(prj.pdks.values()) + installs(roots), allow_network)
        family, libraries, source = family_of(variant), None, ciel.DEFAULT_SOURCE
    return {"family": family, "variant": variant, "commit": commit, "libraries": libraries, "source": source,
            "root": str(root)}


def fetch_lock_path(root: Path, family: str, variant: str, commit: str) -> Path:
    """The file fetch() flocks for one PDK commit under a root: <root>/ciel/<family>/.undine-fetch-<commit>.lock,
    beside ciel's versions/ (which PDK discovery reads), shared by the CLI and every server. Its parts are checked as
    ciel.fetch checks them, before the path is made."""
    if family != family_of(variant):
        raise ciel.CielError(f"{family!r} is not the ciel family of {variant!r} (it is {family_of(variant)!r})")
    ciel._check_name("the PDK family", family)
    if not ciel._COMMIT.fullmatch(commit or ""):
        raise ciel.CielError(f"the PDK commit {commit!r} is not a full commit (40 hex characters): resolve it first")
    return Path(root) / "ciel" / family / f".undine-fetch-{commit}.lock"


@contextlib.contextmanager
def _fetching(root: Path, plan: dict, on_output: Callable[[str], None] | None):
    """One `ciel fetch` of a PDK commit into a root at a time, across processes (the CLI, servers on other ports):
    two would extract into one directory, and each hash a tree the other is still writing. An flock, released when
    the process ends, so a crash leaves nothing to clear."""
    family, commit = plan["family"], plan["commit"]
    path = fetch_lock_path(root, family, plan["variant"], commit)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if on_output:
                on_output(f"waiting for another fetch of {family} {commit[:12]} into {root} to finish")
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def fetch(prj, plan: dict, on_output: Callable[[str], None] | None = None, http: bool = False) -> dict:
    """Runs `ciel fetch` for a plan (fetch_plan, confirmed by the person), then hashes the variant installed and
    compares it with the lock. Holds no project or server lock and never writes the lock; the caller rescans the PDKs.
    The fetch and its hash run under the PDK commit's fetch lock (_fetching), so a second fetch of it, from any
    process, waits. From HTTP only ciel's default source is fetched from. Returns {path, verify}."""
    if http and plan["source"] != ciel.DEFAULT_SOURCE:
        raise PinError(f"the PDK's source is {plan['source']!r}, not ciel's default: fetch it from the command line "
                       f"({_cmd('pdk fetch ' + prj.name)})")
    root = Path(plan["root"])
    with _fetching(root, plan, on_output):
        path = ciel.fetch(root, plan["family"], plan["variant"], plan["commit"], plan["libraries"], plan["source"],
                          on_output=on_output)
        pdk = _find(installs([root]), plan["variant"], plan["commit"])
        if pdk is None:                       # ciel.fetch checked the directory: only a broken layout gets here
            raise PinError(f"ciel installed {path}, but it is not found as a PDK under {root}")
        lock = deps.read_lock(prj.path)
        locked = (lock or {}).get("pdk")
        if isinstance(locked, dict) and isinstance(locked.get("tree"), dict):
            result = _verified(pdk, locked)
            if result["state"] == "modified":
                result["warnings"].append(_reinstall(pdk))
        else:
            pdkpin.part_hashes(pdk)              # hashed now, so that --pin and status find it in the stamp cache
            result = _no_result("not pinned", [f"{deps.LOCK} pins the PDK by id only: "
                                               f"{_cmd('pdk verify ' + prj.name + ' --pin')} pins its tree"])
    return {"path": str(path), "verify": result}
