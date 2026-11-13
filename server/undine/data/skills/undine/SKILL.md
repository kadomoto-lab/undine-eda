---
name: undine
description: "Design analog / mixed-signal IC cells in Undine (schematic, symbol, layout, RTL, design intent, DRC / LVS, ngspice and Verilator simulation, LibreLane) through its typed tools: MCP, `undine call`, or the Python client. Edits go into a changeset a person reviews."
---

# Undine

Undine is an AI-native, web-based EDA environment for custom IC design. A **cell** is the unit; its **views** are a schematic (with a netlist view that
holds the connectivity), a symbol, a layout, RTL, and testbenches. Each cell has a **design intent**: what it has to
achieve (goals, preferences, constraints, specs, context, each with why).

You work through the same typed tools as the in-app assistant. There is no code evaluation in the server: compose the
tools. Every result you read is recorded in the project's audit log.

## Choose the interface

- **MCP** (Claude Desktop / Code, other MCP clients): `undine mcp-stdio`, or Streamable HTTP at `/mcp` with the
  bearer token in `<projects root>/mcp-token`.
- **Shell**: `undine call TOOL --project P key=value key:=json key=@file`; `undine tools [TOOL]` lists tools or shows
  one tool's arguments. The shell keeps its session (and your changeset) between calls.
- **Python**: `from undine.client import Undine` (also `undine.agents.client`) - tools are methods, results come back
  parsed; `Undine(project=..., agent="NAME")` is the shell's `--session NAME` (`session=` takes an MCP session's id).

Details, including starting Undine and sessions: [references/connect.md](references/connect.md).
Pass the real `project` to every tool (not a `p~cs-...` preview).

**Several agents on one connection** (subagents of one client, which share its MCP connection): each passes its own
`agent` name (e.g. `agent: "layout"`) to every tool. Calls with the same name share a changeset; another name's
changeset is not yours to write. Without `agent`, every call of the connection shares one changeset. In the shell,
`--session NAME` is the agent `NAME`: give each agent its own `--session` (and its own helper scripts and scratch
files: an agent that runs another's script works in that agent's session). `open_changeset` and `list_changesets`
say who you are (`you`): check it.

**Verifying another agent's changeset**: pass `changeset: "cs-..."` to the read and check tools (`run_checks`,
`check_specs`, `extract_parasitics`, `get_layout_info`, `render_layout`, `get_drc_report` ...). They run on that
changeset's working copy without its write lock and without making it yours; what you run is recorded in it as its
writer's runs are. Do not `open_changeset id=...` a changeset you only verify.

## The loop

1. **Read before you change.** `get_design_context` (parameters, connectivity, neighbours, checks, recent changes,
   the intent summary), `get_netlist` for what is connected, `get_intent` for the full intent, `get_selection` for
   what the user points at ("this transistor"). To *see* a design, `render_schematic`, `render_layout` and
   `plot_waveform` return a picture with the numbers it shows (MCP, the shell and Python; not the in-app assistant).
2. **Open a changeset** with `open_changeset(title, reason)`, or continue one: `list_changesets`, then
   `open_changeset(id=...)`. Every edit goes into its working copy. Nothing touches the real design until a person
   reviews and applies it in Undine. You cannot apply, and while you write the changeset is yours alone.
3. **Edit** with the view's tools. Each edit takes a `reason`: say why, in the user's terms.
   - For several edits that belong together, use `batch(ops=[{tool, args}, ...], reason)`: one step, all or
     nothing. If one op fails, none is kept and the error names it.
   - Add `dry_run: true` to see each result and what would change, and keep nothing. Reads such as `get_netlist`
     inside the ops show the outcome.
4. **Verify**: `run_checks` (DRC / LVS / ERC on what you changed, baseline and working copy; `get_drc_report` says
   where the violations are), `check_specs` (each spec of
   the intent measured on its testbench, with margins; corners and temperatures too), `run_simulation` for anything
   else, `lint_rtl` / `run_rtl_sim` for RTL. To size against the specs, explore first with `sweep` (no edits). Once DRC and
   LVS are clean, `extract_parasitics` and the same specs with `extracted` give the post-layout figures. A slow block
   can be simulated as its behavioural model with `substitute` (a stand-in of the same pins). `get_changeset` shows
   what you recorded (a step per entry with its reason, its effects counted per cell; `detail: true` each operation)
   and the check results; `get_changeset(id=...)` shows any changeset, also one that was applied, discarded or
   reverted (how, when, by whom, its commit), to verify what was applied.
5. **Hand over**: tell the user what changed, why, what the checks and simulations say, and that it waits for their
   review in Undine (`open_in_undine` over `mcp-stdio`, or the review URL).
6. **Wait for the person instead of polling** when the work goes on after their answer:
   - `wait_for(events=["changeset", "selection"])` returns when they apply or discard the changeset, or select
     something in Undine ("look at this");
   - `job:<id>` returns when a long job (`run_librelane`, a simulation still running) ends; read a simulation's
     results with `run_simulation(job=...)`, stop an outdated one with `cancel_job`;
   - `design:<lib>/<cell>` returns when the cell changes in the real design.

## Rules

- **Work from the intent.** Never relax a `must` constraint or a spec on your own: propose it, and say so.
- **Record lasting requirements.** When the user states one (a requirement or priority, not a one-off "try W = 2"),
  record it with `add_intent` and quote their words in `request`.
- **The netlist is the truth for connectivity; the drawing shows it.** Read `get_netlist`. Tools that place devices
  without coordinates put them in free space and leave connections *pending* (a ratsnest) rather than redrawing what
  a person drew.
- **A lost answer is safe to resend.** Give each write a `request_id` (any unique string). Sending the same call
  again with the same id returns the first result instead of acting twice: after a timeout, resend; do not guess.
  The shell and Python client do this for you.
- **A tool error is an answer.** For example "no changeset is open" means call `open_changeset`. Read it, fix the
  call, and do not retry blindly.
- **A long result is shortened, never cut.** It stays valid JSON; `…` marks what was left out and `_truncated` says
  how much. Ask for the rest with the tool's filters or pages (`get_netlist` offset / instances / nets, `get_intent`
  ids / kinds): [connect.md](references/connect.md#long-results).
- **Checks and simulations are evidence.** A successful tool call is not a passing design. Report the numbers.

## Read only the reference you need

| Task | Reference |
|---|---|
| Connecting, the shell and Python client, sessions | [connect.md](references/connect.md) |
| Schematic: devices, wires, nets, the netlist view, re-wiring and rearranging, symbols and port order | [schematic.md](references/schematic.md) |
| Layout: schematic-driven devices, terminals, routes, vias, taps and guard rings, shapes, labels | [layout.md](references/layout.md) |
| Design intent: cell and view intent, ports, supplies, specs | [intent.md](references/intent.md) |
| DRC / LVS, parasitic extraction, ngspice simulation (also post-layout) | [verification.md](references/verification.md) |
| Specs, sweeps, corners, sizing against the intent | [specs.md](references/specs.md) |
| RTL, Verilator and LibreLane | [rtl.md](references/rtl.md) |
