# Roadmap

Undine is in alpha. Everything under [Features](overview.md#features) works today; this page lists only the work that is
planned and not built yet.

## Packages and PDKs

Libraries and PDKs shared like cargo crates or npm packages: a project asks for a version range of a library, Undine
resolves and fetches it once into a shared store, pins exactly what it got and checks it by hash; publishing is a pull
request to a git index repository, and PDKs are fetched through ciel and pinned by commit and tree hash. The design is
settled in [Packages and PDKs](design/packages.md#phases). Phase 0 (fixes to today's dependencies) and phase 1 (a
shared store that keeps each file once and lets `lib sync` work offline; PDKs pinned by full commit and tree hash and
fetched through ciel after you confirm) are done, and so is phase 2 (a package manifest, so that a library brings its
own dependencies; cell-name clashes and PDK mismatches found before anything is installed; a dependency's code blocks
and RTL used only once a person trusts it; see [Libraries and dependencies](guide/libraries.md)). Still to build:

- [x] **Phase 3: index.** Packages from an index by name and version, public and private, are done: `pkg search`,
  `pkg add`, `pkg update`, version ranges, yanked versions, several indexes
  ([Packages from an index](guide/libraries.md#packages-from-an-index)). The default index,
  [kadomoto-lab/undine-index](https://github.com/kadomoto-lab/undine-index), exists and lists no package yet. `pkg licenses`,
  `pkg vendor` and `pkg sync --from-vendor` are done, and the tapeout bundle carries the locked trees and their
  licences ([Licences and vendoring](guide/libraries.md#licences-and-vendoring)). The library browser has a Packages
  section, and a dependency row shows a newer version ([Packages in the browser](guide/libraries.md#packages-in-the-browser));
  symbol previews of a package's cells are left for later.
  From the browser, a fetch goes only to the hosts of the configured indexes and those given with
  `serve --dep-host`, and every fetch stops at `UNDINE_FETCH_MAX`.
- [ ] **Phase 4: changesets and agents.** Dependency changes reviewed and adopted like any other change; an agent can
  propose a dependency, but only a person fetches it; results that record the package and PDK versions they were
  made with. Done so far: `project.json` and `undine.lock` merge entry by entry, in `git merge` and in a changeset's
  adopt, and an adopt whose merged lock no longer pins what `project.json` asks is refused
  ([merge.md](design/merge.md)).
- [ ] **Phase 5: publishing.** Commands to check, pack and publish a package, with its check report linked from the
  index, and a report of what changed between two versions down to each cell.
- [ ] **Phase 6: PDK profiles.** Each PDK family's process knowledge as a data package that names the PDK versions it
  supports. Done so far: each family's profile is data, a `profile.toml` in a profile package per ciel family built
  into Undine (checked field by field against the Python it replaced), and `./setup.sh`, `pixi run pdk` and Undine's
  choice of PDK version read the tested PDK versions from it ([PDK profiles](guide/pdks.md#pdk-profiles)).

Later: signed index commits or package tags (until then git hashes pin the content), and DRC and LVS in the index CI
(publishing rests on the publisher's own check report until then).

## Design intent

The [design intent](design/intent.md#steps) steps that are not built yet:

- [ ] Overlays on the schematic and layout, and specs shown with their latest testbench results and staleness.
- [ ] Constraints derived for each flow step, stored with the run and traced to the intent, with overrides and agent
  proposals.
- [ ] Importing an existing LibreLane design.

## Netlists and schematics

- [ ] An importer for synthesis results and other netlists without a drawing (the [netlist view](design/netlist.md#not-yet)
  already knows how to draw them).
- [ ] A command in the schematic editor to take a pin off its net (agents have `disconnect_pins`).

## Access

- [ ] Sign-in, so that a server on the internet is safe to expose and browser chats (claude.ai, ChatGPT) can connect
  over MCP. Until then, use Claude Desktop or Claude Code ([Connect an AI agent](getting-started/connect-ai.md)).
