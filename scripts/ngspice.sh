#!/usr/bin/env bash
# Builds ngspice with OSDI (Verilog-A models compiled by OpenVAF, which IHP SG13G2 needs) into the pixi environment.
# conda-forge's ngspice (41) is built without OSDI. Run through pixi (`pixi run ngspice`, part of ./setup.sh): it
# uses the environment's compilers and installs into $CONDA_PREFIX. Nothing is done when that ngspice is already
# there; FORCE=1 builds again.
set -euo pipefail

VERSION=47
SHA256=894e649651f1838a14095e5a5439e7d3aa63e87ede14d283173fda4fcdef675f
URL="https://downloads.sourceforge.net/project/ngspice/ng-spice-rework/$VERSION/ngspice-$VERSION.tar.gz"

: "${CONDA_PREFIX:?run this through pixi: pixi run ngspice}"
NG="$CONDA_PREFIX/bin/ngspice"

has_osdi() {  # the osdi command exists only in a build with OSDI
  local d out
  d=$(mktemp -d)
  printf '* osdi probe\n.control\nosdi\n.endc\n.end\n' > "$d/probe.cir"
  out=$(cd "$d" && SPICE_LIB_DIR="$CONDA_PREFIX/share/ngspice" "$NG" -b probe.cir 2>&1 || true)
  rm -rf "$d"
  ! grep -q "no such command" <<<"$out"
}

if [ -z "${FORCE:-}" ] && [ -x "$NG" ] && "$NG" -v 2>/dev/null | grep -q "ngspice-$VERSION " && has_osdi; then
  echo "ngspice $VERSION with OSDI is installed ($NG)"
  exit 0
fi

BUILD="$CONDA_PREFIX/../../build/ngspice"            # .pixi/build: kept out of the environment and the repository
mkdir -p "$BUILD"
cd "$BUILD"
if ! echo "$SHA256  ngspice-$VERSION.tar.gz" | sha256sum -c --status 2>/dev/null; then
  echo "downloading ngspice $VERSION"
  curl -fL --retry 3 -o "ngspice-$VERSION.tar.gz" "$URL"
  echo "$SHA256  ngspice-$VERSION.tar.gz" | sha256sum -c -
fi
rm -rf "ngspice-$VERSION"
tar -xzf "ngspice-$VERSION.tar.gz"
cd "ngspice-$VERSION"
mkdir -p release && cd release
# batch use and Xschem's terminal: no X11 plots; readline from the environment
../configure --prefix="$CONDA_PREFIX" --enable-osdi --enable-openmp --with-x=no --with-readline=yes --disable-debug \
  > configure.log 2>&1 || { tail -30 configure.log; exit 1; }
make -j"$(nproc)" > make.log 2>&1 || { tail -40 make.log; exit 1; }
make install > install.log 2>&1 || { tail -20 install.log; exit 1; }
"$NG" -v | head -2
has_osdi || { echo "the new ngspice has no OSDI support" >&2; exit 1; }
echo "ngspice $VERSION with OSDI installed into $CONDA_PREFIX"
