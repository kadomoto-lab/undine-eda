# Libraries and dependencies

A project has three kinds of libraries; the library browser groups them:

| Kind | Where | Edited |
|---|---|---|
| **Project libraries** | `libs/<lib>/` in the project | here |
| **Dependencies** | a git repository at a pinned commit, installed in `.undine/deps/<lib>/`; a [package](#packages) also brings the packages it depends on | read-only; changed by *update* |
| **PDK libraries** | the PDK the project is pinned to ([PDKs](pdks.md)) | read-only |

## Dependencies

A library shared between projects (an IP block, a team's standard cells) is pulled in like a package, not linked:
the project names the repository and a branch, tag or commit, and `undine.lock` records the commit it resolved to.
Every checkout of the project then gets exactly the same cells, and a change to the shared library reaches the
project only when someone updates the dependency, as a visible change of `undine.lock`.

```sh
pixi run python -m undine lib add demo analog_ip https://github.com/me/analog_ip.git --rev v1.2.0 --subdir lib
pixi run python -m undine lib update demo analog_ip --rev v1.3.0
pixi run python -m undine lib sync demo          # after a checkout or a pull: install exactly what is locked
pixi run python -m undine lib sync demo --offline   # the same with no network, from the store
```

In the browser: the package icon next to the folder icon (**New library**) in the library browser opens **Add library
dependency**; a dependency's row shows its version (`v1.2.0 @ 2299146`) and has **Update** and **Remove**.

- A dependency's library is a directory of cells (`<cell>.sch`, `<cell>.sym`, `<cell>.oas`) at the top of the
  repository or in `subdir`. Schematics of the project place its symbols as `<lib>/<cell>.sym`.
- A SPICE netlist has one subcircuit per cell name. When a testbench reaches two cells of one name from different
  libraries (your `inv` and a dependency's `inv`) that netlist to other lines, netlisting is refused, naming both;
  rename yours, or place the same cell in both places. Two that netlist to the same lines are written once
  ([troubleshooting](../troubleshooting.md#two-cells-named-inv-are-netlisted-ainv-and-binv-and-they-differ)).
- **Update** reports the files that changed and the cells of the project that use them. Simulation and LVS results
  of those cells become [outdated](verification.md#outdated-results), since their netlists change. Layout cells are
  copied into the project's library when placed, so they keep the old geometry until placed again; the report
  names them.
- **Revert:** undine.lock and project.json are ordinary files of the project. Reverting their change (e.g.
  `git checkout HEAD~1 -- undine.lock project.json`) and running `lib sync` installs the previous version again.
  A dependency the reverted project.json no longer names is a *leftover*: `lib status` reports it as `leftover`
  (exit 1), since its symbols still resolve here and not in a fresh checkout, and `lib sync` removes its files from
  `.undine/deps/<lib>/` and its entry from undine.lock.
- The files of a dependency are installed read-only. If one is edited anyway, `lib status` reports the library as
  `modified` and `lib sync` restores it.
- Fetching needs `git` on the server. Remote URLs (`https://` or `http://`, `ssh://`, `git://`, `git@host:path`) and
  local repositories (an absolute path, `~/...` or `file://`) are accepted. A `rev` names a branch, tag or commit;
  neither it nor `subdir` may start with `-`.

### The store

Undine keeps every dependency file it fetched once per user, in a content-addressed **store**
(`~/.undine/store/`, [The store](../reference/data.md#the-store)), with a copy of each repository it fetched from.
`.undine/deps/<lib>/` is made of **hard links** to the store's files, so:

- a library used by several projects, or by the working copies of a project's changesets, takes its disk once, and a
  new version shares the files that did not change with the old one;
- a dependency fetched once installs again with **no network**: in another project, after a `git pull` that moved it
  back to a version fetched before, or with `lib sync --offline`;
- the installed files are read-only (`-r--r--r--`). Write to a dependency's files and the write is refused; a file
  replaced anyway is reported as `modified`, and `lib sync` installs it again from the store.

Where the store and the project are on different file systems (or the file system refuses a link), the files are
copied instead, read-only too, with no message; put the store on the projects' file system with `UNDINE_STORE` to
share the disk again ([troubleshooting](../troubleshooting.md#dependencies-take-disk-space-in-every-project)).
`python -m undine store verify` checks the store, and `store gc` removes what no project on this machine locks or
installs ([Command line](../reference/cli.md#command-line)).

**After upgrading Undine.** Dependencies installed by an Undine before the store are in no store yet. The first
`lib sync` (or the lock dialog's sync) imports each one whose files match `undine.lock` into the store, with no
network; from then on it syncs offline too.

### Sync installs the lock as it is

`lib sync` installs exactly what `undine.lock` pins and never resolves anything: it does not look at branches or
tags, and never changes the lock. When `project.json` names a dependency the lock does not pin that way (a `git
pull` brought a new `rev`, a dependency added by hand, or a project with dependencies and no lock), the sync is refused
before anything is fetched, naming each one:

```
error: analog_ip: project.json asks for rev v1.3.0, undine.lock locks v1.2.0: run `python -m undine lib lock demo` to resolve it, or `python -m undine lib update demo analog_ip`; nothing was changed
```

`lib lock` fetches and locks what the lock does not pin as `project.json` names it, and nothing else; `lib update`
moves one dependency. Then `lib sync` works. So a sync never changes what the project is checked with: only `lib
add`, `lib update` and `lib lock` do, and the change is in `undine.lock` for review.

`lib sync --offline` (or `UNDINE_OFFLINE=1`) installs from the store and from the repositories it keeps, and refuses,
naming them, the dependencies it has in neither: `offline, and not in the store: analog_ip @ 229914698b1f (sha
e6d28176...)`. Run the sync once with the network.

!!! warning "Local repositories over HTTP"

    The `lib` commands take any local repository. The browser's **Add library dependency** and the HTTP API
    (add, update and sync, also for a path a cloned `project.json` names) refuse one unless the server was started
    with `undine serve --dep-root DIR`, and then take only a repository whose real path (symlinks and `..` resolved,
    its `.git` included) is under one of those directories, as `--import-root` does for imported files. Give
    `--dep-root` once per directory. Without it a browser could make the server read any git repository on its disk
    into a project. The same check covers the store's copy of a repository: a request cannot install from a kept
    copy of a local repository outside those directories, even one a `lib` command fetched. A sync that installs
    from the store's files alone reads no repository and is not checked.

## Packages

A dependency may have a manifest, `undine-package.toml`, at the top of its directory (the repository's top, or its
`subdir`). With one it is a **package**: it has a name and a version, it names the packages it depends on (Undine
installs those too), the PDKs its cells are for, and which of its cells are public. A dependency without a manifest is
a plain git dependency and works as before.

There is no package index yet: every package, and every package it depends on, is found by its git URL and a `rev`.
`lib add` takes a package as it takes any dependency:

```sh
pixi run python -m undine lib add demo analog_ip https://github.com/me/analog_ip.git --rev v1.2.0 --subdir lib
```

```
analog_ip: analog_ip 1.2.0 at 229914698b1f (4 cells, 1 internal), locked in undine.lock
  + sar_common 1.3.1 as sar_common, via analog_ip
  + bias_gen 0.3.2 as bias, bias_gen, via analog_ip
~ sar_common 1.3.1: 1 cell with .control (not trusted): a simulation using it needs `python -m undine lib trust demo sar_common`
```

`--package NAME` (**Package name** in **Add library dependency**) says which package the repository must hold: the
add is refused when its manifest names another one, or when it has none. The library browser shows a package's
version on its row (`analog_ip 1.2.0`), and a package another one brought in under **via** with the package that
needs it.

A package that depends on others, or that is installed under a name other than its own, needs **lock format 2**. A
project made with `new` has a format 1 lock, so run this once (it pins the PDK as installed here, see
[undine.lock](#undinelock)):

```sh
pixi run python -m undine lib lock demo --format 2
```

On a format 1 lock, `lib add`, `lib update` and `lib lock` refuse such a package and change nothing:
`analog_ip: package analog_ip names dependencies (sar_common, bias): needs lock format 2: run python -m undine lib
lock demo --format 2 first; nothing was changed`. A package with no dependencies, added under its own name, works on
either format.

**Check everything, then write.** `lib add`, `lib update`, `lib lock` and `set-pdk` read every manifest they need first
(from git for what they resolve, from the store or the installed files for what stays locked), check versions, names,
cells and the PDK, and refuse once with every problem found, ending `nothing was changed`; only then do they write
`project.json`, `undine.lock` and `.undine/deps/`. Over HTTP the refusal is a 409 `resolution_refused` with the
problems listed ([HTTP API](../reference/api.md#dependency-refusals)); the browser shows them in a dialog over the
form, which keeps what you typed.

### The manifest

```toml
[package]
name = "analog_ip"                 # lower-case letters, digits and _, starting with a letter (at most 64)
version = "1.2.0"                  # semver: MAJOR.MINOR.PATCH, e.g. 1.2.0 or 1.3.0-rc.1
description = "Bandgap, LDO and comparator for sky130"
license = "Apache-2.0"
authors = ["Kadomoto <k@example.org>"]
repository = "https://github.com/me/analog_ip.git"

[pdk]
family = "sky130"
variants = ["sky130A"]
commits = ["8afc8346a57fe1ab7934ba5a6056ea8b43078e71"]
built_with = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"   # the PDK its layouts were drawn and checked on

[dependencies]
sar_common = { git = "https://github.com/me/sar_common.git", rev = "v1.3.1", version = "^1.3" }
bias = { git = "https://github.com/me/bias_gen.git", rev = "v0.3.2", package = "bias_gen", version = "~0.3" }
common = { subdir = "common" }     # a sibling: this repository at this commit, subdir from the repository's top

[exports]
cells = ["bandgap", "ldo", "comparator"]

[files]
include = ["*.sch", "*.sym", "*.oas", "*.netlist.yaml", "*.intent.yaml", "rtl/**", "README.md", "LICENSE"]
```

- **`[package]`**: `name` and `version` are required. The version is a label the package's author gives; what is
  installed is the commit `rev` names, pinned by its files.
- **`[dependencies]`**: each key is the **local name** this package's schematics place that dependency's symbols by
  (`bias/ref.sym`). A table with `git` and `rev` (both required: a tag, branch or commit), and optionally `subdir`,
  `package` (the [package] name it must have; default: the key) and `version`. `{ subdir = "..." }` alone is a
  **sibling**: another package in the same repository at the same commit, its `subdir` counted from the repository's
  top. A dependency must be a package itself: only `project.json` names a plain git dependency.
- **`version`** is checked against the [package] version of the tree `rev` names: `^1.3` (the same major version,
  1.3.0 or later; for `0.x`, the same minor), `~0.3` (the same minor), `=1.3.1`, a bare `1.3` (as `^1.3`) or `*`
  (any). A version with a pre-release (`1.4.0-rc.1`) matches only a requirement that names one of that release.
  Comparators (`>=1.2`, `<2`), lists (`^1.2, <1.5`) and index requirements (`sar_common = "^1.2"`, `{ version = "^2",
  index = "acme" }`) come with the package index and are refused, saying what to write instead
  ([troubleshooting](../troubleshooting.md#an-index-requirement-or-a-version-range)).
- **`[pdk]`**: see [PDK compatibility](#pdk-compatibility). Optional; without it the package is not checked, and
  `lib status` says so.
- **`[exports]`**: the public cells. The others are **internal**: installed, placed by the package's own cells and
  netlisted as usual, but left out of the symbol picker and `list_symbols`, and marked `internal` in the cell list.
  Placing one by its name still works. Without `[exports]` every cell is public. Each exported cell must have a
  `.sym`, `.sch` or `.oas` among the package's files.
- **`[files]`**: `include` lists the files the package consists of; see
  [The include file set and the lock](#the-include-file-set-and-the-lock). Without it, every file of the directory.
- **`[checks]`** and **`[renames]`** are accepted as tables and not used yet (they are for publishing, later).
- Any other table or key is refused, naming it and the known ones: a typo such as `revision =` would otherwise mean
  something else silently.

A manifest that is wrong is refused with where it is (URL, commit and subdir), the key and what to do, and the
chain that led to it: `https://github.com/me/analog_ip.git @ 229914698b1f:lib: undine-package.toml:
dependencies.bias.rev: missing: name a tag, branch or commit, e.g. rev = "v1.2.0" (required by demo -> analog_ip
(https://github.com/me/analog_ip.git v1.2.0 subdir lib))`
([troubleshooting](../troubleshooting.md#undine-packagetoml-is-refused)).
A manifest is a regular UTF-8 file of at most 256 KiB; a symbolic link is refused.

### A dependency's own dependencies

When a package names dependencies, Undine resolves them, and theirs, and installs every one: the project names only
the packages it places itself. `lib add` and `lib update` resolve only the dependency they name and what it needs;
every other locked package stays as it is locked (a `rev` such as `main` that moved meanwhile is not followed).

- **One version per package.** A netlist names a subcircuit, and a layout a cell, by the cell's name alone, so two
  versions of one package cannot be in one project. Two requirements that give one package two different trees are
  refused, naming both chains: `sar_common: two versions are required, demo -> analog_ip 1.2.0 (v1.2.0 @
  229914698b1f) -> sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11) and demo -> sar_common 1.2.0 (v1.2.0 @ 0c4e2b7a1f30):
  align the revs (one version per package)`. A diamond (two packages needing the same version of a third) installs
  it once.
- **A version that does not match** the requirement is refused, naming the chain and the version the `rev` has.
- **Local repositories.** A package from a remote URL may not name a local one (`file://`, `/path`, `~/...`) in its
  manifest. Over HTTP every URL a manifest names is checked against `--dep-root` as the request's own.
- **Which packages are placed by whom.** A package another one brought in is not a dependency of the project:
  `lib update` and `lib remove` refuse it, naming the dependency that places it (`sar_common: placed by analog_ip;
  update or remove that instead`). `lib remove analog_ip` removes it with the packages nothing needs any more, and
  keeps (and names) one still placed by another; `lib update analog_ip` lists the packages it brought in, moved and
  no longer needs.
- **Revert** works as for a plain dependency: restore `project.json` and `undine.lock`, then `lib sync`.

### Local names and aliases

A **local name** is the name a library's symbols are placed by: `C {bias/ref.sym}` places cell `ref` of whatever is
installed as `bias`. Local names are **project-wide**: each package is installed under every local name that places
it, as `.undine/deps/<local name>/` (hard links to the same files, so a second name costs no disk), and the symbol
path stays one list of directories. That is what lets Xschem's own netlister, and Xschem opened on the project, find a
package's dependencies.

- **The package's own name.** A package is also installed under its [package] name, since its own schematics place
  its cells as `<name>/<cell>.sym`. Added as `bias` with `--package bias_gen`, it is installed as `bias` and
  `bias_gen` (`installed as bias and bias_gen`); the browser shows `→ bias_gen` on the `bias` row and `= bias` on
  `bias_gen`'s.
- **One package per name.** Two libraries that give one local name to different packages are refused, naming both
  (`bias: one local name for two packages, ...: give one of them another local name in its requirer's
  [dependencies]`). Names are compared without case (`IP` and `ip` are one directory on a case-insensitive file
  system).
- **Reserved names.** `.undine/deps/` comes first on the symbol path, so a local name may not be a project library's
  name, a PDK library's, `devices`, or the name of a folder in Xschem's library or in a `symbol_paths` entry of
  `project.json`: it would shadow that library. A new local name is refused (`lib add ... sky130_fd_pr`: `sky130_fd_pr:
  the PDK library sky130_fd_pr has that name, and .undine/deps would shadow it: choose another local name; nothing
  was changed`); a name `project.json` names already keeps working, and `lib status` notes that it shadows the
  other library.
- A new project library may not take a name a dependency is installed under either.

### Cell names are disjoint

A netlist has one subcircuit per cell name, and a layout one cell per name, so the cells of all packages, of the
project's libraries and of the PDK's libraries must have different names. Resolution refuses a clash, naming the
cell and both sides: `comparator: analog_ip 1.2.0 and sar_common 1.3.1`.

- A clash is refused only when a package with a manifest is one side. Two plain git dependencies, or a plain
  dependency and a project library, are not refused (as before); `lib status` notes a project cell named like a plain
  dependency's cell, and netlisting a testbench that places both is refused, naming both
  ([troubleshooting](../troubleshooting.md#two-cells-named-inv-are-netlisted-ainv-and-binv-and-they-differ)).
- Only **logic cells** count for the project: a cell with a `.sch`, `.sym`, `.sv` or `.v`. A layout copy of a
  placed cell (an `.oas` alone, which placing a package's cell makes in your library) is not a clash.
- A package's own layout-only cells (copies of cells it places, subcells) clash only with a logic cell of a package
  it does not depend on.
- One package under two local names is one package, not a clash.
- `lib status` reports a project cell named like a package's cell as an issue: rename yours.

### PDK compatibility

A package's `[pdk]` says which PDKs its cells are for, and every package is checked against the project's PDK (as
`undine.lock` pins it), those a dependency brought in included:

- `family` must be the project PDK's family (ciel's: `sky130`, `gf180mcu`, `ihp-sg13`), and the project's variant
  must be among `variants` (sky130A and sky130B have other metal stacks).
- The PDK's commit must be among `commits` (full commits; PDK commits have no order, so there are no ranges). With no
  `commits` and no `compat`, `built_with` alone is the accepted commit; with none of the three, any commit of the
  family and variants is.
- `compat` names compatibility tags (`sky130/2025`), which PDK profiles will define. This Undine knows no tag yet, so
  a package that names only tags is refused, with the hint to list the commits under `commits`.
- `built_with` is the PDK the layouts were drawn and checked on. Used at another accepted commit, `lib status` notes
  it: `drawn on 8afc8346a57f, used at 0c4e2b7a1f30` (check the layouts again).
- A project with no PDK does not fit a package that has `[pdk]`. A PDK with no version (a plain directory, not
  installed by ciel or volare) does not fit one that names commits or `built_with`, since its commit cannot be
  checked: install it with `pdk fetch`. A format 1 id's 12-character commit is matched against the package's full
  commits; nothing is looked up on the network.

A package that does not fit is refused when it is added, updated or locked: `sar_common 1.3.1 does not fit this
project's PDK: sky130A@0c4e2b7a1f30 is not among commits 8afc8346a57f (required by demo -> analog_ip 1.2.0 (...) ->
sar_common 1.3.1 (...))`. Moving the project to a PDK a locked package does not accept is refused too
([PDKs](pdks.md#moving-a-project-to-another-pdk-version)). `pdk verify --pin` and `lib lock --format 2` pin the PDK
as installed whatever the packages accept (the pin records what is there), and `lib lock --format 2` prints a `!`
line per package that does not fit; `lib status` then keeps an issue for each, and `lib sync` installs it with a `~`
warning, until the package or the PDK moves.

### Trusting a dependency's code

Simulating a cell runs what its netlist tells ngspice, and a dependency is someone else's work: a `.control` block can
run `shell`, and an `.include` can read any file on the server. So, when a job is about to run, Undine refuses a
dependency's content that runs commands or reads files nobody looked at, unless a person has trusted that dependency
in the project. This holds for **every** dependency, plain git ones included.

| Tool | Refused in a dependency |
|---|---|
| ngspice (simulation, sweeps, spec checks) | `.control` ... `.endc` blocks (any card that starts with `.control`, as ngspice reads it), `*#` lines, and `.include`, `.lib` and XSPICE `file=` paths that are absolute outside the PDK, have a `..` part, or still hold a `$` or `~` |
| LVS (KLayout, Magic + Netgen) | the same paths (no control code runs there) |
| Verilator (RTL simulation) | `$c` and `$system` wherever they appear (also as a macro's argument), `` `systemc_* ``, DPI imports, `` `include `` / `$fopen` / `$dumpfile` / `$readmem*` / `$writemem*` paths that are absolute, have a `..` part or a `~`, and `` `define `` bodies that build such code |
| LibreLane (a flow whose top library is a dependency) | what Verilator's simulation refuses, before anything is copied into the run |
| Verilator lint | `` `include `` paths only |
| Xschem's netlister (the netlist view's Xschem backend) | `tcleval(...)` (also written with backslashes, such as `tcl\eval(`) in any attribute (and `expr(` with `[` or `$`) of a dependency's `.sch` or `.sym` |

A line counts as the dependency's when it comes from a cell of the dependency or was formed from one of its symbols
(a symbol's `format` or `template` placed in your schematic). A relative path without `..` passes, and so does an
absolute one under the PDK's directory: PDK model files are trusted as the PDK. The netlist view with Undine's own
backend only warns (`not trusted: would be refused in a simulation: ...`). A refusal names every finding, the cell,
the dependency and what to do:

```
ip/opamp (dependency analog_ip 1.2.0, not trusted): code block s1 line 1: .control; a person can trust analog_ip@1.2.0 in this project (undine lib trust demo ip, or Trust... in Undine); an agent cannot
```

**Trusting** is a person's decision, made for one version in one project:

- From the command line: `lib trust PROJECT NAME` lists what it found and asks (`trust analog_ip@1.2.0 in demo?
  [y/N]`); `--yes` does not ask; with no terminal and no `--yes` it refuses. `lib trust PROJECT NAME --untrust` takes
  it back.
- In the browser: **Trust...** in the refusal (simulation, LVS and RTL panels), or the shield button on the
  dependency's row in the library browser, opens a dialog listing what it found, by file and line; **Trust** trusts
  exactly the version shown (a sync or an update in between makes it ask again).
- An agent cannot: no MCP tool, `undine call` command or client method trusts anything, and agents are told to ask
  you ([Agent and MCP](assistant.md)).

Trust is kept in `project.json` as a key per version: `"trusted": ["analog_ip@1.2.0", "ip@229914698b1f"]`, the
package name and version, or for a dependency without a manifest its local name and commit. `lib update`,
`lib remove` and `lib lock` take the key out when they change or drop that dependency, so a new version is refused
until it is trusted again; `lib sync` takes out the keys of what it removes, and adding a package whose content the
lock did not have takes out a key of that name left from earlier (any repository can claim a name and version). The
changesets' working copies use the project's trust only, as it is now, so trust a person gives or takes back reaches
an agent's open changeset; trust is given in the project, never in a changeset preview (the refusal there says so
instead of offering **Trust...**).

`lib status` notes each installed dependency that has such content and is not trusted, and shows `trusted` or `not
trusted` on its row; `lib add` and `lib update` print a `~` line for each.

!!! warning "What trust does not cover"

    The rule is applied when Undine runs a job. Xschem opened on the project outside Undine, or a process with a shell
    that edits `project.json`, is not checked. Trust is bound to a package's name and version, not its commit: an
    `undine.lock` edited by hand to another commit of the same version, then synced, keeps the trust. Review
    `undine.lock` changes as you review code.

### The include file set and the lock

`[files] include` lists what the package consists of. In a format 2 lock, `lib add`, `lib update` and `lib lock`
install only those files (and the manifest), and the entry records it with `"files": "include"`: its `tree` hashes
the included files only, the same on every machine. A file outside `include` (tests, CI scripts, a large
characterisation run) is never fetched into the store nor installed.

- A pattern matches the whole path below the package's directory, with `/`: `*`, `?` and `[...]` stay within one
  part, so `*.sch` matches top-level files only; `**` stands alone between slashes and matches any number of parts
  (`rtl/**`). No leading `/`, no `..`, no empty part (write a directory as `dir/**`).
- The selection must hold at least one top-level cell (`.sch`, `.sym`, `.oas`, `.sv` or `.v`), and every exported
  cell.
- A dependency locked before (an entry with no `files` key) keeps every file of its directory until `lib update` or
  `lib lock` writes it again; `lib sync` never changes an entry. In a format 1 lock every file is installed.

The format 2 lock keeps the direct dependencies in `libraries` and the packages reached only through manifests in
`packages`, with their versions, local names and dependencies ([undine.lock](../reference/data.md#undinelock)).

## undine.lock

Besides the dependencies, the lock records what else the design depends on: the PDK version (the id `project.json`
pins, see [How a project pins its PDK](pdks.md#how-a-project-pins-its-pdk)) and the versions of
KLayout, Xschem (whose `devices/` symbols schematics use), ngspice, Magic and Netgen. A new project starts locked.
A tool's version is the one of the executable Undine finds first. With `$UNDINE_TOOLS` set (`pixi run` sets it), it
looks in `$UNDINE_TOOLS/bin`, then the bin directory of the Python environment Undine runs in, then `PATH`;
`~/undine-tools` is not searched. With `$UNDINE_TOOLS` unset, it looks in the Python environment Undine runs in (a
pixi, conda or virtual environment; not a system Python), then `~/undine-tools/bin`, then `PATH`.

The lock has two formats. Every command writes **format 1** unless the lock is format 2 already: a dependency is
pinned by its commit and a `sha` over the files `git archive` gave, the PDK by its id, the tools by their banners.

```json
{
  "format": 1,
  "pdk": "sky130A@8afc8346a57f",
  "tools": {"klayout": "KLayout 0.30.12", "xschem": "XSCHEM V3.4.5", "ngspice": "ngspice-47 : ...", "magic": "8.3.678", "netgen": "Netgen 1.5.272"},
  "libraries": {
    "analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib",
                  "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "sha": "e6d28176…"}
  }
}
```

**Format 2** pins more, and only two commands write it: `pdk verify PROJECT --pin` and `lib lock PROJECT --format 2`
(an older Undine refuses a format 2 lock rather than rewrite it, so make sure everyone on the project has this
version first):

```json
{
  "format": 2,
  "pdk": {"id": "sky130A@8afc8346a57f", "family": "sky130", "variant": "sky130A",
          "commit": "8afc8346a57fe1ab7934ba5a6056ea8b43078e71",
          "source": "ciel static-web:https://fossi-foundation.github.io/ciel-releases",
          "libraries": ["sky130_fd_pr", "sky130_fd_sc_hd"],
          "tree": {"libs.tech": "sha256:41c0…", "libs.ref/sky130_fd_pr": "sha256:9a7e…", "libs.ref/sky130_fd_sc_hd": "sha256:0f3b…"}},
  "tools": {"undine": "0.1.0 (3f9c2e1)", "pixi_lock": "sha256:13be…",
            "klayout": {"version": "0.30.12", "from": "pixi"}, "xschem": {"version": "3.4.5", "from": "pixi"},
            "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
            "netgen": {"version": "1.5.272", "from": "pixi"}},
  "libraries": {
    "analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib",
                  "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:e6d28176…"}
  }
}
```

- **Dependencies.** In a format 2 lock, `lib add`, `lib update` and `lib lock` pin a dependency by `tree`: a hash of
  its files read straight from git's objects, the same on every machine whatever `.gitattributes` or `core.autocrlf`
  say ([the exact rule](../design/packages.md#index)). An entry already locked keeps its `sha` until one of those
  commands writes it again; `lib sync` never rewrites an entry.
- **Packages.** An entry of a [package](#packages) resolved by one of those commands also records its `package`,
  `version`, local `names`, its `deps` (`{local name: package}`) and `files: "include"`. Packages only other packages
  depend on are kept in a table of their own, `packages`, keyed by package name
  ([Data layout](../reference/data.md#undinelock)). `lib sync` installs every entry under each of its names.
- **PDK.** The full commit, where it came from, its libraries and a hash per part, so that a PDK changed after it was
  installed is noticed ([Pinning the PDK by its files](pdks.md#pinning-the-pdk-by-its-files)).
- **Tools.** Undine's own version and commit, the hash of its `pixi.lock` (null when Undine runs outside its pixi
  environment), and per tool the version number and where the executable came from (`pixi`, `UNDINE_TOOLS`,
  `undine-tools` for `~/undine-tools/bin`, `env` for another conda or virtual environment, `PATH`).

`lib status` (and the **Locked** / **N lock issues** badge at the top of the window) lists everything that differs here.
Three kinds of line:

- **Issues** (`!`; they make the badge show a count and `lib status` exit with code 1): a dependency that is not
  locked as project.json names it (`unlocked`), missing, at another commit or modified, one that project.json no longer
  names but that is still installed or locked (`leftover`; `lib sync` removes it), the PDK missing or not as the lock
  pins it, no undine.lock, a library linked from a directory outside the project, and extra `symbol_paths` (both are
  not versioned with the project). For packages also: one that does not fit the locked PDK (or the one project.json
  asks for), a project cell named like a package's cell, a lock that does not install what a package needs (a
  package it depends on missing from the lock, or a local name taken twice: `lib lock` resolves it), and a
  `trusted` entry in project.json that is not a `name@version` key.
- **Warnings** (`~`): a tool of another version or origin, shown with the executable found here, e.g.
  `klayout: locked 0.28.17, here 0.30.12 from PATH (/usr/bin/klayout)`. Often it is not an upgrade but another copy
  of the tool found first (the CLI and the server may start from different environments, with or without
  `$UNDINE_TOOLS`). pixi pins the tools, and a patch release must not block anyone, so a tool never makes the lock
  fail. Also another Undine version, and a format 2 lock whose `pixi_lock` is null (the tools were not pinned).
- **Notes**: the PDK is unverified, unchecked or not pinned ([PDK states](pdks.md#pdk-states)), another commit of the
  same Undine version or another `pixi.lock`, and for format 1 that it records no origins. For dependencies: one
  whose content a simulation would refuse and that is not [trusted](#trusting-a-dependencys-code), a package with no
  `[pdk]` (or a plain dependency: PDK compatibility not checked), a package used at a commit other than its
  `built_with`, a project cell named like a plain dependency's cell, and a dependency name `project.json` already
  has that shadows a folder of Xschem's library or of `symbol_paths`.

A dependency's row shows its package and version and whether the project trusts it (`-> bias_gen 0.3.2` for a
package under another local name); a package another one brought in has its chain on the next line:

```
  PDK                  pdk        unverified     sky130A@8afc8346a57f
  analog_ip            dependency ok             https://github.com/me/analog_ip.git v1.2.0 @ 229914698b1f  analog_ip 1.2.0, trusted
  sar_common           dependency ok             https://github.com/me/sar_common.git v1.3.1 @ 5b1c0e2a9d11  sar_common 1.3.1, not trusted
                       via demo -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) -> sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11)
~ klayout: locked 0.30.12 from pixi, here 0.30.12 from PATH (/usr/bin/klayout)
  note: PDK sky130A@8afc8346a57f: unverified (trust on first use): it matches the hashes pinned in undine.lock, which no PDK profile publishes yet
  note: library sar_common: 1 cell with .control (not trusted): a simulation using it needs `python -m undine lib trust demo sar_common`
as locked
```

The lock records versions only, not paths, which differ from machine to machine. After an intended upgrade of the
tools, `lib lock` records the versions in use; in a format 2 lock it records the tools only and leaves the PDK's pin
alone. After a `git pull` that changed `project.json`'s dependencies, run `lib lock` too, then `lib sync`
([Sync installs the lock as it is](#sync-installs-the-lock-as-it-is)). To move the project to another PDK version, use
`set-pdk` rather than `lib lock`: it also updates `project.json` and keeps the old version in `pdk_history`
([Moving a project to another PDK version](pdks.md#moving-a-project-to-another-pdk-version)). A command that made the
lock says so: `created undine.lock (format 1)`.

Every command that reads the lock (`lib`, `pdk`, `set-pdk`, the lock dialog) refuses a lock of a newer format (3 or
more), made by a newer Undine, and changes nothing: update Undine (`git pull`, then `./setup.sh`). A lock that is not
valid JSON, or whose `format` no Undine writes, is refused the same way: fix it, or restore it with
`git checkout undine.lock` ([troubleshooting](../troubleshooting.md#undinelock-has-format-3-made-by-a-newer-undine)).

Keep `undine.lock` in version control; `.undine/` (the installed dependencies) is ignored by
[`git-setup`](../reference/data.md#version-control-with-git).
