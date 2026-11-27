"""Phase 4 of docs/design/packages.md, Changesets and evidence: job inputs record the model files a simulation reads
and the PDK profile fields a run uses inside the key, and what the run used (packages, PDK, profile) outside it, so
outdated detection stays on what the job reads: an update that changes one cell outdates only the results that use
that cell, a README change outdates nothing, and a model file a package includes outdates the simulation even when
the netlist text is the same. A changeset's evidence lists each side's packages and PDK, and each job's `about`."""
import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
from undine import tools  # noqa: E402
from undine.layouts import layoutstore  # noqa: E402
from undine.pdks import pdkprofile  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402
from undine.server.api import Server  # noqa: E402
from undine.simulation import modellib, sim  # noqa: E402
from undine.store import deps, provenance, trust  # noqa: E402
from test_changeset import setup  # noqa: E402
from test_netlist_sources import _cell, _code, _devices, _r, _sub  # noqa: E402
from test_pkg_index import NOW, _commit  # noqa: E402
from test_provenance import _fake_job  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

MANIFEST = '[package]\nname = "analog_ip"\nversion = "{v}"\n'


@pytest.fixture(autouse=True)
def _store(fresh_store, monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    return fresh_store


def test_the_worktrees_code_is_tested():
    assert Path(provenance.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])


# ------------------------------------------------------------------ a package, a project using it, simulations


class Case:
    """Package analog_ip (cells inva and invb, a model file inva's code block includes) in a git repository, a
    project whose testbenches tb_a and tb_b place one cell each, and start_sim submitting nothing: each job is a
    status.json with what the run would have recorded."""

    def __init__(self, tmp_path: Path, monkeypatch):
        _devices()
        self.repo = tmp_path / "src" / "analog_ip"
        root = tmp_path / "projects"
        self.srv = Server(root, {}, [root])
        prj = self.srv.store.create("p", None)
        prj.create_library("l")
        deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
        self.dep = deps.dep_dir(prj.path, "analog_ip")
        self.models = self.dep / "models.spice"
        self.jobs: list[sim.Job] = []

        def submit(prj_path, name, text, analysis, meta, osdi=None, timeout_s=None, models=None):
            d = self.srv.jobs.runs_dir(Path(prj_path)) / f"sim-{len(self.jobs):04d}"
            d.mkdir(parents=True)
            j = sim.Job(d)
            j.set(id=d.name, state="done", **meta)
            self.jobs.append(j)
            return j
        monkeypatch.setattr(self.srv.jobs, "submit", submit)

    @property
    def prj(self):
        return self.srv.project("p")

    def release(self, version: str, b_value: str = "2k", model: str = "1k", readme: str = "analog_ip\n") -> None:
        """Version `version` of the package: invb's resistor `b_value`, the model file's parameter `model`."""
        d = self.repo
        _cell(d, "inva", [*_r("devices/res.sym", "R1", "1k"), _code("s1", f".include {self.models}")])
        _cell(d, "invb", _r("devices/res.sym", "R1", b_value))
        _commit(d, {"models.spice": f"* analog_ip models\n.param rmodel={model}\n", "README.md": readme,
                    "undine-package.toml": MANIFEST.format(v=version)}, version, f"v{version}")

    def use(self, version: str) -> None:
        """The project at `version` (added, or updated to it), trusted (inva's .include reads a file outside a PDK)."""
        if "analog_ip" in self.prj.meta.get("libraries", {}):
            deps.update(self.prj, "analog_ip", f"v{version}")
        else:
            deps.add(self.prj, "analog_ip", str(self.repo), f"v{version}")
            for tb, cell in (("tb_a", "inva"), ("tb_b", "invb")):
                _cell(self.prj.library("l").path, tb, _sub(f"analog_ip/{cell}.sym", "x1"), ports=False)
        trust.set_trusted(self.prj, f"analog_ip@{version}", True)

    def run(self, tb: str, reuse: bool = False) -> sim.Job:
        return self.srv.start_sim("p", "l", tb, {"type": "op"}, reuse=reuse)

    def fresh(self, job: sim.Job) -> dict:
        return provenance.freshness(self.srv, "p", job.status())


@pytest.fixture
def case(tmp_path, monkeypatch):
    return Case(tmp_path, monkeypatch)


def test_update_that_changes_one_cell_outdates_only_its_results(case):
    case.release("1.0.0")
    case.release("1.1.0", b_value="3k")
    case.use("1.0.0")
    ja, jb = case.run("tb_a"), case.run("tb_b")
    assert case.fresh(ja) == case.fresh(jb) == {"fresh": True, "changed": []}
    about = ja.status()["about"]
    assert [(p["name"], p["package"], p["version"]) for p in about["packages"]] == [("analog_ip", "analog_ip", "1.0.0")]
    case.use("1.1.0")
    assert case.fresh(ja) == {"fresh": True, "changed": []}                    # inva is the same in 1.1.0
    assert case.fresh(jb) == {"fresh": False, "changed": ["schematic netlist of l/tb_b changed"]}
    assert case.run("tb_a", reuse=True).id == ja.id                             # the same run, whatever the version
    again = case.run("tb_b", reuse=True)
    assert again.id != jb.id and again.status()["about"]["packages"][0]["version"] == "1.1.0"


def test_readme_change_outdates_nothing(case):
    case.release("1.0.0")
    case.release("1.0.1", readme="analog_ip, now with a longer description\n")
    case.use("1.0.0")
    ja, jb = case.run("tb_a"), case.run("tb_b")
    case.use("1.0.1")
    assert deps.read_lock(case.prj.path)["libraries"]["analog_ip"]["version"] == "1.0.1"
    assert case.fresh(ja) == case.fresh(jb) == {"fresh": True, "changed": []}
    assert case.run("tb_a", reuse=True).id == ja.id and case.run("tb_b", reuse=True).id == jb.id


def test_model_file_change_outdates_simulation(case):
    case.release("1.0.0")
    case.release("1.0.2", model="2k")                                          # no cell changes, the model file does
    case.use("1.0.0")
    ja, jb = case.run("tb_a"), case.run("tb_b")
    path = str(case.models.resolve())
    inp = ja.status()["inputs"]
    assert [m["path"] for m in inp["models"]] == [path] and inp["models"][0]["sha"] == provenance.file_sha(case.models)
    assert jb.status()["inputs"]["models"] == []                               # tb_b reads no file
    case.use("1.0.2")
    assert case.fresh(ja) == {"fresh": False, "changed": [f"model file {path} changed"]}
    assert case.fresh(jb) == {"fresh": True, "changed": []}
    assert ja.status()["inputs"]["design"] == case.run("tb_a").status()["inputs"]["design"]   # the same netlist


def test_files_read_follows_includes_and_library_sections(tmp_path):
    """modellib.files_read: .include and .lib targets, transitively, a .lib section's own files only, a missing file
    listed all the same, a line it cannot tell left out."""
    (tmp_path / "lib.spice").write_text(".lib tt\n.include tt.spice\n.endl tt\n.lib ff\n.include ff.spice\n.endl ff\n")
    (tmp_path / "tt.spice").write_text(".model n nmos\n")
    (tmp_path / "ff.spice").write_text(".model n nmos\n")
    (tmp_path / "a.spice").write_text(".include b.spice\n")
    (tmp_path / "b.spice").write_text("* b\n")
    text = f"* tb\n.lib {tmp_path}/lib.spice tt\n.include a.spice\n.include gone.spice\n.include a b c\n.end\n"
    got = modellib.files_read(text, tmp_path)
    assert got == [str(tmp_path / f) for f in ("lib.spice", "tt.spice", "a.spice", "gone.spice", "b.spice")]


# ------------------------------------------------------------------ profile fields in the key, about outside it


def _fake_pdk(tmp_path: Path) -> Pdk:
    """A sky130A install with only its KLayout decks (the profile is SKY130's)."""
    root = tmp_path / "pdks" / "sky130A"
    tech = root / "libs.tech" / "klayout"
    for rel in ("drc/sky130A_mr.drc", "lvs/sky130.lvs"):
        (tech / rel).parent.mkdir(parents=True, exist_ok=True)
        (tech / rel).write_text(f"# {rel}\n")
    return Pdk("sky130A", "sky130", "8afc8346a57f" + "0" * 28, root, klayout_tech=tech)


def test_profile_fields_in_key(tmp_path, monkeypatch):
    pdk = _fake_pdk(tmp_path)
    root = tmp_path / "projects"
    srv = Server(root, {pdk.id: pdk}, [root])
    lib = srv.store.create("p", pdk.id).create_library("l")
    ly = kdb.Layout()
    ly.create_cell("top")
    layoutstore.write(lib.path, ly, {})
    run = srv.prepare_drc("p", "l", "top")
    assert run.inputs["profile"] == {"drc_options": {"feol": True, "beol": True, "offgrid": True}, "deck_vars": {}}
    assert provenance.profile_fields(pdk, provenance.LVS_PROFILE) == {
        "lvs_options": {"lvs_sub": "VSS", "scale": True, "schematic_simplify": True}, "deck_vars": {}}
    assert provenance.profile_fields(pdk, provenance.MAGIC_DRC_PROFILE) == {"magic_maskhints": ["RPM", "URPM"]}
    assert provenance.profile_fields(pdk, provenance.SIM_PROFILE) == {
        "model_lib": "libs.tech/ngspice/sky130.lib.spice", "model_includes": [], "osdi": [],
        "corners": ["tt", "ss", "ff", "sf", "fs"]}
    _fake_job(srv.project("p").path, "20260101-000000-drc", run)
    status = srv.jobs.get(srv.project("p").path, "20260101-000000-drc").status()
    assert provenance.freshness(srv, "p", status) == {"fresh": True, "changed": []}
    # a field the run does not use: an LVS option for a DRC run
    monkeypatch.setitem(pdkprofile.SKY130.lvs_options, "scale", False)
    assert provenance.freshness(srv, "p", status)["fresh"] is True
    monkeypatch.setitem(pdkprofile.SKY130.drc_options, "sky130?_mr.drc", {"feol": True, "beol": True, "offgrid": False})
    assert provenance.freshness(srv, "p", status) == {"fresh": False, "changed": [
        "profile field drc_options: {'feol': True, 'beol': True, 'offgrid': True} → "
        "{'feol': True, 'beol': True, 'offgrid': False}"]}
    assert provenance.key(srv.prepare_drc("p", "l", "top").inputs) != status["key"]
    assert srv.reusable("p", srv.prepare_drc("p", "l", "top").inputs) is None


def test_about_outside_key():
    inp = {"design": [{"view": "text", "sha": "1"}], "pdk": None, "deck": None, "tools": {}, "options": {},
           "models": [{"path": "/m.spice", "sha": "2"}], "profile": {"model_lib": None}}
    a = {"packages": [{"name": "analog_ip", "package": "analog_ip", "version": "1.0.0", "tree": "sha256:1",
                       "index": None}], "pdk": None, "profile": {"family": None, "source": "built-in", "undine": "x"}}
    b = {**a, "packages": [{**a["packages"][0], "version": "1.0.1", "tree": "sha256:2"}]}
    assert provenance.meta(inp, a)["key"] == provenance.meta(inp, b)["key"] == provenance.key(inp)
    assert provenance.meta(inp, a)["about"] == a and "about" not in provenance.meta(inp)
    other = {**inp, "models": [{"path": "/m.spice", "sha": "3"}]}
    assert provenance.key(other) != provenance.key(inp)
    assert provenance.diff(inp, other) == ["model file /m.spice changed"]
    assert provenance.diff(inp, {**inp, "profile": {"model_lib": "x.lib"}}) == ["profile field model_lib: None → x.lib"]


def test_old_job_without_models_stays_fresh(case):
    case.release("1.0.0")
    case.use("1.0.0")
    j = case.run("tb_a")
    st = j.status()
    old = {k: v for k, v in st["inputs"].items() if k not in ("models", "profile")}
    j.set(inputs=old, key=provenance.key(old), drop=("about",))                # as a job recorded before this change
    assert case.fresh(j) == {"fresh": True, "changed": []}                     # compared without them, not unknown
    assert "models" not in provenance.recompute(case.srv, "p", old)
    assert provenance.diff(old, st["inputs"]) == []                            # signoff: nothing to say of them


# ------------------------------------------------------------------ the evidence of a changeset


def test_evidence_manifest_lists_packages_and_pdk(tmp_path):
    root, srv, cs = setup()
    try:
        pdk = _fake_pdk(tmp_path)
        srv.pdks[pdk.id] = pdk
        prj = srv.project("p")
        prj.set_pdk(pdk.id)
        repo = tmp_path / "src" / "analog_ip"
        _commit(repo, {"cell.sym": "v {}\nK {type=subcircuit}\n", "undine-package.toml": MANIFEST.format(v="1.0.0")},
                "1.0.0", "v1.0.0")
        deps.add(prj, "analog_ip", str(repo), "v1.0.0")
        entry = deps.read_lock(prj.path)["libraries"]["analog_ip"]
        meta = cs.create("p", "A note", "evidence", "agent:test")
        work = srv.project(meta["preview"])
        inp = {"design": [], "pdk": pdk.id, "deck": None, "tools": {}, "options": {}}
        about = provenance.about(work, [deps.dep_dir(work.path, "analog_ip").resolve() / "cell.sym"])
        d = srv.jobs.runs_dir(work.path) / "sim-0000"
        d.mkdir(parents=True)
        sim.Job(d).set(id="sim-0000", kind="sim", state="done", **provenance.meta(inp, about))
        cs.discard("p", meta["id"])
        ev = cs.evidence("p", meta["id"])
        kind, h = deps.entry_hash(entry)
        want = [{"name": "analog_ip", "names": ["analog_ip"], "package": entry.get("package"),
                 "version": entry.get("version"), kind: h, "index": entry.get("index")}]
        assert ev["packages"] == {"base": want, "work": want}
        pin = {"id": pdk.id, "commit": pdk.commit, "family": "sky130", "variant": "sky130A", "tree": None}
        assert ev["pdk"] == {"base": pin, "work": pin}
        manifest = json.loads((srv.project("p").path / ev["path"] / "manifest.json").read_text())
        assert (manifest["packages"], manifest["pdk"]) == (ev["packages"], ev["pdk"])
        job = next(j for j in ev["jobs"] if j["id"] == "sim-0000")
        assert job["side"] == "work" and job["about"] == about
        assert [p["name"] for p in about["packages"]] == ["analog_ip"] and about["pdk"] == pin
        assert about["profile"] == {"family": "sky130", "source": "built-in", "undine": tools.undine_version()}
    finally:
        shutil.rmtree(root)


# ------------------------------------------------------------------ regressions from the review


def test_signoff_takes_a_drc_run_from_before_profiles_as_the_same_run(tmp_path):
    """A run recorded before `profile` entered the inputs has another key; signoff takes it as the same run when
    everything it recorded is the same, not as one that "ran with other settings"."""
    pdk = _fake_pdk(tmp_path)
    root = tmp_path / "projects"
    srv = Server(root, {pdk.id: pdk}, [root])
    lib = srv.store.create("p", pdk.id).create_library("l")
    ly = kdb.Layout()
    ly.create_cell("top")
    layoutstore.write(lib.path, ly, {})
    run = srv.prepare_drc("p", "l", "top", "sky130A_mr.drc")
    old = copy.copy(run)
    old.inputs = {k: v for k, v in run.inputs.items() if k != "profile"}
    _fake_job(srv.project("p").path, "20260101-000000-drc", old)
    row = next(c for c in srv.signoff("p", "l", "top") if c.get("kind") == "drc" and c.get("job"))
    assert row["fresh"] is True and row["changed"] == []
    # a run whose recorded design differs is still not the same
    old2 = copy.copy(run)
    old2.inputs = {**old.inputs, "options": {**old.inputs["options"], "other": 1}}
    _fake_job(srv.project("p").path, "20260102-000000-drc", old2)
    row = next(c for c in srv.signoff("p", "l", "top") if c.get("kind") == "drc" and c.get("job"))
    assert row["job"] == "20260102-000000-drc" and row["fresh"] is False


def test_a_magic_drc_run_from_before_its_mask_hints_is_outdated(tmp_path, monkeypatch):
    """Magic's DRC reads the profile's magic_maskhints (sky130: RPM, URPM) as drawn. A run from before (no `profile`
    in its inputs) made them again from the resistors, so its rpm.1 is not today's: signoff and freshness say it is
    outdated, not that it is the same run (provenance.LATER). A PDK without mask hints (GF180MCU, IHP) records no
    profile, so its runs keep their key, and one from before stays fresh."""
    import dataclasses
    from undine.checks import verify
    pdk = _fake_pdk(tmp_path)
    (pdk.root / "libs.tech" / "magic").mkdir(parents=True)
    (pdk.root / "libs.tech" / "magic" / "sky130A.magicrc").write_text("# rc\n")
    if not verify.decks(pdk)["magic"]:
        pytest.skip("Magic is not installed")
    root = tmp_path / "projects"
    srv = Server(root, {pdk.id: pdk}, [root])
    lib = srv.store.create("p", pdk.id).create_library("l")
    ly = kdb.Layout()
    ly.create_cell("top")
    layoutstore.write(lib.path, ly, {})
    prj = srv.project("p").path

    def row():
        return next(c for c in srv.signoff("p", "l", "top") if c.get("tool") == "magic")
    run = srv.prepare_magic_drc("p", "l", "top")
    assert run.inputs["profile"] == {"magic_maskhints": ["RPM", "URPM"]}
    old = copy.copy(run)
    old.inputs = {k: v for k, v in run.inputs.items() if k != "profile"}
    _fake_job(prj, "20260101-000000-drc", old)
    why = ["profile field magic_maskhints: not read → ['RPM', 'URPM']"]
    r = row()
    assert (r["job"], r["fresh"], r["changed"]) == ("20260101-000000-drc", False, why)
    assert provenance.freshness(srv, "p", srv.jobs.get(prj, "20260101-000000-drc").status()) == {"fresh": False,
                                                                                                "changed": why}
    _fake_job(prj, "20260102-000000-drc", run)                                  # a run of today's
    r = row()
    assert (r["job"], r["fresh"], r["changed"]) == ("20260102-000000-drc", True, [])
    # without mask hints: no profile in the inputs (the key the run had before), and the old run is the same run
    real = pdkprofile.for_pdk
    monkeypatch.setattr(pdkprofile, "for_pdk", lambda n, f: dataclasses.replace(real(n, f), magic_maskhints=()))
    plain = srv.prepare_magic_drc("p", "l", "top")
    assert "profile" not in plain.inputs and provenance.key(plain.inputs) == provenance.key(old.inputs)
    r = row()
    assert (r["job"], r["fresh"], r["changed"]) == ("20260101-000000-drc", True, [])
    assert provenance.freshness(srv, "p", srv.jobs.get(prj, "20260101-000000-drc").status())["fresh"] is True


def test_lvs_checks_are_in_the_lvs_key_only_where_the_profile_has_them(tmp_path, monkeypatch):
    """The parameters Undine compares after the LVS deck (lvs_elements' check: sky130's poly resistor W) are a profile
    field of a KLayout LVS run, lvs_checks, recorded only where the profile has some: a PDK without (GF180MCU, IHP)
    keeps its LVS key. A tolerance changed outdates the run; a sky130 run from before lvs_checks compared no W, so it
    is outdated (provenance.unrecorded), not the same run."""
    import dataclasses
    from types import SimpleNamespace
    pdk = _fake_pdk(tmp_path)
    root = tmp_path / "projects"
    srv = Server(root, {pdk.id: pdk}, [root])
    lib = srv.store.create("p", pdk.id).create_library("l")
    ly = kdb.Layout()
    ly.create_cell("top")
    layoutstore.write(lib.path, ly, {})
    prj = srv.project("p").path
    checks = {"sky130_fd_pr__res_x?high_po_(0p35|0p69|1p41|2p85|5p73)": {"W": 0.005}}
    now = provenance.lvs_profile(pdk)
    assert now == {"lvs_options": {"lvs_sub": "VSS", "scale": True, "schematic_simplify": True}, "deck_vars": {},
                   "lvs_checks": checks}
    base = srv.prepare_drc("p", "l", "top").inputs

    def job(jid: str, profile: dict) -> dict:
        _fake_job(prj, jid, SimpleNamespace(kind="lvs", tool="klayout", lib="l", cell="top", deck="sky130.lvs",
                                            inputs={**base, "profile": profile}))
        return provenance.freshness(srv, "p", srv.jobs.get(prj, jid).status())
    assert job("20260101-000000-lvs", now) == {"fresh": True, "changed": []}
    old = {k: v for k, v in now.items() if k != "lvs_checks"}
    assert job("20260102-000000-lvs", old) == {"fresh": False,
                                               "changed": [f"profile field lvs_checks: not read → {checks}"]}
    assert provenance.unrecorded("lvs", "netgen", {"profile": old}, pdk) == []        # Netgen's LVS has no checks
    real = pdkprofile.for_pdk

    def with_check(check: dict):
        return lambda n, f: dataclasses.replace(real(n, f), lvs_elements=tuple(
            dataclasses.replace(el, check=check if el.check else {}) for el in real(n, f).lvs_elements))
    monkeypatch.setattr(pdkprofile, "for_pdk", with_check({"W": 0.01}))
    st = srv.jobs.get(prj, "20260101-000000-lvs").status()
    assert provenance.freshness(srv, "p", st)["changed"] == [
        f"profile field lvs_checks: {checks} → {{'sky130_fd_pr__res_x?high_po_(0p35|0p69|1p41|2p85|5p73)': "
        "{'W': 0.01}}"]
    # a profile without checks: the key LVS runs had, and a run from before stays fresh
    monkeypatch.setattr(pdkprofile, "for_pdk", with_check({}))
    assert provenance.lvs_profile(pdk) == provenance.profile_fields(pdk, provenance.LVS_PROFILE) == old
    assert provenance.freshness(srv, "p", srv.jobs.get(prj, "20260102-000000-lvs").status()) == {"fresh": True,
                                                                                                "changed": []}
    monkeypatch.setattr(pdkprofile, "for_pdk", real)
    for name, prof in pdkprofile.profiles().items():
        if prof.family != "sky130":
            other = SimpleNamespace(profile=prof, name=name)
            assert "lvs_checks" not in provenance.lvs_profile(other), name


def test_files_read_never_raises_and_resolves_from_the_run_directory(tmp_path):
    d = tmp_path / "prj" / "runs" / ".next"
    (tmp_path / "prj" / "models").mkdir(parents=True)
    (tmp_path / "prj" / "models" / "m.spice").write_text("* m\n")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    got = modellib.files_read(f".include ../../models/m.spice\n.include {loop}\n.include bad\x00name\n", d)
    assert str((tmp_path / "prj" / "models" / "m.spice").resolve()) in got


def test_recompute_takes_the_osdi_models_the_pdk_has_now(tmp_path, monkeypatch):
    """OSDI models are loaded by .spiceinit, not the netlist: a model compiled again elsewhere is seen."""
    old, new = tmp_path / "old.osdi", tmp_path / "new.osdi"
    old.write_bytes(b"1")
    new.write_bytes(b"2")
    pdk = _fake_pdk(tmp_path)
    monkeypatch.setattr(type(pdk.profile), "osdi", property(lambda self: ["psp103"]), raising=False)
    monkeypatch.setattr(Pdk, "osdi_files", lambda self: [new])

    class S:
        def project(self, name):
            return type("P", (), {"pdk": pdk, "path": tmp_path})()
    inp = {"design": [], "pdk": pdk.id, "deck": None, "tools": {}, "options": {},
           "models": provenance.model_entries([str(old)])}
    assert provenance.recompute(S(), "p", inp)["models"] == provenance.model_entries([str(new)])
