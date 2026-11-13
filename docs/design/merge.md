# Semantic diff and three-way merge (design)

Status: accepted design; implemented (merge driver, resolve, merge check, semantic diff, the History panel).

## Goal

A project kept in git can be branched and merged like software, with the merge done on the design instead of on bytes:
two people (or two agents) change different cells, or different objects of one cell, on two branches, and git combines
both changes into one design. What cannot be combined is reported object by object, and the cells whose combined
content was never checked on either branch are listed for DRC / LVS.

Merging is the point, more than diffing: diff says what changed, merge makes a new design out of independent changes.
Changesets already merge this way inside Undine (applied by object id onto a design that moved on meanwhile); this
design gives git branches the same rules.

Not a goal: a version-control system of its own (storage, transport and history stay git's), a merge of RTL (text,
merged by git's line merge), a merge driver that GitHub's web merge runs (it runs none: such a merge falls back to
"binary conflict", which only happens when both sides changed the same cell file).

## What makes it tractable

Undine stores one file per cell and view (`libs/<lib>/<cell>.sch`, `.sym`, `.oas`, `.netlist.yaml`, `.intent.yaml`).
Git compares and merges files, so two branches that change different cells never reach a merge driver: git takes each
changed file from the side that changed it. A driver only runs for a file both sides changed.

The second half is identity. A merge must know which object of `ours` and `theirs` is "the same" object of the base:

| View | Object | Identity |
|---|---|---|
| schematic, symbol | instance | its `name` (renames are paired by symbol and placement, like the operation log); an unnamed one: its value |
| | wire | its value: end points (either direction) and properties |
| | text | its value: string, position, rotation, sizes, properties |
| | drawing (lines, boxes, polygons, arcs of a layer) | the whole collection: pin boxes define the symbol's pin order |
| | K, G, V/S/E/F records | per attribute (K), whole block (the others) |
| layout | shape | its value: layer, kind, geometry, properties. A changed shape is a removed and an added one |
| | instance with an `undine_inst` name | the name |
| | instance without one | its value: child cell, transformation, array, properties |
| | meta information (PCell parameters, ...) | its name |
| netlist view | port, instance, code block, pending connection | name / `inst.pin` |
| intent | port, supply, item, view item | name / item id |
| operation log | record | position after the common part (logs are append-only) |

Layout shapes have no ids in GDS / OASIS, and Undine does not invent any: a shape *is* its geometry, as in the
operation log's layout effects. That makes the layout a multiset of shapes per layer, whose three-way merge is exact
where the sides edited different places:

    for every shape s:  n(s) = ours(s) + theirs(s) - base(s)   (when one side kept the base count, the other side's)

so edits to different shapes of one cell combine without any matching heuristics. Where both sides edited the same
place, the regions below decide.

## Rules

One rule for every value (an attribute, a placement, a shape count, a whole drawing collection):

| base | ours | theirs | result |
|---|---|---|---|
| b | b | t | t |
| b | o | b | o |
| b | x | x | x (both made the same change) |
| b | o | t | **conflict**: ours is kept, theirs is reported |

"Missing" is a value: an object added on one side is `missing → v`; deleted on one side, `v → missing`; deleted on one
side and changed on the other is a conflict (delete / modify).

Granularity is chosen so that a merged value is always one somebody wrote:

- An instance's placement (x, y, rotation, flip; for a layout instance the transformation and the array) is one value.
  Ours moving it right and theirs moving it up is a conflict, not a diagonal move nobody made.
- Instance properties merge per property: ours changing `W` and theirs changing `L` of the same transistor combine.
- A symbol's drawing collections are one value each (pin order); the K record merges per attribute.
- An intent item merges per field; `serves` as a set.

Order: an object keeps its position from `ours`; objects only `theirs` added are inserted after the nearest object
before them in `theirs` that is still in the result (at the start when there is none). One order is not cosmetic: the
order of a schematic's port instances (ipin, opin, iopin) is the subcircuit's pin order, so that sequence is one value of
its own: one side reordering, adding or removing ports wins; both changing it differently is a conflict (`ports`).

### Conflicts beyond identity: regions

Objects whose identity is their value (layout shapes, unnamed layout and schematic instances, schematic wires and texts) cannot be
modified, only replaced: an edit is a removed object and an added one. Two edits of one object are then one removed
object and two different replacements, and per-object counts alone would keep both (two versions of a moved contact, a
net reaching both sides' new end points, a deletion undone by the other side's move). So these objects merge by
**region**: the changed objects (removed or added by either side) are grouped, two of them in one region when

- they touch: layout shapes whose bounding boxes touch on one layer; wires with an end point on, or crossing, each
  other; instances or texts at the same position (a layout instance's children are ghost cells, so its position is all
  there is);
- or one is a copy a side added of one that side removed (same shape, same instance or text apart from its position, a
  wire of the same direction and length), paired with the nearest such removed one, each taken once: a move, however
  far.

Per region: changed by one side only, or by both alike, it is taken from that side; changed by both differently, it is
one conflict (`shape:<cell>:<layer>:<first removed shape>`, `inst:<cell>:<...>`, `wire:<first removed wire>`), decided
for the whole region: the side taken's version of every object in it, ours unless resolved to theirs. A conflict says
who did what ("both sides changed", "ours removed, theirs changed", "both sides added") and lists each side's removals
and additions there.

This is deliberately the conservative reading, the one git applies to lines: the same edit on both sides plus an
addition touching it on one side is a conflict (git conflicts on the same changed line plus a line added next to it on
one side), and so are shapes both sides added that touch each other (two nets drawn into each other). A copy a side drew
while removing an identical object is taken for its move. Additions elsewhere, and edits of different places, combine.

Named instances of one child cell (or one symbol) that only ours and only theirs added, moved or swapped to the same position
are a warning: likely one device placed twice.

### What a successful merge does not prove

A merge that combines without conflicts is not a correct design. DRC, LVS and specs decide that, as for a changeset.
`undine merge-check` lists the cells to check: a cell needs checking when the merged content of its hierarchy (its own
views and those of every cell it instantiates, in the layout and in the schematic) equals neither side's. That is exactly
the set of combinations nobody has checked: a cell taken unchanged from one side keeps that side's results, a cell above
a merged one does not.

## Per view

- **Schematic, symbol** (`.sch`, `.sym`): parsed with the Xschem model; the rules above on instances, wires, texts, the
  drawing collections, the K / G / V / S / E / F records. Written with the same writer as the editor. A file whose merged
  model equals one side's keeps that side's bytes.
- **Layout** (`.oas`, one cell plus its children as ghost cells): read with KLayout; shapes per layer by region;
  instances by name or value; meta information by name. Written like the layout store writes a cell file. The ghost
  cells are those of the merged instances.
- **Netlist view** (`.netlist.yaml`): derived from the schematic, and read again from the merged drawing the next time
  Undine reads it. Only `pending` connections exist nowhere else; they merge per pin, and a pin one side connects as
  pending while the other draws it to another net is a conflict (the drawing would silently win). A pin counts as drawn
  on a side when it has no pending connection there and its net is not a lone unnamed net (net<N> of that pin only), and
  base did not already have it drawn there. Other differences follow the drawing and are not reported as conflicts (one
  warning says so).
- **Intent** (`.intent.yaml`): ports and supplies per name, notes as text (line merge), items per field with `serves`
  as a set. An item's identity is its `uid`, made when it is added and never changed; its readable `id` (`c3`) is only
  its name. Ids are allocated in sequence, so both sides adding a `c3` is common: both items are kept, theirs renamed to
  the next free id, and since `serves` is merged as a set of identities, references follow whatever the ids become, in
  this merge and in later ones (the other branch still calling its item `c3` is matched by uid). A cherry-picked item
  keeps its uid, so it is never added twice. Items written before uids existed are matched by id, with the earlier
  heuristics (renumbering theirs' clashing addition, following a renumbering an earlier merge made). New ids continue
  after the highest one in use (a deleted id is not handed out again).
- **Operation log** (`history/<lib>.jsonl`): the records after the common part of both sides: ours', then theirs'
  renumbered (and their `of` references with them), then a `merge` record. A side's own operations are those after the
  part it shares with the base, so a cherry-pick brings in the picked commit's operations only. Undo / redo stacks stop
  at a `merge` record: an edit before the merge is not undone on top of the other side's edits.
  Layout edits are a journal in the log until the layout is saved to its cell files, and the merged cell files are the
  merge of both sides' saved files; so a `merge` record counts as a save. A side's unsaved journal stays pending only when
  the other side did not change the layout (the merged files are then that side's): the merge record lists it
  (`"replay": [seq ...]`) and Undine replays it when it opens the library. When both sides changed the layout and either
  has unsaved edits, the log is a conflict (`layout-journal`): save the layouts on each branch, commit, and merge again
  (resolving it drops the unsaved edits). The pre-commit hook `git-setup` installs refuses a commit whose staged logs
  hold unsaved layout edits, so this only happens with commits made around it.
- Everything else (RTL, `project.json`, `undine.lock`, changeset records) is left to git.
  The package design ([packages.md](packages.md#changesets-and-evidence), phase 4, not implemented) proposes a merge
  rule for `project.json` (`libraries`, `indexes`, `pdk`) and `undine.lock`: entry by entry, then resolved again,
  and an adopt refused when the result does not resolve.

## Git integration

`undine git-setup PROJECT` (already the way a project becomes a git repository) also registers the merge driver:

    .gitattributes   *.sch *.sym *.oas *.netlist.yaml *.intent.yaml history/*.jsonl  merge=undine
    .git/config      merge.undine.driver = <python> -P -m undine merge-driver %O %A %B %P
                     merge.undine.recursive = undine-base
                     merge.undine-base.driver = <python> -P -m undine merge-driver --virtual-base %O %A %B %P

Git runs the driver in the repository's top directory; `-P` keeps that directory off Python's module path, so a file
committed there (a `yaml.py`) never runs during a merge (Python 3.10, which has no `-P`, drops it in a `-c` prelude).
`git-setup` also installs a pre-commit hook (`undine precommit`) that refuses a commit whose staged operation logs hold
unsaved layout edits. All of this is local git configuration: run `git-setup` again in every clone.

A criss-cross merge (two branches that each merged the other) has several merge bases, which git first merges into a
virtual one with the driver `merge.undine.recursive` names. There a conflict takes the bases' own base's value: keeping
ours would make one branch's resolution the base, and the merge would take the other's without a word; against the
older value both resolutions are changes, and the merge reports their conflict. What merged cleanly is kept; a conflict
that has no base value to take (two instances added under one name, an unreadable file) gives the whole older base, as
git does for binary files.

The driver writes the merged file into `%A` and exits 0, or with conflicts writes the best result it has (ours kept for
each conflict) and exits 1, which makes git mark the file as conflicted. Either way it writes a report (conflicts with
both values, warnings, what was combined, the blobs it merged; or the error when the file could not be merged, in which
case it holds ours), under two names: `.git/undine-merge/<path>.<key>.json`, this merge's own (the key is a hash of the
base, ours and theirs blobs), and `.git/undine-merge/<path>.json`, the file's latest. A later merge of the file, which
the operation log has almost every time, so keeps the earlier merge's report. Reports are local to the clone that
merged.

Resolving: git keeps base, ours and theirs of a conflicted file in the index (stages 1, 2, 3). `undine resolve PATH
[--theirs ID ...] [--ours ID ...]` merges them again taking the named side for those conflicts, writes the file and
updates the report; `git add PATH` marks it resolved as usual.

`undine merge-check [DIR]` (the project directory; during a merge, cherry-pick or rebase, or for a merge commit with
`--commit C`): the conflicts still open, the warnings, and the cells to check (above), each with why: its own files were
merged; or they are one side's while cells below it are not as that side has them; or they and the cells below are
one side's but what it instantiates resolves to other cells. Its paths are from the repository's top, as git and
`undine resolve` take them. For a merge commit the cells to check are computed from the commit alone (in any clone);
warnings come from the reports, matched to the merge by their blobs, and a file merged without a report here (merged
in another clone, or by git's octopus strategy, which runs no driver) is listed as such. During a rebase or a
cherry-pick of several commits it covers the commit git stopped at; the earlier ones' warnings were printed when they
were merged.

Files change under a running Undine: git writes the merged files, and a checkout, pull or rebase replaces them. The
schematic, symbol, netlist and intent editors on the server read their file at every operation and keep nothing
between requests, so an edit applies to what git wrote. A library's layout is held in memory, with unsaved edits as a
journal in the log; it keeps a fingerprint of every cell file it read or saved (modification time, size, inode) and of
the log's journal, and compares them at most once a second and always before a save. Without unsaved edits it reads
the files again (and replays a journal git brought, as when the library is opened). With unsaved edits it does not
merge them into the files: the layout is marked stale (the History panel and the status' `stale` name it), and saving
is refused, since it would write the cells as they were over the files git wrote, until a reload drops the edits and
reads the files. The reload is recorded as a layout `save` record with `"args": {"reload": true}`: every reader of the
journal (the replay, the log's merge, the pre-commit hook) already stops at a layout save, so the dropped edits are
never replayed, and undo stops there too. Its own saves and edits never count as a change on disk, and a file touched
with the same content (a stash and pop) is no conflict.

`undine diff OLD NEW` (two files) and `undine diff --rev A [B]` (two commits of a project): the semantic diff, the same
identities as the merge. For layouts both a structural diff (shapes, instances, meta information) and a geometric one
(XOR of each layer's geometry of the cell's own shapes): replacing a polygon by two boxes that cover the same area is a
structural change and no geometric one. Instantiated cells are not flattened into the XOR (a cell file holds them as
ghost cells), so moving an instance is one instance change and no XOR area.

## Not covered (yet)

- Unsaved layout edits are not carried over a merge or checkout done while Undine has the layout open: they are
  dropped by the reload (the log keeps them). Save layouts before running git.
- A text the browser holds whole (an RTL file in its editor, the intent edited as YAML) is saved as it is held, with no
  check that the file is still the one it was read from: saved after git changed the file, it overwrites what git
  wrote.
- A merge started from the web UI (branches, conflict markers drawn on the layout) would use this engine; the History
  panel shows a merge's conflicts, the command to resolve them and the cells to check, and the merge is done with git.
- Cross-file consistency (a schematic instance renamed on one side, its layout instance on the other) is checked by LVS,
  not by the merge.
