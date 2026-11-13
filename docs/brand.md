# Undine — brand and palette

**Idea.** Undine is a quiet place to work: deep water at dusk, one calm aqua, light that comes from the surface.
Soft blues and whites, long calm lines, nothing shouting; it starts from the logo: an interwoven U (a canal) with a wave of aqua, drawn in deep teal.

Principles
- **One accent.** Aqua (`#72bfc6`, the logo's wave) is the only colour that means "this is Undine / this is active".
- **Blue-green neutrals, never grey.** Every surface and text colour leans toward the water, so the UI feels like one body of colour.
- **Light is warm.** Selection is the sun on the surface (`sel`), warnings are sand; danger is coral, not red.
- **Meaning, not decoration.** Lavender is reserved for the AI / agent (moonlit), ice for cross-probing.
- **Data keeps its own colours.** PDK layer colours and Xschem layers are domain data; the schematic scheme is only softened to sit in this palette.

## Palette (dark, the shipped theme)

| Token | Hex | Use |
|---|---|---|
| `--bg0` | `#050e12` | wells: logs, plots |
| `--bg` | `#071217` | app and canvas background (abyss) |
| `--bg1` | `#0c1b22` | panels, top bar |
| `--bg2` | `#11252d` | inputs, hover |
| `--bg3` | `#183039` | raised, selected |
| `--line` / `--line2` | `#1a3540` / `#2a4856` | borders |
| `--text` | `#e6f2f4` | pearl |
| `--text-soft` | `#b6cbd1` | secondary text |
| `--dim` | `#93adb5` | mist: labels |
| `--faint` | `#5d7780` | fog: hints |
| `--teal` | `#1b4c5e` | the logo's deep teal (brand fills, light surfaces) |
| `--accent` | `#72bfc6` | **aqua** — the logo's wave; primary actions, focus |
| `--accent-hi` | `#9ad8dd` | hover, emphasis |
| `--ok` | `#8fd6b5` | seafoam: pass, match |
| `--warn` | `#e6c88f` | sunlit sand: caution |
| `--err` | `#e9929a` | coral: fail, mismatch |
| `--sel` | `#f2d58a` | selection (sunlight on water) |
| `--probe` | `#9bdcf2` | ice: cross-probed objects |
| `--agent` | `#b7a9ee` | moonlit lavender: AI / agent |
| `--apricot` | `#f0b28a` | secondary data colour |
| `--mark` | `#f08a97` | DRC markers |

Plot series order: aqua, seafoam, coral, sand, lavender, ice, apricot, mint.
On light surfaces (documents, slides) use the logo colours: deep teal `#1b4c5e` for text and the mark, aqua `#72bfc6` for the wave, and `#e6f2f4` / white as the ground.

## Type and form

- **Type.** The UI sans is *Atkinson Hyperlegible Next* (`--sans`): its I, l and 1 and its O and 0 have different shapes, so net and
  instance names (`net_l1`, `vbias_I0`) cannot be misread. Japanese is *BIZ UDPGothic*, a Morisawa universal-design face chosen for
  the same reason: バ / パ, ソ / ン and ロ / 口 differ in shape. Both are web fonts, so every OS shows the same faces. Latin and digits
  always come from Atkinson. Digits that line up (tables, counters, plot read-outs) are `tabular-nums`; names keep proportional digits, so `sky130A` has no gap. Code, coordinates
  and values use *JetBrains Mono* (`--mono`). Canvas text reads the same token (`SANS()` in `web/src/theme.ts`).
- **Corners in three tiers.** Structure (sections, lists, panels, wells, plots) is square. Controls and floating surfaces (buttons,
  inputs, selects, chips, menus, dialogs, toasts) take `--radius` (4 px). Only dots and counters are round.
- **No cards.** Group by space and 1 px hairlines; a depth band (`--bg1`) marks the one region that leads, such as the intent on the
  cell page. No boxes around sections, no cards in cards, and no coloured side stripes wider than 1 px.
- **Depth is provenance ("Waterline").** What people and the agent write sits at the surface; what is generated or locked
  sits deeper (`--bg0`). The library browser puts your libraries at the surface and locked dependencies and the PDK below.
  The cell page draws one aqua line, the waterline, between the intent, source views and testbenches above and the netlist
  and checks generated from them below. It is the page's one accent line, and it does not animate.
- **Say where things stand in words.** The cell page opens with a sentence built from what the server reports (which views
  agree, which are out of date, how the checks stand), never a claim the data does not hold.
- **States are words.** A state is coloured text, with a drawn check or warning icon for ok and warn (`--i-check`, `--i-warn`), not
  a pill. A note that needs attention is a tinted band without a frame.

## Logo

Files in `web/public/brand/` (geometry is the original `undine-logo.svg`, unchanged):

| File | Use |
|---|---|
| `undine-logo.svg` | full logo on light backgrounds (deep teal + aqua) |
| `undine-logo-dark.svg` | full logo on dark backgrounds (pearl `#d4ebef` + lagoon `#5fb4bf`) |
| `undine-mark.svg`, `undine-mark-dark.svg` | the U-with-wave mark alone (icons, avatars) |
| `undine-mono.svg` | one colour (`currentColor`) for print, embossing, overlays |
| `../favicon.svg` | mark on a deep-water rounded square |

Keep clear space of one stem-width (the width of the U's left stroke) around the logo; the mark alone should not go below 16 px,
the full logo not below 20 px tall. Do not recolour the wave, stretch, or place the light-surface logo on dark.

In the app the logo is the `Logo` component (`web/src/Logo.tsx`); its colours follow `--logo-body` / `--logo-wave`.
The palette lives in `web/src/styles.css` (`:root`); canvas code reads it through `web/src/theme.ts`, and the layout tile background
is `BACKGROUND` in `server/undine/layouts/layout.py` (keep it equal to `--bg`). A rendered overview is `web/public/brand/palette.html`.
