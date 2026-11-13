"""Spectral figures of a transient run: how an ADC or a sample-and-hold is judged (SNDR, SNR, THD, SFDR, ENOB).

The run is sampled as the circuit is clocked: a waveform at t_k = start + k / fs for k = 0 .. points-1 (linear
interpolation between the simulator's time points; a differential one as the difference of two), or an ADC's output
code built from its bit signals at those times (each bit thresholded, code = sum of bit_i * 2^i, or of the given
weights for a redundant one). The samples go through one FFT with a rectangular window, which assumes coherent
sampling: the input frequency is fin_bin * fs / points with an integer fin_bin (odd and prime to points, so the
samples do not repeat), and the signal falls in a single bin. From the bin powers, DC excluded:

* signal: the largest bin below Nyquist, or fin_bin;
* harmonics: the 2nd to the (harmonics + 1)th, aliased into the first Nyquist zone; THD is their power over the signal;
* noise: every other bin; SNR is the signal over the noise, SNDR over the noise and the harmonics;
* SFDR: the signal over the largest other bin (the spur, often a harmonic);
* ENOB = (SNDR - 1.76) / 6.02; with full_scale (the peak-to-peak range: volts, or 2^N for an N-bit code) the signal's
  level in dBFS.

Power next to the signal bin, above the noise floor, means the sampling was not coherent, or the signal is not steady
over the record (still settling): it leaks into the noise and the figures are pessimistic. The result says so in its
warnings rather than failing. Samples with no signal at all (a stuck ADC, a dead output) are a result too, the worst
one: the figures at their floor, so that the specs on them fail where the ADC stops (a corner) rather than leaving it
out of the worst case.
"""
from __future__ import annotations

import math
import re

import numpy as np

FIGURES = ("sndr_db", "snr_db", "thd_db", "sfdr_db", "enob", "signal_dbfs")
FIELDS = ("signal", "bits", "threshold", "weights", "fs", "period", "points", "start", "fin_bin", "harmonics", "full_scale")
HARMONICS = 5
MIN_POINTS, MAX_POINTS = 16, 1 << 20
FLOOR = 1e-30               # a ratio is at most 300 dB: an exact sine has no noise at all
FLOOR_DB = -10 * math.log10(FLOOR)
QUIET = 1e-20               # a signal bin below this share of the total power (DC too) is rounding error: no signal
# power next to the signal is leakage when it stands above the noise floor (x the median noise bin), is a share of the
# noise and distortion (it changes SNDR), and is above rounding (relative to the signal)
LEAK_FLOOR, LEAK_SHARE, LEAK_MIN = 10.0, 0.01, 1e-15
SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9, "t": 1e12}


# ------------------------------------------------------------------ the request


def config(spec) -> dict:
    """A spectrum request checked, numbers read (SI suffixes too): {signal: [p] or [p, n] | bits, weights, threshold,
    fs, points, start, fin_bin, harmonics, full_scale}. ValueError says what to change."""
    if not isinstance(spec, dict):
        raise ValueError("spectrum is {signal or bits, fs or period, points, start, fin_bin?, harmonics?, full_scale?}")
    if (bad := sorted(set(spec) - set(FIELDS))):
        raise ValueError(f"spectrum: unknown {', '.join(bad)}; it takes {', '.join(FIELDS)}")
    sig, bits = spec.get("signal"), spec.get("bits")
    if bool(sig) == bool(bits):
        raise ValueError("spectrum: give signal (a node, e.g. out, or [outp, outn] for outp - outn) or bits (the ADC's "
                         "output bit signals, LSB first), one of them")
    out: dict = {}
    if sig:
        sig = [sig] if isinstance(sig, str) else sig
        if not isinstance(sig, list) or len(sig) not in (1, 2) or not all(isinstance(s, str) and s.strip() for s in sig):
            raise ValueError("spectrum: signal is a node (e.g. out) or two for a differential one ([outp, outn])")
        out["signal"] = [s.strip() for s in sig]
        if (extra := [k for k in ("threshold", "weights") if spec.get(k) not in (None, "")]):
            raise ValueError(f"spectrum: {', '.join(extra)} go with bits, not with signal")
    else:
        if not isinstance(bits, list) or not 1 <= len(bits) <= 32 or not all(isinstance(b, str) and b.strip() for b in bits):
            raise ValueError("spectrum: bits is the list of the code's bit signals, LSB first, e.g. [d0, d1, ..., d7]")
        out["bits"] = [b.strip() for b in bits]
        weights = spec.get("weights")
        if weights is None:
            out["weights"] = [2.0 ** i for i in range(len(bits))]
        elif not isinstance(weights, list) or len(weights) != len(bits):
            raise ValueError(f"spectrum: weights is one number per bit, LSB first ({len(bits)} of them)")
        else:
            out["weights"] = [_num(w, "weights") for w in weights]
        out["threshold"] = None if spec.get("threshold") in (None, "") else _num(spec["threshold"], "threshold")
    fs, period = spec.get("fs"), spec.get("period")
    if (fs in (None, "")) == (period in (None, "")):
        raise ValueError("spectrum: give the sample rate fs (e.g. '10Meg') or the sample period (e.g. '100n'), one of them")
    rate = _num(fs, "fs") if fs not in (None, "") else _num(period, "period")
    if not rate > 0:
        raise ValueError("spectrum: the sample rate (or period) is positive")
    out["fs"] = rate if fs not in (None, "") else 1.0 / rate
    n = _int(spec.get("points"), "points")
    if n is None or not MIN_POINTS <= n <= MAX_POINTS:
        raise ValueError(f"spectrum: points (the FFT length, e.g. 256 or 4096) is from {MIN_POINTS} to {MAX_POINTS}")
    out["points"] = n
    out["start"] = 0.0 if spec.get("start") in (None, "") else _num(spec["start"], "start")
    if out["start"] < 0:
        raise ValueError("spectrum: start (the time of the first sample) is not negative")
    fin = _int(spec.get("fin_bin"), "fin_bin")
    if fin is not None and not 1 <= fin <= (n - 1) // 2:
        raise ValueError(f"spectrum: fin_bin is the signal's bin, from 1 to {(n - 1) // 2} (below Nyquist)")
    out["fin_bin"] = fin
    h = _int(spec.get("harmonics"), "harmonics")
    if h is not None and not 1 <= h <= 50:
        raise ValueError("spectrum: harmonics (how many count as distortion, from the 2nd) is from 1 to 50")
    out["harmonics"] = HARMONICS if h is None else h
    fsr = None if spec.get("full_scale") in (None, "") else _num(spec["full_scale"], "full_scale")
    if fsr is not None and not fsr > 0:
        raise ValueError("spectrum: full_scale (the peak-to-peak range: volts, or 2^N codes) is positive")
    out["full_scale"] = fsr
    return out


def last_sample(cfg: dict) -> float:
    return cfg["start"] + (cfg["points"] - 1) / cfg["fs"]


def check_run(cfg: dict, analysis) -> str | None:
    """Why a run with this analysis cannot give the spectrum (not a transient, or it stops before the last sample), or
    None. Without an analysis the testbench's own .control runs, and its result is checked instead."""
    if not isinstance(analysis, dict):
        return None
    if analysis.get("type", "tran") != "tran":
        return f"a spectrum is taken from a transient run, not {analysis.get('type')}: analysis {{type: tran, step, stop}}"
    last = last_sample(cfg)
    if analysis.get("stop") in (None, ""):
        return f"give the transient's stop: at least the last sample's time, {last:.6g} s (start + (points - 1) / fs)"
    try:
        stop = _num(analysis["stop"], "stop")
    except ValueError:
        return None                                       # the analysis itself is refused, with its own message
    if stop < last * (1 - 1e-9):
        return f"the transient stops at {stop:.6g} s, before the last sample at {last:.6g} s (start + (points - 1) / fs): " \
               "make stop at least that, or take fewer points"
    try:
        tstart = _num(analysis["tstart"], "tstart") if analysis.get("tstart") not in (None, "") else 0.0
    except ValueError:
        return None
    if tstart > cfg["start"] * (1 + 1e-9):              # the raw file holds the run from tstart on
        return f"the transient keeps its results from tstart {tstart:.6g} s, after the first sample at {cfg['start']:.6g} s: " \
               "make tstart at most the spectrum's start"
    return None


def _num(v, what: str, where: str = "spectrum") -> float:
    x = None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
    elif isinstance(v, str) and (m := re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*(meg|[fpnumkgt])?[a-z]*\s*",
                                                   v, re.I)):
        x = float(m.group(1)) * SI.get((m.group(2) or "").lower(), 1.0)
    if x is None or not math.isfinite(x):
        raise ValueError(f"{where}: {what} is a number (SI suffixes such as 10Meg or 100n are fine), not {v!r}")
    return x


def _int(v, what: str, where: str = "spectrum") -> int | None:
    if v in (None, ""):
        return None
    x = _num(v, what, where)
    if x != int(x):
        raise ValueError(f"{where}: {what} is a whole number, not {v!r}")
    return int(x)


# ------------------------------------------------------------------ the samples


def sequence(raw: dict, cfg: dict) -> tuple[np.ndarray, dict]:
    """The samples the FFT runs on, from a transient raw file (sim.read_raw), and what they are ({source, threshold,
    warnings}). ValueError when the run is not a transient, lacks a signal or does not cover the samples."""
    plot = raw.get("plot") or ""
    if "transient" not in plot.lower():
        raise ValueError(f"a spectrum is taken from a transient run; this one is {plot or 'not one'}: run analysis "
                         "{type: tran, ...}")
    names = [n for n, _ in raw["vars"]]
    time = np.real(raw["data"][names[0]])
    n = cfg["points"]
    t = cfg["start"] + np.arange(n) / cfg["fs"]
    tol = 1e-6 / cfg["fs"]
    if t[0] < time[0] - tol or t[-1] > time[-1] + tol:
        raise ValueError(f"the {n} samples run from {t[0]:.6g} s to {t[-1]:.6g} s, but the run covers {time[0]:.6g} s to "
                         f"{time[-1]:.6g} s: run the transient to at least {t[-1]:.6g} s (stop), or take fewer points or "
                         "an earlier start")
    t = np.clip(t, time[0], time[-1])
    lookup = {k.lower(): k for k in names[1:]}

    def wave(name: str) -> tuple[str, np.ndarray]:
        k = lookup.get(name.lower()) or lookup.get(f"v({name.lower()})")
        if k is None:
            raise ValueError(f"no signal {name} in this run; it has: "
                             + ", ".join([x for x in names[1:] if "#" not in x][:40]))
        return k, np.real(raw["data"][k])

    info: dict = {}
    if "signal" in cfg:
        waves = [wave(s) for s in cfg["signal"]]
        ys = [np.interp(t, time, y) for _, y in waves]
        x = ys[0] - ys[1] if len(ys) == 2 else ys[0]
        info["source"] = " - ".join(k for k, _ in waves)
    else:
        waves = [wave(b) for b in cfg["bits"]]
        thr = cfg["threshold"]
        if thr is None:                                   # midway between the levels the bits reach: half the supply
            thr = 0.5 * (min(float(np.min(y)) for _, y in waves) + max(float(np.max(y)) for _, y in waves))
        x = np.zeros(n)
        for w, (_, y) in zip(cfg["weights"], waves):
            x += w * (np.interp(t, time, y) > thr)
        info["source"] = f"code of {len(waves)} bits ({waves[0][0]} LSB .. {waves[-1][0]} MSB)"
        info["threshold"] = thr
    inside = int(np.count_nonzero((time >= t[0]) & (time <= t[-1])))
    if inside < n:
        info["warnings"] = [f"the run has {inside} time points over the {n} samples, so the samples are interpolated "
                            "between them: a smaller transient step (analysis step) brings the time points closer to the "
                            "samples"]
    return x, info


# ------------------------------------------------------------------ the figures


def figures(x, fs: float, fin_bin: int | None = None, harmonics: int = HARMONICS, full_scale: float | None = None) -> dict:
    """The coherent FFT's figures of a sequence (see the module docstring). Powers are mean squares in the sequence's
    unit (V^2, or codes^2), so a sine of amplitude A has signal_power A^2 / 2. No signal gives the floor (_silent)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 8:
        raise ValueError("a spectrum needs at least 8 samples")
    p = np.abs(np.fft.rfft(x)) ** 2 / n ** 2
    p[1:(n + 1) // 2] *= 2                    # one-sided: each bin below Nyquist holds its mirror image too
    top = (n - 1) // 2                        # the highest bin a signal can be in
    if fin_bin is None:
        b = 1 + int(np.argmax(p[1:top + 1]))
    elif not 1 <= fin_bin <= top:
        raise ValueError(f"fin_bin is from 1 to {top}")
    else:
        b = int(fin_bin)
    ps = float(p[b])
    if ps <= QUIET * float(np.sum(p)):        # constant samples leave rounding in the bins, exactly 0 at some lengths only
        return _silent(x, p, fs, fin_bin, full_scale)
    harm: dict[int, int] = {}
    for h in range(2, harmonics + 2):
        k = (h * b) % n
        k = min(k, n - k)                     # aliased into the first Nyquist zone
        if k not in (0, b) and k not in harm.values():
            harm[h] = k
    ph = float(sum(p[k] for k in harm.values()))
    nd = float(np.sum(p[1:])) - ps            # everything but DC and the signal
    other = p.copy()
    other[0] = other[b] = 0.0
    spur = int(np.argmax(other))
    out = {"points": n, "fs_hz": fs, "fin_bin": b, "fin_hz": b * fs / n, "range": [float(np.min(x)), float(np.max(x))],
           "signal_power": ps}
    if full_scale:
        out["signal_dbfs"] = 10 * math.log10(ps / ((full_scale / 2) ** 2 / 2))
    out.update({"sndr_db": _db(ps, nd), "snr_db": _db(ps, nd - ph), "thd_db": -_db(ps, ph),
                "sfdr_db": _db(ps, float(other[spur])), "spur_bin": spur, "spur_hz": spur * fs / n})
    if (hs := [h for h, k in harm.items() if k == spur]):
        out["spur_harmonic"] = hs[0]
    out["enob"] = (out["sndr_db"] - 1.76) / 6.02
    out["harmonics_dbc"] = {f"hd{h}": -_db(ps, float(p[k])) for h, k in harm.items()}
    warnings = []
    if other[spur] > ps:
        warnings.append(f"bin {spur} is larger than the signal's bin {b}: check fin_bin")
    near = [k for k in (b - 1, b + 1) if 1 <= k <= n // 2 and k not in harm.values()]
    rest = np.delete(p, sorted({0, b, *near, *harm.values()}))
    leak = float(sum(p[k] for k in near))
    if near and rest.size and leak > LEAK_FLOOR * len(near) * float(np.median(rest)) and leak > LEAK_SHARE * nd \
            and leak > LEAK_MIN * ps:
        k = _coherent(b, n, up=b + 1 < len(p) and p[b + 1] > p[b - 1])
        # a signal still settling (or drifting) leaks alike when sampled coherently, so fin may already be bin b's
        unsteady = "the signal is not steady over the record (still settling, or its amplitude or frequency drifting): " \
                   "start the samples later"
        coherent = (f"make fin exactly {k * fs / n:.8g} Hz, bin {k}: fin = fin_bin * fs / points with fin_bin an odd "
                    "whole number prime to points" if k != b else f"fin is not exactly {k * fs / n:.8g} Hz, bin {k}")
        warnings.append(f"the signal leaks into the bins next to it ({-_db(ps, leak):.1f} dBc), so the figures are "
                        f"pessimistic: either the sampling is not coherent ({coherent}), or "
                        + (unsteady if k != b else f"fin is that and {unsteady}"))
    if warnings:
        out["warnings"] = warnings
    return out


def _silent(x: np.ndarray, p: np.ndarray, fs: float, fin_bin: int | None, full_scale: float | None) -> dict:
    """The figures of samples with no signal (in fin_bin, when given): the worst result, not an error, so that a stuck
    ADC at one corner fails its specs there instead of dropping out of the worst case. Every ratio is at its floor."""
    n = len(x)
    largest = 1 + int(np.argmax(p[1:]))
    check = "the signal, the bits and their threshold, and when the samples are taken"
    if np.ptp(x) == 0:
        what = f"the samples are all {x[0]:.6g}"
    elif p[largest] <= QUIET * float(np.sum(p)):
        what = "the samples vary by rounding error only"
    elif fin_bin:
        what, check = f"bin {fin_bin} (fin_bin) holds nothing, the largest is bin {largest}", f"fin_bin, {check}"
    else:
        what = f"nothing below Nyquist, only in its bin {largest} (the samples alternate)"
    out = {"points": n, "fs_hz": fs, **({"fin_bin": fin_bin, "fin_hz": fin_bin * fs / n} if fin_bin else {}),
           "range": [float(np.min(x)), float(np.max(x))], "signal_power": 0.0}
    if full_scale:
        out["signal_dbfs"] = -FLOOR_DB
    out.update({"sndr_db": -FLOOR_DB, "snr_db": -FLOOR_DB, "thd_db": FLOOR_DB, "sfdr_db": -FLOOR_DB,
                "enob": (-FLOOR_DB - 1.76) / 6.02})
    out["warnings"] = [f"no signal: {what}: check {check} (a stuck ADC or a dead output reads so). The figures are at "
                       f"their worst (-{FLOOR_DB:.0f} dB, THD +{FLOOR_DB:.0f} dB) so that the specs on them fail"]
    return out


def _db(signal: float, other: float) -> float:
    return 10 * math.log10(signal / max(other, signal * FLOOR))


def _coherent(b: int, n: int, up: bool = True) -> int:
    """The bin nearest to b (above it first when `up`) that is odd and prime to n: coherent, and every sample at
    another phase of the signal."""
    for d in range(n):
        for k in ((b + d, b - d) if up else (b - d, b + d)):
            if 1 <= k <= (n - 1) // 2 and k % 2 == 1 and math.gcd(k, n) == 1:
                return k
    return b


# ------------------------------------------------------------------ both


def measure(raw: dict, cfg: dict) -> dict:
    """The figures of a spectrum request (config) on a transient raw file, rounded for reading; ValueError when the
    run cannot give them."""
    x, info = sequence(raw, cfg)
    f = figures(x, cfg["fs"], cfg["fin_bin"], cfg["harmonics"], cfg["full_scale"])
    warnings = info.pop("warnings", []) + f.pop("warnings", [])
    b, n = f.get("fin_bin"), cfg["points"]             # (none when there is no signal)
    if "bits" in cfg and b and (g := math.gcd(b, n)) > 1:
        warnings.append(f"fin_bin {b} shares the factor {g} with points {n}: the codes repeat every {n // g} samples, so "
                        "the quantization error falls into a few bins (SFDR and THD look worse, SNR better, than they "
                        f"are); use an odd fin_bin prime to points, e.g. {_coherent(b, n)}")
    out = {**info, "start_s": cfg["start"], **f}
    for k, v in out.items():
        if isinstance(v, float):
            out[k] = _r(v, 6 if k.endswith(("_hz", "_s")) or k == "threshold" else 4)
    out["range"] = [_r(v, 6) for v in out["range"]]
    if "harmonics_dbc" in out:
        out["harmonics_dbc"] = {k: _r(v) for k, v in out["harmonics_dbc"].items()}
    if warnings:
        out["warnings"] = warnings
    return out


def _r(v: float, digits: int = 4) -> float:
    return float(f"{v:.{digits}g}")
