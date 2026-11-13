# Packages and PDKs (design)

Status: design, decided on 2026-11-05 ([Decisions](#decisions)).
**Phases 0, 1** (the store and lock format 2) **and 2** (manifests, transitive dependencies, PDK compatibility
and the trust rule) **are done**; **phases 3 to 6 are not implemented**. [Phases](#phases) says what each phase delivers and, per phase, what exists
today; what a user can do with PDKs now is in the [PDKs guide](../guide/pdks.md), and with dependencies and packages
in [Libraries and dependencies](../guide/libraries.md).

The document has several baselines. [Today](#today) describes the code when this design was written, and its line
numbers are from then. In [Phase 0 status](#phase-0-status), the items done in the first part of phase 0 were checked
against the code right after that part, and the others against the code when phase 0 was finished; their line numbers
are from those points.
[Phase 1 status](#phase-1-status) describes the code of phase 1 by module and function, not by line, and
[Phase 2 status](#phase-2-status) does the same for phase 2.

## Goal

Libraries and PDKs are shared like cargo crates or npm packages: a project asks for `analog_ip ^1.2` on `sky130A`,
Undine finds a version that fits, fetches it once into a shared store, pins exactly what it got, and every checkout of
the project gets the same files, checked by hash. Publishing a library is a pull request to an index repository.

Decided with the user:

- The registry is a **git index repository** (like crates.io-index or a Homebrew tap): name + version → git URL,
  commit, content hash. Publishing is a PR to the index. Private indexes are possible. It builds on today's git
  dependencies.
- **PDKs**: Undine wraps [ciel](https://github.com/fossi-foundation/ciel). The PDK is a special root dependency: ciel
  fetches and installs it, Undine pins the full commit and a tree hash, verifies it, and offers to fetch it (after the
  person confirms) when it is missing. The process knowledge in `pdkprofile.py` becomes **PDK profile** packages.

Not goals: hosting a registry service (an index is a git repository, files stay in git repositories; a tapeout keeps
its own copy with `pkg vendor`, see [Fetch and store](#fetch-and-store)), installing EDA tools (pixi does that),
running a package's code outside simulation (a package is data: cells and a manifest; simulating its cells runs what
they tell the simulator, see [Security](#security)), several versions of one package in one project (see
[Resolution](#resolution)), signing (later, see [Decisions](#decisions)).

## Today

The table describes the code when this design was written. Rows changed since say so: in the first part of phase 0 the
*PDK* row (OSDI is no longer compiled into the PDK tree) and the *locking* row (the CLI and the server now take a
project lock); in the rest of phase 0 the *git dependencies* row (`format` checked, leftovers, `--dep-root`) and the
*netlist* row (a name clash refused). Phase 1 changed the *git dependencies*, *PDK* and *tools* rows again; they are
left as they were, and [Phase 1 status](#phase-1-status) says what they are now. Phase 2 changed the *git
dependencies* row once more (manifests, transitive packages, local names), the *symbol lookup* row (a package's own
dependencies are installed for it, under the names its schematics use) and the *netlist* row (each line is attributed
to the cell body and the symbol it came from); [Phase 2 status](#phase-2-status) says how.

| | Where | What it does / lacks |
|---|---|---|
| git dependencies | `store/deps.py` | `{git, rev, subdir}` in `project.json` `libraries`; `undine.lock` (`format: 1`) pins `commit` and `sha` (SHA-256 over every file's path and SHA-256, `tree_sha`, of the files `git archive` gave); installed read-only into `.undine/deps/<name>/`; `add` / `update` / `remove` / `sync` / `lock` / `status`. No manifest in the dependency, no version ranges, no transitive dependencies, no cache (a temporary bare repo per fetch). `read_lock` does not look at `format`. Since phase 0: `read_lock` refuses a `format` other than 1 (a newer one with "made by a newer Undine"), `sync` removes a dependency gone from `project.json` and `status` reports it as `leftover`, and HTTP takes a local repository only under a `--dep-root` |
| symbol lookup | `xschem.py:311-324`, `project.py:219-234` | `C {<lib>/<cell>.sym}` by the consumer's local library name: the schematic's own directory first, then the first hit on the search path. A dependency that places another library's symbols works only if the consumer added that library under the same name. The Xschem netlister backend (`schematic.xschem_netlist`) and Xschem opened outside Undine use the same path list (`XSCHEM_LIBRARY_PATH`) |
| netlist | `schematic.py:589`, `xschem.py:482` | a subcircuit is named by the symbol's file stem alone (`subckts[symname]`); two different cells of one name in one netlist: the first wins, silently (only a stand-in clash is refused, `schematic.py:527-530`). Since phase 0: two cells of one name that netlist to other lines are refused, naming both; the same lines are written once |
| layout | `layout.py:1340-1377` | placing a cell of another library copies it. A subcell of the same name with other content becomes `name$1`; a cell the layout already has under the placed name is kept as it is, whatever its content (`_copy_cell_from` returns nothing). The operation log keeps `src: {lib}` and the content `sha`, not the dependency's commit |
| PDK | `pdks/pdk.py` | found once at start in `$PDK_ROOT`, `~/.ciel`, `~/.volare` (or `--pdk-root`, which replaces them); `Pdk.version` is the full commit of the PDK's source (open_pdks for sky130 and gf180mcu, IHP-Open-PDK for ihp-sg13), but `Pdk.id` (`sky130A@8afc8346a57f`, 12 characters) is what `project.json`, `undine.lock` and provenance record. No tree hash, no install, no rescan. A missing PDK opens the project with no PDK and `status` says only "locked X, here None". Installed trees are written to after install: sky130's `libs.tech/klayout/python/cells/__pycache__` (when the PCell code is imported), IHP's `libs.tech/ngspice/osdi/*.osdi` (from `scripts/osdi.sh`; since phase 0 it compiles into Undine's cache, `~/.cache/undine/osdi/<PDK id>/`, and leaves the PDK as installed) |
| process knowledge | `pdks/pdkprofile.py` | one Python `Profile` per family (layers, decks, models, PCell library, stack rules); not versioned with the PDK. Supported commits are written in three places (`pcell.SUPPORTED`, `pixi.toml`, `setup.sh`) |
| tools | `tools.py:43-63`, `tools.py:116-131` | the lock compares version banners as text; which binary answers depends on the search order (`$UNDINE_TOOLS` or `~/undine-tools/bin` before `PATH`), so CLI and server can disagree. Here klayout answers 0.28.17 from `~/undine-tools/bin` while `/usr/bin` has 0.30.12, and neither is pixi's |
| provenance | `store/provenance.py` | job inputs record cell content hashes (the netlist hash covers generated subcircuits, the layout hash copied cells), the PDK id, tool banners and the KLayout deck's SHA-256 (a changed deck makes results outdated). Model files, PCell code and the rest of the PDK are not hashed |
| changesets, agents | `api.py:1592-1595`, `agent.py:20` | dependency changes are written straight into the project, refused in a changeset preview; agents cannot add dependencies. `project.json` and `undine.lock` merge as text (`merge.md`) |
| locking | `server/api.py`, `store/deps.py` | the server holds the server-wide `srv.lock` around dependency changes; the CLI holds no lock when it writes `project.json` and `undine.lock`, so a CLI and a server writing one project race. Since phase 0: a fetch holds only the project's `.undine/deps.lock`, and every writer of the two files (requests, the CLI, adopt and revert) takes `meta_lock`, an flock on `.undine/project.lock`; `srv.lock` is held only to swap the files in |

Problems found on the way, fixed in [phase 0](#phases): `read_lock` accepts any `format`; `set-pdk` writes the lock
only when one exists; `sync` leaves the files and lock entry of a dependency removed from `project.json` (and the search
path still finds them); HTTP `add` / `sync` / `update` hold the server-wide lock during a git fetch of up to 600 s; the
CLI writes `project.json` and the lock with no lock at all; HTTP accepts `file://` and absolute paths, so any
repository on the server's disk can be read; the netlister lets the first of two same-named cells win silently;
`scripts/osdi.sh` imports a module that no longer exists. The first part of phase 0 fixed the fetch under `srv.lock`, the CLI with no
lock and `osdi.sh`, and the rest of phase 0 the others; [Phase 0 status](#phase-0-status) says where each fix is and
which tests cover it.

## Packages

A **package** is one library (a directory of cells, as today) plus a manifest `undine-package.toml` at its top. A git
repository may hold several packages in subdirectories. TOML because people write it by hand; Undine reads it with
Python's `tomllib`.

```toml
[package]
name = "analog_ip"                 # unique in its index; the default local library name; [a-z][a-z0-9_]*
version = "1.2.0"                  # semver
description = "Bandgap, LDO and comparator for sky130"
license = "Apache-2.0"             # SPDX expression; required to publish
authors = ["Kadomoto <k@example.org>"]
repository = "https://github.com/me/analog_ip.git"

[pdk]
family = "sky130"
variants = ["sky130A"]             # sky130A and sky130B have other metal stacks
compat = ["sky130/2025"]           # compatibility tags (defined by the PDK profile package), or:
# commits = ["8afc8346a57fe1ab7934ba5a6056ea8b43078e71"]
built_with = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"   # the PDK the layouts were drawn and checked on

[dependencies]
sar_common = "^1.2"                                # from the index this package is published in
bias = { package = "bias_gen", version = "~0.3" }  # placed as bias/<cell>.sym in this package's schematics
tech_cells = { version = "^2", index = "acme" }    # another index

[exports]
cells = ["bandgap", "ldo", "comparator"]          # the public cells; the others are installed and netlisted too, but internal

[files]
include = ["*.sch", "*.sym", "*.oas", "*.netlist.yaml", "*.intent.yaml", "rtl/**", "README.md", "LICENSE"]

[checks]                                          # how `pkg check` runs DRC / LVS on this package's cells
drc = { deck = "sky130A_mr.drc", options = { feol = true, beol = true, density = false } }
waive = [{ cell = "unit_cap", rules = ["m1.2"], reason = "abutment cell: clean once arrayed" }]

[renames]                                         # old cell name -> new, per version that renamed it
"1.2.0" = { comp = "comparator" }
```

- **PDK compatibility** is a hard constraint. PDK commits have no order, so there are no ranges: a package lists
  commits, or compatibility tags. A tag (`sky130/2025`) is a promise, made by the PDK profile package (not by the
  index), that the commits under it give the same layers, rules, PCell geometry **and device models** (model files and
  corners). Tags have one level: a commit that changes a model, even with the same geometry, gets another tag, since
  the offsets, bandwidths and spec results of a published cell would change under it; there is no layout-only tag.
  The profile's `[[pdk]]` entries carry per-library tree hashes (see [PDK profiles](#pdk-profiles-as-packages)), so the
  review of an update or of `set-pdk` lists the commits whose model files differ. PCell geometry also depends on the
  generator: the PDK's PCell code run in Undine's `pcell` pixi environment (gdsfactory, kfactory), which `GEN_VERSION`
  records (it is in every variant's name). `built_with` is always one exact commit.
- **Binary layouts** stay one `.oas` file per cell, as in a project library, so a new version shares the unchanged
  cells with the old one (see [Store](#fetch-and-store)). No git LFS: an LFS pointer file in a package is refused with
  a message.
- **Checks** (`[checks]`): the deck and its options for the package's DRC (unit and abutment cells are often not clean
  alone, and density and antenna rules apply only at the top level), and waived rules per cell, each with a reason.
  `pkg check` and the review show the waivers.
- A published package depends only on index packages. A project may still use plain git dependencies (as today).

*Implemented (phase 2):* `store/manifest.py` reads this file (with `tomllib`, or `tomli` on Python 3.10) and refuses
it on the first problem, naming the key path and what to write (`<where>: undine-package.toml: <key path>: <what>:
<fix>`); unknown keys are refused, `[checks]` and `[renames]` are accepted as tables and not used yet. With no index
yet, `[dependencies]` takes a git form, and the index forms above are parsed but refused at resolution: see
[Phase 2 status](#phase-2-status).

## Index

An index is a git repository:

```
config.json                      {"name": "undine", "format": 1}
an/al/analog_ip.jsonl            one line per version, appended, never rewritten (except "yanked")
pr/of/profile_sky130.jsonl
3/i/inv.jsonl                    names of three letters: 3/<first letter>/
2/op.jsonl                       names of one or two letters: 1/ or 2/
```

One file per package, one JSON line per version. The path follows crates.io-index: a name of four letters or more
goes under its first two and next two letters, a name of three under `3/<first letter>/`, of one or two under `1/` or
`2/`.

```json
{"name": "analog_ip", "vers": "1.2.0", "git": "https://github.com/me/analog_ip.git", "subdir": "lib",
 "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:e6d28176...",
 "pdk": {"family": "sky130", "variants": ["sky130A"], "compat": ["sky130/2025"], "built_with": "8afc8346a57f..."},
 "deps": [{"name": "sar_common", "req": "^1.2"}, {"name": "bias", "package": "bias_gen", "req": "~0.3"},
          {"name": "tech_cells", "req": "^2", "index": "git@github.com:acme/undine-index.git"}],
 "exports": ["bandgap", "ldo", "comparator"], "license": "Apache-2.0", "size": 1843200,
 "check": {"url": "https://github.com/me/analog_ip/releases/download/v1.2.0/undine-check.json", "sha256": "5f2a..."},
 "yanked": false}
```

`tree` is a hash over the commit's own bytes, the same on every machine: for each file the manifest includes (the
manifest too), in path order, its path below `subdir`, its mode (`100644` or `100755`) and the SHA-256 of its blob as
git stores it, read with `git ls-tree -r` and `git cat-file`. No `git archive`: archive applies `.gitattributes`
(export-subst, export-ignore, eol) and the machine's `core.autocrlf`, so a publisher, the index CI and a consumer
could hash one commit differently. A symlink or submodule entry is refused.

The encoding, exactly (phase 1; a golden test, `test_tree_hash_golden_and_order_by_utf8_bytes` in `test_cas.py`, pins
it):

- A file's **object hash** is the SHA-256 of its raw bytes: the blob's content, with no git header (`git hash-object`
  would add `blob <size>\0`). It is written as 64 lower-case hex characters.
- The **tree hash** is `"sha256:"` + the SHA-256 of the concatenation, for each file in the order of the UTF-8 bytes
  of its POSIX path below `subdir`, of the UTF-8 bytes of `f"{mode} {hex} {path}\0"`: `mode` is `100644`, or `100755`
  for an executable; `hex` the object hash; `path` relative, `/`-separated, with no `.` or `..` part.
- The store records a tree as `trees/sha256/<hex>.json`, `{path: [mode, hex]}`, named by the hex of its tree hash.
- A PDK part hash ([Lockfile](#lockfile)) runs the same encoder over the files on disk, with the owner's execute bit as
  the mode.

The commit and the tree are what pins a
version's content; there is no signature (signing is [later](#decisions)). A version's commit and tree never change;
**yanking** sets `yanked: true` (a PR too): resolution skips it, an existing lock keeps it and `status` warns. `check`
is the link to the publisher's check report and its SHA-256 (see [Publishing](#publishing)).

**The default index** is `undine`: a repository in the kadomoto-lab GitHub organisation,
`https://github.com/kadomoto-lab/undine-index.git` (the repository name is a placeholder; the repository is to be created in
[phase 3](#phases)). Its URL is in Undine's configuration. For now kadomoto reviews its PRs.

**Several indexes.** More indexes are named in
`$UNDINE_HOME/config.toml` (`UNDINE_HOME` defaults to `~/.undine`) and, so that a clone of the project works, in
`project.json`:

```json
"indexes": {"acme": "git@github.com:acme/undine-index.git"}
```

A requirement with `index` is resolved in that index. One without it means the index of the package that declares it
(cargo's rule), and for the project's own requirements the default index. So a package from `acme` that depends on
`sar_common` gets acme's `sar_common`, never one someone published under that name in the public index. An index is
known by its URL: the nickname is local, `pkg pack` writes the URL into the index line, and the lock records the URL.
One package name coming from two indexes in one project is an error that names both. Access is git's: ssh keys or a
credential helper (`GIT_TERMINAL_PROMPT=0` stays, there is no password prompt). Each index is cloned once into
`$UNDINE_HOME/store/index/<sha256 of URL>/` and refreshed only on request (`pkg index update`, the **Refresh** button)
or when a resolution needs a version it does not have and the person agrees.

## Resolution

1. Start from the project's dependencies (`project.json` `libraries`, now also `{"package": ..., "version": "^1.2"}`)
   and the PDK.
2. For each package, the highest non-yanked version that satisfies every requirement on it, is compatible with the
   project's PDK commit and variant, and whose own dependencies resolve (backtracking; the graphs are small).
3. **One version per package name per project.** Two requirements with no common version are an error that names both
   chains (`app -> analog_ip 1.2 -> sar_common ^1.2` vs `app -> sar_common ~1.0`). Reason: SPICE subcircuits and layout
   cells are named by cell name, not by library, so two versions of `comparator` in one netlist collide. Several
   major versions by prefixing cell names are left for later ([Decisions](#decisions) 3).
4. **Cell names are disjoint.** The same reason applies across packages: the cells of all resolved packages (exported
   and internal, since internal cells are installed and netlisted all the same), of the project's libraries and of the
   PDK libraries must have different names. A clash is a resolution error that names the cell and both packages
   (`comparator: analog_ip 1.2.0 and sar_common 1.3.1`). One package placed under two local names is one package, not
   a clash. Prefixing subcircuit and layout cell names by library instead was not chosen ([Decisions](#decisions) 4).
5. **Local names.** Each library declares the local names its schematics use (`bias/x.sym`) and the package each one
   means; the lock keeps that table. Local names are project-wide: each resolved package is installed under every
   local name it is placed by (`.undine/deps/<local name>/`, hard links into the store, so a second name costs
   nothing), and the symbol search path stays one list of directories. That is what the Xschem netlister backend and
   Xschem opened outside Undine need: `XSCHEM_LIBRARY_PATH` is one global search by path and cannot express a
   per-library table. So a dependency's own dependencies work without the consumer adding them, a package can be
   placed under another local name (`bias` → `bias_gen`), and a diamond (`A -> C`, `B -> C`) installs `C` once per
   local name. Two libraries that give one local name different packages are refused, naming both. A local name may
   not be a PDK library name (as `deps.add` checks today) or the name of a project library. Per-library tables (one
   local name meaning two packages) were not chosen ([Decisions](#decisions) 5).
6. PDK compatibility is checked for every package, not just direct ones. A project that changes its PDK (`set-pdk`)
   is refused when a locked package is not compatible, with the list.

*Implemented (phase 2), for git requirements only:* with no index, each requirement names one git commit, so there is
one candidate per requirement and no backtracking. `store/resolve.py` walks the graph from `project.json`'s
`libraries`: a version is (package name, tree), the manifest's `version` a label checked against an optional `^`,
`~`, `=`, bare or `*` requirement. Every problem of the walk is collected and refused once, ending "nothing was
changed": two requirements on one package that do not agree (both chains, with version, rev and commit), a package
name other than the one asked for, a manifest-less target (only `project.json` may name a plain git dependency), an
index form, a local URL named by a remote requirer, a bad manifest, a local name claimed for two packages, a reserved
local name, a cell clash and a PDK the package does not accept. `add` and `update` re-resolve only the named
requirement's subtree; every other locked package is held as locked, read from the store or its install and never
fetched (`store/locked.py`). Cell names (rule 4) are compared casefolded among logic views (`.sch`, `.sym`, `.sv`,
`.v`): a project library contributes only those, so a layout copy placed from a package never clashes, and a
package's layout-only cell is excused against its own closure and the PDK. Local names (rule 5) are compared
casefolded, and besides project and PDK library names they may not be `devices` or a subdirectory of Xschem's library
or of a `project.json` `symbol_paths` entry, since `.undine/deps` precedes them on the one symbol path. PDK
compatibility (rule 6) is `pdks/compat.py`: family, variants, full commits and `built_with`; tags are not known yet
(phase 6). Two of the [phase 2 changes for plain git dependencies](#phase-2-status) come from here: `status` reports a
project-library logic cell named like a dependency's logic cell (an issue against a package with a manifest, a note
against a plain dependency; resolution refuses a clash only when a package with a manifest is on one side), and
`lib add` refuses a new local name that is reserved as above, while a name already in `project.json` is only noted.

## Lockfile

`undine.lock` becomes `format: 2`.

```json
{
  "format": 2,
  "pdk": {"id": "sky130A@8afc8346a57f", "family": "sky130", "variant": "sky130A",
          "commit": "8afc8346a57fe1ab7934ba5a6056ea8b43078e71",
          "source": "ciel static-web:https://fossi-foundation.github.io/ciel-releases",
          "libraries": ["sky130_fd_pr", "sky130_fd_sc_hd"],
          "tree": {"libs.tech": "sha256:41c0...", "libs.ref/sky130_fd_pr": "sha256:9a7e...",
                   "libs.ref/sky130_fd_sc_hd": "sha256:0f3b..."},
          "verified": "profile_sky130 1.0.0"},
  "profile": {"package": "profile_sky130", "index": "https://github.com/kadomoto-lab/undine-index.git", "version": "1.0.0",
              "commit": "5b1c...", "tree": "sha256:77aa..."},
  "packages": {
    "analog_ip": {"package": "analog_ip", "index": "https://github.com/kadomoto-lab/undine-index.git", "version": "1.2.0",
                  "git": "https://github.com/me/analog_ip.git", "subdir": "lib",
                  "commit": "229914698b1fb16a629bb73109b39eb1aba40eae", "tree": "sha256:e6d28176...",
                  "names": ["analog_ip"], "deps": {"sar_common": "sar_common", "bias": "bias_gen"}},
    "bias_gen": {"...": "..."}, "sar_common": {"...": "..."}
  },
  "libraries": {"old_ip": {"git": "...", "rev": "v1", "commit": "...", "tree": "sha256:..."}},
  "tools": {"undine": "0.1.0 (3f9c2e1)", "pixi_lock": "sha256:13be...",
            "klayout": {"version": "0.30.12", "from": "PATH"}, "xschem": {"version": "3.4.5", "from": "pixi"},
            "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
            "netgen": {"version": "1.5.272", "from": "pixi"}}
}
```

- **Format.** Phase 0 makes Undine refuse a lock whose `format` it does not know, with "made by a newer Undine". Before
  that, an older Undine reading format 2 would report the `pdk` object as an issue, and its `set-pdk` would overwrite
  `pdk` with a string. So format 2 is written only by an explicit command that needs it, never as a side effect of
  another change, and those commands come from phase 1, released after phase 0's check has been out: a collaborator
  on an older Undine gets a refusal, not a corrupted lock. In phase 1 the writers are `pdk verify PROJECT --pin` and
  `lib lock PROJECT --format 2`, both through one function (`pdkops.upgrade`); `pkg add` and `pkg migrate` write it
  too once they exist, in [phase 3](#phases). Without `lib lock --format 2`, a project with no PDK could not reach
  format 2 in phase 1 (both commands refuse a PDK that is not installed here: `pdk fetch` first). A format 1 lock is read and, by the commands of today
  (`lib add`, `set-pdk`, ...), still written as format 1. *Implemented (phase 0):* `read_lock` refuses a larger
  integer `format` with "made by a newer Undine", and any other value but 1 (a string, `true`, `1.0`, `0`) as one
  "no Undine writes"; a lock without `format` is read as format 1. Every command that changes the project reads the
  lock first, so a refused lock leaves `project.json`, `undine.lock` and `.undine/deps/` as they were. *Implemented
  (phase 1):* `read_lock` reads formats 1 and 2 and refuses 3 or more the same way ("this one reads formats 1 and
  2"). A format 2 lock stays format 2 under every command, and a command that changes it (`lib add`, `update`,
  `set-pdk`, `lib lock`) keeps what it does not change.
- **Packages** are keyed by package name, which is unique in a project because one name from two indexes is refused;
  an entry is (index URL, name) and `sync` checks both. `names` are the local names it is installed under.
- **PDK**: the full commit (open_pdks for sky130 and gf180mcu, IHP-Open-PDK for ihp-sg13), ciel's source, the
  libraries fetched, and one tree hash per part, so a PDK installed with more libraries still verifies. The hash
  leaves out what gets written into a PDK tree after install: `__pycache__/` and `*.pyc` (from importing the PCell
  code; the PCell worker also runs with `PYTHONDONTWRITEBYTECODE=1` from phase 1) and `libs.tech/ngspice/osdi/`
  (compiled there by `scripts/osdi.sh` on installs set up before phase 0). The expected hashes come from the profile package's
  `[[pdk]]` entry for that commit (`verified` names it), so a PDK is checked against published hashes, not against
  whatever the first machine had. Hashing one sky130 variant (1.1 GB) takes seconds once; Undine keeps the result
  with file stamps in `$UNDINE_HOME/store/pdk/` and re-reads a file only when its stamp changed. `project.json` `pdk`
  keeps the id (the requirement); the lock is the pin, and `status` reports them disagreeing. `set-pdk` always writes
  both.
- **Tools** (decided): tools stay out of the package system; pixi pins them (Undine's `pixi.lock`). The lock records
  Undine's version and commit (`0.1.0 (3f9c2e1)`, `+dirty` after the commit when tracked files are changed; another
  version warns, another commit of one version is only a note), the SHA-256 of its `pixi.lock` (`null` when Undine was
  installed without pixi; `status`
  then says the tools are not pinned), and per tool its version **number** and where the binary came from (`pixi`,
  `UNDINE_TOOLS`, `undine-tools` for `~/undine-tools/bin`, `env` for the server's own conda or virtual environment
  that is not pixi's, `PATH`): `tools.find` looks in `$UNDINE_TOOLS` or `~/undine-tools/bin` before `PATH`, so the
  `pixi.lock` hash alone says nothing about which binary answered. Another number or origin is a warning in `status`,
  never a refusal. A format 1 lock records the banners only: it has no `undine` or `pixi_lock`, its tools no origin,
  and `status` notes that it cannot compare them. The stale `tools.klayout` of `project.json` is dropped (no longer
  written or read; an existing key is left on disk).
  The number is parsed from today's banners, one rule per tool (a banner no rule matches is recorded as text with no
  number, and `status` says so):

  | Tool | Banner today | Rule |
  |---|---|---|
  | klayout (`-v`) | `KLayout 0.28.17` | `KLayout (\d+(\.\d+)+)` |
  | xschem (`-v`) | `XSCHEM V3.4.5` | `XSCHEM V(\d+(\.\d+)+)` |
  | ngspice (`-v`) | `ngspice-47 : Circuit level simulation program` | `ngspice-(\d+(\.\d+)*)` |
  | magic (`--version`) | `8.3.678` | `^(\d+(\.\d+)+)$` |
  | netgen (`-batch quit`) | `Netgen 1.5.272` (after the build date is cut, as today) | `Netgen (\d+(\.\d+)+)` |

- `libraries` keeps plain git dependencies as today. In format 2 their `tree` is computed as an index tree is (from
  git's blobs, over every file of the subdir, or over the manifest's `include` when the dependency has a manifest);
  a format 1 `sha` (over `git archive` output) is kept until that entry next changes.
- *Implemented (phase 2):* with no index, the lock's new keys are fewer than the example above shows. `libraries` keeps
  every direct `project.json` git dependency: a manifest-less one, or a phase 1 one not resolved again, is exactly the
  phase 1 entry; one resolved in phase 2 with a manifest adds `files: "include"`, `package`, `version`, `names` (every
  local name, its key first) and `deps` (`{local name: package}`). `packages[package]` holds the packages reached only
  through manifests: `{package, version, git, rev, subdir?, commit, tree, files: "include", names, deps}`, with no
  `index` (phase 3); a sibling records its requirer's `git` and `rev`. One package name appears once across the
  `packages` keys and `libraries[*].package`. A tree covers the manifest's `include` set only when the entry says
  `files: "include"`; with no `files` key it covers every file of the subdir, as in phase 1, and `sync` never adds or
  removes the key. A format 1 lock never gets these keys: a manifest that names dependencies, or a package name other
  than its key, is refused there with "needs lock format 2: run `lib lock PROJECT --format 2` first", and that command
  converts and resolves in one write, or leaves the format 1 lock as it was. `sync` stays frozen over the whole
  graph: it installs every `libraries` and `packages` entry under each of its `names`, from the store when it can,
  and refuses an incomplete graph (a missing package, a name claimed twice or reserved) before anything is read. A
  lock edited by hand is still trusted as it stands, see the trust note under [Security](#security).

## Fetch and store

A shared, content-addressed store, `$UNDINE_HOME/store/` (`~/.undine/store/` by default; `UNDINE_STORE` moves the store
alone):

```
objects/sha256/e6/d28176...       every file, by its SHA-256, read-only
trees/sha256/e6d28176....json     a package version: {path: [mode, object hash]}
trees/archive/<sha>.json          the same for a format 1 entry, named by its `sha` (git archive's files)
git/<sha256 of URL>.git           bare repositories fetched from, kept for the next fetch
index/<sha256 of URL>/            index clones
pdk/<commit>-<variant>-<root>.json   PDK tree hashes and file stamps; <root>: the first 12 hex of the SHA-256 of the
                                  PDK's real path, so one commit under two roots keeps two records
                                  (nocommit-<root>-<name>.json for a PDK with no version)
build/<commit>/<variant>/osdi/    compiled OSDI models (no longer written into the PDK tree; local-<root> in place of
                                  <commit> for a PDK with no version)
projects/<sha256 of path>.json    the projects that added, updated, synced or locked a dependency: {path, synced}
tmp/                              every write lands here first and is renamed into place
.lock                             an flock: shared for a dependency operation, exclusive for `store gc`
```

The store is `$UNDINE_STORE` when set, else `$UNDINE_HOME/store`; `UNDINE_HOME` (default `~/.undine`) also holds the
connections. The variables, and `UNDINE_OFFLINE`, `UNDINE_STORE_LINK`, `UNDINE_CIEL` and `UNDINE_FETCH_MAX`, are in
[Data layout](../reference/data.md#the-store).

- **Fetch**: `git fetch --depth 1` of the commit into the kept bare repository (all refs only when the server will not
  serve a commit by itself, as today), then the files are read straight from git's objects (`git ls-tree -r -z`,
  `git cat-file`), as the [tree hash](#index) is defined; only regular files are taken. Every file goes into
  `objects/` and the tree is checked against the lock or the index entry before anything is installed. Unchanged
  `.oas` cells of a new version are already in the store: **per-file dedup**. *Implemented (phase 1, `store/gitsrc.py`):*
  as described, for plain git dependencies; the file count (100 000) and the size (`UNDINE_FETCH_MAX`, 1 GiB) are
  checked from `git ls-tree -l` before any blob is read. A format 1 entry keeps its `sha` over `git archive` output
  (phase 0's definition, `.gitattributes` and `core.autocrlf` applied): the archive runs in a throwaway repository
  that holds what phase 0's depth-1 fetch into an empty repository held, the commit fetched at depth 1 from the kept
  repository (a local read) and none of its refs, so that `export-subst` (`%d`, `%D`, `%(describe)`) expands as it
  did then, `%h` has the same length (git sizes an abbreviation by the objects it can see, which a kept repository
  that grew would change), and the `sha` comes out the same. A path with a `.git` part (any case, `.git.`, `git~1`),
  which git itself refuses in a tree, is refused by `blob_tree` and by every install.
- **Install**: each `.undine/deps/<local name>/` is made from the tree by hard links into `objects/` (copies across file
  systems, or with `UNDINE_STORE_LINK=copy`). The files are read-only; `store verify` re-hashes the objects, `status`
  re-hashes the installed trees as today. *Implemented (phase 1):* a copy is also made where the file system refuses
  a link (`EPERM`, `EMLINK` at its link limit, `ENOTSUP`); copies are read-only too. Every object is re-hashed before
  it is linked, and an object that fails is read again from git. A changeset's worktrees hard-link the installed
  dependencies too ([Changesets on git branches](changeset-branches.md)).
- **Sync is frozen**: `sync` installs exactly what the lock says and refuses a lock that no longer satisfies
  `project.json` (after a pull that changed a requirement), naming the entry and saying to run `pkg lock` or
  `pkg update`. Resolution happens only in `add`, `update`, `lock` and changeset operations, never as a side effect
  of a sync, so a sync never changes what evidence or an agent sees. Today's `sync` re-resolves a changed or unlocked
  dependency; that changes in phase 1 (`lib lock`, which exists, and `pkg lock` re-resolve on request).
  *Implemented (phase 1):* `lib sync` refuses before anything is read or fetched, naming each entry
  (`analog_ip: project.json asks for rev v2, undine.lock locks v1`, or `not in undine.lock`) and saying to run
  `lib lock PROJECT` or `lib update PROJECT NAME`; `lib lock` re-resolves what the lock does not pin as `project.json`
  names it (fetching it), and only that. A sync never rewrites a lock entry: a format 1 entry keeps its `sha` until
  `add`, `update` or `lib lock` writes the entry into a format 2 lock, where it gets a `tree`.
- **Offline** (`--offline`, `UNDINE_OFFLINE=1`, or no network): resolution uses the index clones as they are; `sync`
  works when every locked tree is in the store, else it names what is missing. A clone or pull of a project is
  followed by `sync` from the store, which needs no network for versions fetched before. *Implemented (phase 1):*
  `lib sync --offline` (or `UNDINE_OFFLINE=1`, or `{"offline": true}` over HTTP) installs from the store's records
  and objects, then from a kept repository that has the commit, and refuses naming every entry it has neither for
  (`offline, and not in the store: analog_ip @ 229914698b1f (sha e6d28176...)`); nothing is changed. A dependency
  installed before the store existed is imported into it by the first sync whose files match the lock, so it then
  syncs offline too.
- **PDK**: `pdk fetch` runs `ciel fetch --pdk-family <family> <commit> -l <lib>...` with the lock's source, into the
  first PDK root Undine scans that is a ciel root (`--pdk-root` when given, else `$PDK_ROOT`, else `~/.ciel`; ciel's
  `--pdk-root` is set to it), and refuses when none of the scanned roots is one (only `~/.volare`, say), since a PDK
  fetched elsewhere would not be found. ciel installs into its own store there (not copied); Undine then hashes it,
  compares with the lock and rescans the PDKs (new: rescan without a restart). ciel stays the installer; Undine never
  edits a PDK tree. *Implemented (phase 1):* the ciel family is taken from the variant (`ihp-sg13g2` →
  `ihp-sg13`); a root qualifies when it has `ciel/`, does not exist yet or is empty. A format 1 id, or a pin from a
  ciel install, assumes ciel's default source, `static-web:https://fossi-foundation.github.io/ciel-releases`; a pin
  from volare is refused. A 12-character id is expanded from the installed PDKs, else with `ciel ls-remote` when the
  person allows the network (`--network`, or the dialog's checkbox). The CLI asks before it runs ciel; over HTTP the
  fetch needs the confirmation token ([Security](#security)) and runs as a job. The CLI cannot rescan a running
  server: it says to use **Rescan PDKs** or `POST /api/pdks/rescan`.
- `store gc` removes objects no project on this machine locks (projects are found from the server's `--root` and a
  list in the store of projects that synced). *Implemented (phase 1):* the projects are those under `--root`, the
  locks in their changesets' worktrees and the store's `projects/` list; what each one locks and what is installed in
  its `.undine/deps/` stay. A lock it cannot read stops it before anything is removed. It removes trees and objects
  only (and `tmp/` entries older than a day): `git/`, `pdk/` and `build/` stay.
- **Vendoring** (`pkg vendor`). Files stay in the publishers' repositories, and a repository that is deleted, made
  private or force-pushed (its commit then garbage-collected) loses that version for every machine whose store lacks
  it. So a tapeout keeps its own copy: `pkg vendor PROJECT DIR` writes, from the store, the pinned tree of every locked
  package, plain git dependency and the profile package into `DIR/vendor/<package>@<version>/` (git dependencies as
  `<name>@<commit>`), with `vendor/vendor.json` (per entry the index URL, git URL, subdir, commit and tree hash, as in
  the lock) and `vendor/LICENSES.json` and `vendor/LICENSES.md` from `pkg licenses`. Each written tree is hashed again
  and compared with the lock; a tree not in the store is fetched first (asks first) or, offline, named and refused.
  The tapeout export runs it into the bundle (`<project>/tapeout/<target>/<top>-<time>/vendor/`), and `report.json`
  lists the vendored trees. The PDK is recorded in `vendor.json` (commit, ciel source, per-library tree hashes, licence)
  but not copied: ciel's releases keep it, and a sky130 variant is 1.1 GB. `pkg sync --from-vendor DIR` reads a vendored
  directory back into the store, checking every tree against the lock, so the project's dependencies can be installed
  again with no repository and no network.

## Publishing

```sh
undine pkg init libs/analog_ip            # writes undine-package.toml from the library (cells, PDK of the project)
undine pkg check libs/analog_ip           # what a publish requires, below
undine pkg pack libs/analog_ip            # tree hash at the tagged commit; prints the index line
undine pkg publish libs/analog_ip --index undine   # branch + commit in a clone of the index, opens the PR
```

`check` requires:

- a clean worktree at a tag that the `repository` URL serves: `v<version>`, or `<name>-v<version>` when the repository
  holds more than one package;
- every exported cell has a symbol that passes `check_symbol` and a schematic that netlists;
- every cell with a layout passes DRC and LVS against `built_with`, with the deck, options and waivers of `[checks]`;
- the intent specs of exported cells that have them are run (`check_specs`); for analog IP they are the contract;
- dependencies resolve from the index; a license; no file over a size limit (default 50 MB) and no LFS pointer;
- the package's code blocks (`.control`, `shell`, `.include` / `.lib` paths) and RTL are listed, and the ones the
  [trust rule](#security) would refuse are flagged.

Against the previous version it reports **semver changes**: an exported cell removed or renamed without a
`[renames]` entry, a symbol pin removed or renamed, or a change to an exported cell's layout bounding box, pin
positions or pin layers (it breaks a consumer's placement and routing) needs a major version (minor before 1.0); a new
cell needs a minor version; another layout change needs at least a patch. A spec result that changed is listed too.

**The check report.** `check` writes `undine-check.json`: the package name, version, commit and tree hash it ran on,
the PDK commit (`built_with`) and the tools (as in the lock's `tools`), per cell the DRC and LVS results with the
waivers applied, the spec results, the semver report, the flagged code blocks and RTL, and the job ids of the runs in
the publisher's project. A report is valid only for the tree it names.

**Publishing.** `publish` pushes no commit or tag to the package's repository. It fetches the commit back from
`repository` and checks the tree, then requires a passing report for that same tree (it runs `check` again when there is
none), and puts the report where the index PR can link it: as an asset of the tag's GitHub release (`gh release
upload`, after the publisher confirms) when the repository is on GitHub, or at a URL the publisher gives with
`--check-url`. It writes the index line with `check: {url, sha256}`, then opens the PR with `gh` when it is installed,
otherwise it prints the branch and the URL to open it. The PR body has a summary of the report (DRC / LVS per cell,
waivers with their reasons, spec results, semver report, flagged code blocks and RTL) and the link. The reviewer of
the default index (kadomoto, for now) reads that summary; the evidence is the publisher's run, said as such in the PR.

The index's own CI (a workflow file Undine provides) repeats what needs no PDK: the fetch and tree check, the name
rules, the code-block and RTL listing, and the report check (the linked file's SHA-256 equals the line's, and it names
the line's commit and tree and passes). DRC and LVS in the index CI, which needs the PDK there (1.1 GB per sky130
variant), come later ([Decisions](#decisions) 9).

## Cells across versions

A cell is known by its name. `[renames]` maps old names to new per version, so an update can say "`comp` is now
`comparator`" instead of "removed + added", and rewrite the consumer's `C {analog_ip/comp.sym}` references when the
person agrees (in the changeset the update is). Stable cell ids were considered and not chosen: every cell file would
need one, and names are already the identity in schematics, layouts and netlists.

`pkg update` (and the review of an update) reports per exported cell: added / removed / renamed / changed, which
views changed (symbol pins, schematic, layout, intent) and which spec results changed; the consumer's cells that place
it (today's `used_by`); layout copies that still have the old geometry, found by the operation log's origin, which now
records `src: {"lib": "analog_ip", "package": "analog_ip@1.2.0", "tree": "sha256:..."}` (copies made before that are
matched by their content `sha` against the old version's cell); and the results that become outdated.

**Refreshing layout copies.** Placing a package cell copies its layout into the consumer, and placing or syncing it
again does not replace a cell the layout already has under that name. So `dep_update` also refreshes the copies: each
copied cell whose origin is the old version is replaced in the changeset by the new version's cell under the same
name, with its subcells (a subcell of the same name with other content becomes `name$1`, as today). A copy the consumer
has edited since (its content no longer equals the old version's cell) is not replaced: it is listed for the person.
The review shows each refresh as a layout diff; the DRC and LVS of the consumer cells that place it are outdated
(their layout hash changed) and are re-run in the changeset. Without this, the schematic and the LVS reference would
come from the new version while the layout kept the old geometry.

## PCell variants and PDK versions

A generated PCell cell is named by the generator generation and its parameters (`pcell.cell_name`), not by PDK
commit; all of a project's cells are generated under its one PDK. A package ships its variant cells as generated under
`built_with`. Under a compatible tag those are taken as they are (the tag promises the same geometry; once the index
CI has a PDK, later, it can regenerate them under each listed commit, in the same `pcell` environment, and compare).
When a copied cell brings a variant subcell whose name the consumer's layout already has with other content, the
copy's subcell becomes `name$1` (as today for subcells) and the update report lists it. PDK commits stay out of
variant names: putting them in would rename every existing variant for nothing within one compatibility tag.

## PDK profiles as packages

`pdkprofile.py` becomes data: one package per family (`profile_sky130`, `profile_gf180mcu`, `profile_ihp_sg13g2`) with
`profile.toml` holding what `Profile` holds today (layers, metals, decks and options, Magic / Netgen files, models and
corners, PCell library, stack rules, LVS elements, limits) and:

```toml
schema = 1                 # the profile format; an Undine that does not know it refuses the profile
undine = ">=0.2"           # the Undine versions that read this profile

[[pdk]]
commit = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
compat = ["sky130/2025"]
tested = true          # replaces pcell.SUPPORTED, pixi.toml's and setup.sh's copies
license = "Apache-2.0"
tree = { "libs.tech" = "sha256:41c0...", "libs.ref/sky130_fd_pr" = "sha256:9a7e...",
         "libs.ref/sky130_fd_sc_hd" = "sha256:0f3b..." }   # expected PDK tree hashes, per part
```

No code: the one computed field (`LvsElement.params`, sky130's MIM area and perimeter) becomes a named formula that
Undine implements (`params = "mim_area_perimeter"`). Unknown fields are refused, not ignored: `Profile` grows with
Undine's features (straps, rings, fill, stacks, `erc_tap`, ...), and an Undine that silently dropped a field would run
DRC or LVS differently. `verified` and `magic_drc` stay with Undine, in a table keyed by family and profile version:
they record what Undine's own tests have run, not facts about the PDK. Undine ships its current profiles built in as
the fallback with no index. A profile change (a deck option, a model path) is an update of the profile package,
reviewed like any other; the profile fields a job uses are in its inputs (see [evidence](#changesets-and-evidence)),
so results that used a changed field become outdated, and the others do not.

## Licences

`license` (SPDX) is required to publish; PDK licences come from the profile package. `pkg licenses PROJECT` lists the
licence of every locked package and the PDK; `pkg vendor` writes that list into the tapeout bundle next to the
vendored trees (see [Fetch and store](#fetch-and-store)). Third-party IP and reference data are fetched as
packages, not copied into Undine or into a project (for example `server/undine/data/tt_sky130A_analog.json` is a
candidate).

## Changesets and evidence

A dependency change is a **changeset operation**, like any other edit, reviewed and adopted:

- `dep_add` / `dep_update` / `dep_remove` / `pdk_set` change `project.json` and `undine.lock` in the changeset's working
  copy and install into its `.undine/deps` from the store (hard links, cheap). The changeset can then place the new
  cells and run checks. The preview no longer refuses dependency changes. `dep_update` refreshes layout copies (see
  [Cells across versions](#cells-across-versions)).
- The review shows the lock diff, the update report above, licences, the download size, and the package's code blocks
  and RTL files (see [Security](#security)).
- **Adopting** merges `project.json` (`libraries`, `indexes`, `pdk`) and `undine.lock` entry by entry, then resolves
  again; when the merged requirements do not resolve, or the merged lock does not satisfy them, the adopt is refused
  with the reason. Then it installs in the project from the store; nothing is fetched at adopt. This is new: today
  adopt is a git merge and both files merge as text (`merge.md`), so two changesets that each change a dependency
  would make line conflicts, or merge cleanly into a lock that does not match the installed trees. Phase 4 adds a
  merge rule for the two files to Undine's git merge driver.
- A person's own **Add** in the library browser makes the same operation in a small changeset and adopts it in one
  click, so there is one path.
- **Agents** may propose `dep_add` / `dep_update` / `dep_remove` in their changeset, using the index clones as they
  are. They never fetch: when a tree is not in the store, the operation is recorded as *needs fetch* (URL, size) and
  the review shows a **Fetch** button. Only a person's confirmation fetches (see the token under
  [HTTP](#ui-cli-http-agent-tools)), and the same goes for refreshing an index and for fetching a PDK. Until the
  fetch, the review marks the operation *unchecked* (no cell of the package was placed or checked in the changeset).
  When the person's fetch ends, the agent's changeset gets a notice (an agent waiting with `wait_for` sees it), the
  operation becomes *installed*, and the agent can place the cells and run checks.
- **Evidence** already hashes `project.json` and `undine.lock`. Job inputs additionally record, outside `key` (for
  information, so evidence names exact contents): each `package@version` and tree used by the job's cells, the PDK
  commit, and the profile package and version. Outdated detection stays on what the job reads: the cells' content
  (the netlist hash covers generated subcircuits, the layout hash copied cells), the deck SHA, and, new, the model
  files the netlist includes (`.lib` / `.include` targets and OSDI files, by hash) and the profile fields the job uses
  (deck options, deck variables, model paths). A changed README or an unrelated cell in a package, or a layer colour
  in a profile, makes nothing outdated.

## UI, CLI, HTTP, agent tools

- **Library browser**: a *Packages* section searches the index clones; a package page shows versions, licence,
  description, exported cells with symbol previews, dependencies, and whether it fits this project's PDK (and why
  not). Dependency rows show `analog_ip 1.2.0` and, when there is one, the newer version.
- **Lock badge**: `locked` / `N lock issues` as today, plus the PDK (verified / unverified / missing → **Fetch...**),
  available updates and yanked versions. *Implemented (phase 1):* the PDK's state is on the PDK badge, whose menu
  has a hint for the state, **Fetch...** (when the PDK is missing or modified) and **Rescan PDKs**; the lock dialog
  shows the lock's format, how each dependency is pinned (`sha` or `tree`, and *not in the store*), the PDK row, and
  the warnings and notes in a lighter style under the issues.
- **CLI** (`undine pkg ...`; `lib ...` stays for plain git dependencies):

  | Command | |
  |---|---|
  | `pkg search TEXT`, `pkg info NAME[@VER]` | from the index clones |
  | `pkg add PROJECT NAME[@REQ] [--as LOCAL] [--index I]` | resolve, fetch (asks first), lock, install |
  | `pkg update PROJECT [NAME] [--to VER] [--dry-run]`, `pkg lock`, `pkg outdated`, `pkg tree`, `pkg why NAME` | `lock` re-resolves without fetching |
  | `pkg remove`, `pkg sync [--offline] [--from-vendor DIR]`, `pkg licenses` | `sync` installs the lock as it is |
  | `pkg vendor PROJECT DIR` | the locked trees and their licences, for a tapeout bundle |
  | `pkg init`, `pkg check`, `pkg pack`, `pkg publish`, `pkg yank NAME@VER` | publishing |
  | `pkg index list / add NAME URL / update [NAME]` | indexes |
  | `pdk list`, `pdk fetch PROJECT`, `pdk verify PROJECT [--pin]` | PDKs through ciel |
  | `store verify`, `store gc` | the store |

- **HTTP**: `GET /api/packages?q=`, `GET /api/packages/{name}`, `POST /api/indexes/{name}/refresh`, changeset
  operations `POST /api/p/{p}/changesets/{id}/deps`, `POST /api/p/{p}/deps/fetch` and `POST /api/p/{p}/pdk/fetch`
  (jobs with progress), `POST /api/pdks/rescan`; the existing `/deps` endpoints keep working. All of them go into
  `docs/reference/api.md` (today `/deps`, `POST /libs`, `GET /api/pdks` and `POST /api/p/{p}/pdk` are missing there).
  *Implemented (phase 1):* `POST /api/pdks/rescan`, `GET /api/p/{p}/pdk/fetch-plan` (what the fetch dialog shows),
  `POST /api/p/{p}/pdk/fetch` and `GET .../pdk/fetch/{job}`; `/deps/sync` takes `{offline}` and `/deps/lock`
  re-resolves; all in the [HTTP API](../reference/api.md#pdk-fetch). There is no route for `lib lock --format 2` or
  `pdk verify --pin`: the command line only, in phase 1.
- **Confirmation token.** A fetch, an index refresh and a PDK fetch over HTTP need one. The UI asks for it with
  `POST /api/confirmations {kind, url, commit, tree}` only after the person clicks in a dialog that shows the URL,
  size and licence; the token is single-use, lives five minutes, is bound to that exact (URL, commit, tree) (for a
  PDK: commit, variant and libraries), and is returned only in that response. No MCP tool, `undine call` command,
  Python client method or built-in agent tool asks for one, carries one or returns one. Said plainly: Undine has no
  authentication yet, and the actor of a request is what the request says (as for adopt and evidence prune today), so
  a process with a shell and the person's access to the server can do what the person's browser does. The token stops
  agents that work through Undine's tools, and mistakes; it is not a defence against a hostile process running as the
  person. *Implemented (phase 1), for PDK fetches only* (decided: phase 4 generalises it to the other kinds):
  `POST /api/confirmations {kind: "pdk", project, commit, variant, libraries, allow_network?}` checks the request
  against the fetch plan the server makes now (`commit` may be the plan's first 7 or more characters, as a format 1
  id has 12) and binds the token to the project and the whole plan (family, variant, full commit, libraries, source,
  root); any other kind, a changeset preview and a source other than ciel's default are refused. `POST
  /api/p/{p}/pdk/fetch {token}` uses the token up, makes the plan again and refuses one that changed since; no token,
  an unknown, used or expired one, or one for another project is a 403. The fetch runs as an in-memory job
  (`GET /api/p/{p}/pdk/fetch/{job}`), lost on a restart. A test in `test_api_phase1.py` checks that no tool,
  `undine call` command or client method reaches either route. *Implemented (phase 2):* a second kind, `trust`:
  `POST /api/confirmations {kind: "trust", project, name}` returns the findings with the token, which is bound to the
  project, the local name, the trust key and the installed tree; `POST /api/p/{p}/trust {name, token}` uses it up and
  refuses a key or tree that changed since. Taking trust back (`{name, untrust: true}`) needs no token. A test in
  `test_agent_phase2.py` checks that no tool, `undine call` command or client method reaches either.
- **Agent tools** (MCP and the built-in agent): `search_packages`, `get_package`, `get_dependency_status`,
  `propose_dependency` (add / update / remove in the agent's changeset). Package and index text (descriptions,
  READMEs, cell names, intent notes, index lines) is data for agents: the tools return it under `agent.py`'s DATA rule,
  never as instructions. `agent.py`'s system prompt and `docs/guide/changesets.md` change from "cannot add
  dependencies" to "can propose them"; the skill gets a short reference on dependencies (today it does not mention
  them).

## Migration

- **Git dependencies** keep working unchanged. `pkg migrate PROJECT NAME` turns one into an index dependency when an
  index version's files (those its manifest includes) equal the installed ones, compared file by file. The commit a
  git dependency pins usually predates the manifest (and a commit without one cannot be published), and today's `sha`
  covers the whole subdir while the index `tree` covers only the included files plus the manifest, so the two hashes
  are not compared. Migrate reinstalls with the manifest's file set and reports the change: the manifest that comes and
  the files outside `include` that go. Lock format 1 is read; format 2 is written by `pkg migrate` (see
  [Lockfile](#lockfile)).
- **PDK ids**: a 12-character id (`sky130A@8afc8346a57f`) is expanded to the full commit from the installed PDKs (a
  unique prefix), else from `ciel ls-remote` when the person allows the network. A PDK is verified against the tree
  hashes its profile package lists for that commit. A commit the profile does not list is locked by the hashes found
  and shown as *unverified* in `status` and the badge (trust on first use, said as such) until a profile lists it. A
  PDK with no version (a plain `<root>/<name>/` directory) is locked by tree hash only, with a warning. `pdk_history`
  stays. *Implemented (phase 1):* all of it, with no profile package yet, so a pinned PDK that matches is
  *unverified*, a note and not an issue (decided). A prefix that matches several commits is refused, listing them.
- **Projects without a lock** (made before locks) get one on the first dependency change, as today, but now `status`
  says it was created. *Implemented (phase 1)* as the result of the operation that created it, since `status` cannot
  tell afterwards: every CLI command that made `undine.lock` prints `created undine.lock (format N)` (`new`, `set-pdk`,
  the `lib` commands, `pdk verify --pin`), and `relock`, `pdkops.upgrade` and `set-pdk` return `created`.
- **Profiles**: phase 6 moves each family's `Profile` to `profile.toml` and checks that the loaded data equals today's
  Python object for every family before the Python is removed.

## Security

- **Trust.** A package is data, but simulating it runs what its cells tell the simulator. A schematic's code blocks go
  to ngspice, where `.control` can run `shell` and `.include` can read any file on the server; a package's `rtl/` goes
  through Verilator, where `$c(...)` and `` `systemc_* `` blocks are C++. So, from phase 2, when a netlist includes a
  cell from a dependency, a `.control` block, a `shell` command, or an `.include` / `.lib` / OSDI path that is absolute
  or leaves the package (`..`) in that cell is refused with the cell named; and Verilator refuses package RTL with
  `$c` or `` `systemc_* ``. A person can mark a package version trusted in the project (`"trusted":
  ["analog_ip@1.2.0"]` in `project.json`); an agent can propose that but the review shows it, and an update drops it.
  The review of a dependency change lists the package's code blocks and RTL files, and `pkg check` and the index CI
  flag them. The PDK's own model files and PCell code are trusted as the PDK, as today.
  *Implemented (phase 2):* `store/trust.py` finds the content, and every dependency installed under `.undine/deps`
  that the project does not trust is checked when a job is about to run. A plain git dependency is checked too: that
  is the first of the [phase 2 changes for plain git dependencies](#phase-2-status), so a simulation whose dependency
  cell has `.control` or an absolute `.include` now needs `lib trust`. What is refused, exactly:

    - **ngspice** (`start_sim`, before a run is reused): every `.control` block (with or without `shell`, also inside a
      `.subckt`; matched by prefix as ngspice does, so `.controlx` and `.control;` too), every `*#` line, and every file an `.include` / `.lib` card or an XSPICE `file=`, `input_file=` or
      `state_file=` parameter reads that, after the netlister's variable substitution, still holds `$` or `~`, is
      absolute outside `pdk.root`, or is relative with a `..` part. `+` continuations are joined first; a card the
      scanner cannot read is refused as *unparsed*. The only absolute root allowed is the PDK's own (`pdk.root`), not
      the dependency's install directory: an `.include` into it would read a file nothing scanned. A line is charged to
      both the cell body it sits in and the symbol it was formed from (`schematic.Source`), so a trusted symbol's
      `@value` set in an untrusted cell, and an untrusted symbol's template default placed in a project cell, are both
      caught; the generated corner `.lib` line passes. A `.subckt` line is a line too (its pins are the placed
      symbol's), and a pin or port name that holds a space or a line break is refused when the netlist is formed.
    - **LVS** (KLayout and netgen): the same paths rule only, since they run no control code, checked when the run
      launches (`Run.launch`, through a gate), so sign-off status and changeset reuse still show results.
    - **Verilator** (`rtl.py`, lint and simulation of a dependency library): `$c` (and `$c8` ... `$c64`, not
      `$clog2` and the other system functions) and `$system` wherever they appear (a macro argument becomes the call
      once expanded), `` `systemc_* ``, `import` / `export "DPI-C"`, `` `define `` bodies holding these, `import`,
      `export` or `DPI`, or pasting tokens (``` `` ```), and a path that is absolute, holds `~` or a `..` part, or is
      not a literal, in `` `include ``, `$fopen`, `$dumpfile` (a simulation is built with `--trace`), `$dumpports`,
      `$readmem*` and `$writemem*`. Comments are skipped. Lint, which compiles nothing, checks `` `include `` paths
      only.
    - **LibreLane** (`librelane.submit`, the `run_librelane` tool and the run route): a flow whose top library is a
      dependency is checked with Verilator's full rule (its Verilator lint and Yosys read the RTL, `$readmem*`
      included), the schematic's gate-level export too, before anything is copied or started.
    - **Xschem's netlister backend** (the netlist route with `backend=xschem`): `tcleval(` in any `.sch` or `.sym` of
      an untrusted install (read with every backslash dropped, as Xschem unescapes a value before it looks: `tcl\eval(`
      counts), and `expr(` whose argument holds `[` or `$`, whatever Undine's own pre-pass made of the
      cell. Only Tcl: that netlist is for display, and Xschem evaluates nothing else when it netlists.

    A refusal names the cell, the dependency and each finding with its line, and says that a person can trust the
    version (`undine lib trust PROJECT NAME`, or **Trust...** in Undine) and an agent cannot. The key is
    `<package>@<version>`, or `<local name>@<commit12>` for a dependency without a manifest; `update`, `remove` and
    `lib lock` drop it whenever the entry's commit or tree changes, `sync` drops the keys of what it removes, and an
    `add` (or `update`, `lib lock`) that resolves a package at content the lock did not have drops any key of that
    name left from earlier content, since any repository can claim a name and version. A changeset's worktree reads
    trust from the real project only (its own `project.json` is a copy from when it opened), so trust a person gives,
    or takes back, after an agent opened its changeset reaches it. Not covered: a package's key
    names its version label, not its tree, so an `undine.lock` edited by hand (or pulled) to another commit of the same
    version keeps the trust through `sync`, which never resolves; binding the key to the tree would close that and is
    left open. Xschem or ngspice run outside Undine is not protected, nor is a `project.json` `trusted` list that a
    process with a shell edits: as for the confirmation token, the rule stops agents working through Undine's tools,
    not a hostile process running as the person.

- No other code from a package, an index or a profile runs: profiles have named formulas only. PCell code stays the
  PDK's, run in the PCell worker as today.
- Files: only regular files from git's objects (no symlinks, no submodules), no absolute paths or `..`, size and
  file-count limits, LFS pointers refused. No checkout and no hooks (bare repository). Plain git dependencies with a
  format 1 lock keep today's `git archive` and its `data` tar filter until their entry next changes. *Implemented
  (phase 1):* installing from complete store records and objects reads no repository and runs no git (each object is
  re-hashed before it is linked); the kept repositories and the archive view are made with `git init --template=`, so
  no hook comes from the user's `init.templateDir`.
- git: `protocol.ext.allow=never` as today. `file://` and absolute paths are allowed from the CLI; from HTTP only under
  roots given with `--dep-root` (like `--import-root`), so a browser cannot read any repository on the server's disk.
  *Implemented (phase 0):* `serve --dep-root DIR`, repeatable, with no default (unlike `--import-root`, whose default
  is home): without it HTTP takes no local repository. The check applies to a URL from the request and to one a
  `project.json` names (`sync`, `update`), before the first fetch. It takes the real path (symlinks and `..`
  resolved), requires an existing directory, and requires the git directory and its common directory that git would
  read there to be under a root too; git then fetches from that git directory. A `file://` URL with a host other than
  `localhost`, a query or a fragment is refused over HTTP. Not covered: a repository under a root whose
  `objects/info/alternates` names objects outside it (it takes write access to that `.git`). Also in phase 0, for the
  CLI and HTTP alike: a `rev` or `subdir` part starting with `-`, a locked commit that is not a full hex id, and a
  dependency name or lock key that is not a library name are refused before git runs or anything is removed; git
  gets `--end-of-options` and `--` before them. *Implemented (phase 1):* with kept repositories git can read a
  commit with no fetch, so these checks (`_check_spec`, `_check_commit`, `_own_dir`, in that order) and, over HTTP,
  the `--dep-root` check now run before every read of a kept repository, not only before a fetch. A request cannot
  read a repository kept from a URL outside the roots, even one a `lib` command fetched; a sync that installs from
  the store reads none and is not checked.
- **Remote hosts.** A `project.json` from an untrusted clone, or an HTTP request, can name any URL. From HTTP a fetch
  goes only to the host of a configured index (`$UNDINE_HOME/config.toml`), a host given with `--dep-host`
  (repeatable), or a URL the person confirmed in the fetch dialog, which shows it in full. Each fetch has a size limit
  (`UNDINE_FETCH_MAX`, default 1 GB) checked while it runs, and the all-refs fallback is not used from HTTP without
  that confirmation. *Implemented (phase 2):* every git URL, rev and subdir a manifest names goes through the same
  checks as one from `project.json` (`_check_spec`, and over HTTP the `--dep-root` rule of `_source`) before any git
  call, and a local URL (`file://`, an absolute path, `~`) in a manifest is refused unless the requirer's own URL is
  local, so a remote package cannot read a private repository on the machine into the project. A remote host is not
  restricted yet: until phase 3's `--dep-host`, an add, update or lock over HTTP fetches from any host a manifest
  it reaches names, as it does from any host the request names.
- Index lines are checked (schema, name rules, URL schemes, hash format) before use; a tree is installed only after it
  matches the lock or the index.
- **Fetches a person confirms.** *Implemented (phase 1), for PDKs only:* a PDK fetch over HTTP needs the
  single-use confirmation token, bound to the project and the exact plan (family, variant, full commit, libraries,
  ciel source, root), which only the browser's dialog asks for after the person's click; from HTTP only ciel's default
  source is fetched ([Confirmation token](#ui-cli-http-agent-tools)). The command line asks before it runs ciel.
  Dependency fetches over HTTP still need no token until phase 4 (the `--dep-root` rule above applies to them).
- **Signing** (later). For now only git hashes pin content: the index line's commit and the tree hash (SHA-256 over
  the blobs, so it does not rest on git's SHA-1), checked on every fetch, on `sync` and on `sync --from-vendor`. A
  changed file, a moved tag or a force-push is detected; who may publish is whoever may merge the index's PRs, and
  someone who can push to the index can still publish a line that points to other content. Signed index commits or
  signed package tags come later.
- **Locks.** No server-wide lock during a fetch: fetches run as jobs that write only to the store (objects are written
  to a temporary name and renamed, so two processes fetching the same file is harmless). The project's locks are
  flocks in `<project>/.undine/`, taken by the CLI and by the server, briefly: `project.lock` around every write of
  `project.json` and `undine.lock` (also by a changeset's adopt and revert), and `deps.lock` around one dependency
  operation at a time, held while it fetches. Phase 0 adds them. *Implemented (phase 0):* both locks, under these names
  (an earlier draft had a single `.undine/lock`). Fetches still run inside the request (without `srv.lock`), into a
  staging directory in `.undine/deps/`, not as jobs into a store. *Implemented (phase 1):* dependency fetches go into
  the store, still inside the request (jobs for them are phase 4); a PDK fetch over HTTP is a job. The order is
  `deps.lock`, then the store's `.lock` held **shared for the whole operation** (fetch, tree record, install, lock
  write and the project's entry in `projects/`), then one flock per kept repository (`git/<h>.git.lock`; readers take
  it shared). `store gc` takes the store lock exclusive, so it never runs between a tree being recorded and the lock
  that names it being written. Every object, record, stamp cache and project record goes to `tmp/` first, then is
  renamed in. Hashing a PDK and running ciel hold no project lock and no server lock. One `ciel fetch` of a PDK commit
  into a root runs at a time, across processes (the CLI and every server): `pdkops.fetch` holds an flock on
  `<root>/ciel/<family>/.undine-fetch-<commit>.lock` around the fetch and the hash that follows; the in-memory job
  lock only shows a second browser fetch as queued. A lock file git leaves in a kept repository when it is killed
  (`shallow.lock`, `packed-refs.lock`, `refs/**/*.lock`) is removed before the next fetch into it, under that
  repository's exclusive flock, which every writer of it holds. A project's `.undine` and `.undine/deps` must be real
  directories: a symbolic link there is refused before any dependency operation lists, locks, stages or removes
  anything.

## Phases

| Phase | Delivers | Tests | Status |
|---|---|---|---|
| 0. Fixes | `format` checked on read (an unknown format refused); `set-pdk` always writes the lock; `sync` removes dependencies gone from `project.json`, `status` reports leftovers; the project flock for the CLI and the server; HTTP fetches outside `srv.lock`; `--dep-root`; the netlister refuses two cells of one name with other content in one netlist; `osdi.sh` fixed (it still writes into the PDK tree; phase 1 moves it); docs for the missing endpoints | lock tests in `test_deps.py`; a lock with `format: 2` is refused by this code; the CLI and a server sync one project at once and the lock and `project.json` stay whole; a slow fake remote does not block another request; `file://` refused over HTTP; two libraries with an `inv` of other content in one testbench are refused, naming both | **Done**, see [Phase 0 status](#phase-0-status) |
| 1. Store and lock 2 | the store, per-file dedup, frozen and offline `sync`; lock format 2 for git dependencies, written only by explicit commands; tree hash from git's blobs; PDK full commit + tree hash with the exclusion list, `pdk verify`, `pdk fetch` through ciel with confirmation (root chosen as above), PDK rescan; the PCell worker with `PYTHONDONTWRITEBYTECODE=1`; OSDI built into the store: profile `osdi` entries become model names (`psp103`), found first in `store/build/<commit>/<variant>/osdi/`, then at today's path in the PDK tree (installs that have them), and `sim.py` and `doctor` take the paths from one function; tool version numbers and origins | two versions share unchanged objects; offline sync from the store; `sync` refuses a lock that no longer satisfies `project.json`; one commit hashes the same with `core.autocrlf=true` and an export-subst file; lock 1 → 2 only on an explicit command; prefix → full commit; a sky130 tree with `__pycache__` and an IHP tree with `osdi/` verify; a changed PDK file is reported; ciel replaced by a fake executable; `pdk fetch` refused when only `~/.volare` is scanned; an IHP simulation loads OSDI from the store; each tool's banner above gives its number | **Done**, see [Phase 1 status](#phase-1-status) |
| 2. Manifest and resolution | `undine-package.toml`; transitive dependencies from manifests (still git URLs); project-wide local names, a package installed under each; cell-name disjointness; PDK compatibility; the trust rule for code blocks and RTL | diamond installs once; conflicting requirements name both chains; two packages with a `comparator` are refused naming both; a local alias resolves, also with the Xschem netlister backend; two libraries giving one local name different packages are refused; an incompatible PDK is refused; a dependency cell with `.control` or an absolute `.include` is refused until trusted | **Done**, see [Phase 2 status](#phase-2-status) |
| 3. Index | index format, several indexes, the default index created (`kadomoto-lab/undine-index`), `pkg search / info / add / update / lock / outdated / tree / why`, version ranges, yank, remote-host policy and fetch size limit, library browser Packages section; `pkg licenses`, `pkg vendor` (also run by the tapeout export) and `pkg sync --from-vendor` | a `file://` index in a temporary directory; yanked skipped but kept when locked; private index named in `project.json`; a dependency without `index` comes from the declaring package's index; one name from two indexes refused; an unlisted host refused over HTTP without confirmation; a fetch over the size limit stopped; a vendored bundle, with the package repository deleted and the store emptied, installs the project again offline through `sync --from-vendor` with the locked trees; a vendored file changed by hand is refused; the tapeout bundle has `vendor/` and `report.json` lists it | **Planned** |
| 4. Changesets and agents | dependency operations in changesets, review of a dependency change (code blocks and RTL listed), the confirmation token, the notice after a fetch, agent tools with the DATA rule, the merge rule for `project.json` and `undine.lock` in the git merge driver (`merge.md` updated), evidence with package and PDK information outside `key` and model files and profile fields inside it | add in a changeset, adopt, restore; an agent's add records *needs fetch*, never fetches, and continues after the person's fetch; no tool returns a token; two changesets adding different packages adopt in either order; conflicting requirements are refused at adopt; an update that changes one cell outdates only results that use that cell; a README change outdates nothing | **Planned** |
| 5. Publishing | `pkg init / check / pack / publish / yank`, `[checks]`, the check report and its link in the index line and PR, semver report, spec results in the report, `[renames]`, cell-level update report, refreshed layout copies, index CI workflow (no DRC / LVS) | publish refuses without a passing report for this tree, and the PR body links the report with its SHA-256; the index CI refuses a report whose SHA-256, commit or tree differs from the line; a renamed cell is reported as renamed and references rewritten; a removed pin or a moved layout pin needs a major version; a waived rule passes `check` and is shown; publish refuses a tree the repository does not serve; tag `<name>-v<version>` in a repository of two packages; after an update the copied cell is refreshed in the changeset and its LVS is outdated, and an edited copy is listed, not replaced | **Planned** |
| 6. PDK profiles | `profile.toml` per family with `schema` and `undine`, formula registry, profile packages in the index with expected PDK tree hashes, compatibility tags, `pcell.SUPPORTED` / `pixi.toml` / `setup.sh` read from them | data profile equals today's `Profile` for sky130, GF180, IHP; an unknown field is refused; a changed deck option outdates DRC results and a changed layer colour does not | **Planned** |

### Phase 0 status

Where each item of phase 0 is in the code. The items done in the first part of phase 0 were checked against the code right after that part, the others against
the code when phase 0 was finished. Tests are in `server/tests/test_deps.py` unless named otherwise.

| Item | Status | Where |
|---|---|---|
| `format` checked on read | Done | `read_lock` (`store/deps.py:90-115`) refuses a larger integer `format` ("made by a newer Undine ... update Undine"), any other value but 1 ("which no Undine writes"), JSON that does not parse and JSON that is not an object, each with what to do and "nothing was changed"; a lock without `format` is read as 1. Every reader refuses it: `lib add` / `update` / `remove` / `sync` / `lock` / `status`, `set-pdk`, the `/deps` routes and `POST /pdk` (HTTP 400, the CLI one line and exit 1); `add` and `update` read it before fetching (`store/deps.py:402-472`), `remove` and `set_pdk` before `project.json` changes. Opening the project (`GET /api/p/{p}`) does not read it. Tests: `test_a_lock_of_a_newer_format_is_refused_by_every_operation_and_left_byte_for_byte`, `test_a_lock_of_a_newer_format_is_refused_over_http_and_by_the_cli`, `test_a_lock_without_format_is_read_as_format_1_and_a_broken_one_is_refused` |
| `set-pdk` always writes the lock | Done | `Project.set_pdk` (`store/project.py:164-187`) writes `pdk` into `undine.lock` whenever the lock is missing or pins another PDK, also when `project.json` already names the new one; a project without a lock gets a new format 1 lock with the tool versions now, as `lib lock` would. The tools answer before `meta_lock` is taken. `cli.md`, `api.md` and `pdks.md` say so. Tests: `test_set_pdk_writes_the_lock_whether_or_not_the_project_has_one`, `test_set_pdk_over_http_and_from_the_cli_creates_a_missing_lock` |
| `sync` removes dependencies gone from `project.json`; `status` reports leftovers | Done | `leftovers` (`store/deps.py:351-369`): a directory in `.undine/deps/` or a lock entry that `project.json` does not name, with its locked and installed commits. `sync` (`store/deps.py:488-550`) removes each with its lock entry and reports it as `removed`; one gone from `project.json` while `sync` fetches is left to the next sync, and a sync whose fetch fails removes nothing. `status` (`store/deps.py:597-611`) lists each as state `leftover` with an issue naming `lib sync` (so `ok` is false), and a lock key that is not a library name as an issue that `sync` leaves alone. `.undine/deps/` stays one entry of the symbol path (`Project.symbol_paths`, `store/project.py:237`, which says why), so a leftover's symbols resolve until `sync`; the netlist cache key covers every directory in `.undine/deps/` (`server/api.py:353-357`). The UI shows the state and offers sync first. Tests: `test_status_lists_a_dependency_gone_from_project_json_and_sync_removes_its_files_lock_entry_and_symbols`, `test_a_sync_that_fails_removes_no_leftover_either`, `test_the_cli_and_http_report_a_leftover_and_sync_removes_it`, `test_status_reports_a_leftovers_installed_commit_from_its_files_not_from_the_lock`, `test_the_netlist_hash_changes_when_sync_removes_a_leftover_a_testbench_places`, `test_sync_never_removes_a_directory_a_lock_key_names_outside_the_deps_folder`, and from phase 0 `test_what_changed_while_fetching_is_not_written_back` |
| The project flock for the CLI and the server | Done | `meta_lock`, an flock on `.undine/project.lock` (`store/deps.py:110-115`), taken by `_committing` (`store/deps.py:238-244`), `relock`, `Project.editing` (`store/project.py:184-190`, also `set_pdk` and `create_library`) and a changeset's adopt or revert (`store/changeset.py:2401-2404`); `.undine/deps.lock` (`store/deps.py:118-121`) for one dependency operation at a time. Tests: `test_project_files_are_written_one_writer_at_a_time`, `test_meta_lock_holds_across_processes_and_a_rename`, `test_an_adopt_waits_for_a_writer_of_project_json`, `test_an_operation_that_waited_works_from_what_the_one_before_wrote` |
| HTTP fetches outside `srv.lock` | Done | `add`, `update`, `sync` fetch under `.undine/deps.lock` only and take the caller's `guard` (`srv.lock`) to install and write (`store/deps.py:238-244`, `server/api.py:1654-1694`). Tests: `test_slow_fetch_does_not_stop_the_server`, `test_sync_is_all_or_nothing` |
| `--dep-root` | Done | `serve --dep-root DIR` (`__main__.py:267-270`; repeatable, no default) into `Server.dep_roots` (`server/api.py:141-143`), passed as `local_roots` to `add`, `update` and `sync`. `_source` (`store/deps.py:206-263`, with `_local_path`, `_under_roots`, `_git_dirs`) checks every URL to fetch, from the request or from `project.json`, before the first fetch: a local one must be an existing directory whose real path, git directory and common directory are under a root, and git fetches from that git directory; a `file://` URL with a host, query or fragment is refused. The CLI passes no roots and takes any path. Also for the CLI: `_check_spec` and `_check_commit` (`store/deps.py:182-203`) refuse a `rev` or `subdir` part starting with `-` and a locked commit that is not a full hex id, `_own_dir` (`store/deps.py:384-391`) a name that is not a library name, and git gets `--end-of-options` and `--` (`_fetch`, `store/deps.py:266-310`). Not covered: `objects/info/alternates` in a repository under a root. Tests: `test_a_local_repository_is_refused_over_http_without_dep_root_and_the_cli_still_takes_it`, `test_a_local_repository_under_a_dep_root_is_added_updated_and_synced_over_http`, `test_a_path_that_escapes_the_dep_root_by_dotdot_or_a_symlink_is_refused_over_http`, `test_a_path_whose_git_directory_is_outside_the_dep_root_is_refused_over_http`, `test_sync_over_http_fetches_a_local_path_project_json_names_only_under_a_dep_root`, `test_serve_dep_root_is_repeatable_and_off_by_default`, `test_a_rev_or_a_lock_commit_that_git_would_read_as_an_option_is_refused_before_git_runs`, `test_a_subdir_that_looks_like_a_git_option_is_refused_before_anything_is_fetched` |
| The netlister refuses two cells of one name with other content | Done | `spice` keeps the schematic behind each subcircuit name (`homes`, `schematics/schematic.py:630-633` and `654`, the top cell included); `same_name_check` (`schematics/schematic.py:563-584`) netlists a second cell of that name and raises `SameNameError` (`schematics/schematic.py:390`), naming both, unless it gives the same lines, which are written once. So every mode refuses it, and with it simulation, LVS, specs and sweeps. The vlsirtools backend runs `spice` first (`schematics/schematic.py:789`); the Xschem backend runs it with `XschemOrder` (`schematics/schematic.py:796-806`, Xschem's own symbol order) before looking for Xschem and stops only on that error (`schematics/schematic.py:815-823`). Some PDK launcher pages are now refused (sky130 `sky130_tests/top`, IHP `start_page`). Tests in `test_netlist.py`: `test_two_cells_of_one_name_with_other_content_are_refused_naming_both`, `test_the_xschem_backend_refuses_only_what_xschem_itself_would_netlist_as_one_cell`, `test_every_pdk_schematic_netlists_the_same_from_its_netlist_view`; the stand-in clash as before in `test_substitute.py` |
| `osdi.sh` fixed | Done, and more | It imports `undine.pdks.pdk` and compiles a copy of `libs.tech/verilog-a` into `Pdk.osdi_dir` (`pdks/pdk.py:43-58`), so the PDK stays as installed: part of phase 1, without the store. Test in `test_pdk.py`: `test_a_pdk_with_verilog_a_models_needs_them_compiled` |
| Docs for the missing endpoints | Done | `GET /api/pdks`, `POST /libs`, `POST /pdk` and the `/deps` calls in `docs/reference/api.md` |

Phase 0 also made `pick_pdk` prefer, for a bare PDK name, a version whose PCells run and then `pcell.SUPPORTED`
(`store/project.py:115-121`; `test_a_bare_pdk_name_picks_the_supported_version` in `test_pdk.py`), made dependency operations re-read
`project.json` once they hold the dependency lock, made `sync` all or nothing, and kept machine paths out of
`undine.lock` (`test_refusals_and_drift`).

Two parts of phase 1 landed early, in phase 0: OSDI is compiled outside the PDK tree (into Undine's cache,
`~/.cache/undine/osdi/<PDK id>/`, not yet a store), and the simulation and `doctor` read its paths through one function
(`Pdk.osdi_files`); and the lock records tool versions, not paths (still as banners, with no number or origin).
Phase 1 finished both ([Phase 1 status](#phase-1-status)).

### Phase 1 status

Where each item of phase 1 is in the code, by module and function (`server/undine/`). Tests are in `server/tests/`;
ciel is replaced by a fake executable (`fake_ciel.py`), and every test runs with a temporary `UNDINE_HOME` and
`UNDINE_STORE`, so none touches `~/.undine`, `~/.ciel` or the network.

| Item | Status | Where |
|---|---|---|
| The store | Done | `home.py` (`home()`: `$UNDINE_HOME` or `~/.undine`; `store_root()`: `$UNDINE_STORE` or `home()/store`) and `store/cas.py`: objects named by the SHA-256 of their bytes, mode 0444; tree records and format 1 archive records, each re-hashed against its name when read; `install` re-hashes every object (one whose only fault is its mode, a `chmod` through a project's link, is made read-only again and kept), then hard-links it (`link_or_copy`: a read-only copy on `EXDEV`, `EPERM`, `EMLINK`, `ENOTSUP`, or with `UNDINE_STORE_LINK=copy`); `store_lock`; `register_project`; `verify` and `gc`. Every write goes through `tmp/`. Tests in `test_cas.py`: `test_tree_hash_golden_and_order_by_utf8_bytes`, `test_put_twice_keeps_one_read_only_object`, `test_install_links_read_only_files`, `test_install_copies_when_asked_or_when_linking_fails`, `test_install_restores_the_mode_of_a_good_object_changed_through_a_project`, `test_install_accepts_a_good_object_whose_mode_does_not_stick`, `test_an_object_edited_in_place_is_caught`, `test_gc_keeps_live_trees_and_frees_the_rest`, `test_gc_waits_for_a_shared_lock` |
| Tree hash from git's blobs, per-file dedup | Done | `store/gitsrc.py`: a kept bare repository per URL (`git/<sha256 of URL>.git`, each commit kept by `refs/undine/<commit>`, one flock per repository), `fetch` (depth 1, the all-refs fallback as before, the result checked against the locked commit), `blob_tree` (`ls-tree -r -z -l`, then `cat-file --batch`; regular files only; symlinks, submodules, LFS pointers, paths that leave the directory and paths with a `.git` part refused; a stale git lock file in the kept repository removed before a fetch, and git killed on a timeout with SIGTERM first; the file count and `UNDINE_FETCH_MAX` checked before any blob is read). Tests in `test_gitsrc.py`: `test_one_commit_hashes_the_same_with_autocrlf_and_export_subst`, `test_modes_enter_the_tree`, `test_two_versions_share_unchanged_objects`, `test_limits_are_checked_before_any_blob_is_read`, `test_blob_tree_refuses_what_cannot_be_installed`, `test_a_kept_commit_needs_no_network_and_survives_gc`, `test_ext_protocol_is_refused`, `test_a_git_part_in_a_path_is_refused_as_git_refuses_it`, `test_a_lock_file_left_by_a_killed_fetch_does_not_stop_the_next_one`, `test_a_timed_out_git_is_asked_to_stop_before_it_is_killed`; in `test_store_sync.py`: `test_update_keeps_the_inode_of_an_unchanged_file` |
| Format 1 entries keep their `sha` | Done | `gitsrc.archive_tree`: `git archive` in a throwaway repository that holds only the commit, fetched at depth 1 from the kept one, and none of its refs, so `export-subst` expands as after phase 0's depth-1 fetch (`%h` too, however large the kept repository grew); `cas.archive_sha` is phase 0's `tree_sha` byte for byte. Tests: `test_archive_sha_equals_phase0_in_a_kept_repository_with_refs`, `test_export_subst_h_is_abbreviated_as_phase_0_did_in_a_large_kept_repository` (`test_gitsrc.py`, against a frozen copy of phase 0's code), `test_a_format_1_lock_keeps_format_1_and_phase_0_shas` (`test_store_sync.py`) |
| Frozen `sync`, `lib lock` re-resolves | Done | `store/deps.py`: `sync` refuses through `_stale` and `_frozen` before anything is read, naming each entry and `lib lock` / `lib update`; it never writes an entry into the lock. `relock` (`lib lock`, `POST /deps/lock`) fetches and installs only what the lock does not pin as `project.json` names it, with add's checks, and takes no `deps.lock` when nothing is stale. Tests in `test_store_sync.py`: `test_sync_refuses_a_lock_that_no_longer_satisfies_project_json_and_lib_lock_resolves_it`, `test_a_project_with_git_dependencies_and_no_lock_is_refused_and_no_lock_is_made` |
| Offline `sync` | Done | `deps.sync(offline=...)`: from the store's records and objects (no repository read, no git), else from a kept repository that has the commit, else refused naming every entry missing (`_not_here`); `lib sync --offline`, `UNDINE_OFFLINE=1`, `POST /deps/sync {"offline": true}`. An install that matches its lock entry but is not in the store is imported (`_seed`); a format 2 install is checked against its tree, without the store's record, with the modes its marker records (`modes`: the files of mode 100755), else as 100644 with `#!` files as 100755. Tests in `test_store_sync.py`: `test_offline_sync_installs_from_the_store_with_no_fetch`, `test_offline_sync_with_an_empty_store_names_what_is_missing_and_changes_nothing`, `test_an_install_made_before_the_store_is_imported_by_sync_and_then_syncs_offline`, `test_a_failed_online_fetch_is_named_with_its_commit_and_hash`, `test_a_tree_with_an_executable_file_is_ok_and_seeded_without_the_stores_record`, `test_a_script_is_found_by_its_shebang_when_the_marker_has_no_modes`, `test_a_chmod_through_one_project_does_not_cost_another_its_offline_sync`, `test_a_linked_deps_directory_is_refused_and_nothing_outside_is_touched` |
| The store lock and the `--dep-root` rule on kept repositories | Done | `deps._in_store` holds `cas.store_lock(shared=True)` from the first fetch to `register_project`; the checks of rule (e) run before every `gitsrc` call that passes lock data to git, and over HTTP `_source` runs before any read of a kept repository (a callable that `_resolve` calls only when git is about to be read). Tests in `test_store_sync.py`: `test_over_http_a_kept_repository_outside_the_dep_roots_is_not_read_but_the_store_installs`, `test_an_option_looking_locked_commit_is_refused_before_any_git_call`, `test_gc_waits_for_an_operation_between_its_tree_and_its_lock_write`, `test_the_store_lists_the_project_and_its_locked_and_installed_trees` |
| Lock format 2, only from explicit commands | Done | `deps.read_lock` reads formats 1 and 2 (`READS`), `lock_pdk`, `lock_tools`, `entry_hash`; `add`, `update` and `lib lock` write a `tree` entry into a format 2 lock and a `sha` entry into a format 1 one. `pdks/pdkops.py`: `upgrade`, the one writer of format 2, run by `pdk verify --pin` and `lib lock --format 2` (which leaves a format 2 lock's pin alone). Tests: `test_lock_format_2_only_on_an_explicit_command`, `test_lib_lock_format_2_prints_what_it_resolved_and_leaves_a_format_2_pin_alone` (`test_cli_phase1.py`), `test_a_format_2_lock_gets_trees_from_update_and_sync_never_converts_an_entry` (`test_store_sync.py`), `test_a_format_2_lock_is_read_and_lib_add_keeps_its_format_and_pdk_object`, `test_today_s_commands_write_format_1_with_banners_and_a_lock_without_format_is_format_1` (`test_lock_format2.py`) |
| PDK full commit and tree hash, with the exclusion list | Done | `pdks/pin.py`: `parts` (`libs.tech`, each `libs.ref/<lib>`, each other top-level `libs.*`), `excluded` (`__pycache__/`, `*.pyc`, `libs.tech/ngspice/osdi/`), `part_hashes` with the stamp cache in `store/pdk/` (a file written in the last 2 s is read again next time, as git does for its index), `pin_object`, `verify`, `expected_trees` (the phase 6 seam, returns None, so a match is *unverified*). A symlink, fifo or device in a PDK is an error naming it. Tests in `test_pdk_pin.py`: `test_a_sky130_tree_with_pycache_still_matches`, `test_an_ihp_tree_with_compiled_osdi_still_matches`, `test_a_part_hash_is_the_store_tree_hash_of_its_files_with_the_exec_bit_as_mode`, `test_a_changed_pdk_file_is_reported_by_part_and_file`, `test_an_extra_library_verifies_and_a_missing_locked_one_is_reported`, `test_a_second_hash_reads_no_file_and_a_touched_file_is_read_again`, `test_hashing_writes_nothing_into_the_pdk` |
| Prefix to full commit | Done | `pin.resolve_commit`: a unique prefix among the installs of the variant, else `ciel ls-remote --pdk-family F` with the network allowed; several matches refused, listing them. Tests in `test_pdk_pin.py`: `test_a_unique_prefix_is_found_among_installs`, `test_an_ambiguous_prefix_is_refused_listing_both`, `test_an_unknown_prefix_offline_is_refused_saying_how_to_allow_the_network`, `test_with_the_network_the_fake_ciel_ls_remote_gives_the_commit_by_family`; `test_a_format_1_id_is_pinned_to_the_full_installed_commit` (`test_pdk_ops.py`) |
| `pdk verify` and the PDK in `status` | Done | `pdkops.verify` (a full hash, from the stamp cache where unchanged; `--pin` through `upgrade`) and `pdkops.pdk_block`, which `deps.status` uses: it reads the stamp cache only, never the PDK, so a stale cache is *unchecked*; a file that vanishes while it is walked (a `ciel rm` meanwhile) makes the state *error*, not a failed request. The `ciel rm` hint is built from the install found here, never from the lock's family. Unverified, unchecked and not pinned are notes; a requirement that disagrees with the pin, a missing or modified PDK and a missing locked library are issues. Tests: `test_verify_without_pin_writes_nothing_and_pin_writes_format_2`, `test_a_modified_pdk_is_reported_and_pin_re_pins_it_reporting_what_changed`, `test_pdk_block_issues_and_notes` (`test_pdk_ops.py`); `test_a_pinned_pdk_with_a_warm_cache_is_unverified_as_a_note_and_ok`, `test_project_json_changed_by_hand_is_an_issue_naming_set_pdk`, `test_no_stamp_cache_is_unchecked_and_reads_no_file_of_the_pdk`, `test_the_reinstall_hint_names_the_install_found_here_not_the_locks_family`, `test_a_pdk_file_that_vanishes_while_status_walks_it_is_an_error_state_not_a_crash` (`test_status_pdk.py`) |
| `pdk fetch` through ciel, with confirmation | Done | `pdks/ciel.py` (`exe`: `$UNDINE_CIEL`, then the ciel beside Undine's Python, then `PATH`; `fetch` with the ciel family; `ls_remote`), `pdkops.fetch_plan` and `fetch`, `pin.fetch_root`; the CLI asks first (`--yes`), HTTP needs the token and runs a job (`pdks/fetchjobs.py`); one fetch of a PDK commit at a time across processes (an flock, see Locks). Tests: `test_pdk_fetch_is_refused_when_only_a_volare_root_is_scanned`, `test_pdk_fetch_asks_then_installs_with_ciel_and_reports_verify` (`test_cli_phase1.py`), `test_a_pdk_fetch_needs_a_fresh_token_bound_to_what_is_fetched`, `test_no_agent_tool_or_client_method_reaches_a_confirmation_or_a_pdk_fetch` (`test_api_phase1.py`), `test_fetch_with_the_fake_ciel_installs_and_verifies_against_the_lock`, `test_two_processes_fetching_one_pdk_commit_run_one_after_the_other` (`test_pdk_ops.py`), `test_ihp_is_fetched_as_its_family` (`test_ciel.py`) |
| PDK rescan | Done | `Server.rescan_pdks`, `POST /api/pdks/rescan`, run after an HTTP fetch: an id is changed when its root or what the scan derived from its files (layer properties, xschem and KLayout directories, source) differs, so a PDK first scanned half extracted is replaced; new and changed ids are set first and removed ones popped after, so a reader never finds the dict empty; readers iterate a copy (`pick_pdk`); the open layouts and editors of a project whose PDK now resolves differently are dropped. Tests in `test_api_phase1.py`: `test_rescan_finds_a_pdk_added_after_start_and_an_open_project_resolves_it`, `test_rescan_racing_readers_never_shows_an_empty_dict_or_loses_an_unchanged_id`, `test_a_pdk_first_scanned_half_extracted_is_replaced_once_its_files_are_there` |
| `set-pdk` on a format 2 lock | Done | `Project.set_pdk` writes the new PDK's pin (hashed before `meta_lock`) and keeps the format and the tools; the same PDK keeps its pin, so a modified tree stays reported. Tests: `test_set_pdk_on_a_format_2_lock_writes_the_new_pin_and_on_format_1_the_id` (`test_lock_format2.py`), `test_set_pdk_on_a_format_2_lock_hashes_without_the_server_lock` (`test_api_phase1.py`) |
| The PCell worker with `PYTHONDONTWRITEBYTECODE=1` | Done | `pcell._run` and `pcell_worker._load`. Tests in `test_pcell.py`: `test_the_worker_is_run_without_writing_bytecode`, `test_a_worker_run_leaves_no_pycache_in_the_pcell_package` |
| OSDI built into the store | Done | Profile `osdi` entries are model names (`psp103`); `Pdk.osdi_dir` is `store/build/<commit>/<variant>/osdi/` (`local-<root hash>` for a PDK with no version) and `Pdk.osdi_files` looks there, then in the PDK's `libs.tech/ngspice/osdi/`, then in the cache phase 0 compiled into; `scripts/osdi.sh` compiles into the store and moves what the old cache has; `doctor` notes models still in the old cache. Tests in `test_osdi_store.py`: `test_profile_lists_model_names`, `test_an_ihp_simulation_loads_osdi_from_the_store`, `test_osdi_sh_moves_the_old_cache_into_the_store`, `test_store_gc_and_verify_leave_build_alone`; `test_osdi_row_hints_at_moving_the_old_cache` (`test_doctor.py`) |
| Tool version numbers and origins | Done | `tools.py`: `VERSION_RULES` and `parse_version` (the table in [Lockfile](#lockfile)), `locate` (path and origin), `tool_record`, `tools_record` (with `undine_version` and `pixi_lock_sha`); `deps.status` warns of another number or origin and notes another Undine commit or `pixi.lock`; `doctor` shows number and origin per tool, Undine's version and the `pixi.lock` hash. Tests in `test_tools.py`: `test_each_tools_banner_gives_its_number`, `test_an_unmatched_banner_is_kept_as_text_and_a_silent_tool_as_a_question_mark`, `test_locate_labels_where_each_tool_came_from`, `test_undine_version_without_git_and_in_a_checkout`; `test_status_on_format_2_warns_of_another_tool_and_notes_another_commit` (`test_lock_format2.py`); `test_tool_rows_show_number_and_origin` (`test_doctor.py`) |
| `store verify`, `store gc` | Done | `__main__.store_command`: `verify` re-hashes every object and record and removes what does not match (`--dry-run` reports only); `gc` reads the locks and installs of the projects under `--root`, their changesets' worktrees and the store's list, under the exclusive store lock, and removes nothing when a lock cannot be read. Tests in `test_cli_phase1.py`: `test_store_verify_finds_an_edited_object_and_removes_it`, `test_store_gc_keeps_what_projects_here_lock_or_install_and_removes_the_rest`, `test_store_gc_with_an_unreadable_lock_removes_nothing` |
| Changeset worktrees hard-link the dependencies | Done | `changeset._dep_copy`, through `cas.link_or_copy`. Tests in `test_changeset_deps_link.py`: `test_worktrees_link_the_dependency`, `test_copies_where_linking_is_refused` |

Decided while building phase 1 (with the user):

- Format 2 is written by `pdk verify --pin` and by `lib lock --format 2`, through one function; `pkg add` and
  `pkg migrate` come in phase 3 ([Lockfile](#lockfile)).
- A PDK fetch over HTTP is confirmed by a narrow, PDK-only token now, built so that phase 4 generalises it.
- OSDI models that phase 0 compiled into `~/.cache/undine/osdi/<id>/` are still found there until `pixi run osdi` moves
  them into the store; `doctor` says so.
- An *unverified* PDK (trust on first use) is a note, not an issue: `status` stays ok and the badge is not red.
- Dependencies installed before phase 1 are imported into the store by the first `sync`, so they sync offline
  without a fetch.

Differences from the text above, and additions:

- `GET /api/p/{p}/pdk/fetch-plan` is an addition: the fetch dialog shows family, variant, commit, libraries, source
  and root before the person confirms. `POST /api/confirmations` also returns the plan.
- `lib lock --format 2` on a lock that is format 2 already does not pin the PDK again (that would silently accept a
  modified PDK): it says to use `pdk verify --pin`.
- `status` never reads the PDK; after a fetch, a PDK is *unchecked* until `pdk verify` hashes it more than 2 s after
  its last write.
- The CLI's `pdk fetch` cannot rescan a running server; it says to use **Rescan PDKs** or `POST /api/pdks/rescan`.
- `store gc` also clears `tmp/` entries older than a day, and the store's project list drops projects that are gone.

Left for later phases: the `pkg` commands (phase 1 has `lib sync --offline` for the frozen offline sync), manifests,
transitive resolution and the `include` file set (phase 2: a phase 1 tree covers every file of the subdir), index
clones, `pkg migrate`, `--dep-host` and the size monitor during `git fetch` (phase 3: phase 1 checks the size and file
count from `ls-tree` before any blob is read), dependency fetches as jobs, the general confirmation token and the merge
rule for format 2 (phase 4), an HTTP route or button for `lib lock --format 2` and `pdk verify --pin` (the command
line only), expected PDK hashes and `verified_by` (phase 6), pruning `git/` from `store gc`, and keeping PDK fetch jobs
across a restart.

### Phase 2 status

Where each item of phase 2 is in the code, by module and function (`server/undine/`, and `web/src/` for the UI). Tests
are in `server/tests/` (the web's in `web/src/`); as in phase 1, every test runs with a temporary `UNDINE_HOME` and
`UNDINE_STORE` and local git repositories, so none touches `~/.undine`, `~/undine-projects` or the network.

| Item | Status | Where |
|---|---|---|
| `undine-package.toml` | Done | `store/manifest.py`: `parse` (the first problem refused as `<where>: undine-package.toml: <key path>: <what>: <fix>`; unknown keys refused naming the known ones, `[checks]` and `[renames]` kept as tables for phase 5; `tomllib`, else `tomli` on Python 3.10), `select` and `check_files` (the `[files]` include set: `*.sch` matches top-level files only, `rtl/**` any depth, the manifest always in; an include set with no cell, or without an exported cell, refused), `from_files`, `from_tree`, `from_archive`, `from_dir` (store objects re-hashed before they are parsed; a symlinked manifest refused). `store/semver.py`: `Version` (strict SemVer 2.0.0, build metadata ignored in precedence) and `Req` (bare, `^`, `~`, `=`, `*`; comparators and comma lists refused with the phase 3 hint). `gitsrc.blob_tree(files="include")` reads only the included blobs, and `gitsrc.read_file` one file at a commit. Tests: `test_manifest.py` (`test_the_design_example_parses`, `test_git_and_sibling_requirements`, `test_each_refusal_names_its_key_path_and_what_to_do`, `test_select_matches_whole_paths_segment_by_segment`, `test_check_files_wants_a_top_level_cell_and_every_export`, `test_from_files_refuses_a_modified_store_object`, `test_parse_falls_back_to_tomli_on_python_3_10`, ...), `test_semver.py` (`test_precedence_follows_semver_section_11`, `test_requirement_meanings`, `test_ranges_are_refused_with_the_phase_3_hint`, ...), `test_gitsrc_include.py` (`test_the_include_tree_is_the_selected_files_plus_the_manifest_and_nothing_else_is_read`, `test_without_files_or_without_a_manifest_include_gives_the_all_tree`, `test_a_link_or_submodule_is_refused_only_inside_the_include_set`, `test_an_oversized_or_irregular_manifest_is_refused_before_any_other_blob_is_read`, `test_read_file_reads_one_file_without_the_tree`, ...) |
| Transitive dependencies from manifests (git URLs) | Done | `store/resolve.py`: `resolve` (the walk: one node per package name, each problem collected as a `Problem` with its code and chains, `ResolveError` once), `same_package` (equal trees, or the mixed files modes rule), `chain_text`, `lock_graph`, `reachable`, `incomplete`. `store/locked.py`: `files_of` and `manifest_of`, a locked entry's files and manifest from the store or a verified install, else `NotAvailable` ("run lib sync first"). `store/deps.py`: `add`, `update` (only the named requirement's subtree), `remove` (from the lock graph, no fetch; keeps what another package still places), `relock` (`upgrade=` for `lib lock --format 2`), all through `_Graph`; `NotDirect` for a name a package placed, `NeedsFormat2` on a format 1 lock. Tests: `test_resolve.py` (`test_conflicting_requirements_name_both_chains`, `test_a_phase_1_entry_and_an_include_requirement_at_one_commit_agree_and_the_locked_one_stands`, `test_a_sibling_is_read_at_the_requirer_s_commit_with_a_root_relative_subdir`, `test_every_problem_of_a_requirement_is_collected_in_one_error`, `test_a_cycle_is_walked_once`, ...), `test_locked.py`, `test_deps_phase2.py` (`test_update_moves_a_package_and_its_dependencies_and_drops_what_it_no_longer_needs`, `test_update_of_a_placed_name_is_refused_and_remove_keeps_a_name_another_package_places`, `test_a_fresh_clone_must_sync_before_add`, `test_format_1_refuses_dependencies_and_another_name_and_adds_a_plain_package_as_a_sha_entry`, `test_relock_upgrade_writes_format_2_once_and_a_refusal_leaves_format_1`, `test_plain_dependencies_keep_phase_1_entries_and_a_phase_1_entry_is_not_converted`, `test_a_remote_manifest_may_not_name_a_local_repository`, `test_over_http_dep_root_applies_to_the_urls_a_manifest_names`, ...) |
| Project-wide local names, a package installed under each | Done | `resolve.resolve` gives each node its names (the requirers' keys and the package's own name) and refuses a name claimed for two packages or reserved (casefolded); `deps._reserved` and `deps._shadowed` (project and PDK libraries, `devices`, Xschem's library folders, `symbol_paths` folders); `deps._apply` and `deps.sync` install one tree under every name, hard-linked from the store, each with its marker (`name`, `direct`, `package`, `version`, `via`). `store/project.py`: a transitive name is a read-only dependency `Library` with `package`, `direct` and `via`, learnt from markers (cached by inode, mtime and size; the lock is never read); `create_library` refuses an installed name. Tests: `test_an_alias_installs_one_package_under_both_names`, `test_one_local_name_for_two_packages_is_refused_naming_both_requirers`, `test_two_local_names_differing_only_in_case_are_one_name`, `test_a_reserved_local_name_is_refused_naming_the_chain` (`test_resolve.py`); `test_an_alias_installs_the_package_under_both_names_sharing_inodes`, `test_one_local_name_for_two_packages_is_refused_naming_both` (`test_deps_phase2.py`); `test_a_transitive_install_is_a_read_only_dependency_library_and_the_lock_is_never_read`, `test_markers_and_manifests_are_read_once_until_they_change`, `test_create_library_refuses_an_installed_local_name` (`test_project_phase2.py`); `test_a_new_local_name_may_not_be_a_folder_of_xschems_library_or_of_symbol_paths`, `test_a_name_project_json_already_gives_keeps_working_and_status_notes_what_it_shadows` (`test_reserved_names_phase2.py`) |
| Cell-name disjointness | Done | `resolve.cell_names` (top-level logic views, and layout-only `.oas` cells) and `resolve.check_disjoint`, fed by `deps._project_cells` and `deps._pdk_cells`; refused only with a package with a manifest on one side; `status` reports the rest (`Project.dependency_cell_names`). Tests: `test_check_disjoint`, `test_cell_names_are_top_level_logic_and_layout_only_cells` (`test_resolve.py`); `test_cell_names_clash_between_packages_but_not_with_one_package_under_two_names_or_a_layout_copy` (`test_deps_phase2.py`); `test_a_project_cell_named_like_a_packages_cell_is_an_issue_a_layout_copy_is_not` (`test_store_sync_phase2.py`) |
| PDK compatibility | Done | `pdks/compat.py`: `project_target` (the lock's pin, or a format 1 id as a prefix), `check` (family through `family_of`, variants, full commits, `built_with`; a PDK with no version or no PDK refused with what to do), `note` (drawn on another commit than used), `tag_commits` (the phase 6 seam, knows no tag), `locked_incompatible` and `IncompatiblePdk`, which `Project.set_pdk` raises before hashing and again under `editing()`; `pdkops.upgrade` and `verify --pin` report a pin a locked package does not accept as `incompatible`. Tests: `test_pdk_compat.py` (`test_family_variant_and_commit_accepted_and_each_refusal`, `test_a_format_1_prefix_matches_a_full_commit`, `test_compat_only_is_refused_with_the_phase_6_hint`, `test_a_pdk_without_a_version_and_no_pdk_are_refused`, `test_ihp_family_accepts_its_variant`, ...), `test_pdk_check_names_a_transitive_package_and_its_chain` (`test_resolve.py`), `test_set_pdk_is_refused_listing_every_locked_package_that_does_not_accept_it`, `test_plain_git_dependencies_never_block_set_pdk`, `test_upgrade_reports_locked_packages_the_new_pin_does_not_accept` (`test_project_phase2.py`) |
| The trust rule for code blocks and RTL | Done | `store/trust.py`: the scanners (`spice_findings`, `rtl_findings`, `tcl_findings`, `scan_dir`), the record (`key_of`, `trusted_keys`, the real project's keys only in a changeset worktree, `untrusted_dirs`, `set_trusted`, `drop`) and the gates (`check_netlist`, `check_tcl`, `check_rtl`, raising `UntrustedContent`). `schematic.spice` records each line's `Source` (cell body and symbol) and `owner` names it. The gates run in `Server.start_sim` (rules `sim`), at `Run.launch` for KLayout and netgen LVS (`Server._paths_gate`, rules `paths`), in the netlist route with `backend=xschem` (Tcl only), in `rtl.lint` and `rtl.submit_sim`, and in `librelane.submit` (a flow whose top library is a dependency; `run_librelane` returns the refusal, the run route answers 403 `untrusted`, and the sign-off suite returns an LVS refusal per check with its code and findings). Tests: `test_trust_scan.py` (`test_control_blocks_and_control_lines_with_their_lines`, `test_paths_after_substitution`, `test_an_include_into_a_dependency_install_is_refused`, `test_rtl_findings`, `test_tcl_findings_in_a_symbol`, `test_scan_dir_lists_cells_rtl_and_tcl`, ...), `test_netlist_sources.py` (`test_code_blocks_are_attributed_to_their_cell_body_with_the_exact_text`, `test_a_trusted_symbol_placed_in_a_dependency_cell_is_attributed_to_that_cell`, `test_owner_names_packages_plain_dependencies_and_directories`, ...), `test_trust_run.py` (below) |
| Frozen sync, leftovers and status over the lock graph | Done | `deps.sync` installs every entry of the lock graph under each name (`_plan`, `_frozen`, `_fetch_missing`), from the store when it can, rewrites a package's marker when its names or requirers moved, and removes what no `project.json` dependency still reaches; plain-dependency rows stay `{name, action, commit}`. `deps.status` (`_Installed`) gives one row per local name with `package`, `version`, `names`, `direct`, `via`, `chain`, `trusted`, `flagged`, `internal_cells` and `pdk`. Tests in `test_store_sync_phase2.py`: `test_a_frozen_sync_installs_every_package_and_name_from_the_store_with_no_fetch`, `test_offline_with_an_empty_store_names_each_missing_package_once_and_changes_nothing`, `test_plain_dependency_rows_stay_name_action_commit`, `test_a_phase_1_entry_whose_manifest_has_files_still_syncs_every_file_from_git`, `test_removing_a_dependency_from_project_json_removes_what_only_it_reached`, `test_sync_refuses_an_incomplete_graph_or_a_name_claimed_twice_before_anything_is_read`, `test_status_rows_carry_the_package_fields_and_one_row_per_local_name`, `test_status_reports_an_incompatible_package_and_notes_a_built_with_difference`, `test_a_broken_installed_manifest_is_an_issue_row_not_a_crash`, `test_a_plain_dependency_with_control_gets_the_trust_note` |
| `[exports]` | Done | `Library.internal` and the `internal` mark in `Library.cells`; `Server.symbols` and the agents' `list_symbols` leave internal cells out; they still resolve, netlist and may be placed by name. Tests: `test_a_dependency_row_shows_its_package_and_cells_outside_exports_are_internal` (`test_project_phase2.py`), `test_symbols_leave_out_internal_cells_and_the_cell_list_marks_them` (`test_api_phase2.py`) |
| CLI | Done | `__main__.py`: `lib add --package`, the transitive (`+`), moved and dropped package lines of `lib add` / `update` / `remove`, `via` chains and alias rows in `lib status`, the `~` line for untrusted content, `lib trust PROJECT NAME [--untrust] [--yes]` (lists the findings, asks; no terminal and no `--yes` is refused), `lib lock --format 2` converting and resolving in one write, `set-pdk` refused with every package. Tests in `test_cli_phase2.py`: `test_lib_add_prints_the_packages_it_brings_and_what_is_not_trusted`, `test_lib_status_shows_via_and_alias_rows_and_remove_names_what_stays_placed`, `test_lib_update_prints_the_packages_it_brings_moves_and_drops`, `test_lib_trust_lists_asks_and_writes_trusted`, `test_lib_lock_format_2_converts_and_resolves_in_one_write_or_leaves_format_1`, `test_set_pdk_to_a_pdk_locked_packages_refuse_lists_each_and_changes_nothing`, `test_a_pdk_pinned_by_lib_lock_format_2_that_a_package_refuses_is_a_warning`, `test_lib_sync_shows_a_package_the_locked_pdk_does_not_fit` |
| HTTP | Done | `server/api.py`: the `/deps` routes return the new fields (`resolved`, `flagged`, `packages_changed`, `removed_packages`, `kept`); refusals carry codes, with `detail` = the CLI's message: 409 `resolution_refused` (`problems`), 409 `needs_lock_format_2`, 400 `not_direct` (`placed_by`), 409 `pdk_incompatible` (`packages`), 409 `not_synced`, 403 `untrusted` (`findings`); `GET /api/p/{p}/trust`, the `trust` confirmation kind and `POST /api/p/{p}/trust`. Tests in `test_api_phase2.py`: `test_post_deps_reports_the_transitive_packages_and_a_conflict_is_409_with_both_chains`, `test_update_and_remove_of_a_placed_name_are_400_not_direct_naming_the_placer`, `test_the_new_refusals_have_their_codes_and_keep_the_message_as_detail`, `test_trust_needs_a_fresh_trust_token_bound_to_the_key_and_the_installed_tree`, `test_a_pdk_tokens_messages_are_phase_1s`, `test_post_pdk_to_a_pdk_a_locked_package_does_not_accept_is_409_with_the_packages`; `test_http_sim_refusal_is_403_untrusted_with_findings` (`test_trust_run.py`) |
| Agents | Done | `agents/agent_tools.py`: `run_simulation`, `check_specs`, `sweep`, `lint_rtl` and `run_rtl_sim` return a trust refusal as their result, unchanged; no tool, `undine call` command or client method reaches the trust routes or a confirmation. `agents/agent.py`'s prompt and the skill's `verification.md` say that trusting is a person's action. Tests in `test_agent_phase2.py`: `test_no_tool_client_method_or_call_reaches_trust_or_a_confirmation`, `test_the_prompt_says_trusting_a_dependency_is_a_persons_action`, `test_run_simulation_on_an_untrusted_dependency_returns_the_refusal_unchanged`, `test_check_specs_and_sweep_pass_the_refusal_on`, `test_run_rtl_sim_and_lint_rtl_on_an_untrusted_dependency_return_the_refusal` |
| Web UI | Done | `App.tsx`: dependency rows with package, version, `via` and alias tags, Update / Remove on direct rows only, the lock dialog's package and trust columns, a refused add listing every problem, a refused PDK move listing the packages; `TrustDialog.tsx` (findings by file, then the token, then trust; untrust); `UntrustedNotice.tsx`, and `Refused` in `panels/SimPanel.tsx` for the simulation, LVS, intent, netlist and RTL views. Tests: `App.phase2.test.tsx`, `TrustDialog.test.tsx`, `panels/UntrustedPanels.test.tsx` |
| Test: diamond installs once | Done | `test_a_diamond_reads_the_shared_package_once_and_makes_one_node` (`test_resolve.py`), `test_diamond_installs_c_once_hard_linked_and_fetches_it_once` (`test_deps_phase2.py`), `test_packages_of_one_repository_and_commit_are_fetched_once` (`test_store_sync_phase2.py`) |
| Test: conflicting requirements name both chains | Done | `test_conflicting_requirements_name_both_chains` (`test_resolve.py`), `test_conflicting_requirements_name_both_chains_and_nothing_is_written`, `test_a_transitive_name_becomes_direct_for_the_same_package_and_conflicts_for_another_version` (`test_deps_phase2.py`), `test_post_deps_reports_the_transitive_packages_and_a_conflict_is_409_with_both_chains` (`test_api_phase2.py`) |
| Test: two packages with a `comparator` are refused naming both | Done | `test_check_disjoint` (`test_resolve.py`), `test_cell_names_clash_between_packages_but_not_with_one_package_under_two_names_or_a_layout_copy` (`test_deps_phase2.py`): `comparator: analog_ip 1.2.0 and sar_common 1.3.1`, nothing written |
| Test: a local alias resolves, also with the Xschem netlister backend | Done | `test_a_local_alias_resolves_with_undines_netlister`, `test_a_local_alias_resolves_with_the_xschem_netlister` (`test_alias_netlist.py`), `test_an_alias_installs_the_package_under_both_names_sharing_inodes` (`test_deps_phase2.py`) |
| Test: two libraries giving one local name different packages are refused | Done | `test_one_local_name_for_two_packages_is_refused_naming_both_requirers`, `test_two_local_names_differing_only_in_case_are_one_name` (`test_resolve.py`), `test_one_local_name_for_two_packages_is_refused_naming_both` (`test_deps_phase2.py`) |
| Test: an incompatible PDK is refused | Done | `test_an_incompatible_pdk_is_refused_directly_and_through_a_chain` (`test_deps_phase2.py`), `test_set_pdk_is_refused_listing_every_locked_package_that_does_not_accept_it` (`test_project_phase2.py`), `test_set_pdk_to_a_pdk_locked_packages_refuse_lists_each_and_changes_nothing` (`test_cli_phase2.py`), `test_post_pdk_to_a_pdk_a_locked_package_does_not_accept_is_409_with_the_packages` (`test_api_phase2.py`), and the dialog in `App.phase2.test.tsx` |
| Test: a dependency cell with `.control` or an absolute `.include` is refused until trusted | Done | `test_trust_run.py`: `test_a_dependency_cell_with_control_or_an_absolute_include_is_refused_until_trusted`, `test_a_project_symbols_value_set_in_a_dependency_cell_is_refused`, `test_a_dependency_symbols_template_default_is_refused_in_its_own_cell_and_in_a_project_cell`, `test_an_absolute_include_of_a_file_in_the_dependencys_own_install_is_refused`, `test_star_hash_lines_and_control_in_a_dependency_subcircuit_are_refused`, `test_includes_under_the_pdk_root_and_the_generated_corner_line_pass`, `test_a_project_cells_own_control_runs_and_display_netlists_only_warn`, `test_lvs_is_refused_only_at_launch_and_only_for_paths`, `test_the_xschem_route_refuses_untrusted_tcl_whatever_the_prepass_makes_of_it`, `test_rtl_of_an_untrusted_dependency_is_refused_by_simulation_and_lint_until_trusted`, `test_a_changeset_worktree_sees_trust_given_in_the_project_after_it_opened`; `test_trust_goes_with_an_update_to_another_commit_and_start_sim_refuses_again` (`test_deps_phase2.py`); the refusal in the panels in `panels/UntrustedPanels.test.tsx` |

Decided while building phase 2 (proposed in the phase 2 PR, open to review):

- **Git requirements before the index.** A manifest names a dependency as `{ git, rev, subdir?, package?, version? }`
  (git and rev required, so published data never means a moving HEAD; `package` defaults to the key) or as
  `{ subdir }` alone, a sibling in the same repository and commit, its subdir relative to the repository root. The
  index forms are parsed and refused at resolution, showing the git form.
- **Semver.** `Version` and `^`, `~`, `=`, bare and `*`; comparators and comma lists wait for phase 3, where an index
  gives them versions to choose among.
- **Where packages go in the lock.** Direct dependencies stay in `libraries`; packages reached only through manifests
  go to `packages`, with no `index` key. A package name appears once across both tables.
- **What a version of a git package is:** (package name, tree), the identity taken from the tree's manifest even for
  a phase 1 entry, and `git:<local>` only for a tree with no manifest. When `files` modes differ (a phase 1 entry over
  every file and a phase 2 include set), equal (git, commit, subdir) decide, and the locked entry stands as it is.
- **The file set of a tree** is an explicit `files: "include"`, written only by `add`, `update` and `lib lock`, never
  by `sync`; a manifest without `[files]` gives the phase 1 tree id.
- **Fixed packages** are read from the store, else a verified install, else refused with "run lib sync first"
  (`store/locked.py`; HTTP 409 `not_synced`), by `add`, `update`, `lib lock` and `set-pdk` alike: none of them
  fetches what it does not re-resolve.
- **An entry locked before phase 2** (format 1, or a phase 1 format 2 entry: no `package` recorded) is held as that
  plain entry by a command about another dependency: its manifest gives its identity, but its dependencies, its own
  name and its `[pdk]` are neither fetched, checked nor written, and its entry and install stay byte for byte, unless
  a package the command resolves now needs it (then it gets `package`, `version` and `names`, and no `deps` until it
  is resolved). Only `lib update NAME` or `lib lock` (format 2, or `--format 2`) resolves it. `lib status` notes one
  whose package is not installed under its own name, and one whose dependencies are not locked on a format 1 lock;
  on a format 2 lock unlocked dependencies stay an issue.
- **One package, one name in `project.json`.** A second direct name for a package locked with its fields is refused
  (`y: a 1.0.0 is a dependency of this project already, as x`), as it would be a second entry for one package.
  A local name spelled like an existing library in another case is refused too (`library IP exists (as ip)`), as is
  one package reached under two spellings: on a case-insensitive file system they are one directory.
- **The self-name.** A package with a manifest is also installed under its own `[package]` name, since its own
  schematics place its cells as `<name>/<cell>.sym`.
- **Reserved local names**: project and PDK libraries, `devices`, every subdirectory of Xschem's library and of
  `project.json` `symbol_paths`; compared casefolded; names already in `project.json` are only noted.
- **Only `project.json` may name a plain git dependency**: a manifest's dependency needs a manifest of its own
  (identity, version and `[pdk]` come from it).
- **Format 1.** Manifests on a format 1 lock are read and checked; `[dependencies]`, or a name other than the key, is
  refused with the `lib lock --format 2` hint. `lib lock` keeps phase 1's order (relock, then upgrade) and, only when
  relock needs format 2, resolves with an in-memory upgrade and writes once.
- **No built-in compatibility tags.** `compat.tag_commits` is the phase 6 seam; the checks are family, variants,
  full commits and `built_with`; a package without `[pdk]` is noted, not checked.
- **No PDK, a PDK with no version, a format 1 id**: refused with a reason and what to do; a 12-hex id matches as a
  prefix; nothing is fetched.
- **A pin that moves** (`pdk verify --pin`, `lib lock --format 2`) to a PDK a locked package does not accept is
  reported as `!` warnings and status issues, not refused: the pin records what is installed.
- **Cell clashes** are refused only with a package with a manifest on one side; project libraries contribute logic
  views only; a package's layout-only cell is excused against its own closure and the PDK; the rest are status notes.
- **Trust.** The key is `name@version`, or `local@commit12` without a manifest, dropped on any commit or tree change.
  The rules are those under [Security](#security); the only absolute root allowed is `pdk.root`, and a line is
  attributed to both its cell body and its symbol.
- **Where each gate is**: simulation at `start_sim` (before a run is reused), LVS at `Run.launch` (paths only), the
  Xschem backend in its route (Tcl only, whatever the pre-pass does), Verilator in `rtl.py`, LibreLane in
  `librelane.submit` (Verilator's full rule on the top library). A project that declares no git dependency is not
  looked into, so a linked `.undine` with nothing to install refuses nothing.
- **Changeset worktrees** read trust from the real project only: their own `project.json` is a copy from when they
  opened, so a key taken back in the project is taken back in them too.
- **A person trusts** through `lib trust`, the HTTP route with a single-use `trust` token, and the dialog; no agent,
  client or `undine call` path reaches any of them.
- **A project cell named like a dependency cell** is not refused when it is made: `status` reports it (an issue
  against a package, a note against a plain dependency), and phase 0's netlist-time check stays the backstop.
- **Local URLs in a fetched manifest** are refused unless the requirer is local; every transitive URL goes through
  `_check_spec`, and over HTTP through `_source`.
- **`[exports]`**: internal cells are hidden from the picker and `list_symbols` and marked internal; they still
  resolve and netlist, and placing one is allowed (flagging it is `pkg check`'s, phase 5).
- **`add` and `update` re-resolve only the named requirement's subtree**; every other locked package is held fixed,
  since git revs like `main` move.
- **Python stays >= 3.10**, with the `tomli` fallback.
- **Phases 0 to 2 are released together.** Should phase 1 ship first, its `read_lock` must refuse a format 2 lock with
  `packages` or with a `libraries` entry carrying `files`, `package`, `version`, `names` or `deps`, or a phase 1
  Undine would take self-name and alias directories for leftovers and delete them.
- **Commands.** `lib` is extended and gains only `lib trust`; chains appear in `lib status`. The `pkg` group starts in
  phase 3.
- **Unknown keys in a manifest** are refused, naming the key and the known keys, so a typo such as `revision =` does
  not silently mean HEAD.

Differences from the text above, and additions:

- `[dependencies]` takes the git form `{ git, rev, subdir?, package?, version? }` and the sibling form `{ subdir }`,
  which [Packages](#packages) does not show; its index forms are refused until phase 3.
- Lock entries have a `files` key: `"include"` when the tree covers the manifest's include set, absent when it covers
  every file of the subdir ([Lockfile](#lockfile)'s note).
- Direct dependencies with a manifest stay in `libraries`, with `package`, `version`, `names` and `deps`; `packages`
  holds only what manifests reach. The example in [Lockfile](#lockfile) puts every package in `packages`.
- An entry over every file (phase 1) and a requirement for the include set of the same (git, commit, subdir) are one
  package; the locked entry stands. Identity comes from the tree's manifest, `git:<local>` only without one.
- No compatibility tag is built in: a package that lists only `compat` tags is refused with a hint to list its
  commits under `commits`, until profile packages define tags (phase 6).
- A package is also installed under its own name (the self-name), not only under the names requirers give it.
- Local names may not be `devices` or a subdirectory of Xschem's library or of a `symbol_paths` entry, besides
  project and PDK library names ([Resolution](#resolution) rule 5 names only these).
- Trust: the key of a dependency without a manifest is `<local>@<commit12>`; HTTP has a `trust` confirmation kind; a
  changeset worktree reads trust from the real project only. [Security](#security) says an agent can propose trust; in
  phase 2 it cannot (proposals are phase 4).
- The Xschem netlister backend is gated on Tcl only (`tcleval(`, and `expr(` with `[` or `$`), since its netlist is
  for display and Xschem evaluates nothing else.
- LVS and netgen are gated when the run launches, with the paths rule only (no `.control` rule: they run none).
- The trust rule refuses more than [Security](#security) lists: `*#` lines, XSPICE file parameters, paths that keep
  `$` or `~`, absolute paths into the dependency's own install, and in RTL `$system`, DPI, file tasks, `` `include ``
  paths and `` `define `` bodies.
- Semver ranges with comparators (`>=`, `<`, comma lists) are deferred to phase 3, refused with a hint.
- `tomllib` has a fallback: on Python 3.10 Undine reads manifests with `tomli` (`tomli>=1.1; python_version <
  '3.11'` in `server/pyproject.toml`), so the documented non-pixi install keeps working.
- Three behaviour changes for projects with plain git dependencies, each with a status note and a troubleshooting
  entry in [Troubleshooting](../troubleshooting.md): the trust gate applies to every dependency; `status` reports a
  project-library logic cell named like a dependency's; a new local name may not be a subdirectory of Xschem's
  library or of a `symbol_paths` entry. Nothing else that worked in phase 1 is refused, and a format 1 lock or a
  phase 1 format 2 lock keeps working unchanged.

Left for later phases: the package index, index clones, version ranges with comparators, backtracking, yank, `index`
in lock entries, the `pkg` commands (`search`, `info`, `add`, `update`, `lock`, `outdated`, `tree`, `why`),
`pkg migrate`, `--dep-host` and the size monitor during a fetch (phase 3); dependency operations in changesets and
their review (which will reuse `trust.scan_dir`), agents proposing trust or dependencies, the general confirmation
token, dependency fetches as jobs and the merge rule for `undine.lock` (phase 4); `pkg init / check / pack /
publish`, the meaning of `[checks]` and `[renames]`, SPDX parsing, refusing git dependencies when publishing,
checking that a package places only the local names it declares, and refreshed layout copies (phase 5); compatibility
tags as profile data, the commits whose model files differ in a `set-pdk` refusal, and expected PDK tree hashes
(phase 6). Not planned in a phase yet: several major versions of one package and per-library local-name tables
([Decisions](#decisions) 3 and 5), a marker for PDK-neutral packages, rewriting a package's relative `.include` paths
to its install directory, RTL mixed across libraries, refusing a project cell named like a dependency cell when it is
made, renamed, copied or imported, protecting Xschem opened outside Undine and a `project.json` `trusted` list
edited by a process with a shell, binding the trust key to the tree, and an HTTP route or button for
`lib lock --format 2` and `pdk verify --pin` (the command line only, as in phase 1).

<a id="open-questions"></a>

## Decisions

Decided with the user on 2026-11-05. Each item gives the decision and why.

1. **The manifest is TOML.** People write it by hand, and TOML takes comments; Python reads it with `tomllib`.
2. **The default index is a repository in the kadomoto-lab GitHub organisation** (`kadomoto-lab/undine-index`, a placeholder
   name, to be created); for now kadomoto reviews its PRs. One place next to Undine is enough while publishers are few.
3. **Several major versions of one package in a project are refused.** Subcircuits and layout cells are named by cell
   name, so two versions of one cell would collide in one netlist or layout.
4. **A cell-name clash between packages is a resolution error** that names the cell and both packages. Cell names stay
   as their authors wrote them, and the clash is found before anything is placed.
5. **Local names are project-wide**, a package installed under each name it is placed by. The Xschem netlister backend
   and Xschem outside Undine search one path list and cannot express per-library tables.
6. **A person's own add also goes through a changeset, adopted with one click.** One path for people and agents, so
   review, merge at adopt and evidence work the same way.
7. **Install from the store by hard link** (copies across file systems or with `UNDINE_STORE_LINK=copy`). A second
   local name or a changeset's working copy then costs no disk.
8. **One PDK tree hash per library** (and one for `libs.tech`). A PDK installed with more libraries still verifies,
   and a change is reported for the library it is in.
9. **Publishing rests on the publisher's `pkg check`, with a link to its report in the index PR.** DRC and LVS in CI
   need the PDK there (1.1 GB per sky130 variant); the report is bound to the commit and tree, so the reviewer sees
   what was checked. DRC / LVS in the index CI comes later.
10. **The PDK profile package defines compatibility tags, at one level (layout and models).** The profile already lists
    each PDK commit with its tree hashes, and a layout-only level would let spec results change under a published cell.
11. **For now only git hashes pin content: the commit and the tree hash.** They detect any change of content; signing
    comes later.
12. **Tools are recorded as version number and origin per tool, plus Undine's version and its `pixi.lock` hash; a
    mismatch only warns.** pixi already pins the tools, and a refusal would block someone whose tool differs by a
    patch release.
13. **Availability: option (b).** `pkg vendor` writes the pinned contents into the tapeout bundle together with
    `pkg licenses` ([Fetch and store](#fetch-and-store)). The tapeout evidence then does not depend on publishers
    keeping their repositories, with no mirror service to run.

### Later

- Signing: signed index commits or signed package tags.
- DRC / LVS in the index CI (and regenerating PCell variants there), with a PDK in CI.
- Several major versions of one package in a project, by prefixing subcircuit and layout cell names by library.
