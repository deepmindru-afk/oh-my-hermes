"""A unit never inherits the dispatcher's stdin (#2402).

`codex exec "<prompt>"` (0.165) reads a piped stdin to EOF even when the
prompt travels in argv, and appends it as a `<stdin>` block. An agent shell
starts `omh coding run` with an open pipe on its stdin that it never closes,
so a unit that inherited that pipe waited for an EOF that never came and was
killed at its deadline (`failure_kind: timeout`, exit 124).

Each test holds a real, never-closed pipe on the dispatcher process's stdin,
the shape the reporter's agent shell produced; a unit that reads stdin to EOF
has to see the null device and finish at once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from tempfile import TemporaryDirectory
import textwrap
import unittest

from _fanout_host_fence import host_cannot_fence_fanout
from _local_package import load_local_package
from _platform_support import requires_posix

load_local_package()

_TESTS = Path(__file__).resolve().parent
_SOURCE = _TESTS.parent / "src"
# Far below the unit deadlines below: a dispatcher that is still running here
# is the inherited-pipe hang, not a slow host.
_DISPATCHER_DEADLINE_SECONDS = 120


def _hold_stdin_open(argv: list[str], *, env: dict[str, str], cwd: Path) -> tuple[int, str, str]:
    """Run `argv` with an open stdin pipe that is written to and never closed.

    `communicate()` would close the pipe before waiting, which is exactly the
    EOF an agent shell never sends, so the child is awaited with its stdin
    still open and its output captured through files.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        with subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=out, stderr=err, env=env, cwd=str(cwd),
        ) as dispatcher:
            try:
                assert dispatcher.stdin is not None
                # Bytes a unit must never see: the operator's shell owns them.
                dispatcher.stdin.write(b"operator shell input\n")
                dispatcher.stdin.flush()
                returncode = dispatcher.wait(timeout=_DISPATCHER_DEADLINE_SECONDS)
            finally:
                if dispatcher.poll() is None:
                    dispatcher.kill()
                    dispatcher.wait(timeout=30)
        out.seek(0)
        err.seek(0)
        return (
            returncode,
            out.read().decode("utf-8", "replace"),
            err.read().decode("utf-8", "replace"),
        )


class SignalSafeRunnerStdinTests(unittest.TestCase):
    def test_a_unit_reading_stdin_to_eof_does_not_wait_on_the_dispatchers_pipe(self) -> None:
        dispatcher = textwrap.dedent(
            """
            import subprocess, sys
            from _local_package import load_local_package
            load_local_package()
            from omh.coding.fanout_dispatch import signal_safe_unit_runner

            reader = [sys.executable, "-c", "import sys; data = sys.stdin.read(); print('unit stdin bytes', len(data))"]
            try:
                completed = signal_safe_unit_runner(reader, text=True, capture_output=True, timeout=15)
            except subprocess.TimeoutExpired:
                print("unit timed out waiting for stdin EOF")
                sys.exit(3)
            print(completed.stdout.strip())
            sys.exit(completed.returncode)
            """
        )
        with TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(_TESTS)}
            code, stdout, stderr = _hold_stdin_open(
                [sys.executable, "-c", dispatcher], env=env, cwd=Path(tmp)
            )

        self.assertEqual(code, 0, stdout + stderr)
        self.assertEqual(stdout.strip(), "unit stdin bytes 0")

    def test_explicit_input_still_reaches_the_child(self) -> None:
        # The workspace preflight hands git its blob through `input` (#2040);
        # the null-device default must not take that pipe away.
        from omh.coding.fanout_dispatch import signal_safe_unit_runner

        completed = signal_safe_unit_runner(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper(), end='')"],
            text=True,
            capture_output=True,
            timeout=30,
            input="blob\n",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "BLOB\n")


_FAKE_CODEX = """#!/bin/sh
# Stands in for codex-cli 0.165: with a prompt in argv, a piped stdin is
# still read to EOF and appended as a <stdin> block.
case "$1" in
  --version) echo "codex-cli 0.165.0"; exit 0 ;;
esac
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  echo "Usage: codex exec [OPTIONS] [PROMPT]"
  exit 0
fi
cat > /dev/null
exit 0
"""


@requires_posix
class CodingRunStdinTests(unittest.TestCase):
    """The reporter's path: `omh coding run --owner codex` under an agent shell."""

    def test_coding_run_from_a_shell_with_an_open_stdin_pipe_completes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            codex = bin_dir / "codex"
            codex.write_text(_FAKE_CODEX, encoding="utf-8")
            codex.chmod(0o755)
            home = root / "home"
            # The codex owner's state directory is one of the fence's write roots.
            (home / ".codex").mkdir(parents=True)
            repo = root / "repo"
            repo.mkdir()
            for argv in (
                ["git", "init", "-q"],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"],
            ):
                subprocess.run(argv, cwd=str(repo), check=True, capture_output=True, timeout=60)
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                "HOME": str(home),
                "OMH_HOME": str(root / "omh"),
                "HERMES_HOME": str(root / "hermes"),
                "PYTHONPATH": str(_SOURCE),
            }
            env.pop("CODEX_HOME", None)
            argv = [
                sys.executable, "-m", "omh.cli",
                "--omh-home", str(root / "omh"), "--hermes-home", str(root / "hermes"),
                "coding", "run", "--owner", "codex", "--goal", "Write a note.",
                "--repo-root", str(repo), "--timeout", "30",
            ]
            if host_cannot_fence_fanout():
                argv.append("--allow-unconfined")

            code, stdout, stderr = _hold_stdin_open(argv, env=env, cwd=root)

        self.assertTrue(stdout.strip(), stderr)
        unit = json.loads(stdout)["dispatch"]["units"][0]
        observed = {key: unit.get(key) for key in ("status", "failure_kind", "exit_code")}
        self.assertEqual(observed, {"status": "completed", "failure_kind": None, "exit_code": 0}, stderr[-2000:])
        self.assertEqual(code, 0, stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
