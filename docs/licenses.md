# Licenses

Undine is licensed under the [Apache License 2.0](https://github.com/kadomoto-lab/undine-eda/blob/main/LICENSE). Tools, Python packages, symbol libraries and PDKs it works
with keep their own licenses; see [Third-party licenses](licenses.md#third-party-licenses).

## Third-party licenses

Undine's own code (`server/` with its Rust core, `web/`, `scripts/`, `examples/`, `docs/`) is Apache-2.0. The repository
contains no code or data copied from the GPL projects below; it declares them as dependencies, and they are installed
from their own distributions (PyPI, conda-forge / litex-hub, klayout.de, SourceForge for ngspice's source, the OpenVAF
site).

| Component | License | How Undine uses it |
|---|---|---|
| KLayout Python module (`klayout` on PyPI) | GPL-3.0-or-later | **imported by the server** (layout database, tile rendering) |
| KLayout application | GPL-3.0-or-later | separate process (DRC / LVS decks) |
| Xschem | GPL-2.0-or-later | separate process (Xschem netlister); its symbol libraries are read at runtime; Undine reads and writes the `.sch` / `.sym` format with its own code |
| Netgen | see its distribution | separate process (LVS) |
| Magic | MIT | separate process (DRC, extraction) |
| ngspice | BSD-3-Clause (and others, see its distribution) | separate process (simulation); `scripts/ngspice.sh` builds it from its source with OSDI |
| OpenVAF 23.5.0 | GPL-3.0 | separate tool, downloaded by `scripts/osdi.sh` to compile IHP's Verilog-A models |
| Verilator | LGPL-3.0-only OR Artistic-2.0 | separate process (RTL lint and simulation) |
| LibreLane | Apache-2.0 | separate process (RTL to GDS), installed with Nix by the user |
| gdsfactory, kfactory, vlsirtools | MIT, MIT, see its distribution | gdsfactory 8 with kfactory (sky130) and gdsfactory 7.9 (GF180MCU) run in the separate PCell environments; vlsirtools is an optional netlister |
| FastAPI, uvicorn, numpy, orjson, anthropic, ciel | MIT, BSD-3-Clause, BSD-3-Clause (+ others), MPL-2.0 AND (Apache-2.0 OR MIT), Apache-2.0, Apache-2.0 | Python dependencies of the server |
| PyO3 | MIT OR Apache-2.0 | the Rust core's Python bindings |
| React, Vite, lucide-react, CodeMirror 6 (with its legacy Verilog mode), Lezer | MIT, MIT, ISC, MIT, MIT | the web UI; CodeMirror is the RTL editor |
| SkyWater SKY130 PDK (open_pdks) | Apache-2.0 | read at runtime; Tiny Tapeout's pin table (`tt-support-tools`, Apache-2.0) is in `server/undine/data/` |
| GF180MCU PDK (open_pdks), IHP Open PDK | Apache-2.0, Apache-2.0 | read at runtime; their PCell code runs in the PCell environments |
| caravel_user_project, Caravel's empty wrappers (ChipFoundry) | Apache-2.0 | not shipped: read from paths you give (the LibreLane wrapper flow, the ChipFoundry XOR) |
| gf180mcu-precheck (wafer.space), cf-precheck (ChipFoundry) | Apache-2.0 | not shipped: the tapeout targets' checks follow them, with Undine's own code |

**Distributing Undine together with KLayout.** The server imports the GPL-3.0 `klayout` module, so Undine and that
module form one combined program. Apache-2.0 is compatible with GPLv3 (not with GPLv2-only), which is why Undine's own
code can stay Apache-2.0. Whoever *distributes* the two together, for example in a container image, a VM, an installer
or a hosted appliance image, distributes the combination under GPL-3.0 and must meet its terms for it.
