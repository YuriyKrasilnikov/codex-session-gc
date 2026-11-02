"""Attachment refusal and selected-child behavior through the native wire-6 operation."""
import concurrent.futures
import fcntl
import importlib.util
import json
import pathlib
import tempfile
import sqlite3
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("closed_batch", ROOT / "native/helper/tests/closed_work_batch.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
s = b.s


def case(name):
    with tempfile.TemporaryDirectory(prefix="cw-attachment-") as temporary:
        manifest, unrelated, request, aid, uid, payload = b.prepare(pathlib.Path(temporary))
        home = pathlib.Path(manifest["home"])
        root, child = manifest["main"], manifest["target"]
        path = pathlib.Path(manifest["target_path"])
        if name in ("child-only", "child-then-parent", "current-sql-missing", "current-sql-conflict", "invalid-parent"):
            request["deletion_scope"] = [child]
        if name in ("sql-only", "late-sql"):
            if name == "sql-only":
                with s.sqlite_context(home / "state_5.sqlite") as db:
                    db.execute("UPDATE threads SET attached_thread_parent_id=? WHERE id=?", (root, unrelated["id"]))
        else:
            parent = child if name == "invalid-parent" else root
            b.edit_header(path, lambda meta: meta.update(attached_thread=True, forked_from_id=parent))
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET attached_thread_parent_id=? WHERE id=?",
                           (None if name in ("header-only", "current-sql-missing") else unrelated["id"] if name == "current-sql-conflict" else parent, child))
        if name in ("archived", "archived-zstd"):
            archive = home / "archived_sessions" / path.name
            path.rename(archive)
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET rollout_path=?,archived=1 WHERE id=?", (str(archive), child))
            path = archive
            if name == "archived-zstd":
                from compression import zstd
                compressed = pathlib.Path(str(path) + ".zst")
                compressed.write_bytes(zstd.compress(path.read_bytes()))
                path.unlink()
                path = compressed
        b.refresh(request)
        if name == "changed-header":
            b.edit_header(path, lambda meta: meta.update(attached_thread=False))
        before = s.sql_snapshot(request)
        peer = b.peer("hold-guard", home, root) if name == "parent-contention" else None
        if name == "late-sql":
            db = sqlite3.connect(home / "state_5.sqlite", isolation_level=None)
            db.execute("BEGIN IMMEDIATE")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(s.dispatch, request, aid, uid, payload)
                deadline = time.monotonic() + 10
                held = False
                while time.monotonic() < deadline and not pending.done():
                    lock = home / "thread-writer-locks" / f"{root}.lock"
                    if lock.exists():
                        with lock.open("rb") as probe:
                            try:
                                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                held = True
                    if held:
                        break
                    time.sleep(.001)
                assert held, "parent writer ownership not observed"
                db.execute("UPDATE threads SET attached_thread_parent_id=? WHERE id=?", (root, unrelated["id"]))
                db.commit()
                db.close()
                before = s.sql_snapshot(request)
                outcome = pending.result(timeout=30)
        else:
            outcome = s.dispatch(request, aid, uid, payload)
        if peer is not None:
            b.stop(peer)
        expected = "confirmed" if name in ("child-only", "child-then-parent") else "refused"
        assert outcome["status"] == expected, (name, outcome)
        if expected == "refused":
            assert s.sql_snapshot(request) == before, name
            assert path.exists() and pathlib.Path(manifest["main_path"]).exists(), name
        else:
            assert not path.exists() and pathlib.Path(manifest["main_path"]).exists()
            if name == "child-then-parent":
                aid, uid = s.j.AttemptId(uuid.uuid4()), s.j.UnitId(uuid.uuid4())
                receipt = pathlib.Path(temporary) / "parent-journal.sqlite"
                s.j.create(receipt, s.j.StartEnvelope(aid, payload, payload, payload, payload),
                           (s.j.Unit(uid, 0, payload),))
                request.update(journal=str(receipt), attempt_id=str(aid.value), unit_id=str(uid.value),
                               deletion_scope=[root], ownership_scope=dict(root_thread_id=root, thread_ids=[root]),
                               guard_paths=[entry for entry in request["guard_paths"] if not entry.endswith(f"/{child}.lock")])
                b.refresh(request)
                parent_outcome = s.dispatch(request, aid, uid, payload)
                assert parent_outcome["status"] == "confirmed", parent_outcome
                assert not pathlib.Path(manifest["main_path"]).exists()
        print(json.dumps({"case": name, "status": outcome["status"]}), flush=True)


if __name__ == "__main__":
    for name in ("selected-parent-and-child", "child-only", "child-then-parent", "header-only", "sql-only", "late-sql", "parent-contention", "archived", "archived-zstd", "current-sql-missing", "current-sql-conflict", "invalid-parent", "changed-header"):
        case(name)
