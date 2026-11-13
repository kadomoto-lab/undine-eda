#!/usr/bin/env bash
# Installs Undine with pixi (pixi.toml) and checks the result. Every step can be re-run (e.g. after a pull).
#
#   ./setup.sh                  all steps: install ngspice web pdk, then check
#   ./setup.sh web              only these steps
#   ./setup.sh check            only report what is installed / missing (pixi run doctor)
#
# Steps
#   install  pixi install --all: Python, the server, Node, Xschem, Netgen, Magic, the PCell environments
#   ngspice  build ngspice 47 with OSDI into the environment (conda-forge's has no OSDI); a few minutes, once
#   web      build the UI into web/dist
#   pdk      download and enable the PDK with ciel (into $PDK_ROOT or ~/.ciel), unless it is already there
#   osdi     (not run by default) compile the Verilog-A models of installed IHP PDKs with OpenVAF, for ngspice, into
#            Undine's store ($UNDINE_STORE, else ~/.undine/store: build/<commit>/<variant>/osdi)
#
# Not installed here: pixi itself and the KLayout application (both are explained when missing).
# Settings (environment): PDK_FAMILY=sky130  PDK_VERSION=<open_pdks commit; default: the version Undine is tested with>
set -euo pipefail
cd "$(dirname "$0")"

PDK_FAMILY=${PDK_FAMILY:-sky130}
PDK_VERSION=${PDK_VERSION:-8afc8346a57fe1ab7934ba5a6056ea8b43078e71}
KLAYOUT_VERSION=0.30.12

if [ -t 1 ]; then B=$'\033[1;36m' Y=$'\033[33m' N=$'\033[0m'; else B= Y= N=; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
warn() { printf '%s!! %s%s\n' "$Y" "$*" "$N" >&2; }

if ! command -v pixi >/dev/null; then
  warn "pixi not found. Install it (one binary in ~/.pixi/bin), open a new shell, then run ./setup.sh again:"
  warn "  curl -fsSL https://pixi.sh/install.sh | sh"
  warn "(other ways: https://pixi.sh/latest/installation/)"
  exit 1
fi

do_install() {
  step "install: pixi environments (default, pcell, pcell-gf180)"
  pixi install -e default
  pixi install -e pcell       # the PDK's PCell code: gdsfactory 8 with kfactory (sky130), IHP's PyCells
  pixi install -e pcell-gf180 # GF180MCU's PCell code: gdsfactory 7.9
}

do_ngspice() {
  step "ngspice: ngspice with OSDI in the default environment"
  pixi run ngspice
}

do_osdi() {
  step "osdi: Verilog-A models of the installed PDKs (IHP) for ngspice"
  pixi run osdi
}

do_web() {
  step "web: build the UI into web/dist"
  pixi run web
}

do_pdk() {
  step "pdk: $PDK_FAMILY ${PDK_VERSION:0:12} with ciel (into ${PDK_ROOT:-$HOME/.ciel})"
  local have
  have=$(pixi run python -m undine pdks | grep "^${PDK_FAMILY}[^@ ]*@${PDK_VERSION:0:12}" || true)
  if [ -n "$have" ]; then echo "already available:"; echo "$have"; return; fi
  pixi run ciel enable --pdk-family "$PDK_FAMILY" "$PDK_VERSION"
}

klayout_hint() (  # subshell: os-release variables stay local. The official package for this system (klayout.de/build.html)
  local id="" ver="" like="" url="" pkg=""
  if [ -f /etc/os-release ]; then . /etc/os-release; id=${ID:-}; ver=${VERSION_ID%%.*}; like=${ID_LIKE:-}; fi
  case "$id" in
    ubuntu)
      pkg="klayout_${KLAYOUT_VERSION}-1_amd64.deb"; url="https://www.klayout.org/downloads/Ubuntu-${ver}/$pkg"
      warn "  curl -fLO $url"; warn "  sudo apt install ./$pkg" ;;
    *)
      if [[ " $id $like " == *" rhel "* || " $id $like " == *" centos "* || " $id $like " == *" fedora "* ]]; then
        pkg="klayout-${KLAYOUT_VERSION}-0.x86_64.rpm"
        case "$ver" in
          8) url="https://www.klayout.org/downloads/CentOS_8/$pkg" ;;
          9) url="https://www.klayout.org/downloads/RockyLinux_9/$pkg" ;;
        esac
      fi
      if [ -n "$url" ]; then warn "  curl -fLO $url"; warn "  sudo dnf install ./$pkg"
      else warn "  pick the package for your system on https://www.klayout.de/build.html (see README, Before you start)"; fi ;;
  esac
)

do_check() {
  step "check"
  if ! command -v klayout >/dev/null; then
    warn "The KLayout application (DRC / LVS decks) is not on PATH. pixi cannot provide it; install the official package:"
    klayout_hint
  fi
  if ! command -v pmap >/dev/null; then
    warn "pmap is missing (the sky130 KLayout LVS deck calls it): install procps-ng (RHEL family) or procps (Debian, Ubuntu)"
  fi
  local envf="${UNDINE_ENV:-$HOME/.undine/env}"  # the server's secrets, as scripts/restart.sh loads them
  if [ -f "$envf" ]; then set -a; . "$envf"; set +a; fi
  pixi run doctor
}

STEPS=("$@")
[ ${#STEPS[@]} -gt 0 ] || STEPS=(install ngspice web pdk)
FAILED=()
for s in "${STEPS[@]}"; do
  case $s in
    install|ngspice|web|pdk|osdi)
      set +e; (set -e; "do_$s"); rc=$?; set -e
      [ $rc -eq 0 ] || FAILED+=("$s") ;;
    check) ;;
    -h|--help|help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) warn "unknown step '$s' (install ngspice web pdk osdi check)"; exit 2 ;;
  esac
done

set +e; do_check; rc=$?; set -e
if [ ${#FAILED[@]} -gt 0 ]; then
  warn "failed: ${FAILED[*]} -- see the messages above, then run ./setup.sh ${FAILED[*]}"
  exit 1
fi
exit $rc
