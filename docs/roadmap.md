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

- [ ] **Phase 3: index.** An index to search for packages and add them by version, public and private; a Packages
  section in the library browser; the packages' licenses and sources in the tapeout bundle.
- [ ] **Phase 4: changesets and agents.** Dependency changes reviewed and adopted like any other change; an agent can
  propose a dependency, but only a person fetches it; results that record the package and PDK versions they were
  made with.
- [ ] **Phase 5: publishing.** Commands to check, pack and publish a package, with its check report linked from the
  index, and a report of what changed between two versions down to each cell.
- [ ] **Phase 6: PDK profiles.** Each PDK family's process knowledge as a data package that names the PDK versions it
  supports.

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
