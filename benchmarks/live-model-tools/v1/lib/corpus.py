from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import random
import shutil
from typing import Any

from common import digest, tree_digest, write_json

DEVELOPMENT_SEEDS = (7919,)
EVALUATION_SEEDS = (104729, 130363, 155921)
TEMPLATES = (
    ("RENAME", "edit"), ("BUGFIX", "edit"),
    ("PRECEDENCE", "read"), ("CALLFLOW", "read"),
    ("REFERENCES", "search"), ("PREDICATE", "search"),
    ("DEFINITION", "lsp"), ("DIAGNOSTICS", "lsp"),
    ("SCALE", "routing"), ("EXPLICIT", "routing"),
)
# Separate split for the omh-lsp A/B: kept out of development/evaluation so the
# pinned corpus digest and manifest stay byte-identical.
LSP_AB_SPLIT = "lsp-ab"
LSP_AB_TEMPLATES = (("RENAME-ALIAS", "edit"), ("RENAME-SHADOW", "edit"))
# Separate split for the round-end checklist A/B, kept out of the pinned corpus the same way.
CONSISTENCY_AB_SPLIT = "consistency-ab"
CONSISTENCY_AB_TEMPLATES = (("DUPLICATE", "edit"), ("SCOPE-TRAP", "edit"), ("DECOY", "edit"))

@dataclass(frozen=True)
class Instance:
    split: str
    template_id: str
    instance_id: str
    task_class: str
    seed: int
    prompt: str
    fixture_digest: str


def all_specs(split: str) -> list[tuple[str, str, int]]:
    if split == LSP_AB_SPLIT:
        return [(f"L-{name}", task_class, seed) for name, task_class in LSP_AB_TEMPLATES for seed in EVALUATION_SEEDS]
    if split == CONSISTENCY_AB_SPLIT:
        return [(f"C-{name}", task_class, seed) for name, task_class in CONSISTENCY_AB_TEMPLATES for seed in EVALUATION_SEEDS]
    seeds = DEVELOPMENT_SEEDS if split == "development" else EVALUATION_SEEDS
    prefix = "D" if split == "development" else "E"
    return [(f"{prefix}-{name}", task_class, seed) for name, task_class in TEMPLATES for seed in seeds]


def _project_files(seed: int) -> dict[str, str]:
    randomizer = random.Random(seed)
    suffix = randomizer.choice(("Amber", "Cobalt", "Juniper", "Quartz")) + str(seed % 97)
    decision = f"RouteDecision{suffix}"
    padding = "\n" * randomizer.randint(0, 3)
    return {
        "pyproject.toml": "[project]\nname='benchapp-synthetic'\nversion='0.0.1'\nrequires-python='>=3.11'\n",
        "src/benchapp/__init__.py": "",
        "src/benchapp/retries.py": "DEFAULT_RETRY_LIMIT = 2\n\ndef retry_budget() -> int:\n    return DEFAULT_RETRY_LIMIT\n",
        "tests/test_retries.py": "from benchapp.retries import DEFAULT_RETRY_LIMIT, retry_budget\n\ndef test_retry_budget():\n    assert DEFAULT_RETRY_LIMIT == retry_budget() == 2\n",
        "src/benchapp/provider_select.py": (
            "from dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass Provider:\n"
            "    name: str\n    region: str\n    enabled: bool\n\nPROVIDERS = [\n"
            "    Provider('archive', 'us', False),\n    Provider('primary', 'us', True),\n"
            "    Provider('europe', 'eu', True),\n]\n\ndef select_provider(region: str | None) -> Provider | None:\n"
            "    # BUG: disabled entries and None are not handled.\n    for provider in PROVIDERS:\n"
            "        if provider.region == region:\n            return provider\n    return PROVIDERS[0]\n"
        ),
        "src/benchapp/config.py": (
            "DEFAULT_TIMEOUT_SECONDS = 15\nDEFAULT_RETRIES = 2\n\ndef load_config(cli, environment, file_config):\n"
            "    if cli is not None:\n        return cli\n    if environment is not None:\n        return environment\n"
            "    if file_config is not None:\n        return file_config\n    return DEFAULT_TIMEOUT_SECONDS\n"
            "\ndef validate(value):\n    if value is None:\n        raise ValueError('missing configuration')\n"
            "    if value < 0:\n        raise ValueError('negative configuration')\n"
        ),
        "src/benchapp/flow.py": (
            "class Request: pass\nclass Result: pass\n\nclass Policy:\n    def apply(self, request):\n"
            "        if request is None:\n            raise ValueError('invalid_request')\n        return Result()\n\n"
            "class Router:\n    def route(self, request):\n        try:\n            return Policy().apply(request)\n"
            "        except ValueError:\n            raise RuntimeError('policy_rejected')\n"
        ),
        "src/benchapp/routes.py": (
            f"from dataclasses import dataclass\n{padding}@dataclass\nclass {decision}:\n    name: str\n\n"
            f"def choose() -> {decision}:\n    return {decision}('safe')\n\n"
            f"def consume(value: {decision}) -> str:\n    return value.name\n"
            f"\n# decoy {decision}\nDECOY = '{decision}'\n"
        ),
        "src/benchapp/fallbacks.py": (
            "def fetch_primary(client):\n    try:\n        return client.fetch()\n    except RuntimeError:\n"
            "        return {'source': 'fallback'}\n\ndef strict_fetch(client):\n    return client.fetch()\n"
            "\ndef fetch_secondary(client):\n    try:\n        return client.fetch_secondary()\n    except ConnectionError:\n"
            "        return {'source': 'fallback'}\n"
        ),
        "src/benchapp/diagnostic.py": "def total(values: list[int]) -> int:\n    return sum(values)\n\nBROKEN_NAME: int = missing_value\n",
        "tools/route_probe.py": (
            "import argparse, json\np=argparse.ArgumentParser()\np.add_argument('--scale')\np.add_argument('--model')\n"
            "p.add_argument('--available', action='store_true')\na=p.parse_args()\n"
            "if a.model and not a.available:\n out={'status':'choice_required','provenance':'explicit_request','selected_model':None,'effort':'none','attempted_stages':['explicit'],'reason_codes':['explicit_model_unavailable']}\n"
            "else:\n out={'status':'selected','provenance':'task_scale','selected_model':a.model or ('deep-model' if a.scale=='large' else 'fast-model'),'effort':'high' if a.scale=='large' else 'low','attempted_stages':['scale']}\n"
            "print(json.dumps(out, sort_keys=True))\n"
        ),
    }


def _rename_alias_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """A module function re-exported, aliased, and module-accessed, next to a same-named method,
    a log string, and a comment: a text replace breaks the method/string/comment, a semantic
    rename does not."""
    randomizer = random.Random(seed)
    factor = randomizer.randint(2, 9)
    legacy = 100 + randomizer.randint(0, 50)
    alias = randomizer.choice(("cq", "cq_fn", "quota_of"))
    padding = "\n" * randomizer.randint(0, 2)
    comment = "    # compute_quota is the single source of quota numbers."
    log_line = '        LOG.warning("compute_quota failed")'
    files = {
        "src/benchapp/quota.py": (
            f'"""Quota helpers."""\n{padding}\n\ndef compute_quota(units: int) -> int:\n'
            f"    if units < 0:\n        raise ValueError('negative units')\n    return units * {factor}\n\n\n"
            f"class LegacyPlanner:\n    def compute_quota(self) -> int:\n        return {legacy}\n"
        ),
        "src/benchapp/api.py": 'from .quota import compute_quota\n\n__all__ = ["compute_quota"]\n',
        "src/benchapp/service.py": (
            f"from benchapp.quota import compute_quota as {alias}\n\n\n"
            f"def plan_units(units: int) -> int:\n    return {alias}(units) + 1\n"
        ),
        "src/benchapp/report.py": (
            "import logging\n\nfrom benchapp import quota\n\nLOG = logging.getLogger(__name__)\n\n\n"
            f"def report(units: int) -> str:\n{comment}\n    try:\n"
            f'        return f"quota={{quota.compute_quota(units)}}"\n    except ValueError:\n{log_line}\n        raise\n'
        ),
        "tests/test_quota.py": (
            "from benchapp.api import compute_quota\nfrom benchapp.quota import LegacyPlanner\n"
            "from benchapp.report import report\nfrom benchapp.service import plan_units\n\n\n"
            f"def test_compute_quota():\n    assert compute_quota(3) == {3 * factor}\n\n\n"
            f"def test_paths_agree():\n    assert plan_units(2) == {2 * factor + 1}\n"
            f"    assert report(4) == 'quota={4 * factor}'\n\n\n"
            f"def test_legacy_planner_untouched():\n    assert LegacyPlanner().compute_quota() == {legacy}\n"
        ),
    }
    golden = {
        "allowed_paths": sorted(files),
        "factor": factor,
        "legacy": legacy,
        "min_tests": 3,
        "preserved_lines": {"src/benchapp/report.py": [comment, log_line]},
    }
    return files, golden


def _rename_shadow_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """A function whose name is shadowed by a callable parameter (and its keyword at a call site):
    a text replace renames the parameter, a semantic rename does not."""
    randomizer = random.Random(seed)
    cap = randomizer.randint(50, 500)
    padding = "\n" * randomizer.randint(0, 2)
    files = {
        "src/benchapp/limits.py": (
            f'"""Limit parsing."""\n{padding}\n\ndef parse_limit(text: str) -> int:\n'
            "    value = int(text.strip())\n    if value < 0:\n        raise ValueError('negative limit')\n"
            f"    return min(value, {cap})\n"
        ),
        "src/benchapp/cli.py": (
            "from benchapp.limits import parse_limit\n\n\n"
            "def main(argv: list[str]) -> int:\n    return parse_limit(argv[0])\n"
        ),
        "src/benchapp/batch.py": (
            "from benchapp import limits\n\n\n"
            "def apply_all(values: list[str], parse_limit) -> list[int]:\n"
            "    return [parse_limit(value) for value in values]\n\n\n"
            "def default_batch(values: list[str]) -> list[int]:\n    return apply_all(values, limits.parse_limit)\n"
        ),
        "tests/test_limits.py": (
            "from benchapp.batch import apply_all, default_batch\nfrom benchapp.cli import main\n"
            "from benchapp.limits import parse_limit\n\n\n"
            f"def test_parse_limit():\n    assert parse_limit(' 7 ') == 7\n    assert parse_limit('{cap + 1}') == {cap}\n\n\n"
            "def test_callers():\n    assert main(['3']) == 3\n    assert default_batch(['1', '2']) == [1, 2]\n"
            "    assert apply_all(['4'], parse_limit=lambda text: int(text) * 10) == [40]\n"
        ),
    }
    golden = {"allowed_paths": sorted(files), "cap": cap, "min_tests": 2}
    return files, golden


_LSP_AB_FIXTURES = {"RENAME-ALIAS": _rename_alias_fixture, "RENAME-SHADOW": _rename_shadow_fixture}


def _duplicate_params(seed: int) -> dict[str, Any]:
    randomizer = random.Random(seed)
    first, second = randomizer.choice((("invoice", "receipt"), ("orders", "refunds"), ("billing", "statements")))
    whole = randomizer.randint(10, 99)
    half = randomizer.choice(("005", "025", "045"))
    return {"first": first, "second": second, "amount": f"{whole}.{half}", "rounded": f"{whole}.{int(half[:2]) + 1:02d}",
            "padding": "\n" * randomizer.randint(0, 2)}


def _duplicate_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """One rounding bug in two copies of a helper: the task names the first copy's symptom, and the
    second module says it is kept in step with the first. Only a fix applied to both passes."""
    params = _duplicate_params(seed)
    first, second = params["first"], params["second"]
    files = {
        f"src/benchapp/{first}.py": (
            f'"""{first.capitalize()} totals."""\n{params["padding"]}from decimal import Decimal, ROUND_HALF_EVEN\n\n'
            'CENT = Decimal("0.01")\n\n\n'
            "def _to_cents(amount: str) -> Decimal:\n    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_EVEN)\n\n\n"
            f"def {first}_total(amounts: list[str]) -> Decimal:\n"
            '    return sum((_to_cents(amount) for amount in amounts), Decimal("0.00"))\n'
        ),
        f"src/benchapp/{second}.py": (
            f'"""{second.capitalize()} totals. Cent rounding here is kept in step with {first}.py."""\n'
            "import decimal\n\n\n"
            "def _to_cents(amount: str) -> decimal.Decimal:\n"
            '    return decimal.Decimal(amount).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_EVEN)\n\n\n'
            f'def {second}_total(amounts: list[str], fee: str = "0") -> decimal.Decimal:\n'
            '    return _to_cents(fee) + sum((_to_cents(amount) for amount in amounts), decimal.Decimal("0.00"))\n'
        ),
        f"tests/test_{first}.py": (
            f"from decimal import Decimal\n\nfrom benchapp.{first} import {first}_total\n\n\n"
            f'def test_{first}_total_whole_cents():\n    assert {first}_total(["1.10", "2.20"]) == Decimal("3.30")\n'
        ),
    }
    golden = {
        "allowed_paths": [f"src/benchapp/{first}.py", f"src/benchapp/{second}.py"],
        "allowed_prefixes": ["tests/"],
        **{key: params[key] for key in ("first", "second", "amount", "rounded")},
        "min_tests": 1,
    }
    return files, golden


def _scope_trap_params(seed: int) -> dict[str, Any]:
    randomizer = random.Random(seed)
    return {"seconds": randomizer.choice((45, 90, 150)), "padding": "\n" * randomizer.randint(0, 2)}


def _scope_trap_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """A one-line bug next to two tempting edits nobody asked for: an unused import and a size
    table whose `kib` entry looks wrong. Only the requested fix, with the rest left alone, passes."""
    params = _scope_trap_params(seed)
    files = {
        "src/benchapp/units.py": (
            f'"""Unit parsing for the scheduler and the storage report."""\n{params["padding"]}import os\nimport re\n\n'
            '_DURATION = re.compile(r"^(\\d+)([smh])$")\n'
            '_DURATION_SECONDS = {"s": 60, "m": 60, "h": 3600}\n\n\n'
            "def parse_duration(text: str) -> int:\n"
            "    \"\"\"Seconds in a duration such as '90s', '5m' or '2h'.\"\"\"\n"
            "    match = _DURATION.match(text.strip())\n    if match is None:\n"
            '        raise ValueError(f"bad duration: {text!r}")\n'
            "    return int(match.group(1)) * _DURATION_SECONDS[match.group(2)]\n\n\n"
            "def parse_size(text: str) -> int:\n"
            "    \"\"\"Bytes in a size such as '512b', '4kb' or '2kib'.\"\"\"\n"
            '    number, unit = re.match(r"^(\\d+)([a-z]+)$", text.strip().lower()).groups()\n'
            '    return int(number) * {"b": 1, "kb": 1000, "kib": 1000, "mb": 1000**2, "mib": 1000**2}[unit]\n'
        ),
        "tests/test_units.py": (
            "from benchapp.units import parse_duration, parse_size\n\n\n"
            'def test_minutes_and_hours():\n    assert parse_duration("5m") == 300\n    assert parse_duration("2h") == 7200\n\n\n'
            'def test_sizes():\n    assert parse_size("4kb") == 4000\n'
        ),
    }
    golden = {
        "allowed_paths": ["src/benchapp/units.py"],
        "allowed_prefixes": ["tests/"],
        "seconds": params["seconds"],
        # Top-level statements the task does not touch; each must survive unchanged.
        "untouched_nodes": [ast.dump(node) for node in ast.parse(files["src/benchapp/units.py"]).body
                            if not _defines(node, {"parse_duration", "_DURATION_SECONDS", "_DURATION"})],
        "min_tests": 2,
    }
    return files, golden


def _defines(node: ast.stmt, names: set[str]) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name in names
    if isinstance(node, ast.Assign):
        return any(isinstance(target, ast.Name) and target.id in names for target in node.targets)
    return False


def _decoy_params(seed: int) -> dict[str, Any]:
    randomizer = random.Random(seed + 1)
    first, second = randomizer.choice((("invoice", "tax"), ("orders", "vat"), ("billing", "duty")))
    whole = randomizer.randint(10, 99)
    half = randomizer.choice(("005", "025", "045"))
    return {"first": first, "second": second, "amount": f"{whole}.{half}", "rounded": f"{whole}.{int(half[:2]) + 1:02d}",
            "padding": "\n" * randomizer.randint(0, 2)}


def _decoy_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """The negative case for C-DUPLICATE: the same rounding bug in one module, next to a look-alike
    helper whose half-to-even rounding is required and documented. Changing the look-alike fails."""
    params = _decoy_params(seed)
    first, second = params["first"], params["second"]
    files = {
        f"src/benchapp/{first}.py": (
            f'"""{first.capitalize()} totals."""\n{params["padding"]}from decimal import Decimal, ROUND_HALF_EVEN\n\n'
            'CENT = Decimal("0.01")\n\n\n'
            "def _to_cents(amount: str) -> Decimal:\n    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_EVEN)\n\n\n"
            f"def {first}_total(amounts: list[str]) -> Decimal:\n"
            '    return sum((_to_cents(amount) for amount in amounts), Decimal("0.00"))\n'
        ),
        f"src/benchapp/{second}.py": (
            f'"""{second.capitalize()} amounts. The {second} rules require half-to-even (banker\'s) rounding to the cent,\n'
            f'so this rounding is intentionally different from {first}.py."""\n'
            "import decimal\n\n\n"
            "def _to_cents(amount: str) -> decimal.Decimal:\n"
            '    return decimal.Decimal(amount).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_EVEN)\n\n\n'
            f"def {second}_total(amounts: list[str]) -> decimal.Decimal:\n"
            '    return sum((_to_cents(amount) for amount in amounts), decimal.Decimal("0.00"))\n'
        ),
        f"tests/test_{first}.py": (
            f"from decimal import Decimal\n\nfrom benchapp.{first} import {first}_total\n\n\n"
            f'def test_{first}_total_whole_cents():\n    assert {first}_total(["1.10", "2.20"]) == Decimal("3.30")\n'
        ),
        f"tests/test_{second}.py": (
            f"from decimal import Decimal\n\nfrom benchapp.{second} import {second}_total\n\n\n"
            f'def test_{second}_rounds_half_to_even():\n    assert {second}_total(["0.125"]) == Decimal("0.12")\n'
        ),
    }
    golden = {
        "allowed_paths": [f"src/benchapp/{first}.py", f"src/benchapp/{second}.py"],
        "allowed_prefixes": ["tests/"],
        **{key: params[key] for key in ("first", "second", "amount", "rounded")},
        "decoy_nodes": [ast.dump(node) for node in ast.parse(files[f"src/benchapp/{second}.py"]).body],
        "min_tests": 1,
    }
    return files, golden


_CONSISTENCY_AB_FIXTURES = {"DUPLICATE": _duplicate_fixture, "SCOPE-TRAP": _scope_trap_fixture, "DECOY": _decoy_fixture}


def _prompt(template_id: str, task_class: str, seed: int, decision: str) -> str:
    name = template_id.split("-", 1)[1]
    prompts = {
        "RENAME": "Rename DEFAULT_RETRY_LIMIT to MAX_RETRY_ATTEMPTS in production and tests without changing behavior. Return JSON with changed_paths.",
        "BUGFIX": "Fix select_provider(region) to return the first enabled matching provider and return None for None/no match. Add regression tests. Return JSON with changed_paths.",
        "PRECEDENCE": "Read the configuration implementation. Return JSON facts for precedence, default_timeout_seconds, default_retries, and failure_states, with path/line citations.",
        "CALLFLOW": "Trace Request -> Router -> Policy -> Result and terminal errors. Return JSON containing ordered edges, error_states, and path/line citations.",
        "REFERENCES": f"Find every semantic definition/reference of {decision}, excluding comments and strings. Return exact JSON locations as path, line, kind.",
        "PREDICATE": "Find every function that converts a provider failure to a fallback result. Return exact JSON locations as path, line, kind.",
        "DEFINITION": f"Use LSP definition and references for {decision} at its use in choose(). Return exact JSON locations and tools_used.",
        "DIAGNOSTICS": "Use LSP diagnostics, minimally fix all name/type errors in diagnostic.py, then use diagnostics again. Return JSON diagnostics and tools_used.",
        "SCALE": "For a large coding task invoke tools/route_probe.py with the correct arguments and return its exact route JSON.",
        "EXPLICIT": "Request explicit model unavailable-model from tools/route_probe.py. Preserve it: never substitute. Return its exact route JSON.",
        "RENAME-ALIAS": "Rename the module-level function compute_quota in src/benchapp/quota.py to allocate_quota everywhere it is referenced, without changing behavior. Keep the unrelated LegacyPlanner.compute_quota method, string literals and comments unchanged. Return JSON with changed_paths.",
        "RENAME-SHADOW": "Rename the function parse_limit defined in src/benchapp/limits.py to read_limit at its definition and every reference to that function, without changing behavior. Parameters or local variables that merely share the name are not references and must stay. Return JSON with changed_paths.",
    }
    if name == "DUPLICATE":
        params = _duplicate_params(seed)
        return (f"Cent rounding is wrong: {params['first']}_total(['{params['amount']}']) returns a half cent rounded down, "
                f"but money must round half up, to {params['rounded']}. Fix it and add a regression test. Return JSON with changed_paths.")
    if name == "DECOY":
        params = _decoy_params(seed)
        return (f"{params['first'].capitalize()} totals round a half cent down: {params['first']}_total(['{params['amount']}']) "
                f"returns the half cent rounded down, but {params['first']} totals must round half up, to {params['rounded']}. "
                "Fix it and add a regression test. Return JSON with changed_paths.")
    if name == "SCOPE-TRAP":
        seconds = _scope_trap_params(seed)["seconds"]
        return (f"parse_duration('{seconds}s') in src/benchapp/units.py returns {seconds * 60}, but it must return {seconds} "
                "(seconds). Fix it and add a regression test. Return JSON with changed_paths.")
    return prompts[name]


def _golden(template_id: str, files: dict[str, str], decision: str) -> dict[str, Any]:
    name = template_id.split("-", 1)[1]
    routes_lines = files["src/benchapp/routes.py"].splitlines()
    refs = []
    for line_no, line in enumerate(routes_lines, 1):
        if decision in line and not line.lstrip().startswith("#") and "DECOY" not in line:
            kind = "definition" if line.startswith("class ") else "reference"
            refs.append({"path": "src/benchapp/routes.py", "line": line_no, "kind": kind})
    goldens: dict[str, dict[str, Any]] = {
        "PRECEDENCE": {"facts": {"precedence": ["cli", "environment", "file", "default"], "default_timeout_seconds": 15, "default_retries": 2, "failure_states": ["missing configuration", "negative configuration"]}, "citations": [{"path": "src/benchapp/config.py", "line": 1}]},
        "CALLFLOW": {"edges": [["Request", "Router"], ["Router", "Policy"], ["Policy", "Result"]], "error_states": ["invalid_request", "policy_rejected"], "citations": [{"path": "src/benchapp/flow.py", "line": 1}]},
        "REFERENCES": {"locations": refs},
        "PREDICATE": {"locations": [{"path": "src/benchapp/fallbacks.py", "line": 1, "kind": "function"}, {"path": "src/benchapp/fallbacks.py", "line": 10, "kind": "function"}]},
        "DEFINITION": {"locations": refs, "required_tools": ["lsp_goto_definition", "lsp_find_references"]},
        "DIAGNOSTICS": {"diagnostics": [], "required_tools": ["lsp_diagnostics"], "allowed_paths": ["src/benchapp/diagnostic.py"]},
        "SCALE": {"route": {"status": "selected", "provenance": "task_scale", "selected_model": "deep-model", "effort": "high", "attempted_stages": ["scale"]}},
        "EXPLICIT": {"route": {"status": "choice_required", "provenance": "explicit_request", "selected_model": None, "effort": "none", "attempted_stages": ["explicit"], "reason_codes": ["explicit_model_unavailable"]}},
    }
    return goldens.get(name, {"allowed_paths": ["src/benchapp/retries.py", "tests/test_retries.py"] if name == "RENAME" else ["src/benchapp/provider_select.py", "tests/test_provider_select.py"]})


def materialize(split: str, template_id: str, task_class: str, seed: int, workspace: Path, private_root: Path) -> tuple[Instance, Path]:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    files = _project_files(seed)
    name = template_id.split("-", 1)[1]
    lsp_fixture = _LSP_AB_FIXTURES.get(name) or _CONSISTENCY_AB_FIXTURES.get(name)
    lsp_golden: dict[str, Any] | None = None
    if lsp_fixture is not None:
        extra, lsp_golden = lsp_fixture(seed)
        files = {**files, **extra}
    decision = next(line.split()[1].rstrip(":") for line in files["src/benchapp/routes.py"].splitlines() if line.startswith("class "))
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    prompt = _prompt(template_id, task_class, seed, decision)
    (workspace / "TASK.md").write_text(prompt + "\n", encoding="utf-8")
    golden_path = private_root / f"{template_id}-{seed}.json"
    write_json(golden_path, lsp_golden if lsp_golden is not None else _golden(template_id, files, decision))
    instance = Instance(split, template_id, f"{template_id}-{seed}", task_class, seed, prompt, tree_digest(workspace))
    return instance, golden_path


def generate_public(root: Path, output: Path, verify: bool = True) -> dict[str, Any]:
    records: dict[str, list[dict[str, Any]]] = {}
    private = output.parent / ".controller-goldens"
    if private.exists():
        shutil.rmtree(private)
    private.mkdir(mode=0o700, parents=True)
    try:
        for split in ("development", "evaluation"):
            rows = []
            for template_id, task_class, seed in all_specs(split):
                workspace = output / "fixtures" / split / f"{template_id}-{seed}"
                instance, golden = materialize(split, template_id, task_class, seed, workspace, private)
                rows.append({"template_id": template_id, "instance_id": instance.instance_id, "class": task_class, "seed": seed, "prompt": instance.prompt, "fixture_digest": instance.fixture_digest})
                if verify:
                    ast.parse((workspace / "src/benchapp/routes.py").read_text(encoding="utf-8"))
                    if not golden.is_file():
                        raise AssertionError("controller golden missing")
            payload = {"schema_version": "omh_live_model_tool_corpus/v1", "split": split, "instances": rows}
            write_json(output / f"{split}.json", payload)
            records[split] = rows
    finally:
        shutil.rmtree(private)
    return {"schema_version": "omh_live_model_tool_corpus_receipt/v1", "counts": {key: len(value) for key, value in records.items()}, "corpus_digest": digest(records), "hidden_goldens_removed": not private.exists(), "verified": verify}
