# Connecting to Undine

All three ways reach the same tools on a running Undine server (default `http://127.0.0.1:8700`, or `$UNDINE_URL`).
MCP must be on in that server: start it with `UNDINE_MCP=1 ./scripts/restart.sh`, or `undine serve --mcp`.

## MCP

- **Local desktop clients**: `undine mcp-stdio`. It reuses a running Undine or starts one with MCP on, relays to
  `/mcp`, and opens the changeset's review in the browser once when you open a changeset. It also adds the tool
  `open_in_undine` (project, changeset) to show it again. With `--ssh HOST` it reaches an Undine on another machine
  through an SSH port forward and reads the token there (it never starts that server).
- **Over HTTP**: POST JSON-RPC to `/mcp` with `authorization: Bearer <token>`. The token is in
  `<projects root>/mcp-token`. The `mcp-session-id` from `initialize` identifies your session; your open changeset
  belongs to it. DELETE `/mcp` ends the session.
- **Several agents on one connection**: every tool takes an optional `agent`, your own name. Pass it when several
  agents share this connection, such as a client's subagents over one `mcp-stdio`: otherwise they share one changeset,
  and one agent's edits land in the changeset another opened. Calls with the same name share a changeset (and its
  write lock); the history records the name with each edit (`agent:<client>/<agent>`); one agent cannot write a
  changeset another holds, and the refusal names the holder. `list_changesets` marks your changeset (`yours`) and says
  who is writing each (`writer`). Without `agent`, every call of the connection shares one changeset. A name is up to
  64 letters, digits, `.`, `_` or `-`; give each agent its own and keep it for all of its calls.
- **Request ids**: write tools take an optional `request_id`. The same id again returns the first result (marked
  `_meta: {"undine/replayed": true}`) instead of acting twice. Ids are per `agent`: another agent's same id is a call
  of its own, never a replay of yours.
- **Long results**: an MCP client gets up to 20000 characters per result (`params._meta: {"undine/max_chars": N}` asks
  for another budget, 0 for all of it). See [Long results](#long-results).
- **Resources**: this guide is served as `undine://skills/undine/...` (resources/list, resources/read).

## Shell: `undine call`

```sh
undine tools                                   # every tool, one line each
undine tools set_instance_params               # its arguments
export UNDINE_PROJECT=demo
undine call get_netlist lib=mylib cell=inv
undine call open_changeset title="Wider pull-down" reason="fall time misses s1"
undine call set_instance_params lib=mylib cell=inv instance=M2 params:='{"W": "1.5"}' reason="faster fall"
undine call write_rtl lib=mylib cell=counter text=@counter.sv reason="add a synchronous clear"
undine call run_checks
undine session end                             # let go of the session; the changeset stays open for review
```

- **Argument syntax**: `key=value` is a string, `key:=json` any JSON value (number, list, object, `true`),
  `key=@path` the text of a file, and `key:=@path` the JSON value in a file (`instances:=@cells.json`). `--json
  '{...}'` (`--json -` for stdin, `--json @args.json` from a file) passes them all at once. A lone value where a
  tool takes a list of names is that list (`signals=out`, `objects=M5`), and `key=true` / `key=false` is a yes / no
  (`move=false` is false).
- **Output**: the result is printed as JSON (`--raw` keeps the tool's own text), at most 25000 characters: a longer
  one is shortened inside and stays valid JSON ([Long results](#long-results)). `--max-chars N` (or
  `$UNDINE_MAX_CHARS`) changes that; `--max-chars 0` prints all of it, for a file or `jq`.
- **Tool arguments**: `undine tools NAME` shows each argument's shape, also what a list holds, e.g. `objects: [string
  or {kind: instance|wire, id: string}, ...]`. A wrong shape is a usage error that shows the right one.
- **Pictures** (`render_layout`, `render_schematic`, `plot_waveform`): written to `--out FILE.png` (or a new file in
  `--out` when it is a directory), else to a new file in `--out-dir`, `$UNDINE_OUT_DIR`, the folder of
  `$UNDINE_CLI_SESSIONS` when it is set, or the current directory; the output gives the path.
- **Exit codes**: 1 when a tool refuses or fails, with its message on stderr; 2 when Undine cannot be reached.
- **Starting the server**: `--start` starts Undine with MCP on if it is not running.
- **Another machine**: `--ssh HOST` (or `$UNDINE_SSH`) forwards a port to Undine on `HOST` for the call and reads the
  token there; `--url` is then the server as seen on `HOST`. Sessions are kept per host. It never starts that server.
- **Retries**: every call carries a request id, printed first on stderr (`undine: request id ...`). A write sent
  again with the same id (`--request-id ID`, for example after your shell's timeout cut the call off, or when a
  script is re-run after a failure) returns the first result instead of acting twice.
- **Sessions**: the shell keeps one MCP session per `--session` name (default `default`) in
  `~/.undine/cli-sessions.json`. A changeset you opened stays yours in the next call, also after a server restart (the
  server keeps the session; if it forgot it, the client starts a new one and continues the changesets it had open).
  A new session of your own continues one with `open_changeset id=cs-...`. Agents that call `undine call` side by side
  each use a `--session` of their own (or the same session with `agent=NAME` each), and helper scripts of their own:
  a script that names another's `--session` or sessions file runs your calls in that agent's session.
- **A named session is an agent**: `--session NAME` calls every tool as `agent=NAME` (unless you pass `agent`), so the
  history (`agent:undine-cli/NAME`), the review and the write lock name you. The default session is unnamed. stderr
  says who each call runs as (`undine: request id ... (agent NAME)`), and `open_changeset` / `list_changesets` answer
  with `you`. A session that opened its changeset before this version goes on with it under its name (once: a
  changeset you open later with `agent=""` stays the unnamed agent's).
- **Verifying another's changeset**: `undine call run_checks changeset=cs-... --session verify` (also `check_specs`,
  `extract_parasitics`, `get_layout_info`, `render_layout`, `get_drc_report` and the other reads) runs on that
  changeset without taking its write lock or making it yours. See [verification.md](verification.md).
- **Server restarts**: a call whose `--session` already had a session with that server, made while it restarts,
  waits until it is back (up to 120 s, `$UNDINE_RESTART_WAIT_S`; also over `--ssh`) and is sent again with the same
  request id, so a write is done once. A call with a new `--session` name says at once that Undine is not running.
  Simulations go on. A `wait_for` in flight answers `"stopping": true` when the server stops: wait again.

## Python

```python
from undine.client import Undine, ToolError

with Undine(project="demo", agent="cdac-layout") as u:   # url=, start=True, ssh="user@host" as needed
    net = u.get_netlist(lib="mylib", cell="inv")            # a dict
    cs = u.open_changeset(title="Wider pull-down", reason="fall time misses s1")
    for inst in ("M2", "M4"):
        u.set_instance_params(lib="mylib", cell="inv", instance=inst, params={"W": "1.5"}, reason="faster fall")
    print(u.run_checks())
    print("review:", u.review_url(cs["id"]))
```

- **Who you are**: `agent="NAME"` is the shell's `--session NAME`: the history, the review and the write lock name it,
  and a changeset you open is that agent's. `session=` is not a name but the id of an MCP session (`u.session` of
  another client) to continue it; a name there is refused.
- **Calls**: `u.call("tool", **args)` is the same as `u.tool(**args)`.
- **Over SSH**: `ssh="user@host"` keeps a port forward open while the client runs and opens it again if it drops
  (a call is then sent again; its request id keeps a write from acting twice). `u.disconnect()` closes it and keeps
  the session; `close()` ends both.
- **Several edits at once**: `u.batch(ops=[{"tool": ..., "args": {...}}, ...], reason=..., dry_run=True)` runs
  them as one step, all or nothing.
- **Lost answers**: every call carries a `request_id` (`u.last_request_id`). When an answer is lost (a timeout, a
  dropped connection), the client sends the call again with the same id, so the server never acts twice.
  `u.last_replayed` says whether an answer was the first call's.
- **Whole results**: a script gets every result in full. `Undine(max_chars=N)` asks for at most N characters, as an
  MCP client gets them.
- **Errors**: `ToolError` carries the tool's message; `UndineError` means Undine cannot be used.
- **Ending a session**: leaving the `with` block ends the session. Pass `session=u.session` to another client to
  continue it.
- **Dependencies**: the client is the standard library only.

## Long results

A result longer than its reader's budget is shortened inside, never cut through, so it stays valid JSON. The budget
is 9000 characters for the in-app assistant, 20000 for MCP clients, 25000 printed by `undine call`, and all of it
for Python.

- What holds most of it gives way, and several large parts (the netlists of a `batch`) are shortened alike. A list
  or an object of many entries (instances by name) keeps its first entries, a text its start, and each says so in
  place: `"…": "76 more (116 in all)"` in an object, `"… 334 more (512 in all)"` as a list's last item, and a text
  ends in `… (2210 more characters)`.
- `_truncated` gives the full size, the budget, what was cut (`"instances": "40 of 116"`) and how to ask for the rest.
- Ask for less rather than more: `get_netlist(offset, limit)` pages a large cell (the answer has `next_offset`), and
  `instances` / `nets` pick a few; a testbench's long code blocks: `code: false` (their names only) or `code_limit`
  (characters per block); `get_intent(ids, kinds)`; `get_design_context(objects)` lists those first;
  `run_simulation` with fewer `signals`; `get_changeset` gives one entry per step (a batch is one; steps alike, the
  same operations for the same reason, as one with `steps: n`), its reason once and per cell its effects counted (the
  cells a step only created, such as a `sync_layout`'s PCell variants, in its `new_cells`; the answer's own
  `new_cells` lists every cell the changeset created and still has), `detail: true` each
  operation, `cell: "lib/cell"` one cell's (`operations` counts that cell's); `get_history` gives the cell's last
  steps whole, a batch of any size as one.
