#!/usr/bin/env bash
# Compiles the Verilog-A models of the installed PDKs that need them (IHP SG13G2 / SG13CMOS5L: PSP103 and friends)
# into OSDI files ngspice loads, with the PDK's own script (libs.tech/verilog-a/openvaf-compile-va.sh), run on a copy
# of libs.tech/verilog-a: the PDK stays as installed. The files go to Undine's store, build/<commit>/<variant>/osdi
# ($UNDINE_STORE, else $UNDINE_HOME/store, by default ~/.undine/store; Pdk.osdi_dir), where Undine looks for them first
# (then in the PDK's libs.tech/ngspice/osdi, where an older setup compiled them). Models compiled before the store
# (into Undine's cache, osdi/<PDK id>: $UNDINE_CACHE, else ~/.cache/undine) are moved into it, not compiled again.
# OpenVAF 23.5.0 is downloaded once into .pixi/build/openvaf: the OpenVAF-Reloaded builds need glibc 2.39. Run through
# pixi (`pixi run osdi`, ./setup.sh osdi). PDKs whose models Undine finds are skipped; FORCE=1 compiles again. -n (or
# DRY_RUN=1) says what it would do and changes nothing.
set -euo pipefail

VERSION=23_5_0
SHA256=79c0e08ad948a7a9f460dc87be88b261bbd99b63a4038db3c64680189f44e4f0
URL="https://openva.fra1.cdn.digitaloceanspaces.com/openvaf_${VERSION}_linux_amd64.tar.gz"   # linked from openvaf.semimod.de

DRY=${DRY_RUN:-}
case "${1:-}" in
  -n|--dry-run) DRY=1 ;;
  "") ;;
  *) echo "usage: $0 [-n|--dry-run]" >&2; exit 2 ;;
esac

: "${CONDA_PREFIX:?run this through pixi: pixi run osdi}"

# the PDKs whose profile lists OSDI models: "<root> TAB <where the models go> TAB <how many Undine does not find> TAB
# <the model files, space-separated> TAB <the old cache>"; a command substitution, so that a failure stops the script
# (set -e)
found=$(python - <<'EOF'
from undine.pdks.pdk import default_pdk_roots, discover
for p in discover(default_pdk_roots()).values():
    if p.profile.osdi and (p.root / "libs.tech" / "verilog-a" / "openvaf-compile-va.sh").is_file():
        print(p.root, p.osdi_dir, sum(not f.is_file() for f in p.osdi_files()),
              " ".join(f"{m}.osdi" for m in p.profile.osdi), p.old_osdi_cache, sep="\t")
EOF
)
PDKS=()
[ -n "$found" ] && mapfile -t PDKS <<< "$found"
if [ ${#PDKS[@]} -eq 0 ]; then
  echo "no installed PDK needs compiled Verilog-A models"
  exit 0
fi

# moves file $1 into directory $2 by a rename, so that a running ngspice never loads a half-written file: across file
# systems (the old cache and the store), a copy next to the target first, then the source removed
move() {
  local tmp="$2/.$(basename "$1").$$.new"
  if [ "$(stat -c %d "$1")" = "$(stat -c %d "$2")" ]; then
    mv -f "$1" "$2/"
  else
    cp "$1" "$tmp"
    mv -f "$tmp" "$2/$(basename "$1")"
    rm -f "$1"
  fi
}

todo=()
for line in "${PDKS[@]}"; do
  IFS=$'\t' read -r root dest missing models old <<< "$line"
  # models an earlier Undine compiled into the old cache that the store does not have yet: moved, not compiled again
  for m in $models; do
    [ -f "$old/$m" ] && [ ! -f "$dest/$m" ] || continue
    if [ -n "$DRY" ]; then
      echo "$(basename "$root"): would move $m from $old into $dest"
    else
      mkdir -p "$dest"
      move "$old/$m" "$dest"
      echo "$(basename "$root"): moved $m from $old into $dest"
    fi
  done
  [ -n "$DRY" ] || rmdir "$old" 2>/dev/null || true
  if [ "$missing" = 0 ] && [ -z "${FORCE:-}" ]; then
    echo "$(basename "$root"): models already compiled"
  else
    todo+=("$line")
  fi
done
[ ${#todo[@]} -eq 0 ] && exit 0

OV="$CONDA_PREFIX/../../build/openvaf"
if [ ! -x "$OV/openvaf" ]; then
  if [ -n "$DRY" ]; then
    echo "would download OpenVAF ${VERSION//_/.} into $OV"
  else
    mkdir -p "$OV"
    echo "downloading OpenVAF ${VERSION//_/.}"
    curl -fL --retry 3 -o "$OV/openvaf.tar.gz" "$URL"
    echo "$SHA256  $OV/openvaf.tar.gz" | sha256sum -c -
    tar -xzf "$OV/openvaf.tar.gz" -C "$OV"
    rm "$OV/openvaf.tar.gz"
  fi
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
for line in "${todo[@]}"; do
  IFS=$'\t' read -r root dest missing models old <<< "$line"
  if [ -n "$DRY" ]; then
    echo "$(basename "$root"): would compile its Verilog-A models into $dest"
    continue
  fi
  echo "$(basename "$root"): compiling its Verilog-A models into $dest"
  rm -rf "$work/verilog-a" "$work/ngspice"
  cp -r "$root/libs.tech/verilog-a" "$work/verilog-a"          # the script writes ../ngspice/osdi: the copy's, not the PDK's
  chmod -R u+w "$work/verilog-a"
  (cd "$work/verilog-a" && PATH="$OV:$PATH" bash ./openvaf-compile-va.sh > /dev/null)
  # the PDK's script goes on past a model OpenVAF fails on: every model must be there, or none is taken
  for m in $models; do
    if [ ! -s "$work/ngspice/osdi/$m" ]; then
      echo "$(basename "$root"): OpenVAF did not produce $m; nothing copied into $dest" >&2
      exit 1
    fi
  done
  # each file renamed into place: a running ngspice that loaded the old one keeps it, never a half-written file
  mkdir -p "$dest"
  for m in $models; do
    cp "$work/ngspice/osdi/$m" "$dest/.$m.$$.new"
    mv -f "$dest/.$m.$$.new" "$dest/$m"
  done
  ls "$dest"
done
