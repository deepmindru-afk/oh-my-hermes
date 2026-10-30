"""A route with no provider must not inherit one that cannot serve its model.

Observed live: a session ran on `custom:cliproxy`, a `providers.cliproxy`
block listing only Claude models. A category whose head is `gpt-6.1-sol`
(chain entry with no provider) was routed with no provider, so its children
inherited cliproxy and every one died on "model not found" before a single
model call. The inheritance guard let it through twice over:

- the session spelled its provider `custom:cliproxy` while the entitlement
  record held the bare block name `cliproxy`, so the lookup found nothing and
  answered "unknown";
- even with the spellings matched, the record called cliproxy a `gateway`,
  which serves everything by default, so the answer stayed "unknown".

Hermes itself says what the block serves: its `models:` list, the same list
`find_custom_provider_identity_by_model` reads. These tests pin that the
guard now reads it, pins the one provider known to serve the model instead of
refusing, refuses with a usable answer when the choice is not single, and is
silent everywhere it was silent before. Every test uses temporary homes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.hermes_delegation import (  # noqa: E402
    provider_block_name,
    provider_family_for,
    providers_known_to_serve_alias,
)
from omh.plugin_bundle.omh.provider_detection import config_provider_declared_models  # noqa: E402
from omh.plugin_bundle.omh.tools.delegate_route_tool import omh_delegate_route_handler  # noqa: E402

CLIPROXY_SESSION = (
    "model:\n"
    "  default: claude-opus-5-5\n"
    "  provider: custom:cliproxy\n"
    "providers:\n"
    "  cliproxy:\n"
    "    api: http://127.0.0.1:8317/v1\n"
    "    key_env: CLIPROXY_API_KEY\n"
    "    default_model: claude-sonnet-5-5\n"
    "    transport: chat_completions\n"
    "    models:\n"
    "      claude-opus-5-5:\n"
    "        context_length: 1000000\n"
    "      claude-sonnet-5-5:\n"
    "        context_length: 1000000\n"
    "  opengateway:\n"
    "    api: https://apis.opengateway.ai/v1\n"
    "    key_env: OPENGATEWAY_API_KEY\n"
    "    default_model: z-ai/glm-5.3-flash-ultrafast\n"
    "    transport: chat_completions\n"
)


class DeclaredModelsTests(unittest.TestCase):
    def test_every_spelling_hermes_matches_is_read(self) -> None:
        text = (
            "providers:\n"
            "  mapping:\n"
            "    models:\n"
            "      Model-A:\n"
            "        context_length: 4\n"
            "      model-b: {}\n"
            "  listing:\n"
            "    models:\n"
            "      - plain-id\n"
            "      - id: by-id\n"
            "      - name: by-name\n"
            "      - context_length: 9\n"
            "  default-only:\n"
            "    default_model: lonely\n"
            "model:\n"
            "  provider: mapping\n"
        )
        self.assertEqual(
            config_provider_declared_models(text),
            {
                "mapping": frozenset({"model-a", "model-b"}),
                "listing": frozenset({"plain-id", "by-id", "by-name"}),
            },
        )

    def test_a_block_with_no_models_section_is_absent_not_empty(self) -> None:
        self.assertEqual(config_provider_declared_models("providers:\n  og:\n    api: https://x\n"), {})
        self.assertEqual(config_provider_declared_models(""), {})

    def test_a_models_key_outside_providers_is_not_read(self) -> None:
        text = "auxiliary:\n  vision:\n    models:\n      - something\nproviders:\n  og:\n    api: https://x\n"
        self.assertEqual(config_provider_declared_models(text), {})


class ProviderSpellingTests(unittest.TestCase):
    def test_custom_prefix_and_bare_block_name_are_one_provider(self) -> None:
        recorded = {"providers": {"cliproxy": "gateway", "custom:og": "openai-codex"}}
        self.assertEqual(provider_family_for("custom:cliproxy", recorded), "gateway")
        self.assertEqual(provider_family_for("og", recorded), "openai-codex")
        self.assertEqual(provider_family_for("custom:missing", recorded), "")
        self.assertEqual(provider_block_name("custom:cliproxy"), "cliproxy")
        self.assertEqual(provider_block_name("openai-codex"), "openai-codex")

    def test_known_servers_leave_out_relays(self) -> None:
        recorded = {"providers": {"cliproxy": "gateway", "openai-codex": "openai-codex", "og": "openrouter"}}
        self.assertEqual(providers_known_to_serve_alias("gpt-6.1-sol", recorded), ("openai-codex",))
        self.assertEqual(providers_known_to_serve_alias("some-uncatalogued-model", recorded), ())


class InheritedProviderRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.hermes_home = root / "hermes"
        self.omh_home = root / "omh"
        self.hermes_home.mkdir()
        (self.omh_home / "routing").mkdir(parents=True)
        self.config = self.hermes_home / "config.yaml"
        self.config.write_text(CLIPROXY_SESSION, encoding="utf-8")
        self.entitlements({"cliproxy": "gateway", "openai-codex": "openai-codex", "opengateway": "gateway"})
        self.chains(
            {
                "deep": [
                    {"model": "gpt-6.1-sol", "reasoning_effort": "high"},
                    {"model": "claude-opus-5-5", "reasoning_effort": "high"},
                ],
                "architect": [
                    {"model": "claude-opus-5-5", "reasoning_effort": "high"},
                    {"model": "gpt-6.1-sol", "reasoning_effort": "high"},
                ],
            }
        )

    def entitlements(self, providers: dict[str, str]) -> None:
        (self.omh_home / "routing" / "providers.json").write_text(
            json.dumps({"schema_version": "provider_entitlements/v1", "providers": providers, "subscription_clis": []}),
            encoding="utf-8",
        )

    def chains(self, categories: dict[str, list[dict[str, str]]]) -> None:
        (self.omh_home / "routing" / "model-chains.json").write_text(
            json.dumps({"schema_version": "mixture_chain_overrides/v1", "categories": categories}),
            encoding="utf-8",
        )

    def call(self, **args: object) -> dict:
        payload = {"hermes_home": str(self.hermes_home), "omh_home": str(self.omh_home), **args}
        return json.loads(omh_delegate_route_handler(payload, session_id="s-1", task_id="s-1"))

    def delegation_block(self) -> str:
        text = self.config.read_text(encoding="utf-8")
        return text.split("delegation:", 1)[1] if "delegation:" in text else ""

    def test_the_incident_head_is_pinned_to_the_one_provider_that_serves_it(self) -> None:
        result = self.call(action="set", category="deep")
        self.assertEqual(result["status"], "routed", result)
        self.assertEqual(result["applied"]["model"], "gpt-6.1-sol")
        self.assertEqual(result["applied"]["provider"], "openai-codex")
        self.assertEqual(
            result["provider_pinned"], {"inherited_provider": "custom:cliproxy", "pinned_provider": "openai-codex"}
        )
        self.assertIn("provider: 'openai-codex'", self.delegation_block())

    def test_a_model_the_inherited_block_lists_is_left_on_it(self) -> None:
        result = self.call(action="set", category="architect")
        self.assertEqual(result["status"], "routed", result)
        self.assertNotIn("provider", result["applied"])
        self.assertNotIn("provider_pinned", result)
        self.assertNotIn("provider:", self.delegation_block())

    def test_a_fallback_onto_an_unlisted_model_is_pinned_too(self) -> None:
        self.assertEqual(self.call(action="set", category="architect")["status"], "routed")
        result = self.call(action="fallback", category="architect")
        self.assertEqual(result["status"], "fell_back", result)
        self.assertEqual(result["applied"]["provider"], "openai-codex")
        self.assertEqual(result["provider_pinned"]["inherited_provider"], "custom:cliproxy")

    def test_no_single_known_server_refuses_and_names_only_providers_that_could(self) -> None:
        self.entitlements({"cliproxy": "gateway", "opengateway": "gateway"})
        before = self.config.read_text(encoding="utf-8")
        result = self.call(action="set", category="deep")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["inherited_provider"], "custom:cliproxy")
        # The listed block cannot serve it, so it is no candidate; the relay
        # that lists nothing still may.
        self.assertEqual(result["providers_serving_alias"], ["opengateway"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), before, "a refusal writes nothing")

    def test_two_known_servers_are_not_chosen_between(self) -> None:
        self.entitlements({"cliproxy": "gateway", "openai-codex": "openai-codex", "openai": "openai"})
        self.config.write_text(CLIPROXY_SESSION + "  openai:\n    api: https://api.openai.com/v1\n", encoding="utf-8")
        result = self.call(action="set", category="deep")
        self.assertEqual(result["status"], "error", result)
        self.assertIn("openai-codex", result["providers_serving_alias"])

    def test_a_relay_that_lists_nothing_stays_unknown_and_dispatches(self) -> None:
        self.config.write_text(CLIPROXY_SESSION.replace("provider: custom:cliproxy", "provider: custom:opengateway"), encoding="utf-8")
        result = self.call(action="set", category="deep")
        self.assertEqual(result["status"], "routed", result)
        self.assertNotIn("provider_pinned", result)
        self.assertNotIn("provider", result["applied"])

    def test_an_explicit_provider_is_never_second_guessed(self) -> None:
        result = self.call(action="set", model="gpt-6.1-sol", provider="opengateway")
        self.assertEqual(result["status"], "routed", result)
        self.assertEqual(result["applied"]["provider"], "opengateway")
        self.assertNotIn("provider_pinned", result)

    def test_an_unrecorded_provider_is_never_pinned(self) -> None:
        # The catalog knows openai-codex serves it, but this machine never
        # linked or recorded it: nothing is pinned to a provider the person
        # did not configure.
        self.entitlements({"cliproxy": "gateway"})
        result = self.call(action="set", category="deep")
        self.assertEqual(result["status"], "error", result)
        self.assertNotIn("provider:", self.delegation_block())


if __name__ == "__main__":
    unittest.main()
