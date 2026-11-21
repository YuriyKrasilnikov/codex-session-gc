"""Disposable native Keep and cleanup integration witnesses."""
import concurrent.futures
import fcntl
import importlib.util
import json
import os
import pathlib
import subprocess
import sqlite3
import tempfile
import threading
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("closed_batch", ROOT / "native/helper/tests/closed_work_batch.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
s = b.s


def keep(home, operation, identity=None, **changes):
    stat = home.stat()
    request = dict(operation=operation, codex_home=str(home), sqlite_home=str(home),
                   home_device=stat.st_dev, home_inode=stat.st_ino)
    if identity is not None:
        request["thread_id"] = identity
    request.update(changes)
    proc = subprocess.run([s.BIN], input=json.dumps(request), text=True,
                          capture_output=True, timeout=30)
    reply = json.loads(proc.stdout)
    assert proc.returncode == (0 if reply["status"] == "confirmed" else 1), (proc, reply)
    return reply


def exercise(mark_before_prepare):
    with tempfile.TemporaryDirectory(prefix="csgc-keep-fixture-") as temporary:
        base = pathlib.Path(temporary)
        (base / "first").mkdir()
        manifest, _, request, aid, uid, payload = b.prepare(base / "first")
        home = pathlib.Path(request["codex_home"])
        target = manifest["target"]
        assert keep(home, "keep_list")["thread_ids"] == []
        assert not (home / ".codex-session-gc/keep.sqlite3").exists()
        if mark_before_prepare:
            assert keep(home, "keep_set", target)["status"] == "confirmed"
        b.refresh(request)
        before = s.sql_snapshot(request), s.history_snapshot(home)
        if not mark_before_prepare:
            assert keep(home, "keep_set", target)["status"] == "confirmed"
        assert keep(home, "keep_list")["thread_ids"] == [target]
        assert keep(home, "keep_set", target)["detail"] == "thread already kept"
        with (home / ".tmp/rollout-maintenance.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert keep(home, "keep_clear", target)["status"] == "refused"
        assert keep(home, "keep_clear", target, home_inode=home.stat().st_ino + 1)["status"] == "refused"
        assert keep(home, "keep_list")["thread_ids"] == [target]
        outcome = s.dispatch(request, aid, uid, payload)
        assert outcome["status"] == "refused" and not outcome["removed_rollouts"] and not outcome["committed_databases"], outcome
        assert (s.sql_snapshot(request), s.history_snapshot(home)) == before
        assert keep(home, "keep_clear", target)["status"] == "confirmed"
        assert keep(home, "keep_clear", target)["detail"] == "thread was not kept"
        assert keep(home, "keep_set", str(uuid.uuid4()))["status"] == "refused"
        retry_aid, retry_uid = s.j.AttemptId(uuid.uuid4()), s.j.UnitId(uuid.uuid4())
        retry = dict(request, journal=str(base / "retry.sqlite"),
                     attempt_id=str(retry_aid.value), unit_id=str(retry_uid.value))
        s.j.create(pathlib.Path(retry["journal"]), s.j.StartEnvelope(retry_aid, payload, payload, payload, payload),
                   (s.j.Unit(retry_uid, 0, payload),))
        assert s.dispatch(retry, retry_aid, retry_uid, payload)["status"] == "confirmed"
        assert keep(home, "keep_set", target)["status"] == "refused"


def separate_homes():
    with tempfile.TemporaryDirectory(prefix="csgc-keep-homes-") as temporary:
        base = pathlib.Path(temporary)
        (base / "a").mkdir()
        (base / "b").mkdir()
        first, _, req1, *_ = b.prepare(base / "a")
        _, _, req2, *_ = b.prepare(base / "b")
        home1, home2 = pathlib.Path(req1["codex_home"]), pathlib.Path(req2["codex_home"])
        source = pathlib.Path(first["target_path"])
        with source.open("ab") as stream:
            stream.write(b"\n" * 8192)
        old = time.time() - 8 * 86400
        os.utime(source, (old, old))
        compressed = b.native("compress-stock", home1, str(source))
        assert compressed.returncode == 0, compressed.stderr
        assert json.loads(compressed.stdout)["status"] == "published"
        assert not source.exists() and pathlib.Path(str(source) + ".zst").is_file()
        assert keep(home1, "keep_set", first["target"])["status"] == "confirmed"
        assert keep(home2, "keep_list")["thread_ids"] == []
        assert keep(home2, "keep_set", first["target"])["status"] == "refused"
        assert keep(home1, "keep_list")["thread_ids"] == [first["target"]]
        other_dir = home2 / ".codex-session-gc"
        other_dir.mkdir(mode=0o700)
        os.link(home1 / ".codex-session-gc/keep.sqlite3", other_dir / "keep.sqlite3")
        assert keep(home1, "keep_list")["status"] == "refused"
        assert keep(home2, "keep_list")["status"] == "refused"
        assert keep(home1, "keep_clear", first["target"])["status"] == "refused"
        (other_dir / "keep.sqlite3").unlink()
        assert keep(home1, "keep_list")["thread_ids"] == [first["target"]]
        assert keep(home1, "keep_clear", first["target"], sqlite_home=str(home2))["status"] == "refused"
        pathlib.Path(first["target_path"] + ".zst").unlink()
        assert keep(home1, "keep_clear", first["target"])["status"] == "confirmed"
        assert keep(home1, "keep_set", first["target"])["status"] == "refused"
        assert keep(home1, "keep_set", first["main"])["status"] == "confirmed"
        with sqlite3.connect(home1 / ".codex-session-gc/keep.sqlite3") as db:
            db.execute("CREATE TRIGGER sqliteXhidden AFTER INSERT ON kept_threads BEGIN SELECT 1; END")
        assert keep(home1, "keep_list")["status"] == "refused"
        assert keep(home1, "keep_clear", first["target"])["status"] == "refused"


def simultaneous_keep_and_cleanup():
    with tempfile.TemporaryDirectory(prefix="csgc-keep-race-") as temporary:
        base = pathlib.Path(temporary)
        manifest, _, request, aid, uid, payload = b.prepare(base)
        home = pathlib.Path(request["codex_home"])
        target = manifest["target"]
        b.refresh(request)
        barrier = threading.Barrier(2)

        def mark():
            barrier.wait(timeout=10)
            return keep(home, "keep_set", target)

        def cleanup():
            barrier.wait(timeout=10)
            return s.dispatch(request, aid, uid, payload)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            mark_future = executor.submit(mark)
            cleanup_future = executor.submit(cleanup)
            marked, outcome = mark_future.result(timeout=40), cleanup_future.result(timeout=40)
        assert marked["status"] in ("confirmed", "refused"), marked
        assert outcome["status"] in ("confirmed", "refused"), outcome
        if marked["status"] == "confirmed":
            assert outcome["status"] == "refused", (marked, outcome)
            assert keep(home, "keep_list")["thread_ids"] == [target]
            assert pathlib.Path(manifest["target_path"]).exists()
        else:
            assert outcome["status"] == "confirmed", (marked, outcome)
            assert keep(home, "keep_list")["thread_ids"] == []
            assert not pathlib.Path(manifest["target_path"]).exists()


if __name__ == "__main__":
    exercise(True)
    exercise(False)
    separate_homes()
    simultaneous_keep_and_cleanup()
    print(json.dumps({"status": "passed", "scope": "disposable native Keep and cleanup fixtures", "native_sha256": s.NATIVE_DIGEST}))
