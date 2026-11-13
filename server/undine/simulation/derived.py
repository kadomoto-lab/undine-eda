"""Derived measures: a spec's measure computed from other measures of the same run, or from its raw file.

* arithmetic over measures: `-avg_i * 1.8`, `(tr + tf) / 2`; numbers may carry SI suffixes (`0.44m`, `3n`);
* max / min / mean / abs / sqrt over measures: `max(tconv1, tconv2, tconv3)`, `min(tw_msb, tw_lsb)`;
* `settle(signal, tol, from, to, target)`: the settling time of a transient signal, read from the raw file: how long
  after `from` the signal enters ±tol around `target` and stays there until `to` (default the end of the run);
  `target` defaults to the signal's value at `to`. ngspice's .meas cannot do this: a WHEN level is fixed when the
  netlist is read, so it cannot be a measured final value.

A name in an expression is any measure a run offers (specs.measure: a .meas result, v(node), x1.xm1.gm,
v(out).max ...). ngspice's own `.meas ... param='max(a, b, ...)'` can fail ("insertnumber: fails", "Undefined
parameter"); these are evaluated by Undine after the run, and one that cannot be evaluated says why without touching
the others.
"""
from __future__ import annotations

import ast
import math
import operator
import re

import numpy as np

FUNCS = ("max", "min", "mean", "abs", "sqrt", "settle")
SETTLE_ARGS = ("tol", "start", "stop", "target")         # after the signal, in this order; from / to are start / stop
_REF = re.compile(r"\b([vi])\(([^()]*)\)", re.I)          # v(out), i(vdd), v(a,b): node names may be Python keywords
_BUS = re.compile(r"\b[A-Za-z_]\w*(?:\[[^\[\]]*\])+")      # a bus bit, d[3] or x1.xsw[2]: part of a name, not a subscript
_NUM = re.compile(r"(?<![\w.])((?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)(meg|[fpnumkgt])?([a-z]*)(?![\w.])", re.I)
_SCALE = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9, "t": 1e12}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow}


def _prepare(text: str) -> tuple[str, dict[str, str]]:
    refs: dict[str, str] = {}

    def ref(m):
        key = f"_r{len(refs)}_"
        refs[key] = m.group(0)
        return key

    def num(m):
        if not (m.group(2) or m.group(3)):
            return m.group(0)
        return f"({m.group(1)}*{_SCALE.get((m.group(2) or '').lower(), 1.0)!r})"
    src = _NUM.sub(num, _BUS.sub(ref, _REF.sub(ref, text.strip())))
    return re.sub(r"\bfrom\s*=", "start=", re.sub(r"\bto\s*=", "stop=", src)), refs


def _parse(text: str):
    src, refs = _prepare(text)
    try:
        return ast.parse(src, mode="eval").body, refs
    except SyntaxError:
        return None, refs


def _name(node, refs: dict[str, str]) -> str | None:
    """The measure a node names (a name, a v(...) / i(...), a bus bit, and attributes: x1.xm1.gm, v(out).max,
    x1.xsw[2].gm), or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join(refs.get(p, p) for p in [node.id, *reversed(parts)])


def is_derived(text) -> bool:
    """Whether a measure is an expression (arithmetic, a function) rather than one measure's name."""
    if not isinstance(text, str) or not text.strip():
        return False
    node, refs = _parse(text)
    if node is None:                     # one that does not parse is an expression when it has what a name has not
        return bool(re.search(r"[-+*/,()\s]", _prepare(text)[0]))
    return _name(node, refs) is None


def check(text: str) -> None:
    """ValueError when a derived measure cannot be evaluated (syntax, an unknown function, settle without tol)."""
    evaluate(text, lambda name: 1.0, None)


def references(text: str) -> tuple[list[str], list[str]]:
    """(the measures a derived measure reads, the signals its settle() read from the raw file)."""
    names: list[str] = []
    sigs: list[str] = []
    try:
        evaluate(text, lambda name: names.append(name) or 1.0, None, sigs)
    except ValueError:
        pass
    return list(dict.fromkeys(names)), list(dict.fromkeys(sigs))


def evaluate(text: str, lookup, trace, signals: list | None = None) -> float:
    """The value of a derived measure. `lookup(name)` gives a measure of the run (None: missing); `trace(signal)` gives
    (time, values) of a transient signal (None for a check: settle is then only parsed, its signal added to
    `signals`). ValueError says what failed."""
    node, refs = _parse(text)
    if node is None:
        raise ValueError(f"cannot read the measure {text!r}: write an expression of measure names, numbers, + - * / "
                         f"and {', '.join(FUNCS)}, e.g. max(t1, t2) or settle(v(out), tol=0.5m, from=10n)")

    def ev(n) -> float:
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            try:
                return float(_OPS[type(n.op)](a, b))
            except (ZeroDivisionError, OverflowError) as e:
                raise ValueError(f"{text}: {e}") from e
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCS:
            if n.func.id == "settle":
                return settle(n)
            if n.keywords or not n.args:
                raise ValueError(f"{n.func.id}() takes measures or numbers, e.g. {n.func.id}(t1, t2)")
            args = [ev(a) for a in n.args]
            if n.func.id in ("abs", "sqrt") and len(args) != 1:
                raise ValueError(f"{n.func.id}() takes one value")
            if n.func.id == "sqrt" and args[0] < 0:
                raise ValueError(f"{text}: sqrt of {args[0]:g}")
            return {"max": max, "min": min, "mean": lambda *a: sum(a) / len(a), "abs": abs,
                    "sqrt": math.sqrt}[n.func.id](*args)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            raise ValueError(f"no function {n.func.id}(): one of {', '.join(FUNCS)}")
        name = _name(n, refs)
        if name is None:
            raise ValueError(f"cannot read {ast.unparse(n)!r} in the measure {text!r}")
        v = lookup(name)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            raise ValueError(f"no measure {name} in this run")
        return float(v)

    def settle(n) -> float:
        if not n.args:
            raise ValueError("settle(signal, tol=..., from=..., to=..., target=...) names its signal first, e.g. "
                             "settle(v(out), tol=0.5m, from=10n)")
        sig = _name(n.args[0], refs)
        if sig is None:
            raise ValueError("settle(): the first argument is the signal, e.g. v(out)")
        if len(n.args) > 1 + len(SETTLE_ARGS):
            raise ValueError("settle() takes the signal, tol, from, to and target")
        given = dict(zip(SETTLE_ARGS, n.args[1:]))
        for k in n.keywords:
            if k.arg not in SETTLE_ARGS:
                raise ValueError(f"settle(): unknown {k.arg}; it takes tol, from, to, target")
            given[k.arg] = k.value
        if "tol" not in given:
            raise ValueError("settle() needs tol: the band around the final value (or target), e.g. tol=0.5m")
        a = {k: ev(v) for k, v in given.items()}
        if a["tol"] <= 0:
            raise ValueError("settle(): tol is positive")
        if trace is None:
            if signals is not None:
                signals.append(sig)
            return 1.0
        x, y = trace(sig)
        return settling_time(x, y, a["tol"], a.get("start"), a.get("stop"), a.get("target"), sig)

    return ev(node)


def settling_time(x: np.ndarray, y: np.ndarray, tol: float, start: float | None = None, stop: float | None = None,
                  target: float | None = None, name: str = "the signal") -> float:
    """How long after `start` y enters ±tol around `target` (default: y at `stop`) and stays there until `stop`, with
    the crossing interpolated; 0 when it is inside from `start` on. ValueError when it ends outside the band."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    t0 = float(x[0]) if start is None else float(start)
    t1 = float(x[-1]) if stop is None else float(stop)
    if not x[0] <= t0 < t1 <= x[-1] * (1 + 1e-12):
        raise ValueError(f"settle({name}): from {t0:g} s and to {t1:g} s must lie in the run ({x[0]:g} .. {x[-1]:g} s), "
                         "from before to")
    inside = (x > t0) & (x < t1)
    xs = np.concatenate([[t0], x[inside], [t1]])
    ys = np.concatenate([[np.interp(t0, x, y)], y[inside], [np.interp(t1, x, y)]])
    tgt = float(ys[-1]) if target is None else float(target)
    dev = ys - tgt
    out = np.abs(dev) > tol
    if out[-1]:
        raise ValueError(f"settle({name}): at {t1:g} s it is {dev[-1]:+.4g} from the target {tgt:.6g}, outside ±{tol:g}: "
                         "it has not settled (a later to, or a wider tol)")
    if not out.any():
        return 0.0
    i = int(np.nonzero(out)[0][-1])
    edge = tol if dev[i] > 0 else -tol
    t = xs[i] + (edge - dev[i]) / (dev[i + 1] - dev[i]) * (xs[i + 1] - xs[i])
    return float(t - t0)
