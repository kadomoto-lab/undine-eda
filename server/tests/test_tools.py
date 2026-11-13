"""Where the external tools are found (tools.py): $UNDINE_TOOLS, else the server's own environment before
~/undine-tools, however the server was started (the MCP launcher starts it with the environment's Python, without
`pixi run`), and the PATH its child processes get."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402


def _tool(d: Path, name: str = "ngspice") -> str:
    (d / "bin").mkdir(parents=True, exist_ok=True)
    (d / "bin" / name).write_text("#!/bin/sh\n")
    os.chmod(d / "bin" / name, 0o755)
    return str(d / "bin" / name)


def _envs(tmp_path, monkeypatch, conda=True):
    env, home = tmp_path / "pixi-env", tmp_path / "home"
    (env / "conda-meta").mkdir(parents=True) if conda else env.mkdir()
    monkeypatch.setattr(sys, "prefix", str(env))
    monkeypatch.setattr(sys, "base_prefix", str(env))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UNDINE_TOOLS", raising=False)
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    return env, home / "undine-tools"


def test_without_undine_tools_the_servers_own_environment_comes_before_undine_tools(tmp_path, monkeypatch):
    env, old = _envs(tmp_path, monkeypatch)
    ours, theirs = _tool(env), _tool(old)
    _tool(old, "klayout")
    assert tools.find("ngspice") == ours                     # not an older ngspice in ~/undine-tools
    assert tools.find("klayout") == str(old / "bin" / "klayout")     # what the environment lacks: from there still
    path = tools.env()["PATH"].split(":")
    assert path[:2] == [str(env / "bin"), str(old / "bin")]
    monkeypatch.setenv("UNDINE_TOOLS", str(old))             # a prefix named on purpose comes first
    assert tools.find("ngspice") == theirs and tools.env()["PATH"].split(":")[0] == str(old / "bin")


def test_a_system_interpreter_is_not_an_environment_of_its_own(tmp_path, monkeypatch):
    usr, old = _envs(tmp_path, monkeypatch, conda=False)
    monkeypatch.setattr(sys, "base_prefix", str(usr))
    _tool(usr)
    theirs = _tool(old)
    assert tools.own_env() is None and tools.find("ngspice") == theirs      # ~/undine-tools replaces /usr's tools
    assert tools.tool_env() is None
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python"))        # a virtual environment is one
    assert tools.own_env() == usr and tools.find("ngspice") == str(usr / "bin" / "ngspice")
    assert tools.tool_env() == usr                                           # with tools of its own: a tool prefix
    (usr / "bin" / "ngspice").unlink()
    assert tools.tool_env() is None and tools.find("ngspice") == theirs     # without: ~/undine-tools still


def test_undine_tools_set_to_the_servers_own_environment_loads_no_libraries_of_it(tmp_path, monkeypatch):
    env, old = _envs(tmp_path, monkeypatch)
    ours = _tool(env)
    _tool(old)
    monkeypatch.setenv("UNDINE_TOOLS", str(env))             # as the MCP launcher starts a server (mcp_local.server_env)
    e = tools.env()
    assert tools.find("ngspice") == ours and e["PATH"].split(":")[0] == str(env / "bin")
    assert "LD_LIBRARY_PATH" not in e                        # a system KLayout must not load the environment's libraries
    monkeypatch.setenv("UNDINE_TOOLS", str(old))             # another conda prefix may need its own
    assert tools.env()["LD_LIBRARY_PATH"].startswith(str(old / "lib"))


# --- what a lock records of the tools: version numbers, origins, Undine's version and pixi.lock (tools_record) ---

import hashlib  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

BANNERS = {"klayout": "KLayout 0.28.17", "xschem": "XSCHEM V3.4.5",
           "ngspice": "******\n** ngspice-47 : Circuit level simulation program\n** Creation Date: today\n******",
           "magic": "8.3.678", "netgen": "Netgen 1.5.272 compiled on Mon Jan 1 00:00:00 UTC 2024\n"}


def _banner(d: Path, name: str, text: str) -> str:
    """A fake tool that prints text whatever it is asked (with the shell's echo: tests empty PATH)."""
    exe = Path(_tool(d, name))
    exe.write_text("#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in text.splitlines()))
    return str(exe)


def test_each_tools_banner_gives_its_number():
    numbers = {"klayout": "0.28.17", "xschem": "3.4.5", "ngspice": "47", "magic": "8.3.678", "netgen": "1.5.272"}
    for name, banner in {"klayout": "KLayout 0.28.17", "xschem": "XSCHEM V3.4.5",
                         "ngspice": "ngspice-47 : Circuit level simulation program", "magic": "8.3.678",
                         "netgen": "Netgen 1.5.272"}.items():
        assert tools.parse_version(name, banner) == numbers[name], name
    assert tools.parse_version("netgen", "Netgen 1.5.272 compiled on Mon Jan 1") == "1.5.272"
    assert tools.parse_version("ngspice", "ngspice-44.2 : Circuit level simulation program") == "44.2"
    assert tools.parse_version("magic", "Magic 8.3.678") is None             # the rule is the whole line
    assert tools.parse_version("klayout", "?") is None and tools.parse_version("verilator", "Verilator 5") is None


def test_the_records_of_fake_tools_and_their_banners_unchanged(tmp_path, monkeypatch):
    env, _ = _envs(tmp_path, monkeypatch)
    for name, text in BANNERS.items():
        _banner(env, name, text)
    assert [tools.version(n) for n in tools.TOOLS] == [   # the banner lines as before: provenance keys use them
        "KLayout 0.28.17", "XSCHEM V3.4.5", "ngspice-47 : Circuit level simulation program", "8.3.678",
        "Netgen 1.5.272"]
    rec = {n: tools.tool_record(n) for n in tools.TOOLS}
    assert rec == {"klayout": {"version": "0.28.17", "from": "env"}, "xschem": {"version": "3.4.5", "from": "env"},
                   "ngspice": {"version": "47", "from": "env"}, "magic": {"version": "8.3.678", "from": "env"},
                   "netgen": {"version": "1.5.272", "from": "env"}}


def test_an_unmatched_banner_is_kept_as_text_and_a_silent_tool_as_a_question_mark(tmp_path, monkeypatch):
    env, _ = _envs(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    _banner(env, "klayout", "KLayout nightly 2026")
    exe = Path(_tool(env, "magic"))
    exe.write_text("#!/bin/sh\nexit 1\n")                    # prints nothing: version() says '?'
    assert tools.tool_record("klayout") == {"version": None, "from": "env", "text": "KLayout nightly 2026"}
    assert tools.version("magic") == "?"
    assert tools.tool_record("magic") == {"version": None, "from": "env", "text": "?"}
    assert tools.tool_record("netgen") is None and tools.locate("netgen") is None and tools.find("netgen") is None


def test_locate_labels_where_each_tool_came_from(tmp_path, monkeypatch):
    env, old = _envs(tmp_path, monkeypatch)
    on_path = tmp_path / "usr"
    monkeypatch.setenv("PATH", str(on_path / "bin"))
    _tool(env, "ngspice"), _tool(old, "klayout"), _tool(on_path, "magic")
    assert tools.locate("ngspice") == (str(env / "bin" / "ngspice"), "env")
    assert tools.locate("klayout") == (str(old / "bin" / "klayout"), "undine-tools")
    assert tools.locate("magic") == (str(on_path / "bin" / "magic"), "PATH")
    pixi = tmp_path / "proj" / ".pixi" / "envs" / "default"
    _tool(pixi, "xschem")
    monkeypatch.setenv("UNDINE_TOOLS", str(pixi))             # as pixi.toml sets it
    assert tools.locate("xschem") == (str(pixi / "bin" / "xschem"), "pixi")
    other = tmp_path / "conda"
    _tool(other, "netgen")
    monkeypatch.setenv("UNDINE_TOOLS", str(other))
    assert tools.locate("netgen") == (str(other / "bin" / "netgen"), "UNDINE_TOOLS")
    monkeypatch.setenv("UNDINE_TOOLS", str(old))              # ~/undine-tools named on purpose is that variable
    assert tools.locate("klayout") == (str(old / "bin" / "klayout"), "UNDINE_TOOLS")
    for name in ("ngspice", "klayout", "magic"):              # find: the same path, a str as callers expect
        found = tools.find(name)
        assert isinstance(found, str) and found == tools.locate(name)[0]
    assert tools.find("xschem") is None and tools.locate("xschem") is None     # the pixi prefix is no longer named


def test_the_servers_own_pixi_environment_is_pixi(tmp_path, monkeypatch):
    _envs(tmp_path, monkeypatch)
    pixi = tmp_path / "proj" / ".pixi" / "envs" / "default"
    (pixi / "conda-meta").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(pixi))
    monkeypatch.setattr(sys, "base_prefix", str(pixi))
    _tool(pixi, "ngspice")
    assert tools.locate("ngspice") == (str(pixi / "bin" / "ngspice"), "pixi")
    link = tmp_path / "linked"
    link.symlink_to(pixi)                                     # reached through a link to the environment
    monkeypatch.setenv("UNDINE_TOOLS", str(link))
    assert tools.locate("ngspice") == (str(link / "bin" / "ngspice"), "pixi")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    repo = tmp_path / "undine-eda"
    (repo / "server").mkdir(parents=True)
    (repo / "server" / "pyproject.toml").write_text('[project]\nname = "undine-eda"\nversion = "9.8.7"\n')
    (repo / "pixi.lock").write_text("version: 6\n")
    monkeypatch.setattr(tools, "_repo", lambda: repo)
    import importlib.metadata

    def no_package(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(importlib.metadata, "version", no_package)     # a checkout run without installing
    tools.undine_version.cache_clear()
    yield repo
    tools.undine_version.cache_clear()


def test_undine_version_without_git_and_in_a_checkout(fake_repo, monkeypatch):
    assert tools.undine_version() == "9.8.7"                  # no checkout: the version alone
    _git(fake_repo, "init", "-q")
    _git(fake_repo, "add", ".")
    _git(fake_repo, "commit", "-q", "-m", "one")
    short = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=fake_repo, capture_output=True,
                           text=True).stdout.strip()
    assert tools._undine_version(fake_repo) == f"9.8.7 ({short})"
    (fake_repo / "untracked.txt").write_text("x")             # not a modified work tree
    assert tools._undine_version(fake_repo) == f"9.8.7 ({short})"
    (fake_repo / "pixi.lock").write_text("version: 7\n")
    assert tools._undine_version(fake_repo) == f"9.8.7 ({short}+dirty)"
    assert tools.undine_version() == "9.8.7"                  # cached: asked once per server
    monkeypatch.setenv("PATH", str(fake_repo / "no-bin"))     # git not installed: never raises
    assert tools._undine_version(fake_repo) == "9.8.7"


def test_pixi_lock_sha_only_inside_the_checkouts_pixi_environment(fake_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    assert tools.pixi_lock_sha() is None
    env = fake_repo / ".pixi" / "envs" / "default"
    env.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(env))
    assert tools.pixi_lock_sha() == "sha256:" + hashlib.sha256(b"version: 6\n").hexdigest()
    (fake_repo / "pixi.lock").unlink()
    assert tools.pixi_lock_sha() is None


def test_tools_record_has_undine_pixi_lock_and_each_tool(fake_repo, tmp_path, monkeypatch):
    env, _ = _envs(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    _banner(env, "ngspice", BANNERS["ngspice"])
    rec = tools.tools_record()
    assert list(rec) == ["undine", "pixi_lock", "klayout", "xschem", "ngspice", "magic", "netgen"]
    assert rec["undine"] == "9.8.7" and rec["pixi_lock"] is None
    assert rec["ngspice"] == {"version": "47", "from": "env"} and rec["klayout"] is None


def test_tools_names_the_tools_the_lock_has_always_recorded():
    from undine.store import deps
    assert tools.TOOLS == deps.TOOLS == ("klayout", "xschem", "ngspice", "magic", "netgen")
