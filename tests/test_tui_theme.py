"""The OMH TUI's role-based theme over the shipped palettes."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
import unittest

from omh.skin_pack import SKIN_THEMES, hex_to_rgb, skin_colors
from omh.tui.style import Style
from omh.tui.theme import (
    DEFAULT_LAUNCH, DEFAULT_THEME, GLYPH_ROLES, LAUNCH_PRESETS, TEXT_CONTRAST, TEXT_ROLES, build_theme, contrast_ratio,
    is_light, surface_for, theme_named, with_prompt_band,
)

# The backgrounds every palette is measured on: none reported (the palette's own status-bar background stands in
# for a dark terminal), pure white and black, and the near-white and near-black defaults common terminals ship.
LIGHT_BACKGROUNDS = ("#FFFFFF", "#F6F8FA", "#FDF6E3")
DARK_BACKGROUNDS = ("#000000", "#1E1E1E", "#002B36")
BAND_ROLES = ("ok_band", "error_band")


def luminance(colour: str) -> float:
    """WCAG 2 relative luminance, written out here rather than borrowed from the module under test."""
    rgb = hex_to_rgb(colour)
    assert rgb is not None, colour
    linear = []
    for value in rgb:
        channel = value / 255
        linear.append(channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4)
    red, green, blue = linear
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class ThemeTests(unittest.TestCase):
    def test_every_shipped_palette_paints_every_role_but_the_terminals_own_text(self) -> None:
        for skin in SKIN_THEMES:
            for background in (None, *LIGHT_BACKGROUNDS):
                theme = build_theme(skin, background)
                for field in fields(theme):
                    # `tool` joined the terminal's own foreground with the rail and kind colours (2026-10-22): a call's
                    # name is bold in its family's hue (`kinds`, tested in test_tui_rail_kinds.py), else the default foreground.
                    # An unhighlighted menu row's name joined them too (2026-10-27): menus have no fill, and the
                    # highlighted row is the pointer and `menu_current` (tested below).
                    # `prompt_band` is the app's to set from the launch look (`with_prompt_band`), not the palette's.
                    if field.name in ("name", "text", "user", "tool", "menu", "prompt_band"):
                        continue
                    if background is None and field.name in BAND_ROLES:
                        continue  # no band is painted over a background nobody reported
                    with self.subTest(theme=skin.short_name, background=background, role=field.name):
                        value = getattr(theme, field.name)
                        for style in value if isinstance(value, tuple) else (value,):  # a gradient is its steps
                            self.assertTrue(style.fg or style.bg, "a role without any colour paints as plain text")

    def test_menus_have_no_fill_and_the_highlight_reads_on_light_and_dark(self) -> None:
        # The owner's screenshot of 2026-10-27: every completion row ran on a teal band that stopped where its words
        # did, so the right edges were ragged, and the highlighted band was barely lighter. A menu has no fill on
        # any palette or background; its highlighted name is bold in the skill hue at 4.5:1 on either side.
        for skin in SKIN_THEMES:
            for background in (None, *LIGHT_BACKGROUNDS, *DARK_BACKGROUNDS):
                theme = build_theme(skin, background)
                with self.subTest(theme=skin.short_name, background=background):
                    self.assertEqual((theme.menu.bg, theme.menu_current.bg), (None, None))
                    self.assertEqual(theme.menu, Style())
                    self.assertEqual(theme.menu_current, theme.slash)
                    self.assertTrue(theme.menu_current.bold)
                    surface = background or surface_for(skin, None)
                    self.assertGreaterEqual(contrast_ratio(theme.menu_current.fg, surface), TEXT_CONTRAST)

    def test_a_sent_prompt_s_band_is_each_launch_look_s_subtle_tint_and_none_without_a_background(self) -> None:
        skin = SKIN_THEMES[0]
        for background in (*LIGHT_BACKGROUNDS, *DARK_BACKGROUNDS):
            bands = {look: with_prompt_band(build_theme(skin, background), skin, background, look).prompt_band.bg
                     for look in LAUNCH_PRESETS}
            with self.subTest(background=background):
                self.assertEqual(len(set(bands.values())), len(LAUNCH_PRESETS), "each look its own tint")
                for band in bands.values():
                    self.assertTrue(1.08 <= contrast_ratio(band, background) <= 1.8, band)  # seen, and quiet
        self.assertEqual(with_prompt_band(build_theme(skin), skin, None, DEFAULT_LAUNCH).prompt_band, Style())

    def test_body_text_and_the_prompt_mark_are_the_terminals_default_foreground(self) -> None:
        # A light terminal showed near-white body text on white (1.08:1); the default foreground is readable on
        # whatever background the person chose.
        for skin in SKIN_THEMES:
            for background in (None, *LIGHT_BACKGROUNDS, *DARK_BACKGROUNDS):
                theme = build_theme(skin, background)
                with self.subTest(theme=skin.short_name, background=background):
                    self.assertIsNone(theme.text.fg)
                    self.assertIsNone(theme.user.fg)
                    self.assertTrue(theme.user.bold)

    def test_every_role_read_as_text_reaches_4_5_and_every_glyph_3_on_every_background(self) -> None:
        for skin in SKIN_THEMES:
            for background in (None, *LIGHT_BACKGROUNDS, *DARK_BACKGROUNDS):
                theme = build_theme(skin, background)
                surface = background or skin_colors(skin.skin_name)["status_bar_bg"]
                for role, minimum in [*((role, 4.5) for role in TEXT_ROLES), *((role, 3.0) for role in GLYPH_ROLES)]:
                    with self.subTest(theme=skin.short_name, background=background, role=role):
                        self.assertGreaterEqual(contrast(getattr(theme, role).fg, surface), minimum)

    def test_a_light_background_gets_light_diff_bands_under_the_default_foreground(self) -> None:
        for skin in SKIN_THEMES:
            for background in LIGHT_BACKGROUNDS:
                theme = build_theme(skin, background)
                for role in ("diff_add", "diff_add_word", "diff_remove", "diff_remove_word"):
                    with self.subTest(theme=skin.short_name, background=background, role=role):
                        style = getattr(theme, role)
                        self.assertIsNone(style.fg)  # the terminal's dark text
                        self.assertGreaterEqual(contrast(style.bg, "#000000"), 7.0)
        dark = build_theme(SKIN_THEMES[0], "#000000")
        self.assertEqual(dark.diff_add.bg, skin_colors(SKIN_THEMES[0].skin_name)["diff_added"])

    def test_a_dark_diff_band_carries_its_own_text_colour(self) -> None:
        # With no background reported the theme assumes dark; a light terminal's own dark text on the dark band
        # read 1.5:1, so the band says what colour its text is, as it did before the light-terminal work.
        for skin in SKIN_THEMES:
            for background in (None, *DARK_BACKGROUNDS):
                theme = build_theme(skin, background)
                for role in ("diff_add", "diff_remove"):
                    with self.subTest(theme=skin.short_name, background=background, role=role):
                        style = getattr(theme, role)
                        self.assertIsNotNone(style.fg)
                        self.assertGreaterEqual(contrast(style.fg, style.bg), 7.0)

    def test_light_is_read_from_the_background_itself(self) -> None:
        self.assertTrue(all(is_light(colour) for colour in LIGHT_BACKGROUNDS))
        self.assertFalse(any(is_light(colour) for colour in DARK_BACKGROUNDS))

    def test_roles_follow_the_palette(self) -> None:
        amber = theme_named("amber")
        # The palette's accent is the banner title's alone; `running` is state, in the approved blue on every palette
        # (#2223: decoupled from the delegation view's edge gradient, which keeps its own stops).
        self.assertEqual(amber.brand.fg, skin_colors("omh-amber")["ui_accent"])
        self.assertEqual((amber.running.fg, theme_named("amber", "#FFFFFF").running.fg), ("#58A6FF", "#2374C9"))
        self.assertNotEqual(amber.running.fg, amber.dag_words[-1].fg)
        self.assertEqual(amber.diff_add.bg, skin_colors("omh-amber")["diff_added"])
        # A colour that clears the background is the palette's own; amber's comments (3.6:1) are lifted for reading.
        for role in ("syntax_keyword", "syntax_string", "syntax_number"):
            with self.subTest(role=role):
                self.assertEqual(getattr(amber, role).fg, skin_colors("omh-amber")[role])
        self.assertNotEqual(amber.syntax_comment.fg, skin_colors("omh-amber")["syntax_comment"])

    def test_keywords_and_comments_differ_from_text_even_where_the_colours_match(self) -> None:
        mono = theme_named("mono")
        self.assertNotEqual(mono.syntax_keyword, mono.text)
        self.assertNotEqual(mono.syntax_comment, mono.text)

    def test_hints_read_at_text_contrast_on_every_palette(self) -> None:
        # With no background reported, the status-bar background stands in for the dark terminal behind the info line.
        for skin in SKIN_THEMES:
            theme = build_theme(skin)
            background = skin_colors(skin.skin_name).get("status_bar_bg") or theme.status.bg
            with self.subTest(theme=skin.short_name):
                self.assertGreaterEqual(contrast(theme.hint.fg, background), 4.5)
                self.assertGreaterEqual(contrast(theme.muted.fg, background), 4.5)
                self.assertGreaterEqual(contrast(theme.rule.fg, background), 3.0)

    def test_accept_plan_and_the_hint_differ_on_the_info_line_on_every_palette(self) -> None:
        # The composer's box no longer changes with the mode; the info line says it in words and in the mode's colour.
        for skin in SKIN_THEMES:
            for background in (None, *LIGHT_BACKGROUNDS, *DARK_BACKGROUNDS):
                theme = build_theme(skin, background)
                roles = {"hint": theme.hint.fg, "accept": theme.mode_accept.fg, "plan": theme.mode_plan.fg}
                with self.subTest(theme=skin.short_name, background=background):
                    self.assertEqual(len(set(roles.values())), 3, roles)

    def test_a_palette_that_clears_the_background_keeps_its_own_colours(self) -> None:
        sky = theme_named("sky")
        self.assertEqual(sky.brand.fg, skin_colors("omh")["ui_accent"])
        self.assertEqual(sky.syntax_string.fg, skin_colors("omh")["syntax_string"])
        self.assertNotEqual(theme_named("sky", "#FFFFFF").brand.fg, skin_colors("omh")["ui_accent"])  # 2.0:1 on white

    def test_the_chrome_is_neutral_grey_on_every_palette(self) -> None:
        # The rail and kind colours (2026-10-22): hue says state, a tool's family or the brand; the transcript's
        # chrome (a call's arguments, what it printed, its glyphs, the composer's rules) is one neutral grey per role,
        # the same on every palette, lifted only as far as its floor takes.
        for background in ("#0E151E", "#FFFFFF"):
            chrome = {(role, getattr(build_theme(skin, background), role).fg)
                      for skin in SKIN_THEMES for role in ("muted", "faint", "hint", "dim", "rule")}
            with self.subTest(background=background):
                self.assertEqual(len({role for role, _colour in chrome}), len(chrome))  # one colour per role
                for _role, colour in chrome:
                    red, green, blue = hex_to_rgb(colour)
                    self.assertLessEqual(max(red, green, blue) - min(red, green, blue), 24, colour)  # grey, not a hue

    def test_names_resolve_by_short_or_skin_name_and_default_otherwise(self) -> None:
        self.assertEqual(theme_named("omh-crimson").name, "crimson")
        self.assertEqual(theme_named("crimson").name, "crimson")
        self.assertEqual(theme_named("not-a-theme"), DEFAULT_THEME)
        self.assertEqual(DEFAULT_THEME.name, "sky")


class InfoLineContrastTests(unittest.TestCase):
    """The info line has no bar: its words and marks are measured against the terminal's background."""

    def line(self, background: str | None, skin: object, stage: str) -> tuple[list, str]:
        from test_tui_modes import make_app
        from test_tui_panels import hud_payload
        from omh.tui.panels import HudSnapshot
        from omh.tui.theme import surface_for

        theme = build_theme(skin, background)  # type: ignore[arg-type]
        app, _client = make_app(theme=theme, busy=True, rows=24, cols=200)
        app.background = background  # the badge's look is painted against it, as the theme is
        payload = hud_payload()
        payload["repeat"] = {"status": "observed", "consecutive": 4, "period": 2, "intercepted": 1, "stage": stage}
        payload["activity"].update(live=True, post_tool_call_observed=True, open_call_count=2, oldest_open_elapsed_seconds=12)
        app.hud = HudSnapshot(app.stored_id, payload, 0.0)
        app.approvals_off = app.plan_mode = True  # both labels, in the modes' colours
        app.model, app.context_percent, app.effort = "some/model", 96.0, "high"
        return list(app._info_line(200)), surface_for(skin, background)  # type: ignore[arg-type]

    def test_every_coloured_word_on_the_info_line_reaches_4_5_against_the_background(self) -> None:
        for skin in SKIN_THEMES:
            for background in (None, *LIGHT_BACKGROUNDS, *DARK_BACKGROUNDS):
                for stage in ("watching", "blocking", "approval"):
                    spans, surface = self.line(background, skin, stage)
                    self.assertTrue(any(span.text == "ctx 96%" for span in spans))
                    for span in spans:
                        if not span.text.strip() or span.style.fg is None:
                            continue
                        minimum = 4.5 if any(char.isalnum() for char in span.text) else 3.0
                        with self.subTest(theme=skin.short_name, background=background, stage=stage, text=span.text):
                            self.assertIsNone(span.style.bg)
                            self.assertGreaterEqual(contrast(span.style.fg, surface), minimum)


class StatusBarTintTests(unittest.TestCase):
    """The audit's G8: on a light terminal the status bar was the palette's dark bar, a band across a light screen.

    The bar now backs only the pager's footer; the info line and the sticky prompt have none.
    """

    def test_a_light_terminal_gets_a_light_bar_of_the_palettes_own_and_a_dark_or_unknown_one_keeps_it(self) -> None:
        tints = set()
        for skin in SKIN_THEMES:
            own = build_theme(skin, None).status.bg  # no background reported: the palette's bar
            assert own is not None
            self.assertFalse(is_light(own), skin.short_name)
            for background in DARK_BACKGROUNDS:
                with self.subTest(theme=skin.short_name, background=background):
                    self.assertEqual(build_theme(skin, background).status.bg, own)
            for background in LIGHT_BACKGROUNDS:
                with self.subTest(theme=skin.short_name, background=background):
                    bar = build_theme(skin, background).status.bg
                    assert bar is not None
                    self.assertTrue(is_light(bar), bar)
                    self.assertNotEqual(bar.upper(), background.upper())  # still a bar, not the background
                    if background == "#FFFFFF":
                        tints.add(bar)
        self.assertGreater(len(tints), 1)  # each palette tints its own bar, not one grey for all


class DimCarriesNoWordsTests(unittest.TestCase):
    """`dim` reaches only the 3:1 a glyph needs, so no span the TUI draws in it may hold words."""

    def test_no_span_written_out_in_dim_holds_a_letter_or_a_digit(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src" / "tui"
        found = []
        for path in sorted(source.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Span" and len(node.args) == 2):
                    continue
                style = node.args[1]
                if not (isinstance(style, ast.Attribute) and style.attr == "dim"):
                    continue
                text = node.args[0]
                parts = [text] if isinstance(text, ast.Constant) else text.values if isinstance(text, ast.JoinedStr) else []
                words = "".join(part.value for part in parts if isinstance(part, ast.Constant) and isinstance(part.value, str))
                if any(ch.isalnum() for ch in words):
                    found.append(f"{path.name}:{node.lineno} {words!r}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()


class MidGreyContrastTests(unittest.TestCase):
    """Every ink with a contrast floor meets it on every shipped skin over the greys and the two common backgrounds.

    The #2256 review measured crimson's text inks at 4.36:1 on #707070: the lift stopped at the palette's own ink
    (`banner_text`, #FFEDE6), which white passes and that ink does not. A sweep at a step of 2 found every skin
    short somewhere between #6E6E6E and #747474.
    """

    BACKGROUNDS = (*(f"#{level:02X}{level:02X}{level:02X}" for level in range(0x40, 0xF1, 2)), "#0D1117", "#FFFFFF")

    def test_every_ink_meets_its_floor_on_every_skin_and_grey(self) -> None:
        from omh.tui.theme import (
            EDGE_CONTRAST, LAUNCH_PRESETS, TEXT_CONTRAST, aura_moves, contrast_ratio, launch_palette, launch_styles,
            surface_for,
        )

        short = []
        for skin in SKIN_THEMES:
            for background in self.BACKGROUNDS:
                theme = build_theme(skin, background)
                surface = surface_for(skin, background)
                inks = [(role, getattr(theme, role).fg, TEXT_CONTRAST) for role in TEXT_ROLES]
                inks += [(role, getattr(theme, role).fg, EDGE_CONTRAST) for role in GLYPH_ROLES]
                inks += [("kinds", style.fg, TEXT_CONTRAST) for style in theme.kinds]
                inks += [("dag_words", style.fg, TEXT_CONTRAST) for style in theme.dag_words]
                inks += [("dag_edges", style.fg, EDGE_CONTRAST) for style in theme.dag_edges]
                if aura_moves(theme):
                    inks += [("aura", style.fg, TEXT_CONTRAST) for style in (*theme.aura, *theme.aura_glint)]
                for preset in LAUNCH_PRESETS:
                    styles = launch_styles(skin, background, launch_palette(preset, surface))
                    inks += [(f"{preset}.brand", styles.brand.fg, TEXT_CONTRAST),
                             (f"{preset}.label", styles.label.fg, TEXT_CONTRAST),
                             (f"{preset}.edge", styles.edge.fg, EDGE_CONTRAST)]
                short += [(skin.short_name, background, role, round(contrast_ratio(fg, surface), 2), floor)
                          for role, fg, floor in inks if contrast_ratio(fg, surface) < floor]
        self.assertEqual(short[:12], [], f"{len(short)} inks under their floor")

    def test_a_short_ink_on_a_light_background_goes_on_toward_black(self) -> None:
        # No shipped palette needs the lift's second leg on a light background, so the sweep never runs it there: a
        # leg that always went to white would pass it. Here the ink given, a mid grey, is short on a light background
        # (one well inside the light side, and one just past the black/white crossover) and the lift must go darker.
        from omh.tui.theme import BLACK, TEXT_CONTRAST, _lifted, contrast_ratio

        for background, toward in (("#BBBBBB", "#777777"), ("#767676", "#9A9A9A")):
            with self.subTest(background=background):
                self.assertTrue(is_light(background))
                self.assertLess(contrast_ratio(toward, background), TEXT_CONTRAST)
                lifted = _lifted("#CCCCCC", toward, background, TEXT_CONTRAST)
                self.assertGreaterEqual(contrast_ratio(lifted, background), TEXT_CONTRAST)
                self.assertGreater(contrast_ratio(lifted, "#FFFFFF"), contrast_ratio(toward, "#FFFFFF"), "darker")
                self.assertLess(contrast_ratio(lifted, BLACK), contrast_ratio(toward, BLACK), "toward black")


class LiftCostTests(unittest.TestCase):
    """The lift answers what its stepwise definition does, and a repaint of the launch logo asks for no lift again
    (QA P1: the logo lifted some 600 glyph colours on every paint, re-parsing each hex at every step)."""

    def test_the_lift_is_the_stepwise_mix_it_names(self) -> None:
        from omh.tui.theme import BLACK, EDGE_CONTRAST, WHITE, _lifted, _mix

        def stepwise(colour: str, toward: str, background: str, minimum: float) -> str:
            end = BLACK if is_light(background) else WHITE
            for start, target in ((colour, toward), (toward, end)):
                for step in range(21):
                    candidate = _mix(start, target, step / 20)
                    if hex_to_rgb(candidate) is not None and contrast(candidate, background) >= minimum:
                        return candidate
            return end

        greys = [f"#{value:02X}{value:02X}{value:02X}" for value in range(0, 256, 17)]
        tints = ["#F778BA", "#d58cf0", "#4A4F66", "#C5DE70", "#FFEDE6", "#707070"]
        for colour in greys + tints:
            for background in greys[::3] + ["#1E1E2E", "#FDF6E3"]:
                for toward in ("#FFEDE6", BLACK, WHITE):
                    for minimum in (EDGE_CONTRAST, TEXT_CONTRAST):
                        self.assertEqual(_lifted(colour, toward, background, minimum),
                                         stepwise(colour, toward, background, minimum),
                                         (colour, toward, background, minimum))

    def test_a_second_logo_paint_lifts_nothing_again(self) -> None:
        from omh.skin_pack import default_theme
        from omh.tui.theme import _lifted, launch_palette, logo_lines

        skin = default_theme()
        for preset in LAUNCH_PRESETS:
            with self.subTest(preset=preset):
                palette = launch_palette(preset, surface_for(skin, "#1E1E1E"))
                first = logo_lines(skin, "#1E1E1E", palette)
                misses = _lifted.cache_info().misses
                self.assertEqual(logo_lines(skin, "#1E1E1E", palette), first)
                self.assertEqual(_lifted.cache_info().misses, misses)
