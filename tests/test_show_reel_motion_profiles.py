"""Editor-facing motion metrics use native cadence and explicit intentional holds."""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1] / "src/skills/show_reel_toolkit"


def load():
    path = KIT / "scripts" / "motion_check.py"
    sys.path.insert(0, str(path.parent))
    try:
        return runpy.run_path(str(path))
    finally:
        sys.path.pop(0)


class MotionProfileTests(unittest.TestCase):
    def analyze(self, areas, lumas=None, **kwargs):
        api = load()
        self.assertIn("analyze_series", api, "A1 needs a stdlib series analyzer")
        return api["analyze_series"](areas, [100.] * len(areas) if lumas is None else lumas, fps=10, **kwargs)

    def test_short_still_passes_general_but_not_kinetic(self):
        areas = [.2] * 10 + [0.] * 7 + [.2] * 10
        self.assertEqual(self.analyze(areas)["verdict"], "PASS")
        report = self.analyze(areas, profile="kinetic")
        self.assertEqual(report["verdict"], "REVISE")
        issue = next(i for i in report["issues"] if i["kind"] == "unannotated_stillness")
        self.assertAlmostEqual(issue["at"], 1.)
        self.assertAlmostEqual(issue["duration"], .7)

    def test_long_still_is_exempt_only_where_annotated(self):
        areas = [.2] * 10 + [0.] * 12 + [.2] * 10
        self.assertEqual(self.analyze(areas)["verdict"], "REVISE")
        held = self.analyze(areas, holds=[{"start": 1, "end": 2.2, "reason": "reading"}])
        self.assertEqual(held["verdict"], "PASS")
        self.assertEqual(held["metrics"]["longest_unannotated_still_s"], 0)
        partial = self.analyze(areas, holds=[{"start": 1.5, "end": 1.7, "reason": "pause"}])
        self.assertAlmostEqual(partial["metrics"]["longest_unannotated_still_s"], .5)
        self.assertEqual(partial["metrics"]["still_runs"], [[1., 1.2]])
        with self.assertRaises(ValueError):
            self.analyze(areas, holds=[{"start": 1, "end": 2.2, "reason": ""}])

    def test_half_still_is_measured_not_inferred_from_mean_delta(self):
        report = self.analyze([0., .2] * 10)
        self.assertAlmostEqual(report["metrics"]["still_share"], .5, delta=.02)
        self.assertAlmostEqual(report["metrics"]["high_motion_share"], .5)
        self.assertAlmostEqual(report["metrics"]["mean_change_area"], .1)
        self.assertEqual(report["artistic"], "not_observed")
        self.assertEqual(self.analyze([0., .2] * 10, profile="kinetic")["verdict"], "REVISE")

    def test_flash_count_is_opposing_nonoverlapping_pairs(self):
        four = self.analyze([.2] * 10, [0., 100., 0., 100., 0., 100., 0., 100., 0., 0.])
        self.assertEqual(four["metrics"]["max_flashes_per_s"], 4)
        self.assertIn("flash_review_required", [i["kind"] for i in four["issues"]])
        three = self.analyze([.2] * 10, [0., 100., 0., 100., 0., 100., 0., 0., 0., 0.])
        self.assertEqual(three["metrics"]["max_flashes_per_s"], 3)
        self.assertEqual(three["verdict"], "PASS")
        flipped = self.analyze([.2] * 10, [0.] * 5 + [100.] * 5)
        self.assertEqual(flipped["metrics"]["max_flashes_per_s"], 0)
        self.assertEqual(flipped["verdict"], "PASS")

    def test_flash_onset_is_not_shifted_by_same_direction_jumps(self):
        # Three rising steps are one excursion, not three possible onsets.
        lumas = [0., 50., 100., 150., 0., 0., 0., 100., 0., 100., 0., 100., 0., 0.]
        report = self.analyze([.2] * len(lumas), lumas)
        self.assertEqual(report["metrics"]["max_flashes_per_s"], 3)
        self.assertEqual(report["verdict"], "PASS")

    def test_endcard_excludes_tail_from_all_metrics(self):
        report = self.analyze([.2] * 10 + [0.] * 20, [100.] * 10 + [0., 100.] * 10, endcard=2)
        metrics = report["metrics"]
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(metrics["seconds"], 1)
        self.assertEqual(metrics["still_share"], 0)
        self.assertEqual(metrics["still_runs"], [])
        self.assertEqual(metrics["max_flashes_per_s"], 0)
        self.assertAlmostEqual(metrics["mean_change_area"], .2)

    def test_cut_thresholds_and_native_fps(self):
        api = load()
        self.assertIn("analyze_series", api)
        report = api["analyze_series"]([.45, .46, .6, .61], [100., 120., 120., 120.], fps=60)
        self.assertEqual(report["metrics"]["fps"], 60)
        self.assertAlmostEqual(report["metrics"]["cuts_per_10s"], 300.)

    def test_raw_gray_wrapper_uses_changed_pixel_area(self):
        api = load()
        frames = [bytes([32] * 16)] * 9
        self.assertEqual(api["analyze_frames"](frames, fps=4)["verdict"], "REVISE")
        self.assertEqual(api["analyze_frames"](frames, fps=4, holds=[{"start": 0, "end": 2, "reason": "end title"}])["verdict"], "PASS")
        tiny = api["analyze_frames"]([bytes(100), bytes([1] * 100)], fps=30)
        self.assertEqual(tiny["metrics"]["still_share"], 1)

    def test_invalid_inputs_are_not_successful_measurements(self):
        for areas, lumas, kwargs in [([], [], {}), ([0.], [], {}), ([float("nan")], [100.], {}), ([1.1], [100.], {}), ([0.], [100.], {"profile": "unknown"}), ([0.], [100.], {"endcard": 1})]:
            with self.subTest(areas=areas, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.analyze(areas, lumas, **kwargs)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class MotionDecodeTests(unittest.TestCase):
    def test_solid_freeze_cli_and_reference_are_measured(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "freeze.mp4"
            holds = root / "holds.json"
            holds.write_text(json.dumps([{"start": 0, "end": 2, "reason": "reviewed title"}]), encoding="utf-8")
            ffmpeg = shutil.which("ffmpeg")
            ffprobe = shutil.which("ffprobe")
            assert ffmpeg is not None and ffprobe is not None
            subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=0.5", "-vf", "tpad=stop_mode=clone:stop_duration=1.5", "-c:v", "libx264", str(video)], check=True, timeout=30)
            command = [sys.executable, str(KIT / "scripts/motion_check.py"), str(video), "--ffmpeg", ffmpeg, "--ffprobe", ffprobe]
            measured = subprocess.run(command + ["--reference", str(video)], capture_output=True, text=True, timeout=60)
            self.assertEqual(measured.returncode, 1, measured.stderr)
            report = json.loads(measured.stdout)
            self.assertEqual(report["verdict"], "REVISE")
            self.assertEqual(report["metrics"]["fps"], 30)
            self.assertGreater(report["metrics"]["longest_unannotated_still_s"], 1.4)
            self.assertEqual(report["artifact_sha256"], report["reference"]["artifact_sha256"])
            held = subprocess.run(command + ["--holds", str(holds)], capture_output=True, text=True, timeout=60)
            self.assertEqual(held.returncode, 0, held.stderr)
            self.assertEqual(json.loads(held.stdout)["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
