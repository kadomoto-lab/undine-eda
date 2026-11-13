"""Assistant turns as jobs: a chat request is accepted at once and runs in the background (docs/design/live-changesets.md).

One JSON file per turn: <project>/llm/turns/<id>.json
    {"id", "conversation", "client_id", "state", "created", "ended", "changeset",
     "events": [{"i", "t", "type": "text" | "tool" | "changeset" | "state", ...}],
     "reply", "actions", "usage", "stop", "error"}
States: running -> done | failed; stopping -> stopped; interrupted (the server ended while it ran).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

ACTIVE = ("running", "stopping")
FINAL = ("done", "failed", "stopped", "interrupted")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Turns:
    def __init__(self):
        self.live: dict[str, dict] = {}            # id -> the record of a turn running in this process
        self.stop_events: dict[str, threading.Event] = {}
        self.lock = threading.RLock()

    @staticmethod
    def _dir(project_path: Path) -> Path:
        return project_path / "llm" / "turns"

    def _write(self, project_path: Path, rec: dict) -> None:
        d = self._dir(project_path)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{rec['id']}.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False))
        os.replace(tmp, d / f"{rec['id']}.json")

    def get(self, project_path: Path, tid: str) -> dict:
        with self.lock:
            if tid in self.live:
                return json.loads(json.dumps(self.live[tid]))
        p = self._dir(project_path) / f"{tid}.json"
        if not p.is_file():
            raise KeyError(f"no turn {tid}")
        rec = json.loads(p.read_text())
        if rec["state"] in ACTIVE:                 # a former server process ran it
            rec["state"], rec["error"] = "interrupted", "the server stopped while this turn ran; its committed changes are kept"
        return rec

    def find(self, project_path: Path, conversation: str, client_id: str | None) -> dict | None:
        """The turn already accepted for this request (a retried POST must not start a second one)."""
        if not client_id:
            return None
        d = self._dir(project_path)
        for p in sorted(d.glob("t-*.json")) if d.is_dir() else []:
            rec = json.loads(p.read_text())
            if rec.get("conversation") == conversation and rec.get("client_id") == client_id:
                return self.get(project_path, rec["id"])
        return None

    def running(self, conversation: str) -> dict | None:
        with self.lock:
            return next((r for r in self.live.values() if r["conversation"] == conversation), None)

    def create(self, project_path: Path, conversation: str, client_id: str | None, changeset: str | None) -> dict:
        rec = {"id": "t-" + uuid.uuid4().hex[:10], "conversation": conversation, "client_id": client_id,
               "state": "running", "created": _now(), "ended": None, "changeset": changeset, "events": [],
               "reply": None, "actions": [], "usage": None, "stop": None, "error": None}
        with self.lock:
            self.live[rec["id"]] = rec
            self.stop_events[rec["id"]] = threading.Event()
            self._write(project_path, rec)
        return rec

    def event(self, project_path: Path, tid: str, ev: dict) -> None:
        with self.lock:
            rec = self.live[tid]
            rec["events"].append({"i": len(rec["events"]), "t": _now(), **ev})
            if ev.get("type") == "changeset":
                rec["changeset"] = ev["id"]
            self._write(project_path, rec)

    def stop(self, project_path: Path, tid: str) -> dict:
        with self.lock:
            rec = self.live.get(tid)
            if rec and rec["state"] == "running":
                rec["state"] = "stopping"
                self.stop_events[tid].set()
                rec["events"].append({"i": len(rec["events"]), "t": _now(), "type": "state", "state": "stopping"})
                self._write(project_path, rec)
        return self.get(project_path, tid)

    def stopping(self, tid: str) -> bool:
        ev = self.stop_events.get(tid)
        return bool(ev and ev.is_set())

    def finish(self, project_path: Path, tid: str, **fields) -> dict:
        with self.lock:
            rec = self.live.pop(tid)
            self.stop_events.pop(tid, None)
            rec.update(fields)
            if rec["state"] in ACTIVE:
                # a stop request that came after the last step does not make a finished turn "stopped"
                rec["state"] = "stopped" if fields.get("stop") == "stopped" else ("failed" if rec.get("error") else "done")
            rec["ended"] = _now()
            rec["events"].append({"i": len(rec["events"]), "t": rec["ended"], "type": "state", "state": rec["state"]})
            self._write(project_path, rec)
            return rec

    @staticmethod
    def since(rec: dict, after: int) -> dict:
        """The record with only the events after index `after` (what a polling client has not seen)."""
        return {**rec, "events": [e for e in rec["events"] if e["i"] >= after]}
