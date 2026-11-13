"""Verification setup errors cannot strand queued jobs and prevent closing a proposal."""
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import klayout.db as kdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undine.checks import magicflow, verify  # noqa: E402
from undine.pdks import pdkprofile
from undine.simulation import sim


@pytest.fixture(params=["klayout_drc", "klayout_lvs", "magic_drc", "netgen_lvs"])
def runner(request, tmp_path, monkeypatch):
    pdk = SimpleNamespace(root=tmp_path / "pdk", name="test", klayout_tech=tmp_path / "tech", decks_dir=tmp_path / "tech",
                          profile=dataclasses.replace(pdkprofile.GENERIC, netgen_setup="setup.tcl"))
    setup = pdk.root / "libs.tech" / "netgen" / "setup.tcl"
    setup.parent.mkdir(parents=True)
    setup.write_text("# test setup")
    project = tmp_path / "project"
    project.mkdir()
    layout = kdb.Layout()
    layout.create_cell("top")
    jobs = sim.Jobs()
    monkeypatch.setattr(verify, "decks", lambda _: {"drc": ["check.drc"], "lvs": ["check.lvs"], "extra": []})
    monkeypatch.setattr(verify, "_klayout", lambda: "klayout")
    monkeypatch.setattr(magicflow, "_magic", lambda _: ("magic", tmp_path / "magic.rc"))
    monkeypatch.setattr(magicflow, "_env", lambda _: {})
    monkeypatch.setattr(magicflow.tools, "find", lambda _: "netgen")
    args = (jobs, project, pdk, layout, "top", "l")
    launches = {
        "klayout_drc": lambda: verify.start_drc(*args),
        "klayout_lvs": lambda: verify.start_lvs(*args, ".subckt top\n.ends top\n"),
        "magic_drc": lambda: magicflow.start_drc(*args),
        "netgen_lvs": lambda: magicflow.start_lvs(*args, ".subckt top\n.ends top\n"),
    }
    case = SimpleNamespace(backend=request.param, project=project, jobs=jobs, launch=launches[request.param])
    try:
        yield case
    finally:
        for path in (project / "runs").glob("*"):
            verify.LIVE.pop(path.name, None)


def test_missing_executable_does_not_allocate_a_job(runner, monkeypatch):
    def missing(*args):
        raise RuntimeError("verification tool missing")

    monkeypatch.setattr(verify, "_klayout", missing)
    monkeypatch.setattr(magicflow, "_magic", missing)
    with pytest.raises(RuntimeError, match="verification tool missing"):
        runner.launch()
    assert not (runner.project / "runs").exists()
    assert not runner.jobs.live


@pytest.mark.parametrize("failure", ["export", "thread"])
def test_failed_setup_retains_artifacts_and_is_no_longer_active(runner, monkeypatch, failure):
    def export(layout, cell, path, **kwargs):
        path.write_bytes(b"partial or complete exported design")
        if failure == "export":
            raise OSError("input export failed")

    def start():
        raise RuntimeError("worker launch failed")

    monkeypatch.setattr(verify, "_export", export)
    monkeypatch.setattr(verify.threading, "Thread", lambda **kwargs: SimpleNamespace(start=start))
    with pytest.raises((OSError, RuntimeError), match="export failed|launch failed"):
        runner.launch()
    paths = list((runner.project / "runs").iterdir())
    assert len(paths) == 1
    path = paths[0]
    status = sim.Job(path).status()
    assert status["state"] == "failed" and status["finished"] >= status["created"]
    assert status["error"] == ("input export failed" if failure == "export" else "worker launch failed")
    assert (path / "input.gds").read_bytes() == b"partial or complete exported design"
    assert status["id"] not in verify.LIVE and status["id"] not in runner.jobs.live
