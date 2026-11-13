"""pdks/compat.py: whether a package's [pdk] requirement accepts a project's PDK, and the reason when it does not.

The requirement here is a stand-in with manifest.PdkReq's fields, so these tests need nothing but compat; compat reads
only those fields. Nothing reads the disk: the fake PDKs' roots are never opened."""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.pdks import compat  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402

A, B = "a" * 40, "b" * 40
SKY = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"


@dataclass
class Req:
    family: str
    variants: tuple
    commits: tuple = ()
    compat: tuple = ()
    built_with: str | None = None


FAKE_A = Pdk("fakeA", "fake", A, Path("/pdks/fake/a"))
PLAIN = Pdk("fakeA", "fake", "", Path("/pdks/plain/fakeA"))


def _t(installed=FAKE_A, lock=None):
    return compat.project_target(lock, installed)


# ------------------------------------------------------------------ project_target


def test_project_target_from_each_source():
    pin = {"id": "fakeA@aaaaaaaaaaaa", "family": "fake", "variant": "fakeA", "commit": A, "tree": {}}
    t = compat.project_target(pin, FAKE_A)
    assert (t.family, t.variant, t.commit, t.prefix, t.id, t.root) == ("fake", "fakeA", A, None, "fakeA@aaaaaaaaaaaa",
                                                                      "/pdks/fake/a")
    # the pin wins over the install, and the root of another variant's install is not the pin's
    t = compat.project_target(pin, Pdk("fakeB", "fake", B, Path("/pdks/fake/b")))
    assert (t.variant, t.commit, t.root) == ("fakeA", A, None)
    t = compat.project_target(None, FAKE_A)
    assert (t.family, t.variant, t.commit, t.root) == ("fake", "fakeA", A, "/pdks/fake/a")
    t = compat.project_target("fakeA@aaaaaaaaaaaa", FAKE_A)
    assert t.commit == A and t.prefix is None                        # the install over a format 1 id
    t = compat.project_target("sky130A@8afc8346a57f", None)
    assert (t.family, t.variant, t.commit, t.prefix, t.id) == ("sky130", "sky130A", None, "8afc8346a57f",
                                                               "sky130A@8afc8346a57f")
    t = compat.project_target({"id": "sky130A@8afc8346a57f"}, None)  # deps.lock_pdk of a format 1 lock
    assert t.prefix == "8afc8346a57f"
    t = compat.project_target("sky130A", None)
    assert (t.variant, t.commit, t.prefix) == ("sky130A", None, None)
    assert compat.project_target(None, None) is None
    assert compat.project_target("", None) is None
    t = compat.project_target(None, PLAIN)
    assert t.commit is None and t.id == "fakeA"


def test_tag_commits_knows_no_tag_before_profile_packages():
    assert compat.tag_commits("sky130", "2025") is None


# ------------------------------------------------------------------ check


def test_family_variant_and_commit_accepted_and_each_refusal():
    req = Req("fake", ("fakeA",), commits=(A,), built_with=A)
    assert compat.check(req, _t()) is None
    assert compat.check(Req("sky130", ("sky130A",), commits=(A,)), _t()) == "family sky130, this project is fake"
    assert compat.check(Req("fake", ("fakeB", "fakeC"), commits=(A,)), _t()) == "variant fakeA not among fakeB, fakeC"
    why = compat.check(Req("fake", ("fakeA",), commits=(B,)), _t())
    assert why == "fakeA@aaaaaaaaaaaa is not among commits bbbbbbbbbbbb"
    assert "tag" not in why


def test_family_is_compared_through_family_of_not_the_install_directory():
    # an install whose directory family disagrees: family_of(variant) decides
    odd = Pdk("fakeA", "elsewhere", A, Path("/x"))
    assert compat.check(Req("fake", ("fakeA",)), compat.project_target(None, odd)) is None


def test_a_format_1_prefix_matches_a_full_commit():
    t = compat.project_target("sky130A@8afc8346a57f", None)
    assert compat.check(Req("sky130", ("sky130A",), commits=(SKY,)), t) is None
    why = compat.check(Req("sky130", ("sky130A",), commits=(A,)), t)
    assert why == "sky130A@8afc8346a57f is not among commits aaaaaaaaaaaa"


def test_compat_only_is_refused_with_the_phase_6_hint():
    req = Req("fake", ("fakeA",), compat=("fake/2025",), built_with=A)
    why = compat.check(req, _t())
    assert why.startswith("fakeA@aaaaaaaaaaaa is not among commits (none); tag fake/2025 is not known to this Undine "
                          "(tags come with PDK profiles, phase 6): list the commit under `commits`")


def test_commits_plus_an_unknown_tag_accept_through_commits():
    req = Req("fake", ("fakeA",), commits=(A,), compat=("fake/2025",), built_with=A)
    assert compat.check(req, _t()) is None
    why = compat.check(Req("fake", ("fakeA",), commits=(B,), compat=("fake/2025",)), _t())
    assert why == "fakeA@aaaaaaaaaaaa is not among commits bbbbbbbbbbbb"     # commits named: no tag hint


def test_a_known_tag_adds_its_commits(monkeypatch):
    monkeypatch.setattr(compat, "tag_commits", lambda family, tag: {A} if (family, tag) == ("fake", "2025") else None)
    assert compat.check(Req("fake", ("fakeA",), compat=("fake/2025",)), _t()) is None
    assert compat.check(Req("fake", ("fakeA",), compat=("fake/2026",)), _t()).endswith("under `commits`")


def test_built_with_alone_is_the_accepted_set():
    assert compat.check(Req("fake", ("fakeA",), built_with=A), _t()) is None
    why = compat.check(Req("fake", ("fakeA",), built_with=B), _t())
    assert why == "fakeA@aaaaaaaaaaaa is not among commits bbbbbbbbbbbb"


def test_none_of_the_three_checks_family_and_variant_only():
    assert compat.check(Req("fake", ("fakeA",)), _t()) is None
    assert compat.check(Req("fake", ("fakeA",)), _t(PLAIN)) is None   # no commit needed
    assert compat.check(Req("fake", ("fakeB",)), _t()) == "variant fakeA not among fakeB"


def test_a_pdk_without_a_version_and_no_pdk_are_refused():
    why = compat.check(Req("fake", ("fakeA",), commits=(A,)), _t(PLAIN))
    assert why == ("the PDK at /pdks/plain/fakeA has no version, so its commit cannot be checked: install it with "
                   "pdk fetch")
    # a format 2 pin of a plain directory with no install here: named by its id
    pin = {"id": "fakeA", "family": "fake", "variant": "fakeA", "commit": None}
    why = compat.check(Req("fake", ("fakeA",), commits=(A,)), compat.project_target(pin, None))
    assert why.startswith("the PDK fakeA has no version")
    assert compat.check(Req("fake", ("fakeA",), commits=(A,)), None) == "this project has no PDK"
    assert compat.check(Req("fake", ("fakeA",)), None) == "this project has no PDK"


def test_ihp_family_accepts_its_variant():
    ihp = Pdk("ihp-sg13g2", "ihp-sg13", A, Path("/pdks/ihp"))
    assert compat.check(Req("ihp-sg13", ("ihp-sg13g2",), commits=(A,)), _t(ihp)) is None
    assert compat.check(Req("ihp-sg13", ("ihp-sg13cmos5l",)), _t(ihp)) == ("variant ihp-sg13g2 not among "
                                                                           "ihp-sg13cmos5l")


@pytest.mark.parametrize("bad", [None, Req("sky130", ("sky130A",))])
def test_check_reasons_come_in_order(bad):
    # no PDK first, then family before variant
    t = None if bad is None else _t()
    req = Req("sky130", ("sky130B",), commits=(B,))
    assert compat.check(req, t) == ("this project has no PDK" if t is None else "family sky130, this project is fake")


# ------------------------------------------------------------------ note and describe


def test_note_names_the_commit_drawn_on_when_used_at_another():
    req = Req("fake", ("fakeA",), commits=(A, B), built_with=B)
    assert compat.note(req, _t()) == "drawn on bbbbbbbbbbbb, used at aaaaaaaaaaaa"
    assert compat.note(Req("fake", ("fakeA",), commits=(A,), built_with=A), _t()) is None
    assert compat.note(Req("fake", ("fakeA",), commits=(B,), built_with=B), _t()) is None     # refused: no note
    assert compat.note(Req("fake", ("fakeA",)), _t()) is None
    t = compat.project_target("sky130A@8afc8346a57f", None)
    assert compat.note(Req("sky130", ("sky130A",), commits=(SKY, A), built_with=SKY), t) is None
    assert compat.note(Req("sky130", ("sky130A",), commits=(SKY, A), built_with=A), t) == ("drawn on aaaaaaaaaaaa, "
                                                                                           "used at 8afc8346a57f")


def test_describe():
    req = Req("sky130", ("sky130A",), commits=(SKY,), compat=("sky130/2025",), built_with=SKY)
    assert compat.describe(req) == "sky130 [sky130A] commits 8afc8346a57f; tags sky130/2025; built with 8afc8346a57f"
    assert compat.describe(Req("sky130", ("sky130A", "sky130B"))) == "sky130 [sky130A, sky130B]"
