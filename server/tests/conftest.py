"""Changesets are git branches: every test that opens one runs git. It runs without the user's or the system's
configuration (an identity, hooks or ignore rules of theirs must not change what a test sees); Undine gives the
identity of its own commits itself (vcs/branches.py). The reduced model libraries simulations make
(simulation/modellib.py) go to a cache of the test session's own, not the user's, and so do Undine's home and its
content-addressed store (UNDINE_HOME, UNDINE_STORE): no test reads or writes ~/.undine. A test that looks at the store
takes `fresh_store`, a store (and home) of its own."""
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _git_without_user_config(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True, scope="session")
def _model_cache_of_the_session(tmp_path_factory):
    old = os.environ.get("UNDINE_CACHE")
    os.environ["UNDINE_CACHE"] = str(tmp_path_factory.mktemp("cache"))
    yield
    if old is None:
        os.environ.pop("UNDINE_CACHE", None)
    else:
        os.environ["UNDINE_CACHE"] = old


@pytest.fixture(autouse=True, scope="session")
def _home_and_store_of_the_session(tmp_path_factory):
    old = {k: os.environ.get(k) for k in ("UNDINE_HOME", "UNDINE_STORE")}
    home = tmp_path_factory.mktemp("home")
    os.environ["UNDINE_HOME"] = str(home)
    os.environ["UNDINE_STORE"] = str(home / "store")
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def fresh_store(tmp_path_factory, monkeypatch) -> Path:
    """A store (and an UNDINE_HOME) of this test's own, empty; returns the store's root. Through monkeypatch, so it
    holds even after a test that set UNDINE_HOME in os.environ by hand (test_llm_connections.py does)."""
    home = tmp_path_factory.mktemp("fresh-home")
    monkeypatch.setenv("UNDINE_HOME", str(home))
    monkeypatch.setenv("UNDINE_STORE", str(home / "store"))
    return home / "store"
