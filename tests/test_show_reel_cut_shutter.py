"""Cut-aware shutter behavior through the standalone ES-module renderer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1] / "src/skills/show_reel_toolkit"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "optional Node unavailable")
class ShutterTimesTests(unittest.TestCase):
    def node(self, body):
        assert NODE is not None
        script = "const api = await import(process.argv[1]);\n" + body
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, (KIT / "scripts/render.mjs").as_uri()],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_no_cuts_preserves_existing_samples_exactly(self):
        result = self.node("""
const cases = [[0, 8, 4, 1, 2], [.4, 30, 1, .5, 2], [.4, 30, 7, .7, 2],
               [1.99999, 60, 16, 1, 2], [1, 24, 4, 0, 2]];
const rows = cases.map(([t, fps, sub, shutter, end]) => {
  const expected = Array.from({length:sub}, (_,k) =>
    Math.max(0, Math.min(end-1e-4, t+((k+.5)/sub-.5)*shutter/fps)));
  return {expected, actual:api.shutterTimes(t, fps, sub, shutter, [], end)};
});
console.log(JSON.stringify(rows));
""")
        for row in result:
            self.assertEqual(row["actual"], row["expected"])

    def test_cut_boundaries_slide_window_instead_of_clipping_samples(self):
        rows = self.node("""
const cuts = Object.freeze([1]);
console.log(JSON.stringify([.999999, 1, 1.000001].map(t =>
  api.shutterTimes(t, 8, 4, 1, cuts, 2))));
""")
        for index, samples in enumerate(rows):
            self.assertEqual(len(samples), 4)
            self.assertTrue(all(0 <= s < 1 for s in samples) if index == 0
                            else all(1 <= s < 2 for s in samples))
            for a, b in zip(samples, samples[1:]):
                self.assertAlmostEqual(b - a, .125 / 4)
        self.assertEqual(rows[0], [.890625, .921875, .953125, .984375])
        self.assertEqual(rows[1], [1.015625, 1.046875, 1.078125, 1.109375])

    def test_short_shot_shrinks_shutter_to_fit(self):
        rows = self.node("""
console.log(JSON.stringify([1, 1.01, 1.019999].map(t =>
  api.shutterTimes(t, 8, 4, 1, [1, 1.02], 2))));
""")
        for samples in rows:
            self.assertTrue(all(1 <= s < 1.02 for s in samples))
            for actual, expected in zip(samples, [1.0025, 1.0075, 1.0125, 1.0175]):
                self.assertAlmostEqual(actual, expected)

    def test_cut_aware_samples_respect_end_and_zero_shutter(self):
        rows = self.node("""
console.log(JSON.stringify([
  api.shutterTimes(0, 8, 4, 1, [1], 2),
  api.shutterTimes(1.99999, 8, 4, 1, [1], 2),
  api.shutterTimes(1.4, 8, 4, 0, [1], 2),
  api.shutterTimes(1.4, 8, 1, 1, [1], 2),
  api.shutterTimes(1.09, 8, 4, 1, [1, 1.2], 1.1),
  api.shutterTimes(1.99999, 8, 4, 0, [1], 2)
]));
""")
        self.assertTrue(all(0 <= s < 1 for s in rows[0]))
        self.assertTrue(all(1 <= s <= 1.9999 for s in rows[1]))
        self.assertEqual(rows[2], [1.4] * 4)
        self.assertEqual(rows[3], [1.4])
        self.assertTrue(all(1 <= s <= 1.0999 for s in rows[4]))
        self.assertEqual(rows[5], [1.9999] * 4)

    def test_validate_config_refuses_invalid_cuts(self):
        result = self.node("""
const base = {duration:2, fps:8, width:32, height:32, render_files:['reel/index.html']};
const bad = [[1, .5], [1, 1], [0], [-.1], [2], [3], ['1'], [true], [null],
             [NaN], [Infinity], null, '1', 1, {}, Array.from({length:4097}, (_,i)=>(i+1)/4098)];
const refused = bad.map(cuts => {
  try {api.validateConfig({...base,cuts});return false;} catch(e) {return /cuts/.test(e.message);}
});
for(const config of [base, {...base,cuts:[]}, {...base,cuts:[.5,1,1.5]},
                    {...base,cuts:Array.from({length:4096},(_,i)=>(i+1)/4097)}])
  api.validateConfig(Object.freeze(config));
console.log(JSON.stringify(refused));
""")
        self.assertTrue(all(result), result)


@unittest.skipUnless(os.environ.get("SHOW_REEL_HOST_TOOLS"), "explicit host browser/FFmpeg opt-in")
class HostCutRenderTests(unittest.TestCase):
    def test_hard_cut_with_four_samples_has_no_red_blue_ghosting(self):
        tools = json.loads(Path(os.environ["SHOW_REEL_HOST_TOOLS"]).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dest = root / "project"
            init = subprocess.run(
                [sys.executable, str(KIT / "scripts/new_project.py"), str(dest),
                 "--font", os.environ["SHOW_REEL_FONT"], "--duration", "2", "--fps", "8",
                 "--width", "320", "--height", "180"],
                capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            scenes = dest / "reel/scenes.js"
            scenes.write_text(scenes.read_text(encoding="utf-8") + """
for (const [id, start, end, color] of [['red',0,1,'#ff0000'], ['blue',1,2,'#0000ff']])
  REEL.scene({id, start, end, setup(el){el.style.background=color;}, render(){}});
""", encoding="utf-8")
            config_path = dest / "project.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["cuts"] = [1.0]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            out = root / "cut.mp4"
            command = [tools["binaries"]["node"], str(KIT / "scripts/render.mjs"),
                       "--dir", str(dest / "reel"), "--out", str(out),
                       "--browser", tools["chrome"], "--dependency-root", tools["available_puppeteer_core"]["root"],
                       "--ffmpeg", tools["binaries"]["ffmpeg"], "--ffprobe", tools["binaries"]["ffprobe"],
                       "--sub", "4", "--shutter", "1", "--crf", "0"]
            if tools.get("color"):
                command += ["--color", tools["color"]]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=100)
            self.assertEqual(result.returncode, 0, result.stderr)
            decoded = subprocess.run(
                [tools["binaries"]["ffmpeg"], "-v", "error", "-nostdin", "-i", str(out),
                 "-vf", "select='eq(n,7)+eq(n,8)',crop=2:2:160:90", "-fps_mode", "passthrough",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True, timeout=20,
            )
            self.assertEqual(decoded.returncode, 0, decoded.stderr)
            self.assertEqual(len(decoded.stdout), 24)
            for index, expected in enumerate(((255, 0, 0), (0, 0, 255))):
                frame = decoded.stdout[index * 12:(index + 1) * 12]
                for offset in range(0, 12, 3):
                    self.assertTrue(all(abs(a - b) <= 3 for a, b in zip(frame[offset:offset + 3], expected)),
                                    f"frame {7 + index}: {tuple(frame[offset:offset + 3])}, expected {expected}")
            receipt = json.loads(Path(str(out) + ".receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["decoded_frames"], 16)
            self.assertEqual(receipt["cuts"], 1)


if __name__ == "__main__":
    unittest.main()
