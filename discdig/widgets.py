"""Shared presentation helpers: family glyphs, progress cells, marked tables.

Everything here obeys the "usable at 16 colours, beautiful at true colour" rule --
state is carried by a glyph *and* a colour, never by colour alone, so the app
stays readable on a monochrome terminal or for a colour-blind reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from rich.cells import cell_len
from rich.text import Text
from textual.color import Color, Gradient
from textual.widgets import DataTable

from .store import DONE, FAILED, PAUSED, QUEUED, RUNNING, Task

#: family -> (glyph, theme colour).  Glyphs are single-width across common fonts.
FAMILY_STYLE: dict[str, tuple[str, str]] = {
    "directory":  ("/", "bright_blue"),
    "archive":    ("#", "yellow"),
    "image":      ("*", "magenta"),
    "text":       ("=", "white"),
    "document":   ("=", "cyan"),
    "audio":      ("~", "green"),
    "music":      ("~", "bright_green"),
    "video":      (">", "bright_magenta"),
    "poly":       ("^", "cyan"),
    "font":       ("A", "bright_cyan"),
    "executable": ("!", "bright_red"),
    "other":      (".", "bright_black"),
    "unknown":    ("?", "bright_black"),
}

#: Status glyphs.  The colour comes from the theme (see BarPalette); the glyph
#: is what carries the meaning when colour is unavailable.
STATUS_GLYPH: dict[str, str] = {
    QUEUED: "o", RUNNING: "*", PAUSED: "=", DONE: "+", FAILED: "x",
}

# Textual draws its own bars with these, so ours look native beside them.
BAR = "\u2501"        # ━
HALF_RIGHT = "\u2578"  # ╸  leading half-cell
HALF_LEFT = "\u257a"   # ╺  trailing half-cell


@dataclass(frozen=True)
class BarPalette:
    """Colours for the queue bars, derived from whichever theme is active.

    Hard-coded colour names looked wrong the moment anyone changed theme -- a
    "bright_green" bar sits badly on Dracula.  Pulling these from the theme
    means the bar always belongs to the palette around it, and gets there
    without anyone hand-picking colours per theme.
    """

    ramp: tuple[str, ...]   # gradient across the bar, one colour per cell
    track: str              # the unfilled remainder
    head: str               # leading edge of the filled portion
    done: str
    warn: str
    error: str
    paused: str
    muted: str

    @classmethod
    def from_theme(cls, theme: Any, width: int = 64) -> "BarPalette":
        def colour(name: str, fallback: str) -> Color:
            value = getattr(theme, name, None)
            try:
                return Color.parse(value) if value else Color.parse(fallback)
            except Exception:  # noqa: BLE001 - a bad theme must not break the UI
                return Color.parse(fallback)

        background = colour("background", "#000000")
        foreground = colour("foreground", "#ffffff")
        primary = colour("primary", "#bd93f9")
        secondary = colour("secondary", "#6272a4")
        accent = colour("accent", "#ff79c6")

        # Cool -> warm across the bar: on Dracula that reads blue-grey, purple,
        # pink, which is unmistakably of the theme while still standing out
        # against a near-black ground.
        gradient = Gradient((0.0, secondary), (0.55, primary), (1.0, accent))
        ramp = tuple(gradient.get_color(i / max(1, width - 1)).hex for i in range(width))

        return cls(
            ramp=ramp,
            track=background.blend(foreground, 0.18).hex,
            head=accent.blend(foreground, 0.45).hex,
            done=colour("success", "#50fa7b").hex,
            warn=colour("warning", "#ffb86c").hex,
            error=colour("error", "#ff5555").hex,
            paused=colour("warning", "#ffb86c").blend(background, 0.35).hex,
            muted=background.blend(foreground, 0.55).hex,
        )


#: Used before an app is available (the CLI's queue listing, say).
FALLBACK_PALETTE = BarPalette(
    ramp=("#6272a4",) * 8 + ("#bd93f9",) * 8 + ("#ff79c6",) * 8,
    track="#44475a", head="#ffb3e0", done="#50fa7b", warn="#ffb86c",
    error="#ff5555", paused="#a2794b", muted="#8b8fa3",
)


def family_cell(name: str, family: str, marked: bool = False, width: int = 0) -> Text:
    """``> # Some Name`` — mark, family glyph, name, sized to exactly ``width``.

    The name column is sized by us rather than by DataTable's auto-sizing: left
    to itself the table would either stretch to the longest title on the page
    (pushing the other columns off-screen) or leave a band of dead space when
    every name is short.  Padding to a computed width makes the table fill its
    box at any terminal size.
    """
    glyph, colour = FAMILY_STYLE.get(family, FAMILY_STYLE["unknown"])
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("> " if marked else "  ", style="bold bright_yellow" if marked else "")
    text.append(f"{glyph} ", style=colour)
    text.append(name, style="bold" if family == "directory" else "")
    if width > 6:
        room = width - 4  # the "> # " prefix
        if cell_len(name) > room:
            text = Text(no_wrap=True)
            text.append("> " if marked else "  ",
                        style="bold bright_yellow" if marked else "")
            text.append(f"{glyph} ", style=colour)
            text.append(_elide(name, room), style="bold" if family == "directory" else "")
        text.pad_right(max(0, width - cell_len(text.plain)))
    return text


def _elide(name: str, room: int) -> str:
    """Trim from the middle so the extension stays visible."""
    if room < 6:
        return name[:room]
    while cell_len(name) > room:
        head = (room - 1) // 2
        tail = room - 1 - head
        name = name[:head] + "…" + name[len(name) - tail:]
        break
    return name


def bar(fraction: float, width: int, palette: BarPalette,
        colour: str | None = None) -> Text:
    """A gradient bar, accurate to half a cell.

    ``colour`` overrides the gradient with one flat colour, for the states
    where a ramp would be noise rather than information (done, paused).
    """
    fraction = max(0.0, min(1.0, fraction))
    filled = fraction * width
    whole = int(filled)
    half = (filled - whole) >= 0.5

    out = Text(no_wrap=True)
    ramp = palette.ramp
    for i in range(whole):
        shade = colour or ramp[min(len(ramp) - 1, i * len(ramp) // max(1, width))]
        # Brighten the leading cell so the bar has a visible head.
        if colour is None and i == whole - 1 and whole < width:
            shade = palette.head
        out.append(BAR, style=shade)
    if whole < width:
        if half:
            out.append(HALF_RIGHT, style=colour or palette.head)
        out.append(BAR * (width - whole - (1 if half else 0)), style=palette.track)
    return out


def progress_cell(task: Task, width: int = 14,
                  palette: BarPalette | None = None, phase: int = 0) -> Text:
    """One queue row's progress column.

    Colour follows the active theme; the status glyph in the neighbouring
    column still carries the meaning on its own, so nothing here is lost on a
    monochrome terminal.
    """
    palette = palette or FALLBACK_PALETTE
    total = task.total or task.expected_size or 0

    if task.status == DONE:
        colour = palette.warn if task.warn else palette.done
        return Text(BAR * width, style=colour)
    if task.status == FAILED:
        out = Text(BAR * width, style=palette.error)
        return out
    if task.status == PAUSED:
        fraction = (task.downloaded / total) if total else 0.0
        out = bar(fraction, width, palette, colour=palette.paused)
        out.append(f" {fraction * 100:3.0f}%", style=palette.muted)
        return out

    if not total and task.status == RUNNING:
        # No Content-Length yet -- a search bundle the server is still building.
        # A marching highlight says "working" where a 0% bar would say "stuck".
        return _indeterminate(width, palette, phase)

    fraction = (task.downloaded / total) if total else 0.0
    out = bar(fraction, width, palette)
    if total:
        out.append(f" {fraction * 100:3.0f}%",
                   style=palette.muted if task.status != RUNNING else palette.head)
    return out


def _indeterminate(width: int, palette: BarPalette, phase: int) -> Text:
    """A lit band sliding along the track, for transfers of unknown length."""
    span = max(3, width // 4)
    travel = width + span
    start = (phase % travel) - span
    out = Text(no_wrap=True)
    for i in range(width):
        offset = i - start
        if 0 <= offset < span:
            # Brightest in the middle of the band, fading at both ends.
            edge = min(offset, span - 1 - offset) / max(1, (span - 1) / 2)
            shade = palette.ramp[min(len(palette.ramp) - 1,
                                     i * len(palette.ramp) // max(1, width))]
            out.append(BAR, style=palette.head if edge > 0.6 else shade)
        else:
            out.append(BAR, style=palette.track)
    return out


def status_cell(task: Task, palette: BarPalette | None = None) -> Text:
    palette = palette or FALLBACK_PALETTE
    glyph = STATUS_GLYPH.get(task.status, "?")
    colour = {
        QUEUED: palette.muted,
        RUNNING: palette.head,
        PAUSED: palette.warn,
        DONE: palette.done,
        FAILED: palette.error,
    }.get(task.status, palette.muted)
    if task.status == DONE and task.warn:
        glyph, colour = "!", palette.warn
    return Text(f" {glyph} ", style=f"bold {colour}")


def rate(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return ""
    for unit in ("B", "K", "M", "G"):
        if bytes_per_sec < 1024 or unit == "G":
            return f"{bytes_per_sec:.0f}{unit}/s" if unit == "B" else f"{bytes_per_sec:.1f}{unit}/s"
        bytes_per_sec /= 1024
    return ""


def duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0 or seconds > 86400 * 7:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class MarkedTable(DataTable):
    """A DataTable with a persistent multi-selection.

    Textual's table has a cursor but no selection model, and a download queue is
    unusable without one -- you want to tick eight files and press ``d`` once.
    Marks are keyed by the caller's own row identity so they survive a re-render.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.marked: set[str] = set()

    def clear_marks(self) -> None:
        self.marked.clear()

    def toggle_mark(self, key: str) -> bool:
        if key in self.marked:
            self.marked.discard(key)
            return False
        self.marked.add(key)
        return True

    def mark_all(self, keys: Iterable[str]) -> None:
        self.marked.update(keys)

    @property
    def has_marks(self) -> bool:
        return bool(self.marked)
