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
import sys
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
    """Capture completed-child counters before Popen reaps the launcher."""
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.measured = any(pathlib.Path(str(arg)).name == "owned_launch.py" for arg in self.args)
        self.kernel_io = None
        if self.measured:
            self.instances.append(self)

    def _capture_if_exited(self):
        if not self.measured or self.kernel_io is not None or self.returncode is not None:
            return
        try:
            exited = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                self.kernel_io = {
                    key: int(value) for key, value in
                    (line.split(":", 1) for line in pathlib.Path(f"/proc/{self.pid}/io").read_text().splitlines())
                }
        except ChildProcessError as error:
            raise RuntimeError("owned child was reaped before I/O measurement") from error

    def poll(self):
        self._capture_if_exited()
        if self.measured and self.kernel_io is None and self.returncode is None:
            return None
        return super().poll()

    def wait(self, timeout=None):
        if self.measured and self.kernel_io is None:
            deadline = None if timeout is None else time.monotonic() + timeout
            while self.kernel_io is None and self.returncode is None:
                self._capture_if_exited()
                if self.kernel_io is not None:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                time.sleep(.001)
            if deadline is not None:
                timeout = max(0, deadline - time.monotonic())
        return super().wait(timeout=timeout)


class OneShotSubprocess:
    """Replace only one_shot's process factory; leave other producers untouched."""
    Popen = MeasuredPopen

    def __getattr__(self, name):
        return getattr(subprocess, name)


upgrade = b.upgrade


def check_reaped_child_measurement():
    """A child already reaped elsewhere must fail measurement without signalling."""
    for operation in ("poll", "wait"):
        child = MeasuredPopen([sys.executable, "-c", "pass", "owned_launch.py"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        os.waitpid(child.pid, 0)
        signals = []
        child.terminate = lambda: signals.append("terminate")
        child.kill = lambda: signals.append("kill")
        try:
            if operation == "poll":
                s.one_shot._reap(child, 1)
            else:
                child.wait(timeout=1)
        except RuntimeError as error:
            assert "reaped before I/O measurement" in str(error), error
        else:
            raise AssertionError((operation, "reaped child was accepted"))
        assert not signals, (operation, signals)
        assert child.returncode is None, (operation, child.returncode)


def targets(request):
    b.refresh(request)


def case(name, schema_version=62, body_size=None):
    with tempfile.TemporaryDirectory(prefix="cw-direct-") as temporary:
        directory = pathlib.Path(temporary)
        manifest, unrelated, request, aid, uid, payload = b.prepare(directory, schema_version=schema_version)
        home = pathlib.Path(manifest["home"])
        root, child = manifest["main"], manifest["target"]
        path = pathlib.Path(manifest["target_path"])
        request["version"] = 6
        request["deletion_scope"] = [child]
        if schema_version == 62:
            upgrade(home)
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO guardian_review_feedback VALUES(?,?,?)", ("retained", root, b"preserve"))
                db.execute("INSERT INTO thread_read_receipts VALUES(?,?,?,?)", (root, None, "preserve", None))
        if body_size is not None:
            # ftruncate creates a hole; it does not write body_size bytes.
            with path.open("r+b") as stream:
                stream.truncate(body_size)
            sparse_bytes = path.stat().st_blocks * 512
        if name in ("open-to-closed", "late-open-to-closed"):
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.execute("UPDATE thread_spawn_edges SET status='open' WHERE child_thread_id=?", (child,))
        targets(request)
        peer = None
        if name in ("closed-to-open", "open-to-closed", "unknown-status", "parent-change"):
            with sqlite3.connect(home / "state_5.sqlite") as db:
                if name in ("closed-to-open", "open-to-closed", "unknown-status"):
                    status = {"closed-to-open": "open", "open-to-closed": "closed",
                              "unknown-status": "unknown"}[name]
                    db.execute("UPDATE thread_spawn_edges SET status=? WHERE child_thread_id=?", (status, child))
                else:
                    db.execute("UPDATE thread_spawn_edges SET parent_thread_id=? WHERE child_thread_id=?", (unrelated["id"], child))
        if name == "missing-parent":
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.execute("DELETE FROM thread_spawn_edges WHERE child_thread_id=?", (child,))
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
        retained_parent = s.owner_snapshot(request, root)
        retained_other = s.owner_snapshot(request, unrelated["id"])
        unrelated_path = pathlib.Path(unrelated["path"])
        unrelated_bytes = unrelated_path.read_bytes()
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
                if name in ("late-closed-to-open", "late-open-to-closed"):
                    status = "open" if name == "late-closed-to-open" else "closed"
                    db.execute("UPDATE thread_spawn_edges SET status=? WHERE child_thread_id=?", (status, child))
                else:
                    db.execute("INSERT INTO thread_read_receipts VALUES(?,?,?,?)", (child, None, "late", None))
                db.commit()
                db.close()
                before_sql = s.sql_snapshot(request)
                outcome = pending.result(timeout=30)
        else:
            with patch.object(s.one_shot, "subprocess", OneShotSubprocess()):
                before_measured = len(MeasuredPopen.instances)
                outcome = s.dispatch(request, aid, uid, payload)
            measured = MeasuredPopen.instances[before_measured:]
            assert len(measured) == 1 and measured[0].kernel_io is not None, "launcher I/O counters unavailable"
            kernel_io = measured[0].kernel_io
        if peer:
            b.stop(peer)
        confirmed = name in ("eligible", "missing-retained", "io-small", "io-large",
                             "closed-to-open", "open-to-closed",
                             "late-closed-to-open", "late-open-to-closed")
        assert outcome["status"] == ("confirmed" if confirmed else "refused"), (name, outcome)
        if not confirmed:
            assert s.sql_snapshot(request) == before_sql, (name, "SQL changed")
            assert {str(p): (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                    for p in home.rglob("*.jsonl")} == before_files, (name, "histories changed")
        else:
            assert not path.exists() and pathlib.Path(manifest["main_path"]).exists()
            assert not any(s.owner_snapshot(request, child).values())
            assert s.owner_snapshot(request, root) == retained_parent
            assert s.owner_snapshot(request, unrelated["id"]) == retained_other
            assert unrelated_path.read_bytes() == unrelated_bytes
            with sqlite3.connect(home / "state_5.sqlite") as db:
                assert db.execute("SELECT id FROM threads WHERE id=?", (root,)).fetchone()
                if schema_version == 62:
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
    check_reaped_child_measurement()
    case("eligible", 62)
    for name in ("closed-to-open", "open-to-closed", "unknown-status", "parent-change", "missing-parent", "changed-header", "replaced-file",
                 "changed-body-stat", "retained-provider", "retained-header-missing",
                 "oversized-header", "effect-overrun", "retained-writer", "missing-retained"):
        case(name)
    for name in ("selected-guardian", "selected-receipt", "late-receipt"):
        case(name, 62)
    case("late-closed-to-open")
    case("late-open-to-closed")
    small = case("io-small", body_size=128 * 1024)
    large = case("io-large", body_size=4 * 1024**3)
    assert small["kernel_io"]["rchar"] < 16 * 1024**2
    assert large["kernel_io"]["rchar"] < 16 * 1024**2
    assert abs(large["kernel_io"]["rchar"] - small["kernel_io"]["rchar"]) < 65536
    assert large["sparse_allocated_bytes"] < 1024**2
    receipt = os.environ.get("GC_NATIVE_DIRECT_RECEIPT")
    if receipt:
        pathlib.Path(receipt).write_text(json.dumps(dict(cases=RESULTS, native_binary_sha256=s.NATIVE_DIGEST), indent=2) + "\n")
