"""The working glow (owner request 2026-10-28): while a turn runs, a light goes round the terminal's whole screen
in the launch look's logo colours, with a fading trail and no box.

The light runs along the screen's own edge: its top row, its last row, and
the first and last column of every row between. Only the light and its
trail are drawn; the rest of the edge is left blank. Its head is round: the
brightness rises over `NOSE` cells ahead of the head along a circle's
profile and is nearest white at the head itself (`GLINT`, on a dark
background), and behind it the trail runs through the logo's colours
(`Theme.glow_ring`) and fades out over `TRAIL_SHARE` of the way round.

It moves by the clock, not by the app's frames: the head is at
`(seconds % LOOP_SECONDS) / LOOP_SECONDS` of the way round, clockwise from
the top-left corner, whatever the screen's size, and between two cells it
lights both by how near it is, so it glides rather than steps. The way round
is measured through the middle of what each cell lights, in cell widths, a
row `ROW_HEIGHT` of them tall: so the light is as fast going down a side as
along the top, and turning a corner (the top row's upper half to a side's
whole cell) is as far as it looks, with no jump.

The top and bottom rows are drawn with half blocks (`▀`, `▄`), their outer
half lit. A side's cell, and a corner's, is filled whole with its background
colour where the terminal reported its background (`Theme.glow_surface` has a `bg`), so
the top band runs into the side without a step, and because a
terminal's line spacing leaves a half block short of the row and a side
drawn with them looks dashed; without a reported background (one guessed
for a dark screen would paint a dark cell on a light one) it is a half block
(`▌`, `▐`). The inner half of a top or bottom cell takes a faint tint
(`TINT`) under the same condition.

This module only says what the edge's cells are. The screen layer paints
them over the rows (`screen.LiveRegion`), and the app keeps the screen's edge
free for them: its rows are drawn a column in from each side, and the
region's last row, the edge's bottom, is its own blank row (`App._edged`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math

from .style import Line, Span, Style
from .theme import BLACK, WHITE, Theme, blend, is_light

EDGE_MARGIN = 1  # columns the rows are drawn in from each side of the screen, for the glow's sides
LOOP_SECONDS = 4.0  # the light's head goes once round the screen in this long
TICK_SECONDS = 0.05  # how often the app paints while the light moves
ROW_HEIGHT = 2  # a row is about this many cell widths tall
NOSE = 3.0  # cells of the way round the head's round front rises over
TRAIL_SHARE = 0.3  # of the way round, the trail behind the head fades out over
TRAIL_CURVE = 1.6  # how fast the trail fades: above 1 it falls off quickly behind the head, then lingers
PEAK = 0.8  # how much of the ring's colour the head takes, the rest the background's
GLINT = 0.45  # how far the head's colour goes toward white, on a dark background
TINT = 0.25  # the inner half's share of the outer half's colour, on the top and bottom rows
LEVELS = 24  # brightness steps: a cell keeps its bytes until its step changes
EDGE_MIN_COLS = 20  # a screen narrower than this, or shorter than EDGE_MIN_ROWS, keeps its edge and has no glow
EDGE_MIN_ROWS = 8

TOP, BOTTOM, LEFT, RIGHT = "▀", "▄", "▌", "▐"
TOP_LEFT, TOP_RIGHT, BOTTOM_RIGHT, BOTTOM_LEFT = "▛", "▜", "▟", "▙"


@dataclass(frozen=True)
class Edge:
    """The light's cells at one moment: the top and bottom rows, `cols` cells each, and the first (`left`) and last
    (`right`) column of each row between them, top first."""

    cols: int
    top: Line
    bottom: Line
    left: tuple[Line, ...]
    right: tuple[Line, ...]


def edge_fits(cols: int, rows: int) -> bool:
    """Whether a screen `cols` by `rows` is big enough for the glow and the margin it takes."""
    return cols >= EDGE_MIN_COLS and rows >= EDGE_MIN_ROWS


def _cells(cols: int, rows: int) -> tuple[list[tuple[int, int, str, float]], float]:
    """Every cell of the edge, clockwise from the top-left corner, as (row, col, glyph, where), `where` how far round
    the middle of what the cell lights is, in cell widths; and the whole way round.

    The way runs through the top row's upper half, the sides' middle columns and the bottom row's lower half: a
    rectangle `cols - 1` wide and `ROW_HEIGHT * rows - 1` tall, from the top-left cell's middle."""
    wide, tall = cols - 1, ROW_HEIGHT * rows - 1

    def down(row: int) -> float:  # a side cell's middle below the top row's upper half
        return ROW_HEIGHT * row + ROW_HEIGHT / 2 - 0.5

    top = [(0, col, TOP_LEFT if col == 0 else TOP_RIGHT if col == cols - 1 else TOP, float(col)) for col in range(cols)]
    right = [(row, cols - 1, RIGHT, wide + down(row)) for row in range(1, rows - 1)]
    bottom = [(rows - 1, col, BOTTOM_RIGHT if col == cols - 1 else BOTTOM_LEFT if col == 0 else BOTTOM,
               wide + tall + (cols - 1 - col)) for col in range(cols - 1, -1, -1)]
    left = [(row, 0, LEFT, 2 * wide + tall + (tall - down(row))) for row in range(rows - 2, 0, -1)]
    return top + right + bottom + left, 2.0 * (wide + tall)


def _dome(distance: float) -> float:
    """A circle's profile `distance` cells from the head: 1 at the head, 0 from `NOSE` cells on."""
    share = distance / NOSE
    return math.sqrt(1 - share * share) if share < 1 else 0.0


def brightness(back: float, trail: float) -> float:
    """How lit a cell is `back` cells behind the head (ahead of it where negative), `trail` cells the trail's length:
    the round front ahead, and behind, the round head or the fading trail, whichever is brighter."""
    if back < 0:
        return _dome(-back)
    return max(_dome(back), max(0.0, 1 - back / trail) ** TRAIL_CURVE)


def glow_style(theme: Theme, back: float, trail: float, whole: bool) -> Style | None:
    """A cell's colours `back` cells behind the head: the ring's colour that far along the trail, nearer white the
    nearer the head, as bright as `brightness` says, stepped to `LEVELS`. `whole` fills the cell with it (a side or
    a corner, where the theme may tint), else the glyph takes it and the background a faint tint where the theme allows one.
    None where the cell is not lit."""
    level = round(PEAK * brightness(back, trail) * LEVELS) / LEVELS
    if level <= 0:
        return None
    ring = theme.glow_ring
    colour = ring[min(int(max(back, 0.0) / trail * len(ring)), len(ring) - 1)].fg or WHITE
    surface, tint = _surface(theme)
    if not is_light(surface):
        colour = blend(colour, WHITE, GLINT * _dome(abs(back)))
    lit = blend(surface, colour, level)
    if whole and tint:
        return Style(bg=lit)
    return Style(fg=lit, bg=blend(surface, colour, level * TINT) if tint else None)


def _surface(theme: Theme) -> tuple[str, bool]:
    """The colour the glow fades into, and whether it is the background the terminal reported (so cells' backgrounds
    may be painted)."""
    known = theme.glow_surface
    return (known.bg, True) if known.bg is not None else (known.fg or BLACK, False)


def edge(cols: int, rows: int, theme: Theme, seconds: float) -> Edge:
    """The light round a `cols` by `rows` screen at clock time `seconds`."""
    cells, around = _cells(cols, rows)
    head = seconds % LOOP_SECONDS / LOOP_SECONDS * around
    trail = TRAIL_SHARE * around
    grid: dict[tuple[int, int], Span] = {}
    for row, col, glyph, where in cells:
        back = (head - where) % around
        if back > around - NOSE:
            back -= around  # just ahead of the head
        whole = glyph not in (TOP, BOTTOM)  # a side or a corner
        style = glow_style(theme, back, trail, whole=whole)
        if style is None:
            grid[row, col] = Span(" ", Style())
        else:
            grid[row, col] = Span(" " if whole and style.fg is None else glyph, style)
    return Edge(
        cols=cols,
        top=_runs(grid[0, col] for col in range(cols)),
        bottom=_runs(grid[rows - 1, col] for col in range(cols)),
        left=tuple((grid[row, 0],) for row in range(1, rows - 1)),
        right=tuple((grid[row, cols - 1],) for row in range(1, rows - 1)),
    )


def _runs(spans: Iterable[Span]) -> Line:
    """Cells merged into a span a run of one style."""
    merged: list[Span] = []
    for span in spans:
        if merged and merged[-1].style == span.style:
            merged[-1] = Span(merged[-1].text + span.text, span.style)
        else:
            merged.append(span)
    return tuple(merged)
