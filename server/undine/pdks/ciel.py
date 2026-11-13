"""ciel, the PDK installer Undine wraps (https://github.com/fossi-foundation/ciel).

ciel's --pdk-family takes the family, not a variant: one `ciel fetch --pdk-family sky130 <commit>` installs sky130A and
sky130B under <root>/ciel/sky130/versions/<commit>/. Callers pass pdk.family_of(variant). ciel stays the installer:
Undine runs it, checks that the variant is where ciel puts it, and never parses its output for a path.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable

from undine.pdks.pdk import family_of

DEFAULT_SOURCE = "static-web:https://fossi-foundation.github.io/ciel-releases"

_COMMIT = re.compile(r"[0-9a-f]{40}")
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")     # a family or library name: never read as an option


class CielError(RuntimeError):
    pass


def exe() -> Path | None:
    """The ciel to run: $UNDINE_CIEL first (tests put a fake there; when it is set but not a file, no other ciel is
    run), then the one next to this Python (pixi's), then PATH."""
    forced = os.environ.get("UNDINE_CIEL")
    if forced:
        return Path(forced) if Path(forced).is_file() else None
    beside = Path(sys.executable).with_name("ciel")
    if beside.is_file():
        return beside
    found = shutil.which("ciel")
    return Path(found) if found else None


def _exe() -> Path:
    e = exe()
    if e is None:
        raise CielError("ciel is not installed: run `pixi install`, or set UNDINE_CIEL to a ciel executable")
    return e


def _check_name(what: str, value: str) -> None:
    if not _NAME.fullmatch(value or ""):
        raise CielError(f"{what} {value!r} is not a valid name (letters, digits, '_', '.', '+', '-')")


def _run(argv: list[str], timeout: float, on_output: Callable[[str], None] | None,
         keep: int | None = 20) -> tuple[int, list[str]]:
    """Runs ciel with its stdout and stderr merged, passing each line to on_output; returns the exit code and the
    last `keep` lines (all with None). The environment is passed through, GITHUB_TOKEN with it (ciel's GitHub
    sources rate-limit without one)."""
    lines: deque[str] = deque(maxlen=keep)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         text=True, errors="replace", env=dict(os.environ))
    stopped = threading.Event()

    def stop():
        stopped.set()
        p.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_output:
                on_output(line)
        code = p.wait()
    finally:
        timer.cancel()
        p.stdout.close()
    if stopped.is_set():
        raise CielError(f"ciel {argv[1]} did not finish in {timeout:g} s and was stopped")
    return code, list(lines)


def _failed(cmd: str, code: int, tail: list[str]) -> CielError:
    said = next((x.strip() for x in reversed(tail) if x.strip()), "")
    return CielError(f"ciel {cmd} failed (exit {code})" + (f": {said}" if said else ""))


def variant_dir(root: Path, family: str, variant: str, commit: str) -> Path:
    """Where ciel installs a variant of a commit under a PDK root."""
    return Path(root) / "ciel" / family / "versions" / commit / variant


def fetch(root: Path, family: str, variant: str, commit: str, libraries: list[str] | None,
          source: str = DEFAULT_SOURCE, timeout: float = 3600,
          on_output: Callable[[str], None] | None = None) -> Path:
    """Fetches a PDK commit into a ciel root with `ciel fetch` and returns the variant's directory. With no
    libraries ciel installs its default set. The family must be the variant's (sky130 for sky130A): ciel would
    refuse a variant there, and a wrong family would install another PDK."""
    if family != family_of(variant):
        raise CielError(f"{family!r} is not the ciel family of {variant!r} (it is {family_of(variant)!r})")
    if not _COMMIT.fullmatch(commit or ""):
        raise CielError(f"the PDK commit {commit!r} is not a full commit (40 hex characters): resolve it first")
    _check_name("the PDK family", family)
    for lib in libraries or ():
        _check_name("the library", lib)
    if not source or source.startswith("-"):
        raise CielError(f"the ciel data source {source!r} is not valid")
    argv = [str(_exe()), "fetch", "--pdk-root", str(root), "--pdk-family", family, "--data-source", source]
    for lib in libraries or ():
        argv += ["-l", lib]
    argv.append(commit)
    code, tail = _run(argv, timeout, on_output)
    if code != 0:
        raise _failed("fetch", code, tail)
    out = variant_dir(root, family, variant, commit)
    if not (out / "libs.tech").is_dir():
        raise CielError(f"ciel fetch finished but {out / 'libs.tech'} is not there: check the PDK root and the "
                        f"family ({family}) for {variant}")
    return out


def ls_remote(family: str, timeout: float = 120) -> list[str]:
    """The commits ciel's source has for a family (`ciel ls-remote`, which prints one per line when not on a
    terminal). This uses the network."""
    _check_name("the PDK family", family)
    code, lines = _run([str(_exe()), "ls-remote", "--pdk-family", family], timeout, None, keep=None)
    if code != 0:
        raise _failed("ls-remote", code, lines)
    return [x.strip() for x in lines if re.fullmatch(r"[0-9a-f]{7,64}", x.strip())]
