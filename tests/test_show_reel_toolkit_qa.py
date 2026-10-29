"""Reports do not infer artistic, playback, hearing or native-language approval."""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1] / "src/skills/show_reel_toolkit"


def load(name):
    path = KIT / "scripts" / name
    if not path.is_file():
        raise AssertionError(f"missing measured QA helper {name}")
    sys.path.insert(0, str(path.parent))
    try:
        return runpy.run_path(str(path))
    finally:
        sys.path.pop(0)


class TechnicalQATests(unittest.TestCase):
    def test_input_policy_refuses_nonregular_files_before_reading_bytes(self):
        from unittest.mock import patch
        api = load("qa_check.py")
        with tempfile.TemporaryDirectory() as td, patch.object(Path, "read_bytes", side_effect=AssertionError("read of nonregular input")):
            result = api["inspect"](Path(td), audio_mode="muted")
            self.assertEqual(result["verdict"], "HOLD")
            self.assertIsNone(result["measurements"])

    def test_missing_and_corrupt_inputs_never_become_zero_measurement_or_success(self):
        api = load("qa_check.py")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "invalid.mp4"
            path.write_bytes(b"not a video")
            report = api["inspect"](path, audio_mode="muted", ffprobe="missing-explicit-ffprobe", ffmpeg="missing-explicit-ffmpeg")
            self.assertEqual(report["verdict"], "HOLD")
            self.assertIsNone(report["measurements"])
            for key in ("artistic", "playback", "listening", "native_language"):
                self.assertEqual(report[key], "not_observed")
            self.assertTrue(report["artifact_sha256"])
            with self.assertRaises(ValueError):
                api["inspect"](path, audio_mode="auto")

    def test_motion_freeze_is_distinct_from_annotated_intentional_hold(self):
        api = load("motion_check.py")
        frames = [bytes([32]*16)] * 9  # Eight 0.25 s comparisons: a 2 s freeze.
        report = api["analyze_frames"](frames, fps=4)
        self.assertEqual(report["verdict"], "REVISE")
        intentional = api["analyze_frames"](frames, fps=4, holds=[{"start": 0, "end": 1, "reason": "reading hold reviewed in storyboard"}])
        self.assertEqual(intentional["verdict"], "PASS")
        self.assertEqual(intentional["artistic"], "not_observed")
        with self.assertRaises(ValueError):
            api["analyze_frames"]([], fps=4)
        with self.assertRaises(ValueError):
            api["analyze_frames"](frames, fps=4, holds=[{"start": 0, "end": 1, "reason": ""}])


@unittest.skipUnless(os.environ.get("SHOW_REEL_HOST_TOOLS"), "explicit host tool opt-in")
class ActualQATests(unittest.TestCase):
    def test_corrupt_decode_and_missing_required_audio_strict_exit(self):
        tools = json.loads(Path(os.environ["SHOW_REEL_HOST_TOOLS"]).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "muted.mp4"
            subprocess.run([tools["binaries"]["ffmpeg"], "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=4:d=1", "-c:v", "libx264", str(video)], check=True, timeout=20)
            api = load("qa_check.py")
            report = api["inspect"](video, audio_mode="muted", ffprobe=tools["binaries"]["ffprobe"], ffmpeg=tools["binaries"]["ffmpeg"])
            self.assertEqual(report["verdict"], "PASS", report)
            self.assertEqual(report["audio"], "not_applicable_muted")
            required = api["inspect"](video, audio_mode="music-only", ffprobe=tools["binaries"]["ffprobe"], ffmpeg=tools["binaries"]["ffmpeg"])
            self.assertEqual(required["verdict"], "REVISE")
            video.write_bytes(b"corrupt")
            corrupt = api["inspect"](video, audio_mode="muted", ffprobe=tools["binaries"]["ffprobe"], ffmpeg=tools["binaries"]["ffmpeg"])
            self.assertEqual(corrupt["verdict"], "HOLD")


if __name__ == "__main__":
    unittest.main()
