"""Live downloader tests.  Run: python tests/test_downloader_live.py

Uses a throwaway DISCDIG_HOME + download dir so it never touches real state.
Everything fetched here is small (< 1 MB) apart from one deliberately-aborted
partial transfer used to prove resume works.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="discdig-test-"))
os.environ["DISCDIG_HOME"] = str(TMP / "home")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discdig.api import DiscMaster, Entry  # noqa: E402
from discdig.downloader import Downloader, destination, safe_component  # noqa: E402
from discdig.store import DONE, FAILED, PAUSED, QUEUED, Config, Store  # noqa: E402

failures = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global failures
    if not cond:
        failures += 1
    print(f"[{'ok  ' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))


async def drain(dl: Downloader, ids: list[int], timeout: float = 240.0) -> None:
    """Wait until every listed task leaves the queued/running state."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        live = [dl.tasks[i] for i in ids if i in dl.tasks]
        if all(t.status in (DONE, FAILED, PAUSED) for t in live):
            return
        await asyncio.sleep(0.2)
    raise TimeoutError("tasks did not settle: " +
                       str([(t.id, t.status) for t in live]))


async def main() -> int:
    cfg = Config(download_dir=str(TMP / "dl"), concurrency=3, layout="item", verify=True)
    cfg.save()
    store = Store()
    dm = DiscMaster()
    dl = Downloader(store, cfg, dm.client)
    dl.load()
    await dl.start()

    try:
        # --- path helpers --------------------------------------------------
        check("safe_component strips", safe_component('a<b>c:d?') == "a_b_c_d_",
              safe_component('a<b>c:d?'))
        check("safe_component device", safe_component("CON.txt").startswith("_"))
        e = Entry(itemid=1, fileid="A.iso/sub/file.txt", name="file.txt")
        p = destination(cfg, e, "My Item")
        check("layout item", p == Path(cfg.download_dir) / "My Item" / "A.iso" / "sub" / "file.txt", str(p))
        check("layout flat", destination(Config(download_dir="/x", layout="flat"), e).name == "file.txt")

        # --- a real small file from inside an ISO ---------------------------
        good = Entry(
            itemid=14318,
            fileid="Meeting_Pearls_III.iso/Pearls/anim/Eric/Readmes/README!README!README!",
            name="README!README!README!", family="text", size=761,
        )
        added, dupes, skipped = await dl.enqueue([good], item_names={14318: "Meeting Pearls III"})
        check("enqueued 1", (added, dupes, skipped) == (1, 0, 0), str((added, dupes, skipped)))
        tid = next(t.id for t in dl.tasks.values() if t.fileid == good.fileid)
        await drain(dl, [tid])
        t = dl.tasks[tid]
        check("small file done", t.status == DONE, f"{t.status} {t.error}")
        check("small file size", t.path.exists() and t.path.stat().st_size == 761,
              str(t.path.stat().st_size if t.path.exists() else "missing"))
        check("no warning", not t.warn, t.warn)
        check("content correct",
              t.path.read_bytes().startswith(b"Small Curiosities by Eric W. Schwartz"))
        check("no .part left", not t.part_path.exists())

        # --- duplicate is a no-op ------------------------------------------
        added2, dupes2, _ = await dl.enqueue([good])
        check("dupe detected", (added2, dupes2) == (0, 1), str((added2, dupes2)))

        # --- directories are skipped ---------------------------------------
        d = Entry(itemid=14318, fileid="Meeting_Pearls_III.iso/Pearls", name="Pearls",
                  family="directory")
        a3, d3, s3 = await dl.enqueue([d])
        check("directory skipped", (a3, d3, s3) == (0, 0, 1), str((a3, d3, s3)))

        # --- stale index entry -> permanent 404 -----------------------------
        gone = Entry(
            itemid=3327,
            fileid="Aminet 8 (1995)(GTI - Schatztruhe)[!][Oct 1995].iso/Aminet/pix/eric/FantaFilms.lha/README!",
            name="README!", family="text", size=761,
        )
        await dl.enqueue([gone])
        gid = next(t.id for t in dl.tasks.values() if t.fileid == gone.fileid)
        await drain(dl, [gid])
        gt = dl.tasks[gid]
        check("404 -> failed", gt.status == FAILED, gt.status)
        check("404 not retried 4x", gt.attempts == 1, str(gt.attempts))
        check("404 message", "404" in gt.error, gt.error)

        # --- the corruption detector ----------------------------------------
        # Item 17246's archive-internal files currently serve unrelated bytes;
        # the listing says 8.5 KiB, the server hands back gigabytes.
        bad = Entry(itemid=17246, fileid="MAD_DISC_1.ISO/autorun.inf",
                    name="autorun.inf", family="text", size=56)
        await dl.enqueue([bad])
        bid = next(t.id for t in dl.tasks.values() if t.fileid == bad.fileid)
        # Let it get going, then confirm the mismatch was flagged before we
        # waste bandwidth on the whole bogus payload.
        await drain(dl, [bid])
        bt = dl.tasks[bid]
        check("wrong-file served -> refused", bt.status == FAILED, f"{bt.status} {bt.error}")
        check("refusal explains why", "wrong file" in bt.error, bt.error)
        check("nothing written", not bt.path.exists() and bt.downloaded < 8 * 1024**2,
              f"{bt.downloaded} bytes")

        # --- pause on a live transfer ----------------------------------------
        big = Entry(itemid=17246, fileid="04-Totally_Mad_1999_disc_1_outer.jpg",
                    name="04-Totally_Mad_1999_disc_1_outer.jpg", family="image", size=1031297)
        await dl.enqueue([big], item_names={17246: "Totally Mad"})
        pid = next(t.id for t in dl.tasks.values() if t.fileid == big.fileid)
        dl.pause(pid)
        await asyncio.sleep(0.6)
        check("pause works", dl.tasks[pid].status == PAUSED, dl.tasks[pid].status)

        # --- archive.org routing + md5 verification --------------------------
        ia_id = await dm.archive_org_id(17246)
        ia_files = await dm.ia_files(ia_id)
        jpg = Entry(itemid=17246, fileid="01-Totally_Mad_1999_front.jpg",
                    name="01-Totally_Mad_1999_front.jpg", family="image", size=393000)
        await dl.enqueue([jpg], item_names={17246: "Totally Mad"},
                         ia_index={17246: (ia_id, ia_files)})
        jid = next(t.id for t in dl.tasks.values() if t.fileid == jpg.fileid)
        jt = dl.tasks[jid]
        check("routed to archive.org", jt.source == "archive.org", jt.source)
        check("md5 attached", len(jt.md5) == 32, jt.md5)
        check("exact size from IA", jt.size_exact and jt.expected_size == 393332,
              str(jt.expected_size))
        await drain(dl, [jid])
        jt = dl.tasks[jid]
        check("IA download done", jt.status == DONE, f"{jt.status} {jt.error}")
        digest = hashlib.md5(jt.path.read_bytes()).hexdigest()
        check("md5 verified", digest == jt.md5, digest)
        check("no warn on verified", not jt.warn, jt.warn)

        # --- resume from a partial file ---------------------------------------
        jt.path.unlink()
        part = jt.part_path
        part.write_bytes(jt.path.parent.joinpath().as_posix().encode()[:0] + b"")
        full = None
        # seed a genuine prefix so the range request stitches correctly
        r = await dm.client.get(jt.url, headers={"Range": "bytes=0-99999"})
        part.write_bytes(r.content)
        check("part seeded", part.stat().st_size == 100000, str(part.stat().st_size))
        dl.resume(jid)
        store.update(jid, status=QUEUED)
        dl.tasks[jid].status = QUEUED
        dl._pending.put_nowait(jid)
        await drain(dl, [jid])
        jt = dl.tasks[jid]
        check("resumed download done", jt.status == DONE, f"{jt.status} {jt.error}")
        check("resumed md5 ok",
              hashlib.md5(jt.path.read_bytes()).hexdigest() == jt.md5)
        check("resumed size", jt.path.stat().st_size == 393332, str(jt.path.stat().st_size))

        # --- already-on-disk short circuit -------------------------------------
        dl.cancel(jid, delete_partial=False)
        await dl.enqueue([jpg], item_names={17246: "Totally Mad"},
                         ia_index={17246: (ia_id, ia_files)})
        jid2 = next(t.id for t in dl.tasks.values() if t.fileid == jpg.fileid)
        await drain(dl, [jid2])
        check("skips existing file", dl.tasks[jid2].warn == "already on disk",
              dl.tasks[jid2].warn)

        # --- persistence across a restart ---------------------------------------
        prog = dl.progress()
        check("progress counts", prog.done >= 2 and prog.failed >= 1,
              f"done={prog.done} failed={prog.failed} paused={prog.paused}")
        await dl.stop()
        store.close()

        store2 = Store()
        dl2 = Downloader(store2, cfg, dm.client)
        dl2.load()
        check("queue reloaded", len(dl2.tasks) == len(dl.tasks),
              f"{len(dl2.tasks)} vs {len(dl.tasks)}")
        check("paused survives", any(t.status == PAUSED for t in dl2.tasks.values()))
        check("failed survives", any(t.status == FAILED for t in dl2.tasks.values()))
        check("done survives", any(t.status == DONE for t in dl2.tasks.values()))
        n = dl2.retry_failed()
        check("retry_failed requeues", n >= 1, str(n))
        store2.close()
    finally:
        await dm.aclose()
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
