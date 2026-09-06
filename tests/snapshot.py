"""Render the app to plain text so the layout can be eyeballed without a TTY.

Textual's SVG export carries one <text> element per screen row, which is enough
to reconstruct the frame faithfully (colours are dropped, geometry is not).
Run: python tests/snapshot.py [browse|search|queue|help]
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="discdig-snap-"))
os.environ["DISCDIG_HOME"] = str(TMP / "home")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discdig.app import DiscDig  # noqa: E402
from discdig.store import Config  # noqa: E402

frames: list[tuple[str, str]] = []


def to_text(svg: str) -> str:
    rows: dict[float, list[tuple[float, str]]] = {}
    for m in re.finditer(r'<text[^>]*\bx="([\d.]+)"[^>]*\by="([\d.]+)"[^>]*>(.*?)</text>',
                         svg, re.S):
        x, y, body = float(m.group(1)), float(m.group(2)), m.group(3)
        body = html.unescape(re.sub(r"<[^>]+>", "", body))
        rows.setdefault(y, []).append((x, body))
    out = []
    for y in sorted(rows):
        line = ""
        for x, body in sorted(rows[y]):
            col = int(round(x / 8.0)) if x else 0
            line = line.ljust(col) + body
        out.append(line.rstrip())
    return "\n".join(out)


async def shot(pilot, label: str) -> None:
    await pilot.pause()
    frames.append((label, to_text(pilot.app.export_screenshot())))


async def settle(pilot, tries=100):
    for _ in range(tries):
        await pilot.pause()
        await asyncio.sleep(0.1)
        if not any(w.is_running for w in pilot.app.workers):
            return


async def main() -> None:
    Config(download_dir=str(TMP / "dl"), concurrency=2).save()
    app = DiscDig()
    async with app.run_test(size=(132, 40)) as pilot:
        await settle(pilot)
        await shot(pilot, "1. browse — the collections")

        await pilot.press("enter")          # cd-rom
        await settle(pilot)
        app.browse.filter_text = "Calendar"
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        await shot(pilot, "2. browse — items in a genre")

        app.browse.filter_text = "Beatles"
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("space")
        await pilot.press("i")
        await shot(pilot, "3. browse — inside an item, one row marked, detail open")
        await pilot.press("i")

        await pilot.press("2")
        await pilot.pause()
        await pilot.press("f")
        s = app.search
        s.query_one("#query").value = "moire"
        s.run_search()
        await settle(pilot)
        await shot(pilot, "4. search — filters open, results in")

        s.table.focus()
        await pilot.press("space", "space", "space")
        await pilot.press("d")
        await settle(pilot, 30)
        await pilot.press("3")
        await pilot.pause()
        await asyncio.sleep(1.0)
        app.queue.refresh_queue()
        await shot(pilot, "5. queue — downloads in flight")

        await pilot.press("question_mark")
        await shot(pilot, "6. help")
        await pilot.press("escape")

    for label, frame in frames:
        print("\n" + "=" * 132)
        print(label)
        print("=" * 132)
        print(frame)


if __name__ == "__main__":
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    asyncio.run(main())
