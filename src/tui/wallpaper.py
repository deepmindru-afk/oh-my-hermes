"""The living wallpaper (owner request 2026-10-29): each launch look paints the terminal's background its own colour
and keeps a small, sparse scene moving in the screen's blank cells.

The background is the look's `ground` (`theme.look_ground`), which the app
sets as the terminal's default background with OSC 11 while the wallpaper
shows and puts back on every way out (`App._sync_ground`). This module says
only which cells of the scene are lit at a clock time: `scene(look, cols,
rows, seconds, light)` is every lit cell of a `cols` by `rows` screen as
`Cell(row, col, char, fg)`, a pure function of its arguments (no state, and
the scattering comes from a fixed hash, `_noise`, not from `random`), so the
same moment always gives the same cells. The screen layer
(`screen.LiveRegion`) keeps the cells that fall on a blank cell, paints them
with absolute moves over the screen and takes them off before anything can
scroll, as it does the working glow's frame, so none reaches the scrollback.

Each look has one scene (`SCENES`), drawn with plain characters and blocks,
never emoji: moonlit a crescent moon with twinkling stars and slow clouds,
sakura falling petals, aurora wavy ribbons of light, ember rising sparks, gold
floating motes, violet drifting bokeh, graphite soft rain, spectrum a slow
prism shimmer. It is ambience behind work: sparse (`LIT_CEILING`), dim (a
cell's colour is the look's colour at most `PEAK` of the way from the ground,
and never past `AMBIENT_CONTRAST` against it), and slow, and a cell's colour is
stepped to `LEVELS`, so a cell keeps its bytes until its step changes and the
screen layer repaints only what moved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math

from .theme import BLACK, DEFAULT_LAUNCH, LAUNCH_PALETTES, LogoPalette, _logo_stops, blend, contrast_ratio, is_light

TICK_SECONDS = 0.1  # how often the app paints while the wallpaper moves
PEAK = 0.75  # the brightest a cell gets: the look's colour this far from the ground
LEVELS = 8  # brightness steps: a cell keeps its bytes until its step changes
# No cell stands out from the ground by more than this, under the 4.5:1 the theme gives words: ambience never reads
# as loud as text. A step that would is taken down until it does not (spectrum's pink reached 4.98:1 at `PEAK`).
AMBIENT_CONTRAST = 4.0
# At most this share of a screen's cells is lit at any moment: the scene is ambience behind work, and one cell in
# sixteen keeps it a scattering rather than a picture competing with the text (the test samples every look).
LIT_CEILING = 1 / 16
MIN_COLS, MIN_ROWS = 40, 12  # a screen smaller than this has no scene (its ground still shows)
TAU = 2 * math.pi
CLOUD_CELLS = 1800  # moonlit: a cloud for each this many cells of screen, at most three
AURORA_TWO_ROWS = 20  # aurora: a screen this tall has two ribbons, a shorter one one
AURORA_EVERY_ROWS = 40  # aurora: a screen this tall lights its ribbons in every column, a shorter one in every other


@dataclass(frozen=True)
class Cell:
    """One lit cell of the scene: where on the screen (0-based), its character, and its colour."""

    row: int
    col: int
    char: str
    fg: str


def _noise(index: int, salt: int = 0) -> float:
    """A fixed pseudo-random number in [0, 1) for `index` and `salt` (a 32-bit integer hash), the same every call."""
    value = (index * 0x9E3779B1 + salt * 0x85EBCA77 + 0x165667B1) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return value / 0x100000000


def _count(cols: int, rows: int, cells_each: int) -> int:
    """How many of a scattered thing a screen holds: one per `cells_each` cells, at least one."""
    return max(1, cols * rows // cells_each)


def _wave(seconds: float, period: float, phase: float) -> float:
    """0 to 1 and back over `period` seconds, starting `phase` (0 to 1) of the way through."""
    return 0.5 + 0.5 * math.sin(TAU * (seconds / period + phase))


class _Canvas:
    """The lit cells being drawn, the later of two on one cell winning, each colour the ground mixed toward a colour
    by a stepped strength (`LEVELS`)."""

    def __init__(self, cols: int, rows: int, ground: str) -> None:
        self.cols, self.rows, self.ground = cols, rows, ground
        self.cells: dict[tuple[int, int], Cell] = {}

    def put(self, row: float, col: float, char: str, colour: str, strength: float, under: str = "") -> None:
        """Light the cell at (`row`, `col`) with `char` in `colour` mixed from `under` (the ground unless named: a
        pale colour mixed toward black keeps its hue where mixed into a dark ground it turns grey)."""
        steps = round(max(0.0, min(strength, 1.0)) * LEVELS)
        at = (int(math.floor(row)), int(math.floor(col)))
        if steps <= 0 or not (0 <= at[0] < self.rows and 0 <= at[1] < self.cols):
            return
        under = under or self.ground
        mixed = blend(under, colour, PEAK * steps / LEVELS)
        while steps > 1 and contrast_ratio(mixed, self.ground) > AMBIENT_CONTRAST:
            steps -= 1
            mixed = blend(under, colour, PEAK * steps / LEVELS)
        self.cells[at] = Cell(at[0], at[1], char, mixed)


def _moonlit(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """A crescent moon high on the right, stars twinkling, and up to three clouds drifting slowly right (one per
    `CLOUD_CELLS` cells of screen, so a small screen is not all cloud)."""
    cols, rows = canvas.cols, canvas.rows
    stops = _logo_stops(palette)
    for index in range(_count(cols, rows, 100)):
        shine = 0.35 + 0.65 * _wave(seconds, 3 + 4 * _noise(index, 3), _noise(index, 4))
        char = "*" if shine > 0.85 else "+" if shine > 0.6 else "."
        canvas.put(_noise(index, 2) * rows, _noise(index, 1) * cols, char, stops[1], shine * 0.8)
    radius = max(2.0, min(rows * 0.12, cols * 0.06))  # in rows; a cell is half as wide as it is tall
    centre_row, centre_col = rows * 0.18, cols * 0.82
    moonlight = canvas.ground if is_light(canvas.ground) else BLACK  # a pale gold on a dark ground, not a grey
    for row in range(int(centre_row - radius) - 1, int(centre_row + radius) + 2):
        for col in range(int(centre_col - 2 * radius) - 2, int(centre_col + 2 * radius) + 3):
            down, across = row + 0.5 - centre_row, (col + 0.5 - centre_col) / 2
            if down * down + across * across > radius * radius:
                continue
            bite_down, bite_across = down + 0.25 * radius, across - 0.5 * radius
            if bite_down * bite_down + bite_across * bite_across < (0.85 * radius) ** 2:
                continue
            canvas.put(row, col, "█", stops[0], 1.0, moonlight)
    cloud = ("   ▄▄▄    ", " ▄██████▄▄ ", "▀▀▀▀▀▀▀▀▀▀▀▀")
    for index in range(min(3, cols * rows // CLOUD_CELLS)):
        speed = 0.25 + 0.2 * _noise(index, 11)
        span = cols + len(cloud[2])
        left = (_noise(index, 12) * span + seconds * speed) % span - len(cloud[2])
        top = rows * (0.12 + 0.22 * index) + 2 * _noise(index, 13)
        for down, shape in enumerate(cloud):
            for across, char in enumerate(shape):
                if char != " ":
                    canvas.put(top + down, left + across, char, stops[-1], 0.32)


def _sakura(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Petals falling slowly and drifting right, swaying as they fall, turning as they go."""
    cols, rows = canvas.cols, canvas.rows
    stops = _logo_stops(palette)
    for index in range(_count(cols, rows, 80)):
        fall = 0.5 + 0.6 * _noise(index, 1)
        row = (_noise(index, 2) * (rows + 2) + seconds * fall) % (rows + 2) - 1
        sway = (2 + 2 * _noise(index, 3)) * math.sin(TAU * (seconds / (4 + 3 * _noise(index, 4)) + _noise(index, 5)))
        col = (_noise(index, 6) * cols + seconds * 0.4 + sway) % cols
        char = "*'`,"[int(seconds * (0.6 + _noise(index, 7)) + 4 * _noise(index, 8)) % 4]
        canvas.put(row, col, char, stops[index % 2], 0.6 + 0.4 * _noise(index, 9))


def _aurora(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Two ribbons of light waving across the upper screen, brighter and dimmer along their length; where one is
    brightest a fainter curtain hangs under it. A shorter screen lights them in every other column, and a short one has
    one."""
    cols, rows = canvas.cols, canvas.rows
    stops = _logo_stops(palette)
    for ribbon in range(1 if rows < AURORA_TWO_ROWS else 2):
        base, amplitude = rows * (0.16 + 0.16 * ribbon), max(1.0, rows * 0.05)
        length = cols * (0.55 + 0.2 * ribbon)
        for col in range(0, cols, 1 if rows >= AURORA_EVERY_ROWS else 2):
            glow = _wave(seconds, 21 + 6 * ribbon, col / (cols * 0.45) + 0.3 * ribbon)
            if glow < 0.3:
                continue
            row = base + amplitude * math.sin(TAU * (col / length + seconds / (26 + 8 * ribbon) + ribbon / 3))
            third = int((row - math.floor(row)) * 3)
            colour = stops[min(int(col / cols * len(stops)), len(stops) - 1)]
            canvas.put(row, col, "'-."[third], colour, 0.35 + 0.65 * glow)
            if glow > 0.8:
                canvas.put(row + 1, col, ":", colour, 0.35)


def _ember(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Sparks rising from the bottom, wavering, fading and reddening as they rise."""
    cols, rows = canvas.cols, canvas.rows
    stops = _logo_stops(palette)
    for index in range(_count(cols, rows, 110)):
        height = rows * (0.35 + 0.45 * _noise(index, 1))
        rise = 1.0 + 1.2 * _noise(index, 2)
        risen = (seconds * rise + _noise(index, 3) * height) % height
        share = risen / height
        col = _noise(index, 4) * cols + 1.5 * math.sin(TAU * (seconds / 3 + _noise(index, 5)))
        char = "*" if share < 0.3 else "'" if share < 0.65 else "."
        colour = stops[min(1 + int(share * (len(stops) - 1)), len(stops) - 1)]
        canvas.put(rows - 1 - risen, col, char, colour, 1 - 0.8 * share)


def _gold(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Motes floating on slow loops, catching the light now and then."""
    cols, rows = canvas.cols, canvas.rows
    stops = _logo_stops(palette)
    for index in range(_count(cols, rows, 90)):
        col = _noise(index, 1) * cols + 3 * math.sin(TAU * (seconds / (11 + 6 * _noise(index, 2)) + _noise(index, 3)))
        row = _noise(index, 4) * rows + 1.5 * math.sin(TAU * (seconds / (9 + 8 * _noise(index, 5)) + _noise(index, 6)))
        shine = _wave(seconds, 4 + 5 * _noise(index, 7), _noise(index, 8))
        char = "*" if shine > 0.75 else "+" if shine > 0.45 else "."
        canvas.put(row, col, char, stops[1 + index % 3], 0.4 + 0.6 * shine)


def _violet(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Out-of-focus lights drifting up and to the right, each fading in and out; one in four a large ring."""
    cols, rows = canvas.cols, canvas.rows
    stops = _logo_stops(palette)
    ring = ((0, 1, "."), (0, 2, "-"), (0, 3, "."), (1, 0, "("), (1, 4, ")"), (2, 1, "'"), (2, 2, "-"), (2, 3, "'"))
    for index in range(_count(cols, rows, 260)):
        col = (_noise(index, 1) * cols + seconds * 0.15) % cols
        row = (_noise(index, 2) * rows - seconds * 0.08) % rows
        strength = math.sin(math.pi * ((seconds / (8 + 6 * _noise(index, 3)) + _noise(index, 4)) % 1)) * 0.75
        colour = stops[index % len(stops)]
        if index % 4 == 0:
            for down, across, char in ring:
                canvas.put(row + down, col + across, char, colour, strength * 0.8)
        else:
            canvas.put(row, col, "o" if index % 4 == 1 else "°", colour, strength)


def _graphite(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Soft rain: thin drops falling, each landing somewhere new every time it falls; one in six in the look's
    turquoise."""
    cols, rows = canvas.cols, canvas.rows
    grey = palette.greys[0] if palette.greys else palette.label
    for index in range(_count(cols, rows, 220)):
        fall = 2.0 + 1.0 * _noise(index, 1)
        travel = seconds * fall + _noise(index, 2) * (rows + 4)
        drop, row = divmod(travel, rows + 4)
        col = _noise(index * 131 + int(drop), 3) * cols
        colour = palette.spark if palette.spark and index % 6 == 0 else grey
        canvas.put(row - 2, col, "|", colour, 0.85)
        canvas.put(row - 3, col, "'", colour, 0.45)


def _spectrum(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """A slow prism shimmer: a lattice of points lit as two diagonal bands, half a sweep apart, cross it, each point in
    the hue of the spectrum where it stands."""
    cols, rows = canvas.cols, canvas.rows
    stops = _logo_stops(palette)
    centres = [(seconds / 16 + half / 2) % 1.5 - 0.25 for half in range(2)]  # each from off the left to off the right
    for row in range(1, rows, 3):
        for col in range(3 * (row // 3 % 2), cols, 6):
            along = col / cols * 0.75 + row / rows * 0.25
            strength = max(1 - abs(along - centre) / 0.2 for centre in centres)
            if strength <= 0:
                continue
            char = "*" if strength > 0.75 else "+" if strength > 0.4 else "."
            canvas.put(row, col, char, stops[min(int(along * len(stops)), len(stops) - 1)], strength)


SCENES: dict[str, Callable[[_Canvas, LogoPalette, float], None]] = {
    "spectrum": _spectrum, "violet": _violet, "gold": _gold, "graphite": _graphite,
    "sakura": _sakura, "moonlit": _moonlit, "aurora": _aurora, "ember": _ember,
}


def scene(look: str, cols: int, rows: int, seconds: float, light: bool) -> tuple[Cell, ...]:
    """Every lit cell of launch look `look`'s scene on a `cols` by `rows` screen at clock time `seconds`, in its
    light set on a light terminal; the default look's for an unknown name, none on a screen under `MIN_COLS` by
    `MIN_ROWS`. The same arguments give the same cells."""
    if cols < MIN_COLS or rows < MIN_ROWS:
        return ()
    name = look if look in SCENES else DEFAULT_LAUNCH
    palette = LAUNCH_PALETTES[name][1 if light else 0]
    canvas = _Canvas(cols, rows, palette.ground)
    SCENES[name](canvas, palette, seconds)
    return tuple(canvas.cells[at] for at in sorted(canvas.cells))
