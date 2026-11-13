"""Spectral figures of a transient (spectrum.py; run_simulation's spectrum, check_specs' sndr_db ...): a coherent FFT of
a sampled waveform or of an ADC's code built from its bits, against signals of known SNDR, THD and SFDR."""
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.design import intent  # noqa: E402
from undine.simulation import specs, spectrum  # noqa: E402
from test_simresults import _need, _project, _tb  # noqa: E402


def _raw(t, plot="Transient Analysis", **waves):
    return {"plot": plot, "vars": [("time", "time")] + [(f"v({k})", "voltage") for k in waves],
            "data": {"time": np.asarray(t, float), **{f"v({k})": np.asarray(v, float) for k, v in waves.items()}}}


def _stuck(code=127):
    """An 8-bit SAR that never resolves: its bits hold one code for 1.1 ms (0.5 us steps); sampled 1000 times at 1 MHz."""
    t = np.linspace(0, 1.1e-3, 2201)
    raw = _raw(t, **{f"d{i}": np.full(t.size, 1.8 * ((code >> i) & 1)) for i in range(8)})
    return raw, spectrum.config({"bits": [f"d{i}" for i in range(8)], "fs": "1Meg", "points": 1000, "start": "0.5u",
                                 "full_scale": 256})


def _adc(x, bits):
    """An ideal ADC over -1 .. 1: the code of each sample."""
    return np.clip(np.floor((x + 1) / 2 * 2 ** bits), 0, 2 ** bits - 1).astype(int)


def test_a_sine_with_known_noise_and_harmonics():
    n, fs, b = 4096, 1e6, 101
    w = 2 * np.pi * b * np.arange(n) / n
    noise = np.random.default_rng(1).normal(0, 1e-3, n)                    # 1e-6 V^2
    x = 0.5 + np.sin(w) + 1e-3 * np.sin(3 * w + 0.3) + noise                   # DC is not noise; HD3 -60 dBc
    f = spectrum.figures(x, fs)
    ps, pn, ph = 0.5, 1e-6, 0.5e-6
    assert f["fin_bin"] == b and f["fin_hz"] == pytest.approx(b * fs / n) and f["signal_power"] == pytest.approx(ps, rel=1e-3)
    assert f["snr_db"] == pytest.approx(10 * math.log10(ps / pn), abs=0.2)
    assert f["sndr_db"] == pytest.approx(10 * math.log10(ps / (pn + ph)), abs=0.2)
    # HD3's bin holds some noise too: a few tenths of a dB at -60 dBc
    assert f["thd_db"] == pytest.approx(-60, abs=0.3) and f["harmonics_dbc"]["hd3"] == pytest.approx(-60, abs=0.3)
    assert f["sfdr_db"] == -f["harmonics_dbc"]["hd3"] and (f["spur_bin"], f["spur_harmonic"]) == (3 * b, 3)
    assert f["enob"] == pytest.approx((f["sndr_db"] - 1.76) / 6.02) and list(f["harmonics_dbc"]) == ["hd2", "hd3", "hd4", "hd5", "hd6"]
    assert "warnings" not in f
    # harmonics above Nyquist alias back into the first zone: 2 * 1501 -> 1094, 3 * 1501 -> 407
    w = 2 * np.pi * 1501 * np.arange(n) / n
    f = spectrum.figures(np.sin(w) + 1e-4 * np.sin(2 * w) + 1e-3 * np.sin(3 * w), fs, harmonics=2)
    assert (f["spur_bin"], f["spur_harmonic"]) == (407, 3) and f["harmonics_dbc"]["hd2"] == pytest.approx(-80, abs=0.01)
    assert f["thd_db"] == pytest.approx(10 * math.log10((1e-8 + 1e-6) / 1), abs=0.01) and f["sndr_db"] > 59.9
    # the given fin_bin is the signal, even when another bin is larger
    f = spectrum.figures(np.sin(w) + 2 * np.sin(2 * np.pi * 7 * np.arange(n) / n), fs, fin_bin=1501)
    assert f["fin_bin"] == 1501 and f["sfdr_db"] < 0 and "check fin_bin" in f["warnings"][0]


@pytest.mark.parametrize("n", [64, 255, 1000, 3000])
def test_no_signal_is_the_worst_result_at_any_length(n):
    """Constant samples leave exact zeros in the bins at some lengths only, rounding error (1e-30) at others."""
    f = spectrum.figures(np.full(n, 127.0), 1e6, full_scale=256)
    assert (f["sndr_db"], f["snr_db"], f["sfdr_db"], f["thd_db"], f["signal_dbfs"]) == (-300, -300, -300, 300, -300)
    assert f["enob"] < -50 and f["signal_power"] == 0 and "fin_bin" not in f and "harmonics_dbc" not in f
    assert f["warnings"][0].startswith("no signal: the samples are all 127")


def test_no_signal_says_where_it_looked():
    n, fs, k = 256, 1e6, np.arange(256)
    f = spectrum.figures(np.sin(2 * np.pi * 13 * k / n), fs, fin_bin=17)                # the signal is elsewhere
    assert f["fin_bin"] == 17 and f["sndr_db"] == -300
    assert "bin 17 (fin_bin) holds nothing, the largest is bin 13: check fin_bin" in f["warnings"][0]
    f = spectrum.figures(127.0 + k % 2, fs)                                     # a code toggling each sample: only Nyquist
    assert f["sndr_db"] == -300 and "only in its bin 128" in f["warnings"][0]
    assert "rounding error only" in spectrum.figures(0.9 + 1e-14 * np.sin(2 * np.pi * 13 * k / n), fs)["warnings"][0]
    assert spectrum.figures(0.9 + 1e-6 * np.sin(2 * np.pi * 13 * k / n), fs)["sndr_db"] > 200    # small, but a signal
    r = spectrum.measure(*_stuck())                                            # rounded like any result
    assert r["range"] == [127, 127] and r["sndr_db"] == -300 and r["enob"] == -50.13 and "no signal" in r["warnings"][0]


def test_the_bins_at_odd_lengths_and_at_nyquist():
    # odd points: no Nyquist bin, every bin above DC holds its mirror image; the top bin is (n - 1) / 2
    n, k = 255, np.arange(255)
    f = spectrum.figures(0.5 * np.sin(2 * np.pi * 127 * k / n), 1e6)
    assert f["fin_bin"] == 127 and f["signal_power"] == pytest.approx(0.125) and f["sndr_db"] > 250
    f = spectrum.figures(np.sin(2 * np.pi * 100 * k / n) + 1e-3 * np.sin(2 * np.pi * 200 * k / n), 1e6)
    assert f["signal_power"] == pytest.approx(0.5) and f["harmonics_dbc"]["hd2"] == pytest.approx(-60, abs=1e-6)
    assert f["spur_bin"] == 55                                                 # 200 aliased: 255 - 200
    # even points: the Nyquist bin is not doubled, a cosine of amplitude a there has power a^2
    n, k = 256, np.arange(256)
    f = spectrum.figures(np.sin(2 * np.pi * 64 * k / n) + 1e-3 * np.cos(np.pi * k), 1e6)
    assert (f["spur_bin"], f["spur_harmonic"]) == (128, 2)
    assert f["harmonics_dbc"]["hd2"] == pytest.approx(10 * math.log10(1e-6 / 0.5), abs=1e-6)          # -56.99 dBc


@pytest.mark.parametrize("bits", [6, 8, 10])
def test_an_ideal_adc_measures_its_resolution(bits):
    n, fs, b = 4096, 1e6, 1021                                                 # 1021 is prime: every code is hit
    code = _adc(0.999 * np.sin(2 * np.pi * b * np.arange(n) / n), bits)
    f = spectrum.figures(code, fs, full_scale=2 ** bits)
    assert f["sndr_db"] == pytest.approx(6.02 * bits + 1.76, abs=0.5) and f["enob"] == pytest.approx(bits, abs=0.1)
    assert f["signal_dbfs"] == pytest.approx(20 * math.log10(0.999), abs=0.02) and f["sfdr_db"] > f["sndr_db"]
    assert "warnings" not in f


def test_the_code_from_its_bits():
    n, fs, nb, b = 1024, 1e6, 8, 101
    code = _adc(0.89 * np.sin(2 * np.pi * b * np.arange(n) / n), nb)          # -1 dBFS
    edges = np.arange(n) / fs                                                  # each code held for a clock period
    t = np.column_stack([edges, edges + 0.99 / fs]).ravel()
    raw = _raw(t, **{f"d{i}": np.repeat(1.8 * ((code >> i) & 1), 2) for i in range(nb)})
    cfg = spectrum.config({"bits": [f"d{i}" for i in range(nb)], "fs": "1Meg", "points": n, "start": "0.5u", "full_scale": 256})
    x, info = spectrum.sequence(raw, cfg)
    assert (x == code).all() and info["threshold"] == pytest.approx(0.9)       # midway between 0 and 1.8 V
    r = spectrum.measure(raw, cfg)
    assert r["source"].startswith("code of 8 bits (v(d0) LSB") and r["range"] == [float(code.min()), float(code.max())]
    assert r["signal_dbfs"] == pytest.approx(20 * math.log10(0.89), abs=0.05) and r["enob"] == pytest.approx(7.8, abs=0.2)
    assert r["sndr_db"] == round(r["sndr_db"], 2) and "warnings" not in r                # rounded like other results
    # other weights (a redundant SAR) and a given threshold
    raw = _raw([0, 1, 2, 3], a=[1.2, 0, 1.2, 0], b=[0, 1.2, 1.2, 0.4], c=[1.2, 1.2, 0, 0.6])
    cfg = spectrum.config({"bits": ["a", "b", "c"], "weights": [1, 2, 3.5], "threshold": 0.5, "fs": 1, "points": 16, "start": 0})
    cfg["points"] = 4                                                          # (shorter than an FFT, to read the codes)
    assert spectrum.sequence(raw, cfg)[0].tolist() == [4.5, 5.5, 3, 3.5]
    # repeating codes: fin_bin shares a factor with points
    code = _adc(0.9 * np.sin(2 * np.pi * 96 * np.arange(n) / n), nb)
    raw = _raw(t, **{f"d{i}": np.repeat(1.8 * ((code >> i) & 1), 2) for i in range(nb)})
    r = spectrum.measure(raw, spectrum.config({"bits": [f"d{i}" for i in range(nb)], "fs": "1Meg", "points": n, "start": "0.5u"}))
    assert any("shares the factor 32" in w and "e.g. 97" in w for w in r["warnings"]) and "signal_dbfs" not in r


def test_a_differential_waveform_is_sampled_between_time_points():
    n, fs, b = 256, 1e6, 13
    t = np.arange(0, (n + 2) / fs, 0.1 / fs)
    w = 2 * np.pi * b * fs / n * t
    raw = _raw(t, outp=0.9 + 0.5 * np.sin(w) + 0.01 * np.sin(2 * w), outn=0.9 - 0.5 * np.sin(w) + 0.01 * np.sin(2 * w))
    r = spectrum.measure(raw, spectrum.config({"signal": ["outp", "outn"], "fs": fs, "points": n, "start": 0.37 / fs,
                                               "full_scale": 2}))
    assert r["source"] == "v(outp) - v(outn)" and r["fin_bin"] == b and r["signal_dbfs"] == pytest.approx(0, abs=0.01)
    assert r["sndr_db"] > 60 and r["harmonics_dbc"]["hd2"] < -100                 # the common-mode HD2 cancels
    # fewer time points than samples: interpolated, and said so
    coarse = _raw(np.linspace(0, (n + 2) / fs, n // 2), out=np.zeros(n // 2) + np.linspace(0, 1, n // 2))
    r = spectrum.measure(coarse, spectrum.config({"signal": "out", "fs": fs, "points": n}))
    assert "time points over the 256 samples" in r["warnings"][0] and "(analysis step)" in r["warnings"][0]


def test_non_coherent_sampling_is_said_without_failing():
    n, fs = 1024, 1e6
    f = spectrum.figures(np.sin(2 * np.pi * 100.37 * np.arange(n) / n), fs)
    assert f["fin_bin"] == 100 and f["sndr_db"] < 40
    assert "not coherent" in f["warnings"][0] and "bin 101" in f["warnings"][0]         # nearest odd bin prime to 1024, above
    assert "start the samples later" in f["warnings"][0]
    # a high-resolution ADC: a small offset from coherence already costs it
    code = _adc(0.95 * np.sin(2 * np.pi * 1021.001 * np.arange(4096) / 4096), 12)
    assert "not coherent" in spectrum.figures(code, fs)["warnings"][0]
    for seed in range(20):                                                     # coherent with noise: no warning
        x = np.sin(2 * np.pi * 101 * np.arange(n) / n) + np.random.default_rng(seed).normal(0, 1e-3, n)
        assert "warnings" not in spectrum.figures(x, fs)
    assert "warnings" not in spectrum.figures(np.sin(2 * np.pi * 13 * np.arange(256) / 256 + 0.3), fs)   # exact: no leak
    # coherent, but the amplitude still settling over the first tenth: the same skirt; fin is not what to change
    k = np.arange(256)
    x = 0.9 + 0.5 * (1 - 0.01 * np.exp(-k / 25.6)) * np.sin(2 * np.pi * 13 * k / 256) \
        + np.random.default_rng(0).normal(0, 1e-5, 256)
    w = spectrum.figures(x, fs)["warnings"][0]
    assert "fin is not exactly 50781.25 Hz, bin 13)" in w and "fin is that and the signal is not steady" in w
    assert "start the samples later" in w and "make fin" not in w


def test_samples_outside_the_run_or_another_run_are_refused():
    t = np.linspace(0, 1e-3, 1001)
    raw = _raw(t, out=np.sin(2 * np.pi * 1e4 * t))
    with pytest.raises(ValueError, match=r"run the transient to at least 0.001023 s"):
        spectrum.sequence(raw, spectrum.config({"signal": "out", "fs": "1Meg", "points": 1024}))
    with pytest.raises(ValueError, match="the run covers"):
        spectrum.sequence(raw, spectrum.config({"signal": "out", "period": "1u", "points": 512, "start": "0.6m"}))
    assert len(spectrum.sequence(raw, spectrum.config({"signal": "out", "period": "1u", "points": 1001}))[0]) == 1001
    with pytest.raises(ValueError, match="no signal nowhere in this run; it has: v\\(out\\)"):
        spectrum.sequence(raw, spectrum.config({"signal": "nowhere", "fs": "1Meg", "points": 64}))
    with pytest.raises(ValueError, match="transient run; this one is AC Analysis"):
        spectrum.sequence(_raw([1, 10], plot="AC Analysis", out=[1, 1]), spectrum.config({"signal": "out", "fs": 1, "points": 16}))


def test_the_request_is_checked():
    cfg = spectrum.config({"bits": ["d0", "d1"], "period": "100n", "points": "256", "start": "1.5u"})
    assert cfg["fs"] == pytest.approx(1e7) and cfg["weights"] == [1.0, 2.0] and cfg["start"] == pytest.approx(1.5e-6)
    assert cfg["harmonics"] == 5 and cfg["threshold"] is None and cfg["fin_bin"] is None
    assert spectrum.config({"signal": "out", "fs": "10Meg", "points": 64})["signal"] == ["out"]
    for bad, msg in (({"signal": "a", "bits": ["b"], "fs": 1, "points": 64}, "one of them"),
                     ({"signal": "a", "points": 64}, "fs"), ({"signal": "a", "fs": 1, "period": 1, "points": 64}, "fs"),
                     ({"signal": "a", "fs": 1, "points": 8}, "points"), ({"signal": "a", "fs": 1, "points": 64.5}, "whole"),
                     ({"signal": "a", "fs": 1, "points": 64, "fin_bin": 32}, "below Nyquist"),
                     ({"signal": "a", "fs": "fast", "points": 64}, "SI suffixes"),
                     ({"signal": "a", "fs": 1, "points": 64, "threshold": 0.9}, "go with bits"),
                     ({"bits": ["a", "b"], "weights": [1], "fs": 1, "points": 64}, "one number per bit"),
                     ({"bits": "d0", "fs": 1, "points": 64}, "LSB first"),
                     ({"signal": "a", "fs": 1, "points": 64, "start": "-1u"}, "not negative"),
                     ({"signal": "a", "fs": 1, "points": 64, "window": "hann"}, "unknown window"), ("out", "spectrum is")):
        with pytest.raises(ValueError, match=msg):
            spectrum.config(bad)
    cfg = spectrum.config({"signal": "out", "fs": "1Meg", "points": 256, "start": "1u"})        # last sample at 256 us
    assert spectrum.check_run(cfg, None) is None and spectrum.check_run(cfg, {"type": "tran", "stop": "300u"}) is None
    assert "not ac" in spectrum.check_run(cfg, {"type": "ac"})
    assert "before the last sample at 0.000256" in spectrum.check_run(cfg, {"type": "tran", "stop": "100u"})
    assert "give the transient's stop" in spectrum.check_run(cfg, {"type": "tran", "step": "1n"})


def test_a_spec_measures_a_spectral_figure():
    summ = {"spectrum": {"sndr_db": 49.1, "enob": 7.86}, "signals": {"v(out)": {"max": 1}}}
    assert specs.measure(summ, "SNDR_dB") == 49.1 and specs.measure(summ, "signal_dbfs") is None
    assert "sndr_db" in specs.available(summ) and "thd_db" not in specs.available(summ)
    sp = {"bits": ["d0", "d1"], "fs": "1Meg", "points": 64, "start": "0.5u"}
    model = {"items": [{"id": "s1", "kind": "spec", "text": "SNDR", "spec": {"measure": "sndr_db", "min": 10, "testbench": "tb",
                                                                              "analysis": {"type": "tran", "stop": "70u"}, "spectrum": sp}}]}
    p = specs.probes(model, "l")[0]
    assert p["spectrum"]["fs"] == 1e6 and p["spectrum"]["weights"] == [1.0, 2.0]
    for spec, msg in (({"analysis": {"type": "ac"}, "spectrum": sp}, "transient run, not ac"),
                      ({"analysis": {"type": "tran", "stop": "10u"}, "spectrum": sp}, "before the last sample"),
                      ({"spectrum": {**sp, "fs": None}}, "sample rate")):
        with pytest.raises(specs.SpecError, match=f"spec s1: .*{msg}"):
            specs.probes({"items": [{"id": "s1", "kind": "spec", "text": "x", "spec": {"measure": "sndr_db", "min": 1, **spec}}]}, "l")
    # the intent panel says what would not measure
    bad = {"items": [{"id": "s1", "kind": "spec", "text": "a", "spec": {"measure": "enob", "min": 7, "testbench": "tb"}},
                     {"id": "s2", "kind": "spec", "text": "b", "spec": {"measure": "sndr_db", "min": 40, "spectrum": {"bits": ["d0"]}}},
                     {"id": "s3", "kind": "spec", "text": "c", "spec": {"measure": "sfdr_db", "min": 40, "spectrum": sp,
                                                                         "analysis": {"type": "dc"}}},
                     {"id": "s4", "kind": "spec", "text": "d", "spec": {"measure": "signal_dbfs", "min": -2, "spectrum": sp}},
                     {"id": "s5", "kind": "spec", "text": "e", "spec": {"measure": "v(out).max", "max": 2, "spectrum": sp}},
                     {"id": "s6", "kind": "spec", "text": "f", "spec": {"measure": "thd_db", "max": -40, "spectrum": sp,
                                                                         "analysis": {"type": "tran", "stop": "70u"}}}]}
    msgs = {}
    for prob in intent.validate(bad, None):
        msgs.setdefault(prob["entry"], []).append((prob["level"], prob["message"]))
    assert msgs["items.s1"][0][0] == "warning" and "spectral figure" in msgs["items.s1"][0][1]
    assert msgs["items.s2"][0][0] == "error" and "sample rate" in msgs["items.s2"][0][1]
    assert msgs["items.s3"][0][0] == "error" and "not dc" in msgs["items.s3"][0][1]
    assert "needs the full scale" in msgs["items.s4"][0][1] and "not v(out).max" in msgs["items.s5"][0][1]
    assert "items.s6" not in msgs
    # a spectral figure the run does not give says why
    p = {"measure": "sndr_db", "meas": [], "output": None, "spectrum": None}
    assert "sndr_db is a spectral figure: the spec needs spectrum" in specs._missing(p, {"signals": {}}, "j1")
    p["spectrum"] = spectrum.config(sp)
    assert specs._missing(p, {"spectrum": {"error": "no signal dx in this run"}}, "j1") == \
        "its spectrum could not be taken: no signal dx in this run (job j1)"
    assert "needs full_scale" in specs._missing({**p, "measure": "signal_dbfs"}, {"spectrum": {"sndr_db": 40.0}}, "j1")


def test_a_spectral_figure_the_testbench_computes_itself_needs_no_spectrum():
    """A testbench whose .control computes sndr_db and prints it under "Measurements for" (as a hand-written FFT does):
    its spec needs no spectrum, so the intent has no warning about it and check_specs measures it; sfdr_db, which the
    testbench does not print, keeps the warning."""
    tm = intent.testbench_measures
    # a .control's meas or print outside an "echo Measurements for" section prints where no summary reads it
    assert tm(".meas tran tpd TRIG v(a) VAL=1 RISE=1 TARG v(b) VAL=1 RISE=1\n.control\nmeas tran vmax MAX v(out)\n"
              "print sndr_db\necho Measurements for x\n.endc") == {"netlist": {"tpd": "tran"}, "control": []}
    assert tm(".control\necho Measurements for FFT\nprint sndr_db sfdr_db v(out)\nprint all\nmeas tran vmax MAX v(out)\n"
              "echo\nprint enob\necho done\nprint thd_db\n.endc") == {
        "netlist": {}, "control": ["enob", "sfdr_db", "sndr_db", "vmax"]}
    assert tm(".control\necho Measurements for FFT\ntran 1n 1u\nprint sndr_db\n.endc")["control"] == []
    # xschem writes a code block's first line after value=": read from the parsed attribute
    from undine.server.api import _code_text
    sch = 'v {xschem version=3.4.5 file_version=1.2}\nC {devices/code_shown.sym} 0 0 0 0 {name=M\nvalue=".meas tran sndr_db param=1"}\n'
    assert tm(_code_text(sch)) == {"netlist": {"sndr_db": "tran"}, "control": []}
    _need()
    root, srv = _project()
    code = (".control\ntran 1n 100n\nlet sndr_db = 42.5\necho\n"
            "echo Measurements for the testbench's own FFT\nprint sndr_db\necho\n.endc")
    ctx = _tb(srv, "tbf", [("devices/vsource.sym", 0, {"name": "V1", "value": "pulse(0 1 0 1n 1n 10n 20n)"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1p"}),
                           ("devices/code_shown.sym", 400, {"name": "FFT", "value": code})],
              {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    tran = {"type": "tran", "step": "1n", "stop": "100n"}
    for text, measure, more in (("SNDR at least 40 dB", "sndr_db", {}), ("SFDR at least 40 dB", "sfdr_db", {}),
                                ("SNDR at least 40 dB in 100 ns", "sndr_db", {"analysis": tran})):
        res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "tbf", "kind": "spec", "text": text, "reason": "spec",
                                              "spec": {"measure": measure, "min": 40, "testbench": "tbf", **more}})
        assert not err, res
    res, err = T.call(ctx, "get_intent", {"lib": "l", "cell": "tbf"})
    assert not err, res
    probs = {p["entry"]: p["message"] for p in json.loads(res)["problems"]}
    assert "items.s1" not in probs and "sfdr_db is a spectral figure" in probs["items.s2"], probs
    # with its own analysis the spec drops the testbench's .control, and the print in it with it
    assert "sndr_db is a spectral figure" in probs["items.s3"], probs
    res, err = T.call(ctx, "get_design_context", {"lib": "l", "cell": "tbf"})
    assert not err and "s1: sndr_db is a spectral figure" not in res and "s2: sfdr_db is a spectral figure" in res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "tbf", "ids": ["s1"]})
    assert not err, res
    s1 = json.loads(res)["specs"][0]
    assert (s1["status"], s1["worst"]) == ("met", 42.5), s1
    shutil.rmtree(root)


def test_a_stuck_adc_at_one_corner_is_its_worst_case():
    """No signal is a failing value, not a measurement error: the spec is not met there, and a sweep still ranks the
    design point. A point that gave no value at all keeps the spec from being met."""
    stuck = spectrum.measure(*_stuck())
    probe = {"id": "s1", "text": "SNDR", "measure": "sndr_db", "min": 45.0, "max": None, "lib": "l", "testbench": "tb"}

    def row(corner, value=None, **extra):
        r = {"value": value, **specs.verdict(value, 45.0, None), "job": f"j-{corner}", **extra}
        return {"point": {"tb/C1.value": "1p", "corner": corner}, "results": {"s1": r}}
    ss = row("ss", specs.measure({"spectrum": stuck}, "sndr_db"), warnings=stuck["warnings"])
    s = specs.summarize([probe], {"rows": [row("tt", 49.0), ss]})[0]
    assert (s["status"], s["worst"], s["points_met"]) == ("not met", -300, "1/2") and s["worst_at"]["corner"] == "ss"
    assert "no signal: the samples are all 127" in s["warnings"][0]
    folded = specs.fold([row("tt", 49.0), ss], ["tb/C1.value"])[0]
    assert folded["score"] < -1 and not folded["all_pass"] and folded["results"]["s1"]["worst_at"] == {"corner": "ss"}
    # measured and met at tt only: an error at ss, or a run still going there
    err = row("ss", error="its spectrum could not be taken: no signal dx in this run (job j-ss)")
    s = specs.summarize([probe], {"rows": [row("tt", 49.0), err]})[0]
    assert (s["status"], s["worst"], s["points_met"]) == ("not fully measured", 49.0, "1/2")
    assert s["not_measured_at"] == [{"tb/C1.value": "1p", "corner": "ss"}] and s["why"].startswith("its spectrum could not be taken")
    s = specs.summarize([probe], {"rows": [row("tt", 49.0), {"point": {"corner": "ss"}, "results": {"s1": {"pending": True}}}]})[0]
    assert s["status"] == "not fully measured" and "why" not in s and s["running"] == 1
    assert specs.summarize([probe], {"rows": [row("tt", 49.0), row("ss", 47.0)]})[0]["status"] == "met"


# ------------------------------------------------------------------ with ngspice


def _adc_tb(srv):
    """A 4-bit ADC as behavioural sources: a code of the input (0.1 V per step), and its bits at 0 / 1.8 V."""
    c = "min(15, max(0, floor((v(in) - 0.1) * 10)))"
    bit = "1.8 * (floor(v(c) / {a}) - 2 * floor(v(c) / {b}))"
    parts = [("devices/vsource.sym", 0, {"name": "V1", "value": "sin(0.9 0.79 46875)"}),
             ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
             ("devices/bsource.sym", 200, {"name": "BC", "VAR": "V", "FUNC": c})]
    parts += [("devices/bsource.sym", 300 + 100 * i, {"name": f"B{i}", "VAR": "V", "FUNC": bit.format(a=2 ** i, b=2 ** (i + 1))})
              for i in range(4)]
    nets = {"in": ["V1:p", "R1:P"], "c": ["BC:p"], "0": ["V1:m", "R1:M", "BC:m", *[f"B{i}:m" for i in range(4)]],
            **{f"d{i}": [f"B{i}:p"] for i in range(4)}}
    return _tb(srv, "adc", parts, nets)


SPECTRUM = {"bits": ["d0", "d1", "d2", "d3"], "fs": "1Meg", "points": 64, "start": "0.5u", "full_scale": 16}
TRAN = {"type": "tran", "step": "10n", "stop": "70u"}                          # 64 samples of 1 us from 0.5 us; fin = 3 / 64 MHz


def test_an_adc_in_ngspice_measured_by_run_simulation_and_its_specs():
    _need()
    root, srv = _project()
    ctx = _adc_tb(srv)
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "adc", "analysis": TRAN, "signals": ["in", "d3"],
                                             "spectrum": SPECTRUM})
    assert not err, res
    out = json.loads(res)
    s = out["spectrum"]
    assert s["fin_bin"] == 3 and s["fin_hz"] == pytest.approx(46875) and s["range"] == [0, 15]
    assert s["sndr_db"] == pytest.approx(6.02 * 4 + 1.76, abs=1.5) and s["enob"] == pytest.approx(4, abs=0.25)
    assert s["signal_dbfs"] == pytest.approx(20 * math.log10(7.9 / 8), abs=0.2) and "warnings" not in s
    assert out["signals"]["v(d3)"]["max"] == pytest.approx(1.8)                # next to the other results
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "adc", "analysis": TRAN,
                                             "spectrum": {"signal": "in", "fs": "1Meg", "points": 64, "start": "0.5u"}})
    assert not err and json.loads(res)["spectrum"]["sndr_db"] > 80                   # the input itself: a clean sine
    # what cannot give a spectrum is refused before running, or said in the result
    for args, msg in (({"analysis": {"type": "ac"}}, "transient run, not ac"),
                      ({"analysis": {**TRAN, "stop": "50u"}}, "before the last sample"),
                      ({"analysis": TRAN, "spectrum": {**SPECTRUM, "points": 4}}, "points")):
        res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "adc", "spectrum": SPECTRUM, **args})
        assert err and msg in res, res
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "adc", "analysis": TRAN, "spectrum": {**SPECTRUM, "bits": ["d0", "dx"]}})
    assert not err and "no signal dx" in json.loads(res)["spectrum"]["error"]
    # specs: SNDR, ENOB, SFDR and the level, met, on one run; an unreachable SNDR is not met
    for text, spec in (("SNDR at least 24 dB", {"measure": "sndr_db", "min": 24}), ("ENOB at least 3.8", {"measure": "enob", "min": 3.8}),
                       ("SFDR at least 30 dB", {"measure": "sfdr_db", "min": 30}),
                       ("near full scale", {"measure": "signal_dbfs", "min": -0.5}),
                       ("8-bit SNDR", {"measure": "sndr_db", "min": 49})):
        res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "adc", "kind": "spec", "text": text, "reason": "spec",
                                              "spec": {**spec, "testbench": "adc", "analysis": TRAN, "spectrum": SPECTRUM}})
        assert not err, res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "adc"})
    assert not err, res
    by = {s["id"]: s for s in json.loads(res)["specs"]}
    assert [by[k]["status"] for k in ("s1", "s2", "s3", "s4", "s5")] == ["met"] * 4 + ["not met"]
    assert by["s1"]["worst"] == s["sndr_db"] and by["s5"]["margin"] < -20 and len({by[k]["job"] for k in by}) == 1
    # swept: a smaller input (a condition of the testbench) lowers the level and the SNDR with it
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "adc", "vary": {"R1.value": ["1k"]}, "ids": ["s1", "s4"],
                                     "conditions": {"adc/V1.value": ["sin(0.9 0.79 46875)", "sin(0.9 0.39 46875)"]}})
    assert not err, res
    p = json.loads(res)["points"][0]
    assert p["worst_at"]["s4"] == {"adc/V1.value": "sin(0.9 0.39 46875)"} and p["values"]["s4"] == pytest.approx(-6.2, abs=0.3)
    assert p["values"]["s1"] < s["sndr_db"] - 4 and p["not_met"] == ["s1", "s4"]
    # no input at one condition: the code stands still, the worst case rather than a measurement error
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "adc", "ids": ["s1"],
                                           "conditions": {"adc/V1.value": ["sin(0.9 0.79 46875)", "0.95"]}})
    assert not err, res
    w = json.loads(res)["specs"][0]
    assert (w["status"], w["worst"], w["worst_at"]) == ("not met", -300, {"adc/V1.value": "0.95"})
    assert w["warnings"][0].startswith("no signal: the samples are all 8")
    shutil.rmtree(root)
