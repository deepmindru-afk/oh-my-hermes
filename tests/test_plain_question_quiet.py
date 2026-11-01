"""A plain knowledge question gets no skill candidate line; a work request keeps its hint."""

from __future__ import annotations

import unittest

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.plugin_bundle.omh.plain_question_cues import plain_question
from omh.plugin_bundle.omh.skill_shortlist import skill_candidates_for_turn

PLAIN = (
    "Explain TCP vs UDP briefly",
    "Explain what a closure is in JavaScript",
    "Explain Big O notation simply",
    "Explain the CAP theorem",
    "Can you explain what a hash map is?",
    "What's the difference between git merge and rebase?",
    "Explain the difference between let and const",
    "Best way to learn SQL?",
    "Explain what a JWT is",
    "Quick question: what is a webhook?",
    "빅오 표기법 쉽게 설명해줘",
    "TCP랑 UDP 차이 간단히 설명해줘",
    "깃 머지랑 리베이스 차이",
    "SQL 배우는 제일 좋은 방법?",
    "클로저가 뭐야?",
)

WORK = (
    "Explain this paper at beginner level",
    "explain this arxiv paper without dropping details",
    "Explain the attached PDF",
    "Explain why my CI fails after the merge",
    "Explain this stack trace from our server",
    "merge this PR after review",
    "review my PR #12",
    "What's the difference between these two files in my repo?",
    "Explain the code in src/main.py",
    "이 논문 쉽게 설명해줘",
    "첨부한 논문을 초보자 수준으로 설명해줘",
    "우리 레포 PR 머지해줘",
    "이 코드 설명해줘",
    "Explain TCP. Then explain UDP.",
    "Explain TCP\nand UDP",
    "Explain the difference between TCP and UDP and how each one handles loss, ordering, retransmission, "
    "flow control, windowing, and congestion control",
    "Explain what a closure is https://example.com/closure",
    "Explain this:\n```py\nx = 1\n```",
    "Explain `git rebase`",
    "TCP랑 UDP 차이를 아주 자세하게 처음부터 끝까지 빠짐없이 설명해줘 제발",
    "",
)


class PlainQuestionTest(unittest.TestCase):
    def test_plain_questions_are_plain(self) -> None:
        for message in PLAIN:
            with self.subTest(message=message):
                self.assertTrue(plain_question(message))

    def test_work_requests_are_not_plain(self) -> None:
        for message in WORK:
            with self.subTest(message=message):
                self.assertFalse(plain_question(message))

    def test_candidate_line_is_empty_for_a_plain_question(self) -> None:
        for message in PLAIN:
            with self.subTest(message=message):
                self.assertEqual(skill_candidates_for_turn(message), ())

    def test_phrase_match_keeps_its_hint(self) -> None:
        payload = awareness_route_hint("Explain this paper at beginner level")
        ids = [str(hint.get("id")) for hint in (payload or {}).get("hints", []) if isinstance(hint, dict)]
        self.assertTrue(any("paper" in hint_id for hint_id in ids), ids)


if __name__ == "__main__":
    unittest.main()
