"""Where Undine keeps what belongs to the user rather than to a project.

This module imports only os and pathlib, so that the PDK code and the bare python run by setup scripts (osdi.sh) can
import it without pulling in the rest of Undine.

* home(): $UNDINE_HOME, else ~/.undine. Connections (agents/llm.py) live here.
* store_root(): $UNDINE_STORE, else home()/store. The content-addressed store (store/cas.py): dependency files,
  their tree records, kept git repositories, PDK records and builds. Shared by every project of the user.

Both are made absolute, so a relative value means the same directory whatever the working directory is later.
"""
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("UNDINE_HOME") or Path.home() / ".undine").expanduser().absolute()


def store_root() -> Path:
    return Path(os.environ.get("UNDINE_STORE") or home() / "store").expanduser().absolute()
