"""discdig — a terminal browser and download manager for discmaster.textfiles.com.

Layout follows the "header + scrollable list" pattern with a drill-down stack:
a fixed top bar (identity, where you are, what the queue is doing), a tab strip,
one full-height table per pane, and an optional detail column on the right.
Panels never move, so the mental map holds; every action has a key, the footer
shows the important ones, and ``?`` shows the rest.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

import httpx
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.suggester import SuggestFromList
from textual.color import Gradient
from textual.theme import Theme
from textual.widgets import (
    Checkbox, Footer, Input, Label, ProgressBar, Select, Static,
)

from .api import (
    BASE, SECTIONS, DiscMaster, Entry, ItemSummary, Listing, human_size,
)
from .downloader import Downloader, destination, safe_component
from .screens import ConfirmScreen, HelpScreen, InspectScreen, SettingsScreen
from .store import (
    DEFAULT_THEME, DONE, FAILED, PAUSED, QUEUED, RUNNING, Config, Store, Task,
)
from .widgets import (
    BarPalette, MarkedTable, duration, family_cell, progress_cell, rate, status_cell,
)

DISCDIG_THEME = Theme(
    name="discdig",
    primary="#ffb454",      # amber phosphor
    secondary="#5ccfe6",
    accent="#f28fd0",
    success="#3ecf8e",
    warning="#ffd173",
    error="#ff6b6b",
    foreground="#e6e1cf",
    background="#0d0e12",
    surface="#161821",
    panel="#1f222d",
    dark=True,
)

SORTS: list[tuple[str, str]] = [
    ("relevance", "relevance"), ("name.keyword", "filename"), ("size", "size"),
    ("ts", "date"), ("format", "format"), ("family", "family"),
    ("itemid", "item"), ("addts", "when added"), ("b3sum", "random"),
]


# ----------------------------------------------------------------- navigation


@dataclass(frozen=True)
class Loc:
    """One rung of the browse drill-down stack."""

    kind: str                 # root | section | genre | listing
    section: str = ""
    genre: str = ""
    itemid: int = 0
    path: str = ""
    label: str = ""
    #: Pane this rung was jumped into from, if any.  Backing out of it returns
    #: there instead of climbing further up the browse tree -- arriving from a
    #: search and then finding yourself at the collection root is disorienting,
    #: and loses the result list you were working through.
    origin: str = ""

    def crumb(self) -> str:
        return self.label or self.section or "discmaster"


@dataclass
class Row:
    """A table row plus whatever it points at.

    ``name``/``family`` are kept apart from ``cells`` so the first column can be
    re-rendered with or without a selection marker without rebuilding the row.
    """

    key: str
    name: str
    family: str
    cells: tuple[Any, ...]          # columns 2..n
    entry: Entry | None = None
    nav: Loc | None = None
    task_id: int = 0
    search_text: str = ""
    lead: Any = None                # replaces the name cell entirely (queue pane)
    #: One comparable value per column, in column order, for client-side sorting.
    #: Display strings will not do -- "1.5G" sorts before "152M" as text.
    #: None means "no value here"; such rows are parked at the end either way.
    sort_keys: tuple[Any, ...] = ()


# ---------------------------------------------------------------------- panes


class Pane(Vertical):
    """Common table behaviour: marks, vim motions, filtering, detail sync."""

    filter_text: reactive[str] = reactive("")

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("g", "top", "top", show=False),
        Binding("G", "bottom", "bottom", show=False),
        Binding("space", "toggle_mark", "mark", show=False),
        Binding("ctrl+a", "mark_all", "mark all", show=False),
        Binding("slash", "focus_filter", "filter", show=False),
        Binding("escape", "clear_selection", "clear", show=False),
        Binding("s", "cycle_sort", "sort", show=True),
        Binding("S", "reverse_sort", "reverse", show=False),
    ]

    def __init__(self, app_ref: "DiscDig", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dig = app_ref
        self.rows: list[Row] = []
        self.visible_rows: list[Row] = []
        self.busy = False
        self._rendered_width = -1
        #: Column index to sort on, or None for the order the site gave us.
        self.sort_col: int | None = None
        self.sort_desc = False

    # -- the top-bar context slot -----------------------------------------

    def context_line(self) -> str:
        """What this pane wants shown in the top bar while it is on screen.

        One slot serves all three panes, so each has to be able to say what
        belongs there; the app re-reads this on every pane switch rather than
        leaving whichever pane wrote last in possession of the line.
        """
        return ""

    def push_context(self, text: str = "") -> None:
        """Write to the top bar, but only while this pane is the visible one.

        Workers outlive their pane being on screen -- a browse load or a search
        finishes long after the reader has moved to the queue -- and a line
        pinned above an unrelated pane is worse than no line at all.  With no
        argument this restores the pane's own summary.
        """
        if self.dig.active_pane == self.id:
            self.dig.set_context(text or self.context_line())

    def set_busy(self, busy: bool, what: str = "") -> None:
        """Show that work is in flight *without* taking the table out of play.

        Textual's ``widget.loading`` overlays a spinner and steals focus, which
        silently swallows keypresses -- pressing backspace twice while a folder
        loaded only went up one level.  A marker in the header keeps the table
        focused and every key live.
        """
        self.busy = busy
        if busy:
            self.push_context(f"{what} …" if what else "loading …")
        else:
            # Put the pane's own line back, so a load that fails cannot leave
            # "loading …" hanging in the top bar for the rest of the session.
            self.push_context()

    # -- table plumbing ---------------------------------------------------

    @property
    def table(self) -> MarkedTable:
        return self.query_one(MarkedTable)

    def maybe_table(self) -> MarkedTable | None:
        """The table, or None before compose has run / after teardown.

        The 4 Hz refresh timer can fire either side of a pane's lifetime, and a
        NoMatches there would take the whole app down.
        """
        found = self.query(MarkedTable)
        return found.first(MarkedTable) if found else None

    def current(self) -> Row | None:
        table = self.maybe_table()
        if table is None or not self.visible_rows or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self.visible_rows):
            return None
        return self.visible_rows[table.cursor_row]

    def selection(self) -> list[Row]:
        """Marked rows if any, else the row under the cursor."""
        table = self.table
        if table.marked:
            return [r for r in self.rows if r.key in table.marked]
        row = self.current()
        return [row] if row else []

    def render_rows(self, keep_cursor: bool = False) -> None:
        table = self.table
        cursor = table.cursor_row if keep_cursor else 0
        needle = self.filter_text.lower()
        self.visible_rows = [
            r for r in self.rows if not needle or needle in r.search_text.lower()
        ]
        table.clear()
        self.apply_sort()
        width = self.fit_columns()
        self._rendered_width = table.size.width
        for row in self.visible_rows:
            table.add_row(*self.decorate(row, width), key=row.key)
        if self.visible_rows:
            table.move_cursor(row=min(max(cursor, 0), len(self.visible_rows) - 1))
        self.dig.sync_detail()

    #: Index of the column carrying the name + family glyph.
    NAME_COL = 0
    #: Columns that absorb leftover width, as (index, min, ideal, share).
    #: Share splits anything left over once every column has reached its ideal.
    FLEX_COLS: tuple[tuple[int, int, int, float], ...] = ((0, 16, 70, 1.0),)
    #: Fixed columns as (index, ideal, minimum), least important last -- the
    #: last entries give up their space first when the terminal gets narrow.
    COLUMN_PLAN: tuple[tuple[int, int, int], ...] = ()

    def fit_columns(self) -> int:
        """Share the table's width out among the columns; return the name width.

        Fixed columns shrink toward their minimums, least important first, so
        the table stays whole in an 80-column window.  Surplus width is then
        spread across the flexible columns -- plural, because giving it all to
        one column left the file name swimming in blank space on a wide
        terminal while the item title beside it was still being cut off.
        Returns 0 before the first layout, meaning "nothing to size yet".
        """
        table = self.maybe_table()
        if table is None or not table.size.width:
            return 0
        cols = list(table.columns.values())
        pad = table.cell_padding * 2
        budget = table.size.width - pad * len(cols) - 1  # -1 for the scrollbar

        fixed = {i: ideal for i, ideal, _ in self.COLUMN_PLAN}
        flex_min = sum(lo for _, lo, _, _ in self.FLEX_COLS)

        over = sum(fixed.values()) + flex_min - budget
        for i, _ideal, floor in reversed(self.COLUMN_PLAN):
            if over <= 0:
                break
            give = min(over, fixed[i] - floor)
            fixed[i] -= give
            over -= give

        widths = dict(fixed)
        spare = max(0, budget - sum(fixed.values()) - flex_min)
        flex = {i: lo for i, lo, _, _ in self.FLEX_COLS}

        # First bring every flexible column up to its ideal, proportionally.
        head_room = {i: max(0, ideal - lo) for i, lo, ideal, _ in self.FLEX_COLS}
        wanted = sum(head_room.values())
        if wanted and spare:
            take = min(spare, wanted)
            for i, room in head_room.items():
                grant = int(take * room / wanted)
                flex[i] += grant
                spare -= grant

        # Then hand out whatever is still going spare by share.
        if spare:
            shares = {i: sh for i, _, _, sh in self.FLEX_COLS}
            total = sum(shares.values()) or 1.0
            for i, share in shares.items():
                grant = int(spare * share / total)
                flex[i] += grant
            flex[self.FLEX_COLS[0][0]] += spare - sum(
                int(spare * sh / total) for _, _, _, sh in self.FLEX_COLS
            )

        widths.update(flex)
        for i, width in widths.items():
            if i < len(cols):
                # auto_width must go: DataTable's content_width only ever grows,
                # so an auto column keeps the width of the widest row it has ever
                # held and the table sprouts a permanent horizontal scrollbar.
                cols[i].auto_width = False
                cols[i].width = width
                cols[i].content_width = width
        return widths.get(self.NAME_COL, 0)

    def decorate(self, row: Row, width: int = 0) -> tuple[Any, ...]:
        """Build the full cell tuple, drawing the mark into the first column."""
        marked = row.key in self.table.marked
        name_cell = family_cell(row.name, row.family, marked, width or self.fit_columns())
        if row.lead is not None:
            return (row.lead, name_cell, *row.cells)
        return (name_cell, *row.cells)

    def reflow_if_resized(self) -> None:
        """Re-render when the table's width changed (terminal resize, detail
        panel opening).  Polled from the app's timer, which catches every source
        of a size change without chasing individual resize events."""
        table = self.maybe_table()
        if table is None or not table.size.width or not self.rows:
            return
        if table.size.width != self._rendered_width:
            self.render_rows(keep_cursor=True)

    # -- sorting -----------------------------------------------------------

    #: Column indices that can be sorted on, in the order `s` cycles them.
    SORTABLE: tuple[int, ...] = ()
    #: Columns that read better largest-first the moment you choose them.
    SORT_DESC_FIRST: tuple[int, ...] = ()

    def apply_sort(self) -> None:
        """Order `visible_rows` by the chosen column.

        Rows with nothing in that column (a folder has no size) are parked at
        the end in both directions, rather than flooding the top of a
        smallest-first sort.
        """
        col = self.sort_col
        if col is None:
            return
        def value(row: Row) -> Any:
            keys = row.sort_keys
            return keys[col] if col < len(keys) else None
        present = [r for r in self.visible_rows if value(r) is not None]
        missing = [r for r in self.visible_rows if value(r) is None]
        present.sort(key=value, reverse=self.sort_desc)
        self.visible_rows = present + missing

    def sort_by(self, col: int | None) -> None:
        """Sort on a column; choosing the one already active flips direction."""
        if col is not None and col not in self.SORTABLE:
            self.dig.notify("that column can't be sorted")
            return
        if col is not None and col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = col in self.SORT_DESC_FIRST if col is not None else False
        self.label_headers()
        self.render_rows()
        self.dig.notify(self.sort_caption())

    def sort_caption(self) -> str:
        if self.sort_col is None:
            return "sorted the way the site lists it"
        name = str(self.base_headers()[self.sort_col]).strip() or "column"
        return f"sorted by {name}, {'largest' if self.sort_desc else 'smallest'} first" \
            if self.sort_col in self.SORT_DESC_FIRST else \
            f"sorted by {name}, {'Z-A' if self.sort_desc else 'A-Z'}"

    def base_headers(self) -> list[str]:
        """Header text without any sort marker, captured once the table exists."""
        cached = getattr(self, "_base_headers", None)
        if not cached:
            table = self.maybe_table()
            cached = [str(c.label) for c in table.columns.values()] if table else []
            if cached:
                self._base_headers = cached
        return cached

    def label_headers(self) -> None:
        """Mark the sorted column in its header so the state is visible."""
        table = self.maybe_table()
        if table is None:
            return
        base = self.base_headers()
        for i, col in enumerate(table.columns.values()):
            label = base[i] if i < len(base) else ""
            if i == self.sort_col:
                col.label = Text(f"{label} {'v' if self.sort_desc else '^'}",
                                 style="bold")
            else:
                col.label = Text(label)
        table.refresh()

    def action_cycle_sort(self) -> None:
        """Step through the sortable columns, then back to the natural order."""
        if not self.SORTABLE:
            self.dig.notify("nothing to sort here")
            return
        order: list[int | None] = [*self.SORTABLE, None]
        try:
            nxt = order[order.index(self.sort_col) + 1]
        except (ValueError, IndexError):
            nxt = order[0]
        self.sort_col = None if nxt is None else -1  # force sort_by to treat as new
        self.sort_by(nxt)

    def action_reverse_sort(self) -> None:
        if self.sort_col is None:
            self.dig.notify("pick a column with s first")
            return
        self.sort_desc = not self.sort_desc
        self.label_headers()
        self.render_rows()
        self.dig.notify(self.sort_caption())

    def on_data_table_header_selected(self, event: Any) -> None:
        event.stop()
        self.sort_by(event.column_index)

    def watch_filter_text(self) -> None:
        if self.is_mounted:
            self.render_rows()

    @on(Input.Changed, "Input.filter")
    def _row_filter(self, event: Input.Changed) -> None:
        """Narrow the rows as the reader types.

        This lived on SearchPane, but all three panes compose a filter box and
        inherit the "/" that opens it -- so in browse and queue you could open
        the box, type into it, and watch nothing whatsoever happen.
        """
        self.filter_text = event.value

    @on(Input.Submitted, "Input.filter")
    def _row_filter_done(self, event: Input.Submitted) -> None:
        """Enter commits the filter and hands the keys back to the table.

        Filtering is live, so the rows have already narrowed by the time enter
        is pressed; what it has to do is move the cursor out of the text field,
        where j/k/space/d were all being typed as characters.  The box stays on
        screen as a reminder of what is being hidden -- escape clears it.
        """
        event.stop()
        table = self.maybe_table()
        if table is not None:
            table.focus()

    # -- actions ----------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()

    def action_top(self) -> None:
        if self.visible_rows:
            self.table.move_cursor(row=0)

    def action_bottom(self) -> None:
        if self.visible_rows:
            self.table.move_cursor(row=len(self.visible_rows) - 1)

    def action_toggle_mark(self) -> None:
        row = self.current()
        if not row:
            return
        index = self.table.cursor_row
        self.table.toggle_mark(row.key)
        self.render_rows(keep_cursor=True)
        # Marking is usually done in a run, so step down to the next row.
        self.table.move_cursor(row=min(index + 1, len(self.visible_rows) - 1))
        self.dig.refresh_topbar()

    def action_mark_all(self) -> None:
        self.table.mark_all(r.key for r in self.visible_rows)
        self.render_rows(keep_cursor=True)
        self.dig.notify(f"marked {len(self.table.marked)}")

    def action_clear_selection(self) -> None:
        changed = bool(self.table.marked) or bool(self.filter_text)
        self.table.clear_marks()
        self.clear_filter_box()
        if changed:
            self.render_rows(keep_cursor=True)
        self.table.focus()

    def clear_filter_box(self) -> None:
        """Empty and hide the row-filter input, if this pane has one."""
        self.filter_text = ""
        filt = self.query("Input.filter")
        if not filt:
            return
        box = filt.first(Input)
        box.value = ""
        if box.has_focus:
            # Hiding the focused widget would strand the keyboard: hand it back.
            table = self.maybe_table()
            if table is not None:
                table.focus()
        box.display = False

    def focus_target(self):
        """Widget that should take focus when this pane is shown."""
        return self.maybe_table()

    def action_focus_filter(self) -> None:
        boxes = self.query("Input.filter")
        if not boxes:
            return
        box = boxes.first(Input)
        box.display = True
        box.focus()


class BrowsePane(Pane):
    """Collections → genres → items → archives → folders → files."""

    FLEX_COLS = ((0, 16, 70, 1.0),)                               # name
    COLUMN_PLAN = ((2, 6, 6), (1, 30, 6), (3, 10, 0), (4, 7, 0))  # size, kind, date, files
    SORTABLE = (0, 1, 2, 3, 4)          # name, kind, size, date, files
    SORT_DESC_FIRST = (2, 3, 4)         # size, date and count read big-first


    BINDINGS = [
        Binding("backspace,h", "go_up", "up", show=True),
        Binding("l", "descend", "open", show=False),
        Binding("ctrl+n", "reset_browse", "start over", show=True),
        Binding("d", "queue_selected", "queue", show=True),
        Binding("a", "queue_all", "queue all", show=True),
        Binding("R", "queue_recursive", "recurse", show=True),
        Binding("o", "open_web", "web", show=False),
        Binding("ctrl+r", "reload", "reload", show=False),
    ]

    def __init__(self, app_ref: "DiscDig", **kwargs: Any) -> None:
        super().__init__(app_ref, **kwargs)
        self.stack: list[Loc] = [Loc(kind="root", label="discmaster")]
        self.listing: Listing | None = None
        self.item_name: str = ""
        self.archive_org: str = ""

    def compose(self) -> ComposeResult:
        yield Static("discmaster", id="crumbs")
        box = Input(placeholder="filter these rows…   esc to go back", classes="filter")
        box.display = False
        yield box
        table = MarkedTable(id="browse-table")
        table.add_column("Name")
        table.add_column("Kind", width=30)
        table.add_column("Size", width=6)
        table.add_column("Date", width=10)
        table.add_column("Files", width=7)
        yield table

    def on_mount(self) -> None:
        self.load()

    # -- navigation -------------------------------------------------------

    @property
    def here(self) -> Loc:
        return self.stack[-1]

    def push(self, loc: Loc) -> None:
        self.stack.append(loc)
        self.load()

    def action_go_up(self) -> None:
        if len(self.stack) <= 1:
            self.dig.notify("already at the top")
            return
        leaving = self.stack.pop()
        self.load()
        if leaving.origin:
            # Came here from another pane; go back to it rather than surfacing
            # in a part of the tree the reader never navigated through.
            self.dig.show_pane(leaving.origin)

    def action_reload(self) -> None:
        self.load()

    def action_reset_browse(self) -> None:
        """Climb the whole way out, back to the collection root.

        Backspace unwinds one rung at a time, which is no help nine folders
        deep inside an ISO, and left this pane sitting on a stale listing after
        the reader had started over everywhere else.  Same key as the search
        pane's reset: ^n means "blank slate here" in whichever pane you're in.
        """
        if len(self.stack) == 1 and not self.filter_text and not self.table.marked:
            self.dig.notify("already at the top")
            return
        self.stack = [Loc(kind="root", label="discmaster")]
        self.listing = None
        self.item_name = ""
        self.archive_org = ""
        self.sort_col = None
        self.sort_desc = False
        self.clear_filter_box()
        self.load()
        self.dig.notify("back to the top")

    def action_descend(self) -> None:
        row = self.current()
        if row:
            self.open_row(row)

    @on(MarkedTable.RowSelected)
    def _row_selected(self, event: MarkedTable.RowSelected) -> None:
        row = self.current()
        if row:
            self.open_row(row)

    @on(MarkedTable.RowHighlighted)
    def _row_highlighted(self) -> None:
        self.dig.sync_detail()

    def open_row(self, row: Row) -> None:
        if self.busy:
            return  # rows on screen belong to the previous location
        if row.nav:
            self.push(row.nav)
        elif row.entry and row.entry.is_container:
            self.push(Loc(kind="listing", itemid=row.entry.itemid,
                          path=row.entry.fileid, label=row.entry.name))
        elif row.entry:
            self.dig.inspect(row.entry)

    def goto(self, itemid: int, path: str, label: str = "", origin: str = "") -> None:
        """Jump straight to a location (used by search cross-links).

        ``origin`` names the pane to hand control back to when the reader backs
        out of this rung again.
        """
        self.stack = [
            Loc(kind="root", label="discmaster"),
            Loc(kind="listing", itemid=itemid, path=path,
                label=label or path or f"#{itemid}", origin=origin),
        ]
        self.load()

    # -- loading ----------------------------------------------------------

    @work(exclusive=True, group="browse")
    async def load(self) -> None:
        loc = self.here
        table = self.table
        table.clear_marks()
        self.clear_filter_box()
        self.set_busy(True, self.crumb_line())
        try:
            if loc.kind == "root":
                self.rows = self._root_rows()
                self.listing = None
            elif loc.kind == "section":
                self.rows = await self._section_rows(loc)
            elif loc.kind == "genre":
                items = await self.dig.dm.genre_items(loc.section, loc.genre)
                self.rows = self._item_rows(items)
                self.listing = None
            else:
                listing = await self.dig.dm.browse(loc.itemid, loc.path)
                self.listing = listing
                if not loc.path:
                    self.item_name = listing.title
                    self.archive_org = listing.archive_org
                    self.dig.remember_item(loc.itemid, listing.title, listing.archive_org)
                self.rows = self._entry_rows(listing.entries)
        except httpx.HTTPStatusError as exc:
            self.rows = []
            self.dig.notify(f"{exc.response.status_code} loading that page",
                            severity="error")
        except (httpx.HTTPError, RuntimeError) as exc:
            self.rows = []
            self.dig.notify(f"could not load: {exc}", severity="error")
        finally:
            self.set_busy(False)

        # /ftp has no genres, so a section there lands on an item table.
        kind = loc.kind
        if kind == "section" and self.rows and self.rows[0].key.startswith("item:"):
            kind = "genre"
        self.set_headers(kind)
        self.render_rows()
        self.query_one("#crumbs", Static).update(self.crumb_line(markup=True))
        self.push_context()

    def _root_rows(self) -> list[Row]:
        blurbs = {
            "cd-rom": "CD-ROMs, by genre",
            "disk": "floppies and disk images",
            "ftp": "mirrored FTP sites",
            "other": "everything else",
        }
        return [
            Row(key=f"sec:{s}", name=s, family="directory",
                cells=(blurbs[s], "", "", ""),
                nav=Loc(kind="section", section=s, label=s),
                search_text=f"{s} {blurbs[s]}",
                sort_keys=(s, blurbs[s], None, None, None))
            for s in SECTIONS
        ]

    async def _section_rows(self, loc: Loc) -> list[Row]:
        genres = await self.dig.dm.genres(loc.section)
        if not genres:  # /ftp has no genre split
            items = await self.dig.dm.genre_items(loc.section)
            return self._item_rows(items)
        return [
            Row(key=f"gen:{loc.section}:{name}", name=name, family="directory",
                cells=("genre", "", "", f"{count:,}"),
                nav=Loc(kind="genre", section=loc.section, genre=name, label=name),
                search_text=name,
                sort_keys=(name.lower(), "genre", None, None, count))
            for name, count in genres
        ]

    def _item_rows(self, items: list[ItemSummary]) -> list[Row]:
        rows = []
        for it in items:
            self.dig.remember_item(it.itemid, it.title, it.archive_org)
            rows.append(
                Row(
                    key=f"item:{it.itemid}", name=it.title, family="archive",
                    cells=(
                        it.publisher or "",
                        human_size(it.size),
                        it.year,
                        f"{it.num_files:,}" if it.num_files else "",
                    ),
                    nav=Loc(kind="listing", itemid=it.itemid, label=it.title),
                    search_text=f"{it.title} {it.publisher} {it.year}",
                    sort_keys=(it.title.lower(), it.publisher.lower() or None,
                               it.size, it.year or None, it.num_files),
                )
            )
        return rows

    def _entry_rows(self, entries: Iterable[Entry]) -> list[Row]:
        return [
            Row(
                key=f"f:{e.itemid}:{e.fileid}", name=e.name, family=e.family,
                cells=(
                    e.format_name or e.family,
                    human_size(e.size),
                    e.date,
                    f"{e.sub_count:,}" if e.sub_count else "",
                ),
                entry=e,
                search_text=f"{e.name} {e.format_name}",
                sort_keys=(e.name.lower(), (e.format_name or e.family).lower(),
                           e.size, e.date or None, e.sub_count),
            )
            for e in entries
        ]

    #: Column headers per level -- the trailing count is discs at one level and
    #: files at the next, and an unqualified "Files" is misleading for both.
    HEADERS = {
        "root":    ("Name", "Kind", "Size", "Date", ""),
        "section": ("Genre", "Kind", "Size", "Date", "Discs"),
        "genre":   ("Title", "Publisher", "Size", "Year", "Files"),
        "listing": ("Name", "Format", "Size", "Date", "Files"),
    }

    def set_headers(self, kind: str) -> None:
        table = self.maybe_table()
        if table is None:
            return
        labels = self.HEADERS.get(kind, self.HEADERS["listing"])
        self._base_headers = list(labels)
        self.label_headers()

    def context_line(self) -> str:
        return self.crumb_line()

    def crumb_line(self, markup: bool = False) -> str:
        parts: list[str] = ["discmaster"]
        for loc in self.stack[1:]:
            if loc.kind == "listing" and loc.path:
                # A listing reached by drilling in: show just this level's name.
                parts.append(loc.label or loc.path.rsplit("/", 1)[-1])
            else:
                parts.append(loc.crumb())
        line = " / ".join(p for p in parts if p)
        return f"[b]{line}[/b]" if markup else line

    # -- download actions --------------------------------------------------

    def _entries(self, rows: Iterable[Row]) -> list[Entry]:
        return [r.entry for r in rows if r.entry is not None]

    def action_queue_selected(self) -> None:
        entries = self._entries(self.selection())
        if not entries:
            self.dig.notify("nothing here to download")
            return
        self.dig.queue_entries(entries, self._names())

    def action_queue_all(self) -> None:
        entries = [r.entry for r in self.visible_rows
                   if r.entry is not None and r.entry.downloadable]
        if not entries:
            self.dig.notify("no files in this listing")
            return
        self.dig.queue_entries(entries, self._names())

    def action_queue_recursive(self) -> None:
        loc = self.here
        row = self.current()
        if row and row.entry and row.entry.is_container:
            itemid, path, label = row.entry.itemid, row.entry.fileid, row.entry.name
        elif loc.kind == "listing":
            itemid, path, label = loc.itemid, loc.path, loc.label or self.item_name
        else:
            self.dig.notify("stand in a folder, or point at one, first")
            return
        self.dig.queue_recursive(itemid, path, label, self._names())

    def _names(self) -> dict[int, str]:
        loc = self.here
        if loc.kind == "listing" and self.item_name:
            return {loc.itemid: self.item_name}
        return {}

    def action_open_web(self) -> None:
        row = self.current()
        loc = self.here
        if row and row.entry:
            url = (row.entry.browse_url() if row.entry.is_container
                   else row.entry.view_url())
        elif row and row.nav and row.nav.kind == "listing":
            url = f"{BASE}/browse/{row.nav.itemid}"
        elif loc.kind == "listing":
            url = f"{BASE}/browse/{loc.itemid}/{loc.path}" if loc.path else f"{BASE}/browse/{loc.itemid}"
        else:
            url = BASE
        webbrowser.open(url)
        self.dig.notify("opened in your browser")


class SearchPane(Pane):
    """The full search API, filters and all."""

    FLEX_COLS = ((0, 16, 46, 0.5), (4, 18, 44, 0.5))              # name, item
    COLUMN_PLAN = ((2, 6, 6), (1, 24, 6), (3, 10, 0))             # size, kind, date
    SORTABLE = (0, 1, 2, 3, 4)
    SORT_DESC_FIRST = (2, 3)


    BINDINGS = [
        Binding("d", "queue_selected", "queue", show=True),
        Binding("a", "queue_all", "queue page", show=True),
        Binding("B", "bundle", "bundle", show=True),
        Binding("m,ctrl+t", "toggle_mode", "files/discs", show=True),
        Binding("ctrl+n", "reset_search", "start over", show=True),
        Binding("f", "toggle_filters", "filters", show=True),
        Binding("n", "next_page", "next", show=True),
        Binding("p", "prev_page", "prev", show=True),
        Binding("s", "cycle_sort", "sort", show=False),
        Binding("o", "open_web", "web", show=False),
        # '/' means "search" here, not "filter what's on screen" as it does in
        # the browse pane -- typing a query is the primary action in this pane.
        Binding("slash,ctrl+f", "focus_query", "type a query", show=True),
        Binding("backslash", "focus_filter", "filter rows", show=False),
    ]

    def __init__(self, app_ref: "DiscDig", **kwargs: Any) -> None:
        super().__init__(app_ref, **kwargs)
        self.page = 0
        self.total: int | None = None
        self.sort_index = 0
        self.sort_desc = False
        self.last_filters: dict[str, Any] = {}
        #: Last result summary, kept so the top bar can be rebuilt on demand.
        self.summary = ""
        #: "files" searches the 1.8bn indexed files; "discs" searches item titles.
        self.mode = "files"

    def context_line(self) -> str:
        return self.summary or "search"

    def compose(self) -> ComposeResult:
        with Horizontal(id="searchbar"):
            # A visible, clickable switch: "m" alone told nobody what it did.
            yield Static("", id="mode-switch")
            yield Input(id="query")
            yield Static("", classes="status", id="search-status")
        with Vertical(id="filters"):
            with Horizontal(classes="row"):
                yield Label("family")
                yield Select(compact=True, options=[(f, f) for f in (
                    "archive", "audio", "directory", "document", "executable", "font",
                    "image", "music", "other", "poly", "text", "unknown", "video")],
                    prompt="any", id="f-family")
                yield Label("extension")
                yield Input(placeholder="zip | txt", id="f-extension")
            with Horizontal(classes="row"):
                yield Label("format")
                yield Input(placeholder="dexvert format id, e.g. zip", id="f-format")
                yield Label("in item #")
                yield Input(placeholder="itemid", id="f-itemid")
            with Horizontal(classes="row"):
                yield Label("size")
                yield Input(placeholder="min e.g. 1k", id="f-sizeMin")
                yield Input(placeholder="max e.g. 4MB", id="f-sizeMax")
                yield Label("date")
                yield Input(placeholder="YYYYMMDD", id="f-tsMin")
                yield Input(placeholder="YYYYMMDD", id="f-tsMax")
            with Horizontal(classes="row"):
                yield Label("describe")
                yield Input(placeholder="visual: an orange cat", id="f-visual")
                yield Input(placeholder="audio: cat meowing", id="f-auditory")
            with Horizontal(classes="row"):
                yield Checkbox("search contents too", id="f-content")
                yield Checkbox("omit duplicates", id="f-dedup")
                yield Checkbox("deep mode", id="f-deep")
        box = Input(placeholder="filter these rows…   esc to go back", classes="filter")
        box.display = False
        yield box
        table = MarkedTable(id="search-table")
        table.add_column("Name")
        table.add_column("Kind", width=24)
        table.add_column("Size", width=6)
        table.add_column("Date", width=10)
        table.add_column("Item", width=28)
        yield table

    def on_mount(self) -> None:
        self.query_one("#query", Input).suggester = SuggestFromList(
            self.dig.store.history(), case_sensitive=False
        )
        self.show_mode()
        self.set_mode_headers()
        self.query_one("#search-status", Static).update(self.mode_hint())
        self.render_rows()

    # -- running searches ---------------------------------------------------

    def action_focus_query(self) -> None:
        self.query_one("#query", Input).focus()

    def focus_target(self):
        """Start in the query box; once there are hits, the table is more useful."""
        if self.rows:
            return self.maybe_table()
        return self.query_one("#query", Input)

    #: Column index -> discmaster sortBy field.
    SERVER_SORT = {0: "name.keyword", 1: "format", 2: "size", 3: "ts", 4: "itemid"}

    def on_data_table_header_selected(self, event: Any) -> None:
        """Sort the whole result set, not just the page in front of you."""
        event.stop()
        if self.mode == "discs":
            super().on_data_table_header_selected(event)  # discs are all local
            return
        field = self.SERVER_SORT.get(event.column_index)
        if field is None:
            return
        index = next((i for i, (key, _) in enumerate(SORTS) if key == field), None)
        if index is None:
            return
        if index == self.sort_index:
            self.sort_desc = not getattr(self, "sort_desc", False)
        else:
            self.sort_index = index
            self.sort_desc = field in ("size", "ts")  # big/recent first reads better
        self.page = 0
        self.label_headers()
        self.run_search()

    def action_reverse_sort(self) -> None:
        if self.mode == "discs":
            super().action_reverse_sort()
            return
        self.sort_desc = not getattr(self, "sort_desc", False)
        self.page = 0
        self.run_search()

    def label_headers(self) -> None:
        """Mirror the server-side sort in the header, like the local panes do."""
        if self.mode == "discs":
            super().label_headers()
            return
        table = self.maybe_table()
        if table is None:
            return
        field = SORTS[self.sort_index][0]
        active = next((i for i, f in self.SERVER_SORT.items() if f == field), None)
        base = self.base_headers()
        for i, col in enumerate(table.columns.values()):
            label = base[i] if i < len(base) else ""
            col.label = (Text(f"{label} {'v' if self.sort_desc else '^'}", style="bold")
                         if i == active else Text(label))
        table.refresh()

    #: The same five columns carry different things in each mode, so the
    #: headers change with it -- "Item" and "Date" mean nothing on a disc row.
    MODE_HEADERS = {
        "files": ("Name", "Kind", "Size", "Date", "Item"),
        "discs": ("Title", "Publisher", "Size", "Year", "Files"),
    }

    def set_mode_headers(self) -> None:
        if self.maybe_table() is None:
            return
        self._base_headers = list(self.MODE_HEADERS[self.mode])
        self.label_headers()

    def action_toggle_mode(self) -> None:
        self.mode = "discs" if self.mode == "files" else "files"
        self.page = 0
        self.show_mode()
        self.set_mode_headers()
        self.dig.notify(
            "Now finding whole CD-ROMs by title" if self.mode == "discs"
            else "Now finding individual files by name"
        )
        if self.query_one("#query", Input).value.strip():
            self.run_search()
        else:
            self.query_one("#search-status", Static).update(self.mode_hint())
            self.action_focus_query()

    def show_mode(self) -> None:
        """Draw the files/discs switch and match the placeholder to it.

        The switch carries its own shortcut because the alternative -- a bare
        "m" in the footer -- says nothing about what it toggles, and "m" cannot
        work while the query box has focus anyway: it just types an m.
        """
        switch = Text()
        for name, label in (("files", " FILES "), ("discs", " DISCS ")):
            switch.append(label, style="bold black on #ffb454" if self.mode == name
                          else "dim")
        switch.append(" ^t ", style="dim")
        self.query_one("#mode-switch", Static).update(switch)
        box = self.query_one("#query", Input)
        box.placeholder = (
            "title of a CD-ROM, e.g. Rise of the Triad   ·  ^t for files  ·  ? help"
            if self.mode == "discs" else
            "name of a file inside any disc   ·  ^t to find whole discs  ·  ? help"
        )

    def mode_hint(self) -> str:
        return ("looking for whole CD-ROMs by title"
                if self.mode == "discs" else
                "looking for files by name, inside every disc")

    def on_click(self, event: Any) -> None:
        """Clicking the switch flips it."""
        widget = getattr(event, "widget", None)
        if widget is not None and widget.id == "mode-switch":
            self.action_toggle_mode()

    def action_reset_search(self) -> None:
        """Wipe the query, every filter field and the sort, back to a blank slate.

        Filters left set from an earlier search are the classic way to get
        baffling "no results" -- an extension or size range you have forgotten
        about is invisible once the filter panel is closed again.
        """
        self.query_one("#query", Input).value = ""
        for box in self.query("#filters Input"):
            box.value = ""
        for tick in self.query("#filters Checkbox"):
            tick.value = False
        self.query_one("#f-family", Select).clear()
        self.sort_index, self.sort_desc = 0, False
        self.sort_col = None
        self.page = 0
        self.total = None
        self.last_filters = {}
        self.rows = []
        self.clear_filter_box()
        self.table.clear_marks()
        self.label_headers()
        self.render_rows()
        self.query_one("#search-status", Static).update(self.mode_hint())
        self.summary = ""
        self.push_context()
        self.dig.notify("cleared - query, filters and sort are back to default")
        self.action_focus_query()

    def action_toggle_filters(self) -> None:
        self.query_one("#filters").toggle_class("-visible")

    @on(Input.Submitted, "#query")
    def _submitted(self) -> None:
        self.page = 0
        self.run_search()

    @on(Input.Submitted, "#filters Input")
    def _filter_submitted(self) -> None:
        self.page = 0
        self.run_search()

    def collect(self) -> dict[str, Any]:
        def val(sel: str) -> str:
            return self.query_one(sel, Input).value.strip()

        filters: dict[str, Any] = {
            "q": self.query_one("#query", Input).value.strip(),
            "extension": val("#f-extension"),
            "format": val("#f-format"),
            "itemid": val("#f-itemid"),
            "sizeMin": val("#f-sizeMin"),
            "sizeMax": val("#f-sizeMax"),
            "tsMin": val("#f-tsMin"),
            "tsMax": val("#f-tsMax"),
            "visual": val("#f-visual"),
            "auditory": val("#f-auditory"),
            "limit": 100,
            "sortBy": SORTS[self.sort_index][0],
            "sortOrderDesc": "desc" if getattr(self, "sort_desc", False) else "",
            "nsfw": self.dig.cfg.safe_search,
        }
        # An untouched Select yields Select.NULL, not "" -- test for a real string
        # rather than against a sentinel whose name has moved between versions.
        family = self.query_one("#f-family", Select).value
        if isinstance(family, str) and family:
            filters["family"] = family
        if self.query_one("#f-content", Checkbox).value:
            filters["qfields"] = "t"
        if self.query_one("#f-dedup", Checkbox).value:
            filters["dedup"] = "dedup"
        if self.query_one("#f-deep", Checkbox).value:
            filters["mode"] = "deep"
        return filters

    @staticmethod
    def filters_in_use(filters: dict[str, Any]) -> bool:
        """Anything narrowing the search beyond the plain query text."""
        return any(filters.get(k) for k in (
            "extension", "format", "itemid", "sizeMin", "sizeMax", "tsMin", "tsMax",
            "visual", "auditory", "family", "dedup", "qfields", "mode"))

    def action_next_page(self) -> None:
        if self.mode == "discs":
            self.dig.notify("disc results all fit on one page")
            return
        self.page += 1
        self.run_search(keep_filters=True)

    def action_prev_page(self) -> None:
        if self.mode == "discs" or self.page == 0:
            return
        self.page -= 1
        self.run_search(keep_filters=True)

    def action_cycle_sort(self) -> None:
        """In file mode the server does the sorting -- it covers every match,
        not only the page on screen.  Disc results are local, so they use the
        shared client-side sort."""
        if self.mode == "discs":
            super().action_cycle_sort()
            return
        self.sort_index = (self.sort_index + 1) % len(SORTS)
        self.dig.notify(f"sort: {SORTS[self.sort_index][1]}")
        self.page = 0
        self.run_search()

    def preset(self, query: str) -> None:
        self.query_one("#query", Input).value = query
        self.page = 0
        self.run_search()

    @work(exclusive=True, group="search")
    async def run_search(self, keep_filters: bool = False) -> None:
        if self.mode == "discs":
            await self.search_discs()
            return
        filters = self.last_filters if keep_filters and self.last_filters else self.collect()
        self.last_filters = filters
        has_query = any(filters.get(k) for k in
                        ("q", "extension", "format", "itemid", "visual", "auditory",
                         "sizeMin", "sizeMax", "tsMin", "tsMax", "family"))
        if not has_query:
            self.dig.notify("type something to search for")
            return

        status = self.query_one("#search-status", Static)
        status.update("searching…")
        table = self.table
        self.set_busy(True, "searching")
        table.clear_marks()
        self.clear_filter_box()
        started = time.monotonic()
        try:
            page = await self.dig.dm.search(filters, self.page)
            self.rows = self._rows(page.results)
            if not keep_filters:
                self.total, _ = await self.dig.dm.search_count(filters)
            self.dig.store.remember(str(filters.get("q") or ""))
        except (httpx.HTTPError, RuntimeError) as exc:
            self.rows = []
            self.dig.notify(f"search failed: {exc}", severity="error")
        finally:
            self.set_busy(False)

        took = (time.monotonic() - started) * 1000
        shown = len(self.rows)
        total = f"{self.total:,} matches" if self.total is not None else f"{shown} shown"
        pages = ""
        if self.total:
            last = max(1, -(-min(self.total, 10_000) // 100))
            pages = f" · page {self.page + 1}/{last}"
        arrow = "desc" if self.sort_desc else "asc"
        # Say when filters are narrowing things: a forgotten extension or size
        # range is invisible once the panel is closed, and is the usual reason a
        # search that "should" match comes back empty.
        active = " · filters on (^n clears)" if self.filters_in_use(filters) else ""
        status.update(f"{total}{pages} · {took:.0f}ms · "
                      f"sort {SORTS[self.sort_index][1]} {arrow}{active}")
        self.label_headers()
        self.render_rows()
        self.summary = f"search: {filters.get('q') or '(filters)'} — {total}"
        self.push_context()
        self.table.focus()

    async def search_discs(self) -> None:
        """Search item titles across all four collections at once.

        Discmaster has no API for this -- each collection index takes a
        ``?search=`` parameter and answers with an HTML item table -- but the
        four requests run concurrently and land in well under a second.
        Searching files for "Rise of the Triad" returns 127 catalogue .html
        stubs; searching discs returns the one CD-ROM you actually wanted.
        """
        query = self.query_one("#query", Input).value.strip()
        status = self.query_one("#search-status", Static)
        if not query:
            self.dig.notify("type a disc title to look for")
            return

        status.update("searching disc titles...")
        self.set_busy(True, "searching discs")
        self.table.clear_marks()
        self.clear_filter_box()
        started = time.monotonic()
        try:
            found = await asyncio.gather(
                *(self.dig.dm.genre_items(section, search=query) for section in SECTIONS),
                return_exceptions=True,
            )
            items: list[ItemSummary] = []
            seen: set[int] = set()
            for result in found:
                if isinstance(result, BaseException):
                    continue
                for item in result:
                    if item.itemid not in seen:
                        seen.add(item.itemid)
                        items.append(item)
            items.sort(key=lambda i: (i.title.lower(), i.year))
            self.rows = self.disc_rows(items)
            self.total = len(items)
            self.dig.store.remember(query)
        except (httpx.HTTPError, RuntimeError) as exc:
            self.rows = []
            self.dig.notify(f"disc search failed: {exc}", severity="error")
        finally:
            self.set_busy(False)

        took = (time.monotonic() - started) * 1000
        noun = "disc" if self.total == 1 else "discs"
        status.update(f"{self.total:,} {noun} \u00b7 {took:.0f}ms \u00b7 "
                      "enter opens \u00b7 d queues it \u00b7 ^n starts over")
        self.render_rows()
        self.summary = f"discs: {query} \u2014 {self.total:,} found"
        self.push_context()
        self.table.focus()

    def disc_rows(self, items: list[ItemSummary]) -> list[Row]:
        """Disc rows reuse the file columns: title, publisher, size, year, files."""
        rows = []
        for it in items:
            self.dig.remember_item(it.itemid, it.title, it.archive_org)
            rows.append(
                Row(
                    key=f"i:{it.itemid}", name=it.title, family="archive",
                    cells=(
                        it.publisher or "",
                        human_size(it.size),
                        it.year,
                        f"{it.num_files:,} files" if it.num_files else "",
                    ),
                    nav=Loc(kind="listing", itemid=it.itemid, label=it.title),
                    search_text=f"{it.title} {it.publisher} {it.year}",
                    sort_keys=(it.title.lower(), it.publisher.lower() or None,
                               it.size, it.year or None, it.num_files),
                )
            )
        return rows

    def _rows(self, results: list[Entry]) -> list[Row]:
        rows = []
        for e in results:
            if e.item_name:
                self.dig.remember_item(e.itemid, e.item_name, "")
            rows.append(
                Row(
                    key=f"s:{e.itemid}:{e.fileid}", name=e.name, family=e.family,
                    cells=(
                        e.format_name or e.family,
                        human_size(e.size),
                        e.date,
                        e.item_name or f"#{e.itemid}",
                    ),
                    entry=e,
                    search_text=f"{e.name} {e.format_name} {e.item_name}",
                    sort_keys=(e.name.lower(), (e.format_name or e.family).lower(),
                               e.size, e.date or None, e.item_name.lower() or None),
                )
            )
        return rows

    @on(MarkedTable.RowHighlighted)
    def _row_highlighted(self) -> None:
        self.dig.sync_detail()

    @on(MarkedTable.RowSelected)
    def _open(self) -> None:
        """Enter opens: a disc row goes to the item, a file row to its folder."""
        row = self.current()
        if row and row.nav:
            self.dig.browse.goto(row.nav.itemid, "", row.nav.label, origin="search")
            self.dig.show_pane("browse")
            self.dig.notify("backspace returns to your results")
            return
        if not row or not row.entry:
            return
        e = row.entry
        parent = e.fileid.rsplit("/", 1)[0] if "/" in e.fileid else ""
        if e.is_container:
            parent = e.fileid
        self.dig.browse.goto(e.itemid, parent, e.item_name or f"#{e.itemid}",
                             origin="search")
        self.dig.show_pane("browse")
        self.dig.notify("backspace returns to your results")

    # -- downloads ----------------------------------------------------------

    def action_queue_selected(self) -> None:
        rows = self.selection()
        discs = [r for r in rows if r.nav]
        if discs:
            self.dig.queue_discs([(r.nav.itemid, r.name) for r in discs])
            return
        entries = [r.entry for r in rows if r.entry]
        if not entries:
            self.dig.notify("nothing selected")
            return
        self.dig.queue_entries(entries, {})

    def action_queue_all(self) -> None:
        if self.mode == "discs":
            self.dig.notify("mark the discs you want, then press d")
            return
        entries = [r.entry for r in self.visible_rows
                   if r.entry is not None and r.entry.downloadable]
        if not entries:
            self.dig.notify("no downloadable results on this page")
            return
        self.dig.queue_entries(entries, {})

    def action_bundle(self) -> None:
        if not self.last_filters:
            self.dig.notify("run a search first")
            return
        self.dig.queue_bundle(self.last_filters, self.total)

    def action_open_web(self) -> None:
        row = self.current()
        if row and row.entry:
            webbrowser.open(row.entry.view_url())
            self.dig.notify("opened in your browser")


class QueuePane(Pane):
    """Live view of the download queue."""

    NAME_COL = 1  # the status glyph occupies column 0
    # status, progress, size, speed, eta, where
    FLEX_COLS = ((1, 16, 48, 0.6), (6, 14, 32, 0.4))              # name, where
    COLUMN_PLAN = ((0, 3, 3), (2, 20, 8), (3, 6, 6), (4, 9, 0), (5, 7, 0))
    SORTABLE = (0, 1, 2, 3, 4, 6)       # status, name, progress, size, speed, where
    SORT_DESC_FIRST = (2, 3, 4)


    BINDINGS = [
        Binding("p", "pause_resume", "pause", show=True),
        Binding("P", "pause_all", "pause all", show=True),
        Binding("x", "cancel", "cancel", show=True),
        Binding("r", "retry", "retry", show=True),
        Binding("C", "clear", "clear done", show=True),
        Binding("o", "open_web", "web", show=False),
        Binding("plus,equals_sign", "more", "+worker", show=False),
        Binding("minus", "fewer", "-worker", show=False),
        Binding("O", "open_folder", "folder", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="queuehead"):
            with Horizontal(classes="figures"):
                yield Static("", classes="figure", id="q-counts")
                yield Static("", classes="figure", id="q-rate")
                yield Static("", classes="figure", id="q-dest")
            yield ProgressBar(total=100, show_eta=False, id="q-bar")
            yield Static("", id="q-note")
        box = Input(placeholder="filter these rows…   esc to go back", classes="filter")
        box.display = False
        yield box
        table = MarkedTable(id="queue-table")
        table.add_column(" ", width=3)
        table.add_column("Name")
        table.add_column("Progress", width=20)
        table.add_column("Size", width=6)
        table.add_column("Speed", width=9)
        table.add_column("ETA", width=7)
        table.add_column("Where", width=26)
        yield table

    def on_mount(self) -> None:
        self.refresh_queue()

    def refresh_queue(self, keep_cursor: bool = True) -> None:
        dl = self.dig.dl
        # Size the columns first so the bars are drawn to the width they'll get.
        self.fit_columns()
        cols = list(self.table.columns.values()) if self.maybe_table() else []
        bar_width = max(6, (cols[2].width if len(cols) > 2 else 20) - 6)
        palette = self.dig.palette
        tasks = sorted(dl.tasks.values(), key=lambda t: (_status_rank(t.status), -t.id))
        self.rows = [
            Row(
                key=f"t:{t.id}", name=t.name, family=t.family or "other",
                lead=status_cell(t, palette),
                cells=(
                    progress_cell(t, bar_width, palette, self.dig.bar_phase),
                    human_size(t.total or t.expected_size),
                    rate(t.speed),
                    duration(t.eta),
                    t.item_name or f"#{t.itemid}",
                ),
                task_id=t.id,
                search_text=f"{t.name} {t.item_name} {t.status}",
                sort_keys=(_status_rank(t.status), t.name.lower(), t.pct,
                           t.total or t.expected_size, t.speed,
                           t.eta, t.item_name.lower() or None),
            )
            for t in tasks
        ]
        self.render_rows(keep_cursor=keep_cursor)
        self._update_head()

    def context_line(self) -> str:
        if not self.dig.dl.tasks:
            return "queue: empty"
        p = self.dig.dl.progress()
        bits = [f"{p.active} active", f"{p.queued} waiting", f"{p.done} done"]
        if p.failed:
            bits.append(f"{p.failed} failed")
        if p.paused:
            bits.append(f"{p.paused} paused")
        return "queue: " + " · ".join(bits)

    def _update_head(self) -> None:
        p = self.dig.dl.progress()
        paused = self.dig.dl.paused_all
        counts = Text()
        counts.append(f" {p.active} active", style="bold bright_green")
        counts.append(f"  {p.queued} waiting", style="dim")
        counts.append(f"  {p.done} done", style="green")
        if p.failed:
            counts.append(f"  {p.failed} failed", style="bold red")
        if p.paused:
            counts.append(f"  {p.paused} paused", style="yellow")
        self.query_one("#q-counts", Static).update(counts)

        speed = Text()
        speed.append("PAUSED" if paused else rate(p.speed) or "idle",
                     style="bold yellow" if paused else "bold bright_green")
        if p.total:
            speed.append(f"   {human_size(p.downloaded)} / {human_size(p.total)}", style="dim")
        self.query_one("#q-rate", Static).update(speed)

        self.query_one("#q-dest", Static).update(
            Text(f"{self.dig.cfg.concurrency} at a time  →  {self.dig.cfg.download_dir}",
                 style="dim")
        )
        # p.pct measures outstanding work, so it reads 0 once the queue drains;
        # show a full bar in that case rather than an empty one.
        outstanding = p.active + p.queued + p.paused
        pct = p.pct if outstanding else (100.0 if p.done else 0.0)
        self.query_one("#q-bar", ProgressBar).update(total=100, progress=pct)

        row = self.current()
        note = ""
        if row:
            task = self.dig.dl.tasks.get(row.task_id)
            if task and task.error:
                note = f"[red]{task.error}[/red]"
            elif task and task.warn:
                note = f"[yellow]! {task.warn}[/yellow]"
            elif task:
                note = f"[dim]{task.dest}[/dim]"
        self.query_one("#q-note", Static).update(note)
        self.push_context()

    @on(MarkedTable.RowHighlighted)
    def _row_highlighted(self) -> None:
        self._update_head()
        self.dig.sync_detail()

    def _task_ids(self) -> list[int]:
        return [r.task_id for r in self.selection() if r.task_id]

    def action_pause_resume(self) -> None:
        dl = self.dig.dl
        for tid in self._task_ids():
            task = dl.tasks.get(tid)
            if not task:
                continue
            if task.status in (QUEUED, RUNNING):
                dl.pause(tid)
            else:
                dl.resume(tid)
        self.refresh_queue()

    def action_pause_all(self) -> None:
        paused = self.dig.dl.toggle_pause_all()
        self.dig.notify("queue paused" if paused else "queue running")
        self.refresh_queue()

    def action_cancel(self) -> None:
        ids = self._task_ids()
        if not ids:
            return
        for tid in ids:
            self.dig.dl.cancel(tid)
        self.table.clear_marks()
        self.dig.notify(f"removed {len(ids)}")
        self.refresh_queue(keep_cursor=True)

    def action_open_web(self) -> None:
        """Open the discmaster page for the file this row is fetching.

        The help lists "o" among the things that work on a row and every queue
        row names a real file, but the binding only ever existed on browse and
        search, so the key died here.  A queue row is always a file in transit
        rather than something you descended into, so it goes to the file's own
        /view/ page even for archives.  A search bundle has no page: the server
        builds it on demand and it carries itemid 0.
        """
        row = self.current()
        task = self.dig.dl.tasks.get(row.task_id) if row else None
        if task is None:
            return
        if not task.itemid:
            self.dig.notify("a search bundle has no page on discmaster")
            return
        webbrowser.open(Entry(itemid=task.itemid, fileid=task.fileid,
                              name=task.name, family=task.family).view_url())
        self.dig.notify("opened in your browser")

    def action_retry(self) -> None:
        n = self.dig.dl.retry_failed()
        self.dig.notify(f"retrying {n}" if n else "nothing failed")
        self.refresh_queue()

    def action_clear(self) -> None:
        n = self.dig.dl.clear_finished()
        self.dig.notify(f"cleared {n}")
        self.refresh_queue(keep_cursor=False)

    def action_more(self) -> None:
        n = self.dig.dl.set_concurrency(self.dig.cfg.concurrency + 1)
        self.dig.notify(f"{n} downloads at a time")
        self._update_head()

    def action_fewer(self) -> None:
        n = self.dig.dl.set_concurrency(self.dig.cfg.concurrency - 1)
        self.dig.notify(f"{n} downloads at a time")
        self._update_head()

    def action_open_folder(self) -> None:
        path = Path(self.dig.cfg.download_dir)
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            self.dig.notify(f"could not open the folder: {exc}", severity="error")


def _status_rank(status: str) -> int:
    return {RUNNING: 0, QUEUED: 1, PAUSED: 2, FAILED: 3, DONE: 4}.get(status, 5)




# ------------------------------------------------------------------------ app


def asset(name: str) -> Path:
    """Absolute path to a bundled data file, frozen or running from source.

    Textual resolves a relative ``CSS_PATH`` against the module's own file,
    which does not survive being packed by PyInstaller -- the modules end up
    inside ``_internal`` while data files are unpacked beside them.  Handing it
    an absolute path sidesteps the question entirely.
    """
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return root / "discdig" / name
    return Path(__file__).resolve().parent / name


class DiscDig(App[None]):
    CSS_PATH = str(asset("discdig.tcss"))
    TITLE = "discdig"

    BINDINGS = [
        Binding("1", "pane('browse')", "browse", show=True),
        Binding("2", "pane('search')", "search", show=True),
        Binding("3", "pane('queue')", "queue", show=True),
        # 'tab' stays with Textual's focus chain -- the search pane has a dozen
        # fields that need it -- so pane cycling lives on the bracket keys.
        Binding("right_square_bracket", "next_pane", "next pane", show=False),
        Binding("left_square_bracket", "prev_pane", "prev pane", show=False),
        Binding("i", "detail", "detail", show=True),
        Binding("question_mark", "help", "help", show=True),
        Binding("comma", "settings", "settings", show=False),
        Binding("q", "quit", "quit", show=True),
    ]

    PANES = ("browse", "search", "queue")

    def __init__(self, start: str = "browse", query: str = "",
                 goto: tuple[int, str] | None = None) -> None:
        super().__init__()
        self.cfg = Config.load()
        self.store = Store()
        self.dm = DiscMaster()
        self.dl = Downloader(self.store, self.cfg, self.dm.client,
                             on_change=self._queue_dirty)
        self.active_pane = start
        self.start_query = query
        self.start_goto = goto
        self.items: dict[int, tuple[str, str]] = {}  # itemid -> (title, ia identifier)
        self.palette = BarPalette.from_theme(DISCDIG_THEME)
        #: Advances while transfers run, driving the indeterminate bar.
        self.bar_phase = 0
        self._dirty = False
        self._context_text = ""

    # -- composition -------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("discdig", classes="brand")
            yield Static("", classes="context", id="context")
            yield Static("", classes="stats", id="stats")
        with Horizontal(id="tabbar"):
            yield Static("1 browse", classes="tab", id="tab-browse")
            yield Static("2 search", classes="tab", id="tab-search")
            yield Static("3 queue", classes="tab", id="tab-queue")
            yield Static("", classes="spacer")
            yield Static("? keys", classes="hint")
        with Horizontal(id="body"):
            with Horizontal(classes="split"):
                yield BrowsePane(self, id="browse", classes="pane")
                yield SearchPane(self, id="search", classes="pane")
                yield QueuePane(self, id="queue", classes="pane")
            yield VerticalScroll(Static("", id="detail-body"), id="detail")
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme(DISCDIG_THEME)
        saved = self.cfg.theme
        self.theme = saved if saved in self.available_themes else DEFAULT_THEME
        if self.theme != saved:
            self.cfg.theme = self.theme   # self-heal a theme that no longer exists
            self.cfg.save()
        # Remember whatever the command palette's theme picker switches to.
        # Registered as an extra watcher rather than a watch_theme method so we
        # never shadow Textual's own handling of the reactive.
        self.watch(self, "theme", self._remember_theme, init=False)
        self.rebuild_palette()
        self.dl.load()
        await self.dl.start()
        self.show_pane(self.active_pane)
        self.set_interval(0.1, self._tick)
        if self.start_goto:
            self.browse.goto(*self.start_goto)
            self.show_pane("browse")
        if self.start_query:
            self.search.preset(self.start_query)
            self.show_pane("search")
        pending = sum(1 for t in self.dl.tasks.values() if t.status in (QUEUED, PAUSED))
        if pending:
            self.notify(f"{pending} download(s) picked up from last time")

    async def on_unmount(self) -> None:
        await self.dl.stop()
        await self.dm.aclose()
        self.store.close()

    # -- panes --------------------------------------------------------------

    @property
    def browse(self) -> BrowsePane:
        return self.query_one("#browse", BrowsePane)

    @property
    def search(self) -> SearchPane:
        return self.query_one("#search", SearchPane)

    @property
    def queue(self) -> QueuePane:
        return self.query_one("#queue", QueuePane)

    def pane_widget(self, name: str) -> Pane:
        return self.query_one(f"#{name}", Pane)

    def maybe_pane(self, name: str) -> Pane | None:
        found = self.query(f"#{name}")
        return found.first(Pane) if found else None

    def show_pane(self, name: str) -> None:
        self.active_pane = name
        for pane in self.PANES:
            self.query_one(f"#{pane}").display = pane == name
            self.query_one(f"#tab-{pane}").set_class(pane == name, "-active")
        widget = self.pane_widget(name)
        self.refresh_context()
        if name == "queue":
            self.queue.refresh_queue()
        target = widget.focus_target()
        if target is not None:
            target.focus()
        self.sync_detail()

    def action_pane(self, name: str) -> None:
        self.show_pane(name)

    def action_next_pane(self) -> None:
        idx = (self.PANES.index(self.active_pane) + 1) % len(self.PANES)
        self.show_pane(self.PANES[idx])

    def action_prev_pane(self) -> None:
        idx = (self.PANES.index(self.active_pane) - 1) % len(self.PANES)
        self.show_pane(self.PANES[idx])

    # -- chrome --------------------------------------------------------------

    def _remember_theme(self, theme: str) -> None:
        if theme and theme != self.cfg.theme:
            self.cfg.theme = theme
            self.cfg.save()
        self.rebuild_palette()

    def rebuild_palette(self) -> None:
        """Re-derive the bar colours from whatever theme is now active."""
        try:
            self.palette = BarPalette.from_theme(self.current_theme)
        except Exception:  # noqa: BLE001 - never let a theme break the UI
            return
        bars = self.query("#q-bar")
        if bars:
            bar = bars.first(ProgressBar)
            bar.gradient = Gradient.from_colors(
                self.palette.ramp[0], self.palette.ramp[len(self.palette.ramp) // 2],
                self.palette.ramp[-1],
            )
            bar.refresh()
        if self.is_running and self.active_pane == "queue":
            self.queue.refresh_queue()

    def set_context(self, text: str) -> None:
        self._context_text = text
        self.query_one("#context", Static).update(Text(text, overflow="ellipsis", no_wrap=True))

    def refresh_context(self) -> None:
        """Re-read the active pane's own line into the shared top-bar slot.

        One slot serves three panes.  Left to itself it keeps whatever was
        written last from anywhere in the app, so the queue sat under a browse
        crumb, a reset search left "search" hanging over a browse listing, and
        a finished recursive walk pinned its progress line there for good.
        """
        pane = self.maybe_pane(self.active_pane)
        if pane is not None:
            self.set_context(pane.context_line())

    def refresh_topbar(self) -> None:
        p = self.dl.progress()
        stats = Text()
        pane = self.maybe_pane(self.active_pane)
        table = pane.maybe_table() if pane else None
        marks = len(table.marked) if table else 0
        if marks:
            stats.append(f"{marks} marked  ", style="bold bright_yellow")
        if p.active:
            stats.append(f"↓{p.active} ", style="bold bright_green")
            stats.append(f"{rate(p.speed)} ", style="bright_green")
        if p.queued:
            stats.append(f"·{p.queued} ", style="dim")
        if p.failed:
            stats.append(f"!{p.failed} ", style="bold red")
        if self.dl.paused_all:
            stats.append("PAUSED", style="bold yellow")
        self.query_one("#stats", Static).update(stats)

    def _queue_dirty(self) -> None:
        self._dirty = True

    def _tick(self) -> None:
        if not self.is_running:
            return
        self.refresh_topbar()
        pane = self.maybe_pane(self.active_pane)
        if pane is not None:
            pane.reflow_if_resized()
        # The phase only advances while a transfer is live, so an idle queue
        # costs nothing and a keypress is never waiting on an animation frame.
        if self.dl.progress().active:
            self.bar_phase += 1
        if self._dirty:
            self._dirty = False
            if self.active_pane == "queue":
                self.queue.refresh_queue()
        elif self.active_pane == "queue" and self.dl.progress().active:
            self.queue.refresh_queue()

    # -- detail panel ---------------------------------------------------------

    def action_detail(self) -> None:
        panel = self.query_one("#detail")
        panel.toggle_class("-visible")
        self.sync_detail()  # the reflow happens on the next tick

    def sync_detail(self) -> None:
        panel = self.query_one("#detail")
        if not panel.has_class("-visible"):
            return
        body = self.query_one("#detail-body", Static)
        pane = self.maybe_pane(self.active_pane)
        row = pane.current() if pane else None
        if row is None:
            body.update(Text("nothing selected", style="dim"))
            return
        if row.task_id:
            body.update(self._task_detail(self.dl.tasks.get(row.task_id)))
        elif row.entry:
            body.update(self._entry_detail(row.entry))
        else:
            body.update(Text(row.search_text, style="dim"))

    def _entry_detail(self, e: Entry) -> Text:
        out = Text()
        out.append(f"{e.name}\n\n", style="bold #ffb454")

        def row(label: str, value: Any) -> None:
            if value in (None, "", 0):
                return
            out.append(f"{label:>10}  ", style="dim")
            out.append(f"{value}\n")

        row("family", e.family)
        row("format", e.format_name)
        row("size", f"{human_size(e.size)} ({e.size:,} B)" if e.size else "")
        row("date", e.date)
        row("contains", f"{e.sub_count:,} files" if e.sub_count else "")
        row("item", e.item_name or self.items.get(e.itemid, ("", ""))[0])
        row("path", e.fileid)
        ia = self.items.get(e.itemid, ("", ""))[1]
        row("archive", ia)
        if e.b3sum:
            row("blake3", e.b3sum[:32] + "…")
        existing = self.store.have(e.itemid, e.fileid)
        if existing:
            colour = {DONE: "green", FAILED: "red"}.get(existing.status, "yellow")
            out.append(f"\n  already {existing.status}\n", style=f"bold {colour}")
            if existing.warn:
                out.append(f"  ! {existing.warn}\n", style="yellow")
        out.append(f"\n  → {destination(self.cfg, e, self.items.get(e.itemid, ('',''))[0])}\n",
                   style="dim")
        out.append("\n  d queue · o browser · i full details\n", style="dim italic")
        return out

    def _task_detail(self, task: Task | None) -> Text:
        out = Text()
        if task is None:
            return Text("gone", style="dim")
        out.append(f"{task.name}\n\n", style="bold #ffb454")

        def row(label: str, value: Any) -> None:
            if value in (None, "", 0):
                return
            out.append(f"{label:>10}  ", style="dim")
            out.append(f"{value}\n")

        row("status", task.status)
        row("item", task.item_name or f"#{task.itemid}")
        row("path", task.fileid)
        row("source", task.source)
        row("size", human_size(task.total or task.expected_size))
        row("got", f"{task.downloaded:,} B  ({task.pct:.0f}%)")
        row("attempts", task.attempts or "")
        row("md5", task.md5)
        out.append(f"\n{task.dest}\n", style="dim")
        if task.warn:
            out.append(f"\n! {task.warn}\n", style="bold yellow")
        if task.error:
            out.append(f"\nx {task.error}\n", style="bold red")
        return out

    def inspect(self, entry: Entry) -> None:
        self._inspect(entry)

    @work(group="inspect")
    async def _inspect(self, entry: Entry) -> None:
        preview = None
        if entry.family in ("text", "document", "other", "unknown") and (entry.size or 0) < 4_000_000:
            try:
                preview = await self.dm.peek(entry, 4096)
            except httpx.HTTPError:
                preview = None
        self.push_screen(InspectScreen(entry, preview))

    # -- modals ---------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_settings(self) -> None:
        def applied(cfg: Config | None) -> None:
            if cfg:
                self.cfg = cfg
                self.dl.cfg = cfg
                self.dl.set_concurrency(cfg.concurrency)
                self.notify("settings saved")
                if self.active_pane == "queue":
                    self.queue.refresh_queue()

        self.push_screen(SettingsScreen(self.cfg), applied)

    # -- queueing --------------------------------------------------------------

    def remember_item(self, itemid: int, title: str, archive_org: str) -> None:
        old = self.items.get(itemid, ("", ""))
        self.items[itemid] = (title or old[0], archive_org or old[1])

    def queue_entries(self, entries: list[Entry], names: dict[int, str]) -> None:
        self._queue_entries(entries, names)

    @work(group="queue")
    async def _queue_entries(self, entries: list[Entry], names: dict[int, str]) -> None:
        names = {**{k: v[0] for k, v in self.items.items() if v[0]}, **names}
        total = sum(e.size or 0 for e in entries)
        files = [e for e in entries if e.downloadable]

        if total > self.cfg.confirm_over_bytes or len(files) > 100:
            ok = await self.push_screen_wait(ConfirmScreen(
                "Queue these downloads?",
                f"{len(files)} file(s), about {human_size(total)}.\n\n"
                f"They will land in {self.cfg.download_dir}.",
                ok_label="Queue",
            ))
            if not ok:
                return

        ia_index = await self._ia_index(files)
        added, dupes, skipped = await self.dl.enqueue(files, names, ia_index)
        bits = [f"queued {added}"]
        if dupes:
            bits.append(f"{dupes} already in the queue")
        if skipped:
            bits.append(f"{skipped} folders skipped")
        self.notify(" · ".join(bits), severity="information" if added else "warning")
        for pane in self.PANES:
            self.pane_widget(pane).table.clear_marks()
        self.pane_widget(self.active_pane).render_rows(keep_cursor=True)
        if self.active_pane == "queue":
            self.queue.refresh_queue()

    async def _ia_index(self, entries: list[Entry]) -> dict[int, tuple[str, dict[str, Any]]]:
        """Look up archive.org metadata for items with top-level files queued."""
        if not self.cfg.prefer_archive_org:
            return {}
        wanted = {e.itemid for e in entries if "/" not in e.fileid}
        index: dict[int, tuple[str, dict[str, Any]]] = {}
        for itemid in wanted:
            identifier = self.items.get(itemid, ("", ""))[1]
            if not identifier:
                identifier = await self.dm.archive_org_id(itemid)
                self.remember_item(itemid, "", identifier)
            if identifier:
                index[itemid] = (identifier, await self.dm.ia_files(identifier))
        return index

    def queue_discs(self, discs: list[tuple[int, str]]) -> None:
        self._queue_discs(discs)

    @work(group="queue")
    async def _queue_discs(self, discs: list[tuple[int, str]]) -> None:
        """Queue the top-level files of whole items -- the disc images themselves.

        Only the item's own files, not everything extracted out of them: one ISO
        rather than its four thousand members.
        """
        entries: list[Entry] = []
        names: dict[int, str] = {}
        for itemid, label in discs:
            names[itemid] = label
            try:
                listing = await self.dm.browse(itemid, "")
            except httpx.HTTPError as exc:
                self.notify(f"could not read {label}: {exc}", severity="error")
                continue
            self.remember_item(itemid, listing.title, listing.archive_org)
            entries.extend(e for e in listing.entries if e.downloadable)
        if not entries:
            self.notify("nothing downloadable on those discs", severity="warning")
            return
        self.queue_entries(entries, names)

    def queue_recursive(self, itemid: int, path: str, label: str,
                        names: dict[int, str]) -> None:
        self._queue_recursive(itemid, path, label, names)

    @work(group="queue")
    async def _queue_recursive(self, itemid: int, path: str, label: str,
                               names: dict[int, str]) -> None:
        self.notify(f"walking {label or 'item'}…")
        found: list[Entry] = []
        stack = [path]
        seen: set[str] = set()
        limit = self.cfg.recurse_limit
        while stack and len(found) < limit:
            here = stack.pop(0)
            if here in seen:
                continue
            seen.add(here)
            try:
                listing = await self.dm.browse(itemid, here)
            except httpx.HTTPError:
                continue
            for entry in listing.entries:
                if entry.is_dir:
                    stack.append(entry.fileid)
                elif entry.downloadable:
                    found.append(entry)
            if self.active_pane == "browse":
                self.set_context(f"walking… {len(found)} files, {len(stack)} folders left")
        self.refresh_context()

        if not found:
            self.notify("nothing to download in there", severity="warning")
            return
        total = sum(e.size or 0 for e in found)
        capped = " (stopped at the limit)" if len(found) >= limit else ""
        ok = await self.push_screen_wait(ConfirmScreen(
            f"Queue everything under {label or 'this item'}?",
            f"{len(found)} file(s), about {human_size(total)}{capped}.\n\n"
            f"Archives are queued whole — their contents are not expanded again.\n"
            f"Destination: {self.cfg.download_dir}",
            ok_label="Queue them",
        ))
        if not ok:
            return
        ia_index = await self._ia_index(found)
        added, dupes, _ = await self.dl.enqueue(found, {**names, itemid: label}, ia_index)
        self.notify(f"queued {added}" + (f", {dupes} already there" if dupes else ""))
        self.show_pane("queue")

    def queue_bundle(self, filters: dict[str, Any], total: int | None) -> None:
        self._queue_bundle(filters, total)

    @work(group="queue")
    async def _queue_bundle(self, filters: dict[str, Any], total: int | None) -> None:
        label = str(filters.get("q") or "search") or "search"
        ok = await self.push_screen_wait(ConfirmScreen(
            "Download all matches as one bundle?",
            f"discmaster will build a .tar.gz of every original matching "
            f"{'this search' if total is None else f'{total:,} matches'}.\n\n"
            "The server caps a bundle at 1 GiB — anything beyond that is left out.",
            ok_label="Build it",
        ))
        if not ok:
            return
        url = self.dm.bundle_url(filters)
        dest = Path(self.cfg.download_dir) / "bundles" / f"{safe_component(label)}.tar.gz"
        stem, n = dest, 1
        while dest.exists():
            dest = stem.with_name(f"{stem.stem}-{n}.tar.gz")
            n += 1
        task = Task(
            itemid=0, fileid=f"bundle:{url}", name=dest.name,
            item_name="search bundle", dest=str(dest), url=url, source="discmaster",
        )
        _, created = self.dl.enqueue_task(task)
        self.notify("bundle queued — the server needs a moment to build it"
                    if created else "that bundle is already queued")
        self.show_pane("queue")


def run(start: str = "browse", query: str = "",
        goto: tuple[int, str] | None = None) -> None:
    DiscDig(start=start, query=query, goto=goto).run()
