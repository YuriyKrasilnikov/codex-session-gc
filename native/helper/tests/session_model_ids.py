"""Controlled session alias effects against the native fixture, not a stock producer claim."""

import importlib.util
import json
import os
import pathlib
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "alias_batch", ROOT / "native/helper/tests/closed_work_batch.py"
)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)
s = batch.s

# The new native fixture initializes the target schema itself. Keep the old
# profile oracle separate and extend its independent row selection here.
batch.upgrade = lambda home: None
base_bounds = batch.bounds


def alias_bounds(home, selected, root, history_ids=None):
    result = base_bounds(home, selected, root, history_ids)
    values = sorted(selected)
    for bound in result:
        if pathlib.Path(bound["path"]).name == "state_5.sqlite":
            with s.connection(bound["path"]) as db:
                marks = ",".join("?" for _ in values)
                bound["rows"]["session_model_ids"] = [
                    row[0]
                    for row in db.execute(
                        f"SELECT rowid FROM session_model_ids WHERE session_id IN ({marks}) ORDER BY rowid",
                        values,
                    )
                ]
    return result


batch.bounds = alias_bounds


def run_case(case):
    with tempfile.TemporaryDirectory(prefix="gc-native-alias-") as temporary:
        manifest, unrelated, request, aid, uid, payload = batch.prepare(pathlib.Path(temporary))
        home = pathlib.Path(manifest["home"])
        root = manifest["main"]
        selected_alias = manifest["target"] if case == "worker" else root
        if case == "worker":
            request["deletion_scope"] = [selected_alias]
        retained = unrelated["id"]
        retained_path = pathlib.Path(unrelated["path"])
        retained_session = root if case == "worker" else str(uuid.uuid4())
        if case in ("mixed-empty", "mixed-nonempty"):
            retained_session = manifest["target"]
        with s.sqlite_context(home / "state_5.sqlite") as db:
            db.execute("INSERT INTO session_model_ids VALUES(?,?,?,?,?)", (selected_alias, "window", "controlled", 0, "{}"))
            if case != "mixed-empty":
                db.execute("INSERT INTO session_model_ids VALUES(?,?,?,?,?)", (retained_session, "window", "retained", 0, "{}"))
        # Keep a non-root retained header's explicit session independent of its
        # own thread ID, except where the control tests legacy ambiguity.
        header_session = str(uuid.uuid4()) if case == "worker" else retained_session
        batch.edit_header(retained_path, lambda meta: meta.update(session_id=header_session))
        if case in ("shared", "orphan"):
            batch.edit_header(retained_path, lambda meta: meta.update(session_id=root))
        if case == "orphan":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("DELETE FROM threads WHERE id=?", (retained,))
        elif case == "legacy":
            batch.edit_header(retained_path, lambda meta: meta.update(
                source={"subagent": {"other": "controlled"}}, session_id=retained
            ))
        elif case == "sql-missing":
            retained_path.unlink()
        elif case == "empty":
            retained_path.write_bytes(b"")
        batch.refresh(request)
        if case == "late":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO session_model_ids VALUES(?,?,?,?,?)", (root, "window", "late", 1, None))
        elif case == "unrelated-late":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO session_model_ids VALUES(?,?,?,?,?)", (retained_session, "item", "late-retained", 0, "{}"))
        elif case == "membership-changed":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE session_model_ids SET session_id=? WHERE session_id=?", (str(uuid.uuid4()), root))
        before_sql = s.sql_snapshot(request)
        before_history = s.history_snapshot(home)
        outcome = s.dispatch(request, aid, uid, payload)
        if case in ("positive", "worker", "unrelated-late", "mixed-empty"):
            assert outcome["status"] == "confirmed", outcome
            with s.connection(home / "state_5.sqlite") as db:
                rows = db.execute("SELECT session_id,external_key FROM session_model_ids ORDER BY external_key").fetchall()
                expected = [] if case == "mixed-empty" else [(retained_session, "retained")]
                if case == "unrelated-late":
                    expected.insert(0, (retained_session, "late-retained"))
                assert rows == expected, rows
                if case == "mixed-empty":
                    assert json.loads(retained_path.read_text().splitlines()[0])["payload"]["session_id"] == retained_session
                if case in ("positive", "worker", "unrelated-late"):
                    retained_header_path = pathlib.Path(manifest["main_path"]) if case == "worker" else retained_path
                    header = json.loads(retained_header_path.read_text().splitlines()[0])["payload"]
                    assert header["session_id"] == retained_session
                    assert db.execute("SELECT external_key FROM session_model_ids WHERE session_id=? AND kind=?", (header["session_id"], "window")).fetchall() == [("retained",)]
        else:
            assert outcome["status"] == "refused", outcome
            assert s.sql_snapshot(request) == before_sql
            assert s.history_snapshot(home) == before_history
        return {"case": case, "status": outcome["status"], "detail": outcome["detail"],
                "native_binary_sha256": s.NATIVE_DIGEST,
                "scope": "controlled native fixtures; not a stock producer or installed-client witness"}


if __name__ == "__main__":
    names = os.environ.get("GC_NATIVE_CASES", "positive,worker,unrelated-late,mixed-empty,mixed-nonempty,shared,orphan,legacy,sql-missing,empty,late,membership-changed").split(",")
    for name in names:
        print(json.dumps(run_case(name), separators=(",", ":")), flush=True)
