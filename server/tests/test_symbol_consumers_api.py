"""A symbol pin move and its consumer wire updates form one reversible operation."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undine.schematics import schematic, symbolsync, xschem  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402
from undine.store.changeset import ChangesetError  # noqa: E402


def _consumer(symbol, instance, model, x=0, y=0, rot=0, flip=0, embedded=False):
    comp = xschem.XComp(symbol, x, y, rot, flip, {"name": instance}, model if embedded else None)
    wires = []
    for pin in schematic.sym_pins(model):
        px, py = xschem._rot(rot, flip, pin.x, pin.y)
        ox, oy = xschem._rot(rot, flip, pin.x + (-60 if pin.x < 0 else 60), pin.y)
        wires.append((x + px, y + py, x + ox, y + oy, {"lab": f"{instance}_{pin.name}"}))
    return xschem.XFile(comps=[comp], wires=wires)


@pytest.fixture
def consumers(tmp_path):
    srv = Server(tmp_path, {}, [tmp_path], llm_policy="none")
    prj = srv.store.create("p")
    design = prj.create_library("design")
    other = prj.create_library("other")
    model = xschem.make_symbol([("A", "ipin"), ("Y", "opin")])
    (design.path / "block.sym").write_text(xschem.dump(model))
    (design.path / "top.sch").write_text(xschem.dump(_consumer("block.sym", "X1", model)))
    (other.path / "top.sch").write_text(xschem.dump(_consumer("design/block.sym", "X2", model, 200, 100, 1, 1)))
    # The same basename is deliberately a different resolved symbol.
    (other.path / "block.sym").write_text(xschem.dump(model))
    (other.path / "shadow.sch").write_text(xschem.dump(_consumer("block.sym", "SHADOW", model)))
    (design.path / "embedded.sch").write_text(xschem.dump(_consumer("block.sym", "EMBEDDED", model, embedded=True)))
    client = TestClient(create_app(srv, None))
    return SimpleNamespace(root=tmp_path, srv=srv, prj=prj, design=design, other=other, model=model, client=client)


def _post(case, project, lib, cell, view, body):
    response = case.client.post(f"/api/p/{project}/l/{lib}/c/{cell}/{view}/edit", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _pin(case, project="p", name="A"):
    response = case.client.get(f"/api/p/{project}/l/design/c/block/symbol")
    assert response.status_code == 200, response.text
    return next(p for p in response.json()["objects"] if p["kind"] == "pin" and p["name"] == name)


def _move(case, project="p", dx=-20, dy=20):
    pin = _pin(case, project)
    return _post(case, project, "design", "block", "symbol",
                 {"op": "symbol_update", "id": pin["id"], "x": pin["x"] + dx, "y": pin["y"] + dy})


def _files(case, project="p"):
    """The views' content. A schematic is written in Undine's canonical order (xschem.canonical): wires and graphics
    sorted, so restoring a view restores its content, in that order."""
    prj = case.srv.project(project)
    return {(lib.name, path.name): (symbolsync.canonical(path.read_text()).encode() if path.suffix == ".sch" else path.read_bytes())
            for lib in prj.libraries().values() for path in lib.path.iterdir() if path.suffix in (".sch", ".sym")}


def _logs(case, project="p"):
    history = case.srv.project(project).path / "history"
    return {p.name: p.read_bytes() for p in history.glob("*.jsonl")}


def _nets(case, project="p"):
    prj = case.srv.project(project)
    resolver = xschem.SymbolResolver(prj.symbol_paths())
    return {(lib, cell): {d.name: d.nets for d in schematic.extract(prj.library(lib).path / f"{cell}.sch", resolver).devices}
            for lib, cell in (("design", "top"), ("other", "top"), ("other", "shadow"), ("design", "embedded"))}


def test_pin_move_updates_cross_library_rotated_consumers_only(consumers):
    before, nets = _files(consumers), _nets(consumers)
    _move(consumers)
    after = _files(consumers)
    assert _nets(consumers) == nets
    assert after["design", "block.sym"] != before["design", "block.sym"]
    assert after["design", "top.sch"] != before["design", "top.sch"]
    assert after["other", "top.sch"] != before["other", "top.sch"]
    for identity in (("other", "block.sym"), ("other", "shadow.sch"), ("design", "embedded.sch")):
        assert after[identity] == before[identity]


@pytest.mark.parametrize("origin", [("design", "block", "symbol"), ("other", "top", "schematic")])
def test_undo_redo_from_any_member_restores_the_complete_move(consumers, origin):
    before, nets = _files(consumers), _nets(consumers)
    _move(consumers)
    after = _files(consumers)
    _post(consumers, "p", *origin, {"op": "undo"})
    assert _files(consumers) == before and _nets(consumers) == nets
    _post(consumers, "p", *origin, {"op": "redo"})
    assert _files(consumers) == after and _nets(consumers) == nets


def _split_input_wire(case):
    path = case.design.path / "top.sch"
    model = xschem.parse(path.read_text())
    model.wires = [(-60, 0, -80, 0, {"lab": "X1_A"}),
                   (-80, 0, -100, 0, {}), (-100, 0, -120, 0, {}), model.wires[1]]
    path.write_text(xschem.dump(model))


def test_multisegment_consumer_undo_redo_restores_wire_order_and_group_hash(consumers):
    _split_input_wire(consumers)
    before, nets = _files(consumers), _nets(consumers)
    _move(consumers)
    after = _files(consumers)
    assert after != before and _nets(consumers) == nets
    _post(consumers, "p", "other", "top", "schematic", {"op": "undo"})
    assert _files(consumers) == before and _nets(consumers) == nets
    _post(consumers, "p", "design", "block", "symbol", {"op": "redo"})
    assert _files(consumers) == after and _nets(consumers) == nets


def test_multisegment_consumer_apply_revert_restores_wire_order_and_group_hash(consumers):
    _split_input_wire(consumers)
    changesets = Changesets(consumers.srv)
    before, nets = _files(consumers), _nets(consumers)
    meta = changesets.create("p", "Move a pin with segmented wiring")
    _move(consumers, meta["preview"])
    proposed = _files(consumers, meta["preview"])
    changesets.apply("p", meta["id"])
    assert _files(consumers) == proposed and _nets(consumers) == nets
    changesets.revert("p", meta["id"])
    assert _files(consumers) == before and _nets(consumers) == nets
    # a revert is a merge too: its merge record is an undo barrier (docs/design/changeset-branches.md), so undo and
    # redo from any member find nothing to take back and the reverted wiring stays whole
    for origin, op in ((("other", "top", "schematic"), "undo"), (("design", "block", "symbol"), "redo")):
        assert _post(consumers, "p", *origin, {"op": op})["result"] is False
        assert _files(consumers) == before and _nets(consumers) == nets


def test_later_consumer_edit_must_be_undone_before_group_undo(consumers):
    before = _files(consumers)
    _move(consumers)
    _post(consumers, "p", "other", "top", "schematic", {"op": "add_text", "text": "later edit", "x": 400, "y": 400})
    later, logs = _files(consumers), _logs(consumers)
    response = consumers.client.post("/api/p/p/l/design/c/block/symbol/edit", json={"op": "undo"})
    assert response.status_code in (400, 409), response.text
    assert "undo" in response.json()["detail"].lower() or "changed" in response.json()["detail"].lower()
    assert _files(consumers) == later and _logs(consumers) == logs
    _post(consumers, "p", "other", "top", "schematic", {"op": "undo"})
    _post(consumers, "p", "design", "block", "symbol", {"op": "undo"})
    assert _files(consumers) == before


def test_a_pin_move_that_would_merge_nets_changes_no_files_or_history(consumers):
    before, logs = _files(consumers), _logs(consumers)
    pin, other = _pin(consumers), _pin(consumers, name="Y")
    response = consumers.client.post("/api/p/p/l/design/c/block/symbol/edit", json={
        "op": "symbol_update", "id": pin["id"], "x": other["x"], "y": other["y"]})
    assert response.status_code in (400, 409), response.text
    assert _files(consumers) == before and _logs(consumers) == logs


def test_proposal_pin_move_is_isolated_and_apply_revert_preserve_connectivity(consumers):
    changesets = Changesets(consumers.srv)
    before, nets = _files(consumers), _nets(consumers)
    meta = changesets.create("p", "Move block input")
    preview = meta["preview"]
    _move(consumers, preview)
    proposed = _files(consumers, preview)
    assert _files(consumers) == before and _nets(consumers, preview) == nets
    timeline = changesets.timeline("p", meta["id"])
    assert len(timeline["ops"]) == 1, timeline
    assert len(timeline["ops"][0]["cells"]) == 3
    changesets.apply("p", meta["id"])
    assert _files(consumers) == proposed and _nets(consumers) == nets
    changesets.revert("p", meta["id"])
    assert _files(consumers) == before and _nets(consumers) == nets


def test_checkpoint_restore_cannot_split_symbol_from_consumer_updates(consumers):
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Move and restore")
    preview = meta["preview"]
    before, nets = _files(consumers, preview), _nets(consumers, preview)
    _move(consumers, preview)
    timeline = changesets.timeline("p", meta["id"])
    assert len(timeline["ops"]) == 1
    assert timeline["checkpoints"] == [0]
    changesets.restore("p", meta["id"], 0)
    assert _files(consumers, preview) == before and _nets(consumers, preview) == nets


def test_cross_library_apply_conflict_leaves_earlier_library_and_logs_untouched(consumers):
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Group with later conflict")
    _move(consumers, meta["preview"])
    _post(consumers, "p", "other", "top", "schematic", {"op": "add_text", "text": "concurrent edit", "x": 400, "y": 400})
    before, logs = _files(consumers), _logs(consumers)
    with pytest.raises(ChangesetError, match="conflict"):
        changesets.apply("p", meta["id"])
    assert _files(consumers) == before and _logs(consumers) == logs
    assert changesets.get("p", meta["id"])["status"] == "open"


def test_cross_library_revert_conflict_is_atomic(consumers):
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Applied group")
    _move(consumers, meta["preview"])
    changesets.apply("p", meta["id"])
    _post(consumers, "p", "other", "top", "schematic", {"op": "add_text", "text": "later edit", "x": 400, "y": 400})
    before, logs = _files(consumers), _logs(consumers)
    with pytest.raises(ChangesetError, match="conflict"):
        changesets.revert("p", meta["id"])
    assert _files(consumers) == before and _logs(consumers) == logs
    assert changesets.get("p", meta["id"])["status"] == "applied"


def test_applied_group_and_revert_are_undo_barriers(consumers):
    # Apply and revert are merges now (docs/design/changeset-branches.md): the project's log gets the changeset's
    # records, then a merge record after which undo starts again. An applied symbol transaction is taken back with
    # Revert, not with a grouped undo from one of its members, and a revert is not undone either; the group's records
    # stay in the log, tagged with the changeset.
    changesets = Changesets(consumers.srv)
    initial = _files(consumers)
    meta = changesets.create("p", "Revert remains one operation")
    _move(consumers, meta["preview"])
    changesets.apply("p", meta["id"])
    applied, logs = _files(consumers), _logs(consumers)
    for origin in (("other", "top", "schematic"), ("design", "block", "symbol")):
        assert _post(consumers, "p", *origin, {"op": "undo"})["result"] is False
    assert _files(consumers) == applied and _logs(consumers) == logs
    for lib in ("design", "other"):
        recs = consumers.srv.oplog("p", lib).read(limit=100)
        assert {r["changeset"] for r in recs if r.get("symbol_group")} == {meta["id"]}
        assert recs[-1]["op"] == "merge" and recs[-1]["changeset"] == meta["id"]
    changesets.revert("p", meta["id"])
    assert _files(consumers) == initial
    assert _post(consumers, "p", "other", "top", "schematic", {"op": "undo"})["result"] is False
    assert _files(consumers) == initial
    assert consumers.srv.oplog("p", "other").read(limit=1)[-1]["args"] == {"changeset": meta["id"], "action": "revert"}


def _fail_writing(monkeypatch, target):
    """An apply writes every merged file and log at once (branches.write_plan): the rename of `target` into place
    fails, after the files before it in the plan were renamed in."""
    from undine.vcs import branches
    real = branches._replace

    def replace(src, dst):
        if Path(dst) == target:
            raise OSError("consumer write failed")
        return real(src, dst)
    monkeypatch.setattr(branches, "_replace", replace)


def test_consumer_edit_after_undo_prevents_partial_group_redo(consumers):
    _move(consumers)
    _post(consumers, "p", "design", "block", "symbol", {"op": "undo"})
    _post(consumers, "p", "other", "top", "schematic", {"op": "add_text", "text": "branch edit", "x": 400, "y": 400})
    before, logs = _files(consumers), _logs(consumers)
    response = consumers.client.post("/api/p/p/l/design/c/block/symbol/edit", json={"op": "redo"})
    assert response.status_code in (400, 409), response.text
    assert _files(consumers) == before and _logs(consumers) == logs


def test_pin_move_refuses_a_consumer_that_needs_rewiring_in_readonly_library(consumers):
    consumers.prj.meta["libraries"]["locked"] = {"git": "https://example.invalid/lib.git", "rev": "main"}
    consumers.prj.save()
    locked = consumers.prj.library("locked")
    locked.path.mkdir(parents=True)
    (locked.path / "locked.sch").write_text(xschem.dump(_consumer("design/block.sym", "LOCKED", consumers.model)))
    before, logs = _files(consumers), _logs(consumers)
    pin = _pin(consumers)
    response = consumers.client.post("/api/p/p/l/design/c/block/symbol/edit", json={
        "op": "symbol_update", "id": pin["id"], "x": pin["x"] - 20, "y": pin["y"] + 20})
    assert response.status_code in (400, 403, 409), response.text
    assert "read-only" in response.json()["detail"].lower() or "readonly" in response.json()["detail"].lower()
    assert _files(consumers) == before and _logs(consumers) == logs


def test_group_undo_survives_server_restart(consumers):
    before = _files(consumers)
    _move(consumers)
    consumers.srv = Server(consumers.root, {}, [consumers.root], llm_policy="none")
    consumers.client = TestClient(create_app(consumers.srv, None))
    _post(consumers, "p", "other", "top", "schematic", {"op": "undo"})
    assert _files(consumers) == before


def test_later_library_write_failure_rolls_back_files_and_logs(consumers, monkeypatch):
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Replay failure")
    _move(consumers, meta["preview"])
    before, logs = _files(consumers), _logs(consumers)
    _fail_writing(monkeypatch, consumers.other.path / "top.sch")      # the merge writes files, it replays nothing
    with pytest.raises(ChangesetError, match="consumer write failed"):
        changesets.apply("p", meta["id"])
    assert _files(consumers) == before and _logs(consumers) == logs
    assert changesets.get("p", meta["id"])["status"] == "open"


def test_mixed_proposal_failure_preserves_existing_dirty_layout_memory(consumers, monkeypatch):
    import klayout.db as kdb
    from undine.store import provenance
    db = consumers.srv.db("p", "design")
    db.new_cell("layout")
    db.save()
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Symbol plus layout")
    workdb = consumers.srv.db(meta["preview"], "design")
    with workdb.recording("create", {}, "user", None, meta["id"]):
        workdb._commit("layout", [("ins", workdb.layout.layer(1, 0), kdb.Box(0, 0, 100, 100))])
    _move(consumers, meta["preview"])
    # An independent, unsaved real-design edit must survive a later write failure.
    db._commit("layout", [("ins", db.layout.layer(1, 0), kdb.Box(200, 0, 300, 100))])
    expected, logs = provenance.layout_sha(db.layout, "layout"), _logs(consumers)
    layout_files = {p.name: p.read_bytes() for p in consumers.design.path.glob("*.oas")}
    # the apply saves the unsaved layout before it merges (recorded); the failed write puts the save back too
    _fail_writing(monkeypatch, consumers.other.path / "top.sch")
    with pytest.raises(ChangesetError, match="consumer write failed"):
        changesets.apply("p", meta["id"])
    assert db.dirty and provenance.layout_sha(db.layout, "layout") == expected
    assert _logs(consumers) == logs
    assert {p.name: p.read_bytes() for p in consumers.design.path.glob("*.oas")} == layout_files


@pytest.mark.parametrize("operation", ["undo", "redo"])
def test_new_consumer_after_group_blocks_history_change_atomically(consumers, operation):
    _move(consumers)
    if operation == "redo":
        _post(consumers, "p", "design", "block", "symbol", {"op": "undo"})
    model = xschem.parse((consumers.design.path / "block.sym").read_text())
    (consumers.other.path / "late.sch").write_text(xschem.dump(_consumer("design/block.sym", "LATE", model)))
    before, logs = _files(consumers), _logs(consumers)
    response = consumers.client.post("/api/p/p/l/design/c/block/symbol/edit", json={"op": operation})
    assert response.status_code in (400, 409), response.text
    assert "late" in response.json()["detail"].lower()
    assert _files(consumers) == before and _logs(consumers) == logs


@pytest.mark.parametrize("operation", ["apply", "revert"])
def test_new_consumer_after_proposal_blocks_closure_atomically(consumers, operation):
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Group with later new consumer")
    _move(consumers, meta["preview"])
    if operation == "revert":
        changesets.apply("p", meta["id"])
    model = xschem.parse((consumers.design.path / "block.sym").read_text())
    (consumers.other.path / "late.sch").write_text(xschem.dump(_consumer("design/block.sym", "LATE", model)))
    before, logs = _files(consumers), _logs(consumers)
    with pytest.raises(ChangesetError, match="late"):
        getattr(changesets, operation)("p", meta["id"])
    assert _files(consumers) == before and _logs(consumers) == logs
    # a merge of each file alone would leave LATE unconnected without a word: the merge conflicts on it (HTTP 409)
    r = consumers.client.post(f"/api/p/p/changesets/{meta['id']}/{operation}")
    assert r.status_code == 409 and r.json()["code"] == "merge_conflict", r.text
    late = [f for f in r.json()["files"] if f["path"] == "libs/other/late.sch"]
    assert late and late[0]["kind"] == "schematic" and "other/late" in late[0]["conflicts"][0]["what"]
    assert {"id", "what", "base", "ours", "theirs"} <= set(late[0]["conflicts"][0])
    assert _files(consumers) == before and _logs(consumers) == logs
    assert changesets.get("p", meta["id"])["status"] == ("open" if operation == "apply" else "applied")


@pytest.mark.parametrize("entrypoint", ["api", "agent"])
def test_new_schematic_creation_waits_until_symbol_planning_commits(consumers, monkeypatch, entrypoint):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from undine.agents import agent_tools
    from undine.schematics import symbolsync
    meta = Changesets(consumers.srv).create("p", "Serialized planning")
    preview = meta["preview"]
    planned, release, waiting = threading.Event(), threading.Event(), threading.Event()
    owner = []
    original_lock = consumers.srv.changeset_lock
    original_plan = symbolsync._plan_consumers

    class ObservedLock:
        def __enter__(self):
            if owner and threading.get_ident() != owner[0]:
                waiting.set()
            original_lock.acquire()
            return self

        def __exit__(self, *_args):
            original_lock.release()

    def pause_plan(*args, **kwargs):
        owner.append(threading.get_ident())
        planned.set()
        assert release.wait(5), "symbol planning was not released"
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(consumers.srv, "changeset_lock", ObservedLock())
    monkeypatch.setattr(symbolsync, "_plan_consumers", pause_plan)
    path = consumers.srv.project(preview).library("design").path / "fresh.sch"

    def create():
        if entrypoint == "api":
            response = consumers.client.post(f"/api/p/{preview}/l/design/cells", json={"name": "fresh", "views": ["schematic"]})
            assert response.status_code == 200, response.text
            return response.json()
        context = agent_tools.ToolCtx(consumers.srv, "p", changeset=meta["id"])
        return agent_tools.create_cell(context, {"lib": "design", "cell": "fresh", "views": ["schematic"], "reason": "new cell"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        moved = pool.submit(_move, consumers, preview)
        assert planned.wait(5), "symbol planning did not start"
        created = pool.submit(create)
        try:
            assert waiting.wait(5), "creation did not reach the shared transaction lock"
            assert not created.done() and not path.exists()
        finally:
            release.set()
        moved.result(timeout=5)
        assert created.result(timeout=5)["created"] == ["schematic"]
    assert path.is_file()


@pytest.mark.parametrize("stub", [False, True])
def test_a_schematic_the_changeset_drew_after_its_pin_move_is_not_a_consumer_to_follow(consumers, stub):
    # sar11's cs-9886e4: sar_bootsw gained a pin (a symbol transaction), then the changeset drew a new testbench with
    # the new symbol. That testbench is already wired to the new pins: adopting must not plan it as a consumer of the
    # old symbol (which refused the apply: "Cannot preserve connections ... choose another position").
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Move a pin, then draw a new bench")
    preview = meta["preview"]
    old = _pin(consumers, preview)
    _move(consumers, preview)
    lib = consumers.srv.project(preview).library("design").path
    model = xschem.parse((lib / "block.sym").read_text())
    bench = _consumer("block.sym", "XNEW", model, 400, 0)
    if stub:                                         # another net's wire ends where the old pin was: following moves it
        bench.wires.append((400 + old["x"] - 40, old["y"], 400 + old["x"], old["y"], {"lab": "other_net"}))
    (lib / "bench.sch").write_text(xschem.dump(bench))
    drawn = symbolsync.canonical((lib / "bench.sch").read_text()).encode()
    changesets.apply("p", meta["id"])
    assert changesets.get("p", meta["id"])["status"] == "applied"
    assert _files(consumers)["design", "bench.sch"] == drawn       # taken as the changeset drew it


def test_a_consumer_placed_in_the_project_since_still_has_to_follow(consumers):
    # the other side of the rule above: an instance of the old symbol placed in the project after the changeset was
    # opened still refuses the apply
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Move a pin while the project places the block")
    _move(consumers, meta["preview"])
    (consumers.design.path / "late.sch").write_text(xschem.dump(_consumer("block.sym", "XLATE", consumers.model, 600, 0)))
    with pytest.raises(ChangesetError, match="conflict"):
        changesets.apply("p", meta["id"])
    assert changesets.get("p", meta["id"])["status"] == "open"


def test_an_instance_an_undo_brings_back_with_its_old_wiring_is_still_checked(consumers):
    # review of the rule above: X2 is deleted from other/top, the pin moves (other/top is then no member), and an undo
    # brings X2 back with its wire where the old pin was. The tip's other/top is taken whole, but X2 was there at the
    # base, so it is checked: the apply is refused instead of leaving X2's input unconnected.
    changesets = Changesets(consumers.srv)
    meta = changesets.create("p", "Delete, move the pin, undo the delete")
    preview = meta["preview"]
    _post(consumers, preview, "other", "top", "schematic", {"op": "delete", "items": [{"k": "i", "id": "X2"}]})
    _move(consumers, preview)
    _post(consumers, preview, "other", "top", "schematic", {"op": "undo"})
    before = _files(consumers)
    with pytest.raises(ChangesetError, match="conflict"):
        changesets.apply("p", meta["id"])
    assert _files(consumers) == before
    assert changesets.get("p", meta["id"])["status"] == "open"
