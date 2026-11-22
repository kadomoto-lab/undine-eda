"""What Undine's own tests have run on each profile: Profile.verified and Profile.magic_drc.

These are facts about Undine, not about the PDK, so they are not profile keys (profiledata refuses them): a table
keyed by ciel family, then profile package version, then profile name, of (verified, magic_drc).

- `verified` (shown as "tested by Undine"): the profile has been exercised against an installed PDK in Undine's
  tests. Not the PDK state 'verified' (its tree hashes match a profile's), which is another thing.
- `magic_drc`: run_checks runs Magic's drc(full) next to the KLayout deck (the deck lacks rules of Magic's that shuttle
  prechecks run), set once drc(full) has been run on the PDK's cells.

A profile version this table does not list (newer than this Undine, from an index) is not verified, and takes
`magic_drc` from the newest version listed for its family, so that a profile update does not turn Magic DRC off.
"""
from __future__ import annotations

TESTED: dict[str, dict[str, dict[str, tuple[bool, bool]]]] = {
    # sky130A_mr.drc has none of Magic's MV diff / tap rules (diff/tap.14-17) and checks the MIM spacings (capm.11,
    # met3.3c) otherwise: a PCell's 0.45 um butted tap on a 5 V device is clean in KLayout and diff/tap.16 in Magic
    "sky130": {"1.0.0": {"sky130": (True, True)}},
    # exercised on gf180mcuD (open_pdks 792ef670, the version wafer.space pins): KLayout DRC and LVS of
    # gf180mcu_fd_sc_mcu7t5v0__inv_1 (LVS matches), ngspice at typical / ss / ff. wafer.space's precheck runs Magic DRC
    # before KLayout's; drc(full) exercised on gf180mcuA / D: a wire too narrow (M1.1) in a cell and in the top cell,
    # clean on gf180mcu_fd_sc_mcu7t5v0__inv_1 and the nfet / pfet PCells
    "gf180mcu": {"1.0.0": {"gf180mcu": (True, True)}},
    # ihp-sg13g2 (89d42ee6): KLayout DRC of sg13g2_inv_1 (clean) and LVS against its CDL (matches), ngspice 47 with
    # the OSDI models at mos_tt / mos_ss / mos_ff; drc(full): M1.a in a cell and in the top cell, clean on sg13g2_inv_1,
    # with rules the KLayout deck's default run leaves out (M1.d, Gat.e minimum areas, LU.a / LU.b tap distances on a
    # lone PCell). SG13CMOS5L: its DRC deck runs, Magic DRC as SG13G2 (M1.a, in a test); not exercised otherwise
    "ihp-sg13": {"1.0.0": {"ihp-sg13g2": (True, True), "ihp-sg13cmos5l": (False, True)}},
}


def _key(version: str):
    from undine.store.semver import SemverError, Version
    try:
        return (1, Version.parse(version))
    except SemverError:
        return (0, version)


def flags(family: str, version: str, name: str) -> tuple[bool, bool]:
    """(verified, magic_drc) of profile `name` in version `version` of family `family`'s profile package."""
    versions = TESTED.get(family, {})
    if version in versions:
        return versions[version].get(name, (False, False))
    if not versions:
        return False, False
    newest = versions[sorted(versions, key=_key)[-1]]
    return False, newest.get(name, (False, False))[1]
