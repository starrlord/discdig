"""Concurrent, resumable download engine.

Design notes
------------
* Transfers stream to ``<dest>.part`` and are renamed into place only after the
  file is complete and (when a checksum is known) verified, so an interrupted
  run never leaves a truncated file that looks finished.
* Resume uses an HTTP ``Range`` request seeded from the existing ``.part`` size.
  Both discmaster's ``/file/`` endpoint and archive.org honour ranges; if a
  server answers ``200`` instead of ``206`` the partial file is discarded and the
  transfer restarts, which is the only safe interpretation.
* **Size sanity check.** Discmaster occasionally serves the wrong bytes for files
  extracted out of an archive -- observed on item 17246, where an 8.5 KiB file
  came back as 2.7 GB of unrelated data.  Every task therefore carries the size
  the listing advertised, and a mismatch marks the result with a loud warning
  rather than silently accepting garbage.
* Top-level item files (the ISOs and images that archive.org actually hosts) are
  preferentially fetched straight from archive.org, which is faster and supplies
  an MD5 to verify against.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from .api import BASE, DiscMaster, Entry, quote_path
from .store import (
    ACTIVE_STATES, DONE, FAILED, PAUSED, QUEUED, RUNNING, Config, Store, Task,
)

CHUNK = 256 * 1024
#: Listing sizes are rounded ("576.7 MiB"), so allow a little slack before crying foul.
ROUNDED_TOLERANCE = 0.01
_ILLEGAL = '<>:"|?*'


def safe_component(name: str) -> str:
    """Make one path segment safe for Windows and POSIX alike."""
    out = "".join("_" if c in _ILLEGAL or ord(c) < 32 else c for c in name)
    out = out.rstrip(" .") or "_"
    # Reserved DOS device names, with or without an extension.
    stem = out.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}:
        out = "_" + out
    return out[:120]


def destination(cfg: Config, entry: Entry, item_name: str = "") -> Path:
    """Where a given entry lands on disk, per the configured layout."""
    root = Path(cfg.download_dir).expanduser()
    parts = [safe_component(p) for p in entry.fileid.split("/") if p]
    if not parts:
        parts = [safe_component(entry.name or f"item{entry.itemid}")]
    if cfg.layout == "flat":
        return root / parts[-1]
    if cfg.layout == "full":
        return root.joinpath(str(entry.itemid), *parts)
    label = item_name or entry.item_name or f"item{entry.itemid}"
    return root.joinpath(safe_component(label), *parts)


@dataclass
class Progress:
    """Aggregate figures for the queue header."""

    active: int = 0
    queued: int = 0
    done: int = 0
    failed: int = 0
    paused: int = 0
    speed: float = 0.0
    downloaded: int = 0
    total: int = 0

    @property
    def pct(self) -> float:
        return min(100.0, self.downloaded * 100.0 / self.total) if self.total else 0.0


class Downloader:
    """Owns a pool of worker coroutines that drain the persistent queue."""

    def __init__(
        self,
        store: Store,
        config: Config,
        client: httpx.AsyncClient,
        base: str = BASE,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.cfg = config
        self.client = client
        self.base = base.rstrip("/")
        self.on_change = on_change or (lambda: None)

        self.tasks: dict[int, Task] = {}
        self._pending: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        #: Set to ask an in-flight transfer to stop at the next chunk boundary.
        #: Cheaper and less ambiguous than cancelling a nested asyncio task.
        self._stop_flags: dict[int, asyncio.Event] = {}
        self._running = False
        self._paused_all = False
        self._last_flush: dict[int, float] = {}

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        """Pull the persisted queue into memory; anything mid-flight becomes queued."""
        for task in self.store.all():
            if task.status == RUNNING:
                task.status = QUEUED
                self.store.update(task.id, status=QUEUED)
            self.tasks[task.id] = task
            if task.status == QUEUED:
                self._pending.put_nowait(task.id)

    async def start(self) -> None:
        self._running = True
        self._scale_workers(self.cfg.concurrency)

    async def stop(self) -> None:
        self._running = False
        for flag in self._stop_flags.values():
            flag.set()
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        # Persist whatever progress the in-flight transfers reached.
        for task in self.tasks.values():
            if task.status == RUNNING:
                self.store.update(task.id, status=QUEUED, downloaded=task.downloaded)

    def _scale_workers(self, n: int) -> None:
        n = max(1, min(8, n))
        while len(self._workers) < n:
            self._workers.append(asyncio.create_task(self._worker()))
        while len(self._workers) > n:
            self._workers.pop().cancel()

    def set_concurrency(self, n: int) -> int:
        self.cfg.concurrency = max(1, min(8, n))
        self.cfg.save()
        if self._running:
            self._scale_workers(self.cfg.concurrency)
        return self.cfg.concurrency

    @property
    def paused_all(self) -> bool:
        return self._paused_all

    def toggle_pause_all(self) -> bool:
        self._paused_all = not self._paused_all
        if self._paused_all:
            for flag in self._stop_flags.values():
                flag.set()
        else:
            # Re-arm everything that is waiting.  Transfers interrupted by the
            # pause re-queue themselves as they unwind (see _Interrupted).
            for task in self.tasks.values():
                if task.status == QUEUED:
                    self._pending.put_nowait(task.id)
        self.on_change()
        return self._paused_all

    # -- enqueue -----------------------------------------------------------

    async def enqueue(
        self,
        entries: Iterable[Entry],
        item_names: dict[int, str] | None = None,
        ia_index: dict[int, tuple[str, dict[str, Any]]] | None = None,
    ) -> tuple[int, int, int]:
        """Queue entries.  Returns ``(added, duplicates, skipped)``.

        ``ia_index`` maps itemid -> (archive.org identifier, file metadata) and is
        used to route top-level files through archive.org with an MD5 to check.
        """
        item_names = item_names or {}
        ia_index = ia_index or {}
        added = dupes = skipped = 0
        new: list[Task] = []

        for entry in entries:
            if not entry.downloadable:
                skipped += 1
                continue
            dest = destination(self.cfg, entry, item_names.get(entry.itemid, ""))
            url = f"{self.base}/file/{entry.itemid}/{quote_path(entry.fileid)}"
            source, md5, size, exact = "discmaster", "", entry.size, entry.size is not None

            top_level = "/" not in entry.fileid
            if self.cfg.prefer_archive_org and top_level and entry.itemid in ia_index:
                identifier, files = ia_index[entry.itemid]
                meta = files.get(entry.name)
                if identifier and meta:
                    source = "archive.org"
                    url = DiscMaster.ia_url(identifier, entry.name)
                    md5 = meta.get("md5") or ""
                    if meta.get("size"):
                        size, exact = meta["size"], True

            new.append(
                Task(
                    itemid=entry.itemid,
                    fileid=entry.fileid,
                    name=entry.name,
                    family=entry.family,
                    item_name=item_names.get(entry.itemid, entry.item_name),
                    dest=str(dest),
                    url=url,
                    source=source,
                    expected_size=size,
                    size_exact=exact and source == "archive.org",
                    md5=md5,
                )
            )

        for task in new:
            stored, created = self.store.add(task)
            self.tasks[stored.id] = stored
            if created:
                added += 1
                if not self._paused_all:
                    self._pending.put_nowait(stored.id)
            else:
                dupes += 1
        if added:
            self.on_change()
        return added, dupes, skipped

    def enqueue_task(self, task: Task) -> tuple[Task, bool]:
        """Queue an already-built task (used for search bundles, which have no Entry)."""
        stored, created = self.store.add(task)
        self.tasks[stored.id] = stored
        if created and not self._paused_all:
            self._pending.put_nowait(stored.id)
        if created:
            self.on_change()
        return stored, created

    # -- per-task controls -------------------------------------------------

    def pause(self, task_id: int) -> None:
        task = self.tasks.get(task_id)
        if not task or task.status not in (QUEUED, RUNNING):
            return
        task.status = PAUSED
        task.speed = 0.0
        self.store.update(task_id, status=PAUSED, downloaded=task.downloaded)
        flag = self._stop_flags.get(task_id)
        if flag:
            flag.set()
        self.on_change()

    def resume(self, task_id: int) -> None:
        task = self.tasks.get(task_id)
        if not task or task.status not in (PAUSED, FAILED):
            return
        task.status, task.error, task.attempts = QUEUED, "", 0
        self.store.update(task_id, status=QUEUED, error="", attempts=0)
        self._pending.put_nowait(task_id)
        self.on_change()

    def cancel(self, task_id: int, delete_partial: bool = True) -> None:
        task = self.tasks.pop(task_id, None)
        if not task:
            return
        flag = self._stop_flags.get(task_id)
        if flag:
            flag.set()
        if delete_partial:
            task.part_path.unlink(missing_ok=True)
        self.store.remove(task_id)
        self.on_change()

    def clear_finished(self) -> int:
        removed = 0
        for tid, task in list(self.tasks.items()):
            if task.status in (DONE, FAILED):
                del self.tasks[tid]
                removed += 1
        self.store.clear([DONE, FAILED])
        if removed:
            self.on_change()
        return removed

    def retry_failed(self) -> int:
        n = 0
        for task in self.tasks.values():
            if task.status == FAILED:
                self.resume(task.id)
                n += 1
        return n

    # -- aggregate ---------------------------------------------------------

    def progress(self) -> Progress:
        p = Progress()
        for task in self.tasks.values():
            if task.status == RUNNING:
                p.active += 1
                p.speed += task.speed
            elif task.status == QUEUED:
                p.queued += 1
            elif task.status == DONE:
                p.done += 1
            elif task.status == FAILED:
                p.failed += 1
            elif task.status == PAUSED:
                p.paused += 1
            if task.status != DONE:
                size = task.total or task.expected_size or 0
                p.total += size
                p.downloaded += min(task.downloaded, size or task.downloaded)
        return p

    # -- worker ------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            task_id = await self._pending.get()
            try:
                if self._paused_all:
                    continue
                task = self.tasks.get(task_id)
                if not task or task.status != QUEUED:
                    continue
                self._stop_flags[task_id] = asyncio.Event()
                try:
                    await self._run(task)
                finally:
                    self._stop_flags.pop(task_id, None)
                    self.on_change()
            finally:
                self._pending.task_done()

    async def _run(self, task: Task) -> None:
        task.status = RUNNING
        task.error = ""
        task.warn = ""
        task.started_at = task.started_at or time.time()
        self.store.update(task.id, status=RUNNING, started_at=task.started_at, error="")
        self.on_change()

        delay = 1.5
        for attempt in range(1, self.cfg.max_attempts + 1):
            task.attempts = attempt
            try:
                await self._transfer(task)
                return
            except asyncio.CancelledError:
                raise
            except _Interrupted:
                # Paused or removed mid-transfer; the .part file stays for resume.
                # If the stop came from a pause-all that has since been lifted, put
                # the task straight back on the pending queue -- otherwise it would
                # sit in "queued" with nobody left to pick it up.
                if task.status == RUNNING:
                    task.status = QUEUED
                    self.store.update(task.id, status=QUEUED, downloaded=task.downloaded)
                    if not self._paused_all and task.id in self.tasks:
                        self._pending.put_nowait(task.id)
                return
            except _Permanent as exc:
                self._fail(task, str(exc))
                return
            except (httpx.HTTPError, OSError) as exc:
                if attempt >= self.cfg.max_attempts:
                    self._fail(task, f"{type(exc).__name__}: {exc}"[:200])
                    return
                task.error = f"retry {attempt}/{self.cfg.max_attempts}: {exc}"[:200]
                self.on_change()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def _fail(self, task: Task, message: str) -> None:
        task.status = FAILED
        task.error = message
        task.speed = 0.0
        task.finished_at = time.time()
        self.store.update(
            task.id, status=FAILED, error=message, attempts=task.attempts,
            finished_at=task.finished_at, downloaded=task.downloaded,
        )
        self.on_change()

    async def _transfer(self, task: Task) -> None:
        dest = task.path
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Already sitting on disk at the right size?  Don't refetch.
        if dest.exists():
            have = dest.stat().st_size
            if not task.expected_size or _size_ok(have, task.expected_size, task.size_exact):
                task.downloaded = task.total = have
                self._finish(task, warn="already on disk")
                return
            dest.unlink()

        part = task.part_path
        start = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={start}-"} if start else {}

        async with self.client.stream("GET", task.url, headers=headers,
                                      follow_redirects=True) as resp:
            if resp.status_code == 416:  # already have the whole thing
                start = part.stat().st_size if part.exists() else 0
                task.downloaded = task.total = start
                await self._complete(task)
                return
            if resp.status_code == 404:
                raise _Permanent(
                    "404 - file is in the index but not on the server "
                    "(common for nested archives)"
                )
            if resp.status_code == 403:
                raise _Permanent("403 - forbidden")
            resp.raise_for_status()

            if start and resp.status_code != 206:
                start = 0  # server ignored Range; restart cleanly
                part.unlink(missing_ok=True)

            total = _content_total(resp, start)
            task.total = total
            task.downloaded = start
            self._check_size(task, total)
            _ensure_space(dest, (total or 0) - start)

            stop = self._stop_flags.get(task.id)
            mode = "ab" if start else "wb"
            window_bytes, window_start = 0, time.monotonic()
            with open(part, mode, buffering=0) as fh:
                async for chunk in resp.aiter_bytes(CHUNK):
                    if stop is not None and stop.is_set():
                        raise _Interrupted
                    fh.write(chunk)
                    task.downloaded += len(chunk)
                    window_bytes += len(chunk)

                    now = time.monotonic()
                    span = now - window_start
                    if span >= 0.4:
                        rate = window_bytes / span
                        task.speed = rate if not task.speed else task.speed * 0.6 + rate * 0.4
                        remaining = (task.total or 0) - task.downloaded
                        task.eta = remaining / task.speed if task.speed > 0 and remaining > 0 else None
                        window_bytes, window_start = 0, now
                        self._flush(task)
                        self.on_change()

        await self._complete(task)

    async def _complete(self, task: Task) -> None:
        part = task.part_path
        if not part.exists():
            raise _Permanent("transfer produced no data")

        actual = part.stat().st_size
        warn = ""

        if task.total and actual != task.total:
            raise httpx.ReadError(f"short read: {actual} of {task.total} bytes")

        if task.expected_size and not _size_ok(actual, task.expected_size, task.size_exact):
            warn = (
                f"size mismatch: got {actual:,} B, listing said {task.expected_size:,} B "
                "- the server may have served the wrong file"
            )

        if task.md5 and self.cfg.verify:
            digest = await asyncio.to_thread(_md5, part)
            if digest != task.md5:
                part.unlink(missing_ok=True)
                raise _Permanent(f"MD5 mismatch (expected {task.md5[:12]}..., got {digest[:12]}...)")
            warn = warn or ""

        part.replace(task.path)
        task.downloaded = task.total = actual
        self._finish(task, warn=warn)

    def _finish(self, task: Task, warn: str = "") -> None:
        task.status = DONE
        task.speed = 0.0
        task.eta = None
        task.warn = warn
        task.finished_at = time.time()
        self.store.update(
            task.id, status=DONE, warn=warn, downloaded=task.downloaded,
            total=task.total, finished_at=task.finished_at, error="",
        )
        self.on_change()

    def _check_size(self, task: Task, total: int | None) -> None:
        """Cross-check the server's Content-Length against the advertised size.

        A small discrepancy is just the listing's rounding.  A large one means
        the server is handing back a different file entirely -- discmaster does
        this for some archive-internal paths -- so refuse rather than stream
        gigabytes of the wrong data.
        """
        expected = task.expected_size
        if not total or not expected:
            return
        if _size_ok(total, expected, task.size_exact):
            return
        wrong_file = total > expected * 10 and abs(total - expected) > 8 * 1024**2
        message = f"server reports {total:,} B but listing said {expected:,} B"
        if wrong_file:
            raise _Permanent(
                message + " - refusing to download, the server is serving the wrong file"
            )
        task.warn = message

    def _flush(self, task: Task) -> None:
        now = time.monotonic()
        if now - self._last_flush.get(task.id, 0.0) < 2.0:
            return
        self._last_flush[task.id] = now
        self.store.update(task.id, downloaded=task.downloaded, total=task.total)


class _Permanent(Exception):
    """An error that retrying cannot fix."""


class _Interrupted(Exception):
    """The transfer was asked to stop (pause / cancel / shutdown)."""


def _size_ok(actual: int, expected: int, exact: bool) -> bool:
    if exact:
        return actual == expected
    if expected <= 0:
        return True
    return abs(actual - expected) <= max(1024, expected * ROUNDED_TOLERANCE)


def _content_total(resp: httpx.Response, start: int) -> int | None:
    rng = resp.headers.get("content-range")
    if rng and "/" in rng:
        tail = rng.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    length = resp.headers.get("content-length")
    if length and length.isdigit():
        return int(length) + start
    return None


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _ensure_space(dest: Path, needed: int) -> None:
    if needed <= 0:
        return
    try:
        free = shutil.disk_usage(dest.parent).free
    except OSError:
        return
    if free < needed + 64 * 1024 * 1024:
        raise _Permanent(
            f"not enough free space: need {needed / 1024**2:.0f} MiB, "
            f"{free / 1024**2:.0f} MiB available"
        )
