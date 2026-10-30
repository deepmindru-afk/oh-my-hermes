"""Playability policy (H.264 level, hardware decode) and buried-SFX reporting stay measured and fail closed."""
from __future__ import annotations

import array
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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


def probe_json(codec="h264", level=40):
    video = {"codec_type": "video", "codec_name": codec, "duration": "1.0", "avg_frame_rate": "30/1",
             "nb_read_frames": "30", "width": 320, "height": 180}
    if level is not None:
        video["level"] = level
    return json.dumps({"streams": [video], "format": {"duration": "1.0"}})


class PlaybackPolicyTests(unittest.TestCase):
    def inspect_stubbed(self, policy, *, codec="h264", level: int | None = 40, decode_errors=""):
        api = load("qa_check.py")
        scope = api["inspect"].__globals__
        scope["run_tool"] = lambda command: probe_json(codec, level) if "-show_streams" in [str(v) for v in command] else ""
        scope["decode_errors"] = lambda command: decode_errors
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "clip.mp4"
            path.write_bytes(b"stub")
            return api["inspect"](path, audio_mode="muted", policy=policy)

    def test_h264_level_is_measured_and_bounded(self):
        report = self.inspect_stubbed({"max_h264_level": 42}, level=40)
        self.assertEqual(report["measurements"]["h264_level"], 40)
        self.assertEqual(report["verdict"], "PASS")
        report = self.inspect_stubbed({"max_h264_level": 42}, level=52)
        self.assertEqual(report["verdict"], "REVISE")
        self.assertIn("max_h264_level", [c["name"] for c in report["checks"] if c["status"] == "REVISE"])

    def test_non_h264_or_missing_level_fails_level_policy(self):
        report = self.inspect_stubbed({"max_h264_level": 52}, codec="hevc", level=120)
        self.assertIsNone(report["measurements"]["h264_level"])
        self.assertEqual(report["verdict"], "REVISE")
        self.assertEqual(self.inspect_stubbed({"max_h264_level": 52}, level=None)["verdict"], "REVISE")

    def test_hardware_decode_off_macos_holds_never_passes(self):
        with patch.object(sys, "platform", "linux"):
            report = self.inspect_stubbed({"hardware_decode": True, "max_h264_level": 42})
        self.assertEqual(report["verdict"], "HOLD")
        hold = [c for c in report["checks"] if c["name"] == "hardware_decode"]
        self.assertEqual(hold[0]["status"], "HOLD")
        self.assertIn("not observable on this host", hold[0]["reason"])

    def test_hardware_decode_on_macos_revises_on_any_error_line(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(self.inspect_stubbed({"hardware_decode": True})["verdict"], "PASS")
            report = self.inspect_stubbed({"hardware_decode": True}, decode_errors="[h264] hardware accelerator failed\n")
        self.assertEqual(report["verdict"], "REVISE")
        self.assertIn("hardware_decode", [c["name"] for c in report["checks"] if c["status"] == "REVISE"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe") and sys.platform == "darwin", "host ffmpeg/ffprobe on macOS required")
    def test_real_level_40_clip_passes_both_keys_on_macos(self):
        api = load("qa_check.py")
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.mp4"
            subprocess.run([str(shutil.which("ffmpeg")), "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-level:v", "4.0", str(clip)], check=True, timeout=120)
            report = api["inspect"](clip, audio_mode="muted", ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"),
                                    policy={"max_h264_level": 42, "hardware_decode": True})
        self.assertEqual(report["measurements"]["h264_level"], 40)
        self.assertEqual(report["verdict"], "PASS", report["checks"])


class BuriedHitTests(unittest.TestCase):
    def run_mix(self, sfx_rows):
        api = load("mix.py")
        rate = 8000
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            api["write_wav"](root / "bed.wav", array.array("f", [.5, -.5] * rate), rate)
            api["write_wav"](root / "hit.wav", array.array("f", [.5, -.5] * (rate // 5)), rate)
            spec = {"duration": 1, "sample_rate": rate, "mode": "music-only", "music": {"file": "bed.wav"}, "sfx": sfx_rows}
            return api["mix"](spec, root)[2]

    def test_quiet_sfx_under_loud_bed_is_listed_loud_one_is_not(self):
        report = self.run_mix([{"file": "hit.wav", "at": .2, "gain_db": -20}, {"file": "hit.wav", "at": .6, "gain_db": 6}])
        self.assertEqual(len(report["buried_hits"]), 1)
        hit = report["buried_hits"][0]
        self.assertEqual((hit["at"], hit["file"]), (.2, "hit.wav"))
        self.assertLess(hit["sfx_db"], hit["bed_db"])
        self.assertAlmostEqual(hit["bed_db"], -6.02, places=1)

    def test_no_sfx_and_muted_report_empty_list(self):
        self.assertEqual(self.run_mix([])["buried_hits"], [])
        api = load("mix.py")
        with tempfile.TemporaryDirectory() as td:
            muted = api["mix"]({"duration": 1, "sample_rate": 8000, "mode": "muted"}, Path(td))[2]
        self.assertEqual(muted["buried_hits"], [])


if __name__ == "__main__":
    unittest.main()
