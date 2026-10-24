"""Isolated native identity, SQL ownership and bounded-read cases.

Disposable storage exercises exact-ID admission using SQL and writer ownership.
History-body timestamps do not establish current activity.
"""
import concurrent.futures
import fcntl
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("closed_batch", ROOT / "native/helper/tests/closed_work_batch.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
s = b.s
RESULTS = []
Popen = subprocess.Popen


class MeasuredPopen(Popen):
    """Read Linux's completed-child counters before waitpid releases the zombie."""
    def communicate(self, input=None, timeout=None):
        if "codex_session_gc.execution.owned_launch" not in self.args:
            return super().communicate(input, timeout)
        self.stdin.write(input)
        self.stdin.close()
        stdout, stderr = self.stdout.read(), self.stderr.read()
        os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT)
        self.kernel_io = {
            key: int(value) for key, value in
            (line.split(":", 1) for line in pathlib.Path(f"/proc/{self.pid}/io").read_text().splitlines())
        }
        self.wait(timeout=timeout)
        return stdout, stderr


upgrade = b.upgrade

def targets(request):
    b.refresh(request)


def case(name, schema_version=61, body_size=None):
    with tempfile.TemporaryDirectory(prefix="cw-direct-") as temporary:
        directory = pathlib.Path(temporary)
        manifest, unrelated, request, aid, uid, payload = b.prepare(directory, schema_version=schema_version)
        home = pathlib.Path(manifest["home"])
        root, child = manifest["main"], manifest["target"]
        path = pathlib.Path(manifest["target_path"])
        request["version"] = 6
        request["deletion_scope"] = [child]
        if schema_version == 61:
            upgrade(home)
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO guardian_review_feedback VALUES(?,?,?)", ("retained", root, b"preserve"))
                db.execute("INSERT INTO thread_read_receipts VALUES(?,?,?,?)", (root, None, "preserve", None))
        if body_size is not None:
            # ftruncate creates a hole; it does not write body_size bytes.
            with path.open("r+b") as stream:
                stream.truncate(body_size)
            sparse_bytes = path.stat().st_blocks * 512
        targets(request)
        peer = None
        if name in ("closed-status", "parent-change"):
            with sqlite3.connect(home / "state_5.sqlite") as db:
                if name == "closed-status":
                    db.execute("UPDATE thread_spawn_edges SET status='open' WHERE child_thread_id=?", (child,))
                else:
                    db.execute("UPDATE thread_spawn_edges SET parent_thread_id=? WHERE child_thread_id=?", (unrelated["id"], child))
        if name == "changed-header":
            b.edit_header(path, lambda meta: meta.update(agent_role="changed"))
        if name == "replaced-file":
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data)
        if name == "changed-body-stat":
            with path.open("ab") as stream:
                stream.write(b"\nchanged\n")
        if name == "missing-retained":
            missing = str(uuid.uuid4())
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'closed')", (root, missing))
            request["ownership_scope"]["thread_ids"].append(missing)
            request["guard_paths"].append(str(home / "thread-writer-locks" / f"{missing}.lock"))
            request["guard_paths"].sort()
            request["databases"] = b.bounds(home, {child}, root)
        if name == "retained-provider":
            b.edit_header(pathlib.Path(unrelated["path"]), lambda meta: meta.update(history_mode="paginated", history_base={"thread_id": child, "end_ordinal_exclusive": 2, "end_byte_offset": path.stat().st_size}))
        if name == "retained-header-missing":
            pathlib.Path(unrelated["path"]).write_text("{}\n" + " " * 131072)
        if name == "oversized-header":
            path.write_bytes(b" " * 1048577 + b"\n")
            targets(request)
        if name == "effect-overrun":
            bound = next(item for item in request["databases"] if item["path"].endswith("state_5.sqlite"))
            bound["rows"]["threads"] = []
        if name in ("selected-guardian", "selected-receipt"):
            with sqlite3.connect(home / "state_5.sqlite") as db:
                if name == "selected-guardian":
                    db.execute("INSERT INTO guardian_review_feedback VALUES(?,?,?)", ("selected", child, b"protect"))
                else:
                    db.execute("INSERT INTO thread_read_receipts VALUES(?,?,?,?)", (child, None, "protect", None))
        if name == "retained-writer":
            peer = b.peer("hold-guard", home, root)
        # Do not hash historical bodies even in the test oracle.
        before_sql = s.sql_snapshot(request)
        before_files = {str(p): (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                        for p in home.rglob("*.jsonl")}
        if name.startswith("late-"):
            db = sqlite3.connect(home / "state_5.sqlite", isolation_level=None)
            db.execute("BEGIN IMMEDIATE")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(s.dispatch, request, aid, uid, payload)
                deadline = time.monotonic() + 10
                held = False
                while time.monotonic() < deadline and not pending.done():
                    lock = home / "thread-writer-locks" / f"{child}.lock"
                    if lock.exists():
                        with lock.open("rb") as probe:
                            try:
                                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                held = True
                        if held:
                            break
                    time.sleep(.001)
                assert held, "native selected ownership not observed"
                if name == "late-status":
                    db.execute("UPDATE thread_spawn_edges SET status='open' WHERE child_thread_id=?", (child,))
                else:
                    db.execute("INSERT INTO thread_read_receipts VALUES(?,?,?,?)", (child, None, "late", None))
                db.commit()
                db.close()
                before_sql = s.sql_snapshot(request)
                outcome = pending.result(timeout=30)
        else:
            with patch.object(s.subprocess, "Popen", MeasuredPopen):
                outcome = s.dispatch(request, aid, uid, payload)
            kernel_io = s.CHILDREN[-1].kernel_io
        if peer:
            b.stop(peer)
        confirmed = name in ("eligible", "missing-retained", "io-small", "io-large")
        assert outcome["status"] == ("confirmed" if confirmed else "refused"), (name, outcome)
        if not confirmed:
            assert s.sql_snapshot(request) == before_sql, (name, "SQL changed")
            assert {str(p): (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                    for p in home.rglob("*.jsonl")} == before_files, (name, "histories changed")
        else:
            assert not path.exists() and pathlib.Path(manifest["main_path"]).exists()
            with sqlite3.connect(home / "state_5.sqlite") as db:
                assert db.execute("SELECT id FROM threads WHERE id=?", (root,)).fetchone()
                if schema_version == 61:
                    assert db.execute("SELECT thread_id,record FROM guardian_review_feedback").fetchall() == [(root, b"preserve")]
                    assert db.execute("SELECT * FROM thread_read_receipts").fetchall() == [(root, None, "preserve", None)]
        result = dict(case=name, schema=schema_version, outcome=outcome,
                      native_binary_sha256=s.NATIVE_DIGEST)
        if not name.startswith("late-"):
            result["kernel_io"] = kernel_io
        if body_size is not None:
            result.update(body_size=body_size, sparse_allocated_bytes=sparse_bytes)
        RESULTS.append(result)
        print(json.dumps(result), flush=True)
        return result


if __name__ == "__main__":
    case("eligible", 61)
    for name in ("closed-status", "parent-change", "changed-header", "replaced-file",
                 "changed-body-stat", "retained-provider", "retained-header-missing",
                 "oversized-header", "effect-overrun", "retained-writer", "missing-retained"):
        case(name)
    for name in ("selected-guardian", "selected-receipt", "late-receipt"):
        case(name, 61)
    case("late-status")
    small = case("io-small", body_size=128 * 1024)
    large = case("io-large", body_size=4 * 1024**3)
    assert small["kernel_io"]["rchar"] < 16 * 1024**2
    assert large["kernel_io"]["rchar"] < 16 * 1024**2
    assert abs(large["kernel_io"]["rchar"] - small["kernel_io"]["rchar"]) < 65536
    assert large["sparse_allocated_bytes"] < 1024**2
    receipt = os.environ.get("GC_NATIVE_DIRECT_RECEIPT")
    if receipt:
        pathlib.Path(receipt).write_text(json.dumps(dict(cases=RESULTS, native_binary_sha256=s.NATIVE_DIGEST), indent=2) + "\n")
