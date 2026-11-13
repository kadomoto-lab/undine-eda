# Live changesets (design)

Status: accepted design; implemented (steps 1 to 3, and the local MCP launcher), on git branches since
[changesets on git branches](changeset-branches.md).

## Goal

The assistant's work on a proposal is visible while it happens, can be stopped, and can be taken back to any earlier
point, without giving up what changesets provide: the real design is untouched until a person applies the proposal,
checks compare baseline and proposal, and the evidence is archived at apply or discard.

So the changeset stays the unit of review. What changes is that it is no longer a result that arrives at the end, but a
working branch the person watches grow: the UI shows the working copy while the assistant edits it. Since
[changesets on git branches](changeset-branches.md) that is literally so: the working copy is a git worktree of the
branch `undine/<project>/<id>`, committed onto it when a writer's lease ends, and what follows applies to it.

Editing the real design live (with undo) was rejected: it loses the before / after checks and the evidence, mixes the
assistant's edits with people's edits to the real design, and cannot hold for external agents over MCP.

## Decisions

| Question | Decision |
|---|---|
| Can a person edit the proposal while the assistant works on it? | No. One writer per changeset at a time, enforced by the server (below). |
| What does "go back" mean? | Restore the design state at a chosen checkpoint, recorded as a new entry. Not repeated undo. |
| Which checkpoints? | Any point on the changeset's timeline from the start up to now (the end). No reverting of a single item in the middle (yet). |
| Does the review open by itself? | Yes, once, on the screen that sent the request, when the first change is committed. |

## 1. Order and identity (PR 1)

Every operation log is per library (`history/<lib>.jsonl`, `seq` per file). A changeset can touch several libraries, so
its timeline needs one order across them. The working copy is a worktree of the changeset's branch, so its logs start
as the project's were at the base; the changeset's own records are those after the last `seq` at the base, per library
(`base_seq` in its record, the *floor* of its logs). Records written in a changeset's working copy get three more
fields (and `changeset`, its id):

| Field | Meaning |
|---|---|
| `gseq` | Position in the changeset, across all its libraries (1, 2, 3 ...). Assigned when the record is appended. |
| `opid` | One operation as the writer sees it: a tool call of the assistant, one HTTP request of a person. A `create_cell` that makes three views writes three records with one `opid`. Checkpoints are only placed between operations, never inside one. |
| `turn` | The assistant turn (one request in a conversation) the operation belongs to, or null for a person's edit. |

`gseq` is what the UI follows: a drawing is refreshed when a record with a new `gseq` is committed, so changes that a
failing tool call did commit are still shown. Numbering, the timeline, restore, the review's operations, undo and redo
only see the records above the floor: the project's history before the base, and the records of changesets applied
before (which carry their own `gseq`), are not this changeset's. A layout's journal in the base (unsaved layout edits
of the project) is not replayed in the working copy, which starts from the files.

## 2. One writer per changeset (PR 1)

A changeset has at most one write lease: `{owner, kind: turn | mcp, since, expires?, stop_requested}`. While a lease is
held, every design write in the working copy by anyone else is refused (409): manual edits, undo / redo, restoring a
checkpoint, and apply / discard of the changeset. Checks (DRC / LVS jobs) are not design writes and may run.

The check sits where all writes pass: the schematic editor and the layout database consult the working copy's operation
log hooks before they change anything. HTTP routes, the in-app assistant and MCP all end up there, so a second tab or an
external client cannot bypass it.

- **In-app assistant**: the turn takes the lease when it starts in an open changeset, or when it opens one, and gives it
  back when it ends (done, stopped, failed, interrupted).
- **MCP clients** have no notion of a turn. A session takes the lease on its first write in a changeset and keeps it while
  it writes; it lapses after 120 s without a write. Stopping an MCP session revokes its lease; later writes from that
  session are refused until it is released.
- **Several agents on one MCP session** (a client's subagents over one `mcp-stdio` relay) are writers of their own
  when each call names its agent (`agent`): each has its own changeset and lease (owner `mcp:<session>:<agent>`, label
  `<client>/<agent>`, actor `agent:<client>/<agent>`), so one agent never writes the changeset another holds, and
  stopping one stops that agent only. The session's end releases the leases of all its agents. Calls without a name
  are the session's own writer (`mcp:<session>`): they all share one changeset.
- **A named shell session is an agent.** `undine call --session NAME` passes `agent=NAME` on every call that gives no
  `agent` of its own, so the shell's sessions are told apart like subagents (owner `mcp:<session>:NAME`, actor
  `agent:undine-cli/NAME`). The default session (no `--session`) stays unnamed. A session named before this version
  kept its changeset as its unnamed writer's: its first call marks the name as the session's own
  (`_meta: {"undine/own_agent": true}`), and the server hands that changeset and its lease to the name, so the agent
  goes on at once; a stopped writer stays stopped. This happens once: the client then marks the session `named` in its
  sessions file, and a session it made itself is marked from the start. So a changeset the session's unnamed agent
  opens later (`agent=""`) stays that agent's.
- **Who am I.** `open_changeset` and `list_changesets` answer with `you`, the caller as the review names it
  (`undine-cli/bootsw-a (MCP session 3fa693cf)`), and the audit log records each MCP call's session. A call that runs
  in another agent's session (two agents sharing one helper script) shows it at once.

## 3. Assistant turns as jobs (PR 1)

A chat request no longer blocks until the assistant is done. It creates a turn job and returns its id at once.

- **Accepted**: the request, conversation, turn and client request id are saved first. Sending the same client request
  id again returns the same turn (a retried POST after a lost response does not start a second turn).
- **Progress**: the job appends events: assistant text, a tool call started, the records it committed (`gseq`), the tool
  call finished (ok or error). Clients read events after an index, so a reloaded page picks up where it was.
- **States**: `running` → `done` | `failed`; `stopping` → `stopped`; `interrupted`.
  Stop sets `stopping`; the loop checks it before each model call and each tool call, so a tool call that is editing
  finishes its edit first. Changes already committed stay.
- **Server restart**: unfinished turns become `interrupted`, keep their committed changes and are not re-run.
- Checks started by the assistant are jobs of their own; the turn being stopped does not stop them, and the UI shows the
  two separately.
- The conversation stores the user message when the turn is accepted and the reply when it ends; a turn that did not
  finish is shown with its state and is not replayed to the model as if it had.

## 4. Timeline and checkpoint restore (PR 2)

The review gets a timeline view next to the per-object view: operations in `gseq` order grouped by turn, with the person's
edits between them.

**Restore to checkpoint G** brings the working copy to the state it had right after the operation ending at `gseq` G:

- The target state is computed from the log, not by undoing what is currently in effect: records after G are walked
  backwards, and each is inverted, including undo / redo records (inverting an undo re-applies what it undid). This also
  covers "added → checkpoint → undone", which a stack of undos cannot bring back.
- It is applied to every library and cell view it touches, all or nothing: if one part fails, what was already applied is
  taken back and nothing is recorded.
- It is recorded as `restore` records with `to: G`: one per touched cell view with the effects it applied, and a marker
  per library it covers (`view: null`). The operation list of the changeset, the undo stacks and the commits of the
  branch all follow it: the net of the log is recomputed by taking every record before the restore except this
  changeset's records after G (records of earlier changesets in the same log stay), so the review shows exactly what is
  in effect, and the next commit of the branch has the restored files.
- Cells created after G are removed: a schematic or symbol file (`v`) or a layout cell (`c`) is deleted only when it is
  still as created and nothing that remains refers to it; otherwise the restore is refused with the reason. (This is a
  rule for the working copy. Reverting an applied changeset keeps the files it added.)
- Check results are not deleted: they stay in the history and are valid only while their input hashes match.

The next assistant turn is told about it explicitly: the checkpoint restored, the range taken back, and the current design,
so that "created ota" earlier in the conversation is not mistaken for the current state. The report of changes since the
assistant's last turn is built from the log records (including undo, redo and restore), not from the operations still in
effect.

## 5. Opening and following (PR 3)

- The screen that sent the request opens the review when the first change is committed, once. Other screens showing the
  changeset show that the assistant is working.
- A review the person closed is not reopened by later changes.
- The drawing follows the cell the assistant is editing. Choosing another cell or looking at an earlier point of the
  timeline pauses following; "Back to latest" resumes it. "Stop following" and "Stop the assistant" are separate controls.
- Only the cells that changed are reloaded, keeping position and scale.

## 6. Steps and retries (batch, request_id)

- **A step.** `batch` runs several tool calls as one operation (one `opid`), all or nothing.
  - `Changesets.batch` snapshots the working copy (the top folder of every library, `history`, `project.json`,
    `undine.lock`) and the changeset's record, and puts them back if an op fails, or always for a dry run. The
    snapshot is beside the worktree (`.changesets/<project>/<id>/.batch-*`), never in it.
  - The same snapshot-and-put-back is what restore uses when it fails.
- **Numbering never repeats.** Putting records back would make `gseq` restart below numbers a following screen has
  already seen. So the highest number given is kept in `.changesets/<project>/<id>/gseq.floor`, beside the worktree (so
  it is never committed onto the branch nor merged into the project), and the next record is numbered past it.
  - While a step runs, the activity endpoint hides its records (`srv.batching`). A screen sees a kept step at once,
    and never sees a step that was put back.
- **Request ids.** A write called with a `request_id` runs at most once per project. Its success is kept in
  `changesets/requests.jsonl` (the last 1000) and returned again for the same id.
  - Each result is a file of its own in `changesets/requests/`, so the list searched on every write stays small
    when clients take whole results (a `batch` of netlists). One over 100000 characters is kept shortened, as
    [long results](../guide/assistant.md#long-results) are, and says so.
  - The same id with other arguments is refused. A failed call is not kept, since it changed nothing. A retry that
    arrives while the first call runs waits for it.
  - So "did my call go through?" always has an answer, the problem a bridge to a GUI tool cannot solve, because it
    cannot tell whether a timed-out request was executed.

## 7. Reading and checking another's changeset

A verifier (an agent that checks a designer's work) must run the checks on that changeset's working copy without
becoming its writer. Opening it (`open_changeset id=...`) would make it the verifier's changeset, take its lease on the
first write, and put the verifier's jobs into the session's picture of that changeset.

- **`changeset` on the read and check tools.** `get_layout_info`, `get_device_geometry`, `query_shapes`,
  `render_layout`, `get_drc_report`, `get_lvs_report`, `get_netlist`, `get_intent`, `run_checks`, `check_specs`,
  `sweep`, `run_simulation`, `extract_parasitics`, `get_parasitics`, `wait_for`, `plot_waveform`,
  `sample_waveform`, `cancel_job` and the other reads take `changeset: cs-...`. The call runs in a copy of the
  caller's context whose changeset is that one, marked as reading (`ToolCtx.reading`); the caller's own context, its
  open changeset and its session's record of it are not touched. The answer says which changeset it read: a JSON
  answer starts with `changeset`, a text answer with the line `changeset: cs-...`. `changeset` reads also when it is
  the caller's own changeset, unless the caller holds its lease: a verifier that once opened the changeset it checks
  (`open_changeset id=...`) is still a reader, and is neither refused while the writer writes nor given the lease
  (and the writer's work committed under its name) when the writer's lease lapsed. What the answer says to call next
  (`wait_for`, `get_parasitics`, `run_simulation job=`) carries `changeset`, and a job looked for outside its
  changeset says to pass it. `batch` refuses an op with `changeset` (a batch runs in the caller's own changeset).
- **No lease.** A reading call takes and renews no lease, and any path that would write the working copy
  (`need_changeset`) refuses. A write tool given `changeset` is refused before it runs. So the writer goes on writing
  while the verifier checks, and nothing the verifier does can be taken for the writer's edit.
- **Recorded like the owner's.** `run_checks` records its rows in the changeset (`verify`), as the writer's run would,
  and extractions and simulations run in that working copy's `runs/`, where the writer's `get_parasitics`,
  `check_specs (extracted)` and the review find them. A reader does not commit the working copy before its checks
  (the writer's own `run_checks` or the end of its lease does): each check records the layout and schematic it ran
  on (their hashes, taken under the layout's lock, from memory), so a later edit makes the row outdated, not wrong.
  A layout the writer never saved is checked as it is in memory.
- **Jobs.** Every job records who started it (`owner`, and `reader: true` for a reader: `live.started_by`):
  simulations, DRC, LVS, extractions, RTL simulations, LibreLane runs and symbol checks. The reader may stop the jobs
  it started (`cancel_job changeset=...` stops only the caller's own jobs there), and the first restart from an older
  server does not take a reader's simulation as the reader's changeset (`Sessions._seed`).
- **Job ids** are the time to the second and 4 hex digits (`20261104-133330-00b7`). The first 3 digits count the
  ids of that second in the process, so ids of one second sort in the order they were made (the newest run of a cell
  is the last id); the last digit is random. One process never gives an id twice (past 4096 in a second the suffix is
  random), and the run directory is made exclusively, with another id when it exists, so 60 checks started in one
  second never meet.

## Steps

| PR | Content |
|---|---|
| 1 | `gseq` / `opid` / `turn` on records, the write lease and its check on all writes, turn jobs (accept, progress, stop, interrupt), chat UI showing progress with a stop button |
| 2 | Timeline view, restore to checkpoint (including created cells), telling the next turn about restores and undos |
| 3 | Opening the review on the first change, refreshing only changed cells, following the edited cell |
