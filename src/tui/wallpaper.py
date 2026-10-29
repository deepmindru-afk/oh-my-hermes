"""The living wallpaper (owner request 2026-10-29): each launch look paints the terminal's background its own colour
and keeps a scene moving in the screen's blank cells.

The background is the look's `ground` (`theme.look_ground`), which the app
sets as the terminal's default background with OSC 11 while the wallpaper
shows and puts back on every way out (`App._sync_ground`). This module says
only which cells of the scene are lit at a clock time: `scene(look, cols,
rows, seconds, light)` is every lit cell of a `cols` by `rows` screen as
`Cell(row, col, char, fg, bg)`, a pure function of its arguments (no state,
and the scattering comes from a fixed hash, `_noise`, not from `random`), so
the same moment always gives the same cells. The screen layer
(`screen.LiveRegion`) keeps the cells that fall on a blank cell, paints them
with absolute moves over the screen and takes them off before anything can
scroll, as it does the working glow's frame, so none reaches the scrollback.

The scenes are pixel art (owner's choice of 2026-10-29, after characters
such as `%t;:` and rings of `O` read as broken text): a cell is two pixels,
one over the other, drawn with a half block, `▀` in the top pixel's colour
over the bottom pixel's as background, `▄` for a bottom pixel alone, and for
two of one colour a space on that colour, which fills the whole cell where a
block glyph would leave the terminal's line spacing showing as a stripe; a
pixel left out shows the ground. Nothing else is drawn. The motifs are small bitmaps kept here as data (`Sprite`: rows of
letters, each letter a colour role a scene maps to one of the look's
colours, `.` left out), some with frames that make them move: moonlit a
crescent moon with a pulsing halo, drifting clouds and twinkling stars;
sakura a cherry branch from the top-right corner with five-petal blossoms
and falling petals; aurora curtains of light, gradient columns that wave;
ember a campfire whose flame flickers through three frames, with rising
sparks; gold a sun whose rays turn, and motes; violet jellyfish drifting up,
their bells pulsing and tentacles trailing; graphite rain over a puddle
whose ripples spread; spectrum a prism splitting a beam into the five hues.
The motifs sit where a working session leaves the screen blank: its right
side (`RIGHT`), where transcript rows end short of the edge, and for
sakura's branch the top-right corner.

It is ambience behind work: never more than `LIT_CEILING` of the screen, no
pixel past `AMBIENT_CONTRAST` against the ground, and a pixel's colour is
stepped to `LEVELS`, so a cell keeps its bytes until a pixel in it changes
and the screen layer repaints only what moved. Everything that moves moves
within a second.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math

from .theme import BLACK, DEFAULT_LAUNCH, LAUNCH_PALETTES, LogoPalette, _logo_stops, blend, contrast_ratio, is_light

# How often the app paints the scene's changed cells between frames (a turn's frames, every spinner tick, bring it
# along too): about four times a second keeps the motion visible within a second while an idle screen writes little.
TICK_SECONDS = 0.25
PEAK = 0.9  # the furthest a pixel's colour goes from the ground toward the look's, where the cap allows it
LEVELS = 8  # brightness steps: a cell keeps its bytes until its step changes
# No pixel stands out from the ground by more than this, well under the 4.5:1 the theme gives words: ambience never
# reads as loud as text. A step that would is taken down until it does not.
AMBIENT_CONTRAST = 3.0
# At most this share of a screen's cells is lit at any moment: motifs big enough to read at a glance, and five cells
# in six left to the ground, so the scene stays behind the work rather than a picture over it (the test samples every
# look on six screen sizes).
LIT_CEILING = 1 / 6
MIN_COLS, MIN_ROWS = 80, 24  # a screen smaller than this has no scene (its ground still shows): the motifs need room
TAU = 2 * math.pi
# The only glyphs drawn: a cell's top pixel, its bottom pixel, and for two of one colour a space on that background,
# which fills the cell to its edges where a block glyph leaves the terminal's line spacing showing as a stripe.
UPPER, LOWER, FULL = "▀", "▄", " "
# The rows the motifs keep to, as shares of the screen's: the lower half, over the composer, which is blank under the
# launch screen's card and, in a working session, at the right ends of the transcript's rows; and the right side
# (`RIGHT`), where a working session's rows end short of the edge and their tails are blank.
BAND = (0.5, 0.86)
RIGHT = 0.45  # the share of the width the right side starts at
CLEAR = "."  # a bitmap's pixel left out: the ground shows
TWO_CLOUD_CELLS = 4000  # moonlit: a screen of this many cells has room for a second cloud beside the moon
BIG_COLS, BIG_ROWS, BIG_SCALE = 140, 40, 2  # a screen this big draws the motifs' art pixels two pixels square

Sprite = tuple[str, ...]  # a bitmap, top row first, one letter a pixel: a colour role, or `CLEAR`

MOON: Sprite = (  # a crescent moon in a halo; a rim, b body, c shade, h halo
    "............hh................",
    ".........hhh..................",
    "........hh....aa..............",
    "......hh..aaaaaa..............",
    ".....hh..aabbba...............",
    ".....h..abbbba................",
    "....h..abbbbba................",
    "...hh.abbbbbba................",
    "...h.aabbbbbba................",
    "...h.abbbbbbba................",
    "..h..abbbbbbba................",
    "..h..abbbbbbba................",
    "..h.abbbbbbbbba...............",
    "..h.abbbbbbbbba...............",
    "..h..abbbbbbbbba..............",
    "..h..abbbbbbbbbba.............",
    "...h.abbbbbbbbbccaa...........",
    "...h.aabbbbbbbcccccaaaaaa.....",
    "...hh.abbbbbbcccccccccca......",
    "....h..abbbbcccccccccca.......",
    ".....h..abbcccccccccca..h.....",
    ".....hh..aaccccccccaa..hh.....",
    "......hh..aaaaccaaaa..hh......",
    "........hh....aa....hh........",
    ".........hhh......hhh.........",
    "............hhhhhh............",
)
CLOUD: Sprite = (  # a lit top edge, b body, c shaded underside
    "..........aaaa..........",
    ".......aaabbbbaa........",
    ".....aabbbbbbbbba.aaa...",
    "....abbbbbbbbbbbbabbbaa.",
    "..aabbbbbbbbbbbbbbbbbbba",
    ".abbbbbbbbbbbbbbbbbbbbba",
    "abbbbbbbbbbbbbbbbbbbbbbc",
    ".cccccccccccccccccccccc.",
)
STAR: tuple[Sprite, Sprite] = (("s",), (".s.", "sws", ".s."))  # a dim point, then a bright twinkle
BLOSSOM: Sprite = (  # five petals round a centre: p petal, q petal's shade, c centre
    ".p.p.",
    "ppqpp",
    ".qcq.",
    "ppqpp",
    "..p..",
)
PETAL: tuple[Sprite, Sprite] = (("pq",), ("p", "q"))  # a falling petal, turning
LOGS: Sprite = (  # two crossed logs: l log, k cut end
    "kl............lk",
    ".kll........llk.",
    "...llll..llll...",
    "......llll......",
    "...llll..llll...",
    ".kll........llk.",
)
FLAME: tuple[Sprite, Sprite, Sprite] = (  # the fire's three frames: r outer red, o orange, y yellow core
    (
        ".....r......",
        "....rr......",
        "....ror.....",
        "...rroor..r.",
        "...roooor.r.",
        "..rrooyorrr.",
        "..rooyyoor..",
        ".rrooyyyor..",
        ".rooyyyyoor.",
        ".rooyyyyyor.",
        "..rooyyyor..",
        "...rrooorr..",
    ),
    (
        "......r.....",
        "......rr....",
        ".....ror....",
        ".r..roorr...",
        ".r.roooor...",
        ".rrroyoorr..",
        "..rooyyoor..",
        "..royyyoorr.",
        ".rooyyyyoor.",
        ".roooyyyyor.",
        "..rooyyyor..",
        "..rrooorr...",
    ),
    (
        "............",
        ".....r..r...",
        "....rr.ror..",
        "....rorroor.",
        "...rooooor..",
        "...rooyyoor.",
        "..rroyyyoor.",
        "..rooyyyyor.",
        ".rooyyyyyor.",
        ".rooyyyyoor.",
        "..rooyyyor..",
        "...rroooor..",
    ),
)
SUN: Sprite = (  # a sun's disc: o rim, y body, w glint
    "...ooooo...",
    "..oyyyyyo..",
    ".oywwyyyyo.",
    "oywwyyyyyyo",
    "oywyyyyyyyo",
    "oyyyyyyyyyo",
    "oyyyyyyyyyo",
    "oyyyyyyyyyo",
    ".oyyyyyyyo.",
    "..oyyyyyo..",
    "...ooooo...",
)
JELLYFISH: tuple[Sprite, Sprite] = (  # its bell open, then pulsed shut: e edge, b bell, w glint, t tentacle
    (
        "....eeeee....",
        "..eebbbbbee..",
        ".ebwwbbbbbbe.",
        "ebwwbbbbbbbbe",
        "ebbbbbbbbbbbe",
        "eeeeeeeeeeeee",
        ".t..t..t..t..",
        ".t..t..t..t..",
        "..t..t..t..t.",
        "..t..t..t..t.",
        ".t..t..t..t..",
        ".t...t..t..t.",
        "..t..t...t...",
        "..t...t..t...",
        "......t......",
    ),
    (
        ".....eee.....",
        "...eebbbee...",
        "..ebwwbbbbe..",
        "..ewwbbbbbe..",
        "..ebbbbbbbe..",
        "..ebbbbbbbe..",
        "...eeeeeee...",
        "...t.t.t.t...",
        "..t..t..t.t..",
        "..t.t..t..t..",
        "...t..t..t...",
        "...t.t..t.t..",
        "..t..t...t...",
        "..t...t..t...",
        "......t......",
    ),
)
PRISM: Sprite = (  # a glass prism: e edge, f faint glass
    ".......e.......",
    "......efe......",
    ".....effffe....",
    "....effffffe...",
    "...effffffffe..",
    "..effffffffffe.",
    ".effffffffffffe",
    "eeeeeeeeeeeeeee",
)
SPRITES: dict[str, Sprite | tuple[Sprite, ...]] = {
    "MOON": MOON, "CLOUD": CLOUD, "STAR": STAR, "BLOSSOM": BLOSSOM, "PETAL": PETAL, "LOGS": LOGS, "FLAME": FLAME,
    "SUN": SUN, "JELLYFISH": JELLYFISH, "PRISM": PRISM,
}


@dataclass(frozen=True)
class Cell:
    """One lit cell of the scene: where on the screen (0-based), its half-block glyph, its colour, and the colour
    under the glyph's other half (None: the ground)."""

    row: int
    col: int
    char: str
    fg: str
    bg: str | None = None


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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _frame(frames: tuple[Sprite, ...], seconds: float, period: float, phase: float = 0.0) -> Sprite:
    """The frame of `frames` showing at `seconds`, each held `period` seconds in turn."""
    return frames[int(seconds / period + phase * len(frames)) % len(frames)]


class _Canvas:
    """The pixels being drawn, two to a cell's height (`height` pixel rows), the later of two on one pixel winning;
    each colour the ground mixed toward a colour by a stepped strength (`tone`)."""

    def __init__(self, cols: int, rows: int, ground: str, seconds: float) -> None:
        self.cols, self.rows, self.ground, self.seconds = cols, rows, ground, seconds
        self.height = 2 * rows
        self.pixels: dict[tuple[int, int], str] = {}
        self.reach: dict[tuple[str, str], float] = {}  # (colour, under) -> the furthest mix within the cap

    def tone(self, colour: str, strength: float, under: str = "") -> str | None:
        """`colour` mixed from `under` (the ground unless named: a pale colour mixed toward black keeps its hue where
        mixed into a dark ground it turns grey) by `strength` (0 to 1, stepped to `LEVELS`) of the furthest mix that
        stays within `AMBIENT_CONTRAST` of the ground, so a motif's shades stay apart under the cap; None where the
        strength rounds to nothing."""
        steps = round(_clamp(strength, 0.0, 1.0) * LEVELS)
        if steps <= 0:
            return None
        under = under or self.ground
        reach = self.reach.get((colour, under))
        if reach is None:
            reach = PEAK
            while reach > 0 and contrast_ratio(blend(under, colour, reach), self.ground) > AMBIENT_CONTRAST:
                reach -= 1 / 64
            self.reach[colour, under] = reach = max(reach, 0.0)
        return blend(under, colour, reach * steps / LEVELS)

    def dot(self, y: float, x: float, colour: str | None) -> None:
        """Light pixel (`y`, `x`) (pixel rows, columns) in `colour`, already toned; nothing off screen or for None."""
        at = (int(math.floor(y)), int(math.floor(x)))
        if colour is not None and 0 <= at[0] < self.height and 0 <= at[1] < self.cols:
            self.pixels[at] = colour

    def sprite(self, top: float, left: float, art: Sprite, colours: Mapping[str, str | None], scale: int = 1) -> None:
        """Draw `art` with its top-left pixel at (`top`, `left`), each letter in its colour, each art pixel `scale`
        pixels square; `CLEAR` left out."""
        for down, line in enumerate(art):
            for across, letter in enumerate(line):
                if letter == CLEAR:
                    continue
                for y in range(scale):
                    for x in range(scale):
                        self.dot(top + down * scale + y, left + across * scale + x, colours.get(letter))

    def scale(self) -> int:
        """How many pixels square a motif's art pixel is drawn: two on a big screen, so a motif reads at a glance."""
        return BIG_SCALE if self.cols >= BIG_COLS and self.rows >= BIG_ROWS else 1

    def band(self) -> tuple[int, int]:
        """The pixel rows the motifs keep to (`BAND`), first and last."""
        return int(self.height * BAND[0]), int(self.height * BAND[1])

    def cells(self) -> tuple[Cell, ...]:
        """The pixels as cells: `▀` in the top pixel's colour over the bottom one's as its background, `▄` for a bottom
        pixel alone, and a space on the colour where both are one (`FULL`)."""
        cells: dict[tuple[int, int], Cell] = {}
        for (y, x) in self.pixels:
            row = y // 2
            if (row, x) in cells:
                continue
            top, bottom = self.pixels.get((2 * row, x)), self.pixels.get((2 * row + 1, x))
            if top is not None and top == bottom:
                cells[row, x] = Cell(row, x, FULL, top, top)
            elif top is not None:
                cells[row, x] = Cell(row, x, UPPER, top, bottom)
            else:
                cells[row, x] = Cell(row, x, LOWER, bottom or "")
        return tuple(cells[at] for at in sorted(cells))


def _moonlit(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """A crescent moon high on the right, its halo pulsing; two clouds drifting right across the screen, one across the
    moon's foot; stars twinkling."""
    cols, height = canvas.cols, canvas.height
    stops = _logo_stops(palette)
    for index in range(_count(cols, canvas.rows, 140)):
        shine = _wave(seconds, 1.5 + 2 * _noise(index, 3), _noise(index, 4))
        star = STAR[1] if shine > 0.85 else STAR[0]
        colours = {"s": canvas.tone(stops[1], 0.35 + 0.5 * shine), "w": canvas.tone(stops[0], 1.0)}
        canvas.sprite(_noise(index, 2) * height, _noise(index, 1) * cols, star, colours)
    top, bottom = canvas.band()
    moonlight = canvas.ground if is_light(canvas.ground) else BLACK  # a pale gold on a dark ground, not a grey
    moon_top, moon_left = top + 2, cols - len(MOON[0]) - 6
    pulse = _wave(seconds, 2.4, 0.0)
    canvas.sprite(moon_top, moon_left, MOON, {
        "a": canvas.tone(stops[0], 1.0, moonlight), "b": canvas.tone(stops[0], 0.85, moonlight),
        "c": canvas.tone(stops[0], 0.6, moonlight), "h": canvas.tone(stops[0], 0.2 + 0.35 * pulse, moonlight),
    })
    colours = {"a": canvas.tone(stops[1], 0.7), "b": canvas.tone(stops[2], 0.5), "c": canvas.tone(stops[-1], 0.4)}
    scale = canvas.scale()
    width = len(CLOUD[0]) * scale
    for index in range(1 if cols * canvas.rows < TWO_CLOUD_CELLS else 2):
        span = cols + width
        left = (_noise(index, 13) * span + seconds * (1.2 + 0.9 * index) * scale) % span - width
        at = top + (bottom - top - len(CLOUD) * scale) * 0.55
        if index == 1:
            at = moon_top + len(MOON) - 6  # across the moon's foot
        canvas.sprite(at, left, CLOUD, colours, scale)


def _sakura(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """A cherry branch reaching in from the right edge, two pixels thick, swaying a little, with five-petal
    blossoms along it, and petals falling from it, swaying and turning as they fall."""
    cols, height = canvas.cols, canvas.height
    stops = _logo_stops(palette)
    root, _bottom = canvas.band()  # the branch comes in from the right edge at the band's top and droops into it
    reach, drop = min(cols * 0.36, 60.0), _clamp(height * 0.2, 8, 20)
    sway = math.sin(TAU * seconds / 3.0)
    bark, knot = canvas.tone(palette.edge, 1.0), canvas.tone(palette.edge, 0.7)
    scale = canvas.scale()
    for step in range(int(reach)):
        share = step / reach
        y = root + drop * share ** 1.4 + 1.2 * sway * share
        for thick in range(scale + (1 if share < 0.6 else 0)):
            canvas.dot(y + thick, cols - 1 - step, bark)
        if step % 9 == 4:  # a twig
            for down in range(2 * scale):
                canvas.dot(y + scale + 1 + down, cols - 2 - step - down // 2, knot)
    petals = {"p": canvas.tone(stops[0], 1.0), "q": canvas.tone(stops[1], 0.8), "c": canvas.tone(stops[-1], 1.0)}
    span = len(BLOSSOM) * scale
    for index in range(7):
        share = 0.08 + 0.13 * index
        y = root + drop * share ** 1.4 + 1.2 * sway * share
        canvas.sprite(y - span / 2 + scale * 2 * (index % 2), cols - 1 - share * reach - span / 2, BLOSSOM, petals,
                      scale)
    for index in range(_count(cols, canvas.rows, 120)):
        fall = 3.5 + 3.0 * _noise(index, 1)
        y = (_noise(index, 2) * (height + 4) + seconds * fall) % (height + 4) - 2
        drift = (2 + 2 * _noise(index, 3)) * math.sin(TAU * (seconds / (2 + 2 * _noise(index, 4)) + _noise(index, 5)))
        x = (cols - _noise(index, 6) * reach * 1.4 - y * 0.4 + drift) % cols
        colours = {"p": canvas.tone(stops[index % 2], 0.9), "q": canvas.tone(stops[index % 2], 0.6)}
        canvas.sprite(y, x, _frame(PETAL, seconds, 0.6, _noise(index, 7)), colours)


def _aurora(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Curtains of light over the screen's right side: ribbons of pixel columns, three lit and two dark in turn, each
    column a gradient bright at its top and fading down, the tops waving and the lengths breathing, in the look's
    green, teal and violet across the screen."""
    cols = canvas.cols
    stops = _logo_stops(palette)
    top, bottom = canvas.band()
    reach = (bottom - top) * 0.7
    for col in range(int(cols * RIGHT), cols):
        if col % 5 >= 3:
            continue  # the dark gap between two ribbons
        glow = _wave(seconds, 9, col / (cols * 0.6))
        if glow < 0.2:
            continue
        crest = top + reach * (0.15 + 0.15 * math.sin(TAU * (col / (cols * 0.4) + seconds / 4)))
        length = reach * (0.35 + 0.35 * _wave(seconds, 6, col / (cols * 0.3)))
        colour = stops[min(int(((col / cols + seconds / 40) % 1) * len(stops)), len(stops) - 1)]
        for down in range(int(length)):
            fade = 1 - down / length
            canvas.dot(crest + down, col, canvas.tone(colour, (0.35 + 0.65 * glow) * (0.25 + 0.75 * fade)))


def _ember(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """A campfire low on the right: crossed logs, a flame flickering through three frames over them, and sparks
    rising from it, fading and reddening as they climb."""
    cols = canvas.cols
    stops = _logo_stops(palette)
    _top, bottom = canvas.band()
    scale = canvas.scale()
    logs_wide, logs_tall = len(LOGS[0]) * scale, len(LOGS) * scale
    left = int(cols * 0.8) - logs_wide // 2
    canvas.sprite(bottom - logs_tall, left, LOGS, {"l": canvas.tone(palette.edge, 1.0),
                                                   "k": canvas.tone(stops[0], 0.6)}, scale)
    flame = _frame(FLAME, seconds, 0.15)
    colours = {"r": canvas.tone(stops[-1], 0.9), "o": canvas.tone(stops[2], 1.0), "y": canvas.tone(stops[0], 1.0)}
    canvas.sprite(bottom - logs_tall - (len(flame) - 2) * scale, left + (logs_wide - len(flame[0]) * scale) // 2,
                  flame, colours, scale)
    for index in range(_count(cols, canvas.rows, 110)):
        height = canvas.height * (0.3 + 0.4 * _noise(index, 1))
        risen = (seconds * (6.0 + 6.0 * _noise(index, 2)) + _noise(index, 3) * height) % height
        share = risen / height
        x = left + logs_wide / 2 + (_noise(index, 4) - 0.5) * 7 * scale \
            + 3 * math.sin(TAU * (seconds / 2 + _noise(index, 5))) * share
        colour = stops[min(1 + int(share * (len(stops) - 1)), len(stops) - 1)]
        canvas.dot(bottom - logs_tall - 6 * scale - risen, x, canvas.tone(colour, 1 - 0.8 * share))


def _gold(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """A sun high on the right, its rays turning round it in three steps; motes floating across the screen."""
    cols = canvas.cols
    stops = _logo_stops(palette)
    top, bottom = canvas.band()
    for index in range(_count(cols, canvas.rows, 80)):
        x = _noise(index, 1) * cols + 3 * math.sin(TAU * (seconds / (5 + 4 * _noise(index, 2)) + _noise(index, 3)))
        bob = 3 * math.sin(TAU * (seconds / (4 + 4 * _noise(index, 5)) + _noise(index, 6)))
        y = top + _noise(index, 4) * (bottom - top) + bob
        shine = _wave(seconds, 1.5 + 2 * _noise(index, 7), _noise(index, 8))
        canvas.dot(y, x, canvas.tone(stops[1 + index % 3], 0.3 + 0.7 * shine))
    scale = canvas.scale()
    size = len(SUN) * scale
    sun_top, sun_left = top + 6, cols - size - 16
    centre_y, centre_x = sun_top + size / 2, sun_left + size / 2
    turn = int(seconds / 0.4) % 3 * (math.pi / 12)  # three steps of 15 degrees, then round again
    ray = canvas.tone(stops[2], 0.8)
    for index in range(8):
        angle = turn + index * TAU / 8
        for step in range(4 * scale):
            reach = size / 2 + 2 * scale + step
            canvas.dot(centre_y + reach * math.sin(angle), centre_x + reach * math.cos(angle), ray)
    canvas.sprite(sun_top, sun_left, SUN, {"o": canvas.tone(stops[2], 1.0), "y": canvas.tone(stops[1], 1.0),
                                           "w": canvas.tone(stops[0], 1.0)}, scale)


def _violet(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Three jellyfish drifting slowly across the screen's right side in the lower half, each bell pulsing between
    two frames, its tentacles trailing."""
    cols = canvas.cols
    stops = _logo_stops(palette)
    top, bottom = canvas.band()
    scale = canvas.scale()
    width, tall = len(JELLYFISH[0][0]) * scale, len(JELLYFISH[0]) * scale
    for index in range(3):
        span = cols * (1 - RIGHT) + width
        x = cols * RIGHT + ((index / 3 + 0.1 * _noise(index, 1)) * span + seconds * 0.5) % span - width  # spaced
        y = top + max(bottom - top - tall, 0) * (0.15 + 0.35 * index)
        colour = stops[(2 * index + 1) % len(stops)]
        colours = {"e": canvas.tone(colour, 1.0), "b": canvas.tone(colour, 0.65), "w": canvas.tone(stops[0], 1.0),
                   "t": canvas.tone(colour, 0.5)}
        canvas.sprite(y, x, _frame(JELLYFISH, seconds, 1.2, _noise(index, 4)), colours, scale)


def _graphite(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """Rain falling into a puddle low on the screen; where a drop lands a ripple spreads, an ellipse of pixels
    growing on the puddle's line. One drop in six is the look's turquoise."""
    cols = canvas.cols
    grey = palette.greys[0] if palette.greys else palette.label
    _top, puddle = canvas.band()
    surface = canvas.tone(palette.whites[3] if palette.whites else palette.label, 0.5)
    for col in range(int(cols * RIGHT * 0.5), cols):
        if _noise(col, 50) < 0.6:
            canvas.dot(puddle, col, surface)
    for index in range(_count(cols, canvas.rows, 160)):
        fall = 18.0 + 8.0 * _noise(index, 1)
        drop, travel = divmod(seconds * fall + _noise(index, 2) * (puddle + 12), puddle + 12)
        col = int(_noise(index * 131 + int(drop), 3) * cols)
        colour = palette.spark if palette.spark and index % 6 == 0 else grey
        if travel < puddle:
            for tail in range(4):
                canvas.dot(travel - tail, col, canvas.tone(colour, 0.85 - 0.18 * tail))
        else:
            ring = (travel - puddle) / 12  # landed: the ripple grows for the rest of the fall's cycle
            wide, tall = 2 + 8 * ring, 1 + 2 * ring
            tone = canvas.tone(colour, 0.9 - 0.6 * ring)
            for step in range(16):
                angle = step * TAU / 16
                canvas.dot(puddle + tall * math.sin(angle), col + wide * math.cos(angle), tone)


def _spectrum(canvas: _Canvas, palette: LogoPalette, seconds: float) -> None:
    """A prism on the screen's right side, a beam of light going into it from the left and five rays, one per hue,
    fanning out of it to the right; pulses of light run along the beam and the rays."""
    cols = canvas.cols
    stops = _logo_stops(palette)
    scale = canvas.scale()
    top, bottom = canvas.band()
    prism_top, prism_left = top + (bottom - top) * 0.15, cols * 0.62
    canvas.sprite(prism_top, prism_left, PRISM, {"e": canvas.tone(palette.label, 1.0),
                                                 "f": canvas.tone(palette.label, 0.3)}, scale)
    middle = prism_top + len(PRISM) * scale * 0.6
    enter, leave = int(prism_left + 3 * scale), prism_left + (len(PRISM[0]) - 4) * scale
    pulse = seconds * 10
    for col in range(int(cols * RIGHT * 0.8), enter):
        canvas.dot(middle, col, canvas.tone(palette.label, 1.0 if (col - pulse) % 15 < 2 else 0.4))
    for hue in range(len(stops)):
        slope = (hue - (len(stops) - 1) / 2) * 0.12  # pixel rows per column: the fan opens a few rows each way
        for step in range(int(cols - leave)):
            bright = 1.0 if (step - pulse) % 17 < 2 else 0.45
            canvas.dot(middle + slope * step, leave + step, canvas.tone(stops[hue], bright))


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
    canvas = _Canvas(cols, rows, palette.ground, seconds)
    SCENES[name](canvas, palette, seconds)
    return canvas.cells()
