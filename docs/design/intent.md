# Design intent (design)

Status: accepted design. Step 1 is implemented (the intent file and editor, the Intent panel, the agent's context and
tools); steps 2 to 4 are not. This page settles where design intent lives, how it relates to the constraints tools
need, how people write it in the UI, and how a flow step asks for what it needs.

## Goal

Schematic, symbol and layout describe *what the circuit is*. Next to them, a designer has intent that none of these
views holds: which ports go on which side, which transistors must match, how wide a high-current net must be, what gain
the amplifier must reach, why low offset matters more than area. Digital flows keep part of this in tool files (SDC,
pin order files, macro placements); analog designers keep most of it in their heads or in comments.

Undine should hold this intent next to the cell it belongs to, in a form that people, agents and tools all read, that
changesets review like any other change, and that checks can verify. It is also what the agent works from: a person
states the intent, the agent implements it, and the checks show whether the intent is met before the person adopts the
proposal.

## Decisions

| Question | Decision |
|---|---|
| One level or two? | Two. The **cell intent** (requirements: what the cell has to achieve) is meta information on top of the cell; each view carries a **view intent** (how that view realises it), every view item linked with `serves` to the cell items it comes from. The UI shows the cell intent at the top of the cell page and, next to each editor, that view's intent with the cell intent it serves |
| Is intent another view of the cell? | No. Views are representations of one circuit and are compared with each other (LVS). Intent is a set of requirements on them and is compared with checks. It is a layer of the cell next to its views |
| Intent or constraints? | Design intent is the top concept: what the designer wants to achieve and must respect, with kinds of different force (goal, preference, constraint, spec, context). The concrete constraints a tool needs are derived from it per flow step (constraint generation), the way a netlist is derived from a schematic; they are not a second layer of the cell. Constraints a person decides directly are intent items of kind constraint |
| Where does it live? | What belongs to the cell (ports, matching, specs, notes) in the cell's intent file; what depends on how the cell is used or built (clock period of a top level, die area, corners, tool settings) in a project-level flow configuration that names a top cell |
| What format? | Structured YAML that refers to schematic objects by name (instances, nets, ports), validated against the schematic. Tool formats (SDC, LibreLane configuration, pin order files) are generated from it, not edited by hand |
| What is required? | Nothing up front. Each flow step declares what it needs; it derives its constraints from the intent where it can, and when the intent lacks what it needs, it stops, says what is missing, and the item appears in the intent panel to be filled in or explicitly left to the tool |
| Free text? | Yes: notes for the whole cell and a reason on every item. They are not checked, but the agent always receives them, and it can draft structured items from them as a proposal |
| Does the agent use it? | Always. Whatever an agent does to a cell (in the app or over MCP), it works from that cell's intent and the flow configuration, which are part of every design context it receives; it reports conflicts with the intent instead of silently overriding it. What a person's request states as a lasting requirement or priority, the agent writes into the intent, citing the request |

## 1. Views and intent

| | Views | Intent |
|---|---|---|
| Examples | schematic, symbol, layout, netlist | port sides, matching groups, net widths, specs, notes |
| Relation | different representations of the same circuit | requirements on those representations |
| Consistency | LVS, symbol check | flow checks: the placer's report, DRC / LVS, simulation measurements, STA |
| Edited by | editors, agent tools | the intent panel, selection actions, YAML, agent tools |

Intent is bound to a cell but is not drawn: it refers to the schematic's objects by name. Renaming an instance in the
schematic renames the reference, the same way the layout follows the schematic today.

## 2. Where intent lives

**Cell intent**, one file per cell in its library: `<cell>.intent.yaml`, next to `<cell>.sch` and `<cell>.sym`.
Xschem and KLayout ignore it. It travels with the cell when the library is reused as a dependency. It holds:

- **ports**: kind (signal in / out, inout, bias, clock, supply, ground) and side on the cell outline (or `auto`);
- **supplies**: which nets are power and ground (suggested from names such as `vdd`, `vss`);
- **items**, each with an id, a kind, the intent in words (`text`), an optional `reason` (why) and `source` (who added
  it, and the request it came from), and a `uid` Undine gives it when it is added: its identity in a git merge, where
  the readable id may be renumbered ([semantic diff and merge](merge.md)). It never changes; leave it as it is:

| Kind | Example | Force |
|---|---|---|
| goal | minimise the input offset | a direction to optimise |
| preference | performance over area | how to trade off |
| constraint | M1 and M2 symmetric (`rule`: symmetric, instances M1, M2) | to respect: strength `must` (default) or `should` |
| spec | DC gain ≥ 60 dB (`spec`: measure, min / max, testbench) | measured and checked |
| context | drives a 2 pF load | background for decisions |

  A constraint or preference can name the goals it `serves`, so the reason survives when the circuit changes;
- **notes**: free text for the whole cell.

**View intent**, in the same file under `views`: how one view realises the cell intent. The cell intent says *what*
the cell has to achieve (goals, preferences, specs, context; it does not depend on a view); a view intent says *how* a
view does it: the constraints and choices of the schematic, symbol, RTL, layout, or of a test (`test:<testbench cell>`).
Each view item names the cell items it comes from with `serves`, so changing a requirement shows the view intent it
affects, and a view constraint that serves nothing is reported as a constraint without a reason. Ids are unique in the
whole file. Constraints on cell-level items stay valid (older files); new ones belong to the view that has to meet them.

```yaml
items:
  - {id: g1, kind: goal, text: Minimise the input offset}
  - {id: s1, kind: spec, text: DC gain at least 60 dB, spec: {measure: dc_gain_db, min: 60, testbench: tb_ac}}
  - {id: x1, kind: context, text: Drives a 2 pF load}
views:
  schematic:
    items:
      - {id: c1, kind: constraint, text: Input pair M1 / M2 equal and large, serves: [g1],
         rule: {type: match, instances: [M1, M2]}}
  layout:
    items:
      - {id: c2, kind: constraint, text: M1 / M2 in common centroid, serves: [g1],
         rule: {type: symmetric, instances: [M1, M2], style: common_centroid}}
      - {id: c3, kind: constraint, text: Output on the right edge, height at most 20 um}   # its own: serves nothing
  test:tb_ac:
    items:
      - {id: x2, kind: context, text: Measure with the 2 pF load; phase margin below 60 deg fails, serves: [s1, x1]}
```

**Flow configuration**, in the project: `flows/<name>.yaml`, one per way of building a top cell (a Tiny Tapeout tile,
a LibreLane hardening, an analog block for a larger chip). It names its top cell and holds what depends on that use:
clock periods and I/O delays, corners, die area or utilization, power grid, and the tool settings of each step. Tool
settings that Undine does not model pass through unchanged (for example LibreLane variables).

A cell intent file:

```yaml
version: 1
notes: |
  Feeds a 10-bit ADC. Low offset matters more than area.
ports:
  inp: {kind: signal_in, side: W}
  inn: {kind: signal_in, side: W}
  out: {kind: signal_out, side: E}
  ibias: {kind: bias_in, side: auto, decided_by: user, reason: no preference}
supplies: {vdd: supply, vss: ground}
items:
  - {id: g1, kind: goal, text: Minimise the input offset, reason: feeds a 10-bit ADC,
     source: {by: agent:claude, request: "offset as small as possible", turn: t-7}}
  - {id: p1, kind: preference, text: Performance over area, source: {by: user}}
  - {id: c1, kind: constraint, text: M1 and M2 symmetric, strength: must, serves: [g1],
     rule: {type: symmetric, instances: [M1, M2], style: common_centroid}, source: {by: user}}
  - {id: s1, kind: spec, text: DC gain at least 60 dB,
     spec: {measure: dc_gain_db, min: 60, testbench: tb_ac}, source: {by: user}}
  - {id: x1, kind: context, text: Drives a 2 pF load, source: {by: user}}
```

`side: auto` with `decided_by` records the decision to let the tool choose (section 5); it is not the same as a
missing value.

## 3. Derived constraints

The constraints a tool reads are concrete and specific to it: a placer's symmetry groups and keep-outs, a router's net
widths, LibreLane's SDC and pin order file. They are derived from the intent, the design and the flow configuration
when a step runs (constraint generation), not kept as a layer of the cell:

- **Derived per step, stored with the run.** Each derived constraint records the intent items it comes from
  (`derived_from: [g1, c1]`). When the circuit changes, the step derives them again from the same intent; the old set
  stays with the old run.
- **Visible, not edited.** Like a netlist, the derived constraints of a step can be viewed (read-only, with the
  generated tool files), they are listed with the run's results (met or not), and they are drawn over the schematic
  and layout (symmetry axes, matched groups, port sides).
- **Overrides go back to the intent.** Pinning or changing a derived constraint records the decision as an intent item
  of kind constraint with the person as its source, so a later derivation keeps it.
- **Who derives.** Rules that follow directly from the intent (a symmetric rule, a port side) are derived by the step
  itself; judgements (common centroid or interdigitated for a goal of low offset) are proposed by the agent, with the
  intent items they serve, for the person to accept.

## 4. What each step needs

Each flow step declares the intent it reads, which of it is required to derive its constraints, and how the result is
checked. This one table drives three things: the message when a step stops, the missing items shown in the panel, and
the readiness chips. Implemented so far: simulation (a testbench for each spec) and layout (port kinds and sides,
supply and ground).

| Step | Required | Optional | Checked by |
|---|---|---|---|
| Simulation | testbench for each spec it measures | specs | measurements against spec limits (`check_specs`, at corners and temperatures; `sweep` explores) |
| Schematic-driven layout (device sync) | none | constraints (device variants) | LVS of devices |
| Analog placement | port sides, supplies, matched / symmetric groups (or "none") | keep-outs, guard rings | placer report: groups honoured, no overlaps, DRC |
| Analog routing | supplies, widths of nets marked critical | shielding, maximum length | DRC, LVS, extracted width per net |
| LibreLane export (digital) | clock port and period (flow configuration), supplies | pin sides and order, macros | STA (setup / hold), DRC, LVS |
| Tapeout (Tiny Tapeout) | port kinds matching the tile template | specs | the sign-off suite |

## 5. Asking for intent when a step needs it

A step starts; before running a tool it derives its constraints (section 3). Where the intent lacks what a derivation
needs:

1. The step stops and says which items are missing and why the step needs them ("analog placement needs the side of
   port `ibias`").
2. The missing items appear in the intent panel as unfilled entries marked *required by analog placement*. They are
   shown, not stored: nothing is written to the intent file until someone fills them in, so empty entries never show up
   in diffs or reviews.
3. Each one can be filled in, or explicitly left to the tool ("let the tool decide"). The latter is recorded with who
   decided and why, and the step no longer stops for it. After the run, the value the tool chose can be pinned.
4. Filling in or changing intent makes earlier results that depend on it stale, the same way schematic edits do for
   checks today.

When the agent runs the step and it stops, the agent proposes values with reasons in its changeset ("`ibias` on the
west side, next to the bias generator"; "`tail` 0.5 µm wide for 80 µA"). The person reviews and adopts them like any
other change.

## 6. Writing intent in the UI

**The cell page.** A cell is the unit: clicking its name opens its page.

- **The top.** The page opens with a sentence on where the cell stands, then the cell intent: items by kind, notes,
  readiness per step and problems. *Edit intent* opens the editor below.
- **The views.** Then come the cell's **views**:
  - the sources (schematic, symbol, RTL, layout), each with its view-intent count and whether it still matches the
    schematic;
  - the testbenches: schematics that place the cell and drive it, or that a spec names, with their latest simulation
    and whether it is current;
  - the cells it is used in.
- **Below the waterline.** An aqua line marks where what is written ends. Under it, on deeper ground, is what is
  generated: the netlist, on demand, and DRC / LVS with their freshness.

The page only shows relations the design holds (`server/undine/store/cellviews.py`); it never makes one up.

**In each editor**, the Intent tab shows that view's intent (add and edit items, choosing the cell items they serve)
next to the cell intent, read-only, with the items this view serves marked and a link to the cell page. The schematic,
symbol, RTL and layout editors have it.

The cell intent editor:

- **Readiness** at the top, per step: "Ready for simulation", "Layout: 2 missing", "LibreLane: not a target". Missing
  items do not block saving; they block only the step that needs them.
- **Sections** for ports, supplies, goals, preferences, constraints, specs, context and notes; one form adds any kind.
  Ports come from the schematic; supplies are suggested from net names.
- **From the selection**: select M1 and M2 in the schematic and choose *Symmetric*, *Match*, *Critical net* or
  *Keep-out*. No names are typed, so references are always valid.
- **Overlays**: matched groups and symmetry axes on the schematic and layout; port sides on the cell outline.
- **Specs with results**: each spec shows its testbench and the latest result (met / not met / stale).
- **Notes and reasons**: a free-text notes field for the cell, and a reason on each item. *Draft constraints from
  notes* asks the agent to turn notes into structured items, delivered as a proposal.
- **YAML**: the same content as text, for bulk edits and readable diffs; both stay in sync.
- **Severity**: red for a missing required item of a step that was asked for; yellow for a reference to something that
  no longer exists or a stale result.

## 7. The agent works from the intent

Every time an agent works on a cell, the intent is part of what it knows, not something it may look up:

- **In the context**: the design context the server builds for the agent (`<design_context>`, `get_design_context`)
  includes the cell's intent (ports, supplies, constraints, specs with their latest results, notes and reasons) and the
  flow configuration that names the cell, for in-app turns and MCP clients alike. A `get_intent` tool returns the full
  file.
- **In the work**: the agent's instructions say to satisfy the intent: place and size devices for the constraints,
  simulate the specs it affects, and read the notes as the designer's priorities ("low offset matters more than area").
  Tools that act on intent-relevant objects return the intent that applies ("M1 and M2 are a symmetric pair").
- **Conflicts are reported, not resolved silently**: when a request contradicts the intent ("make M2 smaller" for a
  matched pair), or a result misses a spec, the agent says so and proposes either the design change that keeps the
  intent or an intent change, with a reason.
- **Intent changes are proposals**: the agent can add or change intent only in a changeset, with a reason on each item;
  the review lists intent changes separately from design changes, so a person sees when the requirements, not just the
  implementation, were changed.
- **Missing intent**: when a task needs intent that is not there (section 5), the agent proposes values with reasons
  rather than guessing silently.
- **Intent from requests**: when a person's message states something that should outlast the task, a requirement
  ("it must drive 2 pF"), a constraint ("keep `ibias` on the left"), a spec ("GBW at least 10 MHz") or a priority
  ("offset matters more than area"), the agent writes it into the intent in the same changeset as its work. The item's
  source records the request (the quoted sentence and the turn), and the agent says in its reply what it added. When
  the person later confirms or extends the item, their words are added to its source (`requests`, each with its date
  and who added it) after the words it came from, never in their place: the item keeps the whole record of what was
  asked, and the panel shows it. One-off
  directions for the task at hand ("try W = 2", "move M5 down") stay out of the intent. In the review the person sees
  these items like any other intent change, and can drop them before adopting.

## 8. Changesets, history and review

The intent file is versioned like a schematic: edits go through an editor that records one effect per entry
(`{"k": "n", "id": "items.c1", "do": "add" | "del" | "mod"}`) with actor and reason, so undo / redo, adopting,
reverting and restoring a proposal work as for a schematic; an agent edits intent only in a changeset.
The review lists intent changes by item ("`ibias` side: unset → W", "match M3, M4 added") next to the design changes,
and the checks section shows which specs and constraints the proposal meets.

## 9. Tool formats

Tool files are generated from intent and flow configuration at run time, and kept with the run:

| Intent | LibreLane |
|---|---|
| flow: top cell, clock port and period | `DESIGN_NAME`, `CLOCK_PORT`, `CLOCK_PERIOD` |
| flow: I/O delays, false paths | an SDC file (`PNR_SDC_FILE`); without one, LibreLane falls back to its base SDC built from the clock settings |
| supplies | `VDD_NETS`, `GND_NETS` |
| port sides and order | a pin order file (`IO_PIN_ORDER_CFG`: `#N` / `#S` / `#E` / `#W` sections of pin-name patterns, `@min_distance`) |
| macro instances and positions | `MACROS` (per macro: views and instances with location and orientation) |
| flow: other tool settings | passed through as configuration variables, including `pdk::` / `scl::` overrides |

Importing an existing LibreLane design maps the known variables into intent and flow configuration and keeps the rest
as pass-through settings. An existing SDC is kept as a file in the flow configuration until its constraints can be
represented; it is not parsed into intent.

Tools that Undine runs itself (for example an analog placer and router, if the layout work uses one) read intent directly, with no generated file in between.

## Open questions

- **Hierarchy**: a constraint in a subcell versus the same constraint from the parent's context (a parent wants two
  instances of a subcell matched). Likely: constraints on instances belong to the cell that contains the instances.
- **Digital cells**: a cell with an RTL view (Verilog) and no schematic has ports from the RTL; references then validate
  against the RTL's ports and nets.
- **Schematic attributes**: Xschem already has net and instance attributes. Undine should not scatter intent into them;
  whether to show them in the panel read-only is open.
- **Spec measurements** (decided): a measure is a `.meas` name of the testbench, one of `run_simulation`'s AC figures
  (the spec names `output`, and `analysis` unless the testbench runs one), `v(node)`, `<device>.<quantity>`,
  `<signal>.<statistic>`, a spectral figure of a transient (`sndr_db`, `snr_db`, `thd_db`, `sfdr_db`, `enob`,
  `signal_dbfs`; the spec's `spectrum` says how to sample the run: a node or an ADC's bits, `fs`, `points`, `start`;
  a testbench that gives the figure itself needs none, and the intent then does not warn about a missing `spectrum`:
  a `.meas` of that name outside `.control`, or a `.control` that prints it or measures it with `meas` after
  `echo Measurements for ...`; a spec with its own `analysis` drops the testbench's `.control`, so only such a `.meas`
  of that analysis counts there),
  the result of the spec's own `meas` (`.meas` statements added to its runs only), a settling time read from the raw
  file (`settle(v(out), tol=0.5m, from=10n)`), or a derived measure over the others (`max(t1, t2)`, `-iavg * 1.8`);
  see `server/undine/simulation/specs.py`, `derived.py` and `spectrum.py`.
- **No value** (decided): a run whose `.meas` gives no value (a level never crossed) leaves the spec not measured
  there, since a value it never had cannot be compared with a limit. Some requirements are exactly that event ("the
  DAC controls cleared after clk rose": a control that never clears gives no value), so a spec may say `missing: fail`:
  a `.meas` that ran and never triggered (a level never crossed, ngspice's "out of interval", or a `param=` of such a
  `.meas`) then means not met there, reported with the reason "no value", each set of points with its own reason. A
  `.meas` that could not run (a node the run does not have, a measure no `.meas` gives, an analysis that does not
  run) stays not measured: the spec is wrong, not the design.
- **Where a spec is checked** (decided): a spec may carry `corners`, `temps` and `conditions`, used when check_specs or
  sweep is not told where to check it. `corners` and `temps` are a list, or a mapping to the limits that differ there
  (`{tt: {}, ss: {max: 97n}}`: checked at tt and ss, with 97 ns at ss; a temperature's limit over its corner's), so
  one requirement with a relaxed slow corner stays one spec. Still open: where project-level corner and temperature
  sets live (the flow configuration), and showing the latest results in the intent panel.
- **Stand-ins** (decided): a top level places its real cells (layout, LVS and extraction need them), but some of its
  measurements are only practical with a behavioural model of a slow block, for example an ADC's SNDR from a long
  transient with an event-driven model of its SAR controller. A spec's `substitute` ({lib/cell: lib/cell}) netlists
  every instance of a cell, at any depth below its testbench, as a stand-in with the same pins in the same order, so a
  plain `check_specs` reproduces the number and its report names the stand-in. A testbench's own intent gives the
  stand-ins all its runs use by default, as a constraint `rule: {type: substitute, cells: {lib/cell: lib/cell}}`: the
  simulation step reads it like any rule, and it has a why. The call's stand-ins go over the spec's, the spec's over
  the testbench's, and null takes one back. They are not schematic attributes (see above) nor a configuration view
  of their own: a stand-in is a decision about how a measurement is made, so it lives with that measurement. The
  stand-ins are part of a run's inputs (its testbench netlist entry), so a result with them never stands in for one
  without. See [Stand-ins](../guide/verification.md#stand-ins).

## Steps

| Step | Content |
|---|---|
| 1b (done) | Cell intent and view intent (views in the file, links with serves, checks for missing and dangling links), the cell page (intent on top; design, test, used-in and derived views with freshness), the view's intent in each editor |
| 1 (done) | Cell intent file and editor (entry effects, undo, changesets, review), Intent panel (readiness, ports, supplies, items by kind, notes, YAML), intent in the agent's design context, intent tools, rules in the agent's instructions |
| 2 | Overlays on the schematic and layout, specs with their latest testbench results and staleness (constraints from the selection are in step 1) |
| 3 | Derived constraints per step (stored with the run, traced to intent, overlays, overrides to intent), stop and ask in a step, agent proposals for derivations |
| 4 (partly done) | Flow configuration and LibreLane runs with derived settings traced to their sources (done); importing an existing LibreLane design (not yet) |
