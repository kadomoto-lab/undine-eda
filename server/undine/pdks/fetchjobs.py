"""PDK fetches over HTTP: the person's confirmation token and the jobs that run `ciel fetch` (docs/design/packages.md,
UI, CLI, HTTP).

A PDK fetch runs for minutes and must not hold a request, the server's lock or a project's lock: it runs as a job on a
thread of its own, and the browser polls it. sim.Jobs does not fit: its jobs run ngspice in a process group, persist
on disk and are handed over to the next server. These are in memory only, so a server restart loses them (the ciel
run goes on to its end or is cut with the server; `pdk fetch` again, or a rescan, finds what it installed).

Only a person's click fetches. The UI asks for a token (Confirmations.issue) after the person confirmed a dialog that
shows what will be fetched; the token is single use, lives TTL seconds and is bound to exactly that, and the fetch
route takes it (Confirmations.take). No MCP tool, `undine call` command, client method or agent tool asks for one or
carries one. Undine has no authentication yet, so this stops agents working through Undine's tools, and mistakes; it is
not a defence against a hostile process running as the person. Phase 1 confirms kind 'pdk'; phase 2 adds kind 'trust'
(a person trusting a dependency's code, bound to what is installed); phase 4 adds kind 'dependency' (a dependency
fetch, bound to the project, the operation, the exact fetches and project.json with undine.lock) and kind 'index' (an
index refresh, bound to its URL). A dependency fetch runs as a job too, in a Jobs of its own (kind "dependency"), with
approximate progress (Job.report, from gitsrc.PROGRESS).
"""
from __future__ import annotations

import collections
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Hashable

TTL = 300               # seconds a confirmation token lives (read when one is issued, so tests can shorten it)
TAIL = 200              # lines of a job's output it keeps
KEEP = 50               # finished jobs kept for polling; older ones are forgotten


class Confirmations:
    """Single-use tokens, each bound to what the person confirmed ({kind, ...})."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[float, dict]] = {}       # token -> (deadline on the monotonic clock, binding)

    def issue(self, binding: dict) -> tuple[str, float]:
        """A new token for `binding` (it must have 'kind'); returns (token, expiry as a Unix time)."""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._tokens = {t: v for t, v in self._tokens.items() if v[0] > now}     # expired ones go
            self._tokens[token] = (now + TTL, dict(binding))
        return token, time.time() + TTL

    def take(self, token: str | None, kind: str) -> dict:
        """The binding of a token, which is used up by this call whatever the caller does next. PermissionError for
        no token, an unknown or used one, an expired one, or one of another kind."""
        if not token:
            raise PermissionError(f"{_what(kind)} needs the person's confirmation: no token was given")
        with self._lock:
            got = self._tokens.pop(token, None)
        if got is None:
            raise PermissionError(f"the confirmation token is unknown or was used already: {_again(kind)}")
        deadline, binding = got
        if time.monotonic() > deadline:
            raise PermissionError(f"the confirmation token has expired: {_again(kind)}")
        if binding.get("kind") != kind:
            raise PermissionError(f"the confirmation token is for {_what(binding.get('kind'))}, not {_what(kind)}")
        return binding


# what a token of each kind confirms, for take()'s messages; any other kind reads as a fetch (phase 1's wording)
_WHAT = {"trust": ("trusting a dependency", "confirm the trust again"),
         "dependency": ("a dependency fetch", "confirm the fetch again"),
         "index": ("an index refresh", "confirm the refresh again")}


def _what(kind) -> str:
    return _WHAT[kind][0] if kind in _WHAT else f"a {kind} fetch"


def _again(kind) -> str:
    return _WHAT[kind][1] if kind in _WHAT else "confirm the fetch again"


class JobFailed(Exception):
    """A job's failure with the code (and extra fields) the HTTP error of the same refusal has, for the person's
    browser to act on (code needs_fetch with the new plan, say)."""

    def __init__(self, message: str, code: str | None = None, extra: dict | None = None):
        super().__init__(message)
        self.code, self.extra = code, dict(extra or {})


_here = threading.local()


def current() -> "Job | None":
    """The job running on this thread (Jobs._run), else None."""
    return getattr(_here, "job", None)


@dataclass
class Job:
    id: str
    project: str
    kind: str = "pdk"                      # pdk | dependency
    state: str = "queued"                  # queued | running | done | failed
    log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=TAIL))
    result: dict | None = None
    error: str | None = None
    code: str | None = None                # a failure's code (JobFailed), as the HTTP error would have it
    extra: dict = field(default_factory=dict)
    progress: dict | None = None           # the last report: {url, bytes, limit} while git fetches, {url, files, of}
    created: float = field(default_factory=time.time)
    ended: float | None = None

    def say(self, line: str) -> None:
        self.log.append(line.rstrip("\n"))

    def report(self, **p) -> None:
        """Progress, approximate (a fetch counts the pack's bytes, not the files'): the last report is kept."""
        self.progress = dict(p)

    def public(self) -> dict:
        out = {"id": self.id, "project": self.project, "kind": self.kind, "state": self.state, "log": list(self.log),
               "result": self.result, "error": self.error, "code": self.code, "progress": self.progress,
               "created": self.created, "ended": self.ended}
        return out | {k: v for k, v in self.extra.items() if k not in out}


class Jobs:
    """Fetch jobs in memory. Jobs with one key (root, family, commit) run one after the other: a second fetch of the
    same PDK waits, queued, for the first (ciel would race itself in one directory). This lock only shows the wait as
    'queued' in this server; pdkops.fetch's flock (<root>/ciel/<family>/.undine-fetch-<commit>.lock) is what keeps a
    `pdk fetch` from the CLI, or another server, from running ciel at the same time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._keys: dict[Hashable, threading.Lock] = {}

    def start(self, key: Hashable, project: str, fn: Callable[[Callable[[str], None]], dict],
              kind: str = "pdk") -> Job:
        """Runs fn(say) on a thread once no other job of `key` runs; its return value is the job's result, an
        exception its error (the message only; a JobFailed's code and extra fields too). current() is the job, on its
        own thread."""
        job = Job(secrets.token_hex(8), project, kind)
        with self._lock:
            self._forget()
            self._jobs[job.id] = job
            one = self._keys.setdefault(key, threading.Lock())
        threading.Thread(target=self._run, args=(job, one, fn), name=f"{kind}-fetch-{job.id}", daemon=True).start()
        return job

    def _run(self, job: Job, one: threading.Lock, fn) -> None:
        with one:
            job.state = "running"
            _here.job = job
            try:
                job.result = fn(job.say)
                job.state = "done"
            except Exception as e:             # the person sees why in the job; the server goes on
                job.error = str(e) or type(e).__name__
                if isinstance(e, JobFailed):
                    job.code, job.extra = e.code, e.extra
                job.state = "failed"
            finally:
                _here.job = None
                job.ended = time.time()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _forget(self) -> None:
        ended = sorted((j for j in self._jobs.values() if j.ended is not None), key=lambda j: j.ended)
        for j in ended[:max(0, len(ended) - KEEP)]:
            del self._jobs[j.id]
