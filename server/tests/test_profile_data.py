"""PDK profiles as data (packages phase 6): profile.toml, the formula and stack registry, the built-in packages.

The golden data (data/profiles_golden.json, profile_golden.py) was written from the Python profiles before they became
profile.toml: the profiles loaded from the TOML files must give every value it holds, of the same type."""
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_golden  # noqa: E402
from undine.pdks import formulas, pdkprofile, profiledata, tested  # noqa: E402
from undine.store import manifest  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GOLD = json.loads(profile_golden.GOLDEN.read_text())


def fresh() -> dict:
    """The built-in profile packages loaded again from their files (not the cached copy): {package: ProfileSet}."""
    return {n: profiledata.load_dir(profiledata.BUILTIN / n) for n in profiledata.BUILTIN_PACKAGES}


def same(a, golden) -> list[str]:
    return profile_golden.diff(json.loads(json.dumps(profile_golden.canon(a))), golden)


@pytest.mark.parametrize("package,name", [("profile_sky130", "sky130"), ("profile_gf180mcu", "gf180mcu"),
                                          ("profile_ihp_sg13", "ihp-sg13g2"), ("profile_ihp_sg13", "ihp-sg13cmos5l")])
def test_data_profile_equals_todays_profile(package, name):
    p = fresh()[package].profiles[name]
    assert not same(p, GOLD["profiles"][name])
    assert not same(p, profile_golden.canon(pdkprofile.PROFILES[name]))      # and what pdkprofile serves


def test_for_pdk_per_variant_equals_today():
    sets = fresh()
    for v, want in GOLD["variants"].items():
        s = next((s for s in sets.values() if v in s.variants), None)
        if s is None:                                    # sky130a, sky130C: for_pdk's rule for unlisted names
            continue
        p = s.profiles[s.variants[v]]
        assert p.family == want["profile"]
        assert not same(p.stacks.get(v, p.stack), want["stack"]), v
    assert {v for s in sets.values() for v in s.variants} == {"sky130A", "sky130B", "gf180mcuA", "gf180mcuB",
                                                              "gf180mcuC", "gf180mcuD", "ihp-sg13g2", "ihp-sg13cmos5l"}


def test_golden_data_of_pdkprofile_is_unchanged():
    """Everything the golden file holds (profiles, each variant's stack, deck -rd values, Magic / Netgen files, the
    family rule, the MIM formula), as pdkprofile serves it now."""
    assert not profile_golden.diff(json.loads(json.dumps(profile_golden.golden(pdkprofile))), GOLD)


def test_mim_formula_is_todays_function():
    f = formulas.FORMULAS["mim_area_perimeter"]
    assert f is pdkprofile._mim
    assert {e.params for e in fresh()["profile_sky130"].profiles["sky130"].lvs_elements} == {
        f, formulas.res_length, formulas.bjt_emitters}
    assert f({"w": 1.17, "l": 2.5}) == GOLD["formulas"]["mim_area_perimeter"]
    assert formulas.res_length({"l": 3.88, "m": 2.0}, "sky130_fd_pr__res_xhigh_po_0p35") == GOLD["formulas"]["res_length"]
    assert formulas.bjt_emitters({"m": 24.0}) == GOLD["formulas"]["bjt_emitters"]


def test_builtin_packages_are_profile_packages_with_a_manifest():
    for name in profiledata.BUILTIN_PACKAGES:
        d = profiledata.BUILTIN / name
        m = manifest.from_dir(d, name)
        assert m.name == name and str(m.version) == "1.0.0" and m.license == "Apache-2.0"
        assert manifest.select(m.include, [p.name for p in d.iterdir()]) == {"profile.toml", manifest.MANIFEST}
        s = profiledata.load_dir(d)
        assert (s.package, s.version) == (name, "1.0.0")
        assert all(e.tested and e.license == "Apache-2.0" and not e.tree for e in s.pdk)
    assert [s.family for s in profiledata.builtin()] == ["sky130", "gf180mcu", "ihp-sg13"]


def test_pcell_envs_are_environments_of_pixi_toml():
    envs = tomllib.loads((REPO / "pixi.toml").read_text())["environments"]
    assert set(profiledata.PCELL_ENVS) <= set(envs)


def test_verified_and_magic_drc_come_from_undines_table():
    assert tested.flags("ihp-sg13", "1.0.0", "ihp-sg13cmos5l") == (False, True)
    assert tested.flags("sky130", "1.0.0", "sky130") == (True, True)
    # a profile version this Undine has not tested: not verified, Magic DRC as the newest it has
    assert tested.flags("sky130", "1.3.0", "sky130") == (False, True)
    assert tested.flags("sky130", "1.3.0", "sky130x") == (False, False)
    assert tested.flags("mystery", "1.0.0", "mystery") == (False, False)
    newer = (profiledata.BUILTIN / "profile_sky130" / "profile.toml").read_text()
    p = profiledata.load(newer, "x", "profile_sky130", "1.3.0").profiles["sky130"]
    assert (p.verified, p.magic_drc) == (False, True)


def test_a_stack_generator_makes_what_the_written_stack_would():
    st = formulas.gf180_stack(2)
    assert [c.name for c in st.conductors] == ["poly", "metal1", "metal2"] and not st.plates
    assert st.cuts[-1].rules == "V1.1, V1.2b, V1.3a, V1.3c, V1.3d, V1.4a, V1.4b, V1.4c"


# ------------------------------------------------------------------ refusals

MINI = """\
schema = 1
undine = ">=0.1.0"
family = "sky130"
{top}
[[profile]]
name = "sky130"
label = "x"
variants = ["sky130A"]
metals = ["met1"]
max_metal = "met1"
layers = {{ met1 = [68, 20] }}
{prof}
{tail}
"""


def mini(top="", prof="", tail="") -> str:
    return MINI.format(top=top, prof=prof, tail=tail)


def load(text, version="1.0.0"):
    return profiledata.load(text, "test", "profile_sky130", version, undine_version="0.1.0")


def refused(text, *words):
    with pytest.raises(profiledata.ProfileError) as e:
        load(text)
    msg = str(e.value)
    assert msg.startswith("test: profile.toml: ") and all(w in msg for w in words), msg
    return msg


def test_a_minimal_profile_loads_with_the_defaults():
    p = load(mini()).profiles["sky130"]
    assert p.layers == {"met1": (68, 20)} and p.metals == ("met1",) and p.stack is None and p.grid_nm == 5
    assert p.verified and p.magic_drc                    # Undine's table: sky130 1.0.0


@pytest.mark.parametrize("text,where", [
    (mini(top="bogus = 1"), "bogus"),
    (mini(prof="bogus = 1"), "profile[0].bogus"),
    (mini(tail='[profile.stack]\ndiff = "diff"\npoly = "poly"\nwell = "nwell"\ntap = "tap"\nconductors = []\n'
               'cuts = []\nbogus = 1'), "profile[0].stack.bogus"),
    (mini(tail='[profile.strap]\nvia = 0.1\nenc1 = [0.1, 0.1]\nenc2 = [0.1, 0.1]\nm2_space = 0.1\nbogus = 1'),
     "profile[0].strap.bogus"),
    (mini(tail='[[pdk]]\ncommit = "' + "a" * 40 + '"\nbogus = 1'), "pdk[0].bogus"),
])
def test_an_unknown_profile_field_is_refused(text, where):
    refused(text, f": {where}: unknown key")


@pytest.mark.parametrize("key", ["verified", "magic_drc"])
def test_undines_own_flags_are_not_profile_keys(key):
    refused(mini(prof=f"{key} = true"), f"profile[0].{key}", "pdks/tested.py")


@pytest.mark.parametrize("text,words", [
    (mini().replace("schema = 1", "schema = 2"), ("schema", "format 2", "update Undine")),
    (mini().replace('">=0.1.0"', '">=0.2"'), ("undine", "made for Undine >=0.2, this is 0.1.0")),
    (mini().replace('family = "sky130"', 'family = "Sky 130"'), ("family", "not a PDK family")),
    (mini(prof='variants = ["sky130B"]').replace('variants = ["sky130A"]\n', ""), ()),     # still valid
    (mini().replace('variants = ["sky130A"]', 'variants = ["gf180mcuD"]'), ("variants", "not a variant of sky130")),
    (mini(tail='[[profile]]\nname = "other"\nlabel = "y"\nvariants = ["sky130A"]\nmetals = []\nmax_metal = ""\n'
               'layers = {}'), ("profile[1].variants", "sky130A is a variant of [[profile]] 'sky130' too")),
    (mini(prof="metals = [1]").replace('metals = ["met1"]\n', ""), ("profile[0].metals[0]", "not a string")),
    (mini().replace("[68, 20]", "[68]"), ('layers.met1', "1 items, not 2")),
    (mini().replace("max_metal", "#"), ("profile[0].max_metal", "missing")),
    (mini(prof='drc_switches = "maybe"'), ("drc_switches", "not a switch mode")),
    (mini(prof='magic_rc = "{name.__class__}.magicrc"'), ("magic_rc", "not a placeholder of this field")),
    (mini(prof='magic_rc = "{corner}.magicrc"'), ("magic_rc", "{corner} is not a placeholder")),
    (mini(prof='mismatch_section = "{corner!r}"'), ("mismatch_section",)),
    (mini(prof='model_lib = "../../etc/passwd"'), ("model_lib", "has a .. part")),
    (mini(prof='model_lib = "/etc/passwd"'), ("model_lib", "not a relative")),
    (mini(prof='model_vars = [["X", "/abs"]]'), ("model_vars[0][1]", "not a relative")),
    (mini(prof="stdcells = [['(unclosed', 'x']]"), ("stdcells[0][0]", "not a regular expression")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "C"\nparams = "os.system"'),
     ("lvs_elements[0].params", "not a formula Undine has", "mim_area_perimeter")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "R"\ncheck = { W = "0.5%" }'),
     ("lvs_elements[0].check.W", "not a number")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "R"\ncheck = { W = 0 }'),
     ("lvs_elements[0].check.W", "not a tolerance", "above 0")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "R"\ncheck = { W = -0.1 }'),
     ("lvs_elements[0].check.W", "not a tolerance")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "R"\ncheck = { W = nan }'),
     ("lvs_elements[0].check.W", "not a tolerance")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "R"\ncheck = { "W L" = 0.1 }'),
     ('lvs_elements[0].check."W L"', "not a parameter name")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "R"\ncheck = { W = { tol = 0.1 } }'),
     ("lvs_elements[0].check.W", "not a number")),
    (mini(tail='[[profile.lvs_elements]]\nmodel = "x"\nletter = "R"\ncheck = { W = 1 }'), ()),     # still valid
    (mini(tail='[profile.pcell_lib]\npaths = ["python"]\nmodule = "cells"\nlibrary = "x"\nenv = "default"'),
     ("pcell_lib.env", "not a PCell environment")),
    (mini(tail='[profile.pcell_lib]\npaths = ["../x"]\nmodule = "cells"\nlibrary = "x"'), ("pcell_lib.paths[0]",)),
    (mini(tail='[profile.pcell_lib]\npaths = ["python"]\nmodule = "os; rm"\nlibrary = "x"'),
     ("pcell_lib.module", "not a dotted name")),
    (mini(tail='[profile.pcell_lib]\npaths = ["python"]\nmodule = "m"\nlibrary = "x"\nlimits = [["cap", "a b", 1]]'),
     ("pcell_lib.limits[0]",)),
    (mini(tail='[profile.deck_vars]\ngf180mcuA = { metal_level = "3LM" }'),
     ("deck_vars.gf180mcuA", "not among this profile's variants")),
    (mini(tail='[profile.drc_options]\n"x.drc" = { list = [1, 2] }'), ('drc_options."x.drc".list',)),
    (mini(tail='[profile.stack]\ngenerator = "rm_rf@1"'), ("stack.generator", "not a stack generator", "gf180_stack@1")),
    (mini(tail='[profile.stack]\ngenerator = "gf180_stack@1"\nthin = 2\ncolour = "red"'),
     ("stack.colour", "not an argument of gf180_stack@1")),
    (mini(tail='[profile.stack]\ngenerator = "gf180_stack@1"'), ("stack.thin", "missing")),
    (mini(tail='[profile.stack]\ngenerator = "gf180_stack@1"\nthin = 2.5'), ("stack.thin", "not an integer")),
    (mini(tail='[[pdk]]\ncommit = "8afc8346a57f"'), ("pdk[0].commit", "not a full commit")),
    (mini(tail='[[pdk]]\ncommit = "' + "a" * 40 + '"\n[[pdk]]\ncommit = "' + "a" * 40 + '"'),
     ("pdk[1].commit", "listed twice")),
    (mini(tail='[[pdk]]\ncommit = "' + "a" * 40 + '"\ncompat = ["gf180mcu/2025"]'),
     ("pdk[0].compat[0]", "not a tag of sky130")),
    (mini(tail='[[pdk]]\ncommit = "' + "a" * 40 + '"\n[pdk.tree.sky130B]\n"libs.tech" = "sha256:00"'),
     ("pdk[0].tree.sky130B", "not a variant")),
    (mini(tail='[[pdk]]\ncommit = "' + "a" * 40 + '"\n[pdk.tree.sky130A]\n"libs.tech" = "sha256:00"'),
     ('pdk[0].tree.sky130A."libs.tech"', "tree hash")),
    ("schema = 1\nundine = '*'\nfamily = 'sky130'\n", ("profile", "missing")),
    ("schema = [", ("not valid TOML",)),
])
def test_a_bad_profile_is_refused_with_its_key_path(text, words):
    if not words:
        load(text)
        return
    refused(text, *words)


def test_a_float_field_takes_an_integer_and_an_untyped_table_keeps_its_types():
    p = load(mini(tail='[profile.strap]\nvia = 1\nenc1 = [0, 1]\nenc2 = [0.1, 0.1]\nm2_space = 0.1\n'
                       '[profile.lvs_options]\nscale = true\nlevel = 1\nmode = "deep"')).profiles["sky130"]
    assert type(p.strap.via) is float and p.strap.enc1 == (0.0, 1.0) and type(p.strap.enc1[0]) is float
    assert p.lvs_options == {"scale": True, "level": 1, "mode": "deep"} and type(p.lvs_options["level"]) is int
    assert p.lvs_rd({}, "sky130A")["level"] == "1"        # not 1.0: the deck reads the text


def test_a_tree_and_tags_of_the_family_are_read():
    h = "sha256:" + "0" * 64
    s = load(mini(tail='[[pdk]]\ncommit = "' + "a" * 40 + '"\ncompat = ["sky130/2025"]\nlicense = "Apache-2.0"\n'
                        f'[pdk.tree.sky130A]\n"libs.tech" = "{h}"\n'
                        '[[pdk]]\ncommit = "' + "b" * 40 + '"\ntested = true'))
    a, b = s.pdk
    assert a.compat == ("sky130/2025",) and a.tree == {"sky130A": {"libs.tech": h}} and not a.tested
    assert s.tested_commits() == ("b" * 40,)


def test_a_directory_that_is_not_a_profile_package_is_refused(tmp_path):
    (tmp_path / manifest.MANIFEST).write_text('[package]\nname = "analog_ip"\nversion = "1.0.0"\n')
    with pytest.raises(profiledata.ProfileError, match="not a profile package's name"):
        profiledata.load_dir(tmp_path)
    (tmp_path / manifest.MANIFEST).write_text('[package]\nname = "profile_sky130"\nversion = "1.0.0"\n')
    with pytest.raises(profiledata.ProfileError, match="no profile.toml"):
        profiledata.load_dir(tmp_path)
    (tmp_path / "profile.toml").write_text(mini())
    assert profiledata.load_dir(tmp_path).version == "1.0.0"


def test_pdkprofile_serves_the_builtin_toml_profiles_and_holds_no_profile_data():
    sky, gf, ihp = profiledata.builtin()
    assert pdkprofile.SKY130 is sky.profiles["sky130"] and pdkprofile.GF180 is gf.profiles["gf180mcu"]
    assert pdkprofile.IHP is ihp.profiles["ihp-sg13g2"] and pdkprofile.IHP_CMOS5L is ihp.profiles["ihp-sg13cmos5l"]
    assert list(pdkprofile.PROFILES) == ["sky130", "gf180mcu", "ihp-sg13g2", "ihp-sg13cmos5l"]
    b = pdkprofile.for_pdk("gf180mcuB", "gf180mcu")
    assert b is pdkprofile.for_pdk("gf180mcuB", "gf180mcu") and b.stack is gf.profiles["gf180mcu"].stacks["gf180mcuB"]
    assert pdkprofile.for_pdk("gf180mcuD", "gf180mcu") is pdkprofile.GF180
    src = Path(pdkprofile.__file__).read_text()
    assert src.count("Profile(family=") == 1 and "Stack(" not in src.split("class Profile")[1]     # GENERIC only
    with pytest.raises(AttributeError):
        pdkprofile.SKY131


# ------------------------------------------------------------------ the tested commits (pcell, pixi.toml, setup.sh)


def _cli(*argv: str) -> tuple[int, str]:
    import contextlib
    import io
    from undine import __main__ as cli
    out, argv0 = io.StringIO(), sys.argv
    sys.argv = ["undine", "--pdk-root", "/nonexistent", *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.cli()
                code = 0
            except SystemExit as e:
                code = e.code or 0
    finally:
        sys.argv = argv0
    return code, out.getvalue()


def test_tested_commits_come_from_the_builtin_profiles():
    assert profiledata.tested_commits("sky130") == ("8afc8346a57fe1ab7934ba5a6056ea8b43078e71",)
    assert profiledata.tested_commits("gf180mcu") == ("792ef67037d74c8c51811eab3ce23cf793491296",)
    assert profiledata.tested_commits("ihp-sg13") == ("89d42ee6d2fd9b50b86a1aada6fee6673c3e6008",)
    assert profiledata.tested_commits("mystery") == () and profiledata.tested_commits(None) == ()
    from undine.pdks import pcell
    assert not hasattr(pcell, "SUPPORTED")


def test_pdk_tested_prints_each_familys_commit():
    for fam in ("sky130", "gf180mcu", "ihp-sg13"):
        assert _cli("pdk", "tested", fam) == (0, profiledata.tested_commits(fam)[0] + "\n")
    assert _cli("pdk", "tested", "sky130", "--all")[1].splitlines() == list(profiledata.tested_commits("sky130"))
    code, out = _cli("pdk", "tested", "mystery")
    assert code == 1 and "no built-in profile lists a tested commit of 'mystery'" in out and "ihp-sg13" in out


def test_pdk_enable_runs_ciel_enable_with_the_tested_commit(tmp_path, monkeypatch):
    log = tmp_path / "ciel.log"
    exe = tmp_path / "ciel"
    exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    exe.chmod(0o755)
    monkeypatch.setenv("UNDINE_CIEL", str(exe))
    assert _cli("pdk", "enable", "gf180mcu")[0] == 0
    assert _cli("pdk", "enable", "sky130", "0" * 40)[0] == 0
    # the global --pdk-root reaches ciel, as `pdk fetch`'s does (_cli passes /nonexistent)
    assert log.read_text().splitlines() == [
        "enable --pdk-root /nonexistent --pdk-family gf180mcu 792ef67037d74c8c51811eab3ce23cf793491296",
        "enable --pdk-root /nonexistent --pdk-family sky130 " + "0" * 40]
    code, out = _cli("pdk", "enable", "mystery")
    assert code == 1 and "name the commit" in out
    assert _cli("pdk", "enable", "sky130", "-rf")[0] != 0                  # never an option to ciel
    assert len(log.read_text().splitlines()) == 2


def test_setup_sh_and_pixi_name_no_commit_literal():
    import re
    for f in ("setup.sh", "pixi.toml"):
        assert not re.search(r"\b[0-9a-f]{40}\b", (REPO / f).read_text()), f
    assert tomllib.loads((REPO / "pixi.toml").read_text())["tasks"]["pdk"] == "python -m undine pdk enable sky130"
    assert 'pixi run python -m undine pdk tested "$PDK_FAMILY"' in (REPO / "setup.sh").read_text()


def test_pick_pdk_prefers_a_tested_commit_from_the_profile(monkeypatch):
    from undine.pdks import pcell
    from undine.pdks.pdk import Pdk
    from undine.store.project import pick_pdk
    good, other = profiledata.tested_commits("gf180mcu")[0], "0123456789abcdef0123456789abcdef01234567"
    pdks = {p.id: p for p in (Pdk("gf180mcuD", "gf180mcu", other, Path("/x")),
                              Pdk("gf180mcuD", "gf180mcu", good, Path("/y")))}
    monkeypatch.setattr(pcell, "macros_dir", lambda p: Path("/pcells"))
    assert pick_pdk(pdks, "gf180mcuD").version == good                    # GF180 has a tested commit too now


def test_ws_pdk_is_a_tested_gf180_commit():
    from undine.checks import tapeout
    assert tapeout.WS_PDK[1] in profiledata.tested_commits("gf180mcu")



# ------------------------------------------------------------------ regressions from the review


@pytest.mark.parametrize("field, value, fragment", [
    ("stdcells", [["sg13g2_\\w+", "../../../../tmp/evil"]], "is not a library name"),
    ("stdcells", [["sg13g2_(\\w+)", "\\3"]], "names a group its pattern"),
    ("stdcells", [["a{99999999999}", "x"]], "is not a regular expression"),
    ("corners", ["tt\n.control"], "control character"),
    ("corners", ["t t"], "is not a corner name"),
    ("model_lib", "a.lib tt\n.control", "control character"),
    ("model_lib", "a}x{y.lib", "brace"),
    ("model_lib", "a}; exec touch x; set z {x", "whitespace"),
    ("model_vars", [["X {}; exec x", "libs.tech"]], "is not a variable name"),
    ("osdi", ["../../evil.osdi"], "has a .. part"),
    ("osdi", ["dir/psp.osdi"], "is not a file name"),
])
def test_profile_strings_that_reach_a_netlist_or_tcl_are_refused(field, value, fragment):
    base = profiledata.builtin_for("sky130")
    doc = tomllib.loads((profiledata.BUILTIN / "profile_sky130" / profiledata.PROFILE_TOML).read_text())
    doc["profile"][0][field] = value
    with pytest.raises(profiledata.ProfileError, match=re.escape(fragment)):
        profiledata._Reader("test").document(doc, "profile_sky130", "1.0.0", "0.1.0")
    assert base is not None


def test_a_huge_number_or_deep_nesting_is_a_profile_error():
    doc = tomllib.loads((profiledata.BUILTIN / "profile_sky130" / profiledata.PROFILE_TOML).read_text())
    ring = next((k for k, v in doc["profile"][0].items() if isinstance(v, dict) and "width" in v), None)
    if ring:
        doc["profile"][0][ring]["width"] = 10 ** 400
        with pytest.raises(profiledata.ProfileError, match="too large"):
            profiledata._Reader("test").document(doc, "profile_sky130", "1.0.0", "0.1.0")
    with pytest.raises(profiledata.ProfileError, match="nested too deep"):
        profiledata.load("x = " + "[" * 100000 + "]" * 100000, "test", "profile_sky130", "1.0.0", "0.1.0")


def test_a_bare_pdk_name_keeps_the_version_the_lock_pins(tmp_path, monkeypatch):
    from undine.pdks.pdk import Pdk
    from undine.store import deps
    from undine.store.project import ProjectStore
    old = Pdk("gf180mcuD", "gf180mcu", "0123456789ab" + "0" * 28, tmp_path / "old")
    new = Pdk("gf180mcuD", "gf180mcu", "792ef67037d74c8c51811eab3ce23cf793491296", tmp_path / "new")
    pdks = {old.id: old, new.id: new}
    prj = ProjectStore(tmp_path / "projects", pdks).create("p", None)
    prj.meta["pdk"] = "gf180mcuD"
    prj.save()
    assert prj.pdk is new                                            # no lock: the tested version
    deps.write_lock(prj.path, {"format": 2, "tools": {}, "libraries": {},
                               "pdk": {"id": old.id, "variant": "gf180mcuD", "commit": old.version}})
    assert ProjectStore(tmp_path / "projects", pdks).get("p").pdk is old    # the pin, not the newly preferred one
