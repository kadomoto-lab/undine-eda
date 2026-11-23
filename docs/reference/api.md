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
| `POST /api/p/{project}/deps` `{name, git, rev?, subdir?, package?}` or `{name, package, version?, index?}` | add a [library dependency](../guide/libraries.md) (`lib add`), or with no `git` a [package from an index](../guide/libraries.md#packages-from-an-index) (`pkg add`: `version` a requirement, default `^` the newest that fits; `index` a nickname or URL, default the default index; needs lock format 2): fetched, installed read-only and locked, with the packages it depends on. Returns the lock entry with `name`, `cells`, `public_cells`, `internal_cells`, `created` (whether it made undine.lock), `package` and `version` (null for a plain dependency), `resolved` (each entry resolved: `{name, ...entry}`, for a package also `package`, `version`, `names`, `via`, `flagged`, `trusted`), `flagged` (`{spice, rtl, tcl}`: what the trust rule finds in it) and `trusted`. `package`: the [package] name its manifest must give. A local `git` (`file://`, `/path`, `~/path`) is refused unless the server was started with `--dep-root` and the repository is under one of those directories; the same holds for sync and update, and for every URL a manifest names. A fetch from a remote repository is never made by this route: it answers 409 `needs_fetch` with the plan, which a person confirms ([Dependency fetch](#dependency-fetch)); a local repository under `--dep-root` needs no confirmation. Refusals: [below](#dependency-refusals) |
| `POST /api/p/{project}/deps/sync` `{offline?}` | install every dependency exactly as locked (`lib sync`), and remove each leftover (its files and its undine.lock entry); returns `{libraries: [{name, action, commit}]}`, `action` one of `ok` (installed as locked already), `installed`, `removed` (`commit` may be null for `removed`). A package's row also has `package`, and a row whose package the locked PDK does not fit has `warning` (`CHAIN: does not fit the PDK ID: REASON`): the package is installed all the same, and `GET /deps` reports it as an issue. The body is optional: `{"offline": true}` never fetches, `false` may, and no body (or `null`) follows the server's `UNDINE_OFFLINE`. Frozen: a lock that does not pin a dependency as project.json names it is refused before anything is read (`lib lock` resolves it). All or nothing: when one dependency fails (a remote unreachable, files that do not match the lock, offline and not in the store), none is installed and none removed. A dependency in the [store](data.md#the-store) is installed from there and reads no repository; a local repository, also one project.json names, is checked against `--dep-root` before git reads it (a fetch, or a commit kept in the store's copy of the repository). An entry only a remote repository has is 409 `needs_fetch` ([Dependency fetch](#dependency-fetch)), before anything is fetched |
| `POST /api/p/{project}/deps/lock` | `lib lock`: fetch, install and lock what undine.lock does not pin as project.json names it (under the `--dep-root` rule, as add; a remote is 409 `needs_fetch`), and record the tool versions in use now (in a format 1 lock the PDK id too); returns the status with `resolved: [{name, git, rev?, subdir?, commit, sha or tree}]`. It writes the lock in its own format: making it format 2 is the command line's (`lib lock --format 2`, `pdk verify --pin`) |
| `POST /api/p/{project}/deps/{name}/update` `{rev?, version?}` | move a dependency to `rev` (default: its rev again), or a package from an index to the newest version its requirement accepts (`version`: a new requirement, written to project.json); returns `{name, from, to, rev, changed, used_by, packages_changed}`: the changed files and the cells using them, and `packages_changed: [{package, names, from, to, changed}]` for each entry that moved, was brought in (`from` null) or is no longer needed (`to` null). The trust of what moved is taken out of project.json. A package from an index moves within the index's local copy as it is (the route never fetches the index: a person refreshes it first); a fetch from a remote is 409 `needs_fetch` |
| `POST /api/p/{project}/deps/{name}/remove` | remove a dependency, with the packages nothing needs any more; returns `{ok, name, removed_packages: [{package, version, commit, names}], kept: [{name, placed_by}]}` (`package` null for a plain dependency; `kept`: packages another dependency still places) |
| `POST /api/p/{project}/deps/changeset` `{action, name?, git?, rev?, subdir?, package?, version?, index?, pdk?, title?, reason?}` | the browser's own Add, Update, Remove and Set PDK: a small changeset opened (title `title`, default `Add NAME`, `Update NAME`, `Remove NAME`, `Set the PDK to PDK`; a project in no git repository is made one first, as any [changeset](../guide/changesets.md) open does), the operation run in its working copy (`action` `add`, `update`, `remove` with the fields of the routes above, or `pdk` with `pdk`), then adopted, which installs it in the project from the store. Returns `{changeset, status: "applied", result, git_setup, first_commit, first_commit_note, deps?, deps_warning?, deps_dropped?, deps_pending?}`: `result` what the direct route returns (`POST /deps`, `/deps/{name}/update`, `/deps/{name}/remove`, or `{from, to, created}` as `POST /pdk`), `git_setup`, `first_commit` and `first_commit_note` as the changeset's open answers them, the rest as the adopt's [Merge](#changesets). What the request alone shows wrong (an add with neither `git` nor `package`, a name that is taken or is no dependency, no or an unknown `pdk`, and a fetch from a remote no person confirmed: 409 `needs_fetch`, its plan's `request` with `adopt: true`) is refused before anything is opened, so such a refusal never makes the project a repository. An operation refused answers as the direct route would, the changeset discarded; an adopt refused (409 `merge_conflict`, ...) carries `changeset` in its body and `params`, the changeset left open for review. On a preview (`{project}~{id}`) the operation goes into that changeset, not adopted: `status: "open"`. The direct routes above work as before |
| `GET /api/p/{project}/trust` | what the project trusts and what each installed dependency holds: `{trusted: [keys], dependencies: [{name, package, version, key, trusted, findings}]}`, one row per local name. In a changeset preview `trusted` includes the project's keys. A finding is `{content, cell, file, where, kind, line, text}` ([below](#trusting-a-dependency)) |
| `GET /api/packages?q=&index=&project=` | the [Packages section](../guide/libraries.md#packages-in-the-browser): the newest version that is not yanked of each package of an index whose name or description holds `q` (every package for an empty one): `{index, index_name, commit, packages: [{name, version, description, license, yanked, fits}]}`. `index`: a nickname or URL (default: the default index); `project`: for the nicknames its project.json names, and `fits` (`{ok, reason}`: whether the version accepts the project's PDK; null without `project`). It never fetches the index: one with no local copy is 409 `index_not_fetched` with `{index, url, host}` (`index` its nickname, `host` null for a local one), and the local copy is read until a refresh |
| `GET /api/packages/{name}?index=&project=` | every version of a package, newest first: `{index, index_name, commit, name, newest, locked, versions: [{name, version, description, license, yanked, fits, git, subdir, commit, tree, size, pdk, exports, deps: [{name, package, req, index, index_name}]}]}`. `newest`: the newest that is not yanked; `locked`: the version the project locks of it from this index, or null. 404 when the index lists no such package; 409 `index_not_fetched` as above |
| `GET /api/indexes?project=` | the indexes known here and to the project: `{indexes: [{name, url, commit}]}` (`commit` null before the first fetch) |
| `POST /api/indexes/{name}/refresh?project=` `{token}` | fetch the index `name` (a nickname) again, with a confirmation token of kind `index` for its URL ([Dependency fetch](#dependency-fetch)): `{index, index_name, commit}`. 403 for no token, an unknown, used or expired one, one of another kind, and one for another index or project |
| `POST /api/p/{project}/trust` `{name, token}` or `{name, untrust: true}` | trust the dependency installed as `name`, with a confirmation token of kind `trust`, or take that trust back (no token): `{key, trusted}`. [Below](#trusting-a-dependency) |

### The lock status

`GET /api/p/{project}/deps` (and `/deps/lock`) answers:

| Field | |
|---|---|
| `format` | the lock's format, 1 or 2; null without undine.lock |
| `libraries` | `[{name, kind, state, ...}]`: own libraries (`kind: "own"`, or `"linked"` from outside the project), and dependencies (`kind: "dependency"`) with `git`, `rev`, `subdir`, `locked` and `installed` (commits), `state` (`ok`, `unlocked`, `missing`, `other version`, `modified`, `leftover`), `hash` (`"sha"` for a format 1 entry, `"tree"` for a format 2 one, null when not locked) and `in_store` (whether its files are all in the store, so that a sync needs no network). One row per local name: the dependencies project.json names in its order, then the names packages place, then leftovers. Each dependency row also has `package` and `version` (null for a plain dependency), `names` (every local name of that entry), `direct`, `via` (the packages that place it), `chain` (`demo -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) -> ...`, or null), `trusted` and `flagged` (`{spice, rtl, tcl}`; both null when not installed), `internal_cells`, and `pdk` (`{compatible, reason, note, built_with}`: `compatible` null with `reason` `no manifest` or `no [pdk]` when not checked; null for a row not locked). A package from an index has `index` (its URL) and `requirement` (project.json's), and `newer` (`{accepted, newest}`) when the local copy of its index lists a newer version: the newest its requirement accepts and the newest listed, each null when not newer. The local copy is read as it is, never fetched |
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
  229914698b1f (sha e6d28176...): run the sync with the network ...`. The adopt of a changeset refuses the same way
  (409 `merge_conflict`, a conflict of undine.lock) a lock entry it brings whose files are neither in the store nor
  installed in the project: `analog_ip @ 229914698b1f (...) is not in the store: fetch it first`.
- **An index the browser may not read.** `GET /api/packages`, `/api/packages/{name}`, the confirmation of kind
  `index` and `/api/indexes/{name}/refresh` read an index under the rule of a git dependency: a local index only below
  `--dep-root`, a remote one only on a host of the configured indexes or one given with `--dep-host`; refused (400)
  before any fetch.
- **A local repository outside `--dep-root`.** `POST /deps`, `/deps/sync`, `/deps/lock` and `/deps/{name}/update` refuse a `git`
  that is `file://`, an absolute path or `~/...` unless its real path (symlinks and `..` resolved) and its git
  directory are under a directory given with `serve --dep-root`; the message names `--dep-root` and the
  `python -m undine lib ...` command that does it from the command line. A `file://` URL with a host other than
  `localhost`, a query or a fragment is refused as `not a plain file:// URL (file:///path/to/repo)`. Remote URLs
  (`https://`, `ssh://`, `git://`, `git@host:path`) are checked by their host instead (next item).
- **A remote host not allowed.** The same routes fetch a remote repository or index only from the host of an index
  this machine configures (the default index's, `github.com`, and those in `$UNDINE_HOME/config.toml`, not a
  project's own `indexes`) or one given with `serve --dep-host`. This covers a URL from the request and one that
  project.json, a manifest or an index line names. An index on another host is refused before git runs:
  `https://gitlab.com/me/index.git: from the browser this server fetches only from github.com (...); not from
  gitlab.com. Start the server with --dep-host gitlab.com, or run python -m undine pkg index update ... from the
  command line`. A repository on another host is a fetch for the person to confirm (409 `needs_fetch`, its row
  `allowed: false`: the dialog warns), which then passes whatever its host. From these routes, a commit the server
  does not serve by itself (an abbreviated id, a commit no branch or tag reaches) is not looked for by fetching every
  branch and tag unless the person confirmed that URL; the message says to run the operation once from the command
  line, which keeps the commit for the browser to read.

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
| `needs_fetch` | 409 | `POST /deps`, `/deps/sync`, `/deps/lock`, `/deps/{name}/update`, `/deps/changeset`, `/changesets/{id}/deps`: a fetch from a remote no person confirmed ([Dependency fetch](#dependency-fetch)) | `plan: {op, fetches, complete, project, request}` |
| `index_not_fetched` | 409 | `GET /api/packages`, `/api/packages/{name}`, and an add, update or lock that reads an index with no local copy: a person refreshes it first | `index`, `url`, `host` |
| `busy` | 409 | a confirmation of kind `dependency`, or the one-click `POST /deps/changeset` (before anything is opened), while another dependency operation of the project runs | |
| `not_pending` | 409 | a confirmation of kind `dependency` with `op_id`, or `POST /deps/fetch` with its token, when the agent's operation no longer waits for a fetch: a restore took it back, a fetch installed it already, a later operation on the same dependency replaced it, it could no longer apply (dropped, with a `fetch_failed` notice: the first such confirmation drops it), or its changeset was applied or discarded (the token is used up) | |
| `files_changed` | 403 | a dependency fetch job whose project.json or undine.lock changed after the confirmation, found once it holds the dependency lock | |

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

## Dependency fetch

Over HTTP a dependency operation fetches from a remote repository only what a person confirmed in the browser's
**Fetch dependencies** dialog, and an index is fetched only by a refresh the person confirmed
([Confirmation token](../design/packages.md#ui-cli-http-agent-tools)). A route that would fetch answers 409
`needs_fetch` with nothing fetched or changed; its `plan`:

| Field | |
|---|---|
| `op` | `add`, `update`, `lock` or `sync` |
| `fetches` | `[{url, rev, commit, subdir, tree, size, license, package, version, host, allowed}]`: each repository and commit (or rev) to fetch. A package from an index has its commit, tree, size and licence from the index line; a plain git dependency named by a rev has `rev` and, before its first fetch, `commit` null (after one, the commit that fetch gave, checked again). `host` is the URL's host (null for a local one) and `allowed` whether the server fetches from it without asking (the configured indexes' hosts and `--dep-host`) |
| `complete` | false when what is fetched may name more (a plain git dependency's manifest, read only once fetched): a second confirmation may follow |
| `project` | the project (the real one, also for a changeset) |
| `request` | the operation, as the confirmation takes it back: `{op, name?, git?, rev?, subdir?, package?, version?, index?, changeset?, op_id?, adopt?, reason?, title?}`; `changeset` runs it in that changeset's working copy, `op_id` names an agent's operation of that changeset waiting for a fetch ([below](#an-agents-operation-waiting-for-a-fetch)), `adopt: true` is the browser's one-click operation (a small changeset, adopted) |

| Call | |
|---|---|
| `POST /api/confirmations` `{kind: "dependency", project, ...request, fetches: [{url, rev, commit, subdir, tree}]}` | after the person's click: `{token, expires, plan}`. The server makes the plan again (fetching nothing; staging is looked at too, nothing written) and the request's `fetches` must be exactly its fetches (by URL, rev, commit and subdir; a tree both give must agree), else 400 (`... show the fetch dialog again`). The token is single use, lives five minutes and is bound to the project, the operation, those fetches and project.json with undine.lock (byte for byte, of the changeset's working copy for a changeset). 409 `busy` while another dependency operation of the project runs |
| `POST /api/p/{project}/deps/fetch` `{token}` | use the token up and run the operation as a job: `{job}` (below). It fetches from a remote exactly the confirmed fetches (a URL so confirmed passes whatever its host), a local repository under `--dep-root`, and nothing else. 403 for no token, an unknown, used or expired one, one of another kind or project, and when project.json or undine.lock changed since the confirmation (`... changed since the fetch was confirmed: confirm the fetch again`); the token is used up either way |
| `GET /api/p/{project}/deps/fetch/{job}` | the job: `{id, project, kind: "dependency", state, log, progress, result, error, code, created, ended}`; `state` one of `queued`, `running`, `done`, `failed`; `progress` approximate, `{url, bytes, limit}` while git fetches (the pack's bytes received) and `{url, files, of}` while the files are read into the store; `result` what the operation's route returns (`POST /deps`, `/deps/sync`, `/deps/lock`, `/deps/{name}/update`, `/deps/changeset`, `/changesets/{id}/deps`); `error` and `code` as that route's refusal (a refusal with no code of its own gets the status's: `invalid_request`, `not_found` ...), with its extra fields (`changeset`, `problems`, ...) in `params` and, where they do not clash with the job's own, in the job. A one-click operation waits for the project's dependency lock, then under it checks the files, plans again and opens its changeset; a fetch the token does not hold (the index refreshed meanwhile, a kept commit gone) is refused (`needs_fetch`) before anything is opened. The operation checks project.json and undine.lock again once it holds the project's dependency lock (a job may wait behind another that changes them): changed, it fails with `code: "files_changed"`, nothing fetched or written. A fetch that shows more to fetch (a plain git dependency's manifest naming another, or a rev that now gives another commit than the plan showed) fails with `code: "needs_fetch"` and the next `plan`, which the person confirms again. 404 for a job the server does not know, or a PDK job |
| `POST /api/confirmations` `{kind: "index", index, project?}` | after the person's click on the index's refresh: `{token, expires, index, url, host, commit}` (`commit` the local copy's, null before the first fetch), the token bound to the index's URL and the project. The index is checked as a refresh checks it (`--dep-root`, `--dep-host`; 400) |

One dependency job of a project runs at a time; they live in the server's memory, apart from the PDK's, and a restart
forgets them (what a job fetched stays in the store, and the next try installs from there). The command line fetches
as before, asking nothing. No MCP tool, `undine call` command, Python client method or agent tool asks for a token of
these kinds, carries one or starts a fetch.

### An agent's operation waiting for a fetch

An agent proposes a dependency operation in its changeset (`propose_dependency`); it never fetches. What needs a fetch
is recorded in the working copy's project log as waiting for one (`{k: "d", state: "needs_fetch", op_id, needs}`,
nothing written or installed), and `GET /api/p/{project}/changesets/{id}` lists it while the changeset is open:
`pending_deps: [{op_id, action, op, name, args, actor, reason, gseq, t, needs, plan}]`, `plan` the needs_fetch plan
whose `request` carries `changeset` and `op_id`. The record's `notices: [{at, kind, op_id, name, text}]` are what the
agent's `wait_for(event='changeset')` returns, each once per agent (also those that came before the call;
`notices_given` counts them per reader); a notice with `closes: true` dropped its operation, which a `dep_drop` record
of the project log (`drops`) records, so a restore past it brings the operation back. Only an operation that can no
longer apply is dropped (the name taken, not a dependency); a failure that passes (git, the network, the store, a
configuration) fails and leaves it pending, and so does a resolution refused (409 `resolution_refused`, with a
`fetch_failed` notice that does not close it, once per distinct text). While an agent's batch step of the changeset
runs, the fetch job and a drop are busy (409 `busy`). A
later operation on the same dependency supersedes a pending one (its record's `supersedes`). In `changes`, a needs_fetch effect carries
`outcome` once something became of it (`installed`, `superseded`, `dropped`; in a closed changeset's evidence, one never
fetched `cancelled`).

| Call | |
|---|---|
| `POST /api/confirmations` `{kind: "dependency", project, changeset, op_id, fetches}` | the person's Fetch in the review. The operation's own arguments are used (the record's, not the body's); the plan is made again, and when it differs from `fetches` (the index refreshed since, say) the answer is 409 `needs_fetch` with the new plan for the dialog to show; a plan with nothing left to fetch (the store has it now) is taken as it is. The token is bound as above, and to the operation's id and a hash of its record. 409 `not_pending` when the operation no longer waits (restored away, installed, the changeset closed) |
| `POST /api/p/{project}/deps/fetch` `{token}` | as above; refused with 409 `not_pending` (the token used up) when the operation was cancelled since the confirmation. The job replays the operation in the changeset's working copy, records it installed (`replays: op_id`, `fetched_by`, the agent's actor and reason kept), appends a notice of kind `fetched` to the changeset, and returns `{changeset, op, result, state: "installed", op_id, notice, status}`. The agent's own write lease does not stop it; another writer's is retried for about three seconds, then the job fails with `code: "busy"` (what it fetched stays in the store: the next Fetch only installs). An operation that can no longer apply is dropped with a `fetch_failed` notice that closes it; a resolution refused fails the job with `resolution_refused` and leaves it pending, with a `fetch_failed` notice that does not close it. A fetch that shows more to fetch fails with `needs_fetch` and the next plan, as above |

### The review of a dependency change

`GET /api/p/{project}/changesets/{id}` has `deps`, one row per dependency operation of the changeset still in effect
(the project log's records, oldest first), for an open changeset and a closed one alike: what a person reads before
adopting a dependency change. A proposal a person's fetch installed is the row of the record that installed it (its
`op_id`, `fetched_by`); a `dep_drop` record is no row (the proposal's row has `outcome: "dropped"`).

```text
{gseq, opid, op: dep_add | dep_update | dep_remove | pdk_set, action: add | update | remove | pdk_set, name, actor,
 reason, t, state: installed | needs_fetch | cancelled, outcome?: superseded | dropped, op_id?, fetched_by?, pdk?,
 project: {kind, changes}, lock: {kind, changes},          depmerge.diff of the effect's before and after
 packages: [{package, names, do: add | mod | del, from, to, commit_from, commit_to, hash, license, license_files,
             size, size_from: index | files, changed: {added, removed, modified, *_more} | null,
             cells: [{cell, do: add | del | mod | renamed, views, from?, exported?}], cells_more?}], packages_more?,
 used_by: [text], used_by_more?,                           the project's cells that use a changed or removed cell
 license, size, size_from, own, size_total,                the dependency's own (a needs_fetch row: its own fetch's);
                                                           size_total every package's (fetch's) added up
 code: [{name, package, key, trusted, trust, blocks_total, blocks_refused, blocks: [{cell, file, where, kind, line,
         text, path?, commands?, refused, why}], rtl_total, rtl_refused, rtl: [{path, refused,
         findings: [{kind, line, text, path?}], findings_more?}]}] | null, code_more?,
 trusted, trust, untrusted?: [key], untrusted_count?, trust_taken_back?: [key], trust_unknown?, trust_drop: [key],
 checked: true | false | null, checked_by?: {job, kind, lib?, cell?}, replaced_by?: gseq,
 needs?: [{url, rev, commit, subdir, size, license, package, version}], needs_more?,
 recorded, review_state: stored | made | pending | failed, review_error?}
```

- `packages`, `used_by` and `code` are the operation's **review** (`store/depreview.py`), made right after the
  operation and outside the server's locks, from the effect's before and after and the store (the trees read from the
  store's objects), and kept beside the changeset's worktrees, one per operation, with the PDK its code listing was
  made for; the evidence copies the reviews at closure (the manifest's `reviews`, by `<gseq>-<effect index>`), so a
  closed changeset's review reads the same. `review_state`: `stored` (kept), `made` (this read made it: it was not
  kept yet, or was made for another PDK than the working copy's now, whose code listing is made again), `pending`
  (this read was made under a lock no review is made under: a restore's or a verify's answer) or `failed` (it could
  not be made: `review_error`; not kept, tried again at a later read at most every ten seconds, by the closure once
  more whatever the wait, and archived as failed, with why, only if the closure still cannot make it);
  `recorded` is `review_state == "stored"`. `packages` compares the store's records of both trees, so its `changed` is `null` when the
  store has no record of a side; `cells` applies the new version's `[renames]`; `license` is the manifest's
  (`license_from: "manifest"`), else the local copy of the index's (`"index"`); `size` is the index line's
  (`size_from: "index"`) else the bytes of the files in the store (`"files"`). An index copy being refreshed is not
  read (the review never waits for a fetch): the store's size and the manifest's licence stand. `code` lists, per
  package added or moved, every code block of its cells and every RTL file ([trust](../design/packages.md#security))
  with `refused` exactly as a job's trust rule decides it (the PDK root and the netlister's variables given, so a
  model include under the project's PDK is not refused), packages with refused code first.
- Bounded: at most 10 packages, 10 code entries (a package with no code block and no RTL file is none; `code_more`
  counts the packages with code not listed), 20 files per list, 30 cells per package, 20 cells in
  `used_by`, 20 code blocks and 20 RTL files per operation (over its packages) with 3 findings each, 10 fetches of a
  needs_fetch row; the totals and `*_more` count the rest. Every text
  from a package (paths, cell names, code, findings, licences, versions, a needs_fetch URL) has its control characters
  made spaces and is cut (200 characters): it is data written by others.
- `trusted` (each package's in `code`, and the row's: every version the operation brought, from its lock, whatever the
  review holds) is read from the real project's `trusted` now, for the key the jobs and the trust dialog use
  (`trust.key_of`: the tree's manifest name and version, read from the store whatever the lock format records, else
  `<local name>@<commit12>` for each local name; a code entry under several names has them in `keys`; the keys read
  when the review was made are kept with it, so a store gc'd since names the same ones; one that cannot be told now is
  the lock's package and version, else `<name>@?`, never trusted, with `trust_unknown`). So a version a person trusts
  after the operation shows as trusted. `trust` says it in words, `untrusted` names the first 10 versions not trusted
  and `untrusted_count` counts them all.
- While the changeset is open, a key it takes out of project.json's `trusted` (an add or update to other content
  under a trusted version: `trust_drop` of its rows) counts as not trusted, as the adopt will leave it, whatever the
  project trusts today, and is listed in `trust_taken_back`; trusting it again changes nothing, as it is trusted
  already. Trust is by version, so until the adopt the runs in the changeset do run that code: a code entry under such
  a key says so (`taken_back`, and its `trust` in words). `trust_drop` lists the keys the operation took out of the
  working copy's `trusted` (an update drops the old version's).
- `license` and `size` are the named dependency's own (`own` true; its report is listed first): null, `own` false,
  when only packages it needs moved, or, waiting for a fetch, when none of the fetches is its own (each package's and
  each fetch's are in `packages` and `needs`).
- `checked` is `false` (*unchecked*) until a run of the changeset read the package: a simulation, an LVS or a DRC of
  the working copy, done, whose evidence names one of the trees the operation brought in its `about.packages`, and
  (open) still fresh; `checked_by` names it. Only trees the operation really brought count (an entry whose tree did
  not change, one that only gained a local name, brought none): the named dependency's own tree when it brought it,
  else any of them; and only those the changeset still has (the last operation's lock):
  an operation whose trees a later one replaced all of has `checked: null` and `replaced_by`, that operation's gseq,
  whose row says whether the package was checked. For a closed changeset, the runs its evidence archived. `null`
  otherwise: nothing to check (a removal, a PDK change, an operation that brought no tree).
- A `needs_fetch` row has `needs` (the URL, the size, the licence) and no `code` (nothing fetched yet); its Fetch is
  `pending_deps`' ([An agent's operation waiting for a fetch](#an-agents-operation-waiting-for-a-fetch)).

## PDK fetch

A PDK missing on the server is installed with ciel, only after a person confirms it in the browser's **Fetch PDK**
dialog ([Fetching a project's PDK](../guide/pdks.md#fetching-a-projects-pdk)):

| Call | |
|---|---|
| `GET /api/p/{project}/pdk/fetch-plan?allow_network=false` | what a fetch would run: `{family, variant, commit, libraries, source, root}`, `libraries` null for ciel's default set, `source` ciel's data source, `root` the PDK root ciel installs into (the first the server scans that has `ciel/`, does not exist yet or is empty). The commit comes from undine.lock's pin, else from the 12-character id, expanded from the installed PDKs, or with `allow_network=true` by `ciel ls-remote`. Reads only |
| `POST /api/confirmations` `{kind: "pdk", project, commit, variant, libraries, allow_network?}` | a token for exactly that fetch: `{token, expires, plan}`. The request must match the plan the server makes now (`commit` may be its first 7 or more characters; `libraries` null for ciel's default set), else 400 (`... show the fetch dialog again`). The token is single use, expires after five minutes (`expires`, a Unix time) and is bound to the project and the whole plan. The other kinds are `trust` ([Trusting a dependency](#trusting-a-dependency)), `dependency` and `index` ([Dependency fetch](#dependency-fetch)); any other is a 400. A changeset preview and a source other than ciel's default are refused |
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
| `GET /api/p/{project}/changesets/{id}` | the record (below), with the changes by library (`changes`: the operations, net of undo and redo), the checks with their freshness (`verify`), and while open the writer (`writer`), the last change of each view (`activity`) and the agents' dependency operations waiting for a fetch (`pending_deps`, [An agent's operation waiting for a fetch](#an-agents-operation-waiting-for-a-fetch)); open or closed, the review of its dependency operations (`deps`, [The review of a dependency change](#the-review-of-a-dependency-change)); the record's `notices` are the changeset's notices; once closed, the changes and checks as archived, `retained`, and the evidence with its size now (`evidence.size`); `closing` while it is being closed |
| `GET /api/p/{project}/changesets/evidence` | the evidence of the closed changesets as it is now: `{"bytes", "raw" (of them, the waveforms a prune removes), "changesets": {id: {"bytes", "raw", "files", "shared", "raw_shared", "pruned"}}}` (`size` below) |
| `GET /api/p/{project}/changesets/{id}/evidence[?check=true]` | a closed changeset's evidence: `{"path", "captured", "commits"?, "size", "bytes" (as captured), "linked", "pruned" (the manifest's notes), "jobs" (each with `"pruned"`: its files a prune removed, and the job's `"about"`: the packages, PDK and profile it used, when it recorded them), "packages" and "pdk" (`{"base", "work"}`: what each side's undine.lock pins; see [Job provenance](data.md#job-provenance))}`; `check=true` reads every archived file against its hash: `"check": {"ok": count, "pruned", "missing", "changed": [path in the evidence]}` (a pruned file is `pruned`, not `missing`). 400 for an open one |
| `POST /api/p/{project}/changesets/{id}/evidence/prune` | removes the waveforms (`*.raw`, `*.vcd`, `*.fst`) of a closed changeset's archived runs, keeping their status, logs, results and the manifest's hashes, and notes it in the manifest and the record (`X-Undine-Actor`): `{"pruned": {"t", "by", "files", "bytes", "freed"}, "size"}` (`"freed"`: of the bytes, those no other name held; a working copy the closure kept frees the rest when it is removed). A person's call: 403 for an agent's actor (`agent:...`); 400 for an open changeset; 409 `busy` while it is being closed or reverted |
| `GET /api/p/{project}/changesets/{id}/diff[?lib=L&cell=C]` | the [semantic diff](#git-history) of the branch, in `/git/diff`'s shape: from the base to the working copy while it is open (what no operation recorded too: what an apply merges), to the tip it was closed with afterwards. 400 for an older changeset (no `git`) |
| `POST /api/p/{project}/changesets/{id}/verify[?fresh=true]` | the needed checks on the working copy and the baseline (the working copy is committed onto the branch first): DRC with the KLayout deck and, where the PDK profile says so (sky130), Magic (`checker`), LVS, and its ERC (`kind: "erc"`, warnings with `findings`); a job that ran on the same inputs is reused (`reused`), unless `fresh` |
| `POST /api/p/{project}/changesets/{id}/apply` | merges the branch into the project, all or nothing: the record with `"applied"` (below). A conflict is a 409 `merge_conflict` (below), nothing written, the changeset still open. 400 for an older changeset (open it again as a new one), while someone is writing it, or while an interrupted merge of it is still to be finished (below). One changeset is closed at a time: the call waits while another is being adopted, discarded or reverted (its `closing` says `waiting`); 409 `busy` while this one is being closed already (also for discard and revert) |
| `POST /api/p/{project}/changesets/{id}/revert` | takes an applied changeset back by the merge the other way round (the files it added kept): the record with `"reverted"`. A later edit of what it changed is a 409 `merge_conflict`. 400 for one applied before changesets were git branches, or while an interrupted merge of it is still to be finished |
| `POST /api/p/{project}/changesets/{id}/discard` | archives the evidence, removes the worktrees, keeps the branch: the record with `"discarded"` (`X-Undine-Actor`). 400 while an interrupted merge of it is still to be finished |
| `POST /api/p/{project}/changesets/{id}/deps` `{action, name, git?, rev?, subdir?, package?, version?, index?, reason?}` | a dependency operation in the working copy (`action` `add`, `update` or `remove`, with the fields of `POST /deps` and `/deps/{name}/update`): fetched under the working copy's own dependency lock, then written and installed there (never in the project, nor the baseline: 403), and recorded in its project log (`history/@project.jsonl`, a name no library can have) with project.json and undine.lock before and after; the timeline shows it, restore takes it back (installing from the store), the adopt merges and installs it. Returns `{changeset, op: {gseq, opid} | null (nothing changed), result: what the direct route returns, status: the working copy's deps status}`; refusals as the direct routes', and 409 `busy` while someone else holds the write lease |
| `POST /api/p/{project}/changesets/{id}/pdk` `{pdk, reason?}` | pins the working copy to another PDK, recorded the same way (`pdk_set`): `{changeset, op, old, new, created}`; 409 `pdk_incompatible` as `POST /pdk` |
| `GET .../changesets/{id}/timeline`, `POST .../restore`, `GET .../activity`, `POST .../writer/stop` | the timeline (with the changeset's `notices`), restoring a checkpoint (a restore past an agent's operation waiting for a fetch cancels it: the restore record's `cancelled`), following the writer, stopping it ([Live changesets](../design/live-changesets.md)) |

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
              "kept": [the files the changeset added, kept] (revert),
              "deps"?: [the install's rows, as POST /deps/sync's] (a merge that changed undine.lock or project.json's
              dependencies installs them from the store, offline; nothing is fetched at adopt),
              "deps_warning"?: what the install could not do (said, the merge done: lib sync installs later), or a
              dependency that went while cells still place it, "deps_dropped"?: [{"name", "used_by": ["lib/cell"]}],
              "deps_pending"?: true (another operation held the project's dependencies, a `pkg add` fetching: the
              adopt never waits for it, `lib sync` installs them)}
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
              "cells": [CellChange, each view also with "diff"], "other": [{"path", "status", "deps"?}]}
              (deps: project.json's and undine.lock's semantic diff, entry by entry (vcs/depmerge.diff):
              {"kind": "project" | "lock", "changes": [{"do", "id", "what", "before", "after"}], "more"?: entries
              left out past the first 200, "error"?: a side that is not a JSON object})
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
