"""Job ids (sim.new_run): the time to the second and 4 hex digits, in the order they were made, never given twice,
also with thousands of jobs in one second, from threads at once, or when another server made a run directory in that
second."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.checks import verify  # noqa: E402
from undine.simulation import sim  # noqa: E402


def test_job_ids_never_repeat_in_one_second(tmp_path, monkeypatch):
    """Thousands of ids in one second (time stands still), from threads at once, and a directory another server made
    in that second: every id is new, its directory made, and the ids read and sort as the time they were made."""
    monkeypatch.setattr(sim.time, "strftime", lambda fmt, *a: "20261104-133330")
    runs = tmp_path / "runs"
    runs.mkdir()
    ids: list[str] = []
    lock = threading.Lock()

    def mint(n):
        for _ in range(n):
            jid, d = sim.new_run(runs)
            assert d.is_dir() and d.name == jid
            with lock:
                ids.append(jid)
    threads = [threading.Thread(target=mint, args=(500,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == len(set(ids)) == 4000 and all(i.startswith("20261104-133330-") and len(i) == 20 for i in ids)
    # one thread's ids of one second sort in the order they were made (the newest run is the last id)
    sim._minted = ("", 0, set())
    made = [sim.new_run(runs)[0] for _ in range(300)]
    assert sorted(made) == made
    # another process made a directory in that second: another id; and an id given once is not given again, also in
    # another directory
    other = tmp_path / "other"
    other.mkdir()
    for s in ("000a", "001a"):
        (other / f"20261104-133330-{s}").mkdir()
    sim._minted = ("", 0, set())
    monkeypatch.setattr(sim.uuid, "uuid4", lambda: type("U", (), {"hex": "a" * 32})())
    assert sim.new_run(other)[0] == "20261104-133330-002a"
    assert sim.new_run(tmp_path)[0] == "20261104-133330-003a"
    # past 4096 ids in the second: random suffixes, still never one given before
    sim._minted = ("20261104-133330", 0x1000, {"003a"})
    hexes = iter(["003a" + "0" * 28, "beef" + "0" * 28])
    monkeypatch.setattr(sim.uuid, "uuid4", lambda: type("U", (), {"hex": next(hexes)})())
    assert sim.new_run(tmp_path)[0] == "20261104-133330-beef"
    monkeypatch.undo()
    a, _ = sim.new_run(runs)
    time.sleep(1.05)
    b, _ = sim.new_run(runs)
    assert sorted([b, a]) == [a, b]


def test_sixty_checks_started_in_one_second_have_ids_of_their_own(tmp_path):
    """A fresh run_checks starts about 60 check jobs in one second (verify._new_job): with ids of 4 random hex digits
    two used to collide ("File exists"); now each has its own run directory and its own place in LIVE."""
    jobs = sim.Jobs()
    made = [verify._new_job(jobs, tmp_path, "drc", f"DRC {i}", {}) for i in range(300)]
    assert len({j.id for j in made}) == 300 and all(verify.LIVE[j.id] is j for j in made)
    for j in made:
        jobs.untrack(j)
        verify.LIVE.pop(j.id, None)
