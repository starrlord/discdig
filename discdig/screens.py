"""Modal overlays: help, settings, confirmation, and the file-inspector."""

from __future__ import annotations

import json
import webbrowser
from typing import Any, Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from .api import Entry, human_size
from .store import Config

# ------------------------------------------------------------------ help

HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Everywhere", [
        ("1 2 3", "Browse / Search / Queue"),
        ("] [", "next / previous pane"),
        ("i", "toggle the detail panel"),
        ("?", "this help"),
        (",", "settings"),
        ("ctrl+p", "command palette — themes (remembered), all commands"),
        ("q", "quit"),
    ]),
    ("Moving around", [
        ("up down / j k", "move the cursor"),
        ("pgup pgdn", "page"),
        ("home end / g G", "first / last row"),
        ("enter / l", "open — descend, or jump to a search hit's folder"),
        ("backspace / h", "go up one level"),
        ("", "  (backing out of a search cross-link returns to your results)"),
        ("ctrl+n", "start over in this pane — browse climbs back to the root"),
        ("/", "filter the rows on screen"),
        ("s", "sort by the next column (click a header does the same)"),
        ("S", "reverse the sort"),
    ]),
    ("Choosing things", [
        ("space", "mark / unmark the row"),
        ("ctrl+a", "mark everything listed"),
        ("escape", "clear marks and filter"),
    ]),
    ("Downloading", [
        ("d", "queue marked rows (or the row under the cursor)"),
        ("a", "queue every file in this listing"),
        ("R", "queue this folder recursively"),
        ("B", "fetch all search matches as one .tar.gz bundle"),
        ("o", "open the page in your web browser"),
    ]),
    ("Search pane", [
        ("/", "jump to the query box (ctrl+f works too)"),
        ("enter", "run the search, then land on the results"),
        ("escape", "leave the query box, back to the results"),
        ("m / ctrl+t", "find FILES, or find whole DISCS — the switch in the "
                       "search bar shows which"),
        ("", "  (ctrl+t works even while you are typing a query)"),
        ("ctrl+n", "start over — clears the query, every filter and the sort"),
        ("f", "show / hide the filter fields"),
        ("\\", "filter the results already on screen"),
        ("n / p", "next / previous page"),
        ("s", "cycle the sort order"),
    ]),
    ("Queue pane", [
        ("p", "pause / resume the selected download"),
        ("P", "pause / resume everything"),
        ("x", "cancel and remove"),
        ("r", "retry the failed ones"),
        ("C", "clear finished rows"),
        ("+ / -", "more / fewer simultaneous downloads"),
        ("O", "open the download folder"),
    ]),
]


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,q,question_mark", "dismiss", "close", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("discdig — keys", classes="heading")
            with VerticalScroll(id="help-body"):
                yield Static(self._body())
            yield Static("[dim]esc to close[/]")

    @staticmethod
    def _body() -> Text:
        out = Text()
        for title, rows in HELP_SECTIONS:
            out.append(f"{title}\n", style="bold cyan")
            for keys, what in rows:
                out.append(f"  {keys:<16}", style="bold yellow")
                out.append(f"{what}\n", style="none")
            out.append("\n")
        out.append("Downloads resume where they left off, and the queue survives a restart.\n",
                   style="dim italic")
        return out


# ------------------------------------------------------------------ confirm


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape,n", "no", "cancel"),
        Binding("y,enter", "yes", "confirm"),
    ]

    def __init__(self, title: str, message: str, ok_label: str = "Do it") -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.ok_label = ok_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="heading")
            yield Static(self.message)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", variant="default", id="no")
                yield Button(self.ok_label, variant="primary", id="yes")

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------ settings


class SettingsScreen(ModalScreen[Config | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.cfg = config

    def compose(self) -> ComposeResult:
        cfg = self.cfg
        with Vertical(classes="dialog", id="settings-form"):
            yield Static("Settings", classes="heading")
            with Horizontal(classes="row"):
                yield Label("download to")
                yield Input(cfg.download_dir, id="download_dir")
            with Horizontal(classes="row"):
                yield Label("folder layout")
                yield Select(
                    [("<item title>/<path inside>", "item"),
                     ("<itemid>/<path inside>", "full"),
                     ("filename only", "flat")],
                    value=cfg.layout, id="layout", allow_blank=False,
                )
            with Horizontal(classes="row"):
                yield Label("at once")
                yield Select(
                    [(str(n), n) for n in range(1, 9)],
                    value=cfg.concurrency, id="concurrency", allow_blank=False,
                )
            with Horizontal(classes="row"):
                yield Label("safe search")
                yield Select(
                    [("off — everything", "2"),
                     ("moderate — no adult", "1"),
                     ("strict — nothing racy", "0")],
                    value=cfg.safe_search, id="safe_search", allow_blank=False,
                )
            yield Checkbox("Prefer archive.org for whole-disc files (faster, has checksums)",
                           cfg.prefer_archive_org, id="prefer_archive_org")
            yield Checkbox("Verify MD5 when archive.org provides one",
                           cfg.verify, id="verify")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        cfg = self.cfg
        cfg.download_dir = self.query_one("#download_dir", Input).value.strip() or cfg.download_dir
        cfg.layout = str(self.query_one("#layout", Select).value)
        cfg.concurrency = int(self.query_one("#concurrency", Select).value)  # type: ignore[arg-type]
        cfg.safe_search = str(self.query_one("#safe_search", Select).value)
        cfg.prefer_archive_org = self.query_one("#prefer_archive_org", Checkbox).value
        cfg.verify = self.query_one("#verify", Checkbox).value
        cfg.save()
        self.dismiss(cfg)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------------ inspector


class InspectScreen(ModalScreen[None]):
    """Everything discmaster knows about one file, including detector output."""

    BINDINGS = [
        Binding("escape,i,q", "dismiss", "close"),
        Binding("o", "open_web", "web"),
    ]

    def __init__(self, entry: Entry, preview: bytes | None = None) -> None:
        super().__init__()
        self.entry = entry
        self.preview = preview

    def action_open_web(self) -> None:
        """Open this file's page on discmaster.

        The footer has promised this since the screen was written, but the
        binding was never added, so the key did nothing: a modal screen does
        not fall through to the browse pane's own ``o`` underneath it.  The
        screen stays open, matching the footer -- ``o`` is something you do
        *while* reading the details, not instead of.
        """
        e = self.entry
        webbrowser.open(e.browse_url() if e.is_container else e.view_url())
        self.app.notify("opened in your browser")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.entry.name, classes="heading")
            with VerticalScroll(id="help-body"):
                yield Static(self._body())
            yield Static("[dim]esc to close · o opens it in your browser[/]")

    def _body(self) -> Text:
        e = self.entry
        out = Text()

        def row(label: str, value: Any) -> None:
            if value in (None, "", []):
                return
            out.append(f"{label:>12}  ", style="dim")
            out.append(f"{value}\n")

        row("item", f"{e.item_name or ''} (#{e.itemid})".strip())
        row("path", e.fileid)
        row("family", e.family)
        row("format", e.format_name)
        row("size", f"{human_size(e.size)}  ({e.size:,} bytes)" if e.size else None)
        row("date", e.date)
        row("contains", f"{e.sub_count:,} files" if e.sub_count else None)
        row("blake3", e.b3sum)

        dexid = e.extra.get("dexid") or {}
        if dexid:
            out.append("\ndexvert\n", style="bold cyan")
            row("magic", dexid.get("magic"))
            row("confidence", dexid.get("confidence"))
            row("ext match", dexid.get("extMatch"))
            row("website", dexid.get("website"))

        ids = e.extra.get("ids") or []
        if ids:
            out.append("\nidentified by\n", style="bold cyan")
            for ident in ids[:12]:
                src = ident.get("from", "?")
                magic = str(ident.get("magic", ""))[:70]
                out.append(f"{src:>12}  ", style="dim")
                out.append(f"{magic}\n")

        meta = e.extra.get("meta") or {}
        if meta:
            out.append("\nmetadata\n", style="bold cyan")
            for k, v in list(meta.items())[:20]:
                row(k[:12], str(v)[:70])

        if self.preview is not None:
            out.append("\nfirst bytes\n", style="bold cyan")
            text = self.preview.decode("utf-8", "replace")
            printable = "".join(c if c.isprintable() or c in "\n\t" else "." for c in text)
            out.append(printable[:1600], style="none")

        return out
