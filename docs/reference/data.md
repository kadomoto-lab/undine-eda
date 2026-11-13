# Data layout and operation log

## Data layout (server side)

```
~/undine-projects/                          projects root ($UNDINE_ROOT)
    <project>/project.json                  name, format, pinned PDK, libraries (own and dependencies), symbol paths,
                                            the dependency versions trusted (below)
    <project>/undine.lock                   the commits of the dependencies, PDK and tool versions (see Libraries)
    <project>/.undine/deps/<local name>/    dependencies as installed from undine.lock, one directory per local name:
                                            read-only hard links into the store (below), and .undine-dep.json, what
                                            was installed (below)
    <project>/libs/<lib>/
        <cell>.sch, <cell>.sym              Xschem format (Xschem can open the same directory)
        <cell>.netlist.yaml                 the cell's netlist: its connectivity (design/netlist.md)
        <cell>.oas                          the cell's layout (OASIS), one file per cell
    <project>/history/<lib>.jsonl           operation log (who, what, why) for schematic and layout edits
    <project>/changesets/<id>.json          changeset records
    <project>/changesets/<id>/evidence/    archived proposal changes and verification evidence (on apply/discard)
    <project>/changesets/<id>/merging.json  an apply or revert being written (each file's digest before and after);
                                            left only by a server stopped meanwhile: undine serve finishes it or
                                            takes it back at its start, before anything else writes the project
    <project>/runs/<job>/                   simulation, DRC and LVS jobs (status.json with inputs and output hashes,
                                            netlists, results, logs)
    <project>/tapeout/<target>/             export bundles (+ .zip)
    <project>/llm/                          agent audit log (audit.jsonl), sent pictures, conversations (chats/)
                                            and agent turns with their progress (turns/<id>.json)
    .changesets/<project>/<id>/work         a changeset's working copy: a git worktree of its branch
                                            undine/<project>/<id> (the project <project>~<id>)
    .changesets/<project>/<id>/base         its baseline: the base commit checked out, detached (read-only)
    .changesets/<project>/<id>/tip.json     the branch's last commit while it is open; gseq.floor and the snapshots
                                            of batch and restore are here too, never in the worktree
    .changesets/.gitignore                  "*": the folder ignores itself in any repository the root is kept in
                                            (written when missing, never removed)
    llm.json, mcp-token                     agent connection settings, MCP bearer token
    mcp-sessions.json                       the MCP sessions a restarted server resumes (their changesets, the
                                            stopped agents)
    sim-jobs.lock, sim-queue.jsonl          held by the server whose simulations these are; the ones not started
                                            yet, in order (a restarted server takes both up)
    sim-queue-<id>.jsonl                    a second server's unfinished simulations, while another holds the root
                                            (taken up by the holder once that server has gone)
```

PDK Xschem libraries (e.g. `sky130_fd_pr`) appear as read-only libraries. Extra symbol search paths (e.g. Xschem's own
`xschem_library`) go in `symbol_paths`.

### Layout cell files

A cell's file holds its own shapes, instances and meta information (for a generated PCell: the PCell and its
parameters). The cells it instantiates are in it by name only (KLayout ghost cells), so every cell's geometry is stored
exactly once, and reading all files of a library, in any order, gives the whole hierarchy. Saving writes only the cells
whose content changed and removes the files of deleted cells. File names are the cell names; characters other than
`A-Z a-z 0-9 _ . + $ -` are %-encoded. Cell names that differ only in case need a case-sensitive file system (not a
Windows drive under WSL).

To read a library's layout outside Undine, read all its `.oas` files into one layout (KLayout:
`for f in files: layout.read(f)`), or use `undine.layoutstore.read(lib_dir, layout)`.

### Project format

`format` in `project.json` versions this layout. Undine converts an older project the first time it opens it:

| From | To | What changes |
|---|---|---|
| 2 | 3 | every schematic gets its netlist view, `<cell>.netlist.yaml` ([the netlist view](../design/netlist.md)) |
| 1 (no `format`) | 2 | each library's `layout.oas` (all cells) becomes one `<cell>.oas` per cell; the old file is kept as `layout.oas.pre-split` and can be deleted once the cells look right |

### Version control with git

The files of a project are meant to be kept in git: schematics are text, layout cells are separate files, and saving
does not touch unchanged cells. `python -m undine git-setup PROJECT` makes the project directory a git repository (or
uses the one it is in), ignores job results, tapeout bundles, the agent's log, the installed dependencies, the
`layout.oas.pre-split` a format conversion leaves, the evidence archived with each changeset (its runs, as hard links)
and the API's request log with the results it keeps for retries (`/runs/`, `/tapeout/`, `/llm/`, `/.undine/`,
`*.pre-split`, `/changesets/*/evidence/`, `/changesets/requests.jsonl` and `/changesets/requests/`; anchored at the top
of the project, so that a library named `runs` is not ignored; the changeset records themselves and
[undine.lock](../guide/libraries.md#undinelock) are kept), registers drivers for git and installs a hook:

- a **diff driver** for layout files (`*.oas`, `*.gds`: `diff=undine-layout`), so that `git diff`, `git log -p` and
  `git show` print layout cells as sorted text:

    ```diff
     cell inv  (dbu 0.001 um)
       67/20  box (790,940;1035,1140)
    +  68/20  box (-120,2100;90,2800)
       68/20  box (160,715;390,5405)
       inst sky130_fd_pr__nfet_01v8_w1p0_l0p15_nf1_451f14  r0 *1 645,975
    ```

- a **merge driver** for the files of a cell and the operation log (`*.sch`, `*.sym`, `*.oas`, `*.netlist.yaml`,
  `*.intent.yaml`, `history/*.jsonl`: `merge=undine`), so that `git merge` (and `git pull`, `git rebase`,
  `git cherry-pick`) merges them object by object instead of by lines. With it goes the driver of the **virtual merge
  base** (`merge.undine.recursive = undine-base`, running `merge-driver --virtual-base`): branches that merged each
  other have several merge bases, which git first merges into one. Where those merge bases conflict (two branches
  that resolved one conflict differently), it takes the base they share, so the merge sees both resolutions and stops
  on the conflict instead of taking one of them without a word.

- a **pre-commit hook** (`.git/hooks/pre-commit`, running `python -m undine precommit`) that refuses a commit whose
  staged operation logs hold layout edits not saved to the cell files ([below](#save-layouts-before-committing)). It
  is installed only where the repository has no pre-commit hook of its own and `core.hooksPath` is not set; otherwise
  `git-setup` says so and prints the command to add to the hook there (the History panel shows the same, and the HTTP
  API returns it as `hook_note`). In a hook `git-setup` installed, running it again replaces only the line that runs
  `undine precommit`: lines added to the hook stay.

The attributes are in the project's `.gitattributes`, which is committed, but the commands they name and the hook are
local git configuration (`diff.undine-layout.textconv`, `merge.undine.driver`, `merge.undine.recursive` and
`merge.undine-base.driver` in `.git/config`, the hook in `.git/hooks/`, with the Python that ran `git-setup`). Run
`git-setup` again in every clone, and in a project set up before Undine had the merge driver or the virtual-base driver;
without the merge driver git merges these files by lines, and a layout cell changed on both branches is a binary
conflict; without the virtual-base driver, a conflict that the merge bases of a criss-cross merge resolved differently
is decided without a word. Git runs these commands in the repository's top directory; they run as `python -P`, so a
Python file committed there is never imported in place of a module Undine uses (Python 3.10 has no `-P`: there they run
as `python -c` with a prelude that takes that directory off the module path, and keep the user's site-packages and
`PYTHONPATH`, which `-I` would drop).

**Merging branches.** Two people editing different cells touch different files, and git takes each file from the side
that changed it. When both branches changed the same cell, the merge driver merges its files object by object:
instances by name and their properties one by one, wires, layout shapes, intent items, the records of the operation
log ([semantic diff and merge](../design/merge.md) has the rules). Changes to different objects combine. An object both
branches changed differently is a conflict: the file keeps ours' value for it, git marks the file conflicted, and the
driver names the conflict (a single one in its own words; with several, their ids and "ours kept"):

```
undine: libs/l/inv.sch: schematic, 1 conflict (inst:M2:placement): instance M2: placement; see `undine merge-check`, then `undine resolve libs/l/inv.sch --theirs ID`
```

`python -m undine resolve PATH --theirs ID` (or `--ours ID`, `--all-theirs`, `--all-ours`) merges the file again from
git's index, taking that side for those conflicts. PATH is relative to the current directory or to the repository's
top, as the driver prints it; `git add` marks the file resolved, as usual (resolve prints the command). In the project
directory,
`python -m undine merge-check` (during a merge, cherry-pick or rebase; `--commit C` for a merge commit) lists the
conflicts still open, the warnings (for example instances of one cell or symbol that both branches placed at the same
position: likely one device placed twice; shapes both branches added that touch on one layer are a conflict, not a
warning), and the cells to verify with DRC / LVS: those whose hierarchy is as neither branch had it. A merge without
conflicts is not a verified design.

A rebase, or a cherry-pick of several commits, merges one commit at a time, and `merge-check` covers the one git stopped
at: the commits applied before it are part of ours by then, so their warnings (the driver printed them when it merged
them) and the cells only they merged are not listed. `merge-check` says so when there are such commits.
`git merge --squash`, `git cherry-pick -n` and `git stash pop` leave no commit for theirs: `merge-check` then shows the
conflicted files with their conflicts and warnings, and says that it cannot tell the cells to verify.

The driver's reports are in `.git/undine-merge/`, one for each merge of a file (a later merge of the file, which the
operation log has almost every time, keeps the earlier ones), and stay in the clone that merged: git does not transfer
them. In another clone, `merge-check --commit C` computes the cells to verify from the commit alone, and lists the
files the merge combined that have no report there (their warnings are unknown).

<a id="save-layouts-before-committing"></a>
**Save layouts before committing.** Layout edits reach the cell files (`.oas`) when the layout is saved; until then
they are a journal in the operation log, which Undine replays when it opens the library. A merge cannot replay a
journal on cell files combined from both branches: when both branches changed a library's layout and either has unsaved
edits in its log, the log is a conflict (`layout-journal`). Save the layouts on each branch, commit, and merge again
(resolving the conflict drops the unsaved edits). The pre-commit hook keeps this from happening: it refuses a commit
whose staged log has unsaved layout edits, naming the library and the cells:

```
undine: history/l.jsonl: library l has layout edits not saved to its cell files (cells inv)
undine: save the layout in Undine, `git add` the cell files and the log, and commit again (git commit --no-verify ...)
```

`git commit --no-verify` commits them anyway, as a journal in the log. The commit that concludes a merge or a
cherry-pick (`git merge --squash` and `git cherry-pick -n` included) keeps the unsaved edits the merge left pending (it
decided which of the branches' edits stay), and refuses only those made while concluding it, which neither branch's log
has.

- GitHub's web merge (and any merge on a server) runs no driver: it merges these files by lines and stops at a layout
  cell both branches changed. Merge such branches locally.
- An octopus merge (`git merge A B`, several branches at once) runs no driver either: a file changed on more than one
  of the branches is merged by lines. Merge such branches one at a time; `merge-check --commit C` lists the files an
  octopus merge combined as having no report. When an octopus merge stops at a conflict, `merge-check` takes every
  branch as a side and says that the files were merged by lines; `undine resolve` merges a conflicted file object by
  object.
- Undine reads an open layout again when git changes its cell files (or the journal in its log) and it has no unsaved
  edits. With unsaved edits it refuses to save it, which would write the cells as they were over the files git wrote,
  until the edits are dropped: **Discard the edits and reload** in the History panel
  ([HTTP API](api.md#git-history): `reload`). The log keeps the dropped edits as history, ended by a layout `save`
  record with `"args": {"reload": true}`, so they are never replayed and undo stops there. Save the layouts before
  merging, rebasing, pulling or checking out another branch. Schematics, symbols, netlists and intent are read from
  their files at every edit; a text open in the browser (RTL, the intent as YAML) is saved as the editor holds it, so
  open it again after git changed its file.

<a id="changesets-in-git"></a>
**Changesets in git.** A [changeset](../guide/changesets.md) is a branch of the project's repository, so a project with
changesets is in git: opening the first changeset in a project that is in no repository runs `git-setup` on it and
makes the project's first commit (below), and one in a repository without Undine's merge driver (`merge.undine.driver`) is refused (400
`not_set_up`, naming the command that sets it up: `undine git-setup <project>`). So is a project whose own library a
worktree cannot hold (400 `unsupported_library`), each symbolic link judged as the worktree's copy of it will resolve,
from the link's folder: at the library's folder, above it, inside it or where a link on the way leads, a link out of
the project or to nothing, one whose text is an absolute path or climbs out of the project, one the repository does
not keep (nor what it leads to: ignored, or Undine's own output), or a git repository of its own (a clone, a
submodule). The worktree would write through the link into the project's files, find nothing there, or hold the
library empty ([Changesets](../guide/changesets.md#a-changeset-is-a-git-branch)). That is checked before anything is
set up: a refused open does not make the project a repository. What a changeset leaves in the repository:

| | |
|---|---|
| the first commit | in a project that was in no repository, made right after the `git init` of its first changeset (or of **Set up git** in the History panel), and from the History panel's **Make the first commit** in a repository with none: the project's files as they are (the tree a base commit has, below, from what the index tracks of the project), no parent, on the user's branch, which is made at it only while it has no commit; the index entries of the project's directory are reset to it. From the History panel (and its **Set up git**) not while a log holds unsaved layout edits (409 `unsaved`: save the layouts first); at the opening of a changeset such a log goes in as a journal, as the base has it. Message `Project <project>: its files when Undine set up its repository` (from the History panel: `Project <project>: its first commit, its files as they were`). Without a commit the user's branch gets no merge commit: an apply there is left uncommitted (`"why": "no_commit"`) |
| the base commit | the project's files when it was opened (a temporary index: uncommitted and untracked files included; `runs/`, `tapeout/`, `llm/`, `.undine/`, `*.pre-split` and `changesets/` left out, whether or not the repository ignores them), parent the user's HEAD then; message `Base of <id>: <changeset title>` |
| `refs/heads/undine/<project>/<id>` | the branch while the changeset is open: the base, then a commit of the working copy at each lease end, before checks, apply and discard (message: the title, `Changeset <id> of <project>: <reason>`, the actors, the turns, the operations). Commits are made with `commit-tree`: no hooks, no signing, author the actor (`user`, `agent:<name>`), committer `Undine <undine@localhost>` |
| two worktrees | `.changesets/<project>/<id>/{work,base}` (above), registered in the repository (`git worktree list`). The `.changesets/` folder ignores itself (its `.gitignore` holds `*`; written before the first worktree when it has none, never removed), so any repository the projects root is kept in leaves the worktrees, `tip.json`, `gseq.floor` and the snapshots out of `git status` and `git add -A`, also after the last worktree is gone. When the projects root is in the project's own repository, one line in `.git/info/exclude` (`/<path>/.changesets/`) also excludes the folder while a worktree of the repository is there; a worktree elsewhere in the repository excludes only itself there |
| `refs/undine/changesets/<project>/<id>` | the branch kept once the changeset is applied or discarded (the branch itself is deleted): its commits stay for the evidence and a revert. Also where a branch goes whose changeset has no record (a server stopped while opening it): its working copy committed, its worktrees removed; Undine never deletes a changeset's commits |
| `Apply <id>: <changeset title>` | the merge commit of an apply on the user's branch (parents HEAD and the tip), made only when the branch has a commit and nothing else was uncommitted (otherwise the record's `why` says which: `no_commit`, `changes`; `failed` when the commit failed); a revert's commit is `Revert <id>: <changeset title>`. Paths git ignores and does not track (the logs, if the project's `.gitignore` leaves `history/` out) stay out of it |

The changeset records (`<project>/changesets/`) are not ignored: they are yours to commit or not, and they never enter
a commit Undine makes, nor count as a change that keeps an apply from being committed. Undine's commits run no hook of
the repository, so the pre-commit hook does not check them; the working copy's layouts are saved before each commit.

## Dependency files

What a project's [dependencies](../guide/libraries.md) are, how they are locked and installed, and what a
[package](../guide/libraries.md#packages) says about itself.

### project.json

`libraries` names each dependency the project places itself: `{"git": URL, "rev": REV, "subdir": DIR}`, and
`"package": NAME` when it was added with `--package` (the [package] name its manifest must give). The packages those
depend on are not named here; the lock has them.

`trusted` lists the dependency versions a person [trusted](../guide/libraries.md#trusting-a-dependencys-code) in this
project, one key each: `<package>@<version>` (`analog_ip@1.2.0`), or `<local name>@<commit12>` (`ip@229914698b1f`) for
a dependency without a manifest. `lib trust` and the browser's trust dialog write it; `lib update`, `lib remove` and
`lib lock` take out the keys of what they change or drop. The key is absent when nothing is trusted. A changeset's
working copy counts the project's keys as well as its own.

```json
"libraries": {"analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib"},
              "bias": {"git": "https://github.com/me/bias_gen.git", "rev": "v0.3.2", "package": "bias_gen"}},
"trusted": ["analog_ip@1.2.0"]
```

### undine.lock

The formats are in [undine.lock](../guide/libraries.md#undinelock). Lock format 2 adds, for packages:

- `libraries[<local name>]` keeps each dependency `project.json` names. A plain one, or one locked before packages and
  not resolved since, is the entry as before (`git`, `rev`, `subdir`, `commit`, `sha` or `tree`). One resolved with a
  manifest adds `files: "include"` (its `tree` covers the [include file set](../guide/libraries.md#the-include-file-set-and-the-lock)),
  `package`, `version`, `names` (every local name it is installed under, the key first) and `deps` (`{local name:
  package}`, the dependencies its manifest names).
- `packages[<package>]` holds the packages reached only through manifests: `{package, version, git, rev, subdir?,
  commit, tree, files: "include", names, deps}`. A sibling (`{ subdir = ... }`) records its requirer's `git` and `rev`.
- A package name appears once across both tables. A format 1 lock never has these keys: a package there is locked as
  a plain entry (every file, `sha`).

```json
"libraries": {
  "analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib",
                "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:5d0c…", "files": "include",
                "package": "analog_ip", "version": "1.2.0", "names": ["analog_ip"],
                "deps": {"sar_common": "sar_common", "bias": "bias_gen"}}
},
"packages": {
  "sar_common": {"package": "sar_common", "version": "1.3.1", "git": "https://github.com/me/sar_common.git",
                 "rev": "v1.3.1", "commit": "5b1c0e2a9d11…", "tree": "sha256:77aa…", "files": "include",
                 "names": ["sar_common"], "deps": {}},
  "bias_gen": {"package": "bias_gen", "version": "0.3.2", "git": "https://github.com/me/bias_gen.git",
               "rev": "v0.3.2", "commit": "0c4e2b7a1f30…", "tree": "sha256:9a7e…", "files": "include",
               "names": ["bias", "bias_gen"], "deps": {}}
}
```

`lib sync` installs every entry under each of its `names`, and refuses, before anything is fetched, a lock that does
not install what a package needs (a package its `deps` name that the lock does not lock, a local name given twice):
run `lib lock`.

### The installed dependencies

`.undine/deps/<local name>/`: one directory per local name, made of read-only hard links into the [store](#the-store) (copies across file
systems). A package under two local names is two directories of the same files. Each has `.undine-dep.json`, its
**install marker**: the lock entry it was installed from, `modes` (its executable files, which a `0444` file cannot
show), and for packages `name` (this directory's local name), `direct` (whether `project.json` names it), `package`
and `version`, and `via` (the packages that place it) when another package brought it in. A dependency directory
without a marker was not installed by Undine; `lib sync` removes or reinstalls it. Opening a project never reads
`undine.lock`: the library browser learns the packages other packages brought in from these markers. `.undine/` is
never versioned.

### undine-package.toml

A package's manifest, at the top of its directory: `[package]` (`name`, `version`, and `description`, `license`,
`authors`, `repository`), `[pdk]` (`family`, `variants`, `commits`, `compat`, `built_with`), `[dependencies]`,
`[exports]` (`cells`), `[files]` (`include`), and `[checks]` and `[renames]` (accepted, not used yet). The meaning of
each, with an example, is in [The manifest](../guide/libraries.md#the-manifest). Undine reads it as UTF-8 TOML of at
most 256 KiB, a regular file (a symbolic link is refused), and refuses any table or key it does not know.

## The store

Dependency files are kept once per user, in a content-addressed store, and installed into each project by hard link.
The PDK hashes and the compiled Verilog-A models are kept there too. The store is shared by every project of the
user, on every projects root:

```
~/.undine/                                  per-user files ($UNDINE_HOME): the store, connections.json
    store/                                  the store ($UNDINE_STORE moves it alone)
        objects/sha256/<2 hex>/<62 hex>     every dependency file once, named by the SHA-256 of its bytes; read-only
        trees/sha256/<hex>.json             a dependency's files as a format 2 lock's `tree` names them:
                                            {path: [mode, object hash]}
        trees/archive/<sha>.json            the same, named by a format 1 lock's `sha` (the files `git archive` gave)
        git/<sha256 of the URL>.git         a bare repository per dependency URL, kept: the next version fetches only
                                            what changed, and a commit fetched before needs no network
        pdk/<commit>-<variant>-<root>.json  a PDK's part hashes and the stamps of its files (`pdk verify`): <root> is the
                                            first 12 hex of the SHA-256 of the PDK's real path (nocommit-<root>-<name>
                                            for a PDK with no version)
        build/<commit>/<variant>/osdi/      compiled Verilog-A models (`pixi run osdi`); build/local-<root>/... for a PDK
                                            with no version
        projects/<sha256 of the path>.json  {path, synced}: the projects that added, updated, synced or locked a
                                            dependency; `store gc` keeps what their locks name
        tmp/                                every write lands here first and is then renamed into place
        .lock                               held shared by a dependency operation, exclusive by `store gc`
```

- An object's hash is the SHA-256 of the file's raw bytes; a tree's is `sha256:` and the SHA-256 over
  `"{mode} {hex} {path}\0"` for each file in the order of the UTF-8 bytes of its path (the exact rule is in
  [Packages and PDKs](../design/packages.md#index)). The mode (`100644`, or `100755` for an executable) counts in the
  hash only: every object and every installed file is read-only (`0444`), since one file linked into several projects
  can have only one mode.
- `.undine/deps/<lib>/` holds hard links to the objects, so a library used by ten projects, or by the working copies
  of a project's changesets, takes its disk once. Where the store and the project are on different file systems, or
  the file system refuses a link, the file is copied instead (read-only too). `UNDINE_STORE_LINK=copy` always copies.
- A file edited in place is caught: `lib status` reports the dependency as `modified`, and before an object is linked
  it is hashed again. One that was only made writable (a `chmod` on an installed file changes the object too) still
  hashes to its name: it is made read-only again and kept. `python -m undine store verify` re-hashes the whole store
  and removes what does not match; `store gc` removes what no project here locks or installs
  ([Command line](cli.md#command-line)).
- `.undine` and `.undine/deps` must be the project's own directories: when either is a symbolic link (or a file), the
  dependency commands refuse before they change anything, and `lib status` reports it
  ([troubleshooting](../troubleshooting.md#undinedeps-is-a-link-or-not-a-directory-or-undine-is)).
- Without the store's record of a format 2 tree (the store emptied or moved, or a project copied with its
  `.undine/deps`), `lib status` checks the installed files against the lock's tree with the modes the install marker
  records, and the next `lib sync` imports them into the store.
- Kept repositories (`git/`), PDK records (`pdk/`) and builds (`build/`) are never removed by `store gc`; delete them
  by hand if you need the space (they are made again when needed).

| Variable | Default | Used for |
|---|---|---|
| `UNDINE_HOME` | `~/.undine` | per-user files: the store (unless `UNDINE_STORE`), connections added in the UI |
| `UNDINE_STORE` | `$UNDINE_HOME/store` | the store alone, for example on the file system of the projects root (hard links work only within one file system) |
| `UNDINE_STORE_LINK` | `link` | `copy`: install dependencies as read-only copies, not hard links (any other value is refused) |
| `UNDINE_OFFLINE` | off | `1`: `lib sync` and `POST .../deps/sync` install from the store only and never fetch, as `lib sync --offline` |
| `UNDINE_CIEL` | the `ciel` beside Undine's Python, else `PATH` | the ciel that `pdk fetch` runs; set to a path that is not a file, no ciel runs |
| `UNDINE_FETCH_MAX` | `1073741824` (1 GiB) | the most bytes one dependency's files may have; with more than 100 000 files it is refused too, before any file is read |

## Operation log

Every edit, schematic and layout, is recorded in `<project>/history/<lib>.jsonl` (append-only JSON lines): who (`actor`: `user` or
`agent:<name>`), what (`op` + `args`, with stable object ids), why (`reason`), which proposal (`changeset`), the resulting
file hash, and the structural `effects` (added / removed / modified instances, wires, texts with before/after values).
Undo and redo are derived from the log, so they survive a server restart. Send `X-Undine-Actor: agent:<name>` and
`reason` / `changeset` in the edit request to attribute a change; `GET /api/p/<project>/history?lib=<lib>&cell=<cell>`
reads it. The format is documented at the top of `server/undine/store/oplog.py`.

In a changeset's working copy every record also carries `gseq` (its position in the changeset, across all libraries),
`opid` (the operation it belongs to: one tool call or one HTTP request, possibly several records), `turn` (the agent
turn, or null) and `changeset`. The worktree's log starts as the project's was at the base; the changeset's own records
are those after the last `seq` at the base (`base_seq` in its record): numbering, the timeline, undo and redo see only
those. An apply appends them to the project's log, renumbered (`merged_seq`: their `seq` in the branch), then a record
`{"op": "merge", "args": {"changeset": <id>, "action": "apply"}}`; a revert appends only such a record, with `"action":
"revert"`. Undo stops at a `merge` record. See [Live changesets](../design/live-changesets.md).

## Job provenance

Every simulation, DRC and LVS job records in its `status.json` what it ran on and what it wrote, so a result can be
traced to the design it belongs to and outdated results are visible:

```json
"inputs": {
  "design":  [{"view": "layout",  "lib": "mylib", "cell": "inv", "sha": "5f8e…"},
              {"view": "netlist", "lib": "mylib", "cell": "inv", "top": "inv", "mode": "lvs", "flat": false, "sha": "7a52…"}],
  "pdk":     "sky130A@8afc8346a57f",
  "deck":    {"kind": "lvs", "name": "sky130.lvs", "sha": "9ac9…"},
  "tools":   {"klayout": "KLayout 0.30.12"},
  "options": {}
},
"key":   "3cb3dfc6557fbe4d217c7f48",
"files": {"result.lvsdb": "f68b…", "input.gds": "8830…", "schematic.spice": "7a52…", "extracted.cir": "7484…", "log.txt": "78ab…"},
"stamps": {"result.lvsdb": [182334, 1762149600123456789, 5374021], ...}
```

| Design entry | Hash of |
|---|---|
| `layout` | the cell and everything it instantiates as the tools see it: cell names, layers, shapes, instances. Shape order, cell indexes, instance properties and meta information do not count, so saving and reloading keep the hash |
| `netlist` | the netlist generated from a schematic (for simulation the testbench, for LVS the reference). A comment or a wire moved without changing connectivity keeps it; an edit in a sub-cell changes it. A simulation with stand-ins records them as `substitute` (`{"lib/cell": "lib/cell"}`), and its hash is of the netlist with them: an edit of a stand-in changes it, an edit of the cell it replaces does not. See [Stand-ins](../guide/verification.md#stand-ins) |
| `file` | a SPICE file on the server that LVS compared against |
| `text` | a netlist sent with the request (never reported as outdated) |
| `extraction` | a cell simulated as extracted from its layout (`mode` `c` or `rc`): with `job` (an extraction job named in the request), that job's subcircuit, `is gone` once the job is outdated; without, the subcircuit of its newest up-to-date extraction of that mode, `is gone` once there is none (the layout changed). See [Parasitic extraction](../guide/verification.md#parasitic-extraction-and-post-layout-simulation) |

A parasitic extraction job (`kind: "pex"`) runs on the `layout` and, when the cell has a schematic, its
`schematic-interface` (the ports' names and order); other schematic edits do not make it outdated.

A simulation job also records `models`: for each `.lib` line of the PDK's model library, the `library` and `section`
its netlist names, and either `reduced` (the copy it read, with `cards`: kept, of the section's) or `full` (why it read
the whole library). The inputs are those of the netlist as generated, so the copy does not change the key
([PDK models](../guide/verification.md#pdk-models-in-a-simulation)).

`key` is a hash of all inputs: two jobs with the same key ran on the same thing. A job is **outdated** when its design,
PDK, deck or tool versions differ from what they are now; `GET /api/p/<project>/jobs/<job>/freshness` answers
`{"fresh": true | false | null, "changed": ["layout of mylib/inv changed", ...]}` (`null`: the job is from before inputs
were recorded). The format is documented at the top of `server/undine/store/provenance.py`.

Changeset checks expose the same `fresh` and `changed` fields while the proposal is open. The `ok` field retains
the job's original outcome, so `ok: true` with `fresh: false` means a previous pass, not a pass for the current
proposal. Required checks without a job have `state: "not_run"`. Rechecking reuses completed, current jobs and
starts only missing, outdated, unknown or unsuccessful jobs.

`stamps` gives, for each output file, the `[size, mtime_ns, inode]` it had while it was hashed (a file that changed
meanwhile has none): a changeset's evidence takes the file's hash from `files` instead of reading it again while its
stamp is still the same.

When a changeset is applied or discarded, its changes and check summary are captured with the job directories
from both preview projects in `<project>/changesets/<id>/evidence/`. Job `status.json` files retain `inputs`, `key`
and output-file hashes alongside the actual results and logs, including artifacts from failed launches.
`manifest.json` records the captured changes (with operation arguments), verification summary, archived jobs
and file hashes. The `base/` and `work/` subdirectories retain their project settings, dependency locks and job
directories in `runs/`. For a changeset on git the manifest is `"version": 2` and names the base commit and the tip
that was reviewed (`"commits": {"base", "tip"}`): the design files and the full operation logs (unless the project's
`.gitignore` leaves `history/` out of git) are in those commits, kept under `refs/undine/changesets/<project>/<id>`; an
older changeset's archive (`"version": 1`) also has the logs, in
`history/`. The job directories' files are hard links to the runs' own files, not copies (a copy where the evidence
is on another file system or a link is refused; project settings, dependency locks and an older archive's logs are
copied, since those are written in place): the manifest says how many are links (`"linked"`), the size of it all
(`"bytes"`), and each file's `[size, mtime_ns, inode]` when it was saved (`"sources"`, which the closure compares to
the files to see that nothing changed meanwhile, instead of reading them again; a manifest without it is compared by
the hashes). A finished job's files are never rewritten in place (its `status.json` is written anew and renamed in),
and stopping a job stops all that its process started (its process group, also of a simulation a restarted server
took up), so the links keep what was archived; a run that could still be written is copied instead (its job
cancelled, stopped at its time limit (`"timed_out": true` in its status) or never finished, its ngspice killed while
no server watched it (`"cut": true`, whatever its log says), or a process works in it or holds a file of it open for
writing). A simulation that a restarted server took up is archived as any run once it has ended (its status then has
the hashes and stamps of its outputs too); while it runs, here or in another server on the root, the changeset cannot
be closed. Nor can it while processes a job started still run after the job has ended (the process group its status
records, `"process"`); `cancel_job` stops them. A person can
prune the waveforms (`*.raw`, `*.vcd`, `*.fst`) of a closed changeset's runs (`POST .../evidence/prune`): their
hashes stay in `"files"`, and `"pruned"` notes them, `[{"t", "by", "files": [path in the evidence], "bytes"}]`. The
changeset record includes
`evidence: {path, captured, jobs, bytes, pruned?}` (`pruned`: the last prune's time and person, the files and bytes
of all). The archived check summary keeps freshness as it was at capture time;
it is not recalculated against later edits to the real project. Jobs interrupted by a server restart are shown as
`interrupted` and remain retryable; their original status files are preserved. If edits or new jobs arrive after
the snapshot, `cleanup_warning` explains why the working copy was retained for recovery. The closed changeset
continues to describe the captured snapshot. An apply or a revert that a stopped server left half written and that
was taken back (see `merging.json` above) puts its note in the record's `interrupted` and in `cleanup_warning`; the
note goes once an apply, revert, discard or recovery of the changeset succeeds. See [Changesets](../guide/changesets.md).
