# Install

## 1. Before you start

| You need | Check | Get it |
|---|---|---|
| Linux x86-64 (or WSL2) with glibc 2.28+ | `ldd --version` | Ubuntu 20.04+, RHEL / Rocky / AlmaLinux 8 or 9; see [per distribution](#per-distribution) |
| git, curl | `git --version` | `sudo apt install git curl` / `sudo dnf install git curl` |
| [pixi](https://pixi.sh/) | `pixi --version` | the installer below (one binary in `~/.pixi/bin`), then open a new shell |
| KLayout application 0.30+ (DRC / LVS) | `klayout -v` | the official package for your distribution, [below](#per-distribution); `./setup.sh check` prints the command |
| ~5 GB of disk | | mostly the PDK and the EDA tools |

```sh
curl -fsSL https://pixi.sh/install.sh | sh     # pixi
```

pixi provides everything else from [`pixi.toml`](https://github.com/kadomoto-lab/undine-eda/blob/main/pixi.toml) (one lock file, [`pixi.lock`](https://github.com/kadomoto-lab/undine-eda/blob/main/pixi.lock)): Python, the server, Node.js
for the UI, Xschem, Netgen, Magic, Verilator, vlsirtools, [ciel](https://github.com/fossi-foundation/ciel) for the PDKs, two
PCell environments (gdsfactory 8.x on kfactory for sky130 and IHP's PyCells, gdsfactory 7.9 for GF180MCU), and the Rust
toolchain that builds the server's native core (`server/src`, compiled into the server package). No system Python,
Node, Rust or conda is needed, and these are the same on every distribution. ngspice is built from source inside the pixi
environment by `setup.sh` (step `ngspice`, with pixi's own C compiler): conda-forge's ngspice has no OSDI, which the IHP
models need. The KLayout
application is the one exception: its conda packages no longer install from conda-forge, so it comes from klayout.de as a system
package (the server itself uses the `klayout` Python module from PyPI, through pixi).

### Per distribution

The KLayout application from [klayout.de/build.html](https://www.klayout.de/build.html) (0.30.12, the version of the Python
module in `pixi.lock`). `apt` / `dnf` install its dependencies from the distribution's standard repositories; no EPEL is needed.

| Distribution | KLayout package | Install | Dependencies it pulls in |
|---|---|---|---|
| Ubuntu 22.04 | [Ubuntu-22 .deb](https://www.klayout.org/downloads/Ubuntu-22/klayout_0.30.12-1_amd64.deb) | `sudo apt install ./klayout_0.30.12-1_amd64.deb` | Qt 5.15, Ruby 3.0, Python 3.10, libgit2 1.1 |
| Ubuntu 24.04 | [Ubuntu-24 .deb](https://www.klayout.org/downloads/Ubuntu-24/klayout_0.30.12-1_amd64.deb) | `sudo apt install ./klayout_0.30.12-1_amd64.deb` | Qt 5.15, Ruby 3.2, Python 3.12, libgit2 1.7 |
| Ubuntu 20.04 | `Ubuntu-20` .deb (same file name) | as above | Qt 5, Ruby, Python 3 of that release |
| RHEL / Rocky / AlmaLinux 9 | [RockyLinux_9 .rpm](https://www.klayout.org/downloads/RockyLinux_9/klayout-0.30.12-0.x86_64.rpm) | `sudo dnf install ./klayout-0.30.12-0.x86_64.rpm` | qt5 (base, multimedia, svg, tools, xmlpatterns) 5.15, Ruby 3.0, Python 3.9, http-parser |
| RHEL / Rocky / AlmaLinux 8 | [CentOS_8 .rpm](https://www.klayout.org/downloads/CentOS_8/klayout-0.30.12-0.x86_64.rpm) | `sudo dnf install ./klayout-0.30.12-0.x86_64.rpm` | qt5 (as above), Ruby 2.5, Python 3.6, libgit2 |
| CentOS 7 | | not supported: glibc 2.17 is older than the pixi environment needs (Node.js for the UI needs 2.28), and CentOS 7 reached end of life in 2024 | |

Also needed on every distribution: **`pmap`** (package `procps` on Debian / Ubuntu, `procps-ng` on the RHEL family). The sky130
KLayout LVS deck calls it to log memory use and fails without it. It is part of any normal installation but missing from minimal
container images.

Tested: Ubuntu 22.04 (WSL2), Ubuntu 24.04, Rocky Linux 8 and 9 (containers): `./setup.sh`, the demo designs, simulation and the Tiny Tapeout
sign-off suite (KLayout DRC / LVS, Magic DRC, Magic + Netgen LVS). The other rows follow the same packages but have not been run.

## 2. Install

```sh
git clone https://github.com/kadomoto-lab/undine-eda.git
cd undine-eda
./setup.sh
```

`setup.sh` runs these steps and can be re-run at any time (e.g. after a `git pull`); give step names to run only some of them
(`./setup.sh web`, `./setup.sh --help`):

| Step | Same as | What it does |
|---|---|---|
| `install` | `pixi install -e default`, `-e pcell` and `-e pcell-gf180` | the default environment (server, Node, EDA tools) and the PCell environments: gdsfactory 8 for sky130 (and IHP's PyCells), gdsfactory 7.9 for GF180MCU (the `docs` environment is only for this documentation and its screenshots) |
| `ngspice` | `pixi run ngspice` | builds ngspice 47 with OSDI into the default environment (a few minutes, once; skipped when it is there) |
| `web` | `pixi run web` | builds the UI into `web/dist` |
| `pdk` | close to `pixi run pdk` ([differences](../guide/pdks.md#sky130)) | downloads and enables the SKY130 PDK with ciel (into `$PDK_ROOT`, default `~/.ciel`), pinned to the version Undine is tested with; skipped when that version is already there |
| `osdi` | `pixi run osdi` | not run by default: compiles the Verilog-A models of installed IHP PDKs with OpenVAF, for ngspice (see [IHP SG13G2 Verilog-A models](../guide/pdks.md#ihp-sg13g2-verilog-a-models)) |
| `check` | `pixi run doctor` | runs only the check (below); every run of `setup.sh` ends with it |

!!! note "Upgrading from an older Undine"

    A project records the tool versions it was made with in its `undine.lock`. After an update to ngspice 47, its
    dependency check warns `ngspice: locked 41, here 47 from pixi (…)` (a warning, not a lock issue). Once you have
    checked its results,
    record the new version with `pixi run python -m undine lib lock PROJECT` (see [undine.lock](../guide/libraries.md#undinelock)).

### Other PDKs

GF180MCU, IHP SG13G2 and other versions of SKY130 are installed with ciel too. [PDKs](../guide/pdks.md) lists the
tested versions and what works on each, [installs them](../guide/pdks.md#installing-a-pdk), and explains how a project
is pinned to its PDK and how to move it to another version.

## 3. Check and fix

`setup.sh` ends with a check; run it alone with `./setup.sh check` (or `pixi run doctor`). Each line says what was found
and, when something is missing, the command that fixes it:

```
 ✓ klayout Python module >= 0.30          ok  0.30.12
 ✓ a PDK                                  ok  sky130A@8afc8346a57f, sky130B@8afc8346a57f
 - klayout (DRC / LVS)                    missing  [recommended]  -> install the KLayout application (>= 0.30) ...
```

`required` items are needed to start; without a `recommended` one a feature is off (simulation, DRC/LVS, PCells). Inside `pixi run`
(and in a server the MCP launcher starts from it) the tools come from the pixi environment (`$UNDINE_TOOLS`; unset, from the server's own
environment, then `~/undine-tools`), everything else from `PATH`, so tools installed another way (apt, Homebrew, IIC-OSIC-TOOLS,
source builds) work too. PDKs are found in `$PDK_ROOT`, `~/.ciel` and `~/.volare`
([Where Undine looks for PDKs](../guide/pdks.md#where-undine-looks-for-pdks)).

## Without pixi

Python 3.10+ and Node 18+ from the system:

```sh
python3 -m venv .venv
.venv/bin/pip install -e "server[pdk,dev]"     # server with its Rust core (needs cargo, e.g. rustup), ciel, pytest
(cd web && npm ci && npm run build)                                      # UI
.venv/bin/ciel enable --pdk-family sky130 8afc8346a57fe1ab7934ba5a6056ea8b43078e71
```

Put ngspice (built with `--enable-osdi` for IHP), Xschem, Netgen (the LVS tool) and Magic on `PATH` (or in `$UNDINE_TOOLS/bin`). For PCells, create a Python 3.12
environment with the packages of `[feature.pcell.pypi-dependencies]` in `pixi.toml` (gdsfactory 8.32.2, which brings kfactory
0.23) and point `UNDINE_PCELL_PYTHON` to its `python`. For GF180MCU PCells, a second one with
`[feature.pcell-gf180.pypi-dependencies]` (gdsfactory 7.9.4), pointed to by `UNDINE_PCELL_GF180_PYTHON`. `vlsirtools` (optional netlister): `pip install vlsirtools`.

## Next steps

- [First project](first-project.md): create a project and take a tour of the window.
- [Connect an AI agent](connect-ai.md): let Claude or another AI app work in Undine.
- If the check reports a problem, see [Troubleshooting and FAQ](../troubleshooting.md).
