"""Locates external tools (ngspice, KLayout, Xschem, Netgen).

Search order (search): $UNDINE_TOOLS/bin (set to the pixi environment by `pixi run`, and by the MCP launcher for a
server it starts from one: tool_env), then the bin directory of the server's own Python environment, then PATH (e.g.
the KLayout application from klayout.de). With $UNDINE_TOOLS unset, the server's own environment comes first however
the server was started (a pixi, conda or virtual environment: own_env), then ~/undine-tools (e.g. an older conda
prefix), then PATH: an older ngspice in ~/undine-tools never stands in for the environment's own.

A lock records the tools as numbers with their origin (tools_record): the version number parsed from each banner, where
the binary came from (locate), Undine's own version and the hash of its pixi.lock, which pins the pixi tools.
"""
from __future__ import annotations

import functools
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def cache_root() -> Path:
    """Undine's cache: $UNDINE_CACHE, else $XDG_CACHE_HOME/undine (by default ~/.cache/undine). Absolute: a relative
    $UNDINE_CACHE is taken from the working directory, a relative $XDG_CACHE_HOME is ignored (as the XDG spec says)."""
    if os.environ.get("UNDINE_CACHE"):
        return Path(os.environ["UNDINE_CACHE"]).expanduser().absolute()
    xdg = os.environ.get("XDG_CACHE_HOME") or ""
    return (Path(xdg) if os.path.isabs(xdg) else Path.home() / ".cache").absolute() / "undine"


def tools_prefix() -> Path | None:
    """$UNDINE_TOOLS, else ~/undine-tools: a prefix whose bin/ holds tools; None when it has none."""
    p = Path(os.environ.get("UNDINE_TOOLS") or Path.home() / "undine-tools").expanduser()
    return p if (p / "bin").is_dir() else None


def own_env() -> Path | None:
    """The running interpreter's own environment (sys.prefix) when it is one: a pixi / conda environment or a virtual
    environment, not a system interpreter's prefix (/usr), whose tools ~/undine-tools may be there to replace."""
    p = Path(sys.prefix)
    if (p / "bin").is_dir() and (sys.prefix != sys.base_prefix or (p / "conda-meta").is_dir()):
        return p
    return None


def tool_env() -> Path | None:
    """own_env when it holds Undine's tools (its bin has ngspice, as the pixi environment does), else None: a server's
    tool prefix ($UNDINE_TOOLS) may be set to it without losing ~/undine-tools, which a plain virtual environment or
    a system interpreter leaves to supply them."""
    p = own_env()
    return p if p is not None and (p / "bin" / "ngspice").is_file() else None


def _ahead() -> list[Path]:
    """The bin directories ahead of PATH and of the server's own bin, in order: env() puts them first on PATH."""
    first = [] if os.environ.get("UNDINE_TOOLS") else [own_env()]
    out: list[Path] = []
    for p in (*first, tools_prefix()):
        if p is not None and p / "bin" not in out:
            out.append(p / "bin")
    return out


def search() -> list[Path]:
    """The bin directories find looks in before PATH, in order (the module docstring)."""
    out, own = _ahead(), Path(sys.prefix) / "bin"
    return out if own in out else [*out, own]


def _is_pixi(prefix: Path) -> bool:
    """Whether a prefix is a pixi environment (<project>/.pixi/envs/<env>), also through a link to one."""
    for p in (prefix, prefix.resolve()):
        if len(p.parts) >= 3 and p.parts[-3:-1] == (".pixi", "envs"):
            return True
    return False


def _origin(d: Path) -> str:
    """Where a bin directory of search() comes from, as a lock records it: pixi pins its tools whichever way they are
    reached ($UNDINE_TOOLS, which pixi.toml sets to the environment, or the server's own environment)."""
    named = os.environ.get("UNDINE_TOOLS")
    if _is_pixi(d.parent):
        return "pixi"
    if named and d == Path(named).expanduser() / "bin":
        return "UNDINE_TOOLS"
    if not named and d == Path.home() / "undine-tools" / "bin":
        return "undine-tools"
    return "env"                                      # a conda or virtual environment of the server, or sys.prefix


def locate(name: str) -> tuple[str, str] | None:
    """The executable find finds and its origin: 'pixi', 'UNDINE_TOOLS', 'undine-tools', 'env' or 'PATH'."""
    for d in search():
        if (d / name).is_file():
            return str(d / name), _origin(d)
    exe = shutil.which(name)
    return (exe, "PATH") if exe else None


def find(name: str) -> str | None:
    return (locate(name) or (None, None))[0]


def xschem_library() -> Path | None:
    pre = tools_prefix()
    for base in ([pre] if pre else []) + [Path("/usr"), Path("/usr/local")]:
        p = base / "share" / "xschem" / "xschem_library"
        if p.is_dir():
            return p
    return None


def env() -> dict[str, str]:
    """Environment for child processes (headless Qt, tool prefix libraries)."""
    e = dict(os.environ)
    pre = tools_prefix()
    lead = _ahead()                                   # PATH as find looks (search)
    if lead:
        e["PATH"] = ":".join([*map(str, lead), *([e["PATH"]] if e.get("PATH") else [])])
    if pre and not os.environ.get("PIXI_ENVIRONMENT_NAME") and pre.resolve() != Path(sys.prefix).resolve():
        # an older conda prefix may need it; pixi's binaries find their libraries themselves (also in a server the
        # MCP launcher started, $UNDINE_TOOLS its own environment), and a system KLayout started from a pixi shell must
        # not load the environment's libraries
        e["LD_LIBRARY_PATH"] = f"{pre / 'lib'}:{e.get('LD_LIBRARY_PATH', '')}"
    own = Path(sys.prefix) / "bin"                    # tools of the server's environment call each other (Verilator -> make -> c++)
    if own.is_dir() and str(own) not in e.get("PATH", "").split(":"):
        e["PATH"] = f"{e.get('PATH', '')}:{own}" if lead else f"{own}:{e.get('PATH', '')}"
    e.setdefault("QT_QPA_PLATFORM", "offscreen")
    return e


@functools.cache
def ngspice_osdi(exe: str) -> bool:
    """Whether this ngspice loads OSDI models (Verilog-A compiled by OpenVAF): the osdi command exists only then."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "probe.cir").write_text("* osdi probe\n.control\nosdi\n.endc\n.end\n")
            r = subprocess.run([exe, "-b", "probe.cir"], cwd=d, capture_output=True, text=True, timeout=20, env=env(),
                               stdin=subprocess.DEVNULL)
        return "no such command" not in r.stdout + r.stderr
    except Exception:
        return False


def versions() -> dict[str, str | None]:
    return {n: find(n) for n in ("ngspice", "klayout", "xschem", "netgen")}


_VERSION_FLAGS = {"klayout": ["-v"], "magic": ["--version"], "netgen": ["-batch", "quit"], "ngspice": ["-v"],
                  "xschem": ["-v"]}


@functools.cache
def _version(exe: str, name: str) -> str:
    try:
        r = subprocess.run([exe, *_VERSION_FLAGS.get(name, ["--version"])], capture_output=True, text=True, timeout=20,
                           env=env(), stdin=subprocess.DEVNULL)
        line = next((l for l in (r.stdout + r.stderr).splitlines() if l.strip() and re.search(r"\d", l)), "")
        line = re.sub(r"\s+compiled on .*", "", line.strip(" *\t"))      # Netgen's build date, ngspice's banner stars
        return line[:80] or "?"
    except Exception:
        return "?"


def version(name: str) -> str | None:
    """The tool's version line (e.g. "KLayout 0.30.12"), or None when it is not installed. Asked once per executable."""
    exe = find(name)
    return _version(exe, name) if exe else None


TOOLS = ("klayout", "xschem", "ngspice", "magic", "netgen")      # the tools a lock records

# One rule per tool for the version number in today's banner (version()); group 1 is the number.
VERSION_RULES = {
    "klayout": r"KLayout (\d+(\.\d+)+)",          # KLayout 0.28.17
    "xschem": r"XSCHEM V(\d+(\.\d+)+)",           # XSCHEM V3.4.5
    "ngspice": r"ngspice-(\d+(\.\d+)*)",          # ngspice-47 : Circuit level simulation program
    "magic": r"^(\d+(\.\d+)+)$",                  # 8.3.678
    "netgen": r"Netgen (\d+(\.\d+)+)",            # Netgen 1.5.272 (its build date cut, as version() does)
}


def parse_version(name: str, banner: str | None) -> str | None:
    """The version number in a tool's banner, or None when no rule matches (or the tool did not answer: '?')."""
    rule = VERSION_RULES.get(name)
    if not rule or not banner:
        return None
    m = re.search(rule, re.sub(r"\s+compiled on .*", "", banner.strip()))
    return m.group(1) if m else None


def tool_record(name: str) -> dict | None:
    """What a lock records of one tool: {'version': number, 'from': origin}, with the banner as 'text' when no number
    could be read from it (a banner no rule matches, or '?' when the tool did not answer). None: not installed."""
    found = locate(name)
    if found is None:
        return None
    banner = _version(found[0], name)
    number = parse_version(name, banner) if banner != "?" else None
    return {"version": number, "from": found[1]} | ({} if number else {"text": banner})


def _repo() -> Path:
    """The checkout this module runs from, when it runs from one (server/undine/tools.py)."""
    return Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str | None:
    """A git command's output in repo, or None when it fails: no git, no checkout, a timeout."""
    e = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    try:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=10, env=e,
                           stdin=subprocess.DEVNULL)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _package_version(repo: Path) -> str:
    """Undine's version: the installed package's, else server/pyproject.toml's (a checkout run without installing)."""
    import importlib.metadata
    try:
        return importlib.metadata.version("undine-eda")
    except Exception:
        pass
    try:
        m = re.search(r'^version\s*=\s*"([^"]+)"', (repo / "server" / "pyproject.toml").read_text(), re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def _undine_version(repo: Path) -> str:
    ver = _package_version(repo)
    if not (repo / ".git").exists():         # installed outside a checkout: git would find whatever repo encloses it
        return ver
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    if not commit:
        return ver
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return f"{ver} ({commit}{'+dirty' if dirty else ''})"


@functools.cache
def undine_version() -> str:
    """Undine's version and commit, as a lock records it: '0.1.0 (3f9c2e1)', '+dirty' after the commit when tracked
    files are modified, the version alone outside a git checkout."""
    return _undine_version(_repo())


def pixi_lock_sha() -> str | None:
    """'sha256:<hex>' of Undine's pixi.lock when the server runs in that checkout's pixi environment, else None: then
    nothing pins the tools."""
    repo = _repo()
    lock, envs = repo / "pixi.lock", (repo / ".pixi" / "envs").resolve()
    if not lock.is_file() or not Path(sys.prefix).resolve().is_relative_to(envs):
        return None
    try:
        return "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
    except OSError:
        return None


def tools_record() -> dict:
    """The lock's tools: Undine's version, the pixi.lock hash and each tool's record (None when not installed)."""
    return {"undine": undine_version(), "pixi_lock": pixi_lock_sha(), **{n: tool_record(n) for n in TOOLS}}
