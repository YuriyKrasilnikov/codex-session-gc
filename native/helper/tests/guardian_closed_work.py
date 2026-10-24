"""Bounded wire-6 guardian association and retained-writer qualification.

Only disposable native-produced homes are used. Guard declarations authorize
writer coordination, never an additional deletion target or SQL effect.
"""
import concurrent.futures
from contextlib import closing
from datetime import UTC, datetime
import fcntl
import importlib.util
import json
import os
import pathlib
import sqlite3
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("direct_cases", ROOT / "native/helper/tests/direct_closed_work.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
b, s = d.b, d.s
RESULTS = []


def locked(path):
    if not path.exists():
        return False
    with path.open("rb") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def case(name, schema=61):
    with tempfile.TemporaryDirectory(prefix="gc-guardian-") as temporary:
        directory = pathlib.Path(temporary)
        manifest, guardian, request, aid, uid, payload = b.prepare(directory)
        home = pathlib.Path(manifest["home"])
        parent = manifest["target"]
        guardian_path = pathlib.Path(guardian["path"])
        source = {"subagent": {"other": "guardian"}}
        b.edit_header(guardian_path, lambda meta: meta.update(source=source, parent_thread_id=parent))
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
            db.execute("UPDATE threads SET source=? WHERE id=?", (json.dumps(source), guardian["id"]))
        if schema == 61:
            d.upgrade(home)
        request["version"] = 6
        request["deletion_scope"] = [parent]
        d.targets(request)
        guardian_lock = home / "thread-writer-locks" / f'{guardian["id"]}.lock'
        if name == "python-prepared":
            from codex_session_gc.domain import ThreadId, UtcCutoff
            from codex_session_gc.execution import closed_work_request as collector
            from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
            from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
            from codex_session_gc.planning.closed_work import select_closed_work
            # A real ThreadSpawn source lets the public scoped-facts reader classify
            # the selected agent; the guardian itself remains outside spawn closure.
            spawned = {"subagent": {"thread_spawn": {"parent_thread_id": manifest["main"], "depth": 1}}}
            b.edit_header(pathlib.Path(manifest["target_path"]), lambda meta: meta.update(source=spawned))
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
                db.execute("UPDATE threads SET source=? WHERE id=?", (json.dumps(spawned), parent))
            cutoff = UtcCutoff(datetime(2026, 8, 1, tzinfo=UTC))
            roots = frozenset({ThreadId.parse(manifest["main"])})
            chosen = frozenset({ThreadId.parse(parent)})
            limits = collector.CollectionLimits(4_000_000, 4_000_000, 32_000_000, 100_000, 1_000_000, 30)
            facts = read_closed_work_facts(home, cutoff, roots, chosen, limits)
            selected = select_closed_work(facts.snapshot, cutoff, roots, selected_threads=chosen)
            prepared = collector.prepare_request(facts, selected, cutoff=cutoff,
                codex_home=home, sqlite_home=home, journal=pathlib.Path(request["journal"]),
                attempt_id=aid, unit_id=uid, helper=load_closed_work_contract(),
                default_provider="openai", limits=limits)
            request = json.loads(prepared.data)
            assert str(guardian_lock) in request["guard_paths"]
            assert request["deletion_scope"] == [parent]
        if name not in ("undeclared", "python-prepared", "wire3-guardian"):
            request["guard_paths"].append(str(guardian_lock))
            request["guard_paths"].sort()
        if name == "wire3-guardian":
            request["version"] = 3
            b.refresh(request)
        if name == "ordinary-child":
            ordinary = {"subagent": {"other": "ordinary"}}
            b.edit_header(guardian_path, lambda meta: meta.update(source=ordinary))
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
                db.execute("UPDATE threads SET source=? WHERE id=?", (json.dumps(ordinary), guardian["id"]))
            request["guard_paths"].remove(str(guardian_lock))
        if name == "physical-provider":
            b.edit_header(guardian_path, lambda meta: meta.update(history_mode="paginated",
                history_base=dict(thread_id=parent, end_ordinal_exclusive=2,
                                  end_byte_offset=pathlib.Path(manifest["target_path"]).stat().st_size)))
        if name == "extra-guard":
            request["guard_paths"].append(str(home / "thread-writer-locks" / "00000000-0000-0000-0000-000000000123.lock"))
        if name == "invalid-guard":
            request["guard_paths"].append(str(home / "guardian-unrelated.lock"))
        if name.startswith("sql-"):
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
                if name == "sql-source":
                    db.execute("UPDATE threads SET source='cli' WHERE id=?", (guardian["id"],))
                elif name == "sql-path":
                    db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(guardian_path)+".changed", guardian["id"]))
                elif name == "sql-history":
                    db.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (guardian["id"],))
                elif name == "sql-provider":
                    db.execute("UPDATE threads SET model_provider='changed' WHERE id=?", (guardian["id"],))
                elif name == "sql-role":
                    db.execute("UPDATE threads SET agent_role='changed' WHERE id=?", (guardian["id"],))
                elif name == "sql-archive":
                    db.execute("UPDATE threads SET archived=1 WHERE id=?", (guardian["id"],))
                elif name == "sql-absent":
                    db.execute("DELETE FROM threads WHERE id=?", (guardian["id"],))
                elif name == "sql-edge":
                    db.execute("INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'closed')", (guardian["id"], guardian["id"]))
        peer = None
        if name == "busy":
            peer = b.peer("hold-guard", home, guardian["id"])
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
            guardian_row = db.execute("SELECT * FROM threads WHERE id=?", (guardian["id"],)).fetchall()
        guardian_bytes = guardian_path.read_bytes()
        before_sql = s.sql_snapshot(request)
        before_files = s.history_snapshot(home)
        writer_seen = False
        if name in ("held-through-commit", "late-sql-source", "late-header"):
            db = sqlite3.connect(home / "state_5.sqlite", isolation_level=None)
            if name == "held-through-commit":
                assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
                db.execute("BEGIN")
                db.execute("SELECT id FROM threads").fetchall()
            else:
                db.execute("BEGIN IMMEDIATE")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(s.dispatch, request, aid, uid, payload)
                deadline = time.monotonic() + 10
                if name == "held-through-commit":
                    while pathlib.Path(manifest["target_path"]).exists() and not pending.done():
                        assert time.monotonic() < deadline, "selected unlink was not reached"
                        time.sleep(.001)
                    writer_seen = locked(guardian_lock)
                    assert writer_seen and not pending.done(), "guardian guard was not retained through pending commit"
                    db.rollback()
                else:
                    while not locked(guardian_lock) and not pending.done():
                        assert time.monotonic() < deadline, "guardian writer ownership not observed"
                        time.sleep(.001)
                    writer_seen = locked(guardian_lock)
                    assert writer_seen, "guardian writer ownership not observed"
                    if name == "late-sql-source":
                        db.execute("UPDATE threads SET source='cli' WHERE id=?", (guardian["id"],))
                    else:
                        b.edit_header(guardian_path, lambda meta: meta.update(agent_role="changed-after-lock"))
                        before_files = s.history_snapshot(home)
                    db.commit()
                    before_sql = s.sql_snapshot(request)
                db.close()
                outcome = pending.result(timeout=30)
        else:
            outcome = s.dispatch(request, aid, uid, payload)
        if peer:
            b.stop(peer)
        confirmed = name in ("retained-resume", "held-through-commit", "python-prepared")
        assert outcome["status"] == ("confirmed" if confirmed else "refused"), (name, outcome)
        if not confirmed:
            assert s.sql_snapshot(request) == before_sql, (name, "SQL effects on refusal")
            assert s.history_snapshot(home) == before_files, (name, "history effects on refusal")
            assert outcome["removed_rollouts"] == [] and outcome["committed_databases"] == []
        else:
            assert not pathlib.Path(manifest["target_path"]).exists()
            assert guardian_path.read_bytes() == guardian_bytes
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
                assert db.execute("SELECT * FROM threads WHERE id=?", (guardian["id"],)).fetchall() == guardian_row
                assert db.execute("SELECT id FROM threads WHERE id=?", (parent,)).fetchone() is None
            # Independent existing native read/resume of the guardian's own Legacy history.
            loaded = b.native("sample-history", home, guardian_path)
            assert loaded.returncode == 0, loaded.stderr
            resumed = b.native("resume", home, guardian["id"], guardian_path)
            assert resumed.returncode == 0, resumed.stderr
        if name == "busy":
            assert "active writer" in outcome["detail"]
        if name == "ordinary-child":
            assert "retained ancestry" in outcome["detail"]
        if name == "physical-provider":
            assert "physical provider" in outcome["detail"]
        if name in ("undeclared", "extra-guard", "invalid-guard"):
            assert not guardian_lock.exists(), "supplemental lock was created before exact guard approval"
        result = dict(case=name, schema=schema, outcome=outcome, guardian_writer_observed=writer_seen,
                      native_binary_sha256=s.NATIVE_DIGEST)
        RESULTS.append(result)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    case("retained-resume", 61)
    case("python-prepared", 61)
    for name in ("busy", "held-through-commit", "undeclared", "extra-guard", "invalid-guard",
                 "ordinary-child", "wire3-guardian", "physical-provider", "sql-source", "sql-path", "sql-history",
                 "sql-provider", "sql-role", "sql-archive", "sql-absent", "sql-edge",
                 "late-sql-source", "late-header"):
        case(name)
    receipt = os.environ.get("GC_NATIVE_GUARDIAN_RECEIPT")
    if receipt:
        pathlib.Path(receipt).write_text(json.dumps(dict(cases=RESULTS, native_binary_sha256=s.NATIVE_DIGEST), indent=2)+"\n")
