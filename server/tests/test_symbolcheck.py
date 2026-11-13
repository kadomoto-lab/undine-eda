"""Schematic-symbol checks use the same terminal order as netlisting and retain evidence."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undine.store import provenance  # noqa: E402
from undine.schematics import schematic, symbolcheck, xschem
from undine.simulation import sim
from undine.store.project import ProjectStore  # noqa: E402


@pytest.fixture
def design(tmp_path):
    prj = ProjectStore(tmp_path, {}).create("project")
    lib = prj.create_library("design")
    devices = prj.create_library("devices")
    for kind in ("ipin", "opin", "iopin"):
        sym = xschem.XFile(global_props={"type": kind, "template": "lab=DEFAULT"},
                           boxes=[(5, -2.5, -2.5, 2.5, 2.5, {"name": "p", "dir": "inout"})])
        (devices.path / f"{kind}.sym").write_text(xschem.dump(sym))
    ports = [("IN", "ipin"), ("OUT", "opin"), ("VSS", "iopin")]
    sch = xschem.XFile(comps=[xschem.XComp(f"devices/{kind}.sym", i * 30, 0, 0, 0,
                                         {"name": f"P{i + 1}", "lab": name})
                              for i, (name, kind) in enumerate(ports)])
    (lib.path / "amp.sch").write_text(xschem.dump(sch))
    (lib.path / "amp.sym").write_text(xschem.dump(xschem.make_symbol(ports)))
    return SimpleNamespace(prj=prj, lib=lib, devices=devices, sch=sch,
                           capture=lambda: symbolcheck.capture(prj, "design", "amp"))


def _edit_symbol(design, fn):
    path = design.lib.path / "amp.sym"
    sym = xschem.parse(path.read_text())
    fn(sym)
    path.write_text(xschem.dump(sym))


def _wait(job):
    deadline = time.monotonic() + 5
    while job.status()["state"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.status()["state"] == "done", job.status()
    return job.status()


def test_matching_interface_follows_actual_netlist_order(design):
    snapshot = design.capture()
    result = symbolcheck.compare(snapshot)
    assert result["match"] and result["issues"] == []
    resolver = xschem.SymbolResolver(design.prj.symbol_paths())
    sch = design.lib.path / "amp.sch"
    assert [p["name"] for p in result["schematic_ports"]] == schematic.spice(sch, resolver).ports
    assert [(p["name"], p["direction"]) for p in result["symbol_pins"]] == [
        (p.name, p.dir) for p in schematic.sym_pins(xschem.parse((design.lib.path / "amp.sym").read_text()))]
    assert [p["direction"] for p in result["schematic_ports"]] == ["in", "out", "inout"]


def test_pin_numbers_do_not_override_file_order(design):
    def reverse(sym):
        for i, box in enumerate(sym.boxes):
            box[-1]["pinnumber"] = str(i + 1)
        sym.boxes.reverse()
    _edit_symbol(design, reverse)
    result = symbolcheck.compare(design.capture())
    assert [i["code"] for i in result["issues"]] == ["pin_order_mismatch"]
    assert result["issues"][0]["expected"] == "IN, OUT, VSS"
    assert result["issues"][0]["actual"] == "VSS, OUT, IN"


def test_missing_extra_and_direction_mismatches_are_reported(design):
    def change(sym):
        sym.boxes[0][-1]["dir"] = "out"
        sym.boxes[1][-1]["name"] = "OTHER"
    _edit_symbol(design, change)
    result = symbolcheck.compare(design.capture())
    assert not result["match"]
    assert [i["code"] for i in result["issues"]] == [
        "direction_mismatch", "missing_symbol_pin", "extra_symbol_pin"]
    assert result["issues"][0]["expected"] == "in" and result["issues"][0]["actual"] == "out"


def test_duplicates_empty_names_and_invalid_directions_cannot_pass(design):
    design.sch.comps.append(xschem.XComp("devices/ipin.sym", 90, 0, 0, 0, {"name": "P4", "lab": "IN"}))
    (design.lib.path / "amp.sch").write_text(xschem.dump(design.sch))
    def change(sym):
        sym.boxes[0][-1]["dir"] = "invalid"
        sym.boxes[1][-1]["name"] = ""
        sym.boxes.append(sym.boxes[2])
    _edit_symbol(design, change)
    codes = {i["code"] for i in symbolcheck.compare(design.capture())["issues"]}
    assert {"duplicate_schematic_pin", "duplicate_symbol_pin", "unnamed_symbol_pin", "invalid_direction"} <= codes


def test_embedded_port_and_template_label_follow_connectivity_rules(design):
    embedded = xschem.XFile(global_props={"type": "ipin", "template": "lab=EMBEDDED"})
    design.sch.comps = [xschem.XComp("unavailable.sym", 0, 0, 0, 0, {"name": "P1"}, embedded)]
    (design.lib.path / "amp.sch").write_text(xschem.dump(design.sch))
    (design.lib.path / "amp.sym").write_text(xschem.dump(xschem.make_symbol([("EMBEDDED", "ipin")])))
    snapshot = design.capture()
    assert symbolcheck.compare(snapshot)["match"]
    assert "P1" in snapshot["port_definitions"]
    resolver = xschem.SymbolResolver(design.prj.symbol_paths())
    assert schematic.extract(design.lib.path / "amp.sch", resolver).ports == [("EMBEDDED", "ipin")]


def test_unresolved_symbols_are_not_silently_treated_as_non_ports(design):
    design.sch.comps[0].sym = "unavailable-port-template.sym"
    (design.lib.path / "amp.sch").write_text(xschem.dump(design.sch))
    result = symbolcheck.compare(design.capture())
    assert not result["match"]
    assert result["issues"][0]["code"] == "unresolved_symbol"
    assert result["issues"][0]["name"] == "P1"


@pytest.mark.parametrize("missing", ["schematic", "symbol"])
def test_missing_view_is_a_persisted_check_failure(design, missing):
    (design.lib.path / ("amp.sch" if missing == "schematic" else "amp.sym")).unlink()
    jobs = sim.Jobs()
    job = symbolcheck.start(jobs, design.prj, "design", "amp")
    status = _wait(job)
    assert status["summary"]["match"] is False and status["summary"]["total"] == 1
    assert symbolcheck.result(job)["issues"][0]["code"] == f"missing_{missing}"
    assert sim.Jobs().get(design.prj.path, job.id).status() == status  # reload survives process state


def test_freshness_tracks_interface_but_not_drawing_changes(design):
    status = {"inputs": design.capture()["inputs"]}
    srv = SimpleNamespace(project=lambda _: design.prj)
    fresh = lambda: provenance.freshness(srv, "project", status)
    assert fresh() == {"fresh": True, "changed": []}
    design.sch.comps[0].x += 100
    design.sch.texts.append(xschem.XText("comment", 0, 0, 0, 0, 0.3, 0.3, {}))
    (design.lib.path / "amp.sch").write_text(xschem.dump(design.sch))
    _edit_symbol(design, lambda sym: sym.lines.append((4, 0, 0, 20, 0, {})))
    assert fresh()["fresh"]
    _edit_symbol(design, lambda sym: sym.boxes[0][-1].update(dir="inout"))
    assert fresh() == {"fresh": False, "changed": ["symbol interface of design/amp changed"]}
    _edit_symbol(design, lambda sym: sym.boxes[0][-1].update(dir="in"))
    assert fresh()["fresh"]
    # Changing the resolved port symbol type changes the schematic interface too.
    path = design.devices.path / "ipin.sym"
    sym = xschem.parse(path.read_text())
    sym.global_props["type"] = "opin"
    sym.k_raw = ""
    path.write_text(xschem.dump(sym))
    assert fresh() == {"fresh": False, "changed": ["schematic interface of design/amp changed"]}


def test_missing_view_freshness_changes_when_created(design):
    (design.lib.path / "amp.sym").unlink()
    status = {"inputs": design.capture()["inputs"]}
    srv = SimpleNamespace(project=lambda _: design.prj)
    assert provenance.freshness(srv, "project", status)["fresh"]
    (design.lib.path / "amp.sym").write_text(xschem.dump(xschem.make_symbol([])))
    assert not provenance.freshness(srv, "project", status)["fresh"]


def test_worker_uses_captured_inputs_and_keeps_evidence(design, monkeypatch):
    workers = []
    monkeypatch.setattr(symbolcheck.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: workers.append(kw)))
    jobs = sim.Jobs()
    job = symbolcheck.start(jobs, design.prj, "design", "amp")
    before = (design.lib.path / "amp.sym").read_text()
    _edit_symbol(design, lambda sym: sym.boxes[0][-1].update(name="CHANGED"))
    workers[0]["target"](*workers[0]["args"])
    status = job.status()
    assert status["state"] == "done" and status["summary"]["match"]
    assert (job.path / "input.sym").read_text() == before
    snapshot = json.loads((job.path / "inputs.json").read_text())
    assert snapshot["symbol"]["pins"][0]["name"] == "IN"
    assert snapshot["port_definitions"]
    assert {"input.sch", "input.sym", "inputs.json", "result.json", "log.txt"} <= set(status["files"])
    assert not jobs.live
    assert not provenance.freshness(SimpleNamespace(project=lambda _: design.prj), "project", status)["fresh"]


def test_launch_failure_does_not_leave_a_queued_job(design, monkeypatch):
    def fail():
        raise RuntimeError("worker launch failed")
    monkeypatch.setattr(symbolcheck.threading, "Thread", lambda **kw: SimpleNamespace(start=fail))
    jobs = sim.Jobs()
    with pytest.raises(RuntimeError, match="worker launch failed"):
        symbolcheck.start(jobs, design.prj, "design", "amp")
    statuses = jobs.list(design.prj.path)
    assert len(statuses) == 1 and statuses[0]["state"] == "failed"
    assert not jobs.live


def test_invalid_cell_name_cannot_escape_library(design):
    with pytest.raises(ValueError, match="invalid name"):
        symbolcheck.start(sim.Jobs(), design.prj, "design", "../outside")
