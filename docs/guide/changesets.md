# Changesets

Reviewable, revertible changes.

An agent or a person proposes a change by opening a changeset: a git branch of the project, `undine/<project>/<id>`,
with its own working copy. The real design is untouched until a human applies it.

```sh
H='content-type: application/json'; A='X-Undine-Actor: agent:optimizer'
curl -XPOST $URL/api/p/demo/changesets -H "$H" -H "$A" -d '{"title":"Widen NMOS","reason":"faster falling edge"}'
#  -> {"id": "cs-4ce078", "preview": "demo~cs-4ce078", "git": {"branch": "undine/demo/cs-4ce078", ...}, "git_setup": false, ...}
# edit the working copy with the normal APIs, using the preview as the project name:
curl -XPOST $URL/api/p/demo~cs-4ce078/l/mylib/c/inv/schematic/edit -H "$H" -H "$A" \
     -d '{"op":"set_props","kind":"i","id":"M2","props":{"W":"1.5"},"reason":"wider pull-down"}'
curl -XPOST $URL/api/p/demo/changesets/cs-4ce078/verify -d '{}'   # needed DRC/LVS checks, baseline vs proposal
curl $URL/api/p/demo/changesets/cs-4ce078                         # what changed, by whom and why, plus checks
curl $URL/api/p/demo/changesets/cs-4ce078/diff                    # its files, from where it started to now
curl -XPOST $URL/api/p/demo/changesets/cs-4ce078/apply -d '{}'    # human: merge it into the real design (all-or-nothing)
curl -XPOST $URL/api/p/demo/changesets/cs-4ce078/revert -d '{}'   # later: take it back (refused if later edits conflict)
```

The UI has a **Changes** button (review, run checks, open the preview, adopt, discard, revert). Adopting merges the
proposal's branch into the design object by object (the [semantic merge](../design/merge.md) of a git merge of
Undine's files), so unrelated edits to the real design in the meantime do not collide; an object both sides changed
differently does, and then nothing is written. Layout edits are recorded too (portable effects, persistent undo, and unsaved edits are
re-applied after a restart); see `server/undine/store/oplog.py` and `server/undine/store/changeset.py`, and the design,
[changesets on git branches](../design/changeset-branches.md).

## A changeset is a git branch

- **The first changeset puts the project in git.** A changeset needs the project in a git repository. Opening one in a
  project that is in none does what **Set up git** in the [History panel](history.md) does (`git init`, the attributes,
  the drivers, the hook), then commits the project's files as they are as your branch's first commit (`Project
  <project>: its files when Undine set up its repository`; Undine's own output and the changeset records left out, as
  from the base below, and layout edits not saved yet in the log as they are, as the base has them), so that what you
  adopt later is committed on top of it; the panel says so, and so does `open_changeset` to an agent (`"git_setup":
  true`, `"first_commit"`). A repository with no commit yet that Undine did not just make (you ran `git init` yourself,
  or an older Undine set it up) gets no commit from opening: adopting there leaves the merge uncommitted, the review
  says the repository had no commit yet, and the message on adopting leads to **Make the first commit** in the History
  panel. A project inside a repository nobody set up for Undine (a home directory kept in git,
  for example) is refused, with what to do: set the repository up (**Set up** in the History panel, or
  `undine git-setup <project>`; in a projects root that is not the default one, the message gives the command with
  `--root` too) or move the project out of it; the New changeset dialog says so in the interface's language, naming
  the repository and the project. Undine writes no branch or configuration into a repository nobody set up for it.
- **Where it starts.** Opening makes a commit of the project's files as they are, the *base*: uncommitted edits and new
  files are in it; Undine's own output (`runs/`, `tapeout/`, `llm/`, `.undine/`) and the changeset records
  (`changesets/`) are not; layout edits not saved yet are not either (save first). Your branch, index and working tree
  are not touched.
- **Libraries a working copy cannot hold.** A worktree holds what git stores, and a symbolic link in it is followed
  from where the working copy is. So opening is refused when one of the project's own libraries has one of the things
  below at its folder, above it, inside it or where one of those links leads. The New changeset dialog names the
  library and the path and says what to do, in the interface's language (the HTTP answer is a 400 with code
  `unsupported_library`). This is checked before anything else, so a refused open does not put a project that is in
  no repository into git. Besides the fix given below, putting the files themselves in the project instead of the
  link, or making the library a [dependency](libraries.md#dependencies) (a `"git"` library in `project.json`), always
  works; then open the changeset again. A relative link to another place inside the project that git keeps is fine.
    - A symbolic link that leads out of the project, or nowhere: the working copy would hold the link and write
      through it into your real files, or find nothing.
    - A symbolic link written as an absolute path, even to a place inside the project, or a relative one that climbs
      out of the project on its way (`../../...`): from the working copy it leads to your real files, or elsewhere.
      Make it a relative link that stays inside the project (`ln -sr`).
    - A symbolic link, or what it leads to, that the project's repository does not keep (your `.gitignore` leaves it
      out, or it is Undine's own output such as `llm/`): the working copy would not have it. Let git keep it.
    - A git repository of its own, a clone or a submodule: git keeps only a pointer to it, and the working copy would
      hold the library empty. Make it a dependency, or move its `.git` out of the project.

  A library `project.json` gives as an absolute path (or `~/...`) is not in the project: the working copy reads it
  where it is, and there it is read-only, like a dependency, so a proposal never writes into it. Edit it from the
  project itself.
- **Where it is edited.** The branch `undine/<project>/<id>` starts at the base, and a git worktree of it,
  `<projects root>/.changesets/<project>/<id>/work`, is the working copy: the project `<project>~<id>`. The base is
  checked out next to it (`.../base`, the baseline `<project>~<id>~base` the checks compare with), read-only. The
  `.changesets/` folder ignores itself: Undine writes a `.gitignore` holding `*` into it when it has none (and leaves
  it there), so a repository the projects root is kept in, whichever it is, never shows the working copies or what
  is kept beside them in `git status`, and `git add -A` leaves them alone, also after the last working copy is gone.
  When the projects root is in the project's own repository, Undine also excludes the folder in `.git/info/exclude`
  while a working copy is there.
- **Its commits.** The working copy is committed onto the branch when the agent's turn or MCP session ends, before the
  checks run and before it is adopted or discarded (its open layouts saved first). Each commit says the title, who, the
  turn and the operations since the previous one. The branch is plain git: `git log undine/demo/cs-4ce078`.
- **In the review** the changeset shows its branch, the base it started from (`from 423b409`, the full commit in the
  tooltip) and its number of commits. The [History panel](history.md) of the preview names the branch and the base and
  leads to the review and to the project's history.
- **After it is closed** (adopted or discarded), the worktrees are removed and the branch is kept as
  `refs/undine/changesets/<project>/<id>`, so its commits stay for the evidence and a revert (`git log
  refs/undine/changesets/demo/cs-4ce078`). The review then shows that ref.

## Review screen

Choosing a changeset in the Changes list opens a **review**: the Agent and the cells on the left, the drawing in the
middle and the changeset on the right, all at once, so a proposal can be compared, discussed and decided without switching panels.

- **Changed objects** (*By object*). The operation log is grouped by object: `R1  value: 1k → 12k` with the reasons
  given, even when the object was edited several times (the first before and the last after are shown; an object added
  and removed again, or a value put back, disappears). The raw operations are under *Operation log*.
- **Changed files** (*By file*). The [semantic diff](history.md) of the branch, by cell and view as the History panel
  shows it: from the base to the working copy while the changeset is open, to its last commit once it is closed. It
  also shows what no operation recorded (a library created, a layout imported, a file changed outside Undine), so it is
  exactly what adopting merges. It is read again when the working copy moves on; the button next to it reads it again
  by hand. Its open buttons open a view in the working copy (in the design once adopted). While no operation is
  recorded and nothing is committed yet, the review reads it by itself, so a change only *By file* shows makes
  **Adopt** and **Run needed checks** available. Only one read of it runs at a time: the diff of a large imported layout
  can take seconds, and the review waits for it rather than starting over at each refresh.
- **Diff.** The third mode of the switch shows the proposal with what differs from the confirmed design drawn over it:
  added green, removed red, changed amber. In the schematic that is per instance (by id: moved, flipped, another symbol or
  other parameters count as changed; a removed instance is drawn dashed where it was) and per wire (by end points); in
  the layout it is the difference of the flattened geometry per layer (`GET .../layout/<cell>/diff/<other-project>`), so a
  replaced PCell shows only the area that really changed. Large differences are cut at 4000 polygons.
- **Follow-ups.** A layout change that follows a schematic change (*sync layout* / *generate*) hangs under the schematic
  change that caused it: `M2  W: 1 → 2` with, beneath it, `Follows in the layout: M2  nfet_w1 → nfet_w2`. The link is
  derived from the operation log, as the schematic edits of a device since the layout last followed it (`causes` on the
  operation in the API), so it also holds for older changesets. A layout change that no schematic edit explains, such as
  a hand-drawn wire, stays at the top level.
- **Confirmed / Proposal.** The switch shows the same open views from the real design or from the working copy, at the same position and scale (also after the agent edits the working copy).
  The diff and the confirmed side show the design as it is now, which may have moved on since the proposal was opened;
  *By file* compares with where the proposal started.
- **Click a changed object** to open its view and highlight it in the schematic and the layout (cross-probing). An added
  object switches to the proposal, a removed one to the confirmed design.
- **Adopt and Discard** (Revert, once adopted) are at the top of the column, above the Confirmed / Proposal switch, so
  the usual decision needs no scrolling. A line under them names any check that got worse with the change. Checks, their
  freshness and Run needed checks follow the changes further down. After Adopt or Discard the drawing returns to the real
  design; the column keeps showing the archived evidence.
- **Request changes** opens the proposal with the Agent's message box ready: say what to change and the agent continues
  the same proposal (below). It is offered for a changeset on a git branch, not for an [older one](#older-changesets).
  **Adopt** is the `apply` call of the HTTP API.

## Continuing a proposal with the agent

In a review (or any preview), the agent continues that changeset instead of opening a new one.
Before each turn the agent is told what was added to the changeset since its last turn and by whom: a change by a
person, such as a moved shape or a value typed in the preview, is reported as such, with its reason, and the agent is
instructed to keep it and work around it. A turn with no new changes says so. The agent's checks reuse results that
are still current (see below), so only the checks the edits affect run again. Conversations are kept with the real
project, so they survive applying or discarding the changeset. A closed changeset cannot be continued.

## What the agent can do

There is one agent mode: it answers questions, and when asked to change or create something it opens a changeset (or
continues the one in the review) and edits its working copy. It can create cells (`create_cell`: schematic, symbol, layout),
look up placeable symbols and their pins (`list_symbols`), place, parameterise, wire, move and delete, sync the layout,
draw shapes, and run DRC / LVS and simulations. It cannot adopt a changeset, and it cannot create projects or libraries,
change the PDK, add dependencies, trust a dependency's code, import GDS / OASIS or edit read-only libraries; asked for
one of these it says so and names where a person does it.

**Trust in a changeset.** A simulation, LVS or RTL run in a changeset's working copy is checked by the
[trust rule](libraries.md#trusting-a-dependencys-code) like one in the project: a dependency's cell with a `.control`
block or a file path outside the PDK is refused until a person trusts that dependency. The working copy counts the
project's trust only, as it is now, so trusting in the project (`lib trust`, or **Trust...** in the project's panels),
or taking trust back, reaches every open changeset at once. Trust is given in the project only: a changeset preview refuses it. The agent
reports such a refusal and asks you; it cannot trust anything itself.

A new cell is recorded as operations (`create_cell`; effect `v` for a schematic or symbol file, `c` for a layout cell), shown in
the review as added items (`+ schematic ota`, `+ cell ota`), created in the real project when the changeset is applied
(if a file of that name appeared there meanwhile, the two are merged object by object, and what both define
differently is a conflict), and kept as it is when the changeset is reverted. The
symbol is created after the schematic has its ports, since its pins come from them.

## Timeline and restoring a checkpoint

**Timeline** (next to *By object* in an open changeset) lists what was done in the proposal in order: grouped by the
agent request it belongs to (with the request's text) or by the person who did it, including undo, redo and earlier
restores. Steps no longer in effect are struck through.

**Restore to here** between two steps brings the working copy back to how it was at that point (or as opened, at the top).
It rebuilds that state from the log rather than undoing what is in effect, so something added before the point and undone
after it comes back. Cells created after the point are removed if they are unchanged and nothing that remains uses them.
It covers every library the proposal touches, all or nothing (on any failure the working copy's files and logs are put
back and nothing is recorded), and it is recorded as a `restore` step: the review, undo and apply follow the restored
state, and you can restore to a later point again. It is refused while the agent or an agent is writing. The
agent's next turn is told that the proposal was restored and what was taken back.

`GET /api/p/<project>/changesets/<id>/timeline`, `POST .../restore` with `{"to": <gseq>}`.

## Check results and freshness

The Changes panel compares the baseline and proposal for each changed cell that supports DRC or LVS. A result
includes both its outcome and its freshness: **current**, **outdated**, **freshness unknown**, or **not run**.
Outdated results keep their previous outcome, but are not shown as a current pass or used to label a regression
or fix. The panel refreshes while open, so an edit made in the preview by another client can mark a result outdated.
Each checker has a row of its own: DRC with the PDK's KLayout deck and, on sky130, with Magic (*DRC (Magic)*), LVS, and
the ERC taken from that LVS run, whose findings (a body tap butted against another net's source / drain) are listed
under it as warnings, not violations ([Verification](verification.md#the-checks-of-a-changeset)).

**Run needed checks** starts only checks without a current usable job: missing checks, outdated or unknown inputs,
and failed, cancelled or interrupted jobs. A completed job with current inputs is reused even if it reported DRC violations or
an LVS mismatch; fix the design before checking it again. Jobs already running on the same inputs are reused too.
Freshness uses the same design, PDK, rule-deck and tool input hashes as other verification jobs; see
[Job provenance](../reference/data.md#job-provenance).

Applying or discarding is blocked while jobs in the working copy or baseline are queued or running. This prevents
a result from being removed or partially archived while a tool is still writing it. A server restart keeps
simulations going (the next server takes them up: [Restarting the server](../reference/cli.md#restarting-the-server)),
and such a simulation blocks applying and discarding until it ends, as any running job does; its run is then archived
like any other. The other checks (DRC, LVS, extraction) that the former server left unfinished, and a simulation no server can finish,
appear as **interrupted** and can be retried with **Run needed checks**.

## Adopting: the merge

**Adopt** (`POST .../apply`) commits the working copy (the tip), archives the evidence (below), then merges what the
branch changed into the design:

1. **Each file the branch changed** (between the base and the tip) is merged three ways: as it was at the base, as it is
   in the design now, and as the proposal has it, with the view's merge (schematic, symbol, layout, netlist, intent;
   any other file by lines). A file the design did not change since takes the proposal's; one the design changed
   combines object by object: different objects both go in. The design's open layouts with unsaved edits in the
   libraries the merge touches are saved first (recorded), so the merge takes part in them.
2. **Symbol changes** made with their consumers (a symbol edit that rewired the schematics using it) also need each of
   those views in the design as it was at the base, and no schematic placed since that uses the symbol in a way its
   wiring would have to follow: a file merge cannot see either. A schematic the proposal itself added after changing the
   symbol (a new testbench, say) is already wired to the new pins and does not count.
3. **All or nothing.** Any conflict and nothing is written: the review lists each file with its conflicts, each with
   the value in the design now, in the proposal and when the proposal was opened, worded in the interface's language
   (the HTTP answer is a 409 with code `merge_conflict`, see the [HTTP API](../reference/api.md#changesets)). To adopt
   it anyway, make the two sides agree on those objects and adopt again: change them in the design (putting back the
   value it had when the proposal was opened lets the proposal's value through) or in the proposal (by hand in the
   preview, or **Request changes**). Otherwise discard it; its branch keeps it. An edit made in Undine while the merge
   is being made (an intent or RTL edit too) waits for it and is kept. A file something else changed meanwhile
   (another program writing it, a `git pull`) makes the adopt a conflict, with nothing written and that program's
   writing kept: the review says the file changed while it was being adopted, and that adopting again merges it with
   that edit. A file that cannot be read (an intent or netlist whose YAML is broken) is a conflict too, and the review
   shows why it cannot be read: fix the file, then adopt again.
4. **The log.** The design's operation log gets the changeset's records (tagged with it) and a `merge` record naming
   it. Undo stops there: an adopted changeset is taken back with **Revert**, not with undo.
5. **The commit.** When nothing else was uncommitted in the project before (only what the merge wrote differs from
   your last commit; the changeset records do not count), a merge commit is made on your branch, with HEAD and the
   proposal's tip as parents and the message `Apply <id>: <changeset title>` with the reason, who adopted it and the checks. The
   review and its toast name it, and the [History panel](history.md) shows it. Otherwise the merge stays in your
   working tree, uncommitted, and the review says why (the answer's `why`): the project had other uncommitted changes
   (`changes`: commit it with git when you are ready), the repository has no commit yet (`no_commit`: make the first
   commit in the History panel, which takes the merge too; later adopts are then committed), or the commit failed
   (`failed`: commit it with git).
   Files your `.gitignore` ignores and git does not track stay out of the commit (the operation log, if `history/` is
   ignored: the changeset's records still reach the design's log).
6. The worktrees are removed and the branch is kept (above). The open tabs that show a file the merge wrote read it
   again; the others are left as they are. A tab with text you have not saved (RTL, or an intent's YAML) keeps your
   text and shows a bar above it: copy what you need, then **Reload, discarding my text** to see the merged file (saving
   the old text over the merged file is refused). If you undo your typing back to the text it had, the tab reads the
   merged file by itself and the bar goes.

**Revert** (`POST .../revert`) is the same merge the other way round, from the adopted tip back to the base: objects the
changeset changed get their earlier values; later edits of the same objects are conflicts (nothing written, the review
lists them with the value now, before adopting and as adopted). Files the changeset added (a cell, a view, an intent)
are kept as they are, since what it made may be used elsewhere since; the answer lists them (`kept`). A library the
changeset made stays in `project.json` while it still has files, so the cells kept there, and what you added to it
since, stay part of the project. The log gets a
`merge` record naming the revert. It is committed (`Revert <id>: <changeset title>`, on top of HEAD) or left uncommitted as an
apply is, open tabs follow it as they follow an adopt, and it needs no working copy: the commits are kept.

**Discard** archives the evidence, removes the worktrees and keeps the branch.

**While it runs.** Adopting, discarding or reverting runs on the server and can take a while (saving the evidence of
gigabytes of runs, a large merge). Meanwhile the Changes list and the review show it on that changeset, with how far it
is (`adopting: saving evidence 3.1 / 25 GB`, then `merging`, `cleaning up`), and the **Changes** button in the top bar
says `adopting` and no longer counts it among the open proposals. One changeset is closed at a time: an **Adopt**
pressed on another meanwhile says that it waits for the first, and its review shows `waiting for "…" (adopting) to
finish` until its turn comes. Adopting the same changeset again while it is being adopted is refused (409 `busy`).

## If Undine stops in the middle

An adopt or a revert first writes `<project>/changesets/<id>/merging.json`: the files it is about to write, each with
a digest of how it was before and how it will be, and the subject of its commit. If Undine is stopped while it
writes, `undine serve` deals with it when it starts again, before it reads any schematic or answers any screen (and
prints what it did); so does the next read of the changeset.

- **Finished** when the whole merge was written: the changeset becomes adopted or reverted, with its commit (only a
  commit Undine made for this merge counts, never one of yours made meanwhile) or the merge left uncommitted.
- **Taken back** otherwise, keeping what you did since: each file the merge had already written gets its earlier
  version back, the operation log keeps the records added since, a file the merge added is removed, and a file it had
  not reached yet is left as it is. A file the merge wrote that was changed since is left as it is, and named. The
  changeset stays as it was (open, or adopted for a revert), and the review shows a note saying what happened; adopt
  (revert) it again, which merges what is missing. The note goes once that succeeds.

Either way the operation log gets the changeset's records once. Until that is done, adopting, discarding and
reverting it are refused.

The first time Undine reads a project's changesets after it starts, it also tidies what a stop left in the repository:
a changeset whose record says adopted or discarded but whose working copy or branch is still there is closed now, and
the branch and working copies of a changeset that has no record (Undine stopped while opening it) are put away: the
working copy is committed onto its branch, the worktrees are removed, and the branch is kept as
`refs/undine/changesets/<project>/<id>`. No branch is ever deleted. A working copy a closure kept on purpose (below) is
left alone.

## Evidence after apply or discard

Applying or discarding removes the working copy, but retains the proposal's changes, check results, input hashes and
job output files under `<project>/changesets/<id>/evidence/`. Jobs from both the working copy and baseline are
preserved, including earlier runs superseded by rechecking and any artifacts written by jobs that failed to launch. The
archive also contains the operation arguments, project settings and dependency locks, and names the base commit and the
tip that was reviewed; the design files and the full operation logs are in those commits, kept under the branch's ref.
The changeset's `evidence` field records the archive path, capture time, number of jobs and size.

The runs are not copied: each of their files in the evidence is a hard link to the run's own file, so saving
gigabytes of Monte Carlo waveforms writes none of them, and once the working copy is removed the evidence is the only
place they are. (Where the evidence folder is on another file system than the store's working copies, or links are
refused, the files are copied.) Each file is read once for its hash, and not at all when its run recorded its hash when
it finished and the file has not changed since. Undine never rewrites a finished run's file in place (a job's status is
written anew and renamed in), so nothing it does later changes the evidence through a link. A job's tools run in a
process group of their own, and cancelling a job or stopping it at its time limit stops everything it started (the
compiler a Verilator build runs, the tools of a LibreLane flow; also for a simulation a server took up after a
restart), and the changeset cannot be adopted or discarded
until they are gone, also when they outlive a job that has ended by itself (`cancel_job` stops them). A run that could still be written all the same is copied rather than linked: one whose job was
cancelled, stopped at its time limit or never finished, and one that some process works in or holds a file of open for
writing (Linux). Its copy is the run as it was then, and a write after the closure goes to the working copy's file,
which is removed, never to the evidence. If Undine is stopped while it saves the evidence, what it had saved
(`changesets/<id>/.evidence-*`) is removed when Undine next starts and reads the project's changesets.

**Its size, and pruning waveforms.** The review of a closed changeset says how large its evidence is and how much of
that is waveforms; the Changes list shows each closed changeset's size and the total. **Prune waveforms** in the review
removes the raw waveforms of its runs (`out.raw` and other `*.raw`, `*.vcd`, `*.fst`), after a confirmation that names
the size; it cannot be undone. Everything else stays: each run's `status.json` (its inputs, the hashes of its outputs,
its verdict), logs, netlists, results and measurements, and the manifest with every file's hash as captured, which
also notes what was pruned, when and by whom. The review then says the waveforms were pruned, and a check of the
evidence says such a file was pruned rather than missing. Only a person prunes: no agent tool does it, and the HTTP
call refuses an agent's actor. When the closure kept the working copy (a late edit, below), its runs are the same
files as the evidence's: the confirmation and the message afterwards say how much of the size is freed only once that
working copy is removed.

Closed changesets still show their changes and checks. Their results and freshness are a snapshot **at apply or
discard**, not a statement about the real project's current design. Reverting retains that original evidence.
Changesets closed before evidence archiving was introduced cannot recover files that were already deleted.

If an edit or a new job arrives after the evidence snapshot was captured, the changeset closes with that captured
snapshot: an apply merges the tip that was reviewed, and the late edit is committed onto the branch. The working copy is
retained for recovery: the Changes panel shows a warning and, while that working copy is there, **Open retained
preview** to inspect the late edit or job. Those later changes are not part of the archived snapshot or the applied
changes.

## Older changesets

Changesets opened before changesets were git branches were copies of the libraries (no branch in their record). Those
applied or discarded keep their evidence and are shown as before; reverting one is not offered any more. One still
open can be reviewed and discarded, but not continued (no **Request changes**); adopting it is refused, saying to open
the proposal again as a new changeset. Discarding it removes its copy; it has no branch.

## Changesets and other git branches

A changeset is a git branch that Undine opens, checks, reviews and merges for you, inside one Undine. Work that runs in
parallel for longer (two people, two clones) goes on git branches of your own, and git merges them with the same rules:
object by object, conflicts reported per object, and the cells whose merged content nobody checked listed for
DRC / LVS.

```bash
python -m undine git-setup demo          # once per clone: the diff and merge drivers
git merge feature                        # same-cell edits combine object by object
python -m undine merge-check             # open conflicts, warnings, cells to check
python -m undine resolve libs/mylib/inv.sch --theirs inst:M2:placement
```

A changeset's branch can be handled like any other: pushed, shared, or merged with git (`git merge
undine/demo/cs-4ce078`); a changeset merged that way is outside Undine's record of it, so discard it in Undine
afterwards. Undine does not list remote branches, and a changeset cannot be rebased in Undine yet.

Undine notices files a merge changed under it: an open layout without unsaved edits is read again, one with unsaved
edits refuses to save over the merged files until you reload it, and an RTL or intent save made from a text loaded
before the merge is refused. The [History panel](history.md) shows the branch, the changes and the merge. See
[Version control with git](../reference/data.md#version-control-with-git) and the design,
[semantic diff and merge](../design/merge.md).
