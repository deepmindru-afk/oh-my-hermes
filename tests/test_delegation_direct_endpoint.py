"""A delegation route is refused when Hermes would not send the child where it says.

`omh_delegate_route` writes `delegation.model`, `delegation.reasoning_effort`
and `delegation.provider`. Hermes resolves the child from the merged config,
and these Hermes paths (read at Hermes main 8b9ac5773e and v0.22.2, checked at
v0.21.1) can send it elsewhere while OMH answered `routed`:

* a direct `delegation.base_url`, which `_resolve_delegation_credentials`
  prefers over `delegation.provider` (tools/delegate_tool_config.py:432);
* the main model's local-endpoint bypass, which ignores an explicit requested
  provider when `model.base_url` is local and `model.provider` is unset
  (`_local_endpoint_bypass`, hermes_cli/runtime_provider.py:1001-1011 at main);
* a managed config Hermes layers over `config.yaml`
  (`_merge_managed_overlay`, hermes_cli/config.py:2282 at main);
* a shape OMH's line scan cannot read the way Hermes' loader does.

These tests pin each refusal, every case that must still route, that the
restore path is never refused, that a refusal writes nothing to the config
and records nothing, and that `status` names a direct endpoint. Every test uses temporary homes and its own managed
directory, so nothing reads the developer's `~/.hermes`, `~/.omh` or
`/etc/hermes`.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omh.plugin_bundle.omh import route_endpoint_guard as guard
from omh.plugin_bundle.omh.delegation_route_restore import (
    delegation_route_restore_path,
    restore_delegation_baseline,
    write_route_with_baseline,
)
from omh.plugin_bundle.omh.delegation_routing import (
    read_delegation_route,
    write_delegation_route,
)
from omh.plugin_bundle.omh.hermes_delegation import load_delegation_route_provenance
from omh.plugin_bundle.omh.tools.delegate_route_tool import omh_delegate_route_handler

MODEL_BLOCK = "model:\n  default: anthropic/claude-sonnet-4\n  provider: openrouter\n"
ENDPOINT = "http://127.0.0.1:8000/v1"
LOCAL_MAIN = "http://127.0.0.1:11434/v1"
ENDPOINT_KEY = "local-endpoint-key-dummy"
ALL_DIRECT_KEYS = ["delegation.base_url", "delegation.api_key", "delegation.api_mode"]
BOM = chr(0xFEFF)


def write_chain(omh_home: Path, models: list[str]) -> None:
    routing = omh_home / "routing"
    routing.mkdir(parents=True, exist_ok=True)
    (routing / "model-chains.json").write_text(
        json.dumps({
            "schema_version": "mixture_chain_overrides/v1",
            "categories": {"quick": [{"model": m, "reasoning_effort": "low"} for m in models]},
        }),
        encoding="utf-8",
    )
    (routing / "model-providers.json").write_text(
        json.dumps({
            "schema_version": "model_provider_routes/v1",
            "models": {m: {"provider": "gateway", "model": f"vendor/{m}"} for m in models},
        }),
        encoding="utf-8",
    )


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.home = root / "hermes"
        self.omh_home = root / "omh"
        self.managed_dir = root / "managed"
        self.home.mkdir()
        self.managed_dir.mkdir()
        self.config = self.home / "config.yaml"
        # An empty managed directory of the test's own: Hermes reads
        # $HERMES_MANAGED_DIR before /etc/hermes, so nothing outside the
        # temporary root is ever read.
        environment = mock.patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(self.managed_dir)})
        environment.start()
        self.addCleanup(environment.stop)
        # A route may only name a provider the person configured; these tests
        # route to the providers below, so record them as held. Each keeps the
        # family the serving check resolves it to without a record (its own
        # catalog name, else `unknown`), so that check reads them as before.
        routing = self.omh_home / "routing"
        routing.mkdir(parents=True)
        (routing / "providers.json").write_text(
            json.dumps({
                "schema_version": "provider_entitlements/v1",
                "providers": {
                    **{name: name for name in ("anthropic", "openai", "openai-codex", "google", "openrouter")},
                    **{
                        name: "unknown"
                        for name in (
                            "azure-foundry", "bedrock", "custom", "custom:cli-proxy", "gateway",
                            "google-genai", "moa", "my-vllm", "og", "vertex", "vertexai",
                        )
                    },
                },
                "subscription_clis": [],
            }),
            encoding="utf-8",
        )

    def write_config(self, delegation: str | None = None, *, head: str = MODEL_BLOCK) -> bytes:
        text = head if delegation is None else f"{head}delegation:\n{delegation}"
        self.config.write_text(text, encoding="utf-8")
        return self.config.read_bytes()

    def write_managed(self, text: str) -> None:
        (self.managed_dir / "config.yaml").write_text(text, encoding="utf-8")

    def call(self, **args) -> dict:
        # The host's own keywords, as Hermes passes them on every dispatch;
        # a fallback reads its chain position from this conversation.
        return json.loads(
            omh_delegate_route_handler(
                {"hermes_home": str(self.home), "omh_home": str(self.omh_home), **args},
                session_id="guard-session",
                task_id="guard-task",
            )
        )

    def set_route(self, model: str = "claude-fable-5-1", provider: str = "anthropic") -> dict:
        return self.call(action="set", model=model, provider=provider)

    def assert_refused(self, result: dict, before: bytes, reason: str, keys: list[str] | None = None) -> None:
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["reason"], reason, result)
        if keys is not None:
            self.assertEqual(result["conflicting_keys"], keys)
        self.assertIn("No route was written", result["error"])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertNotIn("say", result)

    def assert_routed(self, result: dict, provider: str) -> None:
        self.assertEqual(result["status"], "routed", result)
        self.assertEqual(read_delegation_route(self.home).get("provider"), provider)


class DirectEndpointTest(GuardTestCase):
    def test_a_provider_route_over_a_direct_endpoint_is_refused_and_nothing_is_recorded(self) -> None:
        before = self.write_config(
            f"  base_url: {ENDPOINT}\n  api_key: {ENDPOINT_KEY}\n  api_mode: chat_completions\n"
        )
        held = {p: p.read_bytes() for p in self.omh_home.rglob("*") if p.is_file()}

        result = self.set_route()

        self.assert_refused(result, before, "direct_endpoint_overrides_provider", ALL_DIRECT_KEYS)
        self.assertIn("as provider 'custom'", result["error"])
        self.assertIn("a route to provider 'anthropic' would report routed", result["error"])
        self.assertIn("hermes config unset delegation.base_url", result["error"])
        self.assertIn("OMH does not change these keys", result["error"])
        self.assertFalse(delegation_route_restore_path(self.omh_home).exists())
        self.assertEqual(load_delegation_route_provenance(self.omh_home), [])
        # A refusal records nothing; the writer's lock file is the only file
        # it leaves, and the fixture's own record is untouched.
        left = sorted(
            str(p.relative_to(self.omh_home)) for p in self.omh_home.rglob("*") if p.is_file() and p not in held
        )
        self.assertTrue(all(name.endswith(".lock") for name in left), left)
        self.assertEqual({p: p.read_bytes() for p in held}, held)
        # The person's values are named by key only, never echoed.
        self.assertNotIn(ENDPOINT_KEY, json.dumps(result))
        self.assertNotIn("127.0.0.1", json.dumps(result))

    def test_conflicting_keys_name_only_the_keys_the_file_sets(self) -> None:
        before = self.write_config(f"  base_url: {ENDPOINT}\n")
        result = self.set_route()
        self.assert_refused(result, before, "direct_endpoint_overrides_provider", ["delegation.base_url"])
        self.assertNotIn("read only with it", result["error"])

    def test_a_category_route_is_refused_like_an_explicit_one(self) -> None:
        write_chain(self.omh_home, ["first-model"])
        before = self.write_config(f"  base_url: {ENDPOINT}\n")
        self.assert_refused(self.call(action="set", category="quick"), before, "direct_endpoint_overrides_provider")

    def test_a_fallback_onto_a_provider_is_refused_once_a_direct_endpoint_appears(self) -> None:
        write_chain(self.omh_home, ["first-model", "second-model"])
        self.write_config("  max_iterations: 50\n")
        self.assertEqual(self.call(action="set", category="quick")["status"], "routed")
        records = len(load_delegation_route_provenance(self.omh_home))
        with self.config.open("a", encoding="utf-8") as handle:
            handle.write(f"  base_url: {ENDPOINT}\n")
        before = self.config.read_bytes()

        result = self.call(action="fallback", category="quick")

        self.assert_refused(result, before, "direct_endpoint_overrides_provider", ["delegation.base_url"])
        self.assertEqual(read_delegation_route(self.home)["model"], "vendor/first-model")
        self.assertEqual(len(load_delegation_route_provenance(self.omh_home)), records)

    def test_an_endpoint_hermes_names_as_another_provider_is_refused(self) -> None:
        cases = (
            # Hermes compares the exact host, not a substring.
            ("https://api.anthropic.com.example.test/v1", "anthropic", "custom"),
            ("https://proxy.test/api.anthropic.com/v1", "anthropic", "custom"),
            # The Codex backend is the host AND its path.
            ("https://chatgpt.com/v1", "openai-codex", "custom"),
            ("https://api.anthropic.com", "custom", "anthropic"),
            # A named custom provider still becomes plain `custom` on the
            # direct branch, with the direct branch's key.
            (ENDPOINT, "custom:cli-proxy", "custom"),
            (ENDPOINT, "my-vllm", "custom"),
        )
        for url, provider, built_as in cases:
            with self.subTest(url=url, provider=provider):
                before = self.write_config(f"  base_url: {url}\n  api_key: {ENDPOINT_KEY}\n")
                result = self.set_route(model="some-model", provider=provider)
                self.assert_refused(result, before, "direct_endpoint_overrides_provider")
                self.assertIn(f"as provider '{built_as}'", result["error"])

    def test_a_matching_endpoint_without_its_own_key_is_refused(self) -> None:
        # The direct branch falls back to the parent session's key, which goes
        # to that endpoint unless a credential pool for it exists.
        for url, provider in (
            ("https://api.anthropic.com", "anthropic"),
            ("https://chatgpt.com/backend-api/codex", "openai-codex"),
            (ENDPOINT, "custom"),
        ):
            for key_line in ("", "  api_key: ''\n", "  api_key: ${ENDPOINT_KEY}\n"):
                with self.subTest(url=url, key_line=key_line):
                    before = self.write_config(f"  base_url: {url}\n{key_line}")
                    result = self.set_route(model="some-model", provider=provider)
                    self.assert_refused(
                        result, before, "direct_endpoint_without_key", ["delegation.base_url", "delegation.api_key"]
                    )
                    self.assertIn("parent session's key", result["error"])


class StrictReadingTest(GuardTestCase):
    """A shape the line scan does not model refuses; reading it as unset would not be safe."""

    def test_a_delegation_line_the_scan_cannot_read_refuses_the_route(self) -> None:
        shapes = {
            "merge key, inline": f"  <<: {{base_url: '{ENDPOINT}'}}\n",
            "double quote continued at column 0": f'  note: "line one\nx: y"\n  base_url: {ENDPOINT}\n',
            "single quote continued at column 0": f"  note: 'line one\nx: y'\n  base_url: {ENDPOINT}\n",
            "flow list continued at column 0": f"  note: [a,\nx: y]\n  base_url: {ENDPOINT}\n",
            "flow map continued at column 0": f"  note: {{a: 1,\nx: y}}\n  base_url: {ENDPOINT}\n",
            "same-indent list": f"  base_url:\n  - {ENDPOINT}\n",
            "explicit key": f"  ? base_url\n  : {ENDPOINT}\n",
            "escaped quoted key": '  "base\\x5furl": ' + ENDPOINT + "\n",
            "repeated key, set last": f"  base_url: ''\n  base_url: {ENDPOINT}\n",
            "repeated key, unset last": f"  base_url: {ENDPOINT}\n  base_url: ''\n",
            "folded block scalar": f"  base_url: >\n    {ENDPOINT}\n",
            "plain scalar continued": "  base_url: http://127.0.0.1\n    :8000/v1\n",
            "unterminated quote": f"  base_url: '{ENDPOINT}\n",
            "tab indentation": f"\tbase_url: {ENDPOINT}\n",
            "shallower line": f"    base_url: {ENDPOINT}\n  api_key: k\n",
        }
        for name, shape in shapes.items():
            with self.subTest(shape=name):
                before = self.write_config(shape)
                result = self.set_route()
                self.assert_refused(result, before, "delegation_unreadable", ["delegation"])
                self.assertIn("cannot read safely", result["error"])

    def test_a_merge_key_from_an_anchor_elsewhere_refuses(self) -> None:
        before = self.write_config(
            "  <<: {max_iterations: 5}\n", head=f"x-ep: &ep\n  base_url: {ENDPOINT}\n{MODEL_BLOCK}"
        )
        self.assert_refused(self.set_route(), before, "delegation_unreadable")
        # Merged from an alias, it is a file Hermes may not load at all.
        before = self.write_config("  <<: *ep\n", head=f"x-ep: &ep\n  base_url: {ENDPOINT}\n{MODEL_BLOCK}")
        self.assert_refused(self.set_route(), before, "config_unloadable", ["delegation"])

    def test_a_value_the_scan_cannot_decide_counts_as_set(self) -> None:
        for shape in (
            "  base_url:\n    url: http://nested.test\n",
            f"  base_url: !!str {ENDPOINT}\n",
            f"  base_url: [{ENDPOINT}]\n",
            "  base_url: 'it''s'\n",
            "  base_url: ${DELEGATION_BASE_URL}\n",
            "  base_url: '${DELEGATION_BASE_URL}'\n",
        ):
            with self.subTest(shape=shape):
                before = self.write_config(shape)
                result = self.set_route()
                self.assert_refused(result, before, "direct_endpoint_overrides_provider", ["delegation.base_url"])
                self.assertIn("cannot read, so it counts as set", result["error"])

    def test_a_spelling_hermes_versions_read_differently_is_undecidable(self) -> None:
        # Hermes 0.21.x loads with PyYAML, which reads these as strings; 0.22
        # and later load with ruamel in YAML 1.1 mode, which reads them as
        # false or zero. Neither can load `=` or `0x_`.
        for spelling in ("n", "N", "0e0", "0E5", "0.0e0", "._0", "=", "0x_"):
            with self.subTest(key="delegation.base_url", spelling=spelling):
                before = self.write_config(f"  base_url: {spelling}\n")
                self.assert_refused(
                    self.set_route(), before, "direct_endpoint_overrides_provider", ["delegation.base_url"]
                )
            with self.subTest(key="model.provider", spelling=spelling):
                # Read as a set provider, it would lift the bypass 0.22 applies.
                before = self.write_config(head=f"model:\n  base_url: {LOCAL_MAIN}\n  provider: {spelling}\n")
                self.assert_refused(self.set_route(), before, "local_endpoint_bypass")

    def test_an_escaped_double_quoted_value_is_not_read_raw(self) -> None:
        # Hermes decodes `\x2e` to `.`, so the child is built as `anthropic`;
        # read raw, the host would look like a `custom` endpoint.
        for value in ('"https://api.anthropic\\x2ecom"', '"https://api.anthropic.com\\t"'):
            with self.subTest(value=value):
                before = self.write_config(f"  base_url: {value}\n  api_key: k\n")
                result = self.set_route(model="some-model", provider="custom")
                self.assert_refused(result, before, "direct_endpoint_overrides_provider")

    def test_a_top_level_line_the_scan_cannot_read_refuses(self) -> None:
        for head in (
            f"{MODEL_BLOCK}model:\n  default: other\n",
            f"<<: *defaults\n{MODEL_BLOCK}",
            f"- item\n{MODEL_BLOCK}",
            f"note: 'unclosed\n{MODEL_BLOCK}",
        ):
            with self.subTest(head=head):
                before = self.write_config("  max_iterations: 5\n", head=head)
                self.assert_refused(self.set_route(), before, "config_unreadable", ["config.yaml"])

    def test_the_endpoint_is_read_at_the_sections_own_indent_and_past_a_comment(self) -> None:
        for shape in (
            f"    base_url: {ENDPOINT}\n",
            f"  base_url: {ENDPOINT}  # the lab box\n",
            f"  base_url: '{ENDPOINT}'\n",
            f'  base_url: "{ENDPOINT}"  # quoted\n',
            f"  'base_url': {ENDPOINT}\n",
        ):
            with self.subTest(shape=shape):
                before = self.write_config(shape)
                self.assert_refused(self.set_route(), before, "direct_endpoint_overrides_provider")


class UnloadableConfigTest(GuardTestCase):
    """An alias or `<<` given as a value in what the guard reads refuses every route.

    Hermes' loader fails on an alias whose anchor is not defined and on `<<`
    as a value (PyYAML at v0.21.1, ruamel at v0.22.2 and main), then uses its
    last good copy of the file, so no route written there takes effect,
    whatever provider it names. The scan does not follow anchors, so a
    defined alias refuses too.
    """

    SHAPES = {
        # model, with its provider pinned or with nothing the bypass reads
        "model flow alias, pinned": "model: {base_url: *a, provider: custom}\n",
        "model block alias, pinned": "model:\n  base_url: *a\n  provider: custom\n",
        "model defined alias, pinned": f"x-local: &a {LOCAL_MAIN}\nmodel:\n  base_url: *a\n  provider: custom\n",
        "model provider alias": "model:\n  default: x\n  provider: *p\n",
        "model flow <<": f"model: {{base_url: {LOCAL_MAIN}, provider: <<}}\n",
        "model block <<": f"model:\n  base_url: {LOCAL_MAIN}\n  provider: <<\n",
        "model header <<": "model: <<\n",
        "model header alias": f"x-local: &local\n  base_url: {LOCAL_MAIN}\nmodel: *local\n",
        "model merge key from an alias": f"x-m: &m\n  base_url: {LOCAL_MAIN}\nmodel:\n  <<: *m\n",
        "model flow list beside a pin": "model:\n  default: x\n  provider: openrouter\n  extra: [*a]\n",
        "model flow mapping holding a list": f"model: {{base_url: '{LOCAL_MAIN}', provider: custom, extra: [*a]}}\n",
        # root keys Hermes reads as model keys
        "root provider <<": f"provider: <<\nmodel:\n  base_url: {LOCAL_MAIN}\n",
        "root base_url alias": "base_url: *a\nmodel:\n  provider: custom\n",
        "root api_base alias": "api_base: *a\nmodel:\n  default: x\n  provider: openrouter\n",
        # delegation
        "delegation alias": f"{MODEL_BLOCK}delegation:\n  base_url: *endpoint\n",
        "delegation << after a comment": f"{MODEL_BLOCK}delegation:\n  api_mode: <<  # x\n",
        "delegation nested list alias": f"{MODEL_BLOCK}delegation:\n  extra:\n    - *endpoint\n",
        "delegation nested mapping alias": f"{MODEL_BLOCK}delegation:\n  extra:\n    k: *endpoint\n",
        "delegation flow list alias": f"{MODEL_BLOCK}delegation:\n  extra: [*ep]\n",
        "delegation flow mapping alias": f"{MODEL_BLOCK}delegation:\n  extra: {{k: *ep}}\n",
        "delegation flow list <<": f"{MODEL_BLOCK}delegation:\n  extra: [<<]\n",
        "delegation nested flow alias": f"{MODEL_BLOCK}delegation:\n  extra:\n    k: [*ep]\n",
        "delegation endpoint and key beside a flow alias": (
            f"{MODEL_BLOCK}delegation:\n  base_url: {ENDPOINT}\n  api_key: k\n  extra: [*ep]\n"
        ),
        # a tag neither loader builds
        "delegation !!merge tag": f"{MODEL_BLOCK}delegation:\n  api_mode: !!merge x\n",
        "delegation local tag": f"{MODEL_BLOCK}delegation:\n  api_mode: !custom x\n",
        "delegation tag in a flow list": f"{MODEL_BLOCK}delegation:\n  extra: [!!python/str x]\n",
        "model verbatim tag": "model:\n  default: x\n  provider: !<tag:yaml.org,2002:merge> x\n",
    }

    def test_every_route_is_refused(self) -> None:
        for name, text in self.SHAPES.items():
            for provider in ("anthropic", "openrouter", "custom", ""):
                with self.subTest(shape=name, provider=provider):
                    self.config.write_text(text, encoding="utf-8")
                    before = self.config.read_bytes()
                    result = (
                        self.set_route(model="some-model", provider=provider)
                        if provider
                        else self.call(action="set", model="local-llama")
                    )
                    self.assert_refused(result, before, "config_unloadable")
                    self.assertIn("would not take effect", result["error"])

    def test_the_refusal_names_where_the_value_is(self) -> None:
        for text, keys in (
            ("model:\n  provider: <<\n", ["model"]),
            ("api_base: *a\nmodel:\n  default: x\n", ["api_base"]),
            (f"{MODEL_BLOCK}delegation:\n  extra: [*ep]\n", ["delegation"]),
            (f"provider: *p\n{MODEL_BLOCK}delegation:\n  base_url: *ep\n", ["provider", "delegation"]),
        ):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                before = self.config.read_bytes()
                self.assert_refused(self.set_route(), before, "config_unloadable", keys)

    def test_values_hermes_loads_route(self) -> None:
        # Text that only looks like an alias, `<<` or a tag, and the tags
        # both loaders build.
        for shape in (
            "  note: a*b\n",
            "  note: see *this*\n",
            "  extra: ['*ep', \"<<\"]\n",
            "  extra: [a, '<< x']\n",
            '  note: "a, *b"\n',
            "  note: a !b\n",
            "  max_iterations: !!int 5\n",
            "  extra: !!seq [a, !!str b]\n",
            "  note: ! text\n",
        ):
            with self.subTest(shape=shape):
                self.write_config(shape)
                self.assert_routed(self.set_route(), "anthropic")

    def test_a_managed_alias_makes_its_section_unreadable(self) -> None:
        # Hermes skips a managed file it cannot load, but the scan cannot
        # tell that file from one whose aliases are defined.
        self.write_config("  max_iterations: 5\n")
        self.write_managed("delegation:\n  base_url: *ep\n")
        result = self.call(action="set", model="local-llama")
        self.assertEqual(result["reason"], "managed_config_unreadable", result)


class LeftToHermesTest(GuardTestCase):
    """Each of these still routes, because Hermes sends the child where the route says."""

    def test_no_direct_endpoint_routes_as_before(self) -> None:
        self.write_config("  max_iterations: 50\n")
        self.assert_routed(self.set_route(), "anthropic")

    def test_every_spelling_hermes_reads_as_unset_routes(self) -> None:
        # Measured with Hermes' own loader at v0.21.1, v0.22.2 and main.
        spellings = (
            "''", '""', "'   '", "~", "null", "Null", "NULL", "false", "False", "no", "Off",
            "0", "-0", "+0", "00", "0_0", "0.0", ".0", "0.", "0.0e+0", ".0e+0", "0:0.0", "0x0", "0x_0",
            "0b0", "", "# none yet",
        )
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                self.write_config(f"  base_url: {spelling}\n  max_iterations: 50\n")
                self.assert_routed(self.set_route(), "anthropic")

    def test_spellings_hermes_reads_as_set_refuse(self) -> None:
        for spelling in ("y", "On", "0o0", "0:0", ".0e0", "+.0", "-.0", "0b", "1", "'0'", "'~'"):
            with self.subTest(spelling=spelling):
                before = self.write_config(f"  base_url: {spelling}\n")
                self.assert_refused(self.set_route(), before, "direct_endpoint_overrides_provider")

    def test_api_key_and_api_mode_without_base_url_route(self) -> None:
        # Hermes reads both only on the direct-endpoint branch.
        self.write_config(f"  api_key: {ENDPOINT_KEY}\n  api_mode: chat_completions\n")
        self.assert_routed(self.set_route(), "anthropic")

    def test_a_native_sdk_provider_routes_over_a_direct_endpoint(self) -> None:
        for provider in ("bedrock", "vertex", "google", "google-genai", "Bedrock"):
            with self.subTest(provider=provider):
                self.write_config(f"  base_url: {ENDPOINT}\n")
                self.assert_routed(self.set_route(model="some-model", provider=provider), provider)

    def test_an_endpoint_hermes_names_as_the_routed_provider_routes_with_its_own_key(self) -> None:
        cases = (
            ("https://api.anthropic.com", "anthropic"),
            ("https://API.Anthropic.com./v1/", "anthropic"),
            ("api.anthropic.com", "anthropic"),
            ("https://chatgpt.com/backend-api/codex", "openai-codex"),
            ("https://chatgpt.com/BACKEND-API/codex/responses", "openai-codex"),
            (ENDPOINT, "custom"),
        )
        for url, provider in cases:
            with self.subTest(url=url, provider=provider):
                self.write_config(f"  base_url: {url}\n  api_key: {ENDPOINT_KEY}\n")
                self.assert_routed(self.set_route(model="some-model", provider=provider), provider)

    def test_a_base_url_outside_the_delegation_section_is_not_its_endpoint(self) -> None:
        head = (
            "model:\n  provider: openrouter\n  base_url: https://openrouter.ai/api/v1\n"
            f"providers:\n  my-vllm:\n    base_url: {ENDPOINT}\n"
        )
        self.write_config(f"  extra:\n    base_url: {ENDPOINT}\n", head=head)
        self.assert_routed(self.set_route(), "anthropic")

    def test_shapes_hermes_itself_writes_route(self) -> None:
        # Hermes dumps lists indented under their key (hermes_yaml.py:97), and
        # its example config leaves whitespace-only lines between keys.
        self.write_config(
            "  max_iterations: 250   # comment\n  \n  fallback_providers:\n"
            "    - provider: openrouter\n      model: x\n  request_overrides: {extra_body: {a: [1, 2]}}\n",
            head="---\n" + MODEL_BLOCK,
        )
        self.assert_routed(self.set_route(), "anthropic")

    def test_a_leading_byte_order_mark_is_kept_and_the_section_reused(self) -> None:
        self.config.write_text(f"{BOM}delegation:\n  max_iterations: 5\n{MODEL_BLOCK}", encoding="utf-8")

        self.assert_routed(self.set_route(), "anthropic")

        text = self.config.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(BOM + "delegation:\n"))
        self.assertEqual(text.count("delegation:"), 1)
        self.assertEqual(read_delegation_route(self.home)["model"], "claude-fable-5-1")


class RouteWithoutProviderTest(GuardTestCase):
    def test_removing_a_native_sdk_provider_beside_a_direct_endpoint_is_refused(self) -> None:
        # Hermes compares the provider case-insensitively.
        for native in ("bedrock", "Bedrock"):
            with self.subTest(native=native):
                before = self.write_config(
                    f"  provider: {native}\n  base_url: https://bedrock-runtime.us-east-1.amazonaws.com\n"
                )
                result = self.call(action="set", model="local-llama")
                self.assert_refused(
                    result, before, "native_provider_removed", ["delegation.provider", "delegation.base_url"]
                )
                self.assertIn(f"'{native}'", result["error"])

    def test_a_route_without_a_provider_otherwise_routes(self) -> None:
        for shape in (
            f"  base_url: {ENDPOINT}\n",
            f"  provider: anthropic\n  base_url: {ENDPOINT}\n",
            "  provider: bedrock\n",
        ):
            with self.subTest(shape=shape):
                self.write_config(shape)
                result = self.call(action="set", model="local-llama")
                self.assertEqual(result["status"], "routed", result)
                self.assertEqual(read_delegation_route(self.home), {"model": "local-llama"})

    def test_a_direct_endpoint_does_not_inherit_the_sessions_provider(self) -> None:
        # Hermes builds the child against delegation.base_url, so the main
        # model's provider, which cannot serve this model, is not checked.
        session = "model:\n  default: gpt-5.6-sol\n  provider: openai-codex\n"
        for name, delegation, managed in (
            ("config.yaml", f"  base_url: {ENDPOINT}\n", None),
            ("managed config", None, f"delegation:\n  base_url: {ENDPOINT}\n"),
        ):
            with self.subTest(endpoint=name):
                if managed:
                    self.write_managed(managed)
                self.write_config(delegation, head=session)
                result = self.call(action="set", model="claude-fable-5-1")
                self.assertEqual(result["status"], "routed", result)
                self.assertEqual(read_delegation_route(self.home), {"model": "claude-fable-5-1"})

    def test_a_named_endpoint_is_checked_in_the_sessions_place(self) -> None:
        # Hermes names the child's provider by the endpoint's URL, so that
        # provider is checked, not the session's: each session here serves
        # the model and the endpoint does not.
        for url, session, model, endpoint_provider in (
            ("https://api.anthropic.com", "openai-codex", "gpt-6.1-sol", "anthropic"),
            ("https://chatgpt.com/backend-api/codex", "anthropic", "claude-fable-5-1", "openai-codex"),
        ):
            with self.subTest(url=url):
                before = self.write_config(f"  base_url: {url}\n", head=f"model:\n  provider: {session}\n")
                result = self.call(action="set", model=model)
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["endpoint_provider"], endpoint_provider)
                self.assertNotIn("inherited_provider", result)
                self.assertIn("hermes config unset delegation.base_url", result["error"])
                self.assertEqual(self.config.read_bytes(), before)

    def test_a_named_endpoint_that_serves_the_model_routes(self) -> None:
        # The session's provider cannot serve Fable; the endpoint's can.
        self.write_config("  base_url: https://api.anthropic.com\n", head="model:\n  provider: openai-codex\n")
        result = self.call(action="set", model="claude-fable-5-1")
        self.assertEqual(result["status"], "routed", result)
        self.assertEqual(read_delegation_route(self.home), {"model": "claude-fable-5-1"})

    def test_a_custom_endpoint_is_not_checked_against_a_recorded_custom_provider(self) -> None:
        # Hermes names every other URL `custom`; a family recorded for the
        # person's `custom` provider says nothing about this endpoint.
        routing = self.omh_home / "routing"
        routing.mkdir(parents=True, exist_ok=True)
        (routing / "providers.json").write_text(
            json.dumps({
                "schema_version": "provider_entitlements/v1",
                "providers": {"custom": "openai-codex"},
                "subscription_clis": [],
            }),
            encoding="utf-8",
        )
        self.write_config(f"  base_url: {ENDPOINT}\n", head="model:\n  provider: openrouter\n")
        result = self.call(action="set", model="claude-fable-5-1")
        self.assertEqual(result["status"], "routed", result)

    def test_an_endpoint_the_scan_cannot_read_keeps_the_inherited_provider_check(self) -> None:
        session = "model:\n  default: gpt-5.6-sol\n  provider: openai-codex\n"
        for delegation in (
            "  base_url: ${DELEGATION_BASE_URL}\n",
            f"  base_url: >\n    {ENDPOINT}\n",
            "  base_url: *endpoint\n",
        ):
            with self.subTest(delegation=delegation):
                before = self.write_config(delegation, head=session)
                result = self.call(action="set", model="claude-fable-5-1")
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["inherited_provider"], "openai-codex")
                # A base_url is set, so no provider is pinned in its place.
                self.assertNotIn("provider_pinned", result)
                self.assertEqual(self.config.read_bytes(), before)

    def test_with_no_endpoint_the_one_known_server_is_pinned_instead(self) -> None:
        # The same inherited provider that cannot serve Fable, with no
        # delegation.base_url: the one recorded provider whose family serves
        # it (anthropic; openrouter is a relay, never pinned on its own) is
        # written instead of a refusal.
        session = "model:\n  default: gpt-5.6-sol\n  provider: openai-codex\n"
        for delegation in (None, "  max_iterations: 5\n"):
            with self.subTest(delegation=delegation):
                self.write_config(delegation, head=session)
                result = self.call(action="set", model="claude-fable-5-1")
                self.assertEqual(result["status"], "routed", result)
                self.assertEqual(
                    result["provider_pinned"], {"inherited_provider": "openai-codex", "pinned_provider": "anthropic"}
                )
                self.assertEqual(read_delegation_route(self.home), {"model": "claude-fable-5-1", "provider": "anthropic"})


class LocalEndpointBypassTest(GuardTestCase):
    def test_a_local_main_endpoint_with_no_provider_refuses_a_provider_route(self) -> None:
        heads = {
            "model.base_url": f"model:\n  default: llama3\n  base_url: {LOCAL_MAIN}\n",
            "provider auto": f"model:\n  base_url: {LOCAL_MAIN}\n  provider: auto\n",
            "provider null": f"model:\n  base_url: {LOCAL_MAIN}\n  provider: ~\n",
            # Unset up to Hermes v0.21.3; from v0.21.4 the text `False`, which
            # lifts the bypass. Refused for the older versions.
            "provider false": f"model:\n  base_url: {LOCAL_MAIN}\n  provider: false\n",
            "LAN host": "model:\n  base_url: http://192.168.1.20:8000/v1\n",
            "non-cloud host": "model:\n  base_url: https://llm.example.internal/v1\n",
            "cloud lookalike": "model:\n  base_url: https://openrouter.ai.example.test/v1\n",
            "cloud name without its dot": "model:\n  base_url: https://evilanthropic.com/v1\n",
            "root base_url": f"base_url: {LOCAL_MAIN}\nmodel:\n  default: llama3\n",
            "root api_base": f"api_base: {LOCAL_MAIN}\nmodel:\n  default: llama3\n",
            "model.api_base": f"model:\n  api_base: {LOCAL_MAIN}\n",
            # Hermes promotes a root base_url before it reads model.api_base.
            "root base_url over model.api_base": (
                f"base_url: {LOCAL_MAIN}\nmodel:\n  api_base: https://openrouter.ai/api/v1\n"
            ),
            "scalar model, root base_url": f"model: llama3\nbase_url: {LOCAL_MAIN}\n",
            "provider from an environment reference": (
                f"model:\n  base_url: {LOCAL_MAIN}\n  provider: '${{MAIN_PROVIDER}}'\n"
            ),
        }
        for name, head in heads.items():
            for provider in ("anthropic", "bedrock", "BEDROCK"):
                with self.subTest(shape=name, provider=provider):
                    before = self.write_config(head=head)
                    result = self.set_route(model="some-model", provider=provider)
                    self.assert_refused(result, before, "local_endpoint_bypass", ["model.base_url", "model.provider"])
                    self.assertIn("hermes config set model.provider", result["error"])

    def test_a_native_provider_beside_a_direct_endpoint_still_meets_the_bypass(self) -> None:
        before = self.write_config(f"  base_url: {ENDPOINT}\n", head=f"model:\n  base_url: {LOCAL_MAIN}\n")
        self.assert_refused(self.set_route(model="some-model", provider="bedrock"), before, "local_endpoint_bypass")

    def test_a_model_section_the_scan_cannot_read_refuses_a_provider_route(self) -> None:
        for head in (
            f"model:\n  default:\n    model: llama3\n    provider: custom\n  base_url: {LOCAL_MAIN}\n",
            f"model:\n  base_url: >\n    {LOCAL_MAIN}\n",
            # Lines that hide a local base_url from the scan unless the whole
            # section counts as unreadable.
            f'model:\n  note: "line one\nx: y"\n  base_url: {LOCAL_MAIN}\n',
            f"model:\n  <<: {{base_url: '{LOCAL_MAIN}'}}\n",
            # A whole value on the header line that is not a plain scalar or a
            # flow mapping this scan reads.
            f"model: &m {{base_url: '{LOCAL_MAIN}'}}\n",
            f"model: !!map {{base_url: '{LOCAL_MAIN}'}}\n",
            "model: ${MAIN_MODEL}\n",
            "model: [llama3]\n",
            f"model: {{base_url: {LOCAL_MAIN}, base_url: ''}}\n",
            f"model: {{base_url:{LOCAL_MAIN}}}\n",
            "model: {base_url: http://127.0.0.1:11434/v1?x=1}\n",
            "model: {base_url: http://127.0.0.1:11434/v1#x}\n",
            # Each would read as pinned to `custom` if the rest were skipped.
            f"model: {{provider: custom, base_url: '{LOCAL_MAIN}'}} trailing\n",
            f"model: {{provider: custom, base_url: '{LOCAL_MAIN}', default:}}\n",
            # A collection counts as set: Hermes reads its text as the URL.
            f"model: {{default: llama3, base_url: [{LOCAL_MAIN}]}}\n",
            # A plain value holding `: `, which no Hermes loader parses.
            f"model: {{provider: custom: x, base_url: '{LOCAL_MAIN}'}}\n",
        ):
            with self.subTest(head=head):
                before = self.write_config(head=head)
                result = self.set_route()
                self.assert_refused(result, before, "local_endpoint_bypass")
                self.assertIn("cannot read safely", result["error"])

    def test_the_bypass_does_not_apply_and_the_route_writes(self) -> None:
        heads = {
            "model.provider pinned": f"model:\n  base_url: {LOCAL_MAIN}\n  provider: custom\n",
            "root provider pinned": f"provider: custom\nmodel:\n  base_url: {LOCAL_MAIN}\n",
            "openrouter host": "model:\n  base_url: https://openrouter.ai/api/v1\n",
            "anthropic subdomain": "model:\n  base_url: https://api.anthropic.com/v1\n",
            "openai subdomain": "model:\n  base_url: https://eu.api.openai.com/v1\n",
            "no base_url": "model:\n  default: llama3\n",
            "unset base_url": "model:\n  base_url: ''\n",
            "model.base_url over root": f"base_url: {LOCAL_MAIN}\nmodel:\n  base_url: https://openrouter.ai/api/v1\n",
            # Text that only opens like `<<` or an alias loads as that text.
            "provider text opening with <<": f"model:\n  base_url: {LOCAL_MAIN}\n  provider: << x\n",
            "quoted provider opening with *": f"model:\n  base_url: {LOCAL_MAIN}\n  provider: '*custom'\n",
        }
        for name, head in heads.items():
            with self.subTest(shape=name):
                self.write_config(head=head)
                self.assert_routed(self.set_route(), "anthropic")

    def test_providers_the_bypass_leaves_alone_route(self) -> None:
        for provider in ("openrouter", "custom", "moa", "azure-foundry", "vertex", "vertexai"):
            with self.subTest(provider=provider):
                self.write_config(head=f"model:\n  base_url: {LOCAL_MAIN}\n")
                self.assert_routed(self.set_route(model="some-model", provider=provider), provider)

    def test_openai_meets_the_bypass_on_the_oldest_hermes_omh_supports(self) -> None:
        # Hermes v0.21.1 sends `openai` through the bypass to OpenRouter;
        # `expand_direct_api_alias` takes it past the bypass only from v0.21.4.
        before = self.write_config(head=f"model:\n  base_url: {LOCAL_MAIN}\n")
        self.assert_refused(self.set_route(model="some-model", provider="openai"), before, "local_endpoint_bypass")

    def test_a_model_section_written_on_one_line_is_read(self) -> None:
        refused = {
            # The shape Hermes main's own save_config keeps for a flow-style model.
            "Hermes-written flow": f"model: {{default: llama3, provider: auto, base_url: {LOCAL_MAIN}}}\n",
            "quoted base_url": f"model: {{default: llama3, base_url: '{LOCAL_MAIN}'}}  # local\n",
            "root base_url beside": f"model: {{default: llama3}}\nbase_url: {LOCAL_MAIN}\n",
        }
        for name, head in refused.items():
            with self.subTest(shape=name):
                before = self.write_config(head=head)
                result = self.set_route()
                self.assert_refused(result, before, "local_endpoint_bypass", ["model.base_url", "model.provider"])
                self.assertNotIn("cannot read safely", result["error"])
        routed = {
            "provider pinned": f"model: {{base_url: '{LOCAL_MAIN}', provider: custom}}\n",
            "quoted comma and hash": f"model: {{default: 'llama3, #4', provider: custom, base_url: '{LOCAL_MAIN}'}}\n",
            "nested values": (
                f"model: {{provider: custom, extra: {{a: [1, 2], b: 'c, d'}}, base_url: '{LOCAL_MAIN}'}}\n"
            ),
            "cloud host": "model: {default: x, base_url: https://openrouter.ai/api/v1}\n",
            "no base_url": "model: {default: x, provider: openrouter}\n",
            "empty": "model: {}\n",
        }
        for name, head in routed.items():
            with self.subTest(shape=name):
                self.write_config(head=head)
                self.assert_routed(self.set_route(), "anthropic")

    def test_a_matching_direct_endpoint_goes_around_the_bypass(self) -> None:
        # The direct branch never asks the runtime provider for an endpoint.
        self.write_config(
            f"  base_url: https://api.anthropic.com\n  api_key: {ENDPOINT_KEY}\n",
            head=f"model:\n  base_url: {LOCAL_MAIN}\n",
        )
        self.assert_routed(self.set_route(), "anthropic")

    def test_a_route_without_a_provider_is_not_affected(self) -> None:
        self.write_config(head=f"model:\n  base_url: {LOCAL_MAIN}\n")
        self.assertEqual(self.call(action="set", model="local-llama")["status"], "routed")


class ManagedLayerTest(GuardTestCase):
    def test_a_managed_direct_endpoint_refuses_the_route(self) -> None:
        self.write_managed(f"delegation:\n  base_url: {ENDPOINT}\n")
        before = self.write_config("  max_iterations: 5\n")
        result = self.set_route()
        self.assert_refused(result, before, "direct_endpoint_overrides_provider", ["delegation.base_url"])
        self.assertIn("from Hermes' managed config", result["error"])

    def test_a_managed_route_key_refuses_the_route(self) -> None:
        for key, value in (("provider", "openrouter"), ("model", "pinned"), ("reasoning_effort", "low"),
                           ("provider", "''")):
            with self.subTest(key=key, value=value):
                self.write_managed(f"delegation:\n  {key}: {value}\n")
                before = self.write_config("  max_iterations: 5\n")
                for args in ({"model": "m", "provider": "anthropic"}, {"model": "m"}):
                    result = self.call(action="set", **args)
                    self.assert_refused(result, before, "managed_config_pins_route", [f"delegation.{key}"])

    def test_a_managed_local_main_endpoint_meets_the_bypass(self) -> None:
        self.write_managed(f"model:\n  base_url: {LOCAL_MAIN}\n")
        before = self.write_config(head="model:\n  default: llama3\n")
        self.assert_refused(self.set_route(), before, "local_endpoint_bypass")

    def test_an_empty_managed_leaf_replaces_only_the_leaf_hermes_merges(self) -> None:
        user = f"model:\n  default: llama3\n  base_url: {LOCAL_MAIN}\n"
        # Hermes moves a root base_url or api_base, or a model.api_base, into
        # model.base_url only when it is set, so an empty one leaves the
        # person's endpoint where it was.
        for managed in ("api_base: ''\n", "base_url: ''\n", "model:\n  api_base: ''\n"):
            with self.subTest(managed=managed):
                self.write_managed(managed)
                before = self.write_config(head=user)
                self.assert_refused(self.set_route(), before, "local_endpoint_bypass")
        # An empty provider there replaces the person's pin.
        self.write_managed("model:\n  provider: ''\n")
        before = self.write_config(head=f"model:\n  base_url: {LOCAL_MAIN}\n  provider: custom\n")
        self.assert_refused(self.set_route(), before, "local_endpoint_bypass")
        # An empty model.base_url there replaces the person's endpoint.
        self.write_managed("model:\n  base_url: ''\n")
        self.write_config(head=user)
        self.assert_routed(self.set_route(), "anthropic")

    def test_a_managed_section_written_on_one_line_is_read(self) -> None:
        cases = (
            (
                f"delegation: {{base_url: '{ENDPOINT}'}}\n",
                "direct_endpoint_overrides_provider", ["delegation.base_url"],
            ),
            ("delegation: {provider: openrouter}\n", "managed_config_pins_route", ["delegation.provider"]),
            (f"model: {{base_url: '{LOCAL_MAIN}'}}\n", "local_endpoint_bypass", ["model.base_url", "model.provider"]),
        )
        for managed, reason, keys in cases:
            with self.subTest(managed=managed):
                self.write_managed(managed)
                before = self.write_config("  max_iterations: 5\n", head="model:\n  default: llama3\n")
                self.assert_refused(self.set_route(), before, reason, keys)

    def test_a_managed_delegation_given_one_value_replaces_the_section(self) -> None:
        # Hermes' merge puts any value but null in place of the person's
        # section, so the route OMH writes would be dropped.
        for value in ("''", "false", "0", "off", "inherit"):
            with self.subTest(value=value):
                self.write_managed(f"delegation: {value}\n")
                before = self.write_config("  max_iterations: 5\n")
                for args in ({"model": "m", "provider": "anthropic"}, {"model": "m"}):
                    result = self.call(action="set", **args)
                    self.assert_refused(result, before, "managed_config_pins_route", ["delegation"])
                    self.assertIn("whole delegation section", result["error"])

    def test_a_null_or_empty_managed_delegation_routes(self) -> None:
        for managed in ("delegation:\n", "delegation: ~\n", "delegation: null  # none\n", "delegation: {}\n"):
            with self.subTest(managed=managed):
                self.write_managed(managed)
                self.write_config("  max_iterations: 5\n")
                self.assert_routed(self.set_route(), "anthropic")

    def test_a_managed_provider_pin_lifts_the_bypass(self) -> None:
        self.write_managed("model:\n  provider: custom\n")
        self.write_config(head=f"model:\n  base_url: {LOCAL_MAIN}\n")
        self.assert_routed(self.set_route(), "anthropic")

    def test_a_managed_file_that_cannot_be_read_refuses(self) -> None:
        before = self.write_config("  max_iterations: 5\n")
        (self.managed_dir / "config.yaml").write_bytes(b"delegation:\n  note: \xff\xfe\n")
        self.assert_refused(self.set_route(), before, "managed_config_unreadable")
        self.write_managed("a: 1\na: 2\n")
        self.assert_refused(self.set_route(), before, "managed_config_unreadable")
        for managed in (
            "delegation:\n  <<: *x\n",
            "delegation: *x\n",
            "delegation: {base_url: a, base_url: b}\n",
            # A value on the header line and lines under it.
            "delegation: {}\n  provider: openrouter\n",
        ):
            self.write_managed(managed)
            self.assert_refused(self.set_route(), before, "managed_config_unreadable")
        with mock.patch.object(guard.Path, "read_bytes", side_effect=PermissionError("denied")):
            result = self.set_route()
        self.assert_refused(result, before, "managed_config_unreadable")
        self.assertIn("PermissionError", result["error"])

    def test_no_managed_file_and_unrelated_managed_keys_route(self) -> None:
        self.write_config("  max_iterations: 5\n")
        self.assert_routed(self.set_route(), "anthropic")
        self.write_managed("display:\n  skin: plain\n")
        self.assert_routed(self.set_route(), "anthropic")

    def test_the_managed_directory_is_found_the_way_hermes_finds_it(self) -> None:
        etc = str(Path("/etc/hermes"))
        with mock.patch.dict(os.environ, {"HERMES_MANAGED_DIR": ""}), \
                mock.patch.object(guard.Path, "is_dir", autospec=True, side_effect=lambda self: str(self) == etc), \
                mock.patch.object(guard.Path, "read_bytes", autospec=True, return_value=b"a: 1\n"):
            path, text, error = guard.managed_config_layer()
        self.assertEqual((path, text, error), (str(Path(etc) / "config.yaml"), "a: 1\n", ""))
        # An override that is not a directory means no layer at all, never
        # /etc/hermes (get_managed_dir, managed_scope.py:45-57).
        not_a_dir = self.managed_dir / "file"
        not_a_dir.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(not_a_dir)}), \
                mock.patch.object(guard.Path, "is_dir", autospec=True, side_effect=lambda self: str(self) == etc):
            self.assertEqual(guard.managed_config_layer(), ("", None, ""))


class StatusNamesTheEndpointTest(GuardTestCase):
    """A route that names no provider runs on delegation.base_url; status says so."""

    def test_status_names_the_endpoint_a_route_without_a_provider_runs_on(self) -> None:
        self.write_config(f"  base_url: {ENDPOINT}\n  api_key: {ENDPOINT_KEY}\n")
        self.assertEqual(self.call(action="set", model="local-llama")["status"], "routed")

        status = self.call(action="status")

        self.assertEqual(status["route"]["model"], "local-llama")
        self.assertEqual(
            status["child_endpoint"], {"from": "config.yaml", "host": "127.0.0.1:8000", "provider": "custom"}
        )
        self.assertNotIn(ENDPOINT_KEY, json.dumps(status))

    def test_status_names_no_endpoint_when_the_child_goes_by_provider(self) -> None:
        for shape in ("  max_iterations: 5\n", "  base_url: ''\n", f"  provider: Bedrock\n  base_url: {ENDPOINT}\n"):
            with self.subTest(shape=shape):
                self.write_config(shape)
                self.assertIsNone(self.call(action="status")["child_endpoint"])

    def test_status_names_a_managed_endpoint_and_one_it_cannot_read(self) -> None:
        managed = str(self.managed_dir / "config.yaml")
        self.write_config(f"  base_url: {ENDPOINT}\n")
        self.write_managed("delegation:\n  base_url: https://api.anthropic.com\n")
        self.assertEqual(
            self.call(action="status")["child_endpoint"],
            {"from": managed, "host": "api.anthropic.com", "provider": "anthropic"},
        )
        for text in ("delegation:\n  base_url: ${ENDPOINT}\n", "delegation: ''\n", "delegation: *x\n"):
            with self.subTest(managed=text):
                self.write_managed(text)
                self.assertEqual(
                    self.call(action="status")["child_endpoint"], {"from": managed, "host": "", "provider": ""}
                )
        (self.managed_dir / "config.yaml").unlink()
        for shape in (f"  base_url: >\n    {ENDPOINT}\n", f"  base_url:\n  - {ENDPOINT}\n"):
            with self.subTest(shape=shape):
                self.write_config(shape)
                self.assertEqual(
                    self.call(action="status")["child_endpoint"], {"from": "config.yaml", "host": "", "provider": ""}
                )


class RestoreIsNotASwitchTest(GuardTestCase):
    def test_restoring_the_persons_provider_beside_a_direct_endpoint_is_not_refused(self) -> None:
        for person in ("person-provider", "bedrock"):
            with self.subTest(person=person):
                self.write_config(f"  provider: '{person}'\n")
                routed = write_route_with_baseline(
                    self.home, omh_home=self.omh_home, session_id="s1", task_id="t1",
                    model="routed-model", provider="og",
                )
                self.assertEqual(routed["status"], "routed", routed)
                with self.config.open("a", encoding="utf-8") as handle:
                    handle.write(f"  base_url: {ENDPOINT}\n")

                result = restore_delegation_baseline(
                    self.home, omh_home=self.omh_home, trigger="session_end", require_writer_session="s1",
                )

                self.assertEqual(result["status"], "restored", result)
                self.assertEqual(read_delegation_route(self.home), {"provider": person})
                self.assertIn(f"base_url: {ENDPOINT}", self.config.read_text(encoding="utf-8"))

    def test_an_exhausted_chain_restores_beside_a_direct_endpoint(self) -> None:
        write_chain(self.omh_home, ["only-model"])
        self.write_config("  max_iterations: 50\n")
        self.assertEqual(self.call(action="set", category="quick")["status"], "routed")
        with self.config.open("a", encoding="utf-8") as handle:
            handle.write(f"  base_url: {ENDPOINT}\n")

        result = self.call(action="fallback", category="quick")

        self.assertEqual(result["status"], "exhausted_to_inherit", result)
        self.assertEqual(read_delegation_route(self.home), {})

    def test_the_raw_writer_guards_only_when_asked(self) -> None:
        self.write_config(f"  base_url: {ENDPOINT}\n")
        asked = write_delegation_route(self.home, model="m", provider="anthropic", guard_route_endpoint=True)
        self.assertEqual(asked["status"], "error")
        self.assertEqual(read_delegation_route(self.home), {})

        unasked = write_delegation_route(self.home, model="m", provider="anthropic")
        self.assertEqual(unasked["status"], "routed")
        self.assertEqual(read_delegation_route(self.home), {"model": "m", "provider": "anthropic"})


if __name__ == "__main__":
    unittest.main()
