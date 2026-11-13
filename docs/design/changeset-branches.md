# Changesets on git branches (design)

Status: accepted design; implemented (`server/undine/vcs/branches.py`, `server/undine/store/changeset.py`, the review
in `web/src/panels/ChangesPanel.tsx`; [how it was built](#as-built) has what the text below leaves open). The user
guide is [Changesets](../guide/changesets.md).

## Goal

A changeset stays what people and agents see: a proposal with a title and a reason, edited in its own working copy by
one writer at a time, watched live, checked before and after, reviewed, then applied, discarded or reverted. What
changes is what it is made of. It was a copy of the project's libraries under `<root>/.changesets/<project>/<id>/
{work,base}`, applied by replaying recorded effects. Now it is git:

| | Before | Now |
|---|---|---|
| the proposal | a copy of the libraries | the branch `undine/<project>/<id>` |
| its working copy | `.changesets/<project>/<id>/work` | a git worktree of that branch (same place) |
| what it started from | a second copy (`base`) | the base commit, made from the project's files when it was opened |
| the baseline's checks | the `base` copy | a detached worktree of the base commit (`.../<id>/base`), read-only |
| review | the recorded operations | the recorded operations, and the semantic diff of the base against the working copy |
| apply | effects replayed by object id | the [semantic three-way merge](merge.md) of the base, the project now and the branch |
| revert | inverse effects | the same merge the other way round |
| afterwards | the record and the evidence | also the branch's commits, kept under `refs/undine/changesets/<project>/<id>` |

Unchanged: the project names `<project>~<id>` (working copy) and `<project>~<id>~base`, the operation log in the
working copy (who, why, turns, timeline, restoring a checkpoint, batches), the writer lease, the before / after checks
and their freshness, the evidence archived at apply or discard, the record `<project>/changesets/<id>.json`, the routes
and the agent tools.

Why: one mechanism for history, branches and merges; a proposal that is a branch can be pushed, shared and merged by
git as well as by Undine; and what combines object by object (the merge driver) is the same code for an apply and for
a branch merge.

## The repository

Changesets need the project in a git repository:

- not in any repository: opening the first changeset runs `git-setup` on it (git init, `.gitattributes`, `.gitignore`,
  the drivers, the hook), then makes the project's first commit on the user's branch (`branches.first_commit`): the
  project's files as they are, the tree built as a base's is (the same paths left out, the same library checks), from
  the entries the user's index has of the project (a file `.gitignore` ignores that the user added with `-f` is kept),
  no parent, author the opener, message `Project <project>: its files when Undine set up its repository`. The branch
  is made at it only while it is still unborn (a commit of the user's made meanwhile wins: the base starts from it,
  `"first_commit": null`), and every index entry of the project's directory is reset to it, their stat data refreshed
  (Undine's output the user had staged is untracked again), so `git status` shows nothing but what the tree leaves out;
  when another git holds the index longer than Undine waits (two seconds), the commit stands and the answer says to
  run `git reset` (`"first_commit_note"`). Without it the branch would stay unborn, and an apply makes no merge commit
  on an unborn branch: every apply would be left uncommitted. The answer says so (`"git_setup": true`,
  `"first_commit": <sha>`). The first commit is made before the base, which has it as its parent; a library refused
  then (below) takes the repository back like any refusal of the open. A log with unsaved layout edits goes into it as
  a journal, as the base has it: nobody is there to save them, and the opening does not save the person's layouts.
- in a repository with no commit yet that Undine did not just make (git init and `git-setup` by hand, or set up by an
  Undine before this): nothing is committed for it, and an apply is left uncommitted saying why (`"why": "no_commit"`);
  the History panel offers **Make the first commit** (`POST /api/p/{project}/git/first-commit`, by the person: no
  agent tool makes it; only in a repository set up for Undine), which commits the project's files as above. In a
  repository whose project is a subdirectory, only the project's directory is committed (the rest of the working tree
  and of the index stays as it is). **Set up** in the History panel makes the first commit too when it ran git init;
  `undine git-setup` itself commits nothing (it says when the repository has no commit yet). Both refuse while a log
  holds layout edits not saved to the cell files (409 `unsaved`, naming the layouts; Set up's answer says so in
  `"commit_refused"`), as the pre-commit hook refuses such a commit: the panel's button waits until they are saved.
- in a repository Undine's merge driver is not registered in (`merge.undine.driver` unset; e.g. a home directory kept
  in git): opening refuses, saying to run `undine git-setup <project>` (in a store that is not the command's default
  one, `undine --root <root> git-setup <project>`; **Set up** in the History panel, as its button reads there) or to
  move the project: a 400 with code `not_set_up`, the repository's top (`"top"`), the project (`"project"`) and the
  command line that sets it up (`"command"`), which the UI words in its own language. Undine does not write branches
  or configuration into a repository nobody set up for it.

A worktree holds what git stores, and git checks a symbolic link out as its text says, to be resolved from the
worktree's folder. So opening also refuses a project whose own library (a path in the project, in `project.json`; not
a `"git"` dependency) a worktree could not hold as the project has it. Each link is judged the way the worktree's copy
of it will resolve (the library's path walked one name at a time, each link's text read from the link's folder): at
the library's folder, above it, inside it, and along where a link leads. Refused:

- a symbolic link that leads out of the project, or nowhere (a loop too): the proposal would write through it into
  the real files, or find nothing;
- a symbolic link whose text is an absolute path, even to a place inside the project: from the worktree it leads to
  the project's files, not to the worktree's copy of them;
- a relative link whose text climbs out of the project (`../../...`), even to come back in: from the worktree it
  leads to other files, or nowhere;
- a link, or the folder it leads to, that the project's repository does not keep (`.gitignore` leaves it out, or it
  is Undine's own output such as `llm/`): the worktree would not have it;
- a folder that is a git repository of its own (a clone, or a submodule's `.git` file): git stores a gitlink, which a
  new worktree holds empty.

What the folder tells (all but what the repository keeps) needs no repository: it is checked first, before anything
is set up, so a refused open runs no `git init` and no `git-setup` (`branches.check_libraries`). What the repository
keeps is checked on the base's tree, before the base commit is made; when that refuses an open that had just made the
project a repository, the repository is taken back (`.git` removed, `.gitignore` and `.gitattributes` as they were),
so a refused open still leaves none. The refusal is a 400 with code
`unsupported_library`, the library (`"library"`), the link or the repository (`"path"`, from the project's directory),
the kind (`"kind"`: `link_out` out of the project, absolute or climbing out; `link_nowhere` to nothing or to what the
repository does not keep; `own_repository`) and the link's text (`"target"`; null for a repository), which the UI words
in its own language.

Copying the files into the worktree instead would make no file of the branch: an apply would not merge them, or would
write the copy over the user's link. What to do: make the link relative and inside the project (`ln -sr`), let the
repository keep what it leads to, put the files themselves in the project instead of the link, or make the library a
dependency (a `"git"` library in `project.json`); a library that is a repository of its own: make it a dependency, or
move its `.git` out of the project (its files then the project's repository's). A relative link that stays inside the
project, to what the repository keeps, is kept (it leads into the worktree's own copy); links and repositories
elsewhere in the project are stored as git stores them.

A library given by an absolute path in `project.json` (or `~/...`, or a path that climbs out of the project) is no
folder of the project and so none of the worktree: the working copy finds it where it is, the real files. There it is
read-only (`changeset.Working`), as a dependency is, so the proposal never writes into it; the project itself edits it
as before.

All git work of a project goes through one lock per repository (the temporary index, `commit-tree`, `update-ref`,
worktrees). Commits are made with `commit-tree` (no hooks, no signing, an explicit author: the actor, committer
Undine). Undine's own output and the changeset records never enter a changeset's commits: `runs/`, `tapeout/`, `llm/`,
`.undine/`, `*.pre-split` and `changesets/` are left out of every tree it builds.

## Opening

1. **Base commit.** The project's working tree as it is (uncommitted edits and untracked files included, the paths
   above excluded) becomes a commit: a temporary index, `write-tree`, `commit-tree` with HEAD as its parent (none on an
   unborn HEAD). The user's branch, index and working tree are untouched.
2. **Branch and worktrees.** `git worktree add -b undine/<project>/<id> .../<id>/work <base>` and
   `git worktree add --detach .../<id>/base <base>`. The preview project is `<worktree>/<prefix>` (the project's
   directory within its repository). Installed dependencies (`.undine/deps`, ignored) are hard-linked into both (their
   files are read-only, and replaced, never written in place: no disk is spent), copied read-only where a link is
   refused (another file system, `UNDINE_STORE_LINK=copy`); their install markers are copied. Library folders git does
   not track (empty ones) are made.
3. **The log.** The worktree's `history/<lib>.jsonl` is the project's log as it was: the changeset's own records are
   those after it (`base_seq`, per library, in the record). Numbering (gseq), the timeline, restore, the review's
   operations, undo and redo only see those; a layout's journal in the base (unsaved layout edits of the project) is
   not replayed in the working copy, which starts from the files, as the copy did. The gseq floor of batches lives next
   to the worktree (`.../<id>/gseq.floor`), never in it.
4. The baseline's libraries are read-only (no journal replay, no save, no new layout cell, no edit by any route; undo
   and redo do nothing).
5. The record gets `"libs": [...]` and `"git": {"branch", "base", "head" (the user's HEAD at open), "prefix",
   "base_seq": {lib: seq}, "tip": null, "ref": null, "commits": 0}`; `"base": {lib: {}}` (the libraries, no per-file
   digests: nothing reads them, and hashing every file made opening slow) stays for older readers.

## While it is open

Everything works on the worktree as it did on the copy. The branch gets a commit of the working copy (message: the
title, the actor, the turn, the operations since the last commit) when the writer lease ends, before checks run, and
before apply or discard, after the working copy's open layouts are saved. The commit is made from the worktree's files
with the same exclusions, so nothing Undine writes there by itself (runs, the floor) is committed.

## Review

The review shows the operations by cell, as before, and the semantic diff of the base against the working copy (the
History panel's rendering), which also shows what no operation recorded (a library created, a layout imported): that
is what apply merges. The baseline's checks run on the base worktree.

## Apply

1. The working copy is committed (the tip).
2. **Plan.** For every path that differs between the base and the tip: merge the base's file, the project's file now,
   and the tip's file with the view's merge (schematic, symbol, layout, netlist, intent; anything else by lines; the
   operation log is not merged as a file, see 5). The project's open layouts with unsaved edits take part as saved:
   they are saved first (recorded), as the old apply saved them.
3. **Checks across files.** A symbol transaction of the changeset (a symbol and the consumers it rewired) must find each
   member view of the project as it was at the base, and no consumer placed since that the transaction does not cover:
   otherwise a conflict, as before. A schematic the merge takes whole from the changeset's tip and that had no instance
   of the symbol at the base (one the changeset drew after its symbol change, such as a new testbench) holds only
   instances placed against the new pins and is not a consumer to follow; likewise, for a revert, one taken whole from
   the base with no instance of the symbol at the tip. An instance that was there before is still checked: an undo can
   bring it back with its old wiring.
4. **All or nothing.** Any conflict: nothing is written; the answer is a 409 with code `merge_conflict` and per file
   `{path, kind, conflicts: [{id, what, base, ours, theirs}]}` (paths from the project's directory). Otherwise every
   merged file is written (temporary files, renamed at the end, put back on a failure), as a compare-and-swap: a file
   changed in the project since the plan read it is the conflict `changed`, and nothing is written.
5. **The log.** The project's log gets the changeset's records (with `"changeset": <id>`) and a `merge` record naming the
   changeset, after which undo starts again: an applied changeset is taken back with **Revert**, not with undo.
6. **Commit.** If the user's branch has a commit, the user's index had nothing staged (Undine's own output and the
   changeset records aside) and the project's working tree was clean before (only what the merge wrote differs from
   HEAD), a merge commit is made on the user's branch:
   parents HEAD and the tip, message `Apply <id>: <changeset title>` with the reason, the actor and the checks; the index is
   reset to it. Otherwise the merge stays uncommitted in the working tree, and the answer says so and why
   (`"commit": null`, `"uncommitted": true`, `"why"`: `"no_commit"` the branch has no commit yet, `"changes"` the
   project had other uncommitted changes, `"failed"` the commit failed, or Undine stopped before it; a committed
   merge's `"why"` is null, an older record has none). The review words each why; for `no_commit` it leads to the
   History panel's first commit.
7. The evidence is archived as before (with the base and tip commits; its runs as hard links, see As built: Evidence),
   between 1 and 2, so the merge is of the tip
   that was archived; the branch moves to `refs/undine/changesets/<project>/<id>`; the worktrees are removed
   (`--force`, then `git worktree prune`). Open editors and layouts of the project read their files again.

## Revert

The same merge with the tip as the base and the base commit as the other side, for the paths the changeset changed:
later edits of the same objects are conflicts (nothing written). Files the changeset added are kept (a cell it made may
be placed elsewhere since), as revert kept created cells before, and so is the `project.json` entry of a library it
made while the library's folder still has files. The log is not merged: the project's log gets a `merge` record naming
the revert. Commit or not, as for apply.

## Discard

The evidence is archived; the branch moves to the kept ref; the worktrees are removed.

## Older changesets

Records of changesets applied or discarded before keep their evidence and are shown as before; reverting one of those
(by inverse effects) is not offered any more. A changeset still open in the old form (a copy, no `"git"` in its record)
can be reviewed and discarded (the review offers no **Request changes** for it); applying it says to open the proposal
again as a new changeset.

## Not covered (yet)

- A changeset whose apply conflicts cannot be rebased in Undine. The merge base stays the base commit, so resolving on
  its branch with git (merging the user's branch into it) does not help an apply: the resolution is a third value. What
  works: make the two sides agree on the conflicting objects (in the design: the value at the base lets the proposal's
  through; or in the working copy) and apply again, or discard it, or merge its branch with git outside Undine and
  discard it.
- A project that is a subdirectory of a large repository checks the whole repository out in each worktree (no sparse
  checkout yet).
- Pushing and pulling changeset branches is plain git; Undine does not list remote branches.

## As built

What the implementation decided where the text above leaves room:

- **The tip while open.** The branch's last commit, the number of commits and the last `gseq` committed are kept in
  `.../<id>/tip.json` while the changeset is open (the record has other writers that rewrite it from older copies: a
  check's rows, a batch putting back its snapshot); the record gets `tip`, `ref` and `commits` when it is closed.
- **When the working copy is committed.** At the end of an assistant turn, when an MCP session ends (DELETE /mcp), after
  a stop, and on the screens' `/live` poll (how a lapsed MCP lease is seen), besides before checks, apply and discard.
  Not while someone other than the current writer holds the lease, so a check started from the UI never commits in the
  middle of an agent's operation: the end of that lease does.
- **The logs.** Neither a merge by lines nor logmerge: each library the merge touches gets the project's log as it is,
  then (apply) the changeset's records after `base_seq`, tagged with it and renumbered (`merged_seq`, with `of` and
  `replay` following), then a `merge` record `{"changeset", "action": apply | revert}`. That gives a barrier naming the
  changeset in every case (logmerge's fast-forward gives none), also in a library whose files the merge writes but whose
  log the changeset did not change. The changeset's records are read from the tip's log; when the tip has none (the
  project's `.gitignore` leaves `history/` out of git), an apply reads them from the working copy's log, up to the
  `gseq` the tip was committed at (kept in `tip.json` also when a commit found nothing new), and a revert reads those
  its apply put in the project's log (`merged_seq`). The symbol checks, the layout-journal check and the log merge all
  work from those records, so an ignored `history/` skips none of them.
- **Reading the records.** The worktree's logs start with the project's whole history, so the records after the floor
  are cached per log file (keyed by its device, inode, size and time): a later read parses only the lines appended
  since (checked against the last bytes read), and a first one finds the floor by bisecting the lines. The activity
  poll, the review, the timeline and restore no longer parse the whole history on every call.
- **The stat cache.** The temporary index of a snapshot or of a commit of the worktree starts from a copy of the index
  of the same working tree (the user's for the base commit, the worktree's for its commits; copied with its time, for
  git's racy-file check), then `read-tree -m` onto the parent keeps the stat data of the entries that did not change:
  git reads only the files that changed since, not every file (a file over `core.bigFileThreshold` used to be
  compressed again at every commit). Entries the user marked assume-unchanged or skip-worktree are read anyway;
  without a usable index (none, or one with a conflict) every file is read. After each commit, also one that found
  nothing to commit, the refreshed temporary index becomes the worktree's own (under `index.lock`, best effort). The
  first commit after opening reads the files the checkout wrote in its last second once; the later ones read only what
  changed.
- **Worktrees inside a repository.** The store's `.changesets/` folder ignores itself, as `.pytest_cache` and
  virtual environments do: before a worktree is added, the nearest folder named `.changesets` above its path gets a
  `.gitignore` holding `*` when it has none (an existing one is left as it is; it is never removed). Every repository
  that contains the store's root then ignores the whole folder, whichever it is: the project's own, or one the store's
  root is kept in while the project is a repository of its own (a clone in the store), and also after the last
  worktree is gone (a `tip.json` a closure left). The worktrees, `tip.json`, `gseq.floor` and the snapshots of a batch
  or a restore stay out of the user's `git status` and `git add -A`. As a further guard, when the store's root is in
  the project's repository, one line in its `info/exclude` excludes the folder (`/<path>/.changesets/`), added before
  `git worktree add` and dropped once no worktree of the repository is left there (after a prune); a worktree
  elsewhere in the repository excludes only itself there.
- **A folder made a link or a file.** The base commit (and a commit of the worktree) takes a tracked folder that is
  now a symbolic link or a file as it is now: the temporary index drops the entries under a folder that is no longer
  a real directory before adding what is there (`git update-index` would refuse it). The user's index is not
  touched.
- **The proposal's unsaved layout edits** are saved before each commit. A journal of layout edits made before a restart,
  with no layout open, is opened (replayed) and saved too; a tip whose log still has one is the conflict
  `layout-journal`.
- **The project's unsaved layout edits** in the libraries the merge touches are saved (recorded as `system`, with the
  changeset) after every check passed, the plan made again on the saved files, and put back (files, log, the layout's
  memory) if the merge is then refused or writing it fails, as far as they are still what the save left (below).
- **Locks.** `changeset_lock` (apply and revert are serialized), then the project's editors (every schematic and symbol
  of its libraries, the intent and RTL editor of every such file in them and of those the merge adds, made when none
  is open, and the editors open), its layouts, `netlist._LOCK`, its logs, then the repository's git lock. An edit
  through Undine that arrives meanwhile (an intent or RTL edit too) waits for the merge and is kept.
- **Compare-and-swap.** Each file the plan writes must still have the digest it had when the plan read it. A file
  changed by something that takes none of those locks (another process, a script, `git pull`) is the conflict
  `changed` (409 `merge_conflict`, "changed in the project while the merge was being made; nothing was written: apply
  it again"); nothing is written, and the layouts saved for the merge are put back as far as they are still what the
  save left. The save records the digest of each cell file it left (`wrote`) and the log's size and digest after it
  (`saved`): a cell file goes back (or, one the save made, is removed) only while it still has that digest; the log
  goes back to its bytes before the save plus the lines appended after the saved bytes, or is left as it is (and
  reported) when it was rewritten. What the other writer wrote meanwhile (a record, a new cell file) is kept. The
  review words the conflict and its next step (adopt or revert again) in the UI's language.
- **An interrupted merge.** Before `write_plan`, `<project>/changesets/<id>/merging.json` records each file's digest
  before and after (a log's sizes too: `size`, the merged log's, and `ours_size`, the project's log it starts with),
  the subject of the merge's commit (`subject`) and how to close the merge. It is marked `written` right after
  `write_plan` returns, updated after the netlist sync (`paths`) and after the commit (`commit`), and removed once the
  record is saved `applied` (`reverted`). A server killed meanwhile leaves it. `undine serve` finishes such merges
  first, before the netlist watcher and the API start (`Changesets.finish_interrupted`, which sweeps every project
  with a `changesets/cs-*/merging.json` and prints what it finished or took back): the watcher would otherwise read a
  schematic the merge had not renamed in yet back into the netlist it had, and record that in the merged log. The
  next access of the changeset (loading it, or the sweep) does the same. A merge counts as finished only when
  `merging.json` says `written`, or has `paths`, or every file is exactly the merged one (a log: when it starts with
  the merged bytes, records appended since). Then the record is saved, the commit found or the merge marked
  uncommitted (its `why` from the HEAD and the cleanliness `merging.json` recorded: `no_commit`, `changes`, else
  `failed`), and the closure done. The commit is one on HEAD's first-parent line whose parents are the recorded
  HEAD (and, for an apply, the tip), whose committer is `Undine <undine@localhost>` and whose first message line is
  the recorded `subject` (in an older `merging.json`, starts with `Apply <id>: ` or `Revert <id>: `): a commit of the
  person's made since is not taken for it. Otherwise the merge is taken back: a file renamed in gets its `.old` file
  back; a log gets the project's bytes plus the records appended since (from `.old`, or from the merged log's own
  start when `.old` is gone); a file the merge added is removed; a file killed between its two renames is put back; a
  file whose `.new` is still beside it was never renamed in, so it is the person's and is kept; a netlist rewritten
  since is read from its schematic again; files renamed in and changed since are left as they are and named. The
  changeset stays as it was (open; applied, for a revert), its record gets the note in `interrupted`, also put in
  `cleanup_warning` so the review shows it, and `merging.json` is removed, so applying again merges the log once. A
  later successful apply, revert, discard or recovery removes the note. Leftover `.new` / `.old` files are removed
  either way. Apply, discard and revert are refused (400) while a `merging.json` is still to be finished (one a
  recovery could not finish nor take back).
- **The sweep.** Once per project and server, at the first access (the list, opening one, loading one; while another
  operation holds `changeset_lock`, a later access), and at `undine serve`'s start for every project with an
  interrupted merge: interrupted merges are finished as above; a closed record whose
  branch or worktrees are still there (a server killed after saving it) gets its closure now (a late edit committed
  and the working copy retained, or the worktrees removed and the branch kept); a branch `undine/<project>/<id>` or a
  worktree with no record (a server killed while opening it) has its working copy committed, its worktrees removed and
  its branch kept as `refs/undine/changesets/<project>/<id>`, never deleted. Records marked `retained` and old-form
  copies are left alone; then `git worktree prune`. Opening and closing in progress are marked busy, so the sweep and
  the recovery skip them: in this process, and across processes by an exclusive `flock` on
  `<root>/.changesets/<project>/<id>.lock`, held for the whole operation and released by the kernel when the process
  ends. A server started while the one before still finishes an apply (a restart) leaves that merge alone; a later
  access finishes it if the old server died first.
- **After writing.** The merged schematics' netlists are read again (`netlist.sync`, so the watcher records nothing),
  open layouts read their files (`check_disk(force=True)`), the editors' symbol caches are cleared. The merge commit has
  these paths too, so after a clean apply the index and the working tree are the commit's; paths git ignores and does
  not track (the logs, when `.gitignore` leaves `history/` out) stay out of it. In the UI, only the tabs that show a
  file the merge wrote (the answer's `paths`) read it again; a tab holding unsaved text (an RTL tab, an intent's YAML)
  keeps it, with a bar offering to reload and discard it; one that becomes clean meanwhile (its typing undone) reads
  the merged file again and drops the bar.
- **A merge commit only on a born HEAD.** On a branch with no commit yet the merge stays uncommitted
  (`"why": "no_commit"`): the project's first commit (made at once where Undine ran git init, else from the History
  panel) gives the branch one.
- **Revert** is a commit with HEAD as its only parent, `Revert <id>: <changeset title>`; the files it keeps are in
  `"reverted": {"kept"}`, and the members of a symbol transaction it keeps are not checked against the tip. The
  merge of `project.json` from the tip to the base would take out the entry of a library the changeset added, and
  with it the cells kept there and the work done in them since; so the revert keeps the entry, as the project has it,
  of each library the changeset added (in the tip's `project.json`, not in the base's) whose folder still has files
  (`_keep_libraries`). When the merged `project.json` then equals the project's, it is not written.
- **Evidence.** Manifest version 2: `"commits": {"base", "tip"}`, and `history/**` is no longer copied (the worktree's
  log is the whole project's, and it is in those commits, unless the project's `.gitignore` leaves `history/` out).
  The runs are archived as hard links to their files, not copies (a copy across file systems or where a link is
  refused; `project.json`, `undine.lock` and an older copy's logs are copied, being written in place): the evidence
  folder (`<project>/changesets/<id>/`) and the worktrees (`<store>/.changesets/`) are on one file system as a rule, a
  finished run's files are never rewritten in place (`Job.set` and the models note in a log write a new file and rename
  it in), and the worktrees go with the closure, so the evidence ends as the only name of its runs. A job's process
  leads a process group of its own (`sim.Process`, for every kind of job; a simulation a restarted server adopted,
  `sim.Foreign`, is that same group): a cancel or a time limit stops the whole group (SIGTERM, then SIGKILL), and a
  closure is refused while any of it is left (`sim.alive`; once the job has ended, by the group its status recorded,
  `sim.left`, which `cancel_job` stops) or while a simulation is still worked on by any server of
  the root (`Jobs.working`: running here or adopted, parked while another live server runs it, handed over, queued), since a cancelled
  Verilator build's make and g++ otherwise outlive verilator and append to the run's log. A run that may be written all
  the same is copied, not linked (`_copied_runs`): its job was cancelled, stopped at its time limit or never finished,
  or a process (any: one of a server gone, one that left its group) works in it or holds a file of it open for writing
  (`sim.writers`, from `/proc`). A copy is hashed as it is made. Each file is hashed once, by a few threads, and a run's
  output whose `[size, mtime_ns, inode]` is still the one its status recorded with its hash (`provenance.outputs`,
  `"stamps"`) not at all. Whether the proposal changed while it was saved, and again at the cleanup, is told by those
  stamps (`"sources"` in the manifest, kept for a cleanup a restart finishes), not by hashing every file again; a
  manifest from before them is compared by its hashes. The manifest stays version 2 (the new keys are additions:
  `"sources"`, `"linked"`, `"bytes"`, `"pruned"`). Measured on a synthetic changeset whose working copy had 3020
  files, 4.7 GB of runs (600 Monte Carlo runs with a 4 MiB `out.raw`, 4 with 512 MiB; dropped from the page cache
  first): the apply took 47 s, wrote 4.7 GB and read 18.7 GB (a copy and three hash passes) before; now 8.4 s, 0.8 MB
  written and the 4.7 GB read once for runs from before stamps, 2.2 s and 3 MB read for runs that recorded them. What
  a server killed while saving left staged (`changesets/<id>/.evidence-*`) is removed by the sweep, so that its links
  hold no runs' bytes unseen. A prune says how much it frees: a file the working copy a closure kept still links
  (`st_nlink` > 1) is freed with that copy (`"shared"`, `"raw_shared"`, `"freed"`).
- **Closing progress.** An apply, discard or revert says how far it is while it waits for the changesets' lock and while
  it runs (`live.Closings`, in memory: phase, the bytes of evidence saved of how many, counted as each file is hashed
  or, where it is copied, as it is copied), in `GET .../changesets`, `GET .../changesets/<id>` and the screens'
  `/live` poll; a screen hands what its poll sees to its Changes list and review, so that one already open follows a
  closure begun elsewhere. A second closure of the same changeset meanwhile is a 409 `busy` rather than a wait that
  ends in "not open".
- **Pruning evidence.** A person (no agent tool; the route refuses an `agent:` actor) can remove the waveforms
  (`*.raw`, `*.vcd`, `*.fst`) of a closed changeset's archived runs; the manifest keeps every hash and notes the prune
  (`"pruned"`, written before the files go), the record sums it (`evidence.pruned`), and the evidence check says such
  a file was pruned rather than missing.
- **Closing** (apply, discard) first adds the libraries the working copy made to the record's `libs`, while it is
  still open, so the closure compares them too and sees no false late edit.
- **Closure that sees a late edit** commits it onto the branch (nothing is lost) and keeps the worktrees, with the
  usual `cleanup_warning` and `"retained": true`, which the sweep leaves alone. The `retained` of a `GET` is true (and
  the review offers **Open retained preview**) only while the changeset is closed and its preview resolves: a branch
  that could not be kept after the worktrees were removed offers nothing to open.
- **What adopting merges.** While an open changeset has no recorded operation and no commit, the review reads its diff
  without the tab being chosen (again at each poll): a change only the diff shows (a cell, or any file but the logs)
  makes **Adopt** and **Run needed checks** available. One read per changeset at a time (a large layout's diff can
  take seconds): a poll never cancels the read in flight nor starts a second one; a result is shown if the review is
  still on that changeset and no later read's result was shown; the working copy moving on, or the button, during a
  read queues one more after it. Once the diff has shown a change, the polls stop reading it; the working copy moving
  on and the button still do.
- **Previews.** `Server.project` finds a preview from the record (`changeset.preview`); `~base` is a `Baseline`, all
  of whose libraries are read-only: a new layout cell is a 403, undo and redo do nothing (no record), and its logs
  refuse any write. `/git` of a preview answers `{"repo": false, "preview": true, "changeset", "branch", "base"}` with
  the preview's own `unsaved`, `stale` and `reads`. The changeset routes take a preview's name for its project.
- **Older records.** Review and discard only; the old replay code is gone (apply 400 with `OLD_FORM`, revert of one
  applied before 400, diff 400). The review offers no **Request changes** for one still open.
