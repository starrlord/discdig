"""Shared presentation helpers: family glyphs, progress cells, marked tables.

Everything here obeys the "usable at 16 colours, beautiful at true colour" rule --
state is carried by a glyph *and* a colour, never by colour alone, so the app
stays readable on a monochrome terminal or for a colour-blind reader.
"""

from __future__ import annotations

from typing import Any, Iterable

from rich.cells import cell_len
from rich.text import Text
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

STATUS_STYLE: dict[str, tuple[str, str]] = {
    QUEUED:  ("o", "bright_black"),
    RUNNING: ("*", "bright_green"),
    PAUSED:  ("=", "yellow"),
    DONE:    ("+", "green"),
    FAILED:  ("x", "bright_red"),
}

_BLOCKS = " ▏▎▍▌▋▊▉█"


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


def bar(fraction: float, width: int = 14, style: str = "green") -> Text:
    """Sub-cell-accurate progress bar built from eighth-block characters."""
    fraction = max(0.0, min(1.0, fraction))
    filled = fraction * width
    whole = int(filled)
    remainder = filled - whole
    out = "█" * whole
    if whole < width:
        out += _BLOCKS[int(remainder * 8)]
        out = out.ljust(width, "·")
    return Text(out[:width], style=style)


def progress_cell(task: Task, width: int = 14) -> Text:
    total = task.total or task.expected_size or 0
    if task.status == DONE:
        colour = "yellow" if task.warn else "green"
        return Text("█" * width, style=colour)
    if task.status == FAILED:
        return Text("·" * width, style="red")
    fraction = (task.downloaded / total) if total else 0.0
    colour = {RUNNING: "bright_green", PAUSED: "yellow"}.get(task.status, "bright_black")
    out = bar(fraction, width, colour)
    if total:
        pct = f"{fraction * 100:4.0f}%"
        out.append(" " + pct, style="bright_black")
    return out


def status_cell(task: Task) -> Text:
    glyph, colour = STATUS_STYLE.get(task.status, ("?", "white"))
    if task.status == DONE and task.warn:
        glyph, colour = "!", "yellow"
    text = Text(f" {glyph} ", style=f"bold {colour}")
    return text


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
