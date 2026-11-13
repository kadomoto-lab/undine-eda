"""The three-way merge primitives every view's merge is built from (vcs/merge3.py): one value, ordered keys, and objects
whose identity is their value merged region by region."""
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.vcs.merge3 import Merge, mapping, order, regions  # noqa: E402


def test_one_value():
    m = Merge()
    assert m.value("v", "v", 1, 1, 2) == 2 and m.value("v", "v", 1, 2, 1) == 2 and m.value("v", "v", 1, 3, 3) == 3
    assert m.conflicts == [] and m.combined == ["v"]
    assert m.value("v", "v", 1, 2, 3) == 2 and [(c.id, c.taken, c.resolved) for c in m.conflicts] == [("v", "ours", False)]
    m = Merge({"v": "theirs"})
    assert m.value("v", "v", 1, 2, 3) == 3 and m.conflicts[0].resolved and m.result(b"", "x").clean


def test_mapping_delete_modify_and_order():
    m = Merge()
    got = mapping(m, "k:", "key ", {"a": 1, "b": 2}, {"a": 1}, {"a": 1, "b": 3, "c": 4})
    assert got == {"a": 1, "c": 4} and [c.id for c in m.conflicts] == ["k:b"]
    assert mapping(Merge(), "k:", "", {"a": 1}, {"a": 1}, {"a": 1, "x": 0}) == {"a": 1, "x": 0}
    assert order(["A", "B"], ["B"], ["A", "X", "B"], {"B", "X"}) == ["X", "B"]   # no surviving predecessor: first
    assert order(["A"], ["A", "o"], ["A", "t"], {"A", "o", "t"}) == ["A", "t", "o"]


# contacts at a point, none touching another; a copy of one is any other contact
def merge(b, o, t, prefer=None):
    m = Merge(prefer)
    out = regions(m, "c:", "cell", "contact", Counter(b), Counter(o), Counter(t),
                  lambda changed: [],
                  lambda v: "contact", lambda v: v)
    return m, sorted(out.elements())


def test_regions_one_side_or_both_alike():
    assert merge([(0, 0)], [(0, 0), (5, 5)], [(0, 0), (9, 9)])[1] == [(0, 0), (5, 5), (9, 9)]
    m, got = merge([(0, 0), (9, 9)], [(9, 9)], [(9, 9)])                       # both removed it
    assert m.conflicts == [] and got == [(9, 9)]
    m, got = merge([(0, 0)], [(1, 0)], [(1, 0)])                               # both moved it alike
    assert m.conflicts == [] and got == [(1, 0)]


def test_regions_never_keep_two_versions_or_lose_a_deletion():
    m, got = merge([(0, 0)], [(1, 0)], [(0, 1)])                               # moved differently
    assert [c.id for c in m.conflicts] == ["c:(0, 0)"] and "both sides changed" in m.conflicts[0].what
    assert got == [(1, 0)]
    assert merge([(0, 0)], [(1, 0)], [(0, 1)], {"c:(0, 0)": "theirs"})[1] == [(0, 1)]
    for far in (1, 10**6):                                                     # moved however far vs removed
        m, got = merge([(0, 0)], [], [(far, 0)])
        assert "ours removed, theirs changed" in m.conflicts[0].what and got == []
        m, got = merge([(0, 0)], [(far, 0)], [])
        assert "ours changed, theirs removed" in m.conflicts[0].what and got == [(far, 0)]


def test_regions_pair_a_copy_with_the_nearest_removed_one():
    # both removed (0,0); ours also moved (100,0) a little: that copy goes with (100,0), not with (0,0)
    m, got = merge([(0, 0), (100, 0)], [(101, 0)], [(100, 0)])
    assert m.conflicts == [] and got == [(101, 0)]
    # one removed copy is taken by one added copy: the second one ours added is an addition of its own
    m, got = merge([(0, 0)], [(1, 0), (500, 0)], [])
    assert len(m.conflicts) == 1 and m.conflicts[0].ours["added"] == [[1, 0]]
    assert got == [(1, 0), (500, 0)]


def test_regions_pair_large_groups_on_a_grid():
    n = 3000
    b = [(i * 10, 0) for i in range(n)]
    m, got = merge(b, [(x + 1, y) for x, y in b], [(x, y + 1) for x, y in b])
    assert len(m.conflicts) == n and got == sorted((x + 1, y) for x, y in b)


def test_regions_pair_a_group_moved_far_with_its_translated_twins():
    # 5000 identical contacts on a grid, all moved by ours farther than they are spread: a ring growing until it
    # reaches them would test every pair (minutes, gigabytes) and pair the nearest ones, not the moved ones
    far = 10**6
    b = [(i % 71 * 400, i // 71 * 400) for i in range(5000)]
    t0 = time.monotonic()
    m, got = merge(b, [(x + far, y) for x, y in b], [(x, y + 1) for x, y in b])
    assert time.monotonic() - t0 < 10
    assert len(m.conflicts) == len(b) and got == sorted((x + far, y) for x, y in b)
    for c in m.conflicts:                                                      # each with its own twin
        [[x, y]] = c.base
        assert c.ours == {"removed": [[x, y]], "added": [[x + far, y]]}
        assert c.theirs == {"removed": [[x, y]], "added": [[x, y + 1]]}


def test_regions_report_who_did_what():
    m, _ = merge([], [(0, 0)], [(0, 0), (0, 0)])                               # a count both changed differently
    assert [c.id for c in m.conflicts] == ["c:(0, 0)"] and "both sides added" in m.conflicts[0].what


def test_regions_never_guess_a_pairing():
    # ours moves a contact array far and deletes a lone contact; theirs deletes the array's corner contact. No shift
    # lines the copies up exactly (the lone one is far from the rest): they become one region, so theirs' deletion is
    # a conflict, never undone in silence by a guessed pairing
    grid = [(i * 340, j * 340) for i in range(30) for j in range(30)]
    b = grid + [(-50000, -50000)]
    o = [(x + 50000, y) for x, y in grid]
    t = [v for v in b if v != (9860, 9860)]
    m, _ = merge(b, o, t)
    assert len(m.conflicts) == 1
