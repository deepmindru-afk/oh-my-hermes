"""Turn signals: the working row's tokens, the done line that closes a turn, the end-of-turn alert, the title.

The working row reads `✻ Verb… (12s · ↓1.2k tokens · esc to interrupt)`,
where the tokens are what the turn has written so far (the rise of the
`output` counter in Hermes' usage blocks, plus an estimate of what streamed
since the last one, never fewer than the row last said). A turn
closes with a done line, `✻ 34s · 3 tools · ↓1.2k`. A turn that ran 10 s or
more ends with the terminal's attention signal (OSC 9 where the terminal
shows it as a notification, OSC 777 where that is its form, the bell
elsewhere; `OMH_TUI_BELL=0` keeps it quiet). The terminal title says the
session's title and what it is doing (OSC 2), set only when it changes and
put back as it was on the way out.
"""

from __future__ import annotations

import unittest

from _tui_lines import line_text
from test_tui_gateway_contract import recorded_events, recorded_rows
from test_tui_choice_ui import info_row
from test_tui_modes import SID, drawn, make_app
from omh.tui import gateway_contract as contract
from omh.tui.app import BELL, TITLE_POP, TITLE_PUSH, TURN_ALERT_SECONDS, App
from omh.tui.gateway import Event
from omh.tui.transcript import TurnSummaryBlock


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def usage(output: int) -> dict:
    return {"usage": {"output": output, "context_used": 1000, "context_max": 100_000}}


def writes(app: App) -> str:
    return "".join(app.terminal.writes)  # type: ignore[attr-defined]


def turn(app: App, clock: Clock, seconds: float, tools: int = 0, out: int | None = None) -> None:
    """A prompt sent, `tools` tool calls, `seconds` on the clock, and the turn's message.complete."""
    app._send_prompt("go")
    app._on_event(Event("message.start", SID, {}))
    for number in range(tools):
        app._on_event(Event("tool.start", SID, {"tool_id": f"t{number}", "name": "terminal", "context": "ls"}))
        app._on_event(Event("tool.complete", SID, {"tool_id": f"t{number}", "name": "terminal", "result": {"output": ""}}))
    if out is not None:
        app._on_event(Event("session.usage", SID, usage(out)))
    clock.now += seconds
    app._on_event(Event("message.complete", SID, {"text": "done", "status": "complete"}))


class WorkingRowTests(unittest.TestCase):
    def test_the_working_row_counts_what_the_turn_has_written(self) -> None:
        clock = Clock()
        app, _client = make_app(clock=clock)
        app._on_event(Event("session.info", SID, usage(500)))
        app._send_prompt("go")
        app._on_event(Event("message.start", SID, {}))
        clock.now += 12
        row = line_text(app._tail(80)[-2])  # the working row, its tip under it
        self.assertIn("(12s · esc to interrupt)", row)  # nothing written yet
        app._on_event(Event("session.usage", SID, usage(1700)))
        self.assertIn("(12s · ↓1.2k tokens · esc to interrupt)", line_text(app._tail(80)[-2]))


    def test_the_first_turn_of_a_fresh_session_counts_its_tokens_in_hermes_recorded_order(self) -> None:
        # Hermes 6c61c3e27d (parity.jsonl todo_turn): the session's first usage block, `session.info` with output 0,
        # comes after prompt.submit, so there is no counter yet when the turn begins.
        app, _client = make_app(clock=Clock())
        app._send_prompt("record a plan")
        rows = [row["frame"]["params"] for row in recorded_rows("parity", "todo_turn")
                if row["dir"] == "in" and row["frame"].get("method") == "event"]
        working = []
        for params in rows:
            app._on_event(Event(params["type"], SID, params.get("payload", {})))
            if params["type"] == "session.usage":
                working.append(line_text(app._tail(80)[-2]))
        self.assertIn("↓63 tokens", working[0])
        summary = [block for block in app.transcript.blocks if isinstance(block, TurnSummaryBlock)]
        self.assertEqual([block.output_tokens for block in summary], [238])

    def test_a_new_session_counts_from_its_own_counter_not_the_last_sessions(self) -> None:
        app, _client = make_app(clock=Clock())
        app._on_event(Event("session.info", SID, usage(5000)))  # the session being left had written 5000
        app._adopt_session({"session_id": "live-2", "stored_session_id": "stored-2", "info": {}})
        app._send_prompt("go")
        for out in (0, 63):
            app._on_event(Event("session.usage", "live-2", usage(out)))
        self.assertIn("↓63 tokens", line_text(app._tail(80)[-2]))


def counted(output: int, calls: int) -> dict:
    return {"usage": {"output": output, "calls": calls}}


def fresh_session(clock: Clock) -> App:
    """A session as Hermes bf0203a794 opens one: its answer and its `session.info` events carry no usage block."""
    app, _client = make_app(clock=clock)
    app._adopt_session({"session_id": SID, "stored_session_id": "stored-1", "info": {"model": "kimi-k3"}})
    return app


def working(app: App, cols: int = 100) -> str:
    return line_text(app._tail(cols)[-2])


class LiveCountTests(unittest.TestCase):
    """Between usage blocks the working row counts what streams, and a usage block puts it back on Hermes' counter.

    Measured on Hermes bf0203a794: a turn's only usage blocks are a
    `session.usage` after each API call ends and the closing
    `message.complete`, a one-call turn's 1.1k-token reply streamed for 7 s
    with no block at all, and each turn's counters start over (`calls` back at
    1).
    """

    def test_the_count_rises_as_the_reply_and_the_reasoning_stream(self) -> None:
        clock = Clock()
        app = fresh_session(clock)
        app._send_prompt("write a paragraph about tide pools")
        app._on_event(Event("message.start", SID, {}))
        self.assertNotIn("tokens", working(app))
        app._on_event(Event("reasoning.delta", SID, {"text": "The user wants one paragraph. " * 4}))  # 120 chars
        self.assertIn("↓30 tokens", working(app))
        app._on_event(Event("message.delta", SID, {"text": "Tide pools form where the ocean retreats. " * 5}))
        self.assertIn("↓83 tokens", working(app))  # 30 + 210 / 4, rounded up

    def test_wide_characters_count_one_each(self) -> None:
        clock = Clock()
        app = fresh_session(clock)
        app._send_prompt("조수 웅덩이에 대해 써줘")
        app._on_event(Event("message.delta", SID, {"text": "조수 웅덩이는 바위틈에 남은 물웅덩이다."}))  # 17 wide, 5 narrow
        self.assertIn("↓19 tokens", working(app))

    def test_a_tool_calls_arguments_count_and_already_streamed_commentary_does_not_count_twice(self) -> None:
        clock = Clock()
        app = fresh_session(clock)
        app._send_prompt("read notes.txt")
        app._on_event(Event("message.delta", SID, {"text": "I'll read the file."}))  # 19 chars
        app._on_event(Event("message.interim", SID, {"text": "I'll read the file.", "already_streamed": True}))
        app._on_event(Event("tool.start", SID, {"tool_id": "t1", "name": "read_file", "args": {"path": "notes.txt"}}))
        self.assertIn("↓10 tokens", working(app))  # (19 + 21) / 4 for `{"path": "notes.txt"}`
        app._on_event(Event("message.interim", SID, {"text": "Commentary that never streamed"}))  # 30 chars
        self.assertIn("↓18 tokens", working(app))

    def test_a_usage_block_reconciles_the_count_and_it_never_steps_back(self) -> None:
        clock = Clock()
        app = fresh_session(clock)
        app._send_prompt("go")
        app._on_event(Event("message.start", SID, {}))
        app._on_event(Event("message.delta", SID, {"text": "x" * 400}))
        self.assertIn("↓100 tokens", working(app))
        app._on_event(Event("session.usage", SID, counted(80, 1)))  # the estimate ran over: the row holds
        self.assertIn("↓100 tokens", working(app))
        app._on_event(Event("message.delta", SID, {"text": "y" * 40}))  # 80 counted + 10 streamed is still under 100
        self.assertIn("↓100 tokens", working(app))
        app._on_event(Event("session.usage", SID, counted(250, 2)))  # the counter is ahead: the row moves up to it
        self.assertIn("↓250 tokens", working(app))
        app._on_event(Event("message.delta", SID, {"text": "z" * 80}))
        self.assertIn("↓270 tokens", working(app))  # 250 counted, 20 streamed since
        app._on_event(Event("message.complete", SID, {"text": "done", "usage": {"output": 262, "calls": 3}}))
        summary = [block for block in app.transcript.blocks if isinstance(block, TurnSummaryBlock)]
        self.assertEqual([block.output_tokens for block in summary], [262])  # the done line is the counter's

    def test_a_fresh_sessions_first_turn_counts_its_first_block_whole(self) -> None:
        # Hermes bf0203a794, first turn of a new session: no usage until the one call ends, then session.usage and
        # message.complete both read 1098. Before, the first block became the turn's base and the turn read 0.
        clock = Clock()
        app = fresh_session(clock)
        app._send_prompt("write a 150-word paragraph about tide pools, no tools")
        app._on_event(Event("message.start", SID, {}))
        app._on_event(Event("session.info", SID, {"running": True, "stored_session_id": "stored-1"}))
        app._on_event(Event("message.delta", SID, {"text": "Tide pools form where the ocean retreats."}))
        self.assertIn("↓11 tokens", working(app))
        app._on_event(Event("session.usage", SID, counted(1098, 1)))
        self.assertIn("↓1.1k tokens", working(app))
        clock.now += 7
        app._on_event(Event("message.complete", SID, {"text": "Tide pools…", "usage": {"output": 1098, "calls": 1}}))
        drawn(app)
        screen = app.terminal.screen.history() + app.terminal.screen.lines()  # type: ignore[attr-defined]
        self.assertIn("  ✻ 7s · ↓1.1k", screen)

    def test_a_counter_that_starts_over_each_turn_counts_each_turn_from_zero(self) -> None:
        # Hermes bf0203a794, three turns of one session: 9, 50 and 21 tokens, each with `calls` 1.
        clock = Clock()
        app = fresh_session(clock)
        for output in (9, 50, 21):
            app._send_prompt("say it")
            app._on_event(Event("message.start", SID, {}))
            app._on_event(Event("session.usage", SID, counted(output, 1)))
            self.assertIn(f"↓{output} tokens", working(app))
            app._on_event(Event("message.complete", SID, {"text": "ok", "usage": {"output": output, "calls": 1}}))
        summary = [block for block in app.transcript.blocks if isinstance(block, TurnSummaryBlock)]
        self.assertEqual([block.output_tokens for block in summary], [9, 50, 21])

    def test_once_the_counter_started_over_a_turns_first_block_counts_whole_even_past_two_calls(self) -> None:
        # Hermes' usage ticker sends one block a second at most, so two quick calls can arrive as one block (70, 2):
        # more output and more calls than the last turn ended on, which alone would read as a rise of 20.
        clock = Clock()
        app = fresh_session(clock)
        for output, calls in ((9, 1), (50, 1), (70, 2)):
            app._send_prompt("say it")
            app._on_event(Event("message.complete", SID, {"text": "ok", "usage": {"output": output, "calls": calls}}))
        summary = [block for block in app.transcript.blocks if isinstance(block, TurnSummaryBlock)]
        self.assertEqual([block.output_tokens for block in summary], [9, 50, 70])

    def test_a_counter_that_keeps_counting_across_turns_counts_each_turns_rise(self) -> None:
        clock = Clock()
        app = fresh_session(clock)
        for output, calls in ((9, 1), (59, 2), (80, 3)):
            app._send_prompt("say it")
            app._on_event(Event("message.start", SID, {}))
            app._on_event(Event("message.complete", SID, {"text": "ok", "usage": {"output": output, "calls": calls}}))
        summary = [block for block in app.transcript.blocks if isinstance(block, TurnSummaryBlock)]
        self.assertEqual([block.output_tokens for block in summary], [9, 50, 21])

    def test_a_narrow_row_drops_the_live_count_before_the_time(self) -> None:
        clock = Clock()
        app = fresh_session(clock)
        app._send_prompt("go")
        app._on_event(Event("message.delta", SID, {"text": "x" * 1716}))
        clock.now += 4
        for cols, said in ((60, "(4s · ↓429 tokens · esc to interrupt)"), (40, "(4s · esc to interrupt)")):
            with self.subTest(cols=cols):
                self.assertTrue(working(app, cols).endswith(said), working(app, cols))


class DoneLineTests(unittest.TestCase):
    def test_a_turn_closes_with_how_long_it_took_its_tools_and_its_tokens(self) -> None:
        clock = Clock()
        app, _client = make_app(clock=clock)
        app._on_event(Event("session.info", SID, usage(100)))
        turn(app, clock, 34, tools=3, out=1300)
        block = app.transcript.blocks[-1]
        self.assertIsInstance(block, TurnSummaryBlock)
        drawn(app)
        screen = app.terminal.screen.history() + app.terminal.screen.lines()  # type: ignore[attr-defined]
        self.assertIn("  ✻ 34s · 3 tools · ↓1.2k", screen)

    def test_one_tool_and_no_tokens_read_as_such(self) -> None:
        clock = Clock()
        app, _client = make_app(clock=clock)
        turn(app, clock, 2, tools=1)
        block = app.transcript.blocks[-1]
        self.assertEqual((block.seconds, block.tools, block.output_tokens), (2.0, 1, None))
        from omh.tui.transcript import block_rows
        from omh.tui.theme import DEFAULT_THEME

        self.assertEqual(line_text(block_rows(block, 60, DEFAULT_THEME)[0]), "  ✻ 2s · 1 tool")


class AlertTests(unittest.TestCase):
    def ends(self, seconds: float, environ: dict[str, str]) -> str:
        clock = Clock()
        app, _client = make_app(clock=clock, environ=environ)
        before = len(writes(app))
        turn(app, clock, seconds)
        return writes(app)[before:]

    def test_a_long_turn_ends_with_the_signal_the_terminal_shows(self) -> None:
        self.assertIn(BELL, self.ends(TURN_ALERT_SECONDS, {"TERM_PROGRAM": "Apple_Terminal"}))
        self.assertIn("\x1b]9;", self.ends(TURN_ALERT_SECONDS, {"TERM_PROGRAM": "iTerm.app"}))
        self.assertIn("\x1b]777;notify;", self.ends(TURN_ALERT_SECONDS, {"TERM": "foot"}))

    def test_a_short_turn_and_a_quiet_setting_signal_nothing(self) -> None:
        self.assertNotIn(BELL, self.ends(TURN_ALERT_SECONDS - 0.5, {"TERM_PROGRAM": "Apple_Terminal"}))
        self.assertNotIn("\x1b]9;", self.ends(TURN_ALERT_SECONDS - 0.5, {"TERM_PROGRAM": "iTerm.app"}))
        quiet = self.ends(60, {"TERM_PROGRAM": "iTerm.app", "OMH_TUI_BELL": "0"})
        self.assertNotIn("\x1b]9;", quiet)
        self.assertNotIn(BELL, quiet)


class TitleTests(unittest.TestCase):
    def test_the_title_names_the_session_and_its_state_once_per_change(self) -> None:
        app, _client = make_app()
        (title_event,) = [payload for kind, payload in recorded_events("parity") if kind == "session.title"][-1:]
        app._on_event(Event("session.title", SID, title_event))
        drawn(app)
        drawn(app)
        written = writes(app)
        self.assertEqual(written.count(TITLE_PUSH), 1)
        self.assertEqual(written.count("\x1b]2;Create todo plan for ship change · idle\x1b\\"), 1)
        app.turn_pending = True
        drawn(app)
        self.assertIn("\x1b]2;Create todo plan for ship change · running\x1b\\", writes(app))
        app._shutdown()
        self.assertTrue(writes(app).endswith(TITLE_POP) or TITLE_POP in writes(app))

    def test_a_title_holds_no_control_characters_and_a_session_without_one_says_hermes(self) -> None:
        app, _client = make_app()
        drawn(app)
        self.assertIn("\x1b]2;Hermes · idle\x1b\\", writes(app))
        app._on_event(Event("session.title", SID, {"session_id": "stored", "title": "a\x07b\x1b]2;evil\x1b\\c"}))
        drawn(app)
        self.assertIn("\x1b]2;abc · idle\x1b\\", writes(app))  # the escape sequence goes whole, as in any gateway text

    def test_the_info_line_leads_its_right_side_with_the_title_cut_short_and_drops_it_first(self) -> None:
        title = "Rename every production cluster before the freeze"
        rows: dict[int, str] = {}
        for cols in range(40, 121):
            app, _client = make_app(cols=cols)
            app.model = "deepseek/deepseek-v4.1-flash"
            app._on_event(Event("session.title", SID, {"session_id": "stored", "title": title}))
            screen = drawn(app)
            rows[cols] = row = screen[info_row(screen)]
            self.assertLessEqual(len(row.rstrip()), cols, row)  # the frame's width, at every width
            if "Rename" in row:  # never shortened past its cap, and never shown at the model's cost
                self.assertIn(f"{title[:23]}… · deepseek/deepseek-v4.1-flash · ", row)
        self.assertIn("Rename every production… · ", rows[120])
        self.assertNotIn("Rename", rows[89])  # 89 columns: the badge, and the right side without the title
        self.assertIn("deepseek/deepseek-v4.1-flash", rows[89])  # the title went first; the model is still whole
        self.assertNotIn("Rename", rows[60])
        app, _client = make_app(cols=120)
        app.model = "deepseek/deepseek-v4.1-flash"
        screen = drawn(app)
        self.assertNotIn(" · deepseek", screen[info_row(screen)])  # no title, nothing before the model

    def test_a_session_info_naming_a_title_shows_it_and_one_naming_none_keeps_it(self) -> None:
        # A resumed session's info names its title; over a shared gateway the rename can come that way too.
        app, _client = make_app(cols=120)
        app._on_event(Event("session.info", SID, {"title": "Fix the\x1b]2;x\x1b\\ login  bug", "model": "m"}))
        self.assertEqual(app.session_title, "Fix the login bug")
        app._on_event(Event("session.info", SID, {"title": "", "model": "m"}))  # a lazy info names none
        self.assertEqual(app.session_title, "Fix the login bug")
        screen = drawn(app)
        self.assertIn("Fix the login bug · m", screen[info_row(screen)])

    def test_the_contract_declares_the_title_event(self) -> None:
        self.assertIn("session.title", contract.APP_EVENTS)
        self.assertEqual(contract.SESSION_TITLE, "title")


class PlanRefreshTests(unittest.TestCase):
    def test_hermes_todo_snapshot_has_the_hud_read_now_instead_of_at_its_next_poll(self) -> None:
        app, _client = make_app()
        (payload,) = [payload for kind, payload in recorded_events("parity") if kind == "todo.updated"]
        app._on_event(Event("todo.updated", SID, payload))
        self.assertEqual(app.poller.refreshes, 1)  # type: ignore[attr-defined]
        app._on_event(Event("todo.updated", "another-session", payload))
        self.assertEqual(app.poller.refreshes, 1)  # type: ignore[attr-defined]
        self.assertIn("todo.updated", contract.APP_EVENTS)


class DocumentedSignalTests(unittest.TestCase):
    def test_installation_names_the_threshold_the_code_uses(self) -> None:
        from pathlib import Path

        prose = " ".join((Path(__file__).resolve().parents[1] / "docs/INSTALLATION.md").read_text(encoding="utf-8").split())
        self.assertIn(f"a turn that ran {TURN_ALERT_SECONDS:g} s or more ends with the terminal's own notification", prose)


if __name__ == "__main__":
    unittest.main()
