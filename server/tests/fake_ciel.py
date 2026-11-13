"""A fake ciel for tests: no network, no ~/.ciel. Tests point Undine at it with monkeypatch.setenv("UNDINE_CIEL", ...).

    exe = make_fake_ciel(tmp_path, ["8afc8346a57fe1ab7934ba5a6056ea8b43078e71"], log=tmp_path / "ciel.log")

It answers what Undine runs:
- `fetch --pdk-root R --pdk-family F --data-source S [-l LIB]... COMMIT` writes every variant of F under
  R/ciel/F/versions/COMMIT/ (libs.tech, and libs.ref with the -l libraries or a default set), as ciel does.
- `ls-remote --pdk-family F` prints the commits, one per line.
Like ciel it refuses a --pdk-family that is a variant (sky130A) or unknown, and a commit it does not have. Every run
appends its argv (without the program) as one JSON line to `log`; read_log() reads them back.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

DEFAULT_LIBS = {"sky130": ["sky130_fd_pr", "sky130_fd_sc_hd"], "gf180mcu": ["gf180mcu_fd_pr", "gf180mcu_fd_sc_mcu7t5v0"],
                "ihp-sg13": ["sg13g2_pr", "sg13g2_stdcell"]}

_SCRIPT = r'''
import json, sys
from pathlib import Path

CFG = json.loads(%(cfg)r)
args = sys.argv[1:]
if CFG["log"]:
    with open(CFG["log"], "a") as f:
        f.write(json.dumps(args) + "\n")
if CFG["fail"]:
    sys.exit("fake ciel: told to fail")


def opt(name, many=False):
    out, rest, i = [], [], 0
    while i < len(args):
        if args[i] == name and i + 1 < len(args):
            out.append(args[i + 1])
            i += 2
        else:
            rest.append(args[i])
            i += 1
    args[:] = rest
    return out if many else (out[-1] if out else None)


cmd = args.pop(0) if args else ""
family = opt("--pdk-family")
if family not in CFG["variants_of"]:
    sys.exit(f"Error: unknown PDK family {family!r} (one of {', '.join(CFG['variants_of'])})")
if cmd == "ls-remote":
    print("\n".join(CFG["commits"]))
elif cmd == "fetch":
    root, source, libs = opt("--pdk-root"), opt("--data-source"), opt("-l", many=True)
    commit = args[-1] if args else ""
    if commit not in CFG["commits"]:
        sys.exit(f"Error: version {commit} not found for {family}")
    print(f"Fetching {family} {commit} from {source}")
    libs = libs or CFG["default_libs"].get(family, ["default"])
    for variant in CFG["variants_of"][family]:
        v = Path(root) / "ciel" / family / "versions" / commit / variant
        for d in ("libs.tech/klayout", "libs.tech/ngspice"):
            (v / d).mkdir(parents=True, exist_ok=True)
        (v / "libs.tech/ngspice/models.spice").write_text(f"* {variant} {commit}\n")
        for lib in libs:
            (v / "libs.ref" / lib).mkdir(parents=True, exist_ok=True)
            (v / "libs.ref" / lib / "README").write_text(f"{lib} of {variant} {commit}\n")
    print("Done.")
else:
    sys.exit(f"Error: fake ciel has no command {cmd!r}")
'''


def make_fake_ciel(dir: Path, commits: list[str],
                   variants_of: dict[str, list[str]] | None = None,
                   fail: bool = False, log: Path | None = None) -> Path:
    """Writes the fake ciel into `dir` and returns the executable."""
    dir = Path(dir)
    dir.mkdir(parents=True, exist_ok=True)
    cfg = {"commits": list(commits), "fail": fail, "log": str(log) if log else None, "default_libs": DEFAULT_LIBS,
           "variants_of": variants_of if variants_of is not None else
           {"sky130": ["sky130A", "sky130B"], "ihp-sg13": ["ihp-sg13g2"]}}
    script = dir / "fake_ciel_main.py"
    script.write_text(_SCRIPT % {"cfg": json.dumps(cfg)})
    exe = dir / "ciel"
    # a shell wrapper, not a #! line naming this Python: a long interpreter path would not fit a #! line
    exe.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(script))} \"$@\"\n")
    exe.chmod(0o755)
    return exe


def read_log(log: Path) -> list[list[str]]:
    """The argv of each run of the fake, in order."""
    log = Path(log)
    return [json.loads(x) for x in log.read_text().splitlines()] if log.is_file() else []
