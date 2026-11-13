# Contributing to Undine

Thank you for your interest in Undine. Contributions of every size are welcome: a bug report with clear steps, a fix
for a typo in the docs, a Japanese translation of a page, a new test, a new feature.

This page explains how to report a problem, propose a change, set up the code, and get a pull request merged. How the
code is organised is in [Architecture and development](docs/development.md).

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md). Security problems are reported privately, as
[SECURITY.md](SECURITY.md) says, not in a public issue.

Undine is in alpha. Interfaces, file formats and the HTTP API can still change between commits; the
[roadmap](docs/roadmap.md) says where it is going.

## Ways to contribute

- **Report a bug.** Something does not work as the docs say, a check gives a wrong result, the UI breaks. See
  [Reporting bugs](#reporting-bugs).
- **Improve the documentation.** Fix what is wrong or unclear, add an example, or translate a page into Japanese (or
  English). See [Documentation](#documentation).
- **Try Undine on a setup that has not been tested.** Undine supports SKY130, GF180MCU and IHP SG13G2, and finds tools
  installed outside pixi. Reports from other distributions, tool versions and PDK versions help.
- **Fix a bug or build a feature.** Small fixes can go straight to a pull request. For bigger changes, open an issue
  first. See [Proposing features](#proposing-features).
- **Review.** Reading open pull requests and design notes and saying what is unclear is a contribution too.

## Reporting bugs

Open an issue at <https://github.com/kadomoto-lab/undine-eda/issues>. Search the open and closed issues first: someone may
have reported it already.

A good report lets someone else see the problem on their own machine. Include:

1. **What you did**, step by step: the commands you ran, or the UI actions (which editor, which button or menu item),
   or the agent tool calls. The shortest sequence that shows the problem is best.
2. **What you expected**, and **what happened instead**: the error message, the wrong result, a screenshot of the UI.
3. **The output of the check**, which lists the versions of Python, KLayout, ngspice and the other tools, and the PDKs
   Undine found:

    ```sh
    pixi run doctor          # or ./setup.sh check, which runs it
    ```

4. **The Undine version**: the commit you run.

    ```sh
    git rev-parse --short HEAD
    ```

5. **The PDK id** the project is pinned to, as the `pdk` field of its `project.json` records it (for example
   `sky130A@8afc8346a57f`). `pixi run python -m undine pdks` lists the PDKs installed on your machine.
6. **Your system**: the Linux distribution and version (or WSL2), and the browser and its version for a UI problem.
7. **Logs**, where they apply: the server's output (the terminal that runs the server with `pixi run serve`,
   `pixi run dev` or `pixi run python -m undine serve`; `$UNDINE_LOG`, default `/tmp/undine.log`, when you started it
   with `scripts/restart.sh`; `$UNDINE_LOG`, default `~/.undine/serve.log`, when an AI app started it through
   `undine mcp-stdio`; see [Troubleshooting](docs/troubleshooting.md#start-here-run-the-check)), the browser console for a
   UI problem, and for a simulation, DRC or LVS job its folder `<project>/runs/<job>/` (`status.json` and the log). The
   [data layout](docs/reference/data.md) says where everything is.

If you can, attach a small project that shows the problem: a library with one or two cells is easier to debug than a
whole chip.

> [!WARNING]
> Issues are public. Do not attach files from a PDK under a non-disclosure agreement, design data you are not allowed
> to share, or secrets such as `~/.undine/env` (it can hold an API key). Cut the example down to what you can share.

## Proposing features

For a small, self-contained improvement (a missing option, a clearer message, a new check on an existing view), open an
issue that says what you want to do and why, or go straight to a pull request.

For a bigger change, open an issue first, so that the direction is agreed with the maintainers before you write much
code. A bigger
change is one that adds a new concept, changes a file format, the HTTP API or the tools agents call, or touches several
parts of the server at once.

Bigger changes in Undine start as a **design note** in [`docs/design/`](docs/design/). Read one or two before you write
your own, for example [live changesets](docs/design/live-changesets.md) or
[packages and PDKs](docs/design/packages.md). A design note has this shape:

- a title that ends in "(design)", for example `# Live changesets (design)`;
- a `Status:` line under it (`Status: design, for review.`, later what is implemented);
- `## Goal`: what the change is for, and what it leaves out of scope;
- `## Decisions`, once they are made: each decision with the reason for it;
- the design itself, in sections; where it is built in stages, numbered sections or a `## Steps` list say what each
  pull request does;
- the open questions (`## Open questions`) and what the note does not cover yet (`## Not covered (yet)`).

Propose the note in a pull request of its own, in English and Japanese like every docs page. Add it to the `nav` of
[`mkdocs.yml`](mkdocs.yml) under **Development** with a title that starts with "Design: " (for example
`"Design: live changesets"`), and its Japanese title under `nav_translations`. The implementation follows in later pull
requests and updates the note as it goes.

## Development setup

Undine develops on Linux x86-64 (or WSL2) with [pixi](https://pixi.sh/) and the
[KLayout application](https://www.klayout.de/build.html) 0.30+, exactly as you install it to use it. Fork the
repository, clone your fork, and run the setup:

```sh
git clone https://github.com/<your-user>/undine-eda.git
cd undine-eda
./setup.sh                  # environments, ngspice, the UI build, the sky130 PDK, then the check
```

[Install](docs/getting-started/install.md) explains each step and what to do when the check reports something missing.

To keep your own projects out of the way while you develop, point Undine at a throw-away projects root and create a
project in it. Then start the API server and the UI with hot reload, in the same shell:

```sh
export UNDINE_ROOT=/tmp/undine-dev
pixi run python -m undine new demo --pdk sky130A
pixi run dev                # API on :8700, Vite on :5173 (proxies /api)
```

Open <http://127.0.0.1:5173>. The server reads `UNDINE_ROOT` once, when it starts: set it before `pixi run dev`, or
stop and restart `pixi run dev` after you set it.

> [!NOTE]
> `pixi run dev` starts the API server on port 8700, the port an installed Undine (`pixi run serve`) also uses. Stop
> any Undine that runs on that port first.

[Architecture and development](docs/development.md) has the architecture, the repository layout, the other development
commands (restarting the server, rebuilding the Rust core) and the rules for UI text in two languages. The
[examples](examples/) build demo designs you can work on.

## Running tests

Run the tests for the parts you changed before you open a pull request.

### Server

```sh
pixi run test                                           # the whole pytest suite in server/tests
pixi run python -m pytest -q server/tests/test_core.py  # one file
pixi run python -m pytest -q server/tests -k merge      # tests whose names match
```

Some tests run the real tools (KLayout, Magic, Netgen, ngspice, Verilator) on installed PDKs. They are skipped when a
tool or a PDK is missing, so a clean run on a machine without them does not cover them: say in your pull request which
PDKs and tools you ran with. `UNDINE_SLOW_TESTS=1` adds the slow comparison of the SKY130 model libraries (about ten
minutes). LibreLane runs are not part of the suite.

Add a test with each fix and feature. Tests live in `server/tests/test_<area>.py`; a test that needs a tool or a PDK
uses `pytest.mark.skipif` with a reason that says what is missing, like the existing ones.

### Web UI

```sh
cd web
pixi run npm run build      # type-checks the TypeScript (tsc -b) and builds the UI
pixi run npm test           # the UI text check (check:i18n), then the Vitest tests
```

`pixi run web` (from the repository root) also builds the UI, after a clean `npm ci`. Component tests sit next to the
code as `*.test.ts` / `*.test.tsx`.

### Docs build

```sh
pixi run -e docs docs-build     # a strict build into site/: a broken link, page or anchor fails it
```

A link to a missing anchor (`page.md#no-such-heading`) fails the build too (`validation: anchors: warn` in
mkdocs.yml, made an error by `--strict`). A Japanese page links
to the English heading ids, so give each linked Japanese heading the English id with `<a id="..."></a>` on the line
above it, as the existing `.ja.md` pages do.

### Continuous integration

GitHub Actions builds the documentation (`mkdocs build --strict`) on changes to `docs/` and `mkdocs.yml`, and builds
and tests the UI (`npm run build`, `npm test`) on changes to `web/`. The server tests do not run in CI, because they
need the EDA tools and the PDKs: run `pixi run test` yourself.

## Code style

There is no formatter or linter to run; follow the code around your change. In particular:

- **Python** (`server/undine`): Python 3.10+ with type hints and `from __future__ import annotations`, absolute
  imports (`from undine.checks import verify`). Each module starts with a docstring that says what it is for. Keep
  lines within about 120 characters. Comments say why, not what.
- **Rust** (`server/src`): the Rust core that reads, renders and connects Xschem schematics. Its results must match the
  Python reference implementations; `server/tests/test_core.py` checks that. After changing it, run `pixi install`
  to rebuild it into the server package.
- **TypeScript** (`web/src`): React with strict TypeScript. Every string the UI shows goes through the translation
  catalogs in `web/src/i18n/`, in English and Japanese in the same change; see
  [UI languages](docs/development.md#ui-languages). Colours come from the brand palette
  ([Brand and palette](docs/brand.md)).
- **Agent tools.** When you add or change a tool agents call, update the tool list in
  [Agent and MCP](docs/guide/assistant.md) and, where it applies, the agent guide in
  `server/undine/data/skills/undine/`. `server/tests/test_skills.py` checks that every tool the guide names exists.
- **Error messages** say what is wrong and what to do about it, in plain words, like the ones `pixi run doctor` prints.

## Documentation

The documentation is a [MkDocs](https://www.mkdocs.org/) site with the Material theme, configured in
[`mkdocs.yml`](mkdocs.yml). The pages are Markdown files in [`docs/`](docs/), and they also read well on GitHub.

- **Two languages side by side.** Every page has an English `page.md` and a Japanese `page.ja.md` next to it. When you
  change a page, change the other language in the same pull request. If you cannot write one of the two languages,
  say so in the pull request, and a maintainer or another contributor will add it. A page without a Japanese version
  shows the English one.
- **Preview** the site while you write; it rebuilds on save:

    ```sh
    pixi run -e docs docs-serve     # http://127.0.0.1:8000
    ```

- **Links** between pages are relative and point to the `.md` file (`[Install](getting-started/install.md)`), so they
  work both on the site and on GitHub. A link from a docs page to a file outside `docs/` (a script, `pixi.toml`,
  `CONTRIBUTING.md`) uses its GitHub URL, `https://github.com/kadomoto-lab/undine-eda/blob/main/<path>`, because the strict
  build rejects relative links that leave `docs/`. A new page also needs an entry in the `nav` of `mkdocs.yml`, with
  its Japanese title under `nav_translations`.
- **Style.** Write plainly, in the second person and the present tense, with short sentences and headings in sentence
  case. Use the commands, UI labels and tool names exactly as the code has them. Where something is explained in the
  user guide, link to it instead of repeating it.
- **Screenshots** in `docs/images/` are captured with Playwright from a running server, at 1440 × 900 and twice the
  pixel density.
    - [`scripts/capture_screenshots.py`](scripts/capture_screenshots.py) captures the screenshots of the README and the
      guides from a throw-away demo project (`pixi run -e docs screenshots`). If your change alters the UI visibly,
      regenerate the affected ones. The script edits that project, so follow the setup in its docstring and never
      point it at your own projects root.
    - [`scripts/capture_showcase.py`](scripts/capture_showcase.py) captures the showcase images (`showcase-*.png`,
      `hero.gif`) of the [SAR ADC case study](docs/tutorials/sar-adc.md). It needs that design project, which is not
      in the repository, so a maintainer regenerates them. If your change alters what they show, say so in the pull
      request.

## Commits and pull requests

1. **Branch** from `main` with a name that says what kind of change it is: `fix/...`, `feature/...`, `docs/...`,
   `ui/...` (for example `fix/symbol-consumer-undo`, `docs/connect-ai`).
2. **Keep a pull request to one topic.** Several small pull requests are easier to review than one large one. Do not
   mix a refactoring with a behavior change.
3. **Write commit messages** the way the history does. The subject line names the area and says what is true after the
   change, in plain words:

    ```text
    Changesets: an instance an undo brings back with its old wiring is still checked at apply
    ```

    For a larger change, the body is a list of what changed and why, one item per change, wrapped at about 120
    characters:

    ```text
    LibreLane from a schematic: review fixes

    - supplies: a run from a schematic takes VDD_NETS / GND_NETS from the schematic's supply ports when the
      intent lists none
    - batch and restore snapshot flows/: a dry run or a failed batch leaves no flow file behind
    ```

4. **Before you open the pull request**, check that:
    - the server tests pass (`pixi run test`), with the PDKs and tools you have,
    - the UI builds and its tests pass (`npm run build`, `npm test` in `web/`) if you changed `web/`,
    - the documentation builds (`pixi run -e docs docs-build`) and both languages are updated if you changed `docs/`,
    - the screenshots from `scripts/capture_screenshots.py` are regenerated if the UI changed visibly.
5. **Describe the pull request**: what it changes and why, how you tested it (which PDKs and tools), and screenshots
   for a UI change. Link the issue it fixes (`Fixes #123`).
6. **Review.** A maintainer reviews every pull request. Answer review comments with new commits on the same branch;
   pull requests are merged with a merge commit, so you do not need to squash them.

## License of contributions

Undine is licensed under the [Apache License 2.0](LICENSE). Under section 5 of the License (Submission of
Contributions), a contribution you submit for inclusion (code, documentation, images, anything else) is licensed under
the Apache License 2.0 too, unless you explicitly state otherwise; this is often called "inbound = outbound". You keep
the copyright of your contribution.

Only contribute work you have the right to contribute. Do not copy code, rule decks, models or other files from a PDK or
a tool whose license does not allow it; the tools and PDKs Undine works with keep their own licenses, listed in
[Licenses](docs/licenses.md). Do not add third-party reference files (such as a shuttle's golden GDS) to the
repository either: take them as a path at run time.
