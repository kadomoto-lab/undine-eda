"""Pinning the real installed sky130A and IHP SG13G2 (skipped where they are not installed): each hashes, verifies
against its own pin, and answers a second time from the stamp cache. Read-only: the PDK trees are only read, and the
cache goes to the test's own store (fresh_store), never ~/.undine."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.home import store_root  # noqa: E402
from undine.pdks import pin  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, installs  # noqa: E402

INSTALLS = installs(default_pdk_roots())


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.mark.parametrize("variant", ["sky130A", "ihp-sg13g2"])
def test_an_installed_pdk_hashes_and_verifies_against_itself(variant, fresh_store, monkeypatch):
    pdk = next((p for p in INSTALLS if p.variant == variant), None)
    if pdk is None:
        pytest.skip(f"{variant} is not installed")
    root = pdk.root.resolve()
    before = {p: p.lstat().st_mtime_ns for p in root.rglob("*")}
    lock = pin.pin_object(pdk)
    assert lock["variant"] == variant and lock["tree"]
    assert "libs.tech" in lock["tree"] and all(f"libs.ref/{lib}" in lock["tree"] for lib in lock["libraries"])
    assert pin.cache_path(pdk).parent == store_root() / "pdk"
    assert str(store_root()).startswith(str(fresh_store))

    reads = []
    real = pin._sha256_file
    monkeypatch.setattr(pin, "_sha256_file", lambda p: reads.append(p) or real(p))
    r = pin.verify(pdk, lock)
    assert (r["state"], r["changed"], r["missing_libraries"]) == ("unverified", [], [])
    assert pin.verify(pdk, lock, only_cached=True)["state"] == "unverified"
    assert reads == []                                   # the second time, from the stamps alone
    assert {p: p.lstat().st_mtime_ns for p in root.rglob("*")} == before
