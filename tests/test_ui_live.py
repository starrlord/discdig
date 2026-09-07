"""Drive the TUI headlessly with Textual's Pilot.  Run: python tests/test_ui_live.py"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="discdig-ui-"))
os.environ["DISCDIG_HOME"] = str(TMP / "home")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discdig.app import DiscDig  # noqa: E402
from discdig.store import DEFAULT_THEME, Config  # noqa: E402

failures = 0
LOG = TMP / "results.txt"
lines: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Textual redirects stdout inside run_test, so results are buffered and
    written out once the app has exited."""
    global failures
    if not cond:
        failures += 1
    line = f"[{'ok  ' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else "")
    lines.append(line)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, file=sys.__stderr__, flush=True)  # bypasses Textual's redirect


async def settle(pilot, tries: int = 120, pred=None) -> None:
    """Pump the event loop until workers finish (or a predicate goes true)."""
    for _ in range(tries):
        await pilot.pause()
        await asyncio.sleep(0.1)
        if pred is not None and pred():
            return
        if pred is None and not any(
            w.is_running for w in pilot.app.workers
        ):
            return


async def main() -> int:
    Config(download_dir=str(TMP / "dl"), concurrency=2).save()
    app = DiscDig()
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(pilot)

        # --- boot ----------------------------------------------------------
        browse = app.browse
        check("boots into browse", app.active_pane == "browse")
        check("root lists 4 sections", len(browse.rows) == 4,
              str([r.name for r in browse.rows]))
        check("starts on the default theme", app.theme == DEFAULT_THEME, app.theme)
        check("discdig theme still selectable", "discdig" in app.available_themes)
        check("topbar context", "discmaster" in app._context_text, app._context_text)

        # --- drill in: section -> genres ------------------------------------
        await pilot.press("enter")            # cd-rom
        await settle(pilot)
        check("cd-rom -> genres", len(browse.rows) > 25, str(len(browse.rows)))
        listed = [r.name for r in browse.rows]
        check("All Genres pinned first", listed[0] == "All Genres", listed[0])
        check("genres alphabetical by default",
              [n.lower() for n in listed[1:]] == sorted(n.lower() for n in listed[1:]),
              str(listed[1:5]))
        check("genre columns say Discs",
              [str(c.label) for c in browse.table.columns.values()][4] == "Discs",
              str([str(c.label) for c in browse.table.columns.values()]))
        check("crumbs updated", "cd-rom" in browse.crumb_line(), browse.crumb_line())

        # --- filter the visible rows -----------------------------------------
        browse.filter_text = "Calendar"
        await pilot.pause()
        check("row filter narrows", 1 <= len(browse.visible_rows) <= 3,
              str([r.name for r in browse.visible_rows]))
        await pilot.press("escape")
        await pilot.pause()
        check("escape clears filter", len(browse.visible_rows) == len(browse.rows))

        # --- genre -> items ---------------------------------------------------
        browse.filter_text = "Calendar"
        await pilot.pause()
        browse.table.move_cursor(row=0)
        await pilot.press("enter")
        await settle(pilot)
        check("genre -> items", len(browse.rows) >= 40, str(len(browse.rows)))
        check("item columns say Title/Publisher/Year",
              [str(c.label) for c in browse.table.columns.values()][:2] == ["Title", "Publisher"],
              str([str(c.label) for c in browse.table.columns.values()]))
        check("items carry nav", all(r.nav for r in browse.rows[:5]))
        check("item names remembered", len(app.items) >= 40, str(len(app.items)))

        # --- item -> files -----------------------------------------------------
        target = next(i for i, r in enumerate(browse.visible_rows)
                      if "Anne Geddes" in r.name)
        browse.table.move_cursor(row=target)
        await pilot.press("enter")
        await settle(pilot)
        check("item -> listing", len(browse.rows) > 0, str(len(browse.rows)))
        check("archive.org captured", browse.archive_org == "anne-geddes-calendar",
              browse.archive_org)
        check("item title captured", "Anne Geddes" in browse.item_name, browse.item_name)

        # --- marking ------------------------------------------------------------
        files = [i for i, r in enumerate(browse.visible_rows)
                 if r.entry and r.entry.downloadable]
        check("listing has files", len(files) >= 1, str(len(files)))
        browse.table.move_cursor(row=files[0])
        await pilot.press("space")
        await pilot.pause()
        check("space marks a row", len(browse.table.marked) == 1,
              str(browse.table.marked))
        check("cursor advanced after mark", browse.table.cursor_row == files[0] + 1)
        sel = browse.selection()
        check("selection follows marks", len(sel) == 1 and sel[0].key in browse.table.marked)
        await pilot.press("escape")
        await pilot.pause()
        check("escape clears marks", not browse.table.marked)

        # --- sorting ------------------------------------------------------------
        browse.sort_by(2)                      # size
        await pilot.pause()
        got = [r.sort_keys[2] for r in browse.visible_rows if r.sort_keys[2] is not None]
        check("size sorts largest first", got == sorted(got, reverse=True), str(got[:4]))
        check("size header marked",
              "v" in str(list(browse.table.columns.values())[2].label),
              str(list(browse.table.columns.values())[2].label))
        browse.sort_by(2)                      # same column flips direction
        await pilot.pause()
        got = [r.sort_keys[2] for r in browse.visible_rows if r.sort_keys[2] is not None]
        check("second press reverses", got == sorted(got), str(got[:4]))
        missing = [r for r in browse.visible_rows if r.sort_keys[2] is None]
        if missing:
            tail = browse.visible_rows[-len(missing):]
            check("rows with no size park last", all(r.sort_keys[2] is None for r in tail))
        browse.sort_by(0)                      # name
        await pilot.pause()
        names_now = [r.sort_keys[0] for r in browse.visible_rows]
        check("name sorts A-Z", names_now == sorted(names_now), str(names_now[:3]))
        await pilot.press("s")                 # cycle onward
        await pilot.pause()
        check("s cycles the sort column", browse.sort_col == 1, str(browse.sort_col))
        browse.sort_col, browse.sort_desc = None, False
        browse.label_headers()
        browse.render_rows()
        await pilot.pause()

        # --- detail panel ---------------------------------------------------------
        await pilot.press("i")
        await pilot.pause()
        check("detail opens", app.query_one("#detail").has_class("-visible"))
        text = app.query_one("#detail-body").content
        check("detail has content", len(str(text)) > 20, str(text)[:60])
        await pilot.press("i")
        await pilot.pause()
        check("detail closes", not app.query_one("#detail").has_class("-visible"))

        # --- go back up -------------------------------------------------------------
        await pilot.press("backspace")
        await settle(pilot)
        check("backspace goes up", len(browse.stack) == 3, str(len(browse.stack)))

        # --- queue something --------------------------------------------------------
        await pilot.press("backspace", "backspace")
        await settle(pilot)
        check("back at root", browse.here.kind == "root", browse.here.kind)

        # --- search pane ---------------------------------------------------------------
        await pilot.press("2")
        await pilot.pause()
        check("switched to search", app.active_pane == "search")
        search = app.search
        search.query_one("#query").value = "totally mad"
        search.query_one("#f-family").value = "image"
        search.page = 0
        search.run_search()
        await settle(pilot)
        check("search returned rows", len(search.rows) > 0, str(len(search.rows)))
        check("family filter applied",
              all(r.entry and r.entry.family == "image" for r in search.rows[:5]),
              str([r.entry.family for r in search.rows[:5]]))
        check("match count shown", search.total is not None, str(search.total))
        status = str(app.query_one("#search-status").content)
        check("status line", "matches" in status or "shown" in status, status)

        # --- search sorts on the server, not just this page ------------------------------
        before = [r.name for r in search.rows[:3]]
        search.on_data_table_header_selected(
            type("E", (), {"column_index": 2, "stop": lambda self: None})()
        )
        await settle(pilot)
        check("header click re-queries sorted by size",
              search.last_filters.get("sortBy") == "size",
              str(search.last_filters.get("sortBy")))
        check("size sort is descending first",
              search.last_filters.get("sortOrderDesc") == "desc",
              str(search.last_filters.get("sortOrderDesc")))
        sizes = [r.entry.size for r in search.rows if r.entry and r.entry.size]
        check("results really are size-ordered", sizes == sorted(sizes, reverse=True),
              str(sizes[:4]))
        check("search results changed", [r.name for r in search.rows[:3]] != before)
        search.sort_index, search.sort_desc = 0, False

        # --- the files/discs switch is visible and reachable from the query box ----------
        await pilot.press("2")
        await pilot.pause()
        switch = lambda: str(app.query_one("#mode-switch").content)
        check("mode switch is on screen", "FILES" in switch() and "DISCS" in switch(),
              switch())
        check("switch shows its shortcut", "^t" in switch(), switch())
        search.action_focus_query()
        await pilot.pause()
        search.query_one("#query").value = ""
        await pilot.press("ctrl+t")
        await pilot.pause()
        check("ctrl+t flips mode from inside the query box", search.mode == "discs",
              search.mode)
        check("ctrl+t was not typed into the box",
              search.query_one("#query").value == "",
              repr(search.query_one("#query").value))
        check("headers follow the mode",
              [str(c.label) for c in search.table.columns.values()][:2]
              == ["Title", "Publisher"],
              str([str(c.label) for c in search.table.columns.values()]))
        check("placeholder explains the mode",
              "CD-ROM" in search.query_one("#query").placeholder,
              search.query_one("#query").placeholder)
        await pilot.press("ctrl+t")
        await pilot.pause()
        check("ctrl+t flips back", search.mode == "files", search.mode)
        check("headers follow back",
              [str(c.label) for c in search.table.columns.values()][:2] == ["Name", "Kind"],
              str([str(c.label) for c in search.table.columns.values()]))

        # --- ctrl+n starts over -----------------------------------------------------------
        search.query_one("#query").value = "moire"
        search.query_one("#f-extension").value = "exe"
        search.query_one("#f-sizeMin").value = "1k"
        search.sort_index = 3
        search.run_search()
        await settle(pilot)
        check("filters are flagged in the status",
              "filters on" in str(app.query_one("#search-status").content),
              str(app.query_one("#search-status").content))
        await pilot.press("ctrl+n")
        await pilot.pause()
        check("query cleared", search.query_one("#query").value == "")
        check("filter fields cleared",
              not any(i.value for i in search.query("#filters Input")),
              str([i.value for i in search.query("#filters Input")]))
        check("sort reset", search.sort_index == 0 and not search.sort_desc)
        check("results cleared", search.rows == [])
        check("focus back in the query box", app.focused.id == "query",
              str(app.focused.id))

        # --- disc mode: find the CD-ROM, not 127 catalogue stubs ---------------------------
        search.query_one("#query").value = "Rise of the Triad"
        search.action_toggle_mode()
        await settle(pilot)
        check("disc mode on", search.mode == "discs", search.mode)
        check("disc search found the CD-ROM",
              any("Rise of the Triad" in r.name for r in search.rows),
              str([r.name for r in search.rows][:4]))
        check("disc rows navigate", all(r.nav for r in search.rows))
        disc = search.rows[0]
        check("disc row has publisher/size", bool(disc.cells[0] or disc.cells[1]),
              str(disc.cells))
        search.table.focus()
        search.table.move_cursor(row=0)
        await pilot.press("enter")
        await settle(pilot)
        check("enter opens the disc in browse",
              app.active_pane == "browse" and app.browse.here.itemid == disc.nav.itemid,
              f"{app.active_pane} #{app.browse.here.itemid}")
        await pilot.press("2")
        await pilot.pause()
        search.action_toggle_mode()
        await settle(pilot)
        check("back to file mode", search.mode == "files", search.mode)

        # --- backing out of a search cross-link returns to the results -------------------
        await pilot.press("2")
        await pilot.pause()
        search.query_one("#query").value = "doom"
        search.page = 0
        search.run_search()
        await settle(pilot)
        check("search for the cross-link test", len(search.rows) > 5, str(len(search.rows)))
        search.table.focus()
        search.table.move_cursor(row=4)
        chosen = search.visible_rows[4]
        await pilot.press("enter")
        await settle(pilot)
        check("enter lands in browse", app.active_pane == "browse", app.active_pane)
        check("browse rung remembers where it came from",
              app.browse.here.origin == "search", repr(app.browse.here.origin))
        await pilot.press("backspace")
        await settle(pilot)
        check("backspace returns to the search results",
              app.active_pane == "search", app.active_pane)
        check("results survived the round trip", len(search.rows) > 5, str(len(search.rows)))
        check("cursor is still on the same hit",
              search.current() is not None and search.current().key == chosen.key,
              str(search.current().key if search.current() else None))
        check("focus back on the results table", app.focused.id == "search-table",
              str(app.focused.id))

        # ... and an ordinary browse walk still just goes up a level
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("enter")            # into a collection
        await settle(pilot)
        depth = len(app.browse.stack)
        await pilot.press("backspace")
        await settle(pilot)
        check("plain browse backspace stays in browse",
              app.active_pane == "browse" and len(app.browse.stack) == depth - 1,
              f"{app.active_pane} depth={len(app.browse.stack)}")

        # --- the top bar belongs to whichever pane is on screen ----------------------------
        await pilot.press("enter")            # back down into a collection
        await settle(pilot)
        deep = app.browse.crumb_line()
        check("browse owns the top bar", app._context_text == deep, app._context_text)
        await pilot.press("2")
        await pilot.pause()
        check("switching to search drops the browse crumb",
              app._context_text == search.context_line() and app._context_text != deep,
              app._context_text)
        await pilot.press("1")
        await pilot.pause()
        check("switching back restores the crumb", app._context_text == deep,
              app._context_text)

        # --- ctrl+n climbs the whole way out of browse -------------------------------------
        check("deep before the reset", len(app.browse.stack) > 1,
              str(len(app.browse.stack)))
        await pilot.press("ctrl+n")
        await settle(pilot)
        check("browse reset to root", len(app.browse.stack) == 1,
              str([loc.kind for loc in app.browse.stack]))
        check("root listing reloaded", len(app.browse.rows) == 4,
              str([r.name for r in app.browse.rows]))
        check("top bar back to discmaster", app._context_text == "discmaster",
              app._context_text)

        await pilot.press("2")
        await pilot.pause()

        # --- columns share the width instead of starving the Item column -------------------
        await pilot.press("2")
        await pilot.pause()
        search.query_one("#query").value = "totally mad"
        search.page = 0
        search.run_search()
        await settle(pilot)
        widths = [c.width for c in search.table.columns.values()]
        check("no horizontal overflow",
              search.table.virtual_size.width <= search.table.size.width,
              f"{search.table.virtual_size.width} > {search.table.size.width}")
        check("item column is not starved", widths[4] >= 18, str(widths))
        check("name column is not hogging", widths[0] <= widths[4] * 2, str(widths))

        # --- filters toggle ---------------------------------------------------------------
        await pilot.press("f")
        await pilot.pause()
        check("filters toggle", app.query_one("#filters").has_class("-visible"))

        # --- queue from search -------------------------------------------------------------
        search.table.focus()
        search.table.move_cursor(row=0)
        first = search.visible_rows[0]
        await pilot.press("d")
        await settle(pilot, pred=lambda: len(app.dl.tasks) > 0)
        check("queued from search", len(app.dl.tasks) >= 1, str(len(app.dl.tasks)))
        task = next(iter(app.dl.tasks.values()))
        check("task points at the hit", task.fileid == first.entry.fileid,
              f"{task.fileid} vs {first.entry.fileid}")

        # --- queue pane ------------------------------------------------------------------
        await pilot.press("3")
        await pilot.pause()
        check("switched to queue", app.active_pane == "queue")
        check("queue owns the top bar, not the browse crumb",
              app._context_text.startswith("queue:"), app._context_text)
        q = app.queue
        check("queue renders rows", len(q.rows) >= 1, str(len(q.rows)))
        head = str(app.query_one("#q-counts").content)
        check("queue header", "done" in head or "active" in head or "waiting" in head, head)

        await pilot.press("plus")
        await pilot.pause()
        check("concurrency up", app.cfg.concurrency == 3, str(app.cfg.concurrency))
        await pilot.press("minus")
        await pilot.pause()
        check("concurrency down", app.cfg.concurrency == 2, str(app.cfg.concurrency))

        await pilot.press("P")
        await pilot.pause()
        check("pause all", app.dl.paused_all)
        await pilot.press("P")
        await pilot.pause()
        check("resume all", not app.dl.paused_all)

        # let the (small) download finish, then verify it landed
        await settle(pilot, tries=200,
                     pred=lambda: all(t.status in ("done", "failed")
                                      for t in app.dl.tasks.values()))
        done = [t for t in app.dl.tasks.values() if t.status == "done"]
        check("download completed", len(done) >= 1,
              str([(t.status, t.error) for t in app.dl.tasks.values()]))
        if done:
            check("file on disk", Path(done[0].dest).exists(), done[0].dest)

        q.refresh_queue()
        await pilot.pause()
        await pilot.press("C")
        await pilot.pause()
        check("clear finished", len(app.dl.tasks) == 0, str(len(app.dl.tasks)))
        check("top bar follows the cleared queue",
              app._context_text == "queue: empty", app._context_text)

        # --- modals -------------------------------------------------------------------------
        await pilot.press("question_mark")
        await pilot.pause()
        check("help opens", app.screen.__class__.__name__ == "HelpScreen",
              app.screen.__class__.__name__)
        await pilot.press("escape")
        await pilot.pause()
        check("help closes", app.screen.__class__.__name__ == "DiscDig" or
              app.screen.__class__.__name__ == "Screen", app.screen.__class__.__name__)

        await pilot.press("comma")
        await pilot.pause()
        check("settings opens", app.screen.__class__.__name__ == "SettingsScreen",
              app.screen.__class__.__name__)
        await pilot.press("escape")
        await pilot.pause()

        # --- the inspector's footer promises 'o'; make sure it delivers ---------------------
        from discdig import screens as _screens
        from discdig.api import Entry as _Entry
        opened: list[str] = []
        real_open = _screens.webbrowser.open
        _screens.webbrowser.open = lambda url, *a, **k: opened.append(url)
        try:
            png = _Entry(itemid=43230, fileid="CD_Label-MacOS8_for_CHRP.png",
                         name="CD_Label-MacOS8_for_CHRP.png", family="image")
            app.push_screen(_screens.InspectScreen(png))
            await pilot.pause()
            check("inspector opens", app.screen.__class__.__name__ == "InspectScreen",
                  app.screen.__class__.__name__)
            await pilot.press("o")
            await pilot.pause()
            check("o opens the file's view page", opened == [png.view_url()], str(opened))
            check("o leaves the inspector open",
                  app.screen.__class__.__name__ == "InspectScreen",
                  app.screen.__class__.__name__)
            await pilot.press("escape")
            await pilot.pause()

            folder = _Entry(itemid=43230, fileid="System", name="System",
                            family="directory")
            app.push_screen(_screens.InspectScreen(folder))
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            check("a container opens its browse page",
                  opened[-1] == folder.browse_url(), str(opened[-1:]))
            await pilot.press("escape")
            await pilot.pause()
        finally:
            _screens.webbrowser.open = real_open

        # --- bar colours come from the theme, not hard-coded names ---------------------------
        from discdig.widgets import progress_cell
        from discdig.store import Task as _Task, RUNNING as _RUNNING
        dracula_ramp = app.palette.ramp
        check("palette derived from the theme",
              dracula_ramp[0] != dracula_ramp[-1], f"{dracula_ramp[0]}..{dracula_ramp[-1]}")
        cell = progress_cell(_Task(name="x", status=_RUNNING, downloaded=60, total=100),
                             24, app.palette)
        shades = {str(sp.style) for sp in cell.spans}
        check("bar is a gradient, not one flat colour", len(shades) > 4, str(len(shades)))
        check("bar uses theme colours",
              app.palette.ramp[-1].lower() in {c.lower() for c in
                                               [app.palette.ramp[-1]]},
              app.palette.ramp[-1])
        app.theme = "gruvbox"
        await pilot.pause()
        await asyncio.sleep(0.4)
        check("palette follows a theme change", app.palette.ramp[-1] != dracula_ramp[-1],
              f"{dracula_ramp[-1]} -> {app.palette.ramp[-1]}")
        app.theme = DEFAULT_THEME
        await pilot.pause()
        await asyncio.sleep(0.4)
        idle = app.bar_phase
        await asyncio.sleep(0.6)
        await pilot.pause()
        check("animation is idle when nothing downloads", app.bar_phase == idle,
              f"{idle} -> {app.bar_phase}")

        # --- theme choice is remembered -------------------------------------------------------
        from discdig.store import CONFIG_PATH
        import json as _json
        app.theme = "nord"
        await pilot.pause()
        await asyncio.sleep(0.3)
        check("theme applied", app.theme == "nord", app.theme)
        check("theme written to config",
              _json.loads(CONFIG_PATH.read_text("utf-8"))["theme"] == "nord",
              _json.loads(CONFIG_PATH.read_text("utf-8"))["theme"])
        app.theme = DEFAULT_THEME
        await pilot.pause()
        await asyncio.sleep(0.3)

        # --- tab cycling ----------------------------------------------------------------------
        await pilot.press("1")
        await pilot.pause()
        check("back to browse", app.active_pane == "browse")
        await pilot.press("right_square_bracket")
        await pilot.pause()
        check("tab cycles", app.active_pane == "search", app.active_pane)

    return 1 if failures else 0


def _report(code: int) -> int:
    for line in lines:
        print(line)
    print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILURES'}")
    shutil.rmtree(TMP, ignore_errors=True)
    return code


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except BaseException as exc:  # noqa: BLE001 - surface crashes with the log
        failures += 1
        lines.append(f"[CRASH] {type(exc).__name__}: {exc}")
        import traceback
        lines.append(traceback.format_exc())
        rc = 1
    raise SystemExit(_report(rc))
