"""Whether a package's [pdk] requirement (manifest.PdkReq) accepts a project's PDK (docs/design/packages.md, PDK
compatibility; Resolution 2 and 6).

PDK commits have no order, so a package names commits, or compatibility tags that stand for sets of commits. Tags are
defined by PDK profile packages (phase 6, Decisions 10); until then tag_commits knows none, so a tag adds nothing and a
package that names only tags is refused with a hint to list its commits.

Pure but for locked_incompatible: nothing else here reads the disk, and nothing reads the network. A format 1 id's
12-hex prefix is matched against full commits as it is (no pin.resolve_commit), and a PDK without a version is refused
rather than taken as compatible.

locked_incompatible checks every package a lock pins against a PDK (set-pdk refuses with its list, IncompatiblePdk;
pdk verify --pin and lib lock --format 2 report it), reading each manifest from the store or the install
(store/locked.py), never from git.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pathlib import Path

from undine.pdks.pdk import Pdk, family_of

_HEX = re.compile(r"[0-9a-f]{7,64}")
_FULL = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass
class Target:
    """The PDK a package is checked against. commit is the full commit when known; prefix is a format 1 id's hex
    (12 characters) when only that is known; neither for a PDK without a version (a plain directory)."""
    family: str
    variant: str
    commit: str | None = None
    prefix: str | None = None
    id: str | None = None
    root: str | None = None


def _from_id(pdk_id: str) -> Target | None:
    """A format 1 id: 'sky130A@8afc8346a57f', or a bare variant for a PDK without a version."""
    variant, _, ver = pdk_id.partition("@")
    if not variant:
        return None
    ver = ver.lower()
    commit = ver if _FULL.fullmatch(ver) else None
    prefix = ver if commit is None and _HEX.fullmatch(ver) else None
    return Target(family_of(variant), variant, commit, prefix, pdk_id, None)


def project_target(lock_pdk: dict | str | None, installed: Pdk | None) -> Target | None:
    """The project's PDK as the lock pins it: a format 2 pin object first, else the installed PDK, else a format 1 id
    (a string, or deps.lock_pdk's {"id": ...}). None when the project has no PDK."""
    if isinstance(lock_pdk, dict) and lock_pdk.get("variant"):
        variant = lock_pdk["variant"]
        commit = (lock_pdk.get("commit") or "").lower() or None
        same = installed is not None and installed.variant == variant
        pdk_id = lock_pdk.get("id") or (f"{variant}@{commit[:12]}" if commit else variant)
        return Target(family_of(variant), variant, commit, None, pdk_id, str(installed.root) if same else None)
    if installed is not None:
        return Target(family_of(installed.variant), installed.variant, installed.commit, None, installed.id,
                      str(installed.root))
    if isinstance(lock_pdk, dict):
        lock_pdk = lock_pdk.get("id")
    if isinstance(lock_pdk, str) and lock_pdk:
        return _from_id(lock_pdk)
    return None


def tag_commits(family: str, tag: str) -> set[str] | None:
    """The commits a compatibility tag stands for, as its PDK profile package publishes them. Profile packages come in
    phase 6 (the seam beside pin.expected_trees): until then no tag is known, and this returns None."""
    return None


def _tags(req) -> tuple[set[str], list[str]]:
    """The commits of req's compat tags, and the tags this Undine does not know."""
    commits: set[str] = set()
    unknown: list[str] = []
    for t in req.compat or ():
        family, _, tag = t.partition("/")
        found = tag_commits(family, tag)
        if found is None:
            unknown.append(t)
        else:
            commits |= found
    return commits, unknown


def _accepted(req) -> set[str]:
    """The commits req accepts: its commits plus those of its known tags; built_with alone when it names neither."""
    if req.commits or req.compat:
        return set(req.commits or ()) | _tags(req)[0]
    return {req.built_with} if req.built_with else set()


def _matches(target: Target, accepted: set[str]) -> bool:
    if target.commit:
        return target.commit in accepted
    return any(c.startswith(target.prefix) for c in accepted)


def _short(commits) -> str:
    return ", ".join(c[:12] for c in sorted(commits))


def check(req, target: Target | None) -> str | None:
    """Why req does not accept target, or None when it does. The first reason that applies, in this order: no PDK,
    family, variant, a PDK without a version, a commit not accepted."""
    if target is None:
        return "this project has no PDK"
    if family_of(target.variant) != req.family:
        return f"family {req.family}, this project is {family_of(target.variant)}"
    if target.variant not in (req.variants or ()):
        return f"variant {target.variant} not among {', '.join(req.variants or ())}"
    if not (req.commits or req.compat or req.built_with):
        return None
    accepted = _accepted(req)
    pdk_id = target.id or target.variant
    if not (target.commit or target.prefix):
        if accepted:
            where = f"the PDK at {target.root}" if target.root else f"the PDK {pdk_id}"
            return f"{where} has no version, so its commit cannot be checked: install it with pdk fetch"
    elif _matches(target, accepted):
        return None
    why = f"{pdk_id} is not among commits {_short(accepted) or '(none)'}"
    unknown = _tags(req)[1]
    if not req.commits and unknown:
        tags = ", ".join(unknown)
        why += (f"; tag {tags} is not known to this Undine (tags come with PDK profiles, phase 6): list the commit "
                f"under `commits`")
    return why


def note(req, target: Target | None) -> str | None:
    """'drawn on <built_with>, used at <commit>' when req accepts target at a commit other than the one its layouts
    were drawn on; None otherwise."""
    if not req.built_with or target is None or check(req, target) is not None:
        return None
    used = target.commit or target.prefix
    if not used or req.built_with.startswith(used):
        return None
    return f"drawn on {req.built_with[:12]}, used at {used[:12]}"


def describe(req) -> str:
    """req in one line: 'sky130 [sky130A] commits 8afc8346a57f; tags sky130/2025; built with 8afc8346a57f'."""
    text = f"{req.family} [{', '.join(req.variants or ())}]"
    parts = []
    if req.commits:
        parts.append(f"commits {_short(req.commits)}")
    if req.compat:
        parts.append(f"tags {', '.join(req.compat)}")
    if req.built_with:
        parts.append(f"built with {req.built_with[:12]}")
    return f"{text} {'; '.join(parts)}" if parts else text


# ------------------------------------------------------------------ the locked packages


class IncompatiblePdk(ValueError):
    """set-pdk refused: locked packages that do not accept the PDK. .packages: locked_incompatible's list."""

    def __init__(self, pdk_id: str, packages: list[dict]):
        self.packages = packages
        lines = "\n".join(f"- {p['chain']}: {p['reason']}" for p in packages)
        n = len(packages)
        super().__init__(f"the PDK {pdk_id} is not accepted by {n} locked package{'s' if n != 1 else ''}:\n{lines}\n"
                         "update or remove them (lib update, lib remove), or choose a PDK they accept; "
                         "nothing was changed")


def _project_name(prj_path: Path) -> str:
    """project.json's name, for the chains; the directory's name when it cannot be read."""
    import json
    try:
        name = json.loads((Path(prj_path) / "project.json").read_text()).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    return name if isinstance(name, str) and name else Path(prj_path).name


def _row(g, root: str, id: str, req=None, version=None, reason: str = "") -> dict:
    e = g.entries[id]
    chains = g.chains(id)
    chain = g.chain_text(root, chains[0]) if chains else g.chain_text(root, [id])
    return {"name": g.names_of(id)[0], "package": e.get("package") if isinstance(e.get("package"), str) else None,
            "version": version if version is not None else e.get("version"), "chain": chain, "reason": reason,
            "family": req.family if req else None, "variants": list(req.variants or ()) if req else [],
            "commits": list(req.commits or ()) if req else [], "compat": list(req.compat or ()) if req else [],
            "built_with": req.built_with if req else None}


def locked_incompatible(prj_path: Path, lock: dict | None, target: Target | None) -> list[dict]:
    """The packages `lock` pins whose [pdk] does not accept target, direct ones first, then those reached through
    them: [{name (the first local name), package, version, chain ('<project> -> analog_ip 1.2.0 (v1.2.0 @
    229914698b1f)'), reason (check's, or why the manifest could not be read), family, variants, commits, compat,
    built_with}].

    A package (an entry with `package`) whose files are neither in the store nor installed is listed with
    'run lib sync first', and one whose manifest cannot be read with that error: neither can be shown to accept it.
    An entry without `package` (a plain git dependency, or a phase 1 one) is checked only when its manifest can be
    read and has [pdk]; otherwise it is skipped, as are packages without [pdk] (status notes those)."""
    from undine.store import locked, resolve
    from undine.store.deps import DepError
    from undine.store.manifest import ManifestError
    g = resolve.lock_graph(lock)
    root = _project_name(prj_path)
    out: list[dict] = []
    order = [*g.direct.values(), *(i for i in g.entries if i not in g.direct.values())]
    for id in order:
        entry = g.entries[id]
        package = isinstance(entry.get("package"), str) and bool(entry.get("package"))
        try:
            m = locked.manifest_of(Path(prj_path), g.names_of(id)[0], entry)
        except (locked.NotAvailable, ManifestError) as e:
            if package:
                out.append(_row(g, root, id, reason=str(e)))
            continue
        except (DepError, OSError):       # a linked .undine/deps: a package's is the caller's to report
            if package:
                raise
            continue
        if m is None or m.pdk is None:
            continue
        why = check(m.pdk, target)
        if why is not None:
            out.append(_row(g, root, id, m.pdk, str(m.version), why) | {"package": m.name})
    return out
