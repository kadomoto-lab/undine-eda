# Troubleshooting and FAQ

This page lists the problems people run into most often, each with its cause and the fix, and then answers common
questions about Undine. Most entries start from a message you see: search this page for a few words of it.

If your problem is not here, see [How do I report a bug?](#how-do-i-report-a-bug).

## Start here: run the check

Most problems with a new installation are a missing tool or PDK. Run the check first:

```sh
./setup.sh check          # or: pixi run doctor
```

Each line says what was found and, when something is missing, the command that fixes it:

| Mark | Meaning |
|---|---|
| `✓` | found |
| `✗` | missing and `required`: the server does not start, or shows nothing |
| `-` | missing and `recommended` (a feature is off, such as simulation, DRC / LVS or PCells) or `optional` |
| `!` | a note, such as a PDK version Undine no longer supports |

The last line is `ready: python -m undine serve`, or `required items are missing` (exit code 1). The
[Install](getting-started/install.md#3-check-and-fix) page explains the levels.

When something fails while Undine runs, look at the server's output. Where it is depends on how you started the server:

| How you started it | Where its output goes |
|---|---|
| `pixi run serve` | the terminal |
| `./scripts/restart.sh` | `$UNDINE_LOG`, default `/tmp/undine.log` |
| an AI app started it (`undine mcp-stdio`) | `$UNDINE_LOG`, default `~/.undine/serve.log` |

A simulation, DRC or LVS job also keeps its own folder, `<project>/runs/<job>/`, with `status.json` and the tool's log
([Data layout](reference/data.md)).

## Installation

### `pixi not found`

**Cause.** `setup.sh` installs everything with [pixi](https://pixi.sh/), which is not installed, or the shell you use
was opened before pixi was installed.

**Fix.** Install pixi, open a new shell, and run `./setup.sh` again:

```sh
curl -fsSL https://pixi.sh/install.sh | sh
```

### `failed: <step> -- see the messages above`

**Cause.** One of the steps of `setup.sh` (`install`, `ngspice`, `web`, `pdk` or `osdi`) failed. The other steps still
ran, and the check ran at the end.

**Fix.** Read the messages of that step above the summary, fix the cause, and run only that step again, for example:

```sh
./setup.sh ngspice
```

Every step can be re-run at any time. `./setup.sh --help` lists them.

### The KLayout application is not on PATH

**Cause.** DRC and LVS run the KLayout *application* (`klayout`) with the PDK's decks. pixi cannot provide it: it
comes from klayout.de as a system package. Without it, the check shows `klayout (DRC / LVS)` as missing, and a DRC or
LVS run fails with `klayout not found (set UNDINE_TOOLS or put it on PATH)`.

**Fix.** Install KLayout 0.30 or newer from [klayout.de/build.html](https://www.klayout.de/build.html).
`./setup.sh check` prints the download and install commands for your distribution; the table in
[Per distribution](getting-started/install.md#per-distribution) in Install lists the packages. Then check:

```sh
klayout -v
```

The server uses the `klayout` *Python module* from PyPI (through pixi) for the layout database; that one is installed
by `setup.sh` and is a separate thing from the application.

### `pmap is missing`, or sky130 LVS fails at once

**Cause.** The sky130 KLayout LVS deck calls `pmap` to log memory use, and fails without it. It is part of any normal
Linux installation, but minimal container images leave it out.

**Fix.** Install the package that provides it:

=== "Debian, Ubuntu"

    ```sh
    sudo apt install procps
    ```

=== "RHEL, Rocky, AlmaLinux"

    ```sh
    sudo dnf install procps-ng
    ```

### My distribution is too old (CentOS 7)

**Cause.** The pixi environment needs glibc 2.28 or newer (Node.js for the UI needs it). CentOS 7 has glibc 2.17.

**Fix.** Use a supported system: Ubuntu 20.04 or newer, RHEL / Rocky / AlmaLinux 8 or 9, or WSL2 on Windows. Check your
glibc with `ldd --version`. See [Before you start](getting-started/install.md#1-before-you-start) in Install.

### `undine._core (Rust core)` is missing

**Cause.** The server has a native core written in Rust (`server/src`), which `pixi install` compiles into the server
package. The build did not run or failed, or the Rust code changed (after a `git pull`) and was not rebuilt.

**Fix.** Run `pixi install` (or `./setup.sh install`) and read its output for a compiler error. Without pixi, the build
needs `cargo` (for example from rustup); see [Install without pixi](getting-started/install.md#without-pixi).

### `web UI build (web/dist)` is missing, or the UI looks out of date

**Cause.** The browser UI is built into `web/dist`. It was never built, or you updated the checkout and the build is
from the older code.

**Fix.** Build it again, then reload the page:

```sh
./setup.sh web            # same as: pixi run web
```

After every `git pull`, `./setup.sh` (all steps) brings everything up to date: the ngspice and PDK steps are skipped
when there is nothing to do, and the UI is rebuilt.

### `ngspice not found`, or a tool installed another way is not used

**Cause.** Undine looks for ngspice, Xschem, Netgen and Magic in the pixi environment first (`$UNDINE_TOOLS`, which
`pixi run` sets), then on `PATH`. A server started with a Python outside pixi does not see the pixi environment's tools.
The check's `pixi environment` line then says `none (not run with pixi)`.

**Fix.** Start Undine through pixi: `pixi run serve`, or `pixi run python -m undine ...`. To use tools from elsewhere
(apt, IIC-OSIC-TOOLS, your own builds), put them on `PATH` or set `UNDINE_TOOLS` to a prefix whose `bin/` holds them
([Configuration](reference/cli.md#configuration-environment)).

### `PCell environment` is missing

**Cause.** PCells run in separate Python environments: `pcell` (gdsfactory 8 with kfactory, for sky130 and IHP) and
`pcell-gf180` (gdsfactory 7.9, for GF180MCU). Placing a PCell then fails with
`the PCell environment is not installed: pixi install -e pcell`.

**Fix.**

```sh
pixi install -e pcell
pixi install -e pcell-gf180     # only for GF180MCU
```

Without pixi, point `UNDINE_PCELL_PYTHON` (and `UNDINE_PCELL_GF180_PYTHON`) at a Python that has those packages.

## PDKs

### No PDK found (`a PDK` in the check, or `undine: no PDK sky130A`)

**Cause.** Undine found no PDK. It looks in `$PDK_ROOT`, `~/.ciel` and `~/.volare`.

**Fix.** Install the sky130 PDK at the version Undine is tested with:

```sh
./setup.sh pdk            # the tested SKY130 version, then the check
```

Then list what Undine finds:

```sh
pixi run python -m undine pdk list
```

For GF180MCU and IHP SG13G2, see [Installing a PDK](guide/pdks.md#installing-a-pdk).

### I installed a PDK, but Undine does not list it

**Cause.** The server looks for PDKs when it starts, so a PDK installed while it runs is not there yet. Or the
PDK is in a directory Undine does not search: you set `PDK_ROOT` in one shell, but the server was started from another
one (or by an AI app) without it.

**Fix.** Click the PDK badge and choose **Rescan PDKs** (or `POST /api/pdks/rescan`): the server looks again, with no
restart. A restart does the same and loses no work. If the PDK is still missing, make `PDK_ROOT` part of the server's
environment (for `./scripts/restart.sh` and for a server an AI app starts, put `PDK_ROOT=...` in `~/.undine/env`),
or give the PDK roots with the global option
`--pdk-root DIR`, which comes before the command, can be repeated, and replaces the default list:

```sh
pixi run python -m undine --pdk-root /opt/pdks --pdk-root ~/.ciel serve
```

`pixi run python -m undine pdk list` shows the PDKs found, with the directory of each. See
[Where Undine looks for PDKs](guide/pdks.md#where-undine-looks-for-pdks).

### `PDK sky130A@… is not on this server`

**Cause.** The project is pinned to an exact PDK version (the `pdk` field of its `project.json`), and that version is
not installed on this machine, for example after you cloned the project on a new computer.

**Fix.** Fetch it: click the PDK badge and choose **Fetch...**, check what the dialog shows, and click **Fetch**; the
project finds the PDK when ciel is done. From the command line (then **Rescan PDKs** in a running server):

```sh
pixi run python -m undine pdk fetch PROJECT            # add --network when the 12-character id is not installed here
```

Or move the project to a version you have:

```sh
pixi run python -m undine set-pdk PROJECT sky130A@8afc8346a57f
```

See [Fetching a project's PDK](guide/pdks.md#fetching-a-projects-pdk).

`set-pdk` records the move in `pdk_history`, and the new version in `undine.lock` (which it creates when the project
has none). See
[Moving a project to another PDK version](guide/pdks.md#moving-a-project-to-another-pdk-version).

### `pdk fetch`: `no PDK root scanned is a ciel root`

**Cause.** ciel installs into a PDK root, and Undine fetches only into one it scans: the first of `--pdk-root` (or
`$PDK_ROOT`, `~/.ciel`, `~/.volare`) that has a `ciel/` directory, does not exist yet or is empty. None of them
qualifies, for example when only `~/.volare` is scanned, or `PDK_ROOT` names a directory with other files in it.

**Fix.** Give a directory for ciel: `--pdk-root DIR` before the command (and the same for the server), or set
`PDK_ROOT` to it. Once a root qualifies, `pixi run python -m undine pdk fetch PROJECT` prints it before it asks.

### `PDK …: libs.tech differs from the pin (…)` (*modified*)

**Cause.** The project's `undine.lock` pins the PDK by its files ([format 2](guide/pdks.md#pinning-the-pdk-by-its-files)),
and a file of the installed PDK is not as it was pinned: the PDK was edited after it was installed, or reinstalled from
another source. `lib status` lists it as an issue, and the PDK badge says *modified*.

**Fix.** Install it again as released, then check:

```sh
pixi run ciel rm --pdk-family sky130 <commit>            # the command the message names
pixi run python -m undine pdk fetch PROJECT
pixi run python -m undine pdk verify PROJECT
```

If the change is intended (the new files are the ones to use), pin the PDK as it is now instead:
`pixi run python -m undine pdk verify PROJECT --pin`.

### `PDK …: unchecked: run python -m undine pdk verify PROJECT`

**Cause.** A note, not an issue: files of the PDK changed since Undine last hashed it (a fetch just now, an update), or
it was never hashed on this machine. `lib status` and the badge never read a PDK themselves.

**Fix.** `pixi run python -m undine pdk verify PROJECT` hashes it (seconds); then the state is *unverified* or
*modified*.

### The PDK badge says *unverified (trust on first use)*

Not an error. The PDK matches the hashes pinned in `undine.lock`, but no PDK profile publishes the hashes of that
version yet, so it is checked against what the machine that pinned it had, not against the release. See
[PDK states](guide/pdks.md#pdk-states).

### The PDK badge says *no longer supported*

**Cause.** The project uses sky130 from open_pdks before 2025 (for example `bdc9412`), whose PCell code Undine no longer
runs. The project still opens, simulates and runs DRC / LVS, but its PCells are unavailable.

**Fix.** Click the badge and choose **Move to …** (when no supported version is installed, the dialog names it: run
`./setup.sh pdk` and **Rescan PDKs** in the badge's menu first), or run:

```sh
pixi run python -m undine set-pdk PROJECT sky130A@8afc8346a57f
```

Layout cells generated from the old PCells stay until you regenerate them (schematic-driven layout: **Regenerate all
devices**). Details: [Versions no longer supported](guide/pdks.md#versions-no-longer-supported).

!!! note
    The `!` note of `pixi run doctor` names the first supported PDK it found, which can be of another family. Move a
    project to a version of its own family (`sky130A@…` for a `sky130A` project).

### `project has no PDK`

**Cause.** The project was created without a PDK (`undine new PROJECT` with no `--pdk`). Simulation, DRC and LVS need
one.

**Fix.** Pin it to one:

```sh
pixi run python -m undine set-pdk PROJECT sky130A
```

A bare name picks an installed version whose PCells Undine runs, the tested one first.

## Simulation

### `Verilog-A models are not compiled` (IHP SG13G2)

**Cause.** IHP's transistor models are Verilog-A. ngspice loads them as compiled OSDI files, which are made once after
the PDK is installed. Until then, a simulation stops with
`ihp-sg13g2's Verilog-A models are not compiled (…): run ./setup.sh osdi (OpenVAF), then simulate again`.

**Fix.**

```sh
./setup.sh osdi
```

This downloads OpenVAF and writes the compiled models to Undine's store,
`~/.undine/store/build/<commit>/<variant>/osdi/`. `FORCE=1 pixi run osdi` compiles them again. See
[IHP SG13G2 Verilog-A models](guide/pdks.md#ihp-sg13g2-verilog-a-models).

### `… from the old cache: run pixi run osdi to move them into the store`

**Cause.** A note of `pixi run doctor` on the compiled-models line. An earlier Undine compiled the IHP models into
its cache, `~/.cache/undine/osdi/<PDK id>/`; they now belong in the store. Simulations still find them there.

**Fix.** Move them (nothing is compiled again):

```sh
pixi run osdi
```

It prints `moved psp103.osdi from … into …` for each model and removes the old directory once it is empty.

### `ngspice with OSDI` is missing

**Cause.** conda-forge's ngspice is built without OSDI, which the IHP models need. `setup.sh` builds its own ngspice 47
with OSDI into the pixi environment; that step did not run, or failed.

**Fix.**

```sh
./setup.sh ngspice        # same as: pixi run ngspice
```

It takes a few minutes, once.

### Simulations slow the whole machine down

**Cause.** By default the server runs one simulation per core, minus two (at least one), each with one ngspice
thread.

**Fix.** Lower `UNDINE_SIM_JOBS` (simulations at once; the others wait in a queue) in the server's environment. Keep
`UNDINE_SIM_THREADS` at `1` on a shared machine. A run that goes on too long stops at `UNDINE_SIM_TIMEOUT` (default
3600 s). See [Configuration](reference/cli.md#configuration-environment).

### `two cells named inv are netlisted, a/inv and b/inv … and they differ`

The whole message: `tb: two cells named inv are netlisted, a/inv and b/inv (instance x2 in tb), and they differ: a
netlist has one subcircuit per name, so one would stand for the other. Rename one of them, or place the same cell in
both places`. It comes from the netlist view, a simulation, LVS, a spec or a sweep, and from every netlist backend.

**Cause.** A SPICE netlist names a subcircuit by its cell name alone. The cell netlisted places two cells of one name
from different libraries (for example your own `inv` and a dependency's `inv`), or a cell from another library with
the name of the top cell, and they netlist to other lines. Before, the first one stood for both without a word. Two
cells of one name that netlist to the same lines are accepted and written once.

**Fix.** Rename one of the two cells in a library of your own, or place the same library's cell in both places. A
cell of a dependency or of the PDK cannot be renamed here: rename yours. Some PDK demo pages place two cells of one
name (sky130's `sky130_tests/top`, IHP's `start_page`) and are refused for the same reason; they are examples, not
cells to netlist.

## Tool versions and `undine.lock`

A project records the PDK version and the versions of KLayout, Xschem, ngspice, Magic and Netgen in `undine.lock`.
`pixi run python -m undine lib status PROJECT`, and the **lock issues** badge at the top of the window, list what differs
on this machine ([undine.lock](guide/libraries.md#undinelock)). A tool of another version or origin is a **warning**
(`~` in `lib status`, a lighter line in the lock dialog): it never counts as a lock issue, never makes `lib status`
exit with code 1, and blocks nothing.

### `ngspice: locked 41, here 47 from pixi (…)`

**Cause.** The project was made before Undine moved to ngspice 47. It is not an error: the warning tells you that
results may differ from those the project was checked with.

**Fix.** Check the project's results with the new version, then record it:

```sh
pixi run python -m undine lib lock PROJECT
```

### `klayout: locked 0.28.17, here 0.30.12 from PATH (/usr/bin/klayout)`

**Cause.** Often not an upgrade, but another copy of the tool found first. The command line and the server may start
from different environments, with or without `$UNDINE_TOOLS`. `from` says where the executable came from (`pixi`,
`UNDINE_TOOLS`, `undine-tools`, `env`, `PATH`), and the path in parentheses is the executable found here. A format 2
lock records the origin too, so the same number from another place also warns.

**Fix.** If the path is not the tool you meant, start the server and the commands the same way (both with `pixi run`).
If the upgrade is intended, record it with `lib lock`, as above.

### `no undine.lock: run python -m undine lib lock PROJECT`

**Cause.** The project has no lock file (it was made before Undine wrote one for new projects).

**Fix.** Run the command the message gives.

### A library dependency is `unlocked`, `missing`, at an `other version` or `modified`

**Cause.** A [library dependency](guide/libraries.md) on this machine is not as `undine.lock` records it: not installed
yet after a clone, at another commit after a `git pull`, or edited by hand.

**Fix.** Install the dependencies exactly as locked:

```sh
pixi run python -m undine lib sync PROJECT
```

### A library dependency is `leftover` (*not in project.json, still here*)

The message: `library X: not in project.json, still installed in .undine/deps/X/ and/or locked in undine.lock`.

**Cause.** `project.json` no longer names the dependency (a `git pull`, a revert, an adopted changeset or a hand edit
removed it), but its files or its lock entry are still here. While `project.json` names any other dependency, its
symbols still resolve on this machine, and not in a fresh checkout.

**Fix.** `lib sync` removes its files and its lock entry. In the browser, click the lock issues badge and choose
**Install the dependencies exactly as locked, remove the ones not in project.json (sync)**, which the dialog offers
first. From the command line:

```sh
pixi run python -m undine lib sync PROJECT
```

If the project still needs the library, add it back to `project.json` (`lib add`) instead.

<a id="undinelock-has-format-2-made-by-a-newer-undine"></a>
### `undine.lock has format 3, made by a newer Undine`

**Cause.** Someone working on the project with a newer Undine wrote a lock in a format this Undine cannot read (this
one reads formats 1 and 2; an Undine from before the store reads format 1 only, and says `format 2` here). Every
`lib` and `pdk` command, `set-pdk`, the lock dialog and the dependency calls of the HTTP API refuse it, so that this Undine never
rewrites the lock and drops what the newer one records. Nothing of the project was changed. The project still opens.

**Fix.** Update Undine, then work on the project:

```sh
git pull
./setup.sh
```

### `undine.lock is not valid JSON`, `is not a lock`, or `has format …, which no Undine writes`

**Cause.** The lock was edited by hand or by another tool, or a git merge left conflict markers in it. A lock without
`format` is read as format 1 and is not an error.

**Fix.** Fix the file, or restore it from git, then run `lib status`:

```sh
git checkout undine.lock
pixi run python -m undine lib status PROJECT
```

### Adding a local repository from the browser: `this server takes no local repository from the browser`

Also: `… is outside the directories this server fetches local repositories from`.

**Cause.** A [library dependency](guide/libraries.md) from a local repository (`file://`, `/path`, `~/path`) was added,
updated or synced through the browser or the HTTP API, and the server was started without `--dep-root`, or the
repository is not under one of those directories. A browser may not make the server read any repository on its disk.

**Fix.** Run the `lib` command the message names from the command line, which takes any local path. Or restart the
server with a directory above the repository:

```sh
pixi run python -m undine serve --dep-root ~/ip
```

## Dependencies and the store

### `lib sync`: `… project.json asks for rev …, undine.lock locks …` (or `not in undine.lock`)

The whole message ends with `run python -m undine lib lock PROJECT to resolve it, or python -m undine lib update
PROJECT NAME; nothing was changed`.

**Cause.** `lib sync` installs exactly what `undine.lock` pins and resolves nothing. `project.json` names a dependency
the lock does not pin that way: a `git pull` brought a `project.json` with another `rev`, `subdir` or URL (and the
lock was not committed with it), a dependency was added by hand, or the project has dependencies and no lock.

**Fix.** Lock what `project.json` names, then sync:

```sh
pixi run python -m undine lib lock PROJECT
pixi run python -m undine lib sync PROJECT
```

`lib lock` fetches and locks only those dependencies. Review the change of `undine.lock` and commit it with
`project.json`. In the browser, the lock dialog offers **lock** first in this case. See
[Sync installs the lock as it is](guide/libraries.md#sync-installs-the-lock-as-it-is).

### `offline, and not in the store: …`

The whole message names each dependency with its commit and hash, and ends with `run the sync with the network
(without --offline or UNDINE_OFFLINE=1) to fetch it; nothing was changed`.

**Cause.** The sync ran offline (`lib sync --offline`, `UNDINE_OFFLINE=1` in the server's environment, or
`{"offline": true}` over HTTP), and a locked version is neither in the store nor in the store's copy of its
repository: it was never fetched on this machine (or the store was emptied, or moved with `UNDINE_STORE`).

**Fix.** Sync once with the network; from then on the version is in the store:

```sh
pixi run python -m undine lib sync PROJECT
```

### `could not fetch … @ …: …`

**Cause.** A locked commit is not in the store and git could not fetch it: the remote is unreachable, it asks for
credentials (Undine never prompts), or the commit is gone from it (a force-push). The message names each dependency
with its commit, and git's own last line. Nothing was installed or removed.

**Fix.** Check the URL and your access (`git ls-remote URL`). If the commit is gone, move the dependency to one that
exists: `pixi run python -m undine lib update PROJECT NAME --rev REV`.

A git that was stopped while it fetched (a timeout, the server or the CLI killed, a power loss) can leave a lock file
such as `shallow.lock` in the store's copy of the repository. Undine removes these before each fetch into that copy
(no other fetch of it can be running then), so the next fetch goes through; when git still reports `Unable to create
'….lock'`, the message names the file.

### `.undine/deps is a link or not a directory` (or `.undine is ...`)

**Cause.** In this project, `.undine` or `.undine/deps` is a symbolic link (or a file), for example checked out from a
repository that committed one. Undine installs dependencies into `.undine/deps/` and removes the ones project.json no
longer names: through a link it would replace or remove directories wherever the link points, so every dependency
command refuses, before it reads, writes or removes anything; `lib status` reports it as an issue.

**Fix.** Remove the link (not what it points to), then sync; Undine makes the directories again:

```sh
rm PROJECT/.undine/deps            # or: rm PROJECT/.undine
pixi run python -m undine lib sync PROJECT
```

If the link is in the project's repository, remove it there too (`git rm --cached .undine/deps`): `.undine/` is
Undine's own and is never versioned.

### Dependencies take disk space in every project

**Cause.** Dependencies are installed by hard links into the store, which cost no disk, but only within one file
system. Where the store (`~/.undine/store`) and the projects are on different file systems (projects on another disk,
a mounted share, a Windows drive under WSL), or the file system refuses a link, each file is copied (read-only), with
no message. `UNDINE_STORE_LINK=copy` copies too.

**Fix.** Put the store on the projects' file system, in the environment of the server and of your shell:

```sh
export UNDINE_STORE=/data/undine-store       # a directory on the projects' file system
pixi run python -m undine lib sync PROJECT   # each project once, offline if you like, after the first fetch
```

See [The store](reference/data.md#the-store).

### `store verify`: `object …: its content does not match its name`

**Cause.** A file in the store was changed: an installed dependency file was made writable and edited (it is the same
file as the store's), or the disk corrupted it. `store verify` removed it (`--dry-run`: it only says so); `lib
status` reports the dependencies that used it as `modified`. An installed file that was only made writable (`chmod
u+w`, its content unchanged) is no damage: `store verify` and the next install make it read-only again.

**Fix.** In each project that uses the dependency:

```sh
pixi run python -m undine lib sync PROJECT
```

The sync takes the file again from the store's copy of the repository, or fetches it. Never edit a dependency's files:
change the library in its own repository and `lib update`.

### `store gc`: `nothing was removed: make these locks readable (or remove them), then run store gc again`

**Cause.** `store gc` keeps what every project on this machine locks, and one of their `undine.lock` files cannot be
read (conflict markers after a merge, a newer format, invalid JSON). Without it gc cannot tell what that project
needs, so it removes nothing. The `!` lines above the message name each lock.

**Fix.** Fix each lock named, or restore it (`git checkout undine.lock`), or remove a project you no longer need; then
run `pixi run python -m undine store gc` again (`--dry-run` first to see what it would remove).

## Packages and trust

`lib add`, `lib update`, `lib lock` and `set-pdk` check every [package](guide/libraries.md#packages) first and
refuse once with every problem, ending `nothing was changed` (`N problems with the dependencies:` and a `- ` line each
when there are several). Over HTTP the problems are listed in the dialog over the form (code `resolution_refused`).

### `…: two versions are required, … and …`

The whole message: `sar_common: two versions are required, demo -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) ->
sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11) and demo -> sar_common 1.2.0 (v1.2.0 @ 0c4e2b7a1f30): align the revs (one
version per package)`.

**Cause.** Two requirements give one package different trees: here the project names `sar_common` at `v1.2.0`, and
`analog_ip` needs it at `v1.3.1`. A project has one version of each package, since a netlist and a layout name cells
by their names alone.

**Fix.** Make the requirements name the same version: move your own (`lib update demo sar_common --rev v1.3.1`), or
move the package that needs the other one to a version that agrees. Each chain shows the `rev` and commit it asks
for.

### `… requires … ^1.3, but … is 1.2.0`

**Cause.** A manifest's `version` requirement does not match the [package] version of the tree its `rev` names
(`version` checks the label; `rev` decides what is installed).

**Fix.** In the requiring package, point `rev` at a version that matches, or change `version`. When the requirement
is your own (`project.json` has no `version`), it comes from a package you depend on: update that package.

### `comparator: analog_ip 1.2.0 and sar_common 1.3.1`

**Cause.** Two packages, or a package and one of your libraries or the PDK's, have a cell of one name. A netlist has
one subcircuit per name and a layout one cell per name, so they cannot both be in the project
([Cell names are disjoint](guide/libraries.md#cell-names-are-disjoint)). Names are compared without case. A layout
copy that placing a package's cell made in your library does not count.

**Fix.** Rename your cell when the clash is with your library. Between two packages, use a version of one that
renames its cell, or ask its author to. `lib status` reports such a clash with a project cell as an issue (`...: a
netlist has one subcircuit per name: rename one of them`).

A **plain** git dependency (no manifest) is not refused: `lib status` only notes a project cell named like one of its
cells (`inv: ip (git 229914698b1f) and project library mylib: ...`), and a testbench that places both is refused when
it is netlisted ([below](#two-cells-named-inv-are-netlisted-ainv-and-binv-and-they-differ)).

### `…: one local name for two packages, … and …`

**Cause.** Local names are project-wide: one directory `.undine/deps/<name>/` serves every schematic. Two libraries
(or the project and a library) give one name, compared without case, to different packages: for example your
`bias` is `bias_gen` and a package's `bias` is another package.

**Fix.** Give one of them another local name: your own with `lib remove` and `lib add` under another name, a
package's in its manifest's `[dependencies]` (a new version of it). A package is also installed under its own
[package] name, so that name is taken too ([Local names and aliases](guide/libraries.md#local-names-and-aliases)).

### `… has that name, and .undine/deps would shadow it` (a reserved name)

The whole message: `sky130_fd_pr: the PDK library sky130_fd_pr has that name, and .undine/deps would shadow it:
choose another local name; nothing was changed`; for a name a manifest gives: `...: ... installs it under this local
name, which is ...: a dependency may not shadow it; rename it in the requirer's [dependencies]`.

**Cause.** `.undine/deps/` comes first on the symbol path, so a dependency named like a project library, a PDK
library, `devices`, or a folder of Xschem's library or of a `symbol_paths` entry would replace that library in every
schematic.

**Fix.** Choose another local name (`lib add PROJECT NAME ...`). For a name a package's manifest gives, that package
has to change it. A name `project.json` has had since before keeps working; `lib status` notes the library it shadows.

### `… does not fit this project's PDK: …`

The whole message names the package, why, and the chains that require it: `sar_common 1.3.1 does not fit this
project's PDK: sky130A@0c4e2b7a1f30 is not among commits 8afc8346a57f (required by demo -> analog_ip 1.2.0 (...) ->
sar_common 1.3.1 (...))`.

**Cause.** The package's `[pdk]` does not accept the PDK `undine.lock` pins: another family or variant, a commit it
does not list, a project with no PDK, or a PDK with no version whose commit cannot be checked
([PDK compatibility](guide/libraries.md#pdk-compatibility)). `tag ... is not known to this Undine`: the package names
only compatibility tags, which this Undine cannot resolve yet.

**Fix.** Use a version of the package that lists your PDK's commit, or move the project to a PDK it accepts
(`set-pdk`). For a PDK with no version, install it with ciel (`pdk fetch`). A package that names only tags needs its
author to list the commits under `commits`.

### `the PDK … is not accepted by N locked packages`

**Cause.** `set-pdk` (or **Move to** in the PDK badge's dialog) to a PDK that a locked package does not accept. The
lines after it name each package with its chain and reason; nothing was changed.

**Fix.** First move those packages to versions that accept the new PDK (`lib update`), or remove them; then move the
PDK. See [Moving a project to another PDK version](guide/pdks.md#moving-a-project-to-another-pdk-version).

A pin made by `pdk verify --pin` or `lib lock --format 2` is not refused, since it records what is installed: both
print a `!` line for each such package, and `lib status` then shows `! ...: does not fit the PDK ...` for each such package until you move it or the PDK.

### `…: not in the store or installed: run lib sync first`

**Cause.** A command that checks the locked packages (`lib add`, `lib update`, `lib lock`, `set-pdk`) reads each one's
manifest from the store or from the project's install, never from the network. In a fresh clone neither has it yet.
Over HTTP: code `not_synced`; for `set-pdk` the package is a line of its refusal instead.

**Fix.** Install what is locked, then run the command again:

```sh
pixi run python -m undine lib sync PROJECT
```

### `… needs lock format 2: run python -m undine lib lock PROJECT --format 2 first`

The whole message: `analog_ip: package analog_ip names dependencies (sar_common, bias): needs lock format 2: ...;
nothing was changed`, or `...: package bias_gen is also installed under its own name bias_gen: ...`.

**Cause.** A package with dependencies, or one installed under a local name other than its own, needs the `packages`,
`names` and `deps` keys of lock format 2. The project's lock is format 1 (every project `new` makes starts so).

**Fix.** Convert the lock once, then run the command again:

```sh
pixi run python -m undine lib lock PROJECT --format 2
```

It pins the PDK as installed here (fetch it first if it is missing) and needs everyone on the project to use this
Undine version. When it is the lock itself that has such a package (after a pull), `lib lock --format 2` converts
and resolves in one step. There is no button for it: the command line only.

### An index requirement, or a version range

The messages: `...: undine-package.toml: dependencies.sar_common: an index requirement (sar_common ^1.2), which comes
with the package index (phase 3): name its git repository, sar_common = { git = "https://...", rev = "v1.2.0",
version = "^1.2" }`, or `...: undine-package.toml: dependencies.sar_common.version: '>=1.2': version ranges come
with the package index (phase 3): use ^, ~ or =`.

**Cause.** The manifest uses a form of the package index, which Undine does not have yet: a dependency given as a
version string or with `index`, or a version requirement with a comparator (`>=`, `<`, `!=`) or a comma.

**Fix.** Give the dependency as a git table, `{ git = "...", rev = "...", version = "^1.2" }` (the message says what
to write), with `^`, `~`, `=` or a bare `MAJOR.MINOR` as the requirement. For a package someone else publishes, ask
its author, or use a version that does.

### `undine-package.toml` is refused

The message has the form `<where>: undine-package.toml: <key>: <what>: <fix>`, for example `... @ 229914698b1f:lib:
undine-package.toml: package.version: '1.2' is not semver: MAJOR.MINOR.PATCH, e.g. 1.2.0`, followed by the chain that
required it.

**Cause.** The package's manifest is not as Undine reads it: not valid TOML or UTF-8, larger than 256 KiB, a symbolic
link, a key or table Undine does not know (`unknown key (made for a newer Undine?)`, which also catches a typo such as
`revision =`), a value of the wrong kind, a name that is not `[a-z][a-z0-9_]*`, a commit given by a prefix, a `subdir`
or include pattern that leaves the package, a `[files] include` that selects no top-level cell, or an exported cell
that is not among its files.

**Fix.** For a package of your own, fix the line the message names, commit, and point `rev` at that commit. For
someone else's, use a version whose manifest Undine reads, or ask its author. [The manifest](guide/libraries.md#the-manifest)
lists every key.

Related, from the same check: `... has no undine-package.toml: a package may depend only on packages` (a manifest
names a plain git repository: only `project.json` may), `... asks for package X, but ... is Y` (the `package` given,
or `--package`, is not what the tree holds), and `... is a path on this machine, and ... comes from ...` (a package
from a remote repository names a local one: ask its publisher for a git URL).

### `…: … is a dependency of this project already, as …`

The whole message: `bias2: bias_gen 0.3.0 is a dependency of this project already, as bias: one package has one name
in project.json; use bias; nothing was changed`.

**Cause.** `lib add` of a package that `project.json` already names under another local name. A package has one
entry in `undine.lock`, so it has one name in `project.json`; its own name and the names packages give it are
installed beside it anyway.

**Fix.** Place its cells through the name you have (`bias/<cell>.sym`), or `lib remove` that name and add it under the
new one.

### `library IP exists (as ip)`, or `IP and ip: one local name spelled two ways`

**Cause.** A local name that differs from an existing library (your own, a dependency, or a name a package is
installed under) only in case. On a case-insensitive file system (macOS, Windows) both are one directory, so removing
one would remove the other's install.

**Fix.** Use the name as it is spelled already, or choose another one. For a manifest that spells a package's name
two ways, that package has to change it.

### `note: …: locked before packages had dependencies; … resolves it`

The whole note: `ip: package analog_ip, which undine.lock does not install under its own name: locked before packages
had dependencies; ...`, or `...: its undine-package.toml names dependencies (...) that undine.lock does not lock: ...`
on a format 1 lock.

**Cause.** A dependency locked by an older Undine (no `package` in its entry) whose manifest asks for more than that
entry installs. It keeps working as it was locked: other `lib` commands neither change nor check it. Its schematics
that place the package's own name (`analog_ip/<cell>.sym`), or the dependencies it names, do not resolve until it is
resolved. On a format 2 lock, dependencies that are not locked are an issue rather than a note.

**Fix.** Resolve it when you are ready: `lib update PROJECT NAME`, or `lib lock PROJECT` (a format 1 lock:
`lib lock PROJECT --format 2`).

### `…: …: pin name '…' holds a space or a line break, which a netlist line cannot carry`

**Cause.** A symbol's pin name (or a schematic's port label) holds a space or a line break. It becomes a word of the
`.subckt` line, so a line break would write cards of its own into the netlist; the netlist is refused.

**Fix.** Rename the pin in the symbol (and the label in its schematic). For a dependency's symbol, ask its author.

### `…: placed by …; update or remove that instead`

**Cause.** `lib update` or `lib remove` of a name that only packages place: it is not a dependency of the project, but
of the package named.

**Fix.** Update or remove that package. `lib remove` removes the packages nothing needs any more along with it; one
another dependency still places stays, and is named (`sar_common: still placed by analog_ip`).

### `… (dependency …, not trusted): …` (a simulation is refused)

The whole message names every finding, then what to do: `ip/opamp (dependency analog_ip 1.2.0, not trusted): code
block s1 line 1: .control; a person can trust analog_ip@1.2.0 in this project (undine lib trust demo ip, or Trust...
in Undine); an agent cannot`. For a dependency without a manifest the label is `git <commit>`.

**Cause.** The simulation (or sweep, or spec check) uses a cell from a [dependency](guide/libraries.md#trusting-a-dependencys-code)
that runs commands (`.control` blocks, `*#` lines) or reads a file outside the PDK (`.include` / `.lib` / `file=`
with an absolute path, `..`, `$` or `~`), and no person has trusted that dependency in this project. This holds for
**plain git dependencies too**: a project whose dependency cell has a `.control` block or an absolute `.include`,
which simulated before, now needs the trust.

**Fix.** Read what was found, and if you accept that it runs, trust that version: **Trust...** in the refusal (or the
shield button on the library's row), or

```sh
pixi run python -m undine lib trust PROJECT NAME
```

An update to another version is refused again until it is trusted. Or use a version of the dependency without such
content, or keep the cell out of the testbench. An agent cannot trust: it reports the refusal and asks you. A
dependency that `lib trust` says `has no install record` was not installed by Undine: `lib sync` removes or
reinstalls it.

The trust dialog's **Trust** may say `what NAME holds changed since it was confirmed ...: confirm the trust again`: a
sync or an update replaced the files meanwhile; open the dialog again.

### RTL: `… (dependency …, not trusted): line …: $c(…)`

**Cause.** **Simulate** (Verilator) on RTL from a dependency that holds C++ or shell code (`$c(...)`,
`` `systemc_* ``, `$system(...)`, DPI imports, a `` `define `` that builds them) or reads or writes a file by an
absolute path or with `..` (`` `include ``, `$fopen`, `$dumpfile`, `$readmem*`, `$writemem*`). **Lint** refuses only such
`` `include `` paths. Every RTL file of the dependency is read.

**Fix.** Trust the dependency as above, if you accept that the code runs; or use a version without it. See
[RTL from a dependency](guide/rtl.md#rtl-from-a-dependency).

### The Xschem netlister: `… (dependency …, not trusted): … tcleval(…)`

**Cause.** The netlist view with the **Xschem** backend runs Xschem's own netlister, which evaluates `tcleval(...)` in
an attribute as Tcl in any symbol it reads. So it is refused while any installed dependency the project does not
trust has one in a `.sch` or `.sym`, whether or not this schematic places it.

**Fix.** Trust the dependency (if you accept the Tcl), or use Undine's own netlister, which never runs Tcl and only
warns (`not trusted: would be refused in a simulation: ...`).

### LVS: `… (dependency …, not trusted): …` when the run starts

**Cause.** The LVS reference netlist (KLayout, or Magic + Netgen) has a line from a dependency's cell with an
`.include` or `.lib` path outside the PDK, and no person has trusted that dependency. LVS runs no control code, so
only paths are checked, and only when a run starts: a sign-off status shown, or a result reused, is not refused.

**Fix.** Trust the dependency (as above) and start the run again; trust given after the run was queued counts.

## Server, ports and restarts

### `[Errno 98] … address already in use`

**Cause.** Another process listens on the port (default 8700), often an Undine that is already running. An AI app that
starts Undine through `undine mcp-stdio` leaves it running after the chat ends. The server exits with code 3.

**Fix.** Check whether it is Undine:

```sh
curl -s http://127.0.0.1:8700/api/health
```

An Undine answers with `"undine": true`, its projects root and whether MCP is on. Use that server, stop it, or start
yours on another port:

```sh
pixi run python -m undine serve --port 8710
```

To stop it, find its process and end it. The server's command line contains `serve`; a line with `mcp-stdio` is the AI
app's launcher, which ends with the chat:

```sh
pgrep -af undine          # Undine processes, with their command lines
kill PID                  # PID of the server's line
```

`./scripts/restart.sh` does not stop a server an AI app started (see the next entry).

### `./scripts/restart.sh` stopped another Undine, or did not stop one

**Cause.** Before it starts a new server, `restart.sh` stops every process on the machine whose command line contains
`python -m undine serve`, whatever its port or projects root. That matches a server started by `pixi run serve` or by
`restart.sh` itself. It does not match a server started with a global option before `serve`, such as
`python -m undine --root … serve` or `--pdk-root … serve`, and that includes every server an AI app starts. Such a
server keeps the port, and the new one exits with `[Errno 98] … address already in use`. `restart.sh` still prints
`server: 200`, because the old server answers its check.

**Fix.** When you run more than one Undine on one machine, start and stop them yourself with
`pixi run python -m undine serve --port …` instead of `restart.sh`. To replace a server an AI app started, stop it as
[`[Errno 98]`](#errno-98-address-already-in-use) shows, then run `restart.sh` (with `UNDINE_MCP=1` if the app needs
MCP), and read the log (`/tmp/undine.log`) rather than trusting `server: 200`.

### I cannot open Undine from another computer

**Cause.** The server listens on `127.0.0.1` by default, so only the machine it runs on reaches it.

**Fix.** Forward the port over SSH and open `http://127.0.0.1:8700` on your computer:

```sh
ssh -L 8700:127.0.0.1:8700 me@eda-server
```

!!! warning
    The UI and the HTTP API have no sign-in; only the MCP endpoint (`/mcp`) checks a token. Anyone who can reach the
    port can read and change every project on the server. Do not start it with `--host 0.0.0.0` on a network you do not
    trust. For AI apps, the launcher's `--ssh HOST` forwards the port for you
    ([Undine on another machine](guide/assistant.md#undine-on-another-machine)).

### `another server still holds the simulations of …`

**Cause.** A second server was started on the same projects root while the first one still runs (or is still
stopping). One server at a time works on a root's simulations.

**Fix.** Nothing, if the first one is about to stop: the new one takes the simulations up once it has gone. Otherwise
stop one of the two.

## AI agents and MCP

[Troubleshooting](getting-started/connect-ai.md#troubleshooting) in Connect an AI agent has the full table. The most
common cases:

### `Undine at … runs without MCP`

**Cause.** The AI app's launcher found a running Undine, but it was started without MCP. The launcher does not restart
it.

**Fix.** Stop it and start it again with MCP on:

```sh
UNDINE_MCP=1 pixi run serve          # or: UNDINE_MCP=1 ./scripts/restart.sh
```

### Claude Code does not show Undine's tools, or only `open_in_undine`

**Cause.** The session was started before you registered Undine, Undine was registered for one folder only (without
`--scope user`), or the launcher could not reach the server.

**Fix.** Start a new session. Check `claude mcp list` for `✔ Connected`. If the tools are there in one folder only,
register again with `--scope user` ([Claude Code](getting-started/connect-ai.md#claude-code)). If the server does not
start, read `~/.undine/serve.log`.

### `… is editing this changeset. It is read only until it finishes.`

**Cause.** An agent is still writing into this changeset. While it writes, the changeset is its alone.

**Fix.** Wait for it, or click **Stop** in the review. A chat that was closed releases the changeset at once; a crashed
one after 2 minutes.

### `the server policy does not allow … connections`

**Cause.** The server was started with `--llm-policy` (or `UNDINE_LLM_POLICY`), which limits the LLM connections that
may be used. With `--llm-policy none`, MCP is off too.

**Fix.** Ask whoever runs the server, or start it with the policy you need
([Agent (in-app)](guide/assistant.md#agent-in-app)).

## Changesets and adopting

A [changeset](guide/changesets.md) is a proposal on a git branch of your project. **Adopt** merges it into your design,
all or nothing. When it does not adopt, nothing is written and your design stays as it was.

### `Wait for running checks before adopting or discarding.`

**Cause.** A job in the proposal or its baseline (a DRC, LVS or simulation run) is queued or running. Adopting while a
tool still writes its results could archive half of them.

**Fix.** Wait for the jobs to end, or cancel them. A simulation the last server left running blocks too, until it ends.

### `Not adopted: … conflict with edits made since the proposal was opened.`

**Cause.** An object (an instance, a wire, a shape, an intent item) was changed both in your design and in the
proposal, to different values. The review lists each file with its conflicts, and for each the value in the design
now, in the proposal and when the proposal was opened.

**Fix.** Make the two sides agree on those objects, then adopt again. Change them in the design (putting back the value
it had when the proposal was opened lets the proposal's value through), or in the proposal (by hand in the preview, or
with **Request changes**). Or discard the proposal; its branch keeps it.
See [Adopting: the merge](guide/changesets.md#adopting-the-merge).

### `Not adopted: … changed in the design while the proposal was being adopted.`

**Cause.** Another program (an editor, a `git pull`) wrote a file of the design during the adopt.

**Fix.** Adopt again. The merge is made anew from the design as it is now, that edit included.

### `The … file cannot be read, so it cannot be merged`

**Cause.** A file the merge needs, such as an intent or netlist YAML, is broken.

**Fix.** Fix the file (the review shows why it cannot be read), then adopt again.

### `waiting for “…” (adopting) to finish`

**Cause.** One changeset is closed at a time. Another one is being adopted, discarded or reverted, which can take a
while for gigabytes of run evidence.

**Fix.** Nothing: this one goes on when the other is done. The review shows how far the other one is.

### Adopted, but `the merge was left uncommitted`

**Cause.** Undine commits the merge only when nothing else in the project was uncommitted. The message says which case
it is:

| Message ends with | Fix |
|---|---|
| `the project had other uncommitted changes` | commit the merge with git when you are ready; the History panel shows what is uncommitted |
| `the repository had no commit yet` | click **Make the first commit** in the History panel; it takes the merge too, and later adopts are committed |
| `its commit failed` | commit it with git |

### Opening a changeset is refused: `… is in the git repository …, which is not set up for Undine`

**Cause.** The project is inside a git repository that nobody set up for Undine, such as a home directory kept in git.
Undine does not write branches or configuration into such a repository.

**Fix.** Set it up with **Set up git** in the History panel, or with the command the message gives, or move the project
out of that repository:

```sh
pixi run python -m undine git-setup PROJECT
```

### Opening a changeset is refused because of a library

**Cause.** A library of the project is a symbolic link that leads out of the project (or nowhere), an absolute link, a
link git does not keep, or a git repository of its own. A changeset's working copy (a git worktree) cannot hold it.

**Fix.** The **New changeset** dialog names the library and the path, and says what to do. Putting the files themselves
in the project, or making the library a [dependency](guide/libraries.md#dependencies), always works. See
[A changeset is a git branch](guide/changesets.md#a-changeset-is-a-git-branch).

### Undine stopped in the middle of an adopt

**Cause.** The server was stopped (a restart, a crash) while it wrote the merge.

**Fix.** Start it again. When it starts, before it serves the UI or any client, it finishes the merge or takes it
back, keeping what you did since, and prints `the interrupted merge of the changeset … was finished or taken back`. If it was taken back, the
review says so: adopt again. See [If Undine stops in the middle](guide/changesets.md#if-undine-stops-in-the-middle).

## Verification

### LVS: `pin VDD: connected to nothing inside the schematic and no VDD label in the layout's top cell`

**Cause.** A schematic port is connected to nothing inside the cell (an interface pin that is there for the symbol,
such as a CDAC's `vdd`). The LVS deck purges a net with no device on it, so the layout has no pin to match it with.

**Fix.** Put a label of that name in the layout's top cell, on a pin label layer (`met1.label` …) and on a shape of that
metal. Undine then accepts the port and lists it under `unused_pins` in the LVS summary. Do not put the label on metal
the layout already uses: `pin VDD: connected to nothing in the schematic but its label is on the layout net …` is an
error too, because it would make another net the cell's port. See [Verification](guide/verification.md).

### LVS: `pin VDPWR: no VDPWR label on the layout net it matched ($12)`

**Cause.** A pin of the schematic's top cell is not a pin of the layout's top cell on the net it matched. KLayout's
comparison alone would accept it, so a supply cut off from its labelled stripe could still "match". Undine reports it
as a mismatch.

**Fix.** Label the net in the layout's top cell with the pin's name, on the shape the net really uses.

### LVS: `tie cell … joins the ports … and …`

**Cause.** In the layout, the HI and LO outputs of `conb_1` are the supplies, so a tie cell on a port joins that port to
another one, and LVS cannot compare one net with two pins.

**Fix.** Give the constant port its own driver (a buffer from the tie cell's net), or take the port out. See
*Standard cells in LVS* in [Verification](guide/verification.md).

### `LibreLane is not available`

**Cause.** LibreLane is not installed where Undine looks for it: `librelane` on `PATH`, or a LibreLane checkout in
`~/librelane` with Nix.

**Fix.** Install it with Nix as its guide says, clone it to `~/librelane` and run its smoke test once, or set
`UNDINE_LIBRELANE` to the command that runs it. See [LibreLane: RTL to GDS](guide/rtl.md#librelane-rtl-to-gds).

### `Verilator is not installed`

**Cause.** The RTL view's lint and simulation need Verilator, which `pixi install` adds.

**Fix.** Run `pixi install`, or put `verilator` on `PATH` ([Installing Verilator](guide/rtl.md#installing-verilator)).

## Git

### The commit is refused: `library … has layout edits not saved to its cell files`

**Cause.** The pre-commit hook that `git-setup` installs refuses a commit whose staged operation log holds layout edits
not saved to the cell files.

**Fix.** Save the layout in Undine, `git add` the cell files and the log, and commit again. See
[Save layouts before committing](reference/data.md#save-layouts-before-committing).

### `project.json cannot be read (…): a git merge may have left conflict markers in it`

**Cause.** A git merge left conflict markers in the project's `project.json`, so it is no longer valid JSON. The project
list shows it with this error.

**Fix.** Resolve the conflict in the file (the History panel lists it), then reload.

### A git merge stopped on a cell file

**Cause.** Both branches changed the same object of a schematic, layout, intent or operation log differently.

**Fix.** Open a shell with Undine's environment, go to the project, list what is open, and take a side for each
conflict (the merge prints the IDs and the commands to go on with):

```sh
cd ~/undine-eda && pixi shell
cd ~/undine-projects/PROJECT
python -m undine merge-check
python -m undine resolve PATH --theirs ID
git add :/PATH
```

The History panel shows the same conflicts and the cells to re-check with DRC / LVS. See
[Git history](guide/history.md#merges-and-the-cells-to-re-check) and [semantic diff and merge](design/merge.md).

## WSL2

Undine runs on WSL2 like on Linux; it is tested on Ubuntu 22.04 under WSL2. A few things differ:

- **Keep projects in the Linux file system.** The default projects root, `~/undine-projects`, is. A Windows drive under
  WSL (`/mnt/c/...`) is not case-sensitive, so cells whose names differ only in case cannot be stored there
  ([Layout cell files](reference/data.md#layout-cell-files)).
- **Run Undine's commands in a WSL terminal**, not in PowerShell or Command Prompt. This includes `claude mcp add`.
- **The browser is the Windows one.** When an AI app starts work, the review opens in your Windows browser.
- **Files inside WSL from Windows.** In Windows Explorer, the Claude Desktop extension built by `pixi run mcpb` is at
  `\\wsl$\<distribution>\home\<you>\undine-eda\dist\`.
- **Claude Desktop without the extension** runs Undine's launcher through `wsl.exe`; the configuration is in
  [Connect an AI agent](getting-started/connect-ai.md#a3-claude-desktop-without-the-extension).

## Large projects

- **The review's diff is cut.** The *Diff* mode draws at most 4000 polygons of difference, and *By file* can take
  seconds for a large imported layout (one read at a time, so it waits rather than starting over).
- **Evidence grows with every closed changeset.** Adopting or discarding keeps the proposal's runs as evidence. The
  review of a closed changeset shows its size, and **Prune waveforms** removes the raw waveforms, keeping the runs'
  records and hashes. See [Evidence after apply or discard](guide/changesets.md#evidence-after-apply-or-discard).
- **A project inside a large repository** checks the whole repository out in each changeset's working copy (no sparse
  checkout yet). Keep a large design in a repository of its own.
- **Many simulations at once.** Tune `UNDINE_SIM_JOBS` and `UNDINE_SIM_THREADS`, as in
  [Simulations slow the whole machine down](#simulations-slow-the-whole-machine-down).
- **Long answers to an agent** are shortened (`undine call` prints at most `UNDINE_MAX_CHARS` characters, 25000 by
  default; see [Long results](guide/assistant.md#long-results)).

## FAQ

### What is Undine?

Undine is an AI-native, web-based EDA environment for custom IC design, analog and mixed-signal: schematic, symbol, layout, RTL,
simulation, DRC / LVS and tapeout, in the browser. The design database, the PDK, PCells, DRC / LVS and simulation run on
a server, behind an HTTP API. The browser UI is one client of that API; scripts and AI agents are others, and every
change is logged and reviewable. See the [Overview](overview.md).

### What is Undine not?

- **Not a hosted service.** You run the server yourself, on your machine or on one you control. It has no user
  accounts or sign-in.
- **Not a replacement for the tools it runs.** Undine drives KLayout, Xschem's formats, ngspice, Magic, Netgen,
  Verilator and LibreLane; their results are theirs. It reads and writes Xschem `.sch` / `.sym` files, so Xschem still
  opens them.
- **Not finished.** Undine is in alpha: interfaces, file formats and the HTTP API can still change between commits.

### Which operating systems does it run on?

The server runs on Linux x86-64 with glibc 2.28 or newer, or on Windows in WSL2. The pixi environment is built for
`linux-64` only, so the server does not run natively on macOS or Windows. The browser can be on any computer that can
reach the server (see [I cannot open Undine from another computer](#i-cannot-open-undine-from-another-computer)).

### Which browsers are supported?

A current desktop browser. The UI is built with Vite's default targets, which are browsers with native ES modules
(Chrome 87, Edge 88, Firefox 78, Safari 14 and newer). Undine's screenshots are captured with Chromium.

### Which PDKs does it support?

SkyWater SKY130, GlobalFoundries GF180MCU and IHP SG13G2, installed with [ciel](https://github.com/fossi-foundation/ciel),
each with its decks, models, corners and PCells. SG13CMOS5L, which ciel installs with SG13G2, is untested: DRC
is set up, LVS and simulation are set up but have not been run, and it has no PCells. A PDK of
another family is listed, but the features that need a [profile](guide/pdks.md#pdk-profiles) are unavailable. The
tested versions are in [Supported PDKs](guide/pdks.md#supported-pdks), the commands in
[Installing a PDK](guide/pdks.md#installing-a-pdk).

### Is my design sent anywhere?

Not by Undine itself. The server runs where you start it and has no telemetry; the browser only draws what the server
sends it. Data leaves your machine in these cases only:

- **An AI agent.** Over MCP, the AI app's provider receives what Undine's tools return. With the in-app Agent, the
  provider of the API key you add receives what the Agent sends; with a local LLM (Ollama), nothing leaves. Everything
  sent is recorded in `<project>/llm/audit.jsonl`, and `--llm-policy none` turns all LLM connections off
  ([Connect an AI agent](getting-started/connect-ai.md)).
- **Downloads you start.** `setup.sh` downloads packages, ngspice's source, the PDK and OpenVAF; `lib add` and
  `lib sync` fetch library dependencies from their git remotes.
- **Fonts.** The UI page loads its fonts from Google Fonts (`fonts.googleapis.com`). No design data is part of that
  request.

The PDK stays on the server too: the browser sees layer colours, symbols, PCell parameters and rendered results, never
models, rule decks or PCell code.

### Can an AI agent change my design without me?

No. An agent works in a changeset, a working copy on a git branch. Your design changes only when a person clicks
**Adopt** in the review, and an agent has no tool to adopt. You can stop an agent with **Stop** in the review, restore
the proposal to an earlier step, or discard it. See [Changesets](guide/changesets.md).

### Will a restart lose my work?

No. Running simulations go on, and the next server on the same projects root takes them up. Queued simulations keep
their order, and agents keep their sessions and changesets. A DRC, LVS, extraction, RTL or LibreLane run that the server
left unfinished is marked `interrupted`: run it again (**Run needed checks** in a review).
[Restarting the server](reference/cli.md#restarting-the-server) has the details.

### Can I use Undine from claude.ai or ChatGPT in the browser?

Not yet. Their connectors reach MCP servers from the provider's cloud, so Undine would have to be reachable from the
internet with a proper sign-in, which is not built. Use Claude Desktop, Claude Code or another local MCP client.

### Can I use my existing Xschem and GDS files?

Yes. Undine reads and writes Xschem's `.sch` / `.sym` format, and imports GDS and OASIS layouts:
**Import existing cell** in a library row of the UI, or the command line:

```sh
pixi run python -m undine import PROJECT LIB chip.gds
```

See [Schematic editor](guide/schematic.md) and [First project](getting-started/first-project.md).

### How is Undine licensed? Can I ship it together with KLayout?

Undine is licensed under the Apache License 2.0. The tools, Python packages and PDKs it works with keep their own
licences. The server imports the KLayout Python module, which is GPL-3.0-or-later, so Undine and that module form one
combined program. Running Undine, also as a service, does not trigger the GPL's obligations. Whoever *distributes* the
two together (a container image, a VM, an installer) distributes the combination under GPL-3.0 and must meet its
terms. See [Licenses](licenses.md) for the full table; it is a summary, not legal advice.

### Can I use Undine in Japanese?

Yes. Choose **日本語** in the language menu at the right of the top bar ([Interface language](guide/language.md)).
Every page of this documentation also has a Japanese version: use the language switch in the header. A page
added without one yet is shown in English.

### How do I report a bug?

Open an issue at <https://github.com/kadomoto-lab/undine-eda/issues>, after searching the open and closed issues. Include
what you did, what you expected and what happened, and:

```sh
pixi run doctor                          # what is installed (tool paths) and the PDKs found
pixi run python -m undine lib status PROJECT   # tool versions vs undine.lock
git rev-parse --short HEAD               # the Undine version
```

Add the project's PDK id (the `pdk` field of its `project.json`), your distribution (or WSL2), the browser for a UI
problem, and the logs from [Start here](#start-here-run-the-check). [CONTRIBUTING.md](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md)
says what makes a good report.

!!! warning
    Issues are public. Do not attach files from a PDK under a non-disclosure agreement, design data you may not share,
    or `~/.undine/env` (it can hold an API key).
