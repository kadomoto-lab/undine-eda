"""PDK profiles: decks, tools and layers come from the PDK family, not from hard-coded sky130 names."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.pdks import pdkprofile  # noqa: E402
from undine.checks import verify
from undine.pdks.pdk import discover, installs  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402


def fake(family_dir: str, files: list[str]) -> Path:
    root = Path(tempfile.mkdtemp())
    p = root / family_dir
    for f in files:
        (p / f).parent.mkdir(parents=True, exist_ok=True)
        (p / f).write_text("# deck\n")
    return root


def test_gf180_profile_finds_its_decks_and_layers():
    root = fake("gf180mcuD", ["libs.tech/klayout/tech/drc/gf180mcu.drc", "libs.tech/klayout/tech/lvs/gf180mcu.lvs",
                              "libs.tech/magic/gf180mcuD.magicrc", "libs.tech/netgen/gf180mcuD_setup.tcl"])
    pdks = discover([root])
    pdk = next(iter(pdks.values()))
    assert pdk.family == "gf180mcu" and pdk.profile.label.startswith("GlobalFoundries") and pdk.profile.verified
    d = verify.decks(pdk)
    assert d["drc"] == ["gf180mcu.drc"] and d["lvs"] == ["gf180mcu.lvs"]           # under libs.tech/klayout/tech
    assert pdk.profile.layers[pdk.profile.gate_layers[0]] == (30, 0)       # poly2
    assert pdk.profile.magic_file(pdk.name) == "gf180mcuD.magicrc"
    shutil.rmtree(root)


def test_decks_get_the_switches_and_metal_stack_the_way_they_read_them():
    sky, gf, ihp = pdkprofile.SKY130, pdkprofile.GF180, pdkprofile.IHP
    assert sky.drc_rd("sky130A_mr.drc", {"beol": False}, "sky130A") == {"feol": "true", "beol": "false", "offgrid": "true"}
    assert sky.lvs_rd({"lvs_sub": "VGND"}, "sky130A") == {"run_mode": "deep", "lvs_sub": "VGND", "scale": "true"}
    rd = gf.drc_rd("gf180mcu.drc", {"feol": True, "beol": False, "offgrid": True}, "gf180mcuD")
    assert rd == {"metal_level": "5LM", "metal_top": "11K", "mim_option": "B", "decks": "all,-density,-antenna,-cup,-beol"}
    assert gf.drc_rd("gf180mcu.drc", {"decks": "density"}, "gf180mcuD")["decks"] == "density"   # a shuttle's density run
    assert gf.lvs_rd({}, "gf180mcuA")["metal_level"] == "3LM"
    assert ihp.drc_rd("ihp-sg13g2.drc", {"feol": True, "beol": True, "offgrid": False}, "ihp-sg13g2") == \
        {"no_feol": "false", "no_beol": "false", "no_offgrid": "true"}
    assert ihp.lvs_rd({}, "ihp-sg13g2") == {"run_mode": "deep"}


def test_ciel_puts_both_ihp_processes_under_one_family():
    h = "89d42ee6d2fd9b50b86a1aada6fee6673c3e6008"
    root = fake(f"ciel/ihp-sg13/versions/{h}/ihp-sg13g2", ["libs.tech/klayout/tech/drc/ihp-sg13g2.drc",
                                                            "libs.tech/klayout/tech/lvs/sg13g2.lvs"])
    fake_in = root / f"ciel/ihp-sg13/versions/{h}/ihp-sg13cmos5l/libs.tech/klayout/tech/drc/ihp-sg13cmos5l.drc"
    fake_in.parent.mkdir(parents=True)
    fake_in.write_text("# deck\n")
    pdks = {p.name: p for p in discover([root]).values()}
    g2, c5 = pdks["ihp-sg13g2"], pdks["ihp-sg13cmos5l"]
    assert g2.family == c5.family == "ihp-sg13"
    assert g2.profile is pdkprofile.IHP and verify.decks(g2)["lvs"] == ["sg13g2.lvs"]
    assert c5.profile is pdkprofile.IHP_CMOS5L and verify.decks(c5)["drc"] == ["ihp-sg13cmos5l.drc"] and not verify.decks(c5)["lvs"]
    assert c5.public()["profile"]["verified"] is False and c5.public()["profile"]["limits"]   # the UI is told what is missing
    assert pdkprofile.for_pdk("sky130B", "sky130").magic_file("sky130B") == "sky130B.magicrc"
    shutil.rmtree(root)


def test_ciel_store_and_enabled_symlink():
    h = "bdc9412b3e468c102d01b7cf6337be06ec6e9c9a"
    root = fake(f"ciel/sky130/versions/{h}/sky130A", ["libs.tech/klayout/tech/sky130A.lyp"])
    (root / "sky130A").symlink_to(f"ciel/sky130/versions/{h}/sky130A")    # what `ciel enable` creates
    pdks = discover([root])
    assert list(pdks) == ["sky130A@bdc9412b3e46"]                          # same id as a volare install of that hash
    pdk = pdks["sky130A@bdc9412b3e46"]
    assert pdk.family == "sky130" and pdk.version == h and pdk.lyp.name == "sky130A.lyp"
    shutil.rmtree(root)


def test_failed_create_leaves_nothing_behind():
    root = Path(tempfile.mkdtemp())
    store = ProjectStore(root, {})
    try:
        store.create("p1", "no-such-pdk")
    except KeyError:
        pass
    assert not (root / "p1").exists()                                      # no half-made project
    (root / "p2").mkdir()                                                   # an empty directory is not a project
    assert store.create("p2").path == root / "p2"
    shutil.rmtree(root)


def test_unknown_family_is_generic_not_sky130():
    assert pdkprofile.for_family("mystery").family == "generic"
    assert pdkprofile.for_family("sky130").family == "sky130" and pdkprofile.for_family("ihp-sg13g2").family == "ihp-sg13g2"



def test_a_pdk_with_verilog_a_models_needs_them_compiled():
    from undine.pdks.pdk import LEGACY_OSDI
    from undine.server.api import Server
    h = "89d42ee6d2fd9b50b86a1aada6fee6673c3e6008"
    root = fake(f"ciel/ihp-sg13/versions/{h}/ihp-sg13g2", ["libs.tech/klayout/tech/drc/ihp-sg13g2.drc"])
    pdks = discover([root])
    srv = Server(root / "projects", pdks, [root])
    prj = srv.store.create("p", next(iter(pdks)))
    old = {k: os.environ.get(k) for k in ("UNDINE_STORE", "UNDINE_CACHE")}
    os.environ["UNDINE_STORE"], os.environ["UNDINE_CACHE"] = str(root / "store"), str(root / "cache")
    try:
        pdk = prj.pdk
        assert pdk.profile.osdi[:2] == ("psp103", "psp103_nqs")                # model names, not paths
        assert pdk.osdi_dir == root / "store" / "build" / h / "ihp-sg13g2" / "osdi"
        try:
            srv._osdi(prj)
            raise AssertionError("models are missing")
        except ValueError as e:
            assert "psp103.osdi" in str(e) and "cap_cmomf.osdi" in str(e) and "./setup.sh osdi" in str(e)
        cache = root / "cache" / "osdi" / "ihp-sg13g2@89d42ee6d2fd"             # an earlier Undine's cache: found when it alone has it
        cache.mkdir(parents=True)
        (cache / "psp103.osdi").write_bytes(b"")
        assert pdk.osdi_files()[0] == cache / "psp103.osdi" and pdk.legacy_osdi_cached()
        try:
            srv._osdi(prj)
            raise AssertionError("models are missing")
        except ValueError as e:
            assert "psp103.osdi" not in str(e) and "psp103_nqs.osdi" in str(e)
        for m in pdk.profile.osdi:                         # compiled into the PDK (an older setup): taken before the cache
            (pdk.root / LEGACY_OSDI).mkdir(parents=True, exist_ok=True)
            (pdk.root / LEGACY_OSDI / f"{m}.osdi").write_bytes(b"")
        assert [p.name for p in srv._osdi(prj)][:2] == ["psp103.osdi", "psp103_nqs.osdi"]
        assert all(f.is_relative_to(pdk.root) for f in srv._osdi(prj)) and not pdk.legacy_osdi_cached()
        pdk.osdi_dir.mkdir(parents=True)                   # where scripts/osdi.sh puts them now: taken first
        (pdk.osdi_dir / "psp103.osdi").write_bytes(b"")
        assert srv._osdi(prj)[0] == pdk.osdi_dir / "psp103.osdi" and srv._osdi(prj)[1].is_relative_to(pdk.root)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(root)


def test_a_pdk_without_a_version_builds_where_it_is():
    import hashlib
    root = fake("ihp-sg13g2", ["libs.tech/klayout/tech/drc/ihp-sg13g2.drc"])     # a plain root, no commit
    pdk = next(iter(discover([root]).values()))
    key = hashlib.sha256(str((root / "ihp-sg13g2").resolve()).encode()).hexdigest()[:12]
    from undine.home import store_root
    assert pdk.osdi_dir == store_root() / "build" / f"local-{key}" / "ihp-sg13g2" / "osdi"
    shutil.rmtree(root)


def test_a_bare_pdk_name_picks_the_supported_version():
    from undine.pdks import pcell
    from undine.pdks.pdk import Pdk
    from undine.store.project import pick_pdk
    good, other = pcell.SUPPORTED["sky130"], "0123456789abcdef0123456789abcdef01234567"
    pdks = {p.id: p for p in (Pdk("sky130A", "sky130", other, Path("/x")), Pdk("sky130A", "sky130", good, Path("/y")))}
    runs = pcell.macros_dir
    pcell.macros_dir = lambda p: Path("/pcells")         # both run their PCells: the tested version is taken
    try:
        assert pick_pdk(pdks, "sky130A").version == good
        assert pick_pdk(pdks, f"sky130A@{other[:12]}").version == other     # an exact id is that one
        pcell.macros_dir = lambda p: Path("/pcells") if p.version == other else None
        assert pick_pdk(pdks, "sky130A").version == other                    # one whose PCells run comes first
    finally:
        pcell.macros_dir = runs


def test_family_of_follows_ciel_and_fixes_a_plain_ihp_root():
    from undine.pdks.pdk import FAMILY_OF_VARIANT, family_of
    assert family_of("sky130A") == family_of("sky130B") == "sky130"
    assert family_of("ihp-sg13g2") == family_of("ihp-sg13cmos5l") == "ihp-sg13"
    assert family_of("gf180mcuD") == "gf180mcu" and family_of("mystery") == "mystery"
    assert set(FAMILY_OF_VARIANT.values()) == {"sky130", "gf180mcu", "ihp-sg13"}
    root = fake("ihp-sg13g2", ["libs.tech/klayout/tech/drc/ihp-sg13g2.drc"])     # a plain root, not under ciel/
    pdk = next(iter(discover([root]).values()))
    assert pdk.family == "ihp-sg13" and pdk.source is None and pdk.commit is None and pdk.id == "ihp-sg13g2"
    assert pdk.profile is pdkprofile.IHP
    shutil.rmtree(root)


def test_source_commit_variant_and_libraries():
    h = "bdc9412b3e468c102d01b7cf6337be06ec6e9c9a"
    root = fake(f"ciel/sky130/versions/{h}/sky130A", ["libs.tech/klayout/tech/sky130A.lyp",
                                                       "libs.ref/sky130_fd_sc_hd/README", "libs.ref/sky130_fd_pr/README"])
    for f in (f"volare/sky130/versions/{h}/sky130B/libs.tech/x", "gf180mcuD/libs.tech/x"):
        (root / f).parent.mkdir(parents=True)
        (root / f).write_text("")
    by = {p.name: p for p in discover([root]).values()}
    assert (by["sky130A"].source, by["sky130B"].source, by["gf180mcuD"].source) == ("ciel", "volare", None)
    a = by["sky130A"]
    assert a.commit == h and a.variant == "sky130A" and a.libraries() == ["sky130_fd_pr", "sky130_fd_sc_hd"]
    assert by["sky130B"].libraries() == [] and by["gf180mcuD"].commit is None
    shutil.rmtree(root)


def test_installs_keeps_what_discover_folds_into_one_id():
    a, b = "8afc8346a57f" + "0" * 28, "8afc8346a57f" + "1" * 28                 # one 12-character prefix
    one, two = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    for base, h in ((one, a), (one, b), (two, a)):
        (base / f"ciel/sky130/versions/{h}/sky130A/libs.tech").mkdir(parents=True)
    (one / "sky130A").symlink_to(f"ciel/sky130/versions/{a}/sky130A")         # the enabled one: not another install
    found = discover([one, two])
    assert list(found) == ["sky130A@8afc8346a57f"] and found["sky130A@8afc8346a57f"].version == a   # first wins
    all_ = installs([one, two, one])
    assert [(p.version, p.root) for p in all_] == [
        (a, one / f"ciel/sky130/versions/{a}/sky130A"), (b, one / f"ciel/sky130/versions/{b}/sky130A"),
        (a, two / f"ciel/sky130/versions/{a}/sky130A")]
    assert all(p.source == "ciel" and p.family == "sky130" for p in all_)
    shutil.rmtree(one)
    shutil.rmtree(two)


def test_legacy_osdi_is_where_ihps_script_puts_models():
    from undine.pdks.pdk import LEGACY_OSDI
    assert LEGACY_OSDI == "libs.tech/ngspice/osdi"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
