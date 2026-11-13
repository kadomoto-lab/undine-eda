# Cells, intent and views

A cell is the unit of the design. Click a cell's name in the library browser to open its **cell page**; the small
`sch` / `sym` / `rtl` / `lay` buttons next to the name still open a view directly.

The cell page opens with the cell's name and a sentence on where it stands: which views agree, which are out of date,
and how its checks stand. Under it, from the top:

- **What it has to do**: the cell intent: goals, preferences, specs, context and notes, then what is ready and what
  is still needed per flow step (*Answer them* opens the editor), and problems. *Edit intent* opens the editor
  (ports, supplies, items, notes, YAML).
- **Views**: where you and the agent work.
  - Schematic, symbol, RTL and layout, the sources. Each shows how many view intent items it has and whether it still
    matches the schematic (a symbol whose pins differ from the schematic's ports, layout devices out of date with the
    schematic). A missing view can be created from here.
  - The cell's testbenches: schematics that place the cell and drive it (sources or simulation commands, no ports of
    their own), and testbenches a spec names. Each shows its latest simulation and whether it is current, or *not run*.
  - *Used in*: the cells that place this cell.
- **The waterline**, an aqua line. Below it, on darker ground, is what is generated from the views above:
  - the **netlist**, on demand;
  - the **checks**, the DRC / LVS results. Each is marked *outdated* when what it was generated from has changed since
    (see [Outdated results](verification.md#outdated-results)).

## Two levels of intent

The **cell intent** says *what* the cell has to achieve; it does not depend on a view. Each view has a **view
intent** that says *how* that view realises it: a matched input pair in the schematic, a common-centroid placement or
a pin edge in the layout, the load a testbench measures with. Every view item names the cell items it serves:

```
Cell intent:      g1 goal  Minimise the input offset
                  s1 spec  DC gain at least 60 dB (tb_ac)
Schematic intent: c1       M1 / M2 equal and large        serves g1
Layout intent:    c2       M1 / M2 in common centroid      serves g1
Test tb_ac:       x2       measure with the 2 pF load      serves s1
```

In each editor (schematic, symbol, RTL, layout), the **Intent** tab shows that view's intent, where you add items and
choose the cell items they serve, next to the cell intent, read-only, with the items this view serves marked;
*Edit on the cell page* opens the cell intent. A view constraint that serves no cell item is reported, so every
constraint keeps its reason. The file format is in the [design document](../design/intent.md).
