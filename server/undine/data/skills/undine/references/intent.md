# Design intent

Each cell has `<cell>.intent.yaml`, on two levels.

- **Cell intent** says *what* the cell has to achieve. It holds the notes, the ports (kind, side), the supplies, and
  items. Each item has a kind, a why and a source:
  - **goal**: a direction to optimise;
  - **preference**: how to trade off;
  - **constraint**: to respect, with strength `must` (the default) or `should`, and an optional machine-checkable
    `rule`. In a testbench's own intent, `rule: {type: substitute, cells: {"lib/cell": "lib/cell"}}` gives the
    stand-ins its runs use by default (a behavioural model in place of a slow cell:
    [verification.md](verification.md#stand-ins-a-behavioural-model-in-place-of-a-cell));
  - **spec**: a measurable target, for example `{measure: dc_gain_db, min: 60, testbench: tb_ac, output: out,
    analysis: {type: ac, fstop: 1G}}` (`output` and `analysis` for AC figures; `meas` with a `.meas` statement for a
    delay or the like, without changing the testbench; a settling time `settle(v(out), tol=0.5m, from=10n)` or a
    derived measure `max(t1, t2)` as its measure; `corners`, `temps` and `conditions` where it is checked by default,
    with the limits that differ per corner: `corners: {tt: {}, ss: {max: 97e-9}}`; `substitute: {"lib/cell":
    "lib/cell"}` to measure it with a stand-in of a cell; `missing: fail` when a `.meas` that gives no value means the
    spec is not met); `check_specs` measures it ([specs.md](specs.md));
  - **context**: background.
- **View intent** (`views.<view>`) says *how* one view realises it. The views are schematic, symbol, rtl, layout,
  and `test:<testbench cell>`. Each item is linked with `serves` to the cell items it comes from.

## Read

`get_intent(lib, cell)` returns both levels. It also reports problems against the schematic and what each flow step
is missing (for example the port sides for layout). `get_design_context` already carries a summary. With many items,
`ids: ["s1", "c3"]` or `kinds: ["spec"]` returns only those (cell and view intent) and the problems about them.

## Write (inside a changeset)

- `add_intent(lib, cell, kind, text, reason, strength?, rule?, spec?, serves?, view?, why?, request?)`.
  - With `view`, the item goes to that view's intent; link it with `serves`.
  - Pass `request` with the user's words when they state a requirement.
- `update_intent(id, fields)` and `remove_intent(id)`. Changing or relaxing what a person set is a proposal: say so.
  - When the user later confirms or extends what an item says, pass their words as `fields.request`: they are added
    to the item's request history (`source.requests`, each with its date and who added it) after the words it came
    from, which are kept. A request is never replaced, so update the item's `text` or `spec` as well when the words
    change what it says.
- `set_port_intent(port, kind?, side?, why?)`.
  - kind is one of signal_in, signal_out, inout, bias_in, clock, supply, ground.
  - side is N, S, E, W or auto.
- `set_supply_intent(net, role?)` marks the supply or ground (omit role to clear).
- `set_intent_notes(text)` replaces the prose notes. Keep what is there unless it is wrong.

## What belongs in the intent

**Belongs**: requirements, priorities and decisions that should outlast the task, such as "the input pair must be
common-centroid" or "gain over 60 dB at 1 kHz".

**Does not belong**: one-off directions such as "try W = 2".

Never relax a `must` or a spec to make a check pass. Propose the change and explain the trade-off.
