# Development

## Architecture

```
 Browser (React, Canvas)          Server (Python + Rust core)
 ─────────────────────            ──────────────────────────────────────────
 tiles (PNG) ◀──────────────────  KLayout LayoutView renders tiles (cached)
 selection outlines, layer list   KLayout DB: select / edit / undo / save
 schematic primitives ◀─────────  undine._core (Rust): Xschem reader / writer, view, connectivity
 user actions ─────────────────▶  Design API (FastAPI, orjson)
 agents (MCP, CLI, Python) ─────▶ the same tools, in changesets a person reviews
                                  PDK registry and process profiles (versions pinned per project)
                                  jobs: ngspice, KLayout / Magic / Netgen, Verilator, LibreLane
```

The heavy lifting is native: KLayout (C++) draws tiles and runs DRC / LVS, ngspice, Magic, Netgen, Verilator and
LibreLane run as tools, and `undine._core` (Rust, `server/src`, compiled into the server package by maturin) reads,
renders and connects Xschem schematics. Python keeps the API, editing and orchestration. On a 9,200-instance schematic
the core took opening it from 1.5 s to 0.12 s, and extracting the connectivity of 4,800 wires from 10 s to 21 ms. The
Python versions it replaced stay in the code as references (`xschem._py_parse` ... `schematic._py_extract`);
`server/tests/test_core.py` checks that both give the same results.

What is specific to a process (layer numbers, rule decks and their switches, Magic / Netgen setup, models and corners,
the PCell library and how its devices map from a schematic) is described once per PDK family in
`undine/pdks/pdkprofile.py`; the rest of the server reads it from there.

## Repository layout

```
setup.sh           sets everything up with pixi (environments, ngspice, UI, PDK), then checks
pixi.toml          environments: default (server, UI toolchain, EDA tools), pcell, pcell-gf180, docs; tasks
server/            the server: one Python package with its Rust core
  pyproject.toml   built with maturin (undine/ plus src/ into undine._core)
  Cargo.toml, src/ the Rust core: Xschem reader / writer, schematic view, connectivity
  undine/
    __main__.py    the undine command (serve, new, call, mcp-stdio, doctor ...)
    doctor.py      what is installed, what is missing
    tools.py       finds the EDA tools ($UNDINE_TOOLS, the pixi environment, PATH)
    server/        the Design API (FastAPI), live writers, idempotent requests
    store/         projects and libraries, changesets, operation log, dependencies, provenance, cell overview
    vcs/           semantic diff and three-way merge per view, the git merge driver, resolve, merge-check,
                   the git history, changesets' branches (base commits, worktrees, merges)
    pdks/          PDK discovery, process profiles (layers, decks, models, PCell libraries), PCells
    schematics/    Xschem files, netlisting, schematic and symbol editing, schematic routing, netlist view
    layouts/       the layout database and its storage, schematic-driven layout
    checks/        KLayout DRC / LVS, Magic and Netgen, shuttle targets (tapeout)
    simulation/    ngspice jobs, their results, specs at corners and sweeps
    design/        design intent, RTL (Verilator), LibreLane
    agents/        the tools agents call, MCP (HTTP and stdio), the CLI / Python client, the in-app agent and its
                   LLM gateway, pictures for agents, the Agent Skill
    data/          the Agent Skill, Tiny Tapeout pin table, extension icon
  tests/           pytest suite
web/               the UI: React + Vite (editors, panels, i18n, theme)
docs/              this documentation (MkDocs, mkdocs.yml), English and Japanese, screenshots in docs/images
scripts/           dev.sh, restart.sh, ngspice.sh and osdi.sh (setup steps), capture_screenshots.py and
                   capture_showcase.py (docs images)
examples/          scripts that build demo designs
```

Imports are absolute: `from undine.checks import verify`, `from undine.store.project import ProjectStore`.

## Local development

```sh
pixi run dev                      # API on :8700, Vite with hot reload on :5173 (proxies /api)
./scripts/restart.sh              # restart the API server in the background (log: $UNDINE_LOG, default /tmp/undine.log)
pixi run test                     # server tests (including the Rust core against its Python references)
pixi run -e docs docs-serve       # this documentation on :8000, rebuilt on save (docs-build: strict build into site/)
```

After changing the Rust core (`server/src`), `pixi install` rebuilds it into the editable server package. Which tests
need the EDA tools and the PDKs, and how to run the slow ones, is in
[Running tests](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md#running-tests).

The documentation is in English (`page.md`) and Japanese (`page.ja.md`) side by side; a page without a Japanese version shows
the English one. How to write and preview pages, and how to regenerate the screenshots, is in
[Contributing](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md#documentation).

## UI languages

The UI uses i18next/react-i18next. English and Japanese catalogs live together in `web/src/i18n/`, grouped by feature.
Add both languages in the same change. Use semantic keys (`review:actions.adopt`), not English sentences as keys.
Each catalog exports `en` and `ja` with `as const`; the shared API infers valid keys and required interpolation variables.

- Call `useI18n()` in components that display translated text. Its `t` function has stable identity: changing language must
  not restart data-loading effects, polling, jobs or editor tools. Do not include locale in component keys.
- Use `t(key, values)` for immediate display. For dialogs, validation errors, notifications and cached labels, store
  `msg(key, values)` (`UiText`) and call `resolveText()` at render time. `joinText()` and nested messages preserve this behavior.
- Translate complete phrases. Use `count` and plural keys (`_one` / `_other` for English, `_other` for Japanese), calling
  the base key. Keep design names/text, SPICE values, units, API/MCP identifiers, user/agent prose and tool logs unchanged.
- Keep default API values separate from localized labels. HTTP errors retain `detail` and add `code`/`params`; unknown
  errors remain readable as raw diagnostics. Timeline `effect_data` and verification `summary` provide structured display
  data while keeping the old fields for existing clients and archives.
- `npm test` checks untranslated JSX/accessible labels, catalog coverage, plural forms, interpolation parity, and switching
  language while a form is open. `npm run build` checks translation call-site types. Product/tool names and units have an
  explicit allowlist in `web/scripts/check-i18n.mjs`; do not add ordinary UI prose to it.

Terminology: **Agent → 設計エージェント**, **proposal → 提案**, **Adopt → 採用**, **Request changes → 修正を依頼**,
**Discard → 破棄**, **schematic → 回路図**, **layout → レイアウト**, **Restore to here → ここまで戻す**.
UI language is a per-browser preference (`undine.language`); it does not change the language of agent responses or MCP clients.

## Contributing

Issues and pull requests are welcome. How to report a bug, propose a feature, run the tests, and prepare a pull
request is in [CONTRIBUTING.md](https://github.com/kadomoto-lab/undine-eda/blob/main/CONTRIBUTING.md).
