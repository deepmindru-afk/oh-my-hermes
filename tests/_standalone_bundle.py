from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


_STANDALONE_BUNDLE_MODULE_NAME = "_test_omh_standalone_bundle"


def bundle_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "plugin_bundle" / "omh"


def standalone_bundle_module_names() -> tuple[str, ...]:
    """Every module in the bundle, read off the directory rather than listed.

    The subject has to be derived. A written list is a list of the modules
    someone thought of, and the module this exists to catch -- a new one whose
    `omh.*` import is not guarded -- is the one nobody thought of. Until
    #1623 the standalone load covered `awareness` alone, so its real subject
    was whatever `awareness` happened to pull in, and three modules that could
    not import at all sat outside it.

    `__init__.py` is left out because it is the entry the loader starts from;
    each subpackage's `__init__` appears here as the package itself.
    """
    bundle = bundle_dir()
    names: list[str] = []
    for path in sorted(bundle.rglob("*.py")):
        parts = path.relative_to(bundle).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    return tuple(names)


def _blocked_import() -> Callable[..., ModuleType]:
    """An import hook that answers as a host with no ``omh`` package would.

    It raises plain `ImportError`, not `ModuleNotFoundError`: both shapes
    reach a plugin host -- the installed package really missing, and Hermes'
    loader handing back a module it failed to exec -- and a guard written for
    only the narrower one is what #1623 was.
    """
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> ModuleType:
        if name == "omh" or name.startswith("omh."):
            raise ImportError(
                f"standalone plugin host has no installed omh package (blocked import of {name!r})",
                name=name,
            )
        return real_import(name, *args, **kwargs)

    return blocked


def _bundle_package() -> tuple[ModuleType, importlib.machinery.ModuleSpec]:
    for name in list(sys.modules):
        if name == _STANDALONE_BUNDLE_MODULE_NAME or name.startswith(
            f"{_STANDALONE_BUNDLE_MODULE_NAME}."
        ):
            sys.modules.pop(name, None)

    directory = bundle_dir()
    spec = importlib.util.spec_from_file_location(
        _STANDALONE_BUNDLE_MODULE_NAME,
        directory / "__init__.py",
        submodule_search_locations=[str(directory)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load the vendored plugin bundle standalone")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_STANDALONE_BUNDLE_MODULE_NAME] = module
    return module, spec


def load_standalone_bundle(names: Iterable[str]) -> dict[str, ModuleType]:
    """Import the named bundle modules with no access to ``omh``.

    The bundle is copied into ``$HERMES_HOME/plugins/omh`` and loaded by
    Hermes' interpreter, which has no reason to have the ``omh`` package on
    its path, so every module in it has to import from inside itself.
    """
    module, spec = _bundle_package()
    loader = spec.loader
    assert loader is not None
    with patch("builtins.__import__", side_effect=_blocked_import()):
        loader.exec_module(module)
        return {
            name: importlib.import_module(f"{_STANDALONE_BUNDLE_MODULE_NAME}.{name}")
            for name in names
        }


def standalone_bundle_import_failures() -> dict[str, str]:
    """Import every bundle module standalone; report the ones that cannot."""
    module, spec = _bundle_package()
    loader = spec.loader
    assert loader is not None
    failures: dict[str, str] = {}
    with patch("builtins.__import__", side_effect=_blocked_import()):
        loader.exec_module(module)
        for name in standalone_bundle_module_names():
            try:
                importlib.import_module(f"{_STANDALONE_BUNDLE_MODULE_NAME}.{name}")
            except Exception as error:  # noqa: BLE001 - any failure to import is the finding
                failures[name] = f"{type(error).__name__}: {error}"
    return failures


def _load_standalone_bundle_awareness():
    """Load bundled awareness without access to the installed ``omh`` package."""
    return load_standalone_bundle(("awareness",))["awareness"]


_NO_PACKAGE_HOST_PRELUDE = """
import importlib.util
import json
import sys

bundle = sys.argv[1]
sys.path[:0] = sys.argv[2:]
spec = importlib.util.spec_from_file_location(
    "hermes_plugin_omh", bundle + "/__init__.py", submodule_search_locations=[bundle]
)
module = importlib.util.module_from_spec(spec)
sys.modules["hermes_plugin_omh"] = module
spec.loader.exec_module(module)
omh_spec = importlib.util.find_spec("omh") is not None
"""


def run_in_no_package_host(
    code: str,
    *,
    cwd: Path,
    extra_paths: Iterable[Path] = (),
    python: Path | None = None,
    run_site: bool = False,
) -> dict:
    """Run `code` in a real interpreter with no `omh` package; return its JSON.

    `-S` keeps the editable install's `.pth` out and `-P` keeps the cwd out,
    so the child is a Hermes host with no `omh` package at all, the state a
    `sys.modules` patch in this process can only imitate. The bundle loads
    under a foreign name, as Hermes' loaders give it one. `code` runs after
    the bundle is importable as `hermes_plugin_omh`, sees `omh_spec`, and must
    print one JSON document. `extra_paths` go first on the child's path, which
    is how a test puts a stray or skewed `omh` in front of it.

    `python` runs another interpreter (a test-made venv) in place of this
    one, and `run_site` lets `site` run in it, which a host whose own
    site directory holds a package it later takes off the path needs (#2415).
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH"}
    }
    result = subprocess.run(
        [
            str(python or sys.executable),
            *([] if run_site else ["-S"]),
            "-P",
            "-c",
            _NO_PACKAGE_HOST_PRELUDE + code,
            str(bundle_dir()),
            *(str(path) for path in extra_paths),
        ],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"no-package host exited {result.returncode}: {result.stderr}")
    return json.loads(result.stdout)
