"""store/resolve.py: the transitive walk over manifests, local names, cell disjointness and the lock's graph. Pure:
the trees are a dictionary here, read by a counting reader."""
import pytest

from undine.store import manifest as mf
from undine.store import resolve as rs
from undine.store.resolve import Node, Requirement, ResolveError
from undine.store.semver import Req

A = "https://example.org/a.git"
B = "https://example.org/b.git"
C = "https://example.org/c.git"


def _commit(n: int) -> str:
    """Reversed, so that the 12 characters a chain shows differ from one n to another (one-to-one)."""
    return f"{n:040x}"[::-1]


def _tree(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def _manifest(name: str, version: str, deps: str = "", pdk: str = "") -> mf.Manifest:
    text = f'[package]\nname = "{name}"\nversion = "{version}"\n'
    if pdk:
        text += f"[pdk]\n{pdk}\n"
    if deps:
        text += f"[dependencies]\n{deps}\n"
    return mf.parse(text.encode(), name)


def _dep(git: str, rev: str, **more) -> str:
    extra = "".join(f', {k} = "{v}"' for k, v in more.items())
    return f'{{ git = "{git}", rev = "{rev}"{extra} }}'


class Repos:
    """Trees by (git, rev, subdir); counts every read."""

    def __init__(self):
        self.trees: dict[tuple, dict] = {}
        self.reads: list[tuple] = []
        self.seen: list[tuple[Requirement, Node | None]] = []

    def put(self, git, rev, manifest=None, *, subdir=None, commit=1, tree=None, mode="include"):
        self.trees[(git, rev, subdir)] = {"commit": _commit(commit), "manifest": manifest,
                                          "tree": _tree(tree if tree is not None else commit), "mode": mode}

    def __call__(self, req: Requirement, parent: Node | None) -> Node:
        self.reads.append((req.git, req.rev, req.subdir))
        self.seen.append((req, parent))
        t = self.trees[(req.git, req.rev, req.subdir)]
        commit = req.commit or t["commit"]
        return Node(id="?", git=req.git, rev=req.rev, subdir=req.subdir, commit=commit, tree=t["tree"],
                    files_mode=t["mode"], manifest=t["manifest"])


def _direct(**reqs) -> dict:
    return {k: Requirement(k, None, *v) if isinstance(v, tuple) else v for k, v in reqs.items()}


def _problems(e: ResolveError, code: str) -> list[rs.Problem]:
    return [p for p in e.value.problems if p.code == code]


# ------------------------------------------------------------------ the walk


def test_a_diamond_reads_the_shared_package_once_and_makes_one_node():
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0", f"c = {_dep(C, 'v1')}"), commit=1)
    r.put(B, "v1", _manifest("pb", "1.0.0", f"c = {_dep(C, 'v1')}"), commit=2)
    r.put(C, "v1", _manifest("c", "1.0.0"), commit=3)
    res = rs.resolve("app", _direct(pa=(A, "v1"), pb=(B, "v1")), r)
    assert r.reads.count((C, "v1", None)) == 1
    assert set(res.nodes) == {"pa", "pb", "c"}
    assert res.names_of("c") == ["c"]
    assert res.deps["pa"] == {"c": "c"} and res.deps["pb"] == {"c": "c"}
    assert sorted(res.requirers("c")) == ["pa", "pb"]
    assert res.direct == {"pa": "pa", "pb": "pb"}
    assert sorted(res.chain_texts("c")) == [
        f"app -> pa 1.0.0 (v1 @ {_commit(1)[:12]}) -> c 1.0.0 (v1 @ {_commit(3)[:12]})",
        f"app -> pb 1.0.0 (v1 @ {_commit(2)[:12]}) -> c 1.0.0 (v1 @ {_commit(3)[:12]})"]


def test_conflicting_requirements_name_both_chains():
    r = Repos()
    r.put(A, "v1.2.0", _manifest("analog_ip", "1.2.0", f"sar_common = {_dep(C, 'v1.3.1')}"), commit=0x2299)
    r.put(C, "v1.3.1", _manifest("sar_common", "1.3.1"), commit=0x5b1c)
    r.put(C, "v1.0.0", _manifest("sar_common", "1.0.0"), commit=0x1000)
    with pytest.raises(ResolveError) as e:                       # direct against transitive
        rs.resolve("app", _direct(sar_common=(C, "v1.0.0"), analog_ip=(A, "v1.2.0")), r)
    [p] = e.value.problems
    assert p.code == "conflict"
    assert p.chains == [f"app -> sar_common 1.0.0 (v1.0.0 @ {_commit(0x1000)[:12]})",
                        f"app -> analog_ip 1.2.0 (v1.2.0 @ {_commit(0x2299)[:12]}) -> "
                        f"sar_common 1.3.1 (v1.3.1 @ {_commit(0x5b1c)[:12]})"]
    assert "align the revs" in p.message and p.chains[0] in p.message and p.chains[1] in p.message
    assert str(e.value).endswith("nothing was changed")

    r.put(B, "v2", _manifest("other", "2.0.0", f"sar_common = {_dep(C, 'v1.0.0')}"), commit=0xb)
    with pytest.raises(ResolveError) as e:                       # transitive against transitive
        rs.resolve("app", _direct(analog_ip=(A, "v1.2.0"), other=(B, "v2")), r)
    [p] = e.value.problems
    assert p.code == "conflict"
    assert p.chains[0].startswith("app -> analog_ip 1.2.0") and p.chains[0].endswith("sar_common 1.3.1 (v1.3.1 @ "
                                                                                     f"{_commit(0x5b1c)[:12]})")
    assert p.chains[1].startswith("app -> other 2.0.0") and p.chains[1].endswith(
        f"sar_common 1.0.0 (v1.0.0 @ {_commit(0x1000)[:12]})")


def test_one_package_from_two_urls_with_one_tree_is_one_node():
    r = Repos()
    mirror = "https://mirror.example.org/c.git"
    r.put(A, "v1", _manifest("pa", "1.0.0", f"c = {_dep(mirror, 'v1')}"), commit=1)
    r.put(C, "v1", _manifest("c", "1.0.0"), commit=3, tree=30)
    r.put(mirror, "v1", _manifest("c", "1.0.0"), commit=3, tree=30)
    res = rs.resolve("app", _direct(c=(C, "v1"), pa=(A, "v1")), r)
    assert set(res.nodes) == {"pa", "c"} and res.nodes["c"].git == C
    assert res.deps["pa"] == {"c": "c"}


def _fixed(manifest, git, rev, commit, tree, mode="all", id=None) -> Node:
    return Node(id=id or (manifest.name if manifest else "?"), git=git, rev=rev, commit=_commit(commit),
                tree=_tree(tree), files_mode=mode, manifest=manifest, fixed=True)


def test_a_phase_1_entry_and_an_include_requirement_at_one_commit_agree_and_the_locked_one_stands():
    r = Repos()
    sar = _manifest("sar_common", "1.3.1")
    full = _commit(0x5b1c)
    r.put(A, "v1.2.0", _manifest("analog_ip", "1.2.0", f"sar_common = {_dep(C, full)}"), commit=0x2299)
    r.put(C, full, sar, commit=0x5b1c, tree=0xbbbb, mode="include")      # the include set: another tree
    locked = _fixed(sar, C, "v1.3.1", 0x5b1c, 0xaaaa)                    # phase 1: every file
    res = rs.resolve("app", _direct(sar_common=(C, "v1.3.1"), analog_ip=(A, "v1.2.0")), r,
                     fixed={"sar_common": locked})
    assert res.nodes["sar_common"] is locked
    assert (C, "v1.3.1", None) not in r.reads                            # the fixed one is not read

    r.put(C, full, sar, commit=0x5b1d, tree=0xbbbb, mode="include")      # another commit
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(sar_common=(C, "v1.3.1"), analog_ip=(A, "v1.2.0")), r,
                   fixed={"sar_common": _fixed(sar, C, "v1.3.1", 0x5b1c, 0xaaaa)})
    assert [p.code for p in e.value.problems] == ["conflict"]


def test_same_package_rules():
    a = Node("p", git=C, commit=_commit(1), tree=_tree(1), files_mode="all")
    assert rs.same_package(a, Node("p", git="https://EXAMPLE.org/c/", commit=_commit(1), tree=_tree(2),
                                   files_mode="include"))
    assert not rs.same_package(a, Node("p", git=C, commit=_commit(1), tree=_tree(2), files_mode="all"))
    assert rs.same_package(a, Node("p", git=B, commit=_commit(9), tree=_tree(1), files_mode="all"))
    assert not rs.same_package(a, Node("p", git=C, commit=_commit(1), subdir="lib", tree=_tree(2),
                                       files_mode="include"))


def test_a_fixed_phase_1_node_with_a_manifest_is_known_by_its_manifest_name():
    r = Repos()
    sar = _manifest("sar_common", "1.3.1")
    r.put(A, "v1.2.0", _manifest("analog_ip", "1.2.0", f"sar_common = {_dep(C, 'v1.3.1')}"), commit=0x2299)
    locked = _fixed(sar, C, "v1.3.1", 0x5b1c, 0xaaaa, id="git:sar")      # keyed `sar` in a phase 1 lock

    def cells(res):                       # one package, so its comparator clashes with nothing
        packages = {rs.name_text(n): ({"comparator": "comparator"}, {}, (), n.manifest is not None)
                    for n in res.nodes.values() if n.package == "sar_common"}
        return rs.check_disjoint(packages, {}, {})

    res = rs.resolve("app", _direct(sar=(C, "v1.3.1"), analog_ip=(A, "v1.2.0")), r, fixed={"sar_common": locked},
                     cells=cells)
    assert set(res.nodes) == {"sar_common", "analog_ip"}
    assert res.direct == {"sar": "sar_common", "analog_ip": "analog_ip"}
    assert res.names_of("sar_common") == ["sar", "sar_common"]
    assert res.deps["analog_ip"] == {"sar_common": "sar_common"}
    assert r.reads == [(A, "v1.2.0", None)]


def test_an_alias_installs_one_package_under_both_names():
    r = Repos()
    r.put(A, "v1", _manifest("analog_ip", "1.2.0", f"bias = {_dep(B, 'v0.3.2', package='bias_gen')}"), commit=1)
    r.put(B, "v0.3.2", _manifest("bias_gen", "0.3.2"), commit=2)
    res = rs.resolve("app", _direct(analog_ip=(A, "v1")), r)
    assert res.names_of("bias_gen") == ["bias", "bias_gen"]
    assert res.deps["analog_ip"] == {"bias": "bias_gen"}
    assert res.requirers("bias_gen") == ["analog_ip"]


def test_one_local_name_for_two_packages_is_refused_naming_both_requirers():
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0", f"common = {_dep(C, 'v1', package='common_x')}"), commit=1)
    r.put(B, "v1", _manifest("pb", "1.0.0", f"common = {_dep(C, 'v2', package='common_y')}"), commit=2)
    r.put(C, "v1", _manifest("common_x", "1.0.0"), commit=3)
    r.put(C, "v2", _manifest("common_y", "1.0.0"), commit=4)
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(pa=(A, "v1"), pb=(B, "v1")), r)
    [p] = e.value.problems
    assert p.code == "local_name" and p.message.startswith("common: ")
    assert p.chains[0].startswith("app -> pa 1.0.0") and p.chains[0].endswith(f"common_x 1.0.0 (v1 @ {_commit(3)[:12]})")
    assert p.chains[1].startswith("app -> pb 1.0.0") and p.chains[1].endswith(f"common_y 1.0.0 (v2 @ {_commit(4)[:12]})")


def test_two_local_names_differing_only_in_case_are_one_name():
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0", f"ip = {_dep(C, 'v1', package='ip_x')}"), commit=1)
    r.put(B, "v1", _manifest("ip_y", "1.0.0"), commit=2)
    r.put(C, "v1", _manifest("ip_x", "1.0.0"), commit=3)
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(IP=(B, "v1"), pa=(A, "v1")), r)
    [p] = _problems(e, "local_name")
    assert p.message.startswith("IP and ip: ")


@pytest.mark.parametrize("name,what", [("mylib", "the project library mylib"),
                                       ("sky130_fd_pr", "the PDK library sky130_fd_pr"),
                                       ("devices", "Xschem's devices library"),
                                       ("xschem_sky130", "a directory of Xschem's library")])
def test_a_reserved_local_name_is_refused_naming_the_chain(name, what):
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0", f"{name} = {_dep(C, 'v1', package='other')}"), commit=1)
    r.put(C, "v1", _manifest("other", "1.0.0"), commit=3)
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(pa=(A, "v1")), r, reserved={name.casefold(): what})
    [p] = e.value.problems
    assert p.code == "reserved_name" and what in p.message
    assert p.chains == [f"app -> pa 1.0.0 (v1 @ {_commit(1)[:12]}) -> other 1.0.0 (v1 @ {_commit(3)[:12]})"]


def test_a_package_whose_own_name_is_reserved_is_refused():
    r = Repos()
    r.put(A, "v1", _manifest("devices", "1.0.0"), commit=1)
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(mydev=(A, "v1")), r, reserved={"devices": "Xschem's devices library"})
    [p] = e.value.problems
    assert p.code == "reserved_name" and "(its own name)" in p.message


def test_a_sibling_is_read_at_the_requirer_s_commit_with_a_root_relative_subdir():
    r = Repos()
    r.put(A, "main", _manifest("pa", "1.0.0", 'pb = { subdir = "b" }'), subdir="a", commit=7)
    r.put(A, "main", _manifest("pb", "1.0.0"), subdir="b", commit=99)        # main moved since: not used
    res = rs.resolve("app", _direct(pa=Requirement("pa", None, A, "main", "a")), r)
    req, parent = r.seen[-1]
    assert (req.git, req.rev, req.subdir, req.commit) == (A, "main", "b", _commit(7))
    assert parent is res.nodes["pa"]
    pb = res.nodes["pb"]
    assert (pb.git, pb.rev, pb.subdir, pb.commit) == (A, "main", "b", _commit(7))


def test_every_problem_of_a_requirement_is_collected_in_one_error():
    r = Repos()
    deps = "\n".join([
        f"wrong = {_dep(B, 'v1', package='bias_gen')}",
        f"old = {_dep(C, 'v1', version='^2.0')}",
        f"plain = {_dep(A, 'plain')}",
        'idx = "^1.2"',
        f"mine = {_dep('file:///home/me/x.git', 'v1')}",
    ])
    r.put(A, "v1", _manifest("pa", "1.0.0", deps), commit=1)
    r.put(B, "v1", _manifest("something_else", "1.0.0"), commit=2)
    r.put(C, "v1", _manifest("old", "1.4.0"), commit=3)
    r.put(A, "plain", None, commit=4)
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(pa=(A, "v1")), r)
    codes = sorted(p.code for p in e.value.problems)
    assert codes == ["index_dependency", "local_url", "no_manifest", "package_mismatch", "version"]
    [idx] = _problems(e, "index_dependency")
    assert 'idx = { git = "https://...", rev = "v1.2.0", version = "^1.2" }' in idx.message
    [ver] = _problems(e, "version")
    assert "old ^2.0" in ver.message and "1.4.0" in ver.message
    assert ("file:///home/me/x.git", "v1", None) not in r.reads           # refused before any read
    assert str(e.value).startswith("5 problems with the dependencies:\n- ")


def test_a_local_requirer_may_name_a_local_url():
    r = Repos()
    here = "/home/me/a"
    r.put(here, "v1", _manifest("pa", "1.0.0", f"pb = {_dep('/home/me/b', 'v1')}"), commit=1)
    r.put("/home/me/b", "v1", _manifest("pb", "1.0.0"), commit=2)
    assert set(rs.resolve("app", _direct(pa=(here, "v1")), r).nodes) == {"pa", "pb"}


def test_a_manifest_error_is_collected_with_its_chain():
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0", f"pb = {_dep(B, 'v1')}"), commit=1)

    def read(req, parent):
        if req.git == B:
            raise mf.ManifestError("pb: undine-package.toml: package.version: missing")
        return r(req, parent)

    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(pa=(A, "v1")), read)
    [p] = e.value.problems
    assert p.code == "manifest" and "app -> pa 1.0.0" in p.message


def test_a_cycle_is_walked_once():
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0", f"pb = {_dep(B, 'v1')}"), commit=1)
    r.put(B, "v1", _manifest("pb", "1.0.0", f"pa = {_dep(A, 'v2')}"), commit=2)
    res = rs.resolve("app", _direct(pa=(A, "v1")), r)
    assert res.deps == {"pa": {"pb": "pb"}, "pb": {"pa": "pa"}}
    assert (A, "v2", None) not in r.reads


def test_pdk_check_names_a_transitive_package_and_its_chain():
    r = Repos()
    pdk = 'family = "sky130"\nvariants = ["sky130B"]'
    r.put(A, "v1", _manifest("pa", "1.0.0", f"pb = {_dep(B, 'v1')}"), commit=1)
    r.put(B, "v1", _manifest("pb", "0.1.0", pdk=pdk), commit=2)
    asked = []

    def check(node):
        asked.append(node.id)
        return "variant sky130A not among sky130B" if node.manifest.pdk.variants == ("sky130B",) else None

    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(pa=(A, "v1")), r, pdk_check=check)
    assert asked == ["pb"]                                       # pa has no [pdk]
    [p] = e.value.problems
    assert p.code == "pdk" and "variant sky130A not among sky130B" in p.message
    assert p.chains == [f"app -> pa 1.0.0 (v1 @ {_commit(1)[:12]}) -> pb 0.1.0 (v1 @ {_commit(2)[:12]})"]


def test_a_plain_direct_dependency_is_git_local():
    r = Repos()
    r.put(A, "v1", None, commit=0xabc)
    res = rs.resolve("app", _direct(old_ip=(A, "v1")), r)
    assert res.direct == {"old_ip": "git:old_ip"} and res.names == {"old_ip": "git:old_ip"}
    assert rs.name_text(res.nodes["git:old_ip"]) == f"old_ip (git {_commit(0xabc)[:12]})"


def test_a_package_s_own_name_taken_by_a_plain_dependency_is_refused():
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0", f"sar_common = {_dep(C, 'v1')}"), commit=1)
    r.put(B, "v1", None, commit=2)
    r.put(C, "v1", _manifest("sar_common", "1.0.0"), commit=3)
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", _direct(sar_common=(B, "v1"), pa=(A, "v1")), r)
    [p] = e.value.problems
    assert p.code == "local_name"
    assert f"sar_common (git {_commit(2)[:12]})" in p.message and "sar_common 1.0.0" in p.message


def test_two_plain_dependencies_of_one_tree_stay_two():
    r = Repos()
    r.put(A, "v1", None, commit=1)
    res = rs.resolve("app", _direct(a=(A, "v1"), b=(A, "v1")), r)
    assert res.direct == {"a": "git:a", "b": "git:b"} and len(r.reads) == 1


def test_a_direct_package_requirement_is_checked():
    r = Repos()
    r.put(A, "v1", _manifest("pa", "1.0.0"), commit=1)
    with pytest.raises(ResolveError) as e:
        rs.resolve("app", {"x": Requirement("x", "pz", A, "v1", None, Req.parse("^1"))}, r)
    assert [p.code for p in e.value.problems] == ["package_mismatch"]
    res = rs.resolve("app", {"x": Requirement("x", "pa", A, "v1", None, Req.parse("^1.0"))}, r)
    assert res.names_of("pa") == ["x", "pa"]


# ------------------------------------------------------------------ cells


def test_cell_names_are_top_level_logic_and_layout_only_cells():
    logic, layout = rs.cell_names(["comparator.sch", "comparator.sym", "comparator.oas", "unit.oas", "top.sv",
                                   "sub/inner.sch", "nfet_01v8_3a4b5c.oas", "layout.oas", "README.md",
                                   "Gate%2B.oas", "undine-package.toml"])
    assert logic == {"comparator": "comparator", "top": "top"}
    assert layout == {"unit": "unit", "gate+": "Gate+"}


def _pkg(logic=(), layout=(), closure=(), manifest=True):
    return ({c.casefold(): c for c in logic}, {c.casefold(): c for c in layout}, set(closure), manifest)


def test_check_disjoint():
    def codes(packages, project=None, pdk=None):
        return [p.message for p in rs.check_disjoint(packages, project or {}, pdk or {})]

    assert codes({"analog_ip 1.2.0": _pkg(["comparator"]), "sar_common 1.3.1": _pkg(["comparator"])}) == \
        ["comparator: analog_ip 1.2.0 and sar_common 1.3.1"]
    assert codes({"analog_ip 1.2.0": _pkg(["comparator", "bandgap"])}) == []         # one package, any names
    assert codes({"analog_ip 1.2.0": _pkg(["comparator"]),
                  "top 0.1.0": _pkg(["top"], ["comparator"], ["analog_ip 1.2.0"])}) == []
    assert codes({"analog_ip 1.2.0": _pkg(["comparator"]),
                  "top 0.1.0": _pkg(["top"], ["comparator"], [])}) == ["comparator: analog_ip 1.2.0 and top 0.1.0"]
    assert codes({"analog_ip 1.2.0": _pkg(["comparator"])}, {"mylib": set()}) == []  # a layout copy: not given
    assert codes({"analog_ip 1.2.0": _pkg(["comparator"])}, {"mylib": {"comparator"}}) == \
        ["comparator: analog_ip 1.2.0 and project library mylib"]
    assert codes({"a (git 000000000001)": _pkg(["comparator"], manifest=False),
                  "b (git 000000000002)": _pkg(["comparator"], manifest=False)}, {"mylib": {"comparator"}},
                 {"sky130_fd_sc_hd": {"comparator"}}) == []
    assert codes({"analog_ip 1.2.0": _pkg(["Comparator"]), "sar_common 1.3.1": _pkg(["comparator"])}) == \
        ["Comparator and comparator: analog_ip 1.2.0 and sar_common 1.3.1"]
    assert codes({"analog_ip 1.2.0": _pkg(["inv"])}, pdk={"sky130_fd_sc_hd": {"inv"}}) == \
        ["inv: analog_ip 1.2.0 and PDK library sky130_fd_sc_hd"]
    assert codes({"analog_ip 1.2.0": _pkg([], ["sky130_fd_sc_hd__inv_1"])},
                 pdk={"sky130_fd_sc_hd": {"sky130_fd_sc_hd__inv_1"}}) == []           # a copy of a PDK cell
    assert codes({"a 1.0.0": _pkg([], ["unit"]), "b 1.0.0": _pkg([], ["unit"])}) == []  # two layout copies
    [p] = rs.check_disjoint({"analog_ip 1.2.0": _pkg(["x"]), "plain (git 1)": _pkg(["x"], manifest=False)}, {}, {})
    assert p.code == "cell_clash"


# ------------------------------------------------------------------ the lock's graph


def _lock():
    return {"format": 2, "libraries": {
        "analog_ip": {"git": A, "rev": "v1.2.0", "commit": _commit(1), "tree": _tree(1), "files": "include",
                      "package": "analog_ip", "version": "1.2.0", "names": ["analog_ip"],
                      "deps": {"sar_common": "sar_common", "bias": "bias_gen"}},
        "sar_common": {"git": C, "rev": "v1.3.1", "commit": _commit(2), "tree": _tree(2), "files": "include",
                       "package": "sar_common", "version": "1.3.1", "names": ["sar_common"], "deps": {}},
        "old_ip": {"git": B, "rev": "v1", "commit": _commit(3), "tree": _tree(3)}},
        "packages": {
        "bias_gen": {"package": "bias_gen", "version": "0.3.2", "git": B, "rev": "v0.3.2", "commit": _commit(4),
                     "tree": _tree(4), "files": "include", "names": ["bias", "bias_gen"], "deps": {}},
        "gone": {"package": "gone", "version": "1.0.0", "git": B, "rev": "v9", "commit": _commit(5),
                 "tree": _tree(5), "files": "include", "names": ["gone"], "deps": {}}}}


def test_lock_graph_finds_targets_in_packages_and_in_libraries():
    g = rs.lock_graph(_lock())
    assert g.direct == {"analog_ip": "analog_ip", "sar_common": "sar_common", "old_ip": "git:old_ip"}
    assert g.deps["analog_ip"] == {"sar_common": "sar_common", "bias": "bias_gen"}
    assert g.missing == {} and g.twice == []
    assert g.names_of("bias_gen") == ["bias", "bias_gen"] and g.names_of("git:old_ip") == ["old_ip"]
    assert g.label("bias_gen") == f"bias_gen 0.3.2 (v0.3.2 @ {_commit(4)[:12]})"
    assert g.label("git:old_ip") == f"old_ip (git {_commit(3)[:12]})"
    assert sorted(g.chains("sar_common")) == [["analog_ip", "sar_common"], ["sar_common"]]
    assert g.chain_text("app", ["analog_ip", "bias_gen"]) == \
        f"app -> analog_ip 1.2.0 (v1.2.0 @ {_commit(1)[:12]}) -> bias_gen 0.3.2 (v0.3.2 @ {_commit(4)[:12]})"


def test_reachable_from_the_direct_names():
    ids, names = rs.reachable(_lock(), ["analog_ip", "sar_common", "old_ip"])
    assert ids == {"analog_ip", "sar_common", "bias_gen", "git:old_ip"}             # `gone` is a leftover
    assert names == {"analog_ip", "sar_common", "bias", "bias_gen", "old_ip"}
    ids, names = rs.reachable(_lock(), ["sar_common"])
    assert ids == {"sar_common"} and names == {"sar_common"}
    assert rs.reachable({"format": 1, "libraries": {"x": {"git": A}}}, ["x"]) == ({"git:x"}, {"x"})
    assert rs.reachable(None, ["x"]) == (set(), set())


def test_incomplete_names_what_cannot_be_installed():
    assert rs.incomplete(_lock()) == []
    lock = _lock()
    lock["libraries"]["analog_ip"]["deps"]["cmp"] = "comparators"                    # a target missing
    lock["packages"]["bias_gen"]["names"] = ["bias", "bias_gen", "Old_IP", "../x"]   # a plain key, not a name
    lock["packages"]["gone"]["names"] = ["gone", "BIAS"]                             # claimed twice
    lock["libraries"]["sar_common"]["names"] = []                                    # deps but no names
    out = rs.incomplete(lock, {"gone": "the PDK library gone"})
    assert f"analog_ip 1.2.0 (v1.2.0 @ {_commit(1)[:12]}): needs comparators (as cmp), which undine.lock does not lock" \
        in out
    assert "bias_gen: the local name Old_IP is the plain git dependency old_ip" in out
    assert "bias_gen: '../x' is not a library name" in out
    assert any(x.startswith("BIAS: one local name for bias_gen 0.3.2") for x in out)
    assert "sar_common: has deps but no names" in out
    assert "gone: the local name gone is the PDK library gone" in out


def test_incomplete_reports_a_phase_1_entry_whose_manifest_has_dependencies():
    lock = {"format": 2, "libraries": {"ip": {"git": A, "rev": "v1", "commit": _commit(1), "tree": _tree(1)}}}
    m = _manifest("analog_ip", "1.2.0", f"sar_common = {_dep(C, 'v1')}")
    assert rs.incomplete(lock, {}, {"ip": m}) == [
        "ip: its undine-package.toml names dependencies (sar_common) that undine.lock does not lock"]
    assert rs.incomplete(lock, {}, {"ip": _manifest("analog_ip", "1.2.0")}) == []


def test_a_package_locked_twice_is_incomplete():
    lock = _lock()
    lock["packages"]["sar_common"] = dict(lock["libraries"]["sar_common"])
    assert "sar_common: locked twice, in packages and in libraries" in rs.incomplete(lock)
