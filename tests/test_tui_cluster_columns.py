"""Columns are counted by grapheme cluster wherever the TUI maps a cell to text or styles text cell by cell.

`width` paints a ZWJ family, an emoji with a skin tone, a VS16 emoji and a
flag as one 2-cell glyph and a base with its combining marks as one unit. The
pager's selection (`_cells`, `_word_at`, `_reversed`), the recall glint
(`transcript._glinted`) and the running box's aura in `/dag`
(`panels._aura_box`) counted code points instead: a double click on a word
after `👨‍👩‍👧` copied nothing, a drag copied half a family, and a style
change landed inside a cluster, which breaks the glyph on the terminal (QA
round 2026-10-28, R-B).
"""

from __future__ import annotations

import base64
import unicodedata
import unittest
from dataclasses import replace

from _vt_screen import VTScreen
from test_tui_bot_board import NOW, bots
from test_tui_modes import drawn, make_app
from omh.plugin_bundle.omh.delegation_view import Segment
from omh.tui import bot_board, subagents
from omh.tui.keys import BUTTON_LEFT, Mouse
from omh.tui.pager import Pager, _reversed
from omh.tui.panels import _aura_box
from omh.tui.style import TRUECOLOR, Span, Style
from omh.tui.theme import DEFAULT_THEME
from omh.tui.transcript import AssistantBlock, _glinted
from omh.tui.width import cluster_width, clusters, line_width

# One cluster each, with what a code-point count made of it.
LEADS = {
    "cjk": "한",
    "decomposed hangul": "한",  # 한 as jamo, as macOS pastes it
    "combining mark": "a̲",  # a with a combining low line: no precomposed form
    "zwj family": "👨‍👩‍👧",  # painted 2, counted 6
    "skin tone": "👍🏻",  # painted 2, counted 4
    "vs16": "☀️",  # painted 2, counted 1 or 3
    "flag": "🇰🇷",  # two regional indicators, one glyph
}
KITTY = {"TERM": "xterm-kitty"}
# A cluster of two or more code points whose first is not in the rest of the page, for a cut to fall inside.
CUT_LEADS = {
    "zwj family": "👨\u200d👩\u200d👧",
    "skin tone": "👍🏻",
    "flag": "🇰🇷",
    "combining mark": "z\u0301",
    "decomposed hangul": "\u1112\u1161\u11ab",
}


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def text_of(line) -> str:
    return "".join(span.text for span in line)


def no_cluster_split(line) -> bool:
    """Every span boundary of `line` falls between two clusters of its text."""
    return [cluster for span in line for cluster in clusters(span.text)] == clusters(text_of(line))


def click(pager: Pager, x: int, now: float) -> str:
    """A plain press and release on column `x` of the first row; what the press picked (a double click's word)."""
    picked = pager.handle_mouse(Mouse(BUTTON_LEFT, x, 1), 10, now)
    pager.handle_mouse(Mouse(BUTTON_LEFT, x, 1, pressed=False), 10, now + 0.01)
    return picked


def drag(pager: Pager, start: int, end: int) -> str:
    pager.handle_mouse(Mouse(BUTTON_LEFT, start, 1), 10, 0.0)
    pager.handle_mouse(Mouse(BUTTON_LEFT, end, 1, motion=True), 10, 0.1)
    return pager.handle_mouse(Mouse(BUTTON_LEFT, end, 1, pressed=False), 10, 0.2)


class PagerSelectionTests(unittest.TestCase):
    def test_a_double_click_on_a_word_after_a_cluster_copies_that_word(self) -> None:
        for name, lead in {**LEADS, "two cjk": "한글"}.items():
            with self.subTest(lead=name):
                pager = Pager("log", [(Span(f"{lead} target"),)])
                pager.frame(40, 10, DEFAULT_THEME)
                x = line_width((Span(f"{lead} "),)) + 1  # the painted column of `t`, as a report counts it
                click(pager, x, 0.0)
                self.assertEqual(click(pager, x, 0.1), "target")

    def test_a_word_of_combining_marks_is_one_word(self) -> None:
        pager = Pager("log", [(Span("x a̲b̲c̲ y"),)])
        pager.frame(40, 10, DEFAULT_THEME)
        click(pager, 4, 0.0)  # on b
        self.assertEqual(click(pager, 4, 0.1), "a̲b̲c̲")

    def test_a_drag_over_either_cell_of_a_cluster_copies_all_of_it(self) -> None:
        for name, lead in LEADS.items():
            with self.subTest(lead=name):
                width = cluster_width(nfc(lead))
                pager = Pager("log", [(Span(f"{lead} x"),)])
                pager.frame(40, 10, DEFAULT_THEME)
                self.assertEqual(drag(pager, 1, width + 1), lead)  # its cells and the space after it, as written
                pager = Pager("log", [(Span(f"{lead} x"),)])
                pager.frame(40, 10, DEFAULT_THEME)
                self.assertEqual(drag(pager, width, width + 2), f"{lead} x")  # from its last cell on

    def test_the_highlight_reverses_a_whole_cluster_and_moves_no_column(self) -> None:
        plain = Style()
        lit = replace(plain, reverse=True)
        for name, lead in LEADS.items():
            with self.subTest(lead=name):
                line = (Span(f"{lead} x"),)
                self.assertEqual(_reversed(line, 0, 0), (Span(lead, lit), Span(" x", plain)))
                pager = Pager("log", [line])
                pager.frame(40, 10, DEFAULT_THEME)
                pager.handle_mouse(Mouse(BUTTON_LEFT, 1, 1), 10, 0.0)
                pager.handle_mouse(Mouse(BUTTON_LEFT, 1, 1, motion=True), 10, 0.1)
                screen = VTScreen(10, 40)
                screen.feed(pager.frame(40, 10, DEFAULT_THEME))
                width = cluster_width(nfc(lead))
                self.assertEqual(screen.line(0), f"{nfc(lead)} x")
                self.assertEqual([7 in screen.attrs_at(0, column) for column in range(width + 2)],
                                 [True] * width + [False, False])

    def test_a_copy_hands_back_the_code_points_it_was_given_not_their_nfc_form(self) -> None:
        # A decomposed macOS path pasted into a shell or grep must match the file it names, byte for byte.
        for word in ("cafe\u0301", "\u1112\u1161\u11ab\u1100\u1173\u11af", "re\u0301sume\u0301.txt"):
            with self.subTest(word=word):
                pager = Pager("log", [(Span(f"open {word} now"),)])
                pager.frame(40, 10, DEFAULT_THEME)
                x = line_width((Span("open "),)) + 2
                click(pager, x, 0.0)
                self.assertEqual(click(pager, x, 0.1), word)
                pager = Pager("log", [(Span(f"open {word} now"),)])
                pager.frame(40, 10, DEFAULT_THEME)
                self.assertEqual(drag(pager, 1, 40), f"open {word} now")
                self.assertEqual(text_of(_reversed((Span(word),), 0, 99)), word)

    def test_the_logs_pager_copies_a_decomposed_path_as_written(self) -> None:
        now = [100.0]
        app, client = make_app(rows=12, cols=60, environ=KITTY, clock=lambda: now[0])
        app.pbcopy = lambda text: False  # type: ignore[method-assign]  # never the real clipboard
        path = "/Users/me/cafe\u0301/notes.txt"
        client.stderr_tail = lambda: [f"read {path} ok"]  # type: ignore[attr-defined]
        app._logs("")
        drawn(app)
        screen = app.terminal.screen  # type: ignore[attr-defined]
        y = next(index for index, line in enumerate(screen.lines()) if "notes" in line)
        x = screen.line(y).index("/Users") + 2
        for _ in range(2):
            app._on_input(Mouse(BUTTON_LEFT, x, y + 1))
            app._on_input(Mouse(BUTTON_LEFT, x, y + 1, pressed=False))
            now[0] += 0.1
        sent = [text.removeprefix("52;c;") for text in screen.osc if text.startswith("52;c;")]
        self.assertEqual([base64.b64decode(text.rstrip("\x07")).decode("utf-8") for text in sent], [path])

    def test_the_app_copies_the_word_after_a_zwj_family_with_the_clipboard_stubbed(self) -> None:
        now = [100.0]
        app, _client = make_app(rows=12, cols=60, environ=KITTY, clock=lambda: now[0])
        handed: list[str] = []
        app.pbcopy = lambda text: handed.append(text) is None  # type: ignore[method-assign]  # never the real clipboard
        app.transcript.blocks.append(AssistantBlock("👨‍👩‍👧 target here"))
        app._history("")
        drawn(app)
        screen = app.terminal.screen  # type: ignore[attr-defined]
        y = next(index for index, line in enumerate(screen.lines()) if "target" in line)
        cells = screen._text[y]
        x = next(column for column in range(len(cells)) if cells[column:column + 6] == list("target")) + 1
        for _ in range(2):
            app._on_input(Mouse(BUTTON_LEFT, x, y + 1))
            app._on_input(Mouse(BUTTON_LEFT, x, y + 1, pressed=False))
            now[0] += 0.1
        sent = [text.removeprefix("52;c;") for text in screen.osc if text.startswith("52;c;")]
        self.assertEqual([base64.b64decode(text.rstrip("\x07")).decode("utf-8") for text in sent], ["target"])
        self.assertEqual(handed, [])


class ClusterCutTests(unittest.TestCase):
    """A cut by character count (a log row, a payload field, a bot's message) ends between two clusters."""

    def test_a_log_row_and_a_payload_field_are_cut_before_the_cluster_the_limit_falls_in(self) -> None:
        for name, lead in CUT_LEADS.items():
            with self.subTest(lead=name):
                text = "a" * 9 + lead + " tail"  # the limit, 10, falls inside `lead`
                self.assertEqual(subagents._cut(text, 10), "a" * 9 + subagents.CUT_MARK)
                self.assertEqual(subagents._one_line(text, 10), "a" * 9 + subagents.CUT_MARK)
                # A row is painted NFC (`wrap_line`) whether cut or not, so the cut keeps the painted form.
                self.assertEqual(subagents._cut(text, 9 + len(lead)), "a" * 9 + nfc(lead) + subagents.CUT_MARK)
                self.assertEqual(subagents._cut(text, len(text)), text)

    def test_a_bots_message_is_cut_before_the_cluster_the_limit_falls_in(self) -> None:
        for name, lead in CUT_LEADS.items():
            with self.subTest(lead=name):
                text = "a" * (bot_board.MESSAGE_SHOWN_CHARS - 1) + lead + " tail"
                snapshot = bot_board.BoardSnapshot(bot_board.BoardView("p", "s1"), bots(), (),
                                                   messages=(bot_board.MessageRow("user", text, NOW),), read_at=NOW)
                page = "".join(text_of(line) for line in bot_board.message_lines(snapshot, None, NOW, 200, DEFAULT_THEME))
                self.assertIn("a…", page)
                self.assertNotIn(lead[0], page, "no piece of the cluster is left before the cut")


class ClusterStyleTests(unittest.TestCase):
    def test_the_recall_glint_starts_on_the_painted_column_and_splits_no_cluster(self) -> None:
        style = Style(fg="#888888")
        for name, lead in LEADS.items():
            with self.subTest(lead=name):
                spans = _glinted(f"{lead}abcdefghij", style, cluster_width(nfc(lead)), DEFAULT_THEME)
                self.assertTrue(no_cluster_split(spans))
                self.assertEqual(spans[0], Span(nfc(lead), style))
                self.assertEqual(spans[1], Span("a", replace(DEFAULT_THEME.aura[0], bold=False)))

    def test_the_running_box_aura_reaches_its_right_border_and_splits_no_cluster(self) -> None:
        aura = {style.fg for style in (*DEFAULT_THEME.aura, *DEFAULT_THEME.aura_glint)}
        for name, lead in LEADS.items():
            with self.subTest(lead=name):
                row = (Segment("│ ", "node:running"), Segment(f"{lead} title", "node-title:running"),
                       Segment(" │", "node:running"))
                size = line_width(tuple(Span(segment.text) for segment in row))
                line = _aura_box(row, 1, DEFAULT_THEME, TRUECOLOR, 0, size, False, 0)
                self.assertEqual(text_of(line), nfc(f"│ {lead} title │"))
                self.assertTrue(no_cluster_split(line))
                self.assertEqual(line[-1].text[-1], "│")
                self.assertIn(line[-1].style.fg, aura, "the right border is the box's last cell")


if __name__ == "__main__":
    unittest.main()
