"""HTTP client for discmaster.textfiles.com.

The site exposes exactly one machine-readable endpoint -- ``/search?outputAs=json``.
Everything else is deliberately-simple HTML aimed at vintage browsers, so the
browse/collection pages are scraped.  The scraping is intentionally structural
rather than positional: discmaster renders each family of files (directories,
archives, images, text, ...) into its own table with a *different* column layout,
so we anchor on the ``<a name="family">`` section markers and then pull links plus
whatever size/date/format text trails each link.  That survives layout churn far
better than counting ``<td>``s.

Endpoints used
--------------
``/search?...&outputAs=json``   search API (``pageNum`` is 0-indexed)
``/search?download=true&...``   302 -> ``/download/<token>.tar.gz`` bundle, max 1 GiB
``/browse/<itemid>[/<path>]``   directory / archive listing (HTML)
``/view/<itemid>/<path>``       file viewer (HTML)
``/file/<itemid>/<path>``       raw original bytes, supports HTTP Range
``/cd-rom|/disk|/ftp|/other``   collection index (HTML)
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

BASE = "https://discmaster.textfiles.com"
IA_BASE = "https://archive.org"

USER_AGENT = "discdig/1.0 (+https://github.com/ - terminal browser for discmaster)"

SECTIONS = ("cd-rom", "disk", "ftp", "other")

#: Families discmaster groups files into, in the order it presents them.
FAMILIES = (
    "directory",
    "archive",
    "image",
    "text",
    "document",
    "audio",
    "music",
    "video",
    "poly",
    "font",
    "executable",
    "other",
    "unknown",
)

_SIZE_UNITS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
}

_TAG_RE = re.compile(r"<[^>]+>")
_SIZE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(b|KiB|MiB|GiB|TiB|kB|MB|GB)\b", re.I)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DURATION_RE = re.compile(r"\b(\d+(?:\.\d+)?[hms])\b")


def strip_tags(fragment: str) -> str:
    """HTML fragment -> plain text, with nbsp collapsed to normal spaces."""
    text = _TAG_RE.sub(" ", fragment)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_size(text: str) -> int | None:
    """``"576.7 MiB"`` -> ``604832563``.  Returns None when no size is present."""
    m = _SIZE_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    return int(number * _SIZE_UNITS[m.group(2).lower()])


def human_size(n: float | None) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" or n >= 100 else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def quote_path(path: str) -> str:
    """Percent-encode a discmaster file path for use in a URL.

    Discmaster's own HTML leaves spaces and most punctuation raw, but a bare
    space is illegal in a request line and ``#``/``?`` would truncate the path,
    so only the genuinely-unsafe characters are escaped.
    """
    return urllib.parse.quote(path, safe="/!$&'()*+,;=:@~[]{}^`\"<>\\|")


# --------------------------------------------------------------------------- models


@dataclass(slots=True)
class Entry:
    """One row of a browse listing, or one search hit."""

    itemid: int
    fileid: str  # path relative to the item root, "" for the item itself
    name: str
    family: str = "unknown"
    format_name: str = ""
    size: int | None = None
    date: str = ""
    sub_count: int | None = None  # files contained, for dirs/archives
    b3sum: str = ""
    item_name: str = ""
    ext: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_dir(self) -> bool:
        return self.family == "directory"

    @property
    def is_archive(self) -> bool:
        return self.family == "archive"

    @property
    def is_container(self) -> bool:
        """True when the row can be descended into."""
        return self.is_dir or self.is_archive

    @property
    def downloadable(self) -> bool:
        """Directories are not real files; everything else has original bytes."""
        return not self.is_dir

    def file_url(self, base: str = BASE) -> str:
        return f"{base}/file/{self.itemid}/{quote_path(self.fileid)}"

    def view_url(self, base: str = BASE) -> str:
        return f"{base}/view/{self.itemid}/{quote_path(self.fileid)}"

    def browse_url(self, base: str = BASE) -> str:
        tail = f"/{quote_path(self.fileid)}" if self.fileid else ""
        return f"{base}/browse/{self.itemid}{tail}"


@dataclass(slots=True)
class Listing:
    """Result of ``/browse/<itemid>[/<path>]``."""

    itemid: int
    path: str
    title: str
    entries: list[Entry]
    archive_org: str = ""  # archive.org identifier, only on the item root page
    subtitle: str = ""  # e.g. "1999 Interactive from Broderbund"

    @property
    def crumbs(self) -> list[str]:
        return [p for p in self.path.split("/") if p]


@dataclass(slots=True)
class ItemSummary:
    """One row of a collection/genre index."""

    itemid: int
    title: str
    year: str = ""
    publisher: str = ""
    num_files: int | None = None
    size: int | None = None
    handled: str = ""
    archive_org: str = ""


@dataclass(slots=True)
class SearchPage:
    results: list[Entry]
    total: int | None
    page: int
    limit: int
    took_ms: int | None = None

    @property
    def pages(self) -> int | None:
        if self.total is None:
            return None
        return max(1, -(-min(self.total, 10_000) // self.limit))


# --------------------------------------------------------------------------- search


#: Search fields the UI exposes, mapped to their discmaster query parameter.
SEARCH_PARAMS = (
    "q", "qfields", "mode", "extension", "visual", "auditory",
    "family", "format", "genre", "itemid", "detection",
    "sizeMin", "sizeMax", "durationMin", "durationMax",
    "widthMin", "heightMin", "widthMax", "heightMax",
    "tsMin", "tsMax", "addedAfter",
    "nsfw", "nsfwOnly", "animated", "unsupported",
    "dedup", "sortBy", "sortOrderDesc", "limit", "b3sum",
)


def build_search_query(filters: dict[str, Any], page: int = 0) -> dict[str, str]:
    """Drop empty values, coerce to strings, add 0-indexed ``pageNum``."""
    out: dict[str, str] = {}
    for key in SEARCH_PARAMS:
        val = filters.get(key)
        if val is None or val == "" or val is False:
            continue
        if val is True:
            val = key  # discmaster's checkboxes post their own name as the value
        if not isinstance(val, (str, int, float)):
            # Guard against UI sentinels (an untouched Select, say) stringifying
            # into the query, which the server accepts and silently matches zero.
            continue
        out[key] = str(val)
    out.setdefault("limit", "100")
    if page:
        out["pageNum"] = str(page)
    return out


class DiscMaster:
    """Async client.  One instance per app; reuses a single HTTP/2 connection pool."""

    def __init__(self, base: str = BASE, timeout: float = 45.0) -> None:
        self.base = base.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._ia_cache: dict[int, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _text(self, url: str, **kw: Any) -> str:
        r = await self._client.get(url, **kw)
        r.raise_for_status()
        return r.text

    # -- search ------------------------------------------------------------

    async def search(self, filters: dict[str, Any], page: int = 0) -> SearchPage:
        params = build_search_query(filters, page)
        limit = int(params.get("limit", 100))

        # The JSON view returns the rows but not the match count, so the count is
        # scraped from the HTML view only when the caller lands on a new query.
        json_params = dict(params, outputAs="json")
        raw = await self._text(f"{self.base}/search", params=json_params)
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("search returned non-JSON (query rejected by server)")
        if isinstance(rows, dict):  # defensive: error payloads
            raise RuntimeError(str(rows.get("error") or rows)[:200])

        results = [self._entry_from_json(row) for row in rows]
        return SearchPage(results=results, total=None, page=page, limit=limit)

    async def search_count(self, filters: dict[str, Any]) -> tuple[int | None, int | None]:
        """Scrape ``N results shown (M matches) (took Xms)`` from the HTML view."""
        params = build_search_query(dict(filters, limit=1), 0)
        try:
            page = await self._text(f"{self.base}/search", params=params)
        except httpx.HTTPError:
            return None, None
        text = strip_tags(page)
        total = None
        took = None
        m = re.search(r"\(([\d,]+)\s+matches?\)", text)
        if m:
            total = int(m.group(1).replace(",", ""))
        m = re.search(r"took\s+(\d+)ms", text)
        if m:
            took = int(m.group(1))
        return total, took

    @staticmethod
    def _entry_from_json(row: dict[str, Any]) -> Entry:
        # Directory hits carry no "fileid" -- only an href -- so recover the path
        # from the link.  The JSON "fileid" is the true path, but "href" is
        # percent-encoded: a name holding a literal '%' arrives there as "%25".
        # Decoding is what makes the two agree.
        fileid = row.get("fileid") or ""
        if not fileid:
            href = row.get("href") or ""
            m = re.match(r"/(?:browse|view)/\d+/?(.*)", href, re.S)
            if m:
                fileid = urllib.parse.unquote(m.group(1))
        return Entry(
            itemid=int(row.get("itemid", 0)),
            fileid=fileid,
            name=row.get("filename") or fileid.rsplit("/", 1)[-1],
            family=row.get("family") or "unknown",
            format_name=row.get("formatName") or row.get("formatid") or "",
            size=row.get("size"),
            date=(row.get("ts") or "")[:10],
            sub_count=row.get("subCount"),
            b3sum=row.get("b3sum") or "",
            item_name=row.get("itemName") or "",
            ext=row.get("ext") or "",
            extra={k: row[k] for k in ("dexid", "ids", "meta", "whenAdded") if k in row},
        )

    def bundle_url(self, filters: dict[str, Any], converted: bool = False) -> str:
        """URL that 302s to a ``.tar.gz`` of every match (server caps it at 1 GiB)."""
        params = build_search_query(filters, 0)
        params["download"] = "true"
        if converted:
            params["converted"] = "true"
        return f"{self.base}/search?{urllib.parse.urlencode(params)}"

    # -- browse ------------------------------------------------------------

    async def browse(self, itemid: int, path: str = "") -> Listing:
        tail = f"/{quote_path(path)}" if path else ""
        page = await self._text(f"{self.base}/browse/{itemid}{tail}")
        return parse_listing(page, itemid, path)

    async def item_details(self, itemid: int) -> Listing:
        return await self.browse(itemid, "")

    # -- collections -------------------------------------------------------

    async def genres(self, section: str) -> list[tuple[str, int]]:
        page = await self._text(f"{self.base}/{section}")
        return parse_genres(page, section)

    async def genre_items(
        self, section: str, genre: str = "", search: str = ""
    ) -> list[ItemSummary]:
        url = f"{self.base}/{section}"
        if genre:
            url += "/" + urllib.parse.quote(genre)
        params = {"search": search} if search else None
        page = await self._text(url, params=params)
        return parse_item_table(page)

    # -- archive.org bridge ------------------------------------------------

    async def archive_org_id(self, itemid: int) -> str:
        """Identifier of the archive.org item this disc was mirrored from."""
        if itemid in self._ia_cache:
            return self._ia_cache[itemid]
        try:
            listing = await self.browse(itemid, "")
        except httpx.HTTPError:
            return ""
        self._ia_cache[itemid] = listing.archive_org
        return listing.archive_org

    async def ia_files(self, identifier: str) -> dict[str, dict[str, Any]]:
        """``{filename: {size, md5, format}}`` for an archive.org item."""
        if not identifier:
            return {}
        try:
            r = await self._client.get(f"{IA_BASE}/metadata/{identifier}", timeout=30.0)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for f in data.get("files") or []:
            name = f.get("name")
            if not name:
                continue
            out[name] = {
                "size": int(f["size"]) if f.get("size") else None,
                "md5": f.get("md5") or "",
                "format": f.get("format") or "",
            }
        return out

    @staticmethod
    def ia_url(identifier: str, name: str) -> str:
        return f"{IA_BASE}/download/{identifier}/{urllib.parse.quote(name)}"

    # -- misc --------------------------------------------------------------

    async def peek(self, entry: Entry, nbytes: int = 8192) -> bytes:
        """First ``nbytes`` of a file, for the preview pane."""
        r = await self._client.get(
            entry.file_url(self.base), headers={"Range": f"bytes=0-{nbytes - 1}"}
        )
        r.raise_for_status()
        return r.content

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client


# --------------------------------------------------------------------------- parsers


_SECTION_RE = re.compile(r'<a name="([a-z]+)"></a>(.*?)(?=<a name="[a-z]+"></a>|\Z)', re.S)
_LINK_RE = re.compile(r'<a\s+href="(/(?:browse|view)/(\d+)(?:/([^"]*))?)"[^>]*>(.*?)</a>', re.S)


def parse_listing(page: str, itemid: int, path: str) -> Listing:
    """Turn a ``/browse/`` page into a :class:`Listing`.

    Each family lives in ``<a name="family"></a>`` ... next marker.  Within a
    section every ``/browse/`` or ``/view/`` link is one entry; the text between
    that link and the following link carries the format, size and date in a
    layout that varies per family, so those are pulled out by pattern instead of
    by column position.
    """
    title = ""
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    if m:
        title = strip_tags(m.group(1))

    archive_org = ""
    m = re.search(r'href="https?://archive\.org/details/([^"]+)"', page)
    if m:
        archive_org = html.unescape(m.group(1)).strip("/")

    subtitle = ""
    m = re.search(r"<tt>([^<]*\bfrom\b[^<]*)</tt>\s*(?:<[^>]+>)*\s*([A-Za-z][^<]*)?<br>", page)
    if m:
        subtitle = strip_tags(m.group(0))

    prefix = f"{path}/" if path else ""
    entries: list[Entry] = []
    seen: set[str] = set()

    # Inline <script> follows the last row of some sections; without this the
    # trailing-text scan reads JavaScript into the format column.
    page = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)

    for sect_m in _SECTION_RE.finditer(page):
        family = sect_m.group(1)
        if family not in FAMILIES:
            continue
        body = sect_m.group(2)
        links = list(_LINK_RE.finditer(body))
        for i, link in enumerate(links):
            href_item = int(link.group(2))
            if href_item != itemid:
                continue
            # Discmaster leaves spaces, brackets and parens raw in its hrefs, but
            # it does percent-encode what would otherwise break the URL: '#'
            # arrives as "%23", and a filename genuinely containing '%' arrives
            # as "%25" -- so decoding is safe, and skipping it is not.  Both
            # layers have to come off, entities first, or the path carries "%23"
            # around as literal text and quote_path re-encodes it to "%2523",
            # which 404s on every view, browse and download URL built from it.
            fileid = urllib.parse.unquote(html.unescape(link.group(3) or ""))
            if not fileid or fileid in seen:
                continue
            # Only direct children -- video/image sections also link to converted
            # renditions nested a level deeper (e.g. ".smk/foo.mp4").
            rest = fileid[len(prefix):] if fileid.startswith(prefix) else None
            if rest is None or not rest or "/" in rest:
                continue
            seen.add(fileid)

            name = strip_tags(link.group(4)) or rest
            tail = body[link.end(): links[i + 1].start() if i + 1 < len(links) else len(body)]
            tail_text = strip_tags(tail)

            size = parse_size(tail_text)
            date_m = _DATE_RE.search(tail_text)
            date = date_m.group(1) if date_m else ""

            # Format = leading text before the first number-ish token.
            fmt = tail_text
            for cut in (date, ):
                if cut:
                    fmt = fmt.split(cut)[0]
            fmt = _SIZE_RE.split(fmt)[0]
            fmt = _DURATION_RE.split(fmt)[0]
            fmt = re.sub(r"\s*\b[\d,]+\b\s*$", " ", fmt).strip(" |,")

            sub_count = None
            cm = re.search(r"\b([\d,]+)\b", tail_text)
            if cm and (family in ("directory", "archive")):
                sub_count = int(cm.group(1).replace(",", ""))

            entries.append(
                Entry(
                    itemid=itemid,
                    fileid=fileid,
                    name=name,
                    family=family,
                    format_name=fmt[:60],
                    size=size,
                    date=date,
                    sub_count=sub_count,
                )
            )

    order = {f: i for i, f in enumerate(FAMILIES)}
    entries.sort(key=lambda e: (order.get(e.family, 99), e.name.lower()))
    return Listing(
        itemid=itemid,
        path=path,
        title=title,
        entries=entries,
        archive_org=archive_org,
        subtitle=subtitle,
    )


_GENRE_RE = re.compile(
    r"<font size=\"\+1\">([\d,\s\xa0]+)</font>.{0,120}?"
    r'<a href="/(?:cd-rom|disk|ftp|other)/([^"]+)">(.*?)</a>',
    re.S,
)


def parse_genres(page: str, section: str) -> list[tuple[str, int]]:
    """Genre names and disc counts, alphabetically.

    The page lays the genres out in a three-across grid that reads
    alphabetically *down each column*, so the order they appear in the HTML
    interleaves three separate alphabetical runs -- which looks like no order at
    all once flattened into a single list.  Re-sorting restores the sequence the
    site actually shows.  "All Genres" is the aggregate, so it stays on top.
    """
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for m in _GENRE_RE.finditer(page):
        count_txt = m.group(1).replace("\xa0", " ").strip().replace(",", "")
        name = html.unescape(urllib.parse.unquote(m.group(2)))
        if name in seen or not count_txt.isdigit():
            continue
        seen.add(name)
        out.append((name, int(count_txt)))
    out.sort(key=lambda g: (g[0] != "All Genres", g[0].lower()))
    return out


_ITEM_LINK_RE = re.compile(r'<a href="/browse/(\d+)">(.*?)</a>', re.S)


def parse_item_table(page: str) -> list[ItemSummary]:
    """Parse the item tables on a collection/genre page.

    Rows are split on ``<tr>`` rather than matched as a whole, because indented
    rows (multi-disc sets) prefix the title cell with a spacer ``<tt>`` and would
    otherwise be skipped -- that dropped 17 of 46 items on /cd-rom/Calendar.
    """
    out: list[ItemSummary] = []
    seen: set[int] = set()
    for row in re.split(r"<tr\b[^>]*>", page):
        m = _ITEM_LINK_RE.search(row)
        if not m:
            continue
        itemid = int(m.group(1))
        if itemid in seen:
            continue
        seen.add(itemid)
        title = strip_tags(m.group(2))
        rest = row[m.end():]
        cells = [strip_tags(c) for c in re.split(r"<td[^>]*>", rest)[1:]]
        ia = ""
        ia_m = re.search(r'href="https?://archive\.org/details/([^"]+)"', rest)
        if ia_m:
            ia = html.unescape(ia_m.group(1)).strip("/")
        year = publisher = handled = ""
        num_files = size = None
        for c in cells:
            if not c:
                continue
            if not year and re.fullmatch(r"(19|20)\d{2}", c):
                year = c
            elif c.endswith("%") and not handled:
                handled = c
            elif _SIZE_RE.fullmatch(c.replace("\xa0", " ").strip()):
                size = parse_size(c)
            elif re.fullmatch(r"[\d,]+", c) and num_files is None:
                num_files = int(c.replace(",", ""))
            elif c.lower() != "archive.org" and not publisher:
                publisher = c
        out.append(
            ItemSummary(
                itemid=itemid, title=title, year=year, publisher=publisher,
                num_files=num_files, size=size, handled=handled, archive_org=ia,
            )
        )
    return out


def format_options(page: str) -> list[tuple[str, str]]:
    """Extract ``<select name="format">`` options from the search page."""
    m = re.search(r'<select[^>]*name="(format|family|genre)"[^>]*>(.*?)</select>', page, re.S)
    if not m:
        return []
    return [
        (html.unescape(v), strip_tags(label))
        for v, label in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)', m.group(2))
        if v
    ]


def parse_select(page: str, name: str) -> list[tuple[str, str]]:
    m = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', page, re.S)
    if not m:
        return []
    out = []
    for v, label in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)', m.group(1)):
        if not v:
            continue
        out.append((html.unescape(v), strip_tags(label)))
    return out


def iter_chunks(seq: Iterable[Any], n: int) -> Iterable[list[Any]]:
    buf: list[Any] = []
    for x in seq:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
