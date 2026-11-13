"""The agent guide shipped with Undine (data/skills/undine): an Agent Skill, a SKILL.md entry with one reference per
task, so an agent reads only what the task needs. The same files are served to MCP clients as resources
(undine://skills/undine/...), printed by `undine skills show`, and copied into an agent's skills folder by
`undine skills install` (e.g. ~/.claude/skills/undine for Claude Code)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "skills"
SCHEME = "undine://skills/"


def files() -> list[Path]:
    """Every file of every skill, SKILL.md first."""
    return sorted(ROOT.rglob("*.md"), key=lambda p: (p.parent.parent != ROOT, str(p)))


def _summary(p: Path) -> str:
    text = p.read_text()
    m = re.search(r'^description:\s*"?(.*?)"?\s*$', text, re.M)
    if m and p.name == "SKILL.md":
        return m.group(1)
    head = next((ln for ln in text.splitlines() if ln.startswith("# ")), p.stem)
    return head.removeprefix("# ").strip()


def resources() -> list[dict]:
    """MCP resources/list entries."""
    return [{"uri": SCHEME + p.relative_to(ROOT).as_posix(), "name": p.relative_to(ROOT).as_posix(),
             "description": _summary(p), "mimeType": "text/markdown"} for p in files()]


def read(uri_or_path: str) -> str:
    """A file by its resource URI, its path under skills/ ('undine/references/layout.md'), or a short name
    ('layout', 'references/layout', 'SKILL'). Never outside the skills folder."""
    rel = uri_or_path.removeprefix(SCHEME).strip("/")
    cands = [rel, f"undine/{rel}", f"undine/{rel}.md", f"undine/references/{rel}", f"undine/references/{rel}.md"]
    for c in cands:
        p = (ROOT / c).resolve()
        if p.is_file() and ROOT.resolve() in p.parents and p.suffix == ".md":
            return p.read_text()
    raise KeyError(f"no such guide: {uri_or_path} (see `undine skills`)")


def install(dest: Path) -> Path:
    """Copy each skill folder into dest (an agent's skills folder); an older copy is replaced."""
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    for skill in sorted(d for d in ROOT.iterdir() if (d / "SKILL.md").is_file()):
        target = dest / skill.name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
    return dest


def add_parser(sub) -> None:
    s = sub.add_parser("skills", help="the agent guide (an Agent Skill): list, show, or install into an agent's skills folder")
    s.add_argument("action", choices=["list", "show", "install"], nargs="?", default="list")
    s.add_argument("name", nargs="?", help="show: SKILL, or a reference like layout (default SKILL)")
    s.add_argument("--dest", type=Path, default=Path("~/.claude/skills"), help="install: the skills folder (default ~/.claude/skills)")


def main(args) -> None:
    import sys

    if args.action == "show":
        sys.stdout.write(read(args.name or "SKILL.md"))
    elif args.action == "install":
        print(f"installed the undine skill into {install(args.dest) / 'undine'}")
    else:
        for r in resources():
            print(f"{r['name']:<36} {r['description'][:90]}")
