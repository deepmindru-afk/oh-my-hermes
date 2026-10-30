"""Tempo and drop timing contracts on deterministic, plain-list envelopes."""
from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import runpy
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1] / "src/skills/show_reel_toolkit"


def load(name):
    path = KIT / "scripts" / name
    if not path.is_file():
        raise AssertionError(f"missing tempo helper {name}")
    sys.path.insert(0, str(path.parent))
    try:
        return runpy.run_path(str(path))
    finally:
        sys.path.pop(0)


def clicks(bpm, seconds=40, rate=100, phase=0.13):
    values = [0.0] * int(seconds * rate)
    t = phase
    while t < seconds:
        values[round(t * rate)] = 1.0
        t += 60 / bpm
    return values


class TempoGridTests(unittest.TestCase):
    def test_hint_keeps_fast_grid_and_fits_phase(self):
        api = load("music_tools.py")
        self.assertIn("fit_grid", api, "pure-Python grid fitter is required")
        fit = api["fit_grid"](clicks(174), 100, bpm_hint=174)
        self.assertAlmostEqual(fit["bpm"], 174, delta=0.3)
        self.assertAlmostEqual(fit["phase"], 0.13, delta=0.02)
        self.assertGreater(fit["grid_strength"], 0.7)
        self.assertLessEqual(fit["grid_strength"], 1)
        self.assertNotIn("warning", fit)

    def test_unhinted_retains_folded_tempo_and_offers_fast_alternative(self):
        api = load("music_tools.py")
        self.assertIn("fit_grid", api)
        fit = api["fit_grid"](clicks(174), 100)
        self.assertAlmostEqual(fit["bpm"], 87, delta=0.5)
        self.assertTrue(any(abs(b - 174) < 1 for b in fit["alternatives"]))

    def test_mismatched_hint_is_weak_and_warns_not_snapped(self):
        api = load("music_tools.py")
        self.assertIn("fit_grid", api)
        fit = api["fit_grid"](clicks(120), 100, bpm_hint=174)
        self.assertGreaterEqual(fit["bpm"], 174 * 0.92)
        self.assertLessEqual(fit["bpm"], 174 * 1.08)
        self.assertEqual(fit.get("warning"), "tempo hint disagrees with the track")
        self.assertLess(fit["grid_strength"], 0.5)

    def test_start_window_selects_only_fitted_stretch(self):
        api = load("music_tools.py")
        self.assertIn("fit_grid", api)
        values = clicks(120, seconds=20) + clicks(174, seconds=20)
        fit = api["fit_grid"](values, 100, bpm_hint=174, start=20, window=20)
        self.assertAlmostEqual(fit["bpm"], 174, delta=0.3)
        period = 60 / fit["bpm"]
        self.assertLess(abs((20.13 - fit["phase"] + period / 2) % period - period / 2), 0.02)
        self.assertNotIn("warning", fit)

    def test_silence_has_no_grid_evidence_and_invalid_inputs_refused(self):
        api = load("music_tools.py")
        self.assertIn("fit_grid", api)
        fit = api["fit_grid"]([0.0] * 500, 100, bpm_hint=174)
        self.assertEqual(fit["grid_strength"], 0)
        for kwargs in ({"bpm_hint": 0}, {"bpm_hint": float("nan")}, {"start": -1}, {"window": -1}, {"start": 99}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                api["fit_grid"]([0.0] * 500, 100, **kwargs)
        with self.assertRaises(ValueError):
            api["fit_grid"]([], 100)
        with self.assertRaises(ValueError):
            api["fit_grid"]([1.0], 0)

    def test_drop_at_bar_eight_is_not_pulled_into_previous_bar(self):
        api = load("music_tools.py")
        self.assertIn("find_drops", api, "pure-Python drop finder is required")
        downbeats = [i * 2.0 for i in range(15)]
        rms = [0.1 if i < 1400 else 0.8 for i in range(3000)]
        drops = api["find_drops"](rms, 100, downbeats, 120)
        self.assertEqual(drops[0]["bar"], 8)
        self.assertEqual(drops[0]["t"], 14)
        self.assertAlmostEqual(drops[0]["rise"], 8, delta=0.01)
        self.assertLessEqual(len(drops), 3)
        self.assertEqual(api["find_drops"]([0.0] * 3000, 100, downbeats, 120), [])
        self.assertEqual(api["find_drops"]([0.5] * 3000, 100, downbeats, 120), [])

    def test_drops_are_limited_ranked_by_rise_then_sorted_in_time(self):
        api = load("music_tools.py")
        self.assertIn("find_drops", api)
        values = [0.1] * 6000
        for bar, level in ((3, 0.3), (9, 0.7), (15, 0.5), (21, 0.9)):
            values[(bar - 1) * 200:(bar + 1) * 200] = [level] * 400
        drops = api["find_drops"](values, 100, [i * 2 for i in range(30)], 120)
        self.assertEqual([d["bar"] for d in drops], [9, 15, 21])

    def test_flat_nonzero_envelope_is_not_rhythmic_evidence(self):
        api = load("music_tools.py")
        for level in (0.1, 0.3, 1e-12):
            with self.subTest(level=level):
                fit = api["fit_grid"]([level] * 500, 100, bpm_hint=174)
                self.assertEqual(fit["grid_strength"], 0)
                self.assertNotIn("warning", fit)

    def test_flat_envelope_stays_flat_when_the_mean_rounds_low(self):
        # Python before 3.12 sums floats naively; 0.3 * 500 then averages to a
        # hair below 0.3 and every flat sample looked like a tiny onset.
        api = load("music_tools.py")
        flat = [0.3] * 500
        naive = 0.0
        for v in flat:
            naive += v
        self.assertNotEqual(naive / len(flat), 0.3)
        fit = api["fit_grid"](flat, 100, bpm_hint=174)
        self.assertEqual(fit["grid_strength"], 0)

    def test_hint_is_a_search_center_not_a_fixed_bpm(self):
        api = load("music_tools.py")
        fit = api["fit_grid"](clicks(171), 100, bpm_hint=174)
        self.assertAlmostEqual(fit["bpm"], 171, delta=0.3)

    def test_drop_finder_refuses_invalid_evidence_and_incomplete_windows(self):
        api = load("music_tools.py")
        self.assertEqual(api["find_drops"]([0.1] * 100, 100, [0], 120), [])
        for values, rate, times, bpm in (([float("nan")], 100, [0], 120),
                                         ([0.1], 0, [0], 120), ([0.1], 100, [0], 0),
                                         ([0.1], 100, [1, 0], 120)):
            with self.subTest(values=values, rate=rate, times=times, bpm=bpm), self.assertRaises(ValueError):
                api["find_drops"](values, rate, times, bpm)

    def test_analyze_cli_exposes_fit_arguments_without_numpy(self):
        api = load("music_tools.py")
        output = io.StringIO()
        with patch.object(sys, "argv", ["music_tools.py", "analyze", "--help"]), redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
            api["main"]()
        self.assertEqual(exit_info.exception.code, 0)
        for flag in ("--bpm", "--start", "--window"):
            self.assertIn(flag, output.getvalue())

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "optional numpy is unavailable")
    def test_analyze_pipeline_emits_grid_drop_and_octave_fields(self):
        import numpy as np
        api = load("music_tools.py")
        rate = 22050
        samples = np.zeros(rate * 12)
        for beat in range(34):
            start = round((0.13 + beat * 60 / 174) * rate)
            pulse = np.arange(min(2205, len(samples) - start)) / rate
            samples[start:start + len(pulse)] += np.sin(2 * np.pi * 80 * pulse) * np.exp(-pulse * 40)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "analysis.json"
            args = SimpleNamespace(file="decoded-fixture.wav", ffmpeg="not-used", out=str(out), bpm=174, start=1, window=8)
            with patch.dict(api["cmd_analyze"].__globals__, decode=lambda *args: samples), redirect_stdout(io.StringIO()):
                api["cmd_analyze"](args)
            analysis = json.loads(out.read_text(encoding="utf-8"))
            self.assertAlmostEqual(analysis["bpm"], 174, delta=0.5)
            self.assertGreaterEqual(analysis["grid_strength"], 0)
            self.assertLessEqual(analysis["grid_strength"], 1)
            self.assertIsInstance(analysis["drops"], list)
            self.assertTrue(any(abs(b - 87) < 0.3 for b in analysis["alternatives"]))
            self.assertTrue(analysis["beats"])

    def test_timeline_drop_cues_and_music_data_share_offset(self):
        api = load("timeline.py")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            analysis = {"bpm": 120, "beats": [0, 0.5], "downbeats": [0, 2], "hits": [1],
                        "drops": [{"t": 2.0, "rise": 4.0, "bar": 2}, {"t": 6.0, "rise": 2.0, "bar": 4}]}
            (root / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
            script = {"duration": 12, "fps": 30, "lines": [], "music": {"analysis": "analysis.json", "start": 1.25},
                      "scenes": [{"id": "drop", "start": "music.drop1", "end": "music.drop2"}]}
            (root / "script.json").write_text(json.dumps(script), encoding="utf-8")
            tl, _, errors = api["compose"](root / "script.json")
            self.assertEqual(errors, [])
            self.assertEqual(tl["cues"]["music.drop1"], 3.25)
            self.assertEqual(tl["cues"]["music.drop2"], 7.25)
            self.assertEqual(tl["music_tl"]["drops"], [{"t": 3.25, "rise": 4.0, "bar": 2}, {"t": 7.25, "rise": 2.0, "bar": 4}])
            self.assertEqual(tl["cues"]["bar2"], 3.25)
            page = (root / "reel" / "timeline.js").read_text(encoding="utf-8")
            self.assertIn('"drops"', page)


if __name__ == "__main__":
    unittest.main()
