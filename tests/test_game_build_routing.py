"""A request to build or fix the look of a browser game reaches `frontend`.

Measured on `afb4f11a5` before this guard: "three.js로 간단한 3D 게임 만들어줘"
and "phaser로 2D 플랫포머 게임 만들어줘" dispatched `deep-interview` on the
greenfield floor alone, "make a browser platformer game in canvas" clarified
toward `browser-operator` and `live-info-operator` (the word `game` is a live
score topic), and "이 게임 화면이 밋밋해 고쳐줘" fell through to the generic
picker. None reached `frontend`, which owns the rendered-scene craft bar and
the screenshot loop a game build needs.
"""

from __future__ import annotations

import unittest

from omh.routing import policy
from omh.wrapper.contract import build_chat_interaction_payload


def _route(message: str) -> dict:
    return build_chat_interaction_payload(message, source="discord")["route"]


def _top(route: dict) -> str:
    rows = route.get("recommendations") or []
    return str(rows[0]["skill"]) if rows else ""


class GameBuildRoutingTests(unittest.TestCase):
    BUILD_OR_POLISH = (
        "three.js로 간단한 3D 게임 만들어줘",
        "브라우저에서 돌아가는 슈팅 게임 만들어줘",
        "phaser로 2D 플랫포머 게임 만들어줘",
        "make a browser platformer game in canvas",
        "build a snake game in the browser with canvas",
        "이 게임 화면이 밋밋해 고쳐줘",
        "make the game feel juicier",
    )

    def test_building_or_polishing_a_browser_game_dispatches_frontend(self) -> None:
        for message in self.BUILD_OR_POLISH:
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual((route["action"], route["selected_skill"]), ("dispatch", "frontend"))

    def test_game_as_a_topic_does_not_name_frontend(self) -> None:
        for message in (
            "요즘 할만한 게임 추천해줘",
            "게임 회사 시장 규모 조사해줘",
            "explain game theory to me",
            "what game is on tonight",
            "make a game trailer from these clips",
            "게임 기획서 써줘",
            "이 게임 너무 재밌다",
            "how do I make a game in Unity?",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route.get("selected_skill"), "frontend")
                self.assertNotEqual(_top(route), "frontend")

    def test_lanes_that_already_own_a_game_request_keep_it(self) -> None:
        for message, owner in (
            ("game sprite frames from this sheet", "sprite-forge"),
            ("fix the collision bug in my game", "ultrawork"),
            ("게임 점프 버그 고쳐줘", "ultrawork"),
        ):
            with self.subTest(message=message):
                self.assertEqual(_route(message)["selected_skill"], owner)

    def test_native_engine_games_are_not_sent_to_the_web_ui_lane(self) -> None:
        for message in ("build a unity game", "godot로 게임 만들어줘", "언리얼로 FPS 게임 만들어줘"):
            with self.subTest(message=message):
                self.assertFalse(policy.game_build_guard_applies(policy.normalized_phrase(message)))


if __name__ == "__main__":
    unittest.main()
