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
# Separate split for the impact-check A/B, kept out of the pinned corpus the same way.
# Each edit template changes a definition whose callers sit in other modules (one through
# an alias, one through the module, one through a package re-export or `**options`), and
# only the definition's own module has a test in the workspace; CONTROL breaks nothing.
IMPACT_AB_SPLIT = "impact-ab"
IMPACT_AB_TEMPLATES = (("SIGNATURE", "edit"), ("RETURN-SHAPE", "edit"), ("KWARG", "edit"), ("CONTROL", "edit"))
SPLITS = ("development", "evaluation", LSP_AB_SPLIT, IMPACT_AB_SPLIT)

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
    if split == IMPACT_AB_SPLIT:
        return [(f"I-{name}", task_class, seed) for name, task_class in IMPACT_AB_TEMPLATES for seed in EVALUATION_SEEDS]
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


def _signature_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """A required parameter added to a function called from four modules: directly, through an
    alias, through the module, and through a package re-export under another name."""
    randomizer = random.Random(seed)
    rate = randomizer.choice((125, 250, 375))
    alias = randomizer.choice(("fee_for", "fee_of", "calc_fee"))
    files = {
        "src/benchapp/__init__.py": "from .pricing import compute_fee as fee\n\n__all__ = [\"fee\"]\n",
        "src/benchapp/pricing.py": (
            '"""Fee arithmetic."""\n\n\ndef compute_fee(amount_cents: int, rate_bp: int) -> int:\n'
            '    """Fee in cents for an amount at a rate in basis points."""\n'
            "    return amount_cents * rate_bp // 10000\n"
        ),
        "src/benchapp/billing.py": (
            "from benchapp.pricing import compute_fee\n\n\n"
            f"def bill_total(amount_cents: int) -> int:\n    return amount_cents + compute_fee(amount_cents, {rate})\n"
        ),
        "src/benchapp/invoices.py": (
            f"from benchapp.pricing import compute_fee as {alias}\n\n\n"
            f"def invoice_fee(amount_cents: int) -> int:\n    return {alias}(amount_cents, {rate * 2})\n"
        ),
        "src/benchapp/report.py": (
            "from benchapp import pricing\n\n\n"
            'def fee_line(amount_cents: int) -> str:\n    return f"fee={pricing.compute_fee(amount_cents, 100)}"\n'
        ),
        "src/benchapp/cli.py": (
            "from benchapp import fee\n\n\n"
            "def cli_fee(argv: list[str]) -> int:\n    return fee(int(argv[0]), 50)\n"
        ),
        "tests/test_pricing.py": (
            "from benchapp.pricing import compute_fee\n\n\n"
            "def test_compute_fee():\n    assert compute_fee(10000, 250) == 250\n"
        ),
    }
    return files, {"allowed_paths": sorted(files), "allowed_prefixes": ["tests/"], "rate": rate}


def _return_shape_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """A dict return turned into a dataclass, read by key in three modules (alias, module, `.get`)."""
    randomizer = random.Random(seed)
    price = randomizer.randint(110, 990)
    alias = randomizer.choice(("make_quote", "quote_for", "new_quote"))
    files = {
        "src/benchapp/quotes.py": (
            f'"""Quotes."""\n\nUNIT_PRICE_CENTS = {price}\n\n\n'
            "def build_quote(units: int) -> dict[str, int]:\n"
            '    return {"units": units, "total_cents": units * UNIT_PRICE_CENTS}\n'
        ),
        "src/benchapp/checkout.py": (
            "from benchapp.quotes import build_quote\n\n\n"
            "def checkout_total(units: int) -> int:\n    quote = build_quote(units)\n"
            '    return quote["total_cents"]\n'
        ),
        "src/benchapp/emailer.py": (
            f"from benchapp.quotes import build_quote as {alias}\n\n\n"
            f"def email_subject(units: int) -> str:\n    quote = {alias}(units)\n"
            "    return f\"Your quote for {quote['units']} units\"\n"
        ),
        "src/benchapp/ledger.py": (
            "from benchapp import quotes\n\n\n"
            "def ledger_entry(units: int) -> tuple[int, int]:\n    quote = quotes.build_quote(units)\n"
            '    return (quote["units"], quote.get("total_cents", 0))\n'
        ),
        "tests/test_quotes.py": (
            "from benchapp.quotes import UNIT_PRICE_CENTS, build_quote\n\n\n"
            "def test_build_quote():\n    quote = build_quote(2)\n"
            '    assert quote["total_cents"] == 2 * UNIT_PRICE_CENTS\n'
        ),
    }
    return files, {"allowed_paths": sorted(files), "allowed_prefixes": ["tests/"], "price": price}


def _kwarg_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """A keyword-only parameter renamed; one caller passes it through `**options`, which a type
    checker cannot follow, and one through a module alias."""
    randomizer = random.Random(seed)
    sync_timeout = randomizer.choice((2.0, 2.5, 4.0))
    crawl_timeout = randomizer.choice((3.0, 6.0, 9.0))
    files = {
        "src/benchapp/http_client.py": (
            '"""A fake HTTP client."""\n\n\n'
            "def fetch(url: str, *, timeout: float = 5.0, retries: int = 1) -> dict[str, object]:\n"
            '    return {"url": url, "timeout": timeout, "retries": retries}\n'
        ),
        "src/benchapp/sync.py": (
            "from benchapp.http_client import fetch\n\n\n"
            f"def sync_page(url: str) -> dict[str, object]:\n    return fetch(url, timeout={sync_timeout})\n"
        ),
        "src/benchapp/crawler.py": (
            "from benchapp.http_client import fetch\n\n"
            f'CRAWL_OPTIONS = {{"timeout": {crawl_timeout}, "retries": 3}}\n\n\n'
            "def crawl(url: str) -> dict[str, object]:\n    return fetch(url, **CRAWL_OPTIONS)\n"
        ),
        "src/benchapp/health.py": (
            "from benchapp import http_client as hc\n\n\n"
            "def ping(url: str) -> dict[str, object]:\n    return hc.fetch(url, timeout=1.0, retries=0)\n"
        ),
        "tests/test_http_client.py": (
            "from benchapp.http_client import fetch\n\n\n"
            "def test_fetch_defaults():\n"
            '    assert fetch("u") == {"url": "u", "timeout": 5.0, "retries": 1}\n'
        ),
    }
    golden = {"allowed_paths": sorted(files), "allowed_prefixes": ["tests/"], "sync_timeout": sync_timeout,
              "crawl_timeout": crawl_timeout}
    return files, golden


def _control_fixture(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """An edit that breaks no caller, next to an importer with an unrelated pre-existing type
    error: the impact check must stay silent here."""
    randomizer = random.Random(seed)
    alias = randomizer.choice(("cents", "as_cents", "to_c"))
    files = {
        "src/benchapp/units.py": (
            '"""Money units."""\n\n\ndef to_cents(amount: float) -> int:\n    return int(round(amount * 100))\n'
        ),
        "src/benchapp/cart.py": (
            "from benchapp.units import to_cents\n\n"
            'LEGACY_LIMIT: int = "unbounded"\n\n\n'
            "def cart_total(prices: list[float]) -> int:\n    return sum(to_cents(price) for price in prices)\n"
        ),
        "src/benchapp/refunds.py": (
            f"from benchapp.units import to_cents as {alias}\n\n\n"
            f"def refund_cents(amount: float) -> int:\n    return {alias}(amount)\n"
        ),
        "src/benchapp/summary.py": (
            "from benchapp import units\n\n\n"
            'def summary_line(amount: float) -> str:\n    return f"{units.to_cents(amount)} cents"\n'
        ),
        "tests/test_units.py": (
            "from benchapp.units import to_cents\n\n\n"
            "def test_to_cents():\n    assert to_cents(1.25) == 125\n"
        ),
    }
    return files, {"allowed_paths": sorted(files), "allowed_prefixes": ["tests/"]}


_IMPACT_AB_FIXTURES = {
    "SIGNATURE": _signature_fixture,
    "RETURN-SHAPE": _return_shape_fixture,
    "KWARG": _kwarg_fixture,
    "CONTROL": _control_fixture,
}


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
        "SIGNATURE": "compute_fee in src/benchapp/pricing.py must now enforce a minimum fee: add a third parameter minimum_cents: int with no default value, and return the larger of the computed fee and minimum_cents. Every existing caller passes minimum_cents=0, so behaviour elsewhere does not change. Return JSON with changed_paths.",
        "RETURN-SHAPE": "Change build_quote in src/benchapp/quotes.py to return a frozen dataclass Quote, defined in quotes.py with fields units: int and total_cents: int, instead of a dict, and update the code that uses its result so behaviour stays the same. Return JSON with changed_paths.",
        "KWARG": "Rename the keyword-only parameter timeout of fetch in src/benchapp/http_client.py to timeout_s (do not keep timeout as an alias). What fetch returns stays the same, and every caller must keep its behaviour. Return JSON with changed_paths.",
        "CONTROL": "Make to_cents in src/benchapp/units.py raise ValueError('negative amount') for a negative amount, without changing its signature or its result for any other amount. Return JSON with changed_paths.",
        "RENAME-SHADOW": "Rename the function parse_limit defined in src/benchapp/limits.py to read_limit at its definition and every reference to that function, without changing behavior. Parameters or local variables that merely share the name are not references and must stay. Return JSON with changed_paths.",
    }
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
    lsp_fixture = _LSP_AB_FIXTURES.get(name) or (_IMPACT_AB_FIXTURES.get(name) if split == IMPACT_AB_SPLIT else None)
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
