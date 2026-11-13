"""store/semver.py: strict SemVer 2.0.0 versions and the requirement forms phase 2 accepts (^, ~, =, bare, *)."""
import pytest

from undine.store.semver import Req, SemverError, Version


def V(s):
    return Version.parse(s)


def test_parse_accepts_strict_semver():
    assert V("1.2.0") == Version(1, 2, 0)
    v = V("1.0.0-rc.1+b5")
    assert (v.major, v.minor, v.patch, v.pre, v.build) == (1, 0, 0, ("rc", 1), "b5")
    assert str(v) == "1.0.0-rc.1+b5"
    assert str(V("0.0.0-0a.x-y")) == "0.0.0-0a.x-y"


@pytest.mark.parametrize("bad", ["1.2", "01.2.0", "1.02.0", "v1.2.0", "1.2.0-", "1.2.0+", "1.2.0-01", "1.2.0-a..b",
                                 " 1.2.0", "1.2.0.0", "", None, 1])
def test_parse_refuses_with_an_example(bad):
    with pytest.raises(SemverError, match=r"not semver: MAJOR\.MINOR\.PATCH, e\.g\. 1\.2\.0"):
        Version.parse(bad)


def test_precedence_follows_semver_section_11():
    chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
             "1.0.0-rc.1", "1.0.0", "2.0.0", "2.1.0", "2.1.1"]
    vs = [V(s) for s in chain]
    assert sorted(reversed(vs)) == vs
    for a, b in zip(vs, vs[1:]):
        assert a < b and b > a and a <= b and not a >= b
    assert V("1.0.0-2") < V("1.0.0-10") < V("1.0.0-a")       # numeric ids numerically, below alphanumeric ones


def test_build_metadata_is_ignored_in_precedence():
    assert V("1.0.0+a") == V("1.0.0+b") and hash(V("1.0.0+a")) == hash(V("1.0.0"))
    assert not V("1.0.0+z") < V("1.0.0+a")


@pytest.mark.parametrize("req, yes, no", [
    ("^1.2.3", ["1.2.3", "1.9.0", "1.2.10"], ["1.2.2", "2.0.0", "0.9.0"]),
    ("^1.2", ["1.2.0", "1.99.1"], ["1.1.9", "2.0.0"]),
    ("^1", ["1.0.0", "1.9.9"], ["0.9.9", "2.0.0"]),
    ("1.2", ["1.2.0", "1.5.0"], ["1.1.0", "2.0.0"]),                 # bare means caret
    ("1.2.3", ["1.2.3", "1.3.0"], ["1.2.2", "2.0.0"]),
    ("^0.3", ["0.3.0", "0.3.9"], ["0.2.9", "0.4.0", "1.0.0"]),
    ("^0.3.1", ["0.3.1", "0.3.5"], ["0.3.0", "0.4.0"]),
    ("^0.0.3", ["0.0.3"], ["0.0.4", "0.0.2"]),
    ("^0.0", ["0.0.0", "0.0.9"], ["0.1.0"]),
    ("^0", ["0.0.0", "0.9.9"], ["1.0.0"]),
    ("~1.2", ["1.2.0", "1.2.9"], ["1.3.0", "1.1.9"]),
    ("~1.2.3", ["1.2.3", "1.2.4"], ["1.2.2", "1.3.0"]),
    ("=1.2.3", ["1.2.3", "1.2.3+build"], ["1.2.4", "1.2.2"]),
    ("*", ["0.0.1", "1.0.0", "99.0.0"], ["1.0.0-rc.1"]),
])
def test_requirement_meanings(req, yes, no):
    r = Req.parse(req)
    assert str(r) == req
    assert [v for v in yes if not r.matches(V(v))] == []
    assert [v for v in no if r.matches(V(v))] == []


def test_a_pre_release_matches_only_a_requirement_naming_its_release_with_a_pre_release():
    assert not Req.parse("^1.2").matches(V("1.3.0-rc.1"))
    assert not Req.parse("^1.2.3").matches(V("1.2.4-rc.1"))
    r = Req.parse("^1.2.3-rc.1")
    assert r.matches(V("1.2.3-rc.1")) and r.matches(V("1.2.3-rc.2")) and r.matches(V("1.2.3")) and r.matches(V("1.4.0"))
    assert not r.matches(V("1.2.3-beta")) and not r.matches(V("1.2.4-rc.1")) and not r.matches(V("2.0.0-rc.1"))
    assert Req.parse("=1.2.3-rc.1").matches(V("1.2.3-rc.1")) and not Req.parse("=1.2.3-rc.1").matches(V("1.2.3"))


@pytest.mark.parametrize("bad", [">=1.2", "<2", "^1, <2", ">1.0.0", "<=1.0.0", "1.2, 1.3"])
def test_ranges_are_refused_with_the_phase_3_hint(bad):
    with pytest.raises(SemverError, match=r"version ranges come with the package index \(phase 3\): use \^, ~ or ="):
        Req.parse(bad)


@pytest.mark.parametrize("bad, msg", [("1", "a bare version needs MAJOR.MINOR"), ("~1", "~ needs MAJOR.MINOR"),
                                      ("^", "not a version requirement"), ("^01.2", "not a version requirement"),
                                      ("=1.2", "not semver"), ("v1.2", "not a version requirement"),
                                      ("latest", "not a version requirement"), (3, "not a version requirement")])
def test_other_malformed_requirements_say_what_to_write(bad, msg):
    with pytest.raises(SemverError, match=msg):
        Req.parse(bad)
