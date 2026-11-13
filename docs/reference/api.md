# HTTP API

Everything the UI does is an HTTP call to the Design API; scripts and agents use the same calls. A running server
describes them itself:

| URL | |
|---|---|
| `http://127.0.0.1:8700/docs` | interactive API documentation (Swagger UI) |
| `http://127.0.0.1:8700/redoc` | the same, as ReDoc |
| `http://127.0.0.1:8700/openapi.json` | the OpenAPI schema |

Conventions:

- Projects, libraries and cells are in the path: `/api/p/{project}/l/{lib}/...`. A changeset's working copy is the project
  `{project}~{changeset}` (see [Changesets](../guide/changesets.md)).
- `X-Undine-Actor: user | agent:<name>` attributes an edit; `reason` in the body records why
  (see the [operation log](data.md#operation-log)).
- Errors are `{"detail": "...", "code": "...", "params": {}}` with 400 (bad request), 403 (not allowed, e.g. a
  read-only library, or a dependency's code no person trusted), 404 (no such object), 409 (someone else is editing that changeset, a whole text sent back was
  loaded from a file that changed since: see below, or a changeset's merge conflicts: see
  [Changesets](#changesets)) or 502 (an LLM connection failed). `code` names the kind (`invalid_request`, `busy`,
  `stale`, `merge_conflict`, `not_set_up`, `unsupported_library`, `untrusted`, `resolution_refused`, ...); `detail` is English, and the UI words the
  kinds it knows in its own language. A kind with values of its own has them at the top level of the body and in
  `params` too.
- The views a client sends back whole, the RTL (`POST .../c/{cell}/rtl` with `op: save`) and the intent's YAML
  (`POST .../c/{cell}/intent/edit` with `op: replace`), take `"base"`: the `"sha"` their `GET` returned with the text
  edited from. When the file changed since (a git merge, checkout or pull), the save is a 409 instead of undoing that
  change; without `base` it is saved as before.
- `GET /api/projects` lists a project whose `project.json` cannot be read (a git merge left conflict markers in it)
  as `{"name", "error"}`; opening it is a 400 that says so.
- Agents can also use the [MCP endpoint](../guide/assistant.md#mcp-external-llm-clients).

## Projects, PDKs and library dependencies

| Call | |
|---|---|
| `GET /api/pdks` | the PDKs the server found: `id` (`name@` the first 12 characters of the version), `name`, `family`, the full `version`, and what Undine supports of it (`profile`) |
| `POST /api/pdks/rescan` | find the PDKs again under the server's PDK roots (one installed or fetched since the server started), with no restart; returns `{pdks}` as `GET /api/pdks` lists them. The open layouts and editors of a project whose PDK now resolves to another install (or to one at last) are reloaded on their next use |
| `GET /api/projects`, `POST /api/projects` `{name, pdk?}` | list the projects; create one pinned to a PDK (an id, or a bare name: a version whose PCells Undine runs, the tested one first) |
| `GET /api/p/{project}` | the project, its PDK and its libraries (own, dependencies, the PDK's). Each library has `package` (a dependency's package name, else null), `direct` (false for a package only other packages place) and `via` (the packages that place it); a dependency's cell outside its package's `[exports]` has `internal: true` in the cell list and is left out of the symbols offered for placement |
| `POST /api/p/{project}/libs` `{name}` | create a library of the project's own (`libs/<name>`, named in project.json) |
| `POST /api/p/{project}/pdk` `{pdk}` | pin the project to another PDK version (recorded in `pdk_history` and in undine.lock, which it creates when the project has none; a format 2 lock gets the new PDK's pin, its tree hashed first); returns `{from, to}`. Not in a changeset preview |
| `GET /api/p/{project}/deps` | the project as locked vs. as installed here (`lib status`), below. A dependency project.json no longer names but still installed or locked is listed with `state: "leftover"` and an issue (so `ok` is false) |
| `POST /api/p/{project}/deps` `{name, git, rev?, subdir?, package?}` | add a [library dependency](../guide/libraries.md) (`lib add`): fetched, installed read-only and locked, with the packages it depends on. Returns the lock entry with `name`, `cells`, `public_cells`, `internal_cells`, `created` (whether it made undine.lock), `package` and `version` (null for a plain dependency), `resolved` (each entry resolved: `{name, ...entry}`, for a package also `package`, `version`, `names`, `via`, `flagged`, `trusted`), `flagged` (`{spice, rtl, tcl}`: what the trust rule finds in it) and `trusted`. `package`: the [package] name its manifest must give. A local `git` (`file://`, `/path`, `~/path`) is refused unless the server was started with `--dep-root` and the repository is under one of those directories; the same holds for sync and update, and for every URL a manifest names. Refusals: [below](#dependency-refusals) |
| `POST /api/p/{project}/deps/sync` `{offline?}` | install every dependency exactly as locked (`lib sync`), and remove each leftover (its files and its undine.lock entry); returns `{libraries: [{name, action, commit}]}`, `action` one of `ok` (installed as locked already), `installed`, `removed` (`commit` may be null for `removed`). A package's row also has `package`, and a row whose package the locked PDK does not fit has `warning` (`CHAIN: does not fit the PDK ID: REASON`): the package is installed all the same, and `GET /deps` reports it as an issue. The body is optional: `{"offline": true}` never fetches, `false` may, and no body (or `null`) follows the server's `UNDINE_OFFLINE`. Frozen: a lock that does not pin a dependency as project.json names it is refused before anything is read (`lib lock` resolves it). All or nothing: when one dependency fails (a remote unreachable, files that do not match the lock, offline and not in the store), none is installed and none removed. A dependency in the [store](data.md#the-store) is installed from there and reads no repository; a local repository, also one project.json names, is checked against `--dep-root` before git reads it (a fetch, or a commit kept in the store's copy of the repository) |
| `POST /api/p/{project}/deps/lock` | `lib lock`: fetch, install and lock what undine.lock does not pin as project.json names it (under the `--dep-root` rule, as add), and record the tool versions in use now (in a format 1 lock the PDK id too); returns the status with `resolved: [{name, git, rev?, subdir?, commit, sha or tree}]`. It writes the lock in its own format: making it format 2 is the command line's (`lib lock --format 2`, `pdk verify --pin`) |
| `POST /api/p/{project}/deps/{name}/update` `{rev?}` | move a dependency to `rev` (default: its rev again); returns `{name, from, to, rev, changed, used_by, packages_changed}`: the changed files and the cells using them, and `packages_changed: [{package, names, from, to, changed}]` for each entry that moved, was brought in (`from` null) or is no longer needed (`to` null). The trust of what moved is taken out of project.json |
| `POST /api/p/{project}/deps/{name}/remove` | remove a dependency, with the packages nothing needs any more; returns `{ok, name, removed_packages: [{package, version, commit, names}], kept: [{name, placed_by}]}` (`package` null for a plain dependency; `kept`: packages another dependency still places) |
| `GET /api/p/{project}/trust` | what the project trusts and what each installed dependency holds: `{trusted: [keys], dependencies: [{name, package, version, key, trusted, findings}]}`, one row per local name. In a changeset preview `trusted` includes the project's keys. A finding is `{content, cell, file, where, kind, line, text}` ([below](#trusting-a-dependency)) |
| `POST /api/p/{project}/trust` `{name, token}` or `{name, untrust: true}` | trust the dependency installed as `name`, with a confirmation token of kind `trust`, or take that trust back (no token): `{key, trusted}`. [Below](#trusting-a-dependency) |

### The lock status

`GET /api/p/{project}/deps` (and `/deps/lock`) answers:

| Field | |
|---|---|
| `format` | the lock's format, 1 or 2; null without undine.lock |
| `libraries` | `[{name, kind, state, ...}]`: own libraries (`kind: "own"`, or `"linked"` from outside the project), and dependencies (`kind: "dependency"`) with `git`, `rev`, `subdir`, `locked` and `installed` (commits), `state` (`ok`, `unlocked`, `missing`, `other version`, `modified`, `leftover`), `hash` (`"sha"` for a format 1 entry, `"tree"` for a format 2 one, null when not locked) and `in_store` (whether its files are all in the store, so that a sync needs no network). One row per local name: the dependencies project.json names in its order, then the names packages place, then leftovers. Each dependency row also has `package` and `version` (null for a plain dependency), `names` (every local name of that entry), `direct`, `via` (the packages that place it), `chain` (`demo -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) -> ...`, or null), `trusted` and `flagged` (`{spice, rtl, tcl}`; both null when not installed), `internal_cells`, and `pdk` (`{compatible, reason, note, built_with}`: `compatible` null with `reason` `no manifest` or `no [pdk]` when not checked; null for a row not locked) |
| `pdk` | `{requested, locked, commit, installed, state, changed, verified_by, ok}`: project.json's PDK, the lock's id and full commit (format 2), the id found here, and `state`, one of `none`, `missing`, `not locked`, `other version`, `not pinned`, `mismatch` (project.json and the lock disagree), `error` (a pin without tree hashes), `unchecked` (files changed since the last hash: run `pdk verify`), `modified`, `unverified` (matches the pin; trust on first use), `verified` (not given before PDK profiles publish hashes). `changed`: per part, the files `added`, `removed`, `modified`. The status never reads the PDK itself, only the hashes kept in the store |
| `tools` | `[{name, locked, now, now_path, ok}]`, `locked` and `now` each `{version, from, text?}` or null (not installed, or not recorded): the version number, where the executable came from (`pixi`, `UNDINE_TOOLS`, `undine-tools`, `env`, `PATH`; null in a format 1 lock), and the banner when no number could be read from it |
| `issues` | what makes `ok` false: a dependency not as locked, the PDK missing, modified or not as project.json asks, no undine.lock, a library linked from outside; a package the PDK does not fit, a project cell named like a package's cell, a lock that does not install what a package needs, a `trusted` entry that is not a key |
| `warnings` | worth a look, never an issue: another tool version or origin, another Undine version, tools not pinned (`pixi.lock` not recorded) |
| `notes` | the PDK unverified, unchecked or not pinned (format 1), another Undine commit or `pixi.lock`, and for format 1 that origins are not recorded; a dependency not trusted that holds what the trust rule refuses, PDK compatibility not checked, a package used at a commit other than its `built_with`, a project cell named like a plain dependency's, a name in project.json that shadows another library |
| `ok` | no issue |

The dependency calls are refused in a changeset preview. They run one at a time per project, each from project.json
as the one before it left it. They fetch (git) without holding up other requests and take the server's lock only to
swap the installed files and write project.json and undine.lock. What they fetch goes into the user's
[store](data.md#the-store), shared by every project, and is installed by hard link; in a format 2 lock, `add`,
`update` and `lock` pin an entry by `tree`, in a format 1 lock by `sha`. Undine's writers of those two files (the server's
requests, the `lib` commands of the CLI, adopting or reverting a changeset) take a lock file, `.undine/project.lock`,
so they never overwrite each other. A git command run on the project (checkout, pull, merge) does not take it: run it
when no `lib` command or dependency call is running.

### Dependency refusals

Refusals, with HTTP 400 and a `detail` that says what to do:

- **undine.lock it cannot read.** Every `/deps` call and `POST /pdk` read the lock before they change anything, and
  refuse one of a newer format (`undine.lock has format 3, made by a newer Undine (this one reads formats 1 and 2):
  update Undine ...`), one with a `format` no Undine writes, and one that is not valid JSON or not a JSON object.
  Nothing of the project is changed. `GET /api/p/{project}` still opens the project.
- **A lock that does not pin what project.json names.** `/deps/sync` refuses it, naming each dependency
  (`analog_ip: project.json asks for rev v2, undine.lock locks v1`, or `not in undine.lock`) and the `lib lock` and
  `lib update` commands; `/deps/lock` resolves it.
- **Offline and not in the store.** `/deps/sync` with `offline` (or `UNDINE_OFFLINE=1`) refuses when a locked
  dependency is neither in the store nor in a kept repository: `offline, and not in the store: analog_ip @
  229914698b1f (sha e6d28176...): run the sync with the network ...`.
- **A local repository outside `--dep-root`.** `POST /deps`, `/deps/sync`, `/deps/lock` and `/deps/{name}/update` refuse a `git`
  that is `file://`, an absolute path or `~/...` unless its real path (symlinks and `..` resolved) and its git
  directory are under a directory given with `serve --dep-root`; the message names `--dep-root` and the
  `python -m undine lib ...` command that does it from the command line. A `file://` URL with a host other than
  `localhost`, a query or a fragment is refused as `not a plain file:// URL (file:///path/to/repo)`. Remote URLs
  (`https://`, `ssh://`, `git://`, `git@host:path`) are not affected.

The refusals of [packages](../guide/libraries.md#packages) have codes of their own; `detail` is the whole message,
and each nothing changed:

| Code | HTTP | When | Extra |
|---|---|---|---|
| `resolution_refused` | 409 | `POST /deps`, `/deps/lock`, `/deps/{name}/update`: the requirements do not resolve (two versions of one package, a version that does not match, one local name for two packages, a reserved name, a cell clash, a package the PDK does not fit, a bad manifest, an index requirement, a local URL from a remote package) | `problems: [{code, message, chains}]`, `code` one of `conflict`, `version`, `local_name`, `reserved_name`, `cell_clash`, `pdk`, `manifest`, `index_dependency`, `local_url`, `package_mismatch`, `no_manifest` |
| `needs_lock_format_2` | 409 | a package with dependencies, or under another local name, on a format 1 lock; no route converts the lock: `lib lock PROJECT --format 2` | |
| `not_direct` | 400 | `/deps/{name}/update` or `/remove` of a name only packages place | `placed_by: [...]` |
| `pdk_incompatible` | 409 | `POST /pdk` to a PDK a locked package does not accept | `packages: [{name, package, version, chain, reason, family, variants, commits, compat, built_with}]` |
| `not_synced` | 409 | a locked package is in neither the store nor the project's install (a fresh clone): `lib sync` first | |
| `untrusted` | 403 | a job that would run a dependency's content no person trusted ([below](#trusting-a-dependency)) | `name`, `package`, `key`, `cell`, `findings` |

### Trusting a dependency

A simulation (`POST /api/p/{project}/sim`; the agents' sweeps and spec checks too), an LVS run (`/lvs`,
`/netgen-lvs`, when it starts), an RTL lint or simulation (`.../c/{cell}/rtl/lint`, `/rtl-sim`), and the netlist
view with Xschem's netlister (`GET .../c/{cell}/netlist?backend=xschem`) are refused with **403** and code `untrusted` when they
would use what the [trust rule](../guide/libraries.md#trusting-a-dependencys-code) refuses in a dependency the project
does not trust. The body has `name` (the dependency's local name), `package` (null without a manifest), `key`
(`analog_ip@1.2.0`, or `ip@229914698b1f`), `cell` (`<local>/<cell>`, or `<local>/<file>` for RTL and Tcl) and
`findings: [{file, kind, line, text}]`; with several dependencies refused at once these name the first, and
`findings` holds them all. `kind` is one of `control`, `control_line`, `path`, `unparsed`, `rtl_c`, `systemc`,
`rtl_system`, `dpi`, `rtl_path`, `rtl_define`, `tcl`. The netlist view with `backend=undine` is not refused: its
`warnings` add `not trusted: would be refused in a simulation: ...`.

A person trusts a dependency in the browser's trust dialog:

| Call | |
|---|---|
| `POST /api/confirmations` `{kind: "trust", project, name}` | after the person's click: `{token, expires, key, tree, findings}`, the token bound to the project, the local name, the key and the installed files' hash (`tree`). 404 when nothing is installed under `name`, 400 in a changeset preview or for an install without a record |
| `POST /api/p/{project}/trust` `{name, token}` | use the token up and add the key to project.json's `trusted`: `{key, trusted: true}`. 403 for no token, an unknown, used or expired one, one of another kind, project or name, and when what is installed changed since it was confirmed (`... changed since it was confirmed ...: confirm the trust again`); 400 in a changeset preview |
| `POST /api/p/{project}/trust` `{name, untrust: true}` | take the trust back, with no token: `{key, trusted: false}` |

A finding of the listing and the confirmation is `{content, cell, file, where, kind, line, text}`: `content` `spice`,
`rtl` or `tcl`; `cell` `<local>/<cell>` (null for RTL); `file` `<local>/<path>`; `where` the file and the instance
and attribute the text is in. As for PDK fetches, no MCP tool, `undine call` command, Python client method or agent
tool asks for a `trust` confirmation or trusts anything.

## PDK fetch

A PDK missing on the server is installed with ciel, only after a person confirms it in the browser's **Fetch PDK**
dialog ([Fetching a project's PDK](../guide/pdks.md#fetching-a-projects-pdk)):

| Call | |
|---|---|
| `GET /api/p/{project}/pdk/fetch-plan?allow_network=false` | what a fetch would run: `{family, variant, commit, libraries, source, root}`, `libraries` null for ciel's default set, `source` ciel's data source, `root` the PDK root ciel installs into (the first the server scans that has `ciel/`, does not exist yet or is empty). The commit comes from undine.lock's pin, else from the 12-character id, expanded from the installed PDKs, or with `allow_network=true` by `ciel ls-remote`. Reads only |
| `POST /api/confirmations` `{kind: "pdk", project, commit, variant, libraries, allow_network?}` | a token for exactly that fetch: `{token, expires, plan}`. The request must match the plan the server makes now (`commit` may be its first 7 or more characters; `libraries` null for ciel's default set), else 400 (`... show the fetch dialog again`). The token is single use, expires after five minutes (`expires`, a Unix time) and is bound to the project and the whole plan. The other kind is `trust` ([Trusting a dependency](#trusting-a-dependency)); any other is a 400. A changeset preview and a source other than ciel's default are refused |
| `POST /api/p/{project}/pdk/fetch` `{token}` | start `ciel fetch` as a job, once the token is used up: `{job}` (below). 403 for no token, an unknown, used or expired one, or one for another project; 400 for a changeset preview, or when the plan changed since it was confirmed (undine.lock or project.json changed: confirm again). One fetch of a PDK at a time: a second one waits, `queued` |
| `GET /api/p/{project}/pdk/fetch/{job}` | the job: `{id, project, state, log, result, error, created, ended}`, `state` one of `queued`, `running`, `done`, `failed`; `log` the last 200 lines of ciel's output; `result` `{path, verify}` once done, `verify` the PDK against undine.lock (`{state, changed, missing_libraries, extra_libraries, warnings}`, `state` as in `pdk verify`); `error` the reason it failed. 404 for a job the server does not know |

The job holds no lock of the server or the project while ciel runs, writes nothing into the project (undine.lock
included), and rescans the PDKs when ciel is done, so the project finds its PDK without a restart. Jobs live in the
server's memory: a restart forgets them (ciel's run ends with the server or runs to its end; fetch again, or rescan,
to find what it installed). The server keeps the last 50 finished jobs.

No MCP tool, `undine call` command, Python client method or agent tool asks for a confirmation token or starts a
fetch: only the dialog does, after the person's click. Undine has no authentication yet, so this keeps agents that work
through Undine's tools, and mistakes, from fetching; it is no defence against a process that runs as the person.

## Changesets

A changeset is a git branch of the project, `undine/<project>/<id>`, and its working copy, the project
`{project}~{id}` (its baseline, the base commit checked out read-only: `{project}~{id}~base`); see
[Changesets](../guide/changesets.md) and the design, [changesets on git branches](../design/changeset-branches.md).
`{project}` in these calls may also be a preview's name (except to open one): they act on the real project.

| Call | |
|---|---|
| `POST /api/p/{project}/changesets` | opens one (`{"title", "reason"}`, `X-Undine-Actor`): the record, with `"git_setup": true` when the project was in no git repository and `git-setup` made it one, `"first_commit"`: the project's first commit made then (its files as they were, a log's unsaved layout edits as a journal, as the base has them; Undine's own output and the changeset records left out; null when none was made: a commit of the person's came first), so that what is applied is committed, and `"first_commit_note"`: what to do after it (git's index not reset to it: run `git reset`), or why none was made; null. 400 `not_set_up` (below) when the project is in a repository not set up for Undine. 400 `unsupported_library` (below) when one of the project's own libraries is one a worktree cannot hold: at its folder, above it, inside it or where a link on the way leads, a symbolic link that leads out of the project or nowhere, whose text is an absolute path or climbs out of the project, or that the repository does not keep (nor what it leads to), or a git repository of its own (a clone, a submodule). That is checked before anything is set up: a refused open makes no repository ([the design](../design/changeset-branches.md#the-repository)) |
| `GET /api/p/{project}/changesets` | the changesets: `id`, `title`, `reason`, `actor`, `created`, `status`, `preview`, and `git`; `closing` (below) while one is being adopted, discarded or reverted. Newest first (by `created`, then by id) |
| `GET /api/p/{project}/changesets/{id}` | the record (below), with the changes by library (`changes`: the operations, net of undo and redo), the checks with their freshness (`verify`), and while open the writer (`writer`) and the last change of each view (`activity`); once closed, the changes and checks as archived, `retained`, and the evidence with its size now (`evidence.size`); `closing` while it is being closed |
| `GET /api/p/{project}/changesets/evidence` | the evidence of the closed changesets as it is now: `{"bytes", "raw" (of them, the waveforms a prune removes), "changesets": {id: {"bytes", "raw", "files", "shared", "raw_shared", "pruned"}}}` (`size` below) |
| `GET /api/p/{project}/changesets/{id}/evidence[?check=true]` | a closed changeset's evidence: `{"path", "captured", "commits"?, "size", "bytes" (as captured), "linked", "pruned" (the manifest's notes), "jobs" (each with `"pruned"`: its files a prune removed)}`; `check=true` reads every archived file against its hash: `"check": {"ok": count, "pruned", "missing", "changed": [path in the evidence]}` (a pruned file is `pruned`, not `missing`). 400 for an open one |
| `POST /api/p/{project}/changesets/{id}/evidence/prune` | removes the waveforms (`*.raw`, `*.vcd`, `*.fst`) of a closed changeset's archived runs, keeping their status, logs, results and the manifest's hashes, and notes it in the manifest and the record (`X-Undine-Actor`): `{"pruned": {"t", "by", "files", "bytes", "freed"}, "size"}` (`"freed"`: of the bytes, those no other name held; a working copy the closure kept frees the rest when it is removed). A person's call: 403 for an agent's actor (`agent:...`); 400 for an open changeset; 409 `busy` while it is being closed or reverted |
| `GET /api/p/{project}/changesets/{id}/diff[?lib=L&cell=C]` | the [semantic diff](#git-history) of the branch, in `/git/diff`'s shape: from the base to the working copy while it is open (what no operation recorded too: what an apply merges), to the tip it was closed with afterwards. 400 for an older changeset (no `git`) |
| `POST /api/p/{project}/changesets/{id}/verify[?fresh=true]` | the needed checks on the working copy and the baseline (the working copy is committed onto the branch first): DRC with the KLayout deck and, where the PDK profile says so (sky130), Magic (`checker`), LVS, and its ERC (`kind: "erc"`, warnings with `findings`); a job that ran on the same inputs is reused (`reused`), unless `fresh` |
| `POST /api/p/{project}/changesets/{id}/apply` | merges the branch into the project, all or nothing: the record with `"applied"` (below). A conflict is a 409 `merge_conflict` (below), nothing written, the changeset still open. 400 for an older changeset (open it again as a new one), while someone is writing it, or while an interrupted merge of it is still to be finished (below). One changeset is closed at a time: the call waits while another is being adopted, discarded or reverted (its `closing` says `waiting`); 409 `busy` while this one is being closed already (also for discard and revert) |
| `POST /api/p/{project}/changesets/{id}/revert` | takes an applied changeset back by the merge the other way round (the files it added kept): the record with `"reverted"`. A later edit of what it changed is a 409 `merge_conflict`. 400 for one applied before changesets were git branches, or while an interrupted merge of it is still to be finished |
| `POST /api/p/{project}/changesets/{id}/discard` | archives the evidence, removes the worktrees, keeps the branch: the record with `"discarded"` (`X-Undine-Actor`). 400 while an interrupted merge of it is still to be finished |
| `GET .../changesets/{id}/timeline`, `POST .../restore`, `GET .../activity`, `POST .../writer/stop` | the timeline, restoring a checkpoint, following the writer, stopping it ([Live changesets](../design/live-changesets.md)) |

```text
record       {"id", "project", "title", "reason", "actor", "created", "status": open | applied | discarded | reverted,
              "preview": "{project}~{id}", "baseline": "{project}~{id}~base", "libs": [lib],
              "git": Git (absent for a changeset made before changesets were git branches: review and discard only),
              "verify": [check], "evidence"?: Evidence, "cleanup_warning"?: text, "closing"?: Closing,
              "interrupted"?: text (a merge a stopped server left was taken back: the note, in cleanup_warning too;
              gone once an apply, revert, discard or recovery succeeds),
              "retained": closed, and the working copy its closure kept (a late edit) is still there to open,
              "applied": Merge | null, "reverted"?: Merge, "discarded"?: {"t", "by"}}
              ("base": {lib: {}}, the libraries, is kept for older readers)
Evidence     {"path", "captured", "jobs": how many runs, "bytes": its size as captured,
              "pruned"?: {"t", "by", "files", "bytes"} (its waveforms removed: the last prune, the files and bytes of all),
              "size"?: {"bytes", "raw", "files", "shared", "raw_shared"} (GET of a closed changeset: as it is now;
              "raw": the waveforms; "shared", "raw_shared": of the bytes and of the waveforms, those of files another
              name still holds, a working copy the closure kept: removing them from the evidence frees them only
              once that copy goes too)}
Closing      {"action": apply | discard | revert, "by", "since",
              "phase": waiting | committing | evidence | merging | cleanup,
              "behind"?: {"id", "title", "action"} | null (waiting: the closure under way; null: another operation),
              "files"?, "files_total"?, "bytes"?, "bytes_total"? (evidence: done of how many)}
              (in memory: only while this server closes it; also in GET /api/p/{project}/live)
Git          {"branch": "undine/<project>/<id>", "base": sha (the project's files when it was opened),
              "head": sha | null (the project's HEAD then), "prefix": the project's directory in its repository,
              "base_seq": {lib: the last seq of the log in the base}, "tip": sha | null (the branch's last commit;
              null: none yet), "ref": "refs/undine/changesets/<project>/<id>" | null (the branch kept once closed),
              "commits": the commits from the base to the tip}
Merge        {"t", "by", "commit": sha | null (the merge commit, or the revert's commit, on the user's branch; null: not
              committed), "uncommitted": the merge was left in the working tree, "why": why it was ("no_commit": the
              repository has no commit yet, POST .../git/first-commit makes one; "changes": the project had other
              uncommitted changes; "failed": the commit failed, or Undine stopped before it; null when committed,
              absent in an older record), "paths": [the files it wrote], "tip": the commit applied (apply),
              "kept": [the files the changeset added, kept] (revert)}
409          {"detail", "code": "merge_conflict", "params": {},
              "files": [{"path" (from the project's directory), "kind": schematic | symbol | layout | netlist |
                         intent | log | text, "conflicts": [Conflict]}]}
400          {"detail", "code": "not_set_up", "top": the repository's top (a path), "project": the project's name,
              "command": the command line that sets it up, "params": {"top", "project", "command"}}
              (opening a changeset in a repository not set up for Undine; "command" is `undine git-setup <project>`,
              with `--root <root>` first in a store that is not the command's default one)
400          {"detail", "code": "unsupported_library", "library", "path": the link or the repository (from the
              project's directory), "kind": link_out | link_nowhere | own_repository, "target": the link's text |
              null (a repository), "params": {"library", "path", "kind", "target"}}
              (opening a changeset of a project with a library a worktree cannot hold. link_out: the link leads out
              of the project, its text is an absolute path, or it climbs out of the project; link_nowhere: it leads
              to nothing, or the repository does not keep it or what it leads to; own_repository: a clone or a
              submodule. The detail says what to do)
```

A `Conflict` is the one of `/git/verify` (below): `{"id", "what", "base", "ours", "theirs", "taken", "resolved"}`; for
an apply, `ours` is the design now, `theirs` the proposal and `base` the value when it was opened; for a revert,
`theirs` is the value before it was adopted and `base` as adopted. A conflict about a whole file has the id `file`
(changed on one side, deleted on the other: `what` "deleted on ours, changed on the other side"; a side that cannot
be read as its view: `what` "the <kind> file cannot be read (<the error>)", e.g. an intent whose YAML is broken, the
sides null; a directory where the file was: `what` "not a file in the project"), `lines` (a file of no view, merged
by lines), `error` (a merge that failed: `what` "cannot merge: ...", a side that cannot be read, a binary file) or
`changed` (the file changed in the project while the merge was being made, by something that did not wait for it,
such as another process: nothing was written, what that process wrote is kept, and applying or reverting again
merges it with that edit), with the sides' states (`changed`, `deleted`, `added`) as values. The checks across files
are conflicts too: of a symbol transaction's member changed since (`symbol:<id>`), of a schematic placed since that
uses the symbol (`symbol-consumer:<lib>/<cell>`), and of unsaved layout edits in the proposal's log
(`layout-journal`). `what` is English; the review words these ids and states in the UI's language itself (a `file`
conflict by the form of its `what`, a file that cannot be read with its error; any other `what` as it is).

An apply or a revert writes `<project>/changesets/<id>/merging.json` before it writes the project's files, and removes
it once the record says applied (reverted). A server stopped in between leaves it: `undine serve` deals with it at
its start, before the netlist watcher and the API, and so does the next request that reads the changeset (or lists
the project's changesets, the first time after a start). The merge is finished when it was all written (the commit
found only if Undine made it for this merge, else the merge is recorded as uncommitted); otherwise the files are put
back, keeping what was done since (records appended to a log, a file the merge had not reached; a file changed since
the merge wrote it is left as it is), and the changeset is left as it was, with the note in `interrupted`. Until
then apply, revert and discard are a 400.

## Git history

A project kept in git (see [Version control with git](data.md#version-control-with-git)) shows its history by cell.
Paths are from the project directory, also when the project is a subdirectory of its repository (`prefix`), except
those of `/git/verify`: it answers what `undine merge-check --json` prints, with paths from the repository's top, as
git and `undine resolve` take them (prepend `prefix` to compare them with the others). A file directly in a library's
directory is a view of a cell: `<cell>.sch` schematic, `.sym` symbol, `.oas` layout, `.netlist.yaml` netlist,
`.intent.yaml` intent, `.sv` / `.v` rtl; every other file (`project.json`, `undine.lock`, `history/<lib>.jsonl`, ...)
is "other", of which the status and a diff list the first 500 by path. Undine's own output (`runs/`, `tapeout/`,
`llm/`, `.undine/`, `*.pre-split`) is never listed, whether or not the repository ignores it. Statuses are `added`,
`deleted`, `modified`, `untracked` (not ignored) and `conflicted`; a renamed file is deleted and added. A changeset's
working copy (`{project}~{changeset}`) is a worktree of its branch, with no history of its own here: its status names
the changeset, and its diff is [the changeset's](#changesets); the log, the diff, verify and setup are a 400 for it.

| Call | |
|---|---|
| `GET /api/p/{project}/git` | the status: the working tree and the index against HEAD by cell, the branch, HEAD's commit, a merge in progress, what `git-setup` set up in this clone, the open layouts with edits not saved to the cell files (`unsaved`; a commit of their log is refused by the pre-commit hook; a library with no cell: the edits cancel out, and the hook refuses them all the same), and the open layouts whose cell files changed on disk under such edits (`stale`: a git checkout, merge or pull; saving them is refused until the edits are dropped, see `reload` below). Outside a repository `{"repo": false, ...}` (`setup` and `prefix` null, the lists empty but `stale`), never an error; for a changeset's working copy `"preview": true` with `"changeset"`, `"branch"` and `"base"`, and its own `unsaved`, `stale` and `reads` |
| `POST /api/p/{project}/git/setup` | [`undine git-setup`](cli.md): `git init` when the project is in no repository, the attributes, the diff and merge drivers, the pre-commit hook; after its `git init`, the project's first commit too (its files as they are, Undine's own output and the changeset records left out, author `user`: an applied changeset is committed only onto a branch with a commit). Returns the status with `"hook_note"`: why the pre-commit hook was not installed, with the line to add to the hook that is there (the repository has a pre-commit hook of its own, or `core.hooksPath` is set: `git-setup` never writes into those), null when it is installed; `"init"`: it ran `git init`; `"gitignore_added"`: the lines of Undine's own output it added to the project's `.gitignore` (the status' `setup.gitignore` lists those it lacks; nothing else adds them); `"commit"`: the first commit (null: no `git init`, or refused); `"commit_refused"`: why it was refused, as `first-commit` would answer (`{"code": "unsaved", "detail", "logs"}` or `{"code": "unsupported_library", "detail", "library", "path", "kind", "target"}`), null; `"index_note"`: git's index not reset to the commit, null. 400 for a changeset's working copy |
| `POST /api/p/{project}/git/first-commit` | the project's first commit, in a repository that has none (made by hand, or set up by an older Undine): its files as they are, from what the index tracks of the project, Undine's own output and the changeset records left out (and untracked again when staged), author `user`; in a repository whose project is a subdirectory, only the project's directory. Returns the status with `"commit"` and `"index_note"` (another git held the index: the commit is made, run `git reset`; null). 409 `has_commits` when the branch has a commit already (one made meanwhile too); 409 `unsaved` with `"logs"` (`[{"path", "lib", "cells"}]`) while a log holds layout edits not saved to the cell files, which the pre-commit hook refuses in a commit too; 400 when the project is in no repository; 400 `not_set_up` (below) in one not set up for Undine; 400 `unsupported_library` (below) for a library a changeset's worktree cannot hold. The History panel's **Make the first commit**: no agent tool makes it |
| `GET /api/p/{project}/git/log?limit=50&skip=0[&lib=L&cell=C]` | the commits of HEAD that changed the project (with `lib` and `cell`: that cell's files), newest first, each with the cells it changed against its first parent. `limit` 1 to 500; `more`: there is a next page |
| `GET /api/p/{project}/git/diff?rev=REV[&lib=L&cell=C]` | the [semantic diff](../design/merge.md) of commit `REV` against its first parent (a root commit: against nothing), or with `rev=worktree` of the working tree against HEAD; by cell and view |
| `GET /api/p/{project}/git/verify[?rev=REV]` | `undine merge-check --commit REV --json` of a merge commit: the cells to verify with DRC / LVS and why, warnings, notes, files merged without a report here. 400 for a commit that is no merge. Without `rev`, `undine merge-check --json` of the merge, cherry-pick or rebase in progress: also the conflicts still open, each with its base, ours and theirs value (400 when none is in progress). Paths from the repository's top |
| `POST /api/p/{project}/l/{lib}/reload` | drops the library's unsaved layout edits and reads its cell files again, what the panel's **Discard the edits and reload** does for a `stale` library: `{"version", "dirty", "result": {"dropped": there were unsaved edits}, "undo"}`. The operation log keeps the dropped edits as history, ended by a layout `save` record with `"args": {"reload": true}`, so they are never replayed and undo stops there. Until then `POST /api/p/{project}/l/{lib}/save` of a `stale` library is a 400 |

Not a repository, an unknown revision, a merge check of a commit with one parent or of no merge in progress is a 400;
an unknown project 404. While `project.json` holds a merge's conflict markers, the status, the log and the diff still
answer, with the libraries of HEAD's `project.json`.

```text
Commit       {"sha", "short", "parents": [sha], "subject", "body", "author", "email", "date" (ISO 8601),
              "refs": [branch and tag names]}
CellChange   {"lib", "cell", "views": [{"view": schematic | symbol | layout | netlist | intent | rtl,
                                        "path", "status"}]}
status       {"repo", "preview", "setup": {"merge", "diff", "hook", "gitignore"} | null, "prefix": "" | "designs/prj/" | null,
              "branch": name | null (detached HEAD), "head": Commit | null (no commit yet),
              "operation": null | "merge" | "cherry-pick" | "rebase", "conflicted": [path],
              "cells": [CellChange], "other": [{"path", "status"}], "unsaved": [{"lib", "cells": [cell]}],
              "stale": [{"lib", "cells": [cell]}], "reads": {lib: n}}
              (reads: how often each open layout read its cell files again because they changed on disk, without
              unsaved edits; an editor showing that library draws anew when it goes up)
              (setup and prefix are null outside a repository and for a changeset's working copy; setup.gitignore:
              the lines of Undine's own output the project's .gitignore lacks, which only git-setup adds)
setup        status + {"hook_note": text | null, "gitignore_added": [line]}
log          {"commits": [Commit + {"cells": [CellChange], "other": number of other files}], "more"}
diff         {"base": sha | null, "rev": sha | null (the working tree),
              "cells": [CellChange, each view also with "diff"], "other": [{"path", "status"}]}
verify       {"ours": sha, "theirs": sha | null (no commit is known for it: git merge --squash, cherry-pick -n),
              "commit": sha | null (in progress), "parents": [sha] | null (more than two: an octopus merge),
              "operation": null (a commit) | "merge" | "cherry-pick" | "rebase",
              "unmerged": [{"path", "kind", "conflicts": [Conflict] | null (git's own conflict markers), "error"?}],
              "warnings": [{"path", "warning"}], "merged": [path], "no_report": [path],
              "verify": [{"lib", "cell", "why", "reason"}], "notes": [text],
              "note_codes": [{"code": no_theirs | octopus | earlier, "operation"?: rebase | cherry-pick}]}
Conflict     {"id", "what", "base", "ours", "theirs", "taken": ours | theirs (the file holds it), "resolved"}
reason       {"kind": "merged"} (its own files were merged)
             | {"kind": "side", "side", "below": [{"lib", "cell", "side": side | null (merged)}]} (its files are
               that side's, the cells below it are not)
             | {"kind": "resolves", "side"} (what it instantiates resolves to other cells)
             side: "ours" | "theirs" | "parent N" (of an octopus merge)
```

A view's `diff` is what `undine diff --json` prints for the file: `{"kind", "changes": [{"do": add | del | mod, "id",
"what", "before", "after"}]}`, for a layout also `"geometry": [{"cell", "layer", "xor_um2", "geometry_unchanged"}]`
(the XOR area of each changed layer) and `"note"` (what that XOR covers: each cell's own shapes, instantiated cells
not flattened into it), for RTL `{"kind": "text", "do"}`, and `{"kind", "error"}` when a version cannot be read. `id`
names the object as the merge does (`inst:M2:prop:W`, `wire:<end points>`, `shape:<cell>:<layer>`, `item:<id>`, ...);
`what` is the English `undine diff` prints; the panel words the ids it knows in the UI's language itself. Likewise
`why` and `notes` of `verify` are merge-check's English, and `reason` and `note_codes` carry the same as data.
