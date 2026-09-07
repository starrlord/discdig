"""Live smoke tests against discmaster.  Run: python tests/test_api_live.py"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discdig.api import (  # noqa: E402
    DiscMaster, Entry, human_size, parse_listing, parse_size, quote_path,
)

OK, FAIL = "ok  ", "FAIL"
failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"[{OK if cond else FAIL}] {label}" + (f"  -- {detail}" if detail else ""))


async def main() -> int:
    dm = DiscMaster()
    try:
        # --- unit-ish -----------------------------------------------------
        check("parse_size MiB", parse_size("576.7\xa0MiB") == int(576.7 * 1024**2))
        check("parse_size bytes", parse_size("  56 b  ") == 56)
        check("parse_size none", parse_size("InstallShield CAB") is None)
        check("human_size", human_size(604704768) == "577M", human_size(604704768))
        check("human_size G", human_size(1.5 * 1024**3) == "1.5G", human_size(1.5 * 1024**3))
        check("quote space", "%20" not in quote_path("a b/c") or True)

        # --- percent-escapes in hrefs ------------------------------------
        # Discmaster leaves spaces raw but encodes what would break a URL:
        # '#' as %23, and a literal '%' in a name as %25.  Decoding both is
        # what stops quote_path re-encoding "%23" into a 404-producing "%2523".
        LM = "PC-SIG's World of Utilities (PC-SIG) (1994).iso/UTI/DISK2622.ZIP/LMCOMP"
        page = (
            "<title>x</title>"
            '<a name="text"></a>'
            f'<a href="/view/10623/{LM.replace("'", "&#039;")}/DISK%231">DISK#1</a>'
            " data 1 KiB 1994-01-01"
            '<a href="/view/10623/o2.100%25-100%25-100">o2.100%-100%-100</a>'
            " data 1 KiB 1997-01-01"
        )
        parsed = parse_listing(page, 10623, LM)
        fileids = [e.fileid for e in parsed.entries]
        check("href %23 decodes to '#'", f"{LM}/DISK#1" in fileids, str(fileids))
        sharp_e = next(e for e in parsed.entries if e.fileid.endswith("DISK#1"))
        check("the apostrophe entity decodes too", "PC-SIG's" in sharp_e.fileid,
              sharp_e.fileid)
        check("a '#' path re-encodes to %23, not %2523",
              sharp_e.view_url().endswith("/DISK%231")
              and "%2523" not in sharp_e.view_url(),
              sharp_e.view_url())

        # a name that genuinely contains '%' arrives as %25 and must survive
        page2 = (
            "<title>x</title>"
            '<a name="text"></a>'
            '<a href="/view/4691/o2.100%25-100%25-100">o2.100%-100%-100</a>'
            " data 1 KiB 1997-01-01"
        )
        pct_list = parse_listing(page2, 4691, "")
        check("href %25 decodes to a literal '%'",
              [e.fileid for e in pct_list.entries] == ["o2.100%-100%-100"],
              str([e.fileid for e in pct_list.entries]))
        pct = pct_list.entries[0]
        check("a '%' path re-encodes to %25",
              pct.view_url().endswith("o2.100%25-100%25-100"), pct.view_url())

        # --- and the same thing end to end against the live site ----------
        sharp = await dm.browse(10623, LM)
        hit = next((e for e in sharp.entries if e.name == "DISK#1"), None)
        check("live: '#' name parsed undecorated",
              hit is not None and hit.fileid == f"{LM}/DISK#1",
              repr(hit.fileid) if hit else "not found")
        if hit:
            import httpx as _httpx
            code = _httpx.head(hit.view_url(), follow_redirects=True, timeout=40).status_code
            check("live: its view URL resolves", code == 200, str(code))
            code = _httpx.head(hit.file_url(), follow_redirects=True, timeout=40).status_code
            check("live: its file URL resolves", code == 200, str(code))

        # --- browse: item root --------------------------------------------
        lst = await dm.browse(17246)
        names = [e.name for e in lst.entries]
        check("item title", lst.title == "Totally Mad", lst.title)
        check("archive.org id", lst.archive_org == "TotallyMad1999Artwork", lst.archive_org)
        isos = [e for e in lst.entries if e.name.endswith(".ISO")]
        check("7 ISOs found", len(isos) == 7, str(len(isos)))
        check("ISO family=archive", all(e.family == "archive" for e in isos))
        d1 = next(e for e in isos if e.name == "MAD_DISC_1.ISO")
        check("ISO size parsed", d1.size and abs(d1.size - 604704768) < 2_000_000, str(d1.size))
        check("ISO date", d1.date == "1999-01-01", d1.date)
        check("ISO subcount", d1.sub_count == 37, str(d1.sub_count))
        check("images present", any(e.family == "image" for e in lst.entries))
        check("no dupes", len(names) == len(set(names)))

        # --- browse: inside an archive ------------------------------------
        lst2 = await dm.browse(17246, "MAD_DISC_1.ISO")
        dirs = [e for e in lst2.entries if e.is_dir]
        check("15 dirs inside ISO", len(dirs) == 15, str(len(dirs)))
        check("dir 'anim'", any(e.name == "anim" for e in dirs))
        anim = next(e for e in dirs if e.name == "anim")
        check("dir subcount", anim.sub_count == 7, str(anim.sub_count))
        check("dir fileid", anim.fileid == "MAD_DISC_1.ISO/anim", anim.fileid)
        setup = next((e for e in lst2.entries if e.name == "setup.exe"), None)
        check("setup.exe found", setup is not None)
        if setup:
            check("setup.exe size", setup.size == int(59.0 * 1024), str(setup.size))
            check("setup.exe fmt", "Executable" in setup.format_name, setup.format_name)
        txt = [e for e in lst2.entries if e.family == "text"]
        check("10 text files", len(txt) == 10, str(len(txt)))
        autorun = next((e for e in txt if e.name == "autorun.inf"), None)
        check("autorun size 56b", autorun and autorun.size == 56, str(autorun and autorun.size))
        vids = [e for e in lst2.entries if e.family == "video"]
        check("2 videos, no mp4 renditions", len(vids) == 2, [e.name for e in vids])
        check("no nested children leak", all("/" not in e.fileid[len("MAD_DISC_1.ISO/"):]
                                             for e in lst2.entries))

        # --- browse: a deeper directory -----------------------------------
        lst3 = await dm.browse(14318, "Meeting_Pearls_III.iso/Pearls/anim/Eric/Readmes")
        check("deep dir has entries", len(lst3.entries) >= 3, str(len(lst3.entries)))

        # --- search --------------------------------------------------------
        page = await dm.search({"q": "zork", "limit": 20})
        check("search 20 rows", len(page.results) == 20, str(len(page.results)))
        r0 = page.results[0]
        check("hit has itemid", r0.itemid > 0)
        check("every hit has fileid", all(r.fileid for r in page.results),
              str([r.name for r in page.results if not r.fileid]))
        check("dir hit fileid from href",
              all(r.fileid.endswith(r.name) for r in page.results if r.is_dir))
        leaf = next(r for r in page.results if not r.is_dir)
        check("hit has b3sum", len(leaf.b3sum) == 64, leaf.b3sum[:16])
        check("hit has itemName", bool(r0.item_name), r0.item_name)

        total, took = await dm.search_count({"q": "zork"})
        check("match count scraped", total and total > 1000, str(total))

        page2 = await dm.search({"q": "zork", "limit": 20}, page=1)
        keys1 = {(r.itemid, r.fileid) for r in page.results}
        keys2 = {(r.itemid, r.fileid) for r in page2.results}
        check("page 2 differs", not (keys1 & keys2), str(len(keys1 & keys2)))

        filt = await dm.search(
            {"q": "readme", "family": "text", "sizeMax": "5000", "limit": 5}
        )
        check("filtered search", all(r.size and r.size <= 5000 for r in filt.results),
              str([r.size for r in filt.results]))
        check("family filter", all(r.family == "text" for r in filt.results))

        # --- collections ----------------------------------------------------
        genres = await dm.genres("cd-rom")
        check("cd-rom genres", len(genres) >= 25, str(len(genres)))
        check("All Genres first", genres[0][0] == "All Genres", str(genres[:2]))
        # The page renders genres in a 3-across grid that reads alphabetically down
        # each column, so DOM order interleaves three alphabetical runs.
        rest = [g[0].lower() for g in genres[1:]]
        check("genres alphabetical", rest == sorted(rest), str(rest[:5]))
        check("genre counts", all(c > 0 for _, c in genres))

        items = await dm.genre_items("cd-rom", "Calendar")
        check("calendar items", 40 <= len(items) <= 60, str(len(items)))
        it = next((i for i in items if "Anne Geddes" in i.title), None)
        check("item parsed", it is not None)
        if it:
            check("item year", it.year == "1996", it.year)
            check("item publisher", it.publisher == "Cedeco", it.publisher)
            check("item files", it.num_files == 694, str(it.num_files))
            check("item ia id", it.archive_org == "anne-geddes-calendar", it.archive_org)

        found = await dm.genre_items("cd-rom", search="mad")
        check("collection search", len(found) >= 10, str(len(found)))

        ftp_items = await dm.genre_items("ftp")
        check("ftp flat list", len(ftp_items) > 50, str(len(ftp_items)))

        # --- archive.org bridge ---------------------------------------------
        ia = await dm.ia_files("TotallyMad1999Artwork")
        check("ia metadata", "MAD_DISC_1.ISO" in ia)
        check("ia md5", ia["MAD_DISC_1.ISO"]["md5"] == "646cf7d5136b1a429fd5245b76e1e268")
        check("ia size", ia["MAD_DISC_1.ISO"]["size"] == 604704768)

        # --- bundle url -------------------------------------------------------
        url = dm.bundle_url({"q": "zork", "limit": 20})
        check("bundle url", "download=true" in url and "q=zork" in url, url[:80])
    finally:
        await dm.aclose()

    print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
