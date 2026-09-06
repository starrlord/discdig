"""Command line front door.

``discdig`` on its own opens the TUI.  The subcommands exist so the tool is
still useful from a script or over a pipe -- ``search`` prints hits, ``get``
downloads without ever drawing a screen, and ``queue`` drains whatever the TUI
left behind.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__
from .api import DiscMaster, Entry, human_size
from .downloader import Downloader
from .store import (
    CONFIG_PATH, DB_PATH, DONE, FAILED, PAUSED, QUEUED, RUNNING, Config, Store,
)
from .widgets import rate


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="discdig",
        description="Browse discmaster.textfiles.com and queue downloads, in your terminal.",
        epilog="Run with no arguments for the full interface.",
    )
    p.add_argument("--version", action="version", version=f"discdig {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("search", help="search, and open the results in the TUI")
    s.add_argument("query", nargs="+")
    s.add_argument("--print", action="store_true", dest="printonly",
                   help="print the hits instead of opening the interface")
    s.add_argument("-n", "--limit", type=int, default=25)
    s.add_argument("--family", default="")
    s.add_argument("--ext", default="")

    b = sub.add_parser("browse", help="open the TUI at an item or path")
    b.add_argument("target", help="itemid, or itemid/path/inside")

    g = sub.add_parser("get", help="download files without the interface")
    g.add_argument("itemid", type=int)
    g.add_argument("paths", nargs="+", help="paths inside the item")
    g.add_argument("-o", "--out", default="", help="download directory")

    q = sub.add_parser("queue", help="show the saved queue, or resume it")
    q.add_argument("--run", action="store_true", help="download everything outstanding")

    sub.add_parser("config", help="show where settings live and what they are")
    c = sub.add_parser("check", help="verify the install: files, TLS and the site")
    c.add_argument("--offline", action="store_true",
                   help="skip the checks that reach discmaster and archive.org")
    return p


def _use_utf8() -> None:
    """Filenames here are full of accents, box drawing and CJK.

    The console script bypasses the shell wrappers that set PYTHONIOENCODING, and
    a default cp1252 stdout turns "moiré" into "moir?" or raises outright.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _use_utf8()
    args = _parser().parse_args(argv)
    cmd = getattr(args, "cmd", None)

    if cmd == "config":
        cfg = Config.load()
        print(f"config file : {CONFIG_PATH}")
        for key, value in vars(cfg).items():
            print(f"{key:>18} : {value}")
        return 0

    if cmd == "check":
        return asyncio.run(_check(args.offline))

    if cmd == "queue":
        return asyncio.run(_queue(args.run))

    if cmd == "get":
        return asyncio.run(_get(args.itemid, args.paths, args.out))

    if cmd == "search" and args.printonly:
        return asyncio.run(_search(args))

    from .app import run  # imported late so the CLI paths stay fast

    if cmd == "search":
        run(start="search", query=" ".join(args.query))
    elif cmd == "browse":
        itemid, _, path = args.target.partition("/")
        if not itemid.isdigit():
            print(f"not an item id: {itemid}", file=sys.stderr)
            return 2
        run(start="browse", goto=(int(itemid), path))
    else:
        run()
    return 0


async def _check(offline: bool = False) -> int:
    """Smoke-test an installation, especially a packaged one.

    A frozen build can fail in ways the source tree never does -- a missing
    stylesheet, or TLS certificates that did not make it into the bundle -- and
    neither shows up until you try to draw a screen or fetch a file.  This
    exercises both without needing a terminal.
    """
    ok = True

    def line(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok ' if good else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    frozen = getattr(sys, "frozen", False)
    print(f"discdig {__version__}  ({'packaged build' if frozen else 'running from source'})")
    print(f"python  {sys.version.split()[0]}")

    from .app import DiscDig, asset  # imported here so `check` stays cheap elsewhere

    css = Path(DiscDig.CSS_PATH)
    line("stylesheet found", css.is_file(), str(css))
    try:
        # Parsing proves both that the file survived packaging and that
        # Textual's CSS machinery came along with it -- a missing stylesheet
        # only shows up as a blank screen at launch otherwise.
        from textual.css.stylesheet import Stylesheet

        from .app import DISCDIG_THEME

        # The sheet is written entirely in theme variables ($primary, $surface
        # …), which only exist once a theme is applied — so parse it against the
        # app's own palette rather than an empty variable table.
        sheet = Stylesheet(variables=DISCDIG_THEME.to_color_system().generate())
        sheet.read(str(css))
        sheet.parse()
        line("stylesheet parses", bool(sheet.rules), f"{len(sheet.rules)} rules")
    except Exception as exc:  # noqa: BLE001
        line("stylesheet parses", False, f"{type(exc).__name__}: {exc}")

    cfg = Config.load()
    try:
        Path(cfg.download_dir).mkdir(parents=True, exist_ok=True)
        line("download folder writable", os.access(cfg.download_dir, os.W_OK), cfg.download_dir)
    except OSError as exc:
        line("download folder writable", False, str(exc))

    try:
        store = Store()
        store.counts()
        store.close()
        line("queue database", True, str(DB_PATH))
    except Exception as exc:  # noqa: BLE001 - report, don't raise, in a check
        line("queue database", False, str(exc))

    if offline:
        print("[--] network checks skipped (--offline)")
        print("\nLocal files look right." if ok
              else "\nSomething is wrong; see the failures above.")
        return 0 if ok else 1

    dm = DiscMaster()
    try:
        page = await dm.search({"q": "doom", "limit": 1})
        line("https to discmaster", bool(page.results), f"{len(page.results)} result")
    except Exception as exc:  # noqa: BLE001
        line("https to discmaster", False, f"{type(exc).__name__}: {exc}")
    try:
        ia = await dm.ia_files("cdrom-doom2-explosion")
        line("https to archive.org", bool(ia), f"{len(ia)} files listed")
    except Exception as exc:  # noqa: BLE001
        line("https to archive.org", False, f"{type(exc).__name__}: {exc}")
    finally:
        await dm.aclose()

    print("\nAll good - run discdig with no arguments to start." if ok
          else "\nSomething is wrong; see the failures above.")
    return 0 if ok else 1


async def _search(args: argparse.Namespace) -> int:
    dm = DiscMaster()
    try:
        filters = {"q": " ".join(args.query), "limit": args.limit}
        if args.family:
            filters["family"] = args.family
        if args.ext:
            filters["extension"] = args.ext
        page = await dm.search(filters)
        total, _ = await dm.search_count(filters)
        for e in page.results:
            print(f"{e.itemid:>7}  {human_size(e.size):>7}  {e.family:<10} "
                  f"{e.fileid[:100]}")
        print(f"\n{len(page.results)} shown"
              + (f" of {total:,} matches" if total else ""), file=sys.stderr)
    finally:
        await dm.aclose()
    return 0


async def _get(itemid: int, paths: list[str], out: str) -> int:
    cfg = Config.load()
    if out:
        cfg.download_dir = out
    store = Store()
    dm = DiscMaster()
    dl = Downloader(store, cfg, dm.client)
    dl.load()
    await dl.start()
    try:
        entries = [Entry(itemid=itemid, fileid=p, name=p.rsplit("/", 1)[-1]) for p in paths]
        ia_id = await dm.archive_org_id(itemid)
        ia = {itemid: (ia_id, await dm.ia_files(ia_id))} if ia_id else {}
        listing = await dm.browse(itemid)
        names = {itemid: listing.title}
        added, dupes, _ = await dl.enqueue(entries, names, ia)
        print(f"queued {added}" + (f" ({dupes} already present)" if dupes else ""),
              file=sys.stderr)
        ids = [t.id for t in dl.tasks.values() if t.fileid in set(paths)]
        await _drain(dl, ids)
        bad = [t for t in dl.tasks.values() if t.id in ids and t.status == FAILED]
        for t in bad:
            print(f"failed: {t.name}: {t.error}", file=sys.stderr)
        return 1 if bad else 0
    finally:
        await dl.stop()
        await dm.aclose()
        store.close()


async def _queue(run_it: bool) -> int:
    cfg = Config.load()
    store = Store()
    dm = DiscMaster()
    dl = Downloader(store, cfg, dm.client)
    dl.load()
    try:
        if not dl.tasks:
            print("the queue is empty")
            return 0
        if not run_it:
            for t in sorted(dl.tasks.values(), key=lambda t: t.id):
                mark = {DONE: "+", FAILED: "x", PAUSED: "=", RUNNING: "*"}.get(t.status, "o")
                size = human_size(t.total or t.expected_size)
                print(f" {mark} {t.status:<7} {size:>7}  {t.name[:60]:<60} {t.item_name[:30]}")
                if t.error:
                    print(f"     {t.error}")
            counts = store.counts()
            print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            return 0

        await dl.start()
        todo = [t.id for t in dl.tasks.values() if t.status in (QUEUED, PAUSED, RUNNING)]
        for tid in todo:
            dl.resume(tid)
        if not todo:
            print("nothing outstanding")
            return 0
        print(f"downloading {len(todo)} file(s)…", file=sys.stderr)
        await _drain(dl, todo, report=True)
        failed = sum(1 for t in dl.tasks.values() if t.status == FAILED)
        return 1 if failed else 0
    finally:
        await dl.stop()
        await dm.aclose()
        store.close()


async def _drain(dl: Downloader, ids: list[int], report: bool = False) -> None:
    last = 0.0
    while True:
        live = [dl.tasks[i] for i in ids if i in dl.tasks]
        if not live or all(t.status in (DONE, FAILED, PAUSED) for t in live):
            return
        if report:
            p = dl.progress()
            line = (f"\r{p.active} active  {p.done} done  {p.failed} failed  "
                    f"{rate(p.speed):>10}  {p.pct:5.1f}%   ")
            sys.stderr.write(line)
            sys.stderr.flush()
        await asyncio.sleep(0.5)
        last += 0.5


if __name__ == "__main__":
    raise SystemExit(main())
