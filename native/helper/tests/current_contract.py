"""Current native wire/schema admission and exact MAIN-root effects.

Every home is produced afresh by the native fixture; user storage is never read.
"""
from contextlib import closing
import importlib.util
import hashlib
import concurrent.futures
import sqlite3
import json
import os
import pathlib
import tempfile
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("current_batch", ROOT / "native/helper/tests/closed_work_batch.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
s = b.s


def admission_case(name):
    with tempfile.TemporaryDirectory(prefix="gc-current-admission-") as temporary:
        manifest, unrelated, request, aid, uid, payload = b.prepare(
            pathlib.Path(temporary), schema_version=59 if name == "schema59" else 61)
        home = pathlib.Path(manifest["home"])
        b.refresh(request)
        if name == "expanded-ownership":
            request["ownership_scope"]["thread_ids"].append(unrelated["id"])
            request["guard_paths"].append(str(home / "thread-writer-locks" / f'{unrelated["id"]}.lock'))
            request["guard_paths"].sort()
        elif name == "contracted-ownership":
            request["ownership_scope"]["thread_ids"].remove(manifest["target"])
        elif name == "duplicate-ownership":
            request["ownership_scope"]["thread_ids"].append(manifest["main"])
        elif name == "wrong-deletion":
            request["deletion_scope"] = [manifest["target"]]
        elif name == "missing-root-guard":
            request["guard_paths"].remove(str(home / "thread-writer-locks" / f'{manifest["main"]}.lock'))
        elif name == "leaf-as-root":
            request["ownership_scope"]["root_thread_id"] = manifest["target"]
        elif name == "root-cycle":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'closed')", (manifest["target"], manifest["main"]))
        elif name.startswith("wire"):
            request["version"] = int(name.removeprefix("wire"))
        elif name == "unknown-request-field":
            request["obsolete"] = True
        elif name == "unknown-target-field":
            request["targets"][0]["rollout_sha256"] = [0] * 32
        elif name == "missing-identity":
            del request["targets"][0]["rollouts"][0]["physical_identity"]
        elif name == "null-header":
            request["targets"][0]["rollouts"][0]["header_sha256"] = None
        elif name == "selected-root-feedback":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO guardian_review_feedback VALUES(?,?,?)", ("selected-root", manifest["main"], b"preserve"))
        elif name == "selected-root-receipt":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("INSERT INTO thread_read_receipts VALUES(?,?,?,?)", (manifest["main"], None, "preserve", None))
        elif name == "migration-success":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE _sqlx_migrations SET success=2 WHERE version=61")
        elif name == "archive-integer":
            child = manifest["target"]
            original = pathlib.Path(manifest["target_path"])
            archived = home / "archived_sessions" / original.name
            original.rename(archived)
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET archived=2,rollout_path=? WHERE id=?", (str(archived), child))
            b.refresh(request)
        elif name == "migration-checksum":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE _sqlx_migrations SET checksum=? WHERE version=61", (b"invalid",))
        elif name == "schema62":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("CREATE TABLE unqualified(id TEXT)")
        before_sql, before_files = s.sql_snapshot(request), s.history_snapshot(home)
        before_guards = set((home / "thread-writer-locks").iterdir())
        result = s.dispatch(request, aid, uid, payload)
        assert result["status"] == "refused", (name, result)
        if name == "expanded-ownership":
            assert set(request["deletion_scope"]) == {manifest["main"], manifest["target"]}
            assert {target["thread_id"] for target in request["targets"]} == set(request["deletion_scope"])
            assert "ownership spawn closure changed" in result["detail"], result
        assert result["removed_rollouts"] == result["committed_databases"] == []
        assert s.sql_snapshot(request) == before_sql
        assert s.history_snapshot(home) == before_files
        if name.startswith("wire") or name in ("unknown-request-field", "unknown-target-field", "missing-identity", "null-header"):
            assert set((home / "thread-writer-locks").iterdir()) == before_guards
        return dict(case=name, outcome=result, native_binary_sha256=s.NATIVE_DIGEST)


def malformed_cases():
    results = []
    for name, raw in (("invalid-json", b"{"), ("array-json", b"[]"),
                      ("null-json", b"null"), ("missing-fields", b'{"version":5}'),
                      ("request-byte-budget", b" " * (16 * 1024 * 1024 + 1))):
        process = subprocess.run([s.BIN], input=raw, capture_output=True, timeout=30)
        outcome = json.loads(process.stdout)
        assert process.returncode == 1 and not process.stderr, (name, process.stderr)
        assert outcome["status"] == "refused", (name, outcome)
        assert outcome["request_sha256"] == list(hashlib.sha256(raw).digest())
        assert outcome["removed_rollouts"] == outcome["committed_databases"] == []
        results.append(dict(case=name, outcome=outcome, native_binary_sha256=s.NATIVE_DIGEST))
    return results


def commit_unknown_case():
    """A real SQLite commit refusal leaves an honest unknown journal outcome."""
    with tempfile.TemporaryDirectory(prefix="gc-current-commit-unknown-") as temporary:
        manifest, unrelated, request, aid, uid, payload = b.prepare(pathlib.Path(temporary))
        home = pathlib.Path(manifest["home"])
        b.refresh(request)
        retained = s.owner_snapshot(request, unrelated["id"])
        state = home / "state_5.sqlite"
        with closing(sqlite3.connect(state, isolation_level=None)) as reader:
            assert reader.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
            reader.execute("BEGIN")
            reader.execute("SELECT id FROM threads").fetchall()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(s.dispatch, request, aid, uid, payload).result(timeout=30)
            reader.rollback()
        assert result["status"] == "unknown", result
        assert "commit evidence is unknown" in result["detail"], result
        assert set(result["removed_rollouts"]) == {rollout["rollout_path"] for target in request["targets"] for rollout in target["rollouts"]}
        assert set(result["committed_databases"]) == {bound["path"] for bound in request["databases"] if pathlib.Path(bound["path"]) != state}
        with s.connection(state) as db:
            for identity in request["deletion_scope"]:
                assert db.execute("SELECT id FROM threads WHERE id=?", (identity,)).fetchone()
        assert not pathlib.Path(manifest["main_path"]).exists()
        assert not pathlib.Path(manifest["target_path"]).exists()
        assert s.owner_snapshot(request, unrelated["id"]) == retained
        return dict(case="real-commit-unknown", outcome=result, native_binary_sha256=s.NATIVE_DIGEST)


def replay_case():
    with tempfile.TemporaryDirectory(prefix="gc-current-replay-") as temporary:
        manifest, unrelated, request, aid, uid, payload = b.prepare(pathlib.Path(temporary))
        home = pathlib.Path(manifest["home"])
        b.refresh(request)
        first = s.dispatch(request, aid, uid, payload)
        assert first["status"] == "confirmed", first
        assert set(first) == {"request_sha256", "status", "detail", "removed_rollouts",
                              "committed_databases", "preserved_name_index"}
        before_sql, before_files = s.sql_snapshot(request), s.history_snapshot(home)
        raw = json.dumps(request, separators=(",", ":")).encode()
        second = subprocess.run([s.BIN], input=raw, capture_output=True, timeout=30)
        outcome = json.loads(second.stdout)
        assert second.returncode == 1 and outcome["status"] == "refused", outcome
        assert "terminal or reconciled" in outcome["detail"], outcome
        assert outcome["removed_rollouts"] == outcome["committed_databases"] == []
        assert s.sql_snapshot(request) == before_sql
        assert s.history_snapshot(home) == before_files
        return dict(case="terminal-replay-refused", outcome=outcome,
                    native_binary_sha256=s.NATIVE_DIGEST)


if __name__ == "__main__":
    results = malformed_cases() + [replay_case(), commit_unknown_case()]
    for result in results:
        print(json.dumps(result), flush=True)
    for name in ("wire1", "wire2", "wire3", "wire4", "wire5", "wire7", "unknown-request-field",
                 "unknown-target-field", "missing-identity", "null-header", "schema59",
                 "schema62", "migration-checksum", "migration-success", "archive-integer", "selected-root-feedback", "selected-root-receipt", "expanded-ownership", "contracted-ownership",
                 "duplicate-ownership", "wrong-deletion", "missing-root-guard", "leaf-as-root", "root-cycle"):
        result = admission_case(name)
        results.append(result)
        print(json.dumps(result), flush=True)
    # Existing independent physical-row oracle covers the actual root and each
    # domain, including queue revisions, history cache, logs, goals and memory.
    for name in ("whole-work", "board-root", "board-late-template", "board-missing-template-bound",
                 "board-wrong-template-bound", "board-old-schema", "board-extra-trigger", "sqlite-busy", "retained-child", "retained-provider",
                 "child-only", "child-board", "busy-root", "busy-child", "busy-sibling",
                 "closure-change", "bound-overrun", "archive-mismatch", "paginated",
                 "compressed-sibling", "running-memory", "expired-memory", "phase2-output",
                 "absent-temp", "absent-temp-invalid-guard", "temp-file", "temp-symlink",
                 "maintenance-busy", "zero-preserved", "zero-hardlink", "zero-symlink"):
        result = b.run_case(name)
        results.append(result)
        print(json.dumps(result), flush=True)
    receipt = os.environ.get("GC_NATIVE_CURRENT_RECEIPT")
    if receipt:
        pathlib.Path(receipt).write_text(json.dumps(dict(cases=results,
            native_binary_sha256=s.NATIVE_DIGEST), indent=2) + "\n")
