"""Exact supplemental state effects and recorder-backed Guardian admission.

Synthetic disposable storage exercises writer ownership and retained data.
The complete Core lifecycle is outside these storage tests.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "supplemental_batch", ROOT / "native/helper/tests/closed_work_batch.py"
)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
s = b.s
SOURCE = {"subagent": {"other": "guardian"}}
TABLES = ("guardian_review_feedback", "thread_read_receipts")
RESULTS = []


def guardian(home, parent):
    made = b.native("add-member", home)
    assert made.returncode == 0, made.stderr
    result = json.loads(made.stdout)
    b.edit_header(
        pathlib.Path(result["path"]),
        lambda meta: meta.update(
            source=SOURCE, parent_thread_id=parent, timestamp="2026-06-01T00:00:00Z"
        ),
    )
    with s.sqlite_context(home / "state_5.sqlite") as db:
        db.execute(
            "UPDATE threads SET source=?, created_at=?, updated_at=?, created_at_ms=?, updated_at_ms=? WHERE id=?",
            (
                json.dumps(SOURCE),
                1780272000,
                1780272000,
                1780272000000,
                1780272000000,
                result["id"],
            ),
        )
    return result


def seed(home, owners):
    with s.sqlite_context(home / "state_5.sqlite") as db:
        for index, identity in enumerate(owners):
            for suffix in ("a", "b"):
                opaque = b"\x00\xffopaque-not-json:" + str(index).encode() + suffix.encode()
                db.execute(
                    "INSERT INTO guardian_review_feedback VALUES(?,?,?)",
                    (identity + suffix, identity, opaque),
                )
            db.execute(
                "INSERT INTO thread_read_receipts VALUES(?,?,?,?)",
                (identity, "unread:" + identity, "revision:" + str(index), None),
            )


def supplemental(home):
    with s.connection(home / "state_5.sqlite") as db:
        return {
            table: db.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid').fetchall()
            for table in TABLES
        }


def retained_rows(rows, selected):
    # Both tables have thread_id as the named ownership column; feedback also has an id column.
    return {
        table: [
            row
            for row in values
            if row[2 if table == "guardian_review_feedback" else 1] not in selected
        ]
        for table, values in rows.items()
    }


def state_bound(request):
    return next(
        bound
        for bound in request["databases"]
        if pathlib.Path(bound["path"]).name == "state_5.sqlite"
    )


def start_reader(path):
    # A different process is required: same-process descriptor closes can release SQLite locks.
    code = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute('PRAGMA journal_mode=DELETE').fetchone()==('delete',); c.execute('BEGIN'); c.execute('SELECT id FROM threads').fetchall(); print('ready',flush=True); sys.stdin.readline(); c.rollback(); c.close()"
    process = s.spawn(
        [sys.executable, "-u", "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "ready", process.stderr.read()
    return process


def case(name):
    with tempfile.TemporaryDirectory(prefix="gc-supplemental-state-") as temporary:
        directory = pathlib.Path(temporary)
        manifest, independent, request, aid, uid, payload = b.prepare(directory)
        home = pathlib.Path(manifest["home"])
        associated_parent = (
            str(uuid.uuid4()) if name == "guardian-absent-parent" else manifest["main"]
        )
        selected_guardian = guardian(home, associated_parent)
        retained_guardian = guardian(home, manifest["main"])
        owners = [
            manifest["main"],
            manifest["target"],
            independent["id"],
            selected_guardian["id"],
            retained_guardian["id"],
        ]
        seed(home, owners)
        is_guardian = name.startswith("guardian-") or name.startswith("relation-")
        if is_guardian:
            selected = {selected_guardian["id"]}
            request["ownership_scope"] = {
                "root_thread_id": selected_guardian["id"],
                "thread_ids": [selected_guardian["id"]],
            }
        elif name.startswith("ordinary-whole"):
            selected = {manifest["main"], manifest["target"]}
        else:
            selected = {manifest["target"]}
        request["deletion_scope"] = sorted(selected)
        guards = [
            str(home / ".tmp/rollout-maintenance.lock"),
            str(home / "thread-writer-locks/.coordination.lock"),
        ]
        guards += [
            str(home / "thread-writer-locks" / (identity + ".lock"))
            for identity in request["ownership_scope"]["thread_ids"]
        ]
        if manifest["main"] in selected:
            guards += [
                str(home / "thread-writer-locks" / (item["id"] + ".lock"))
                for item in (selected_guardian, retained_guardian)
            ]
        request["guard_paths"] = sorted(set(guards))
        peers = []
        reader = None
        recorder_held = False
        recorder_released = False
        try:
            if name in ("guardian-writer-held", "guardian-writer-released"):
                recorder = b.peer(
                    "writer", home, selected_guardian["id"], selected_guardian["path"]
                )
                peers.append(recorder)
                assert b.command(recorder, "append") == "appended"
                recorder_held = True
                if name == "guardian-writer-released":
                    b.stop(recorder)
                    peers.remove(recorder)
                    probe = b.native("try-guard", home, selected_guardian["id"])
                    assert probe.returncode == 0 and probe.stdout.strip() == "ready", probe.stderr
                    recorder_released = True
                    parent_writer = b.peer("writer", home, manifest["main"], manifest["main_path"])
                    peers.append(parent_writer)
                    assert b.command(parent_writer, "append") == "appended"
            if name == "ordinary-whole-retained-guardian-writer":
                retained_writer = b.peer(
                    "writer", home, retained_guardian["id"], retained_guardian["path"]
                )
                peers.append(retained_writer)
                assert b.command(retained_writer, "append") == "appended"
                recorder_held = True
            if name == "ordinary-other-source":
                with s.connection(home / "state_5.sqlite") as db:
                    source = json.loads(
                        db.execute(
                            "SELECT source FROM threads WHERE id=?", (manifest["target"],)
                        ).fetchone()[0]
                    )
                assert source == {"subagent": {"other": "fixture-worker"}}, source
            if name == "ordinary-contradictory-parent":
                contradictory_source = {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": independent["id"],
                            "depth": 1,
                            "agent_path": None,
                            "agent_nickname": None,
                            "agent_role": None,
                        }
                    }
                }
                b.edit_header(
                    pathlib.Path(manifest["target_path"]),
                    lambda meta: meta.update(source=contradictory_source),
                )
                with s.sqlite_context(home / "state_5.sqlite") as db:
                    db.execute(
                        "UPDATE threads SET source=? WHERE id=?",
                        (json.dumps(contradictory_source), manifest["target"]),
                    )
            b.refresh(request)
            if name in ("feedback-underbound", "receipt-underbound"):
                table = TABLES[0 if name == "feedback-underbound" else 1]
                assert state_bound(request)["rows"][table]
                state_bound(request)["rows"][table] = []
            elif name == "retained-row-substitution":
                values = supplemental(home)["guardian_review_feedback"]
                retained = next(row[0] for row in values if row[2] not in selected)
                state_bound(request)["rows"]["guardian_review_feedback"][0] = retained
            elif name == "late-supplemental-row":
                with s.sqlite_context(home / "state_5.sqlite") as db:
                    db.execute(
                        "INSERT INTO guardian_review_feedback VALUES(?,?,?)",
                        ("late", manifest["target"], b"\xfflate"),
                    )
            target = request["targets"][0]
            if name == "relation-root-for-guardian":
                target["parent_relation"] = {"kind": "root"}
            elif name == "relation-wrong-association":
                target["parent_relation"]["associated_parent_thread_id"] = independent["id"]
            elif name == "relation-mixed":
                target["parent_relation"]["parent_thread_id"] = manifest["main"]
            elif name == "relation-unknown":
                target["parent_relation"]["kind"] = "unqualified"
            elif name == "relation-missing-parent":
                target["parent_relation"] = {"kind": "guardian_association"}
            elif name == "relation-null-parent":
                target["parent_relation"]["associated_parent_thread_id"] = None
            elif name == "relation-missing-tag":
                del target["parent_relation"]["kind"]
            elif name == "relation-retired-field":
                target["expected_spawn_parent"] = None
            elif name == "relation-root-extra-field":
                target["parent_relation"] = {
                    "kind": "root",
                    "associated_parent_thread_id": manifest["main"],
                }
            elif name == "guardian-incoming-spawn":
                with s.sqlite_context(home / "state_5.sqlite") as db:
                    db.execute(
                        "INSERT INTO thread_spawn_edges VALUES(?,?,'closed')",
                        (manifest["main"], selected_guardian["id"]),
                    )
            elif name == "guardian-sql-source-change":
                with s.sqlite_context(home / "state_5.sqlite") as db:
                    db.execute(
                        "UPDATE threads SET source='cli' WHERE id=?", (selected_guardian["id"],)
                    )
            elif name == "guardian-header-association-change":
                b.edit_header(
                    pathlib.Path(selected_guardian["path"]),
                    lambda meta: meta.update(parent_thread_id=independent["id"]),
                )
            elif name == "guardian-replaced-file":
                path = pathlib.Path(selected_guardian["path"])
                previous = path.read_bytes()
                path.unlink()
                path.write_bytes(previous)
            elif name == "guardian-self-association":
                b.edit_header(
                    pathlib.Path(selected_guardian["path"]),
                    lambda meta: meta.update(parent_thread_id=selected_guardian["id"]),
                )
                b.refresh(request)
            before_supplemental = supplemental(home)
            retained_owners = {
                identity: s.owner_snapshot(request, identity) for identity in set(owners) - selected
            }
            before_sql, before_files = s.sql_snapshot(request), s.history_snapshot(home)
            if name == "commit-unknown":
                reader = start_reader(home / "state_5.sqlite")
            outcome = s.dispatch(request, aid, uid, payload)
            if reader is not None:
                b.stop(reader)
                reader = None
            confirmed = name in (
                "ordinary-child",
                "ordinary-other-source",
                "ordinary-whole",
                "guardian-selected",
                "guardian-absent-parent",
                "guardian-writer-released",
            )
            expected = (
                "confirmed" if confirmed else "unknown" if name == "commit-unknown" else "refused"
            )
            assert outcome["status"] == expected, (name, outcome)
            if confirmed:
                assert supplemental(home) == retained_rows(before_supplemental, selected)
                assert set(outcome["removed_rollouts"]) == {
                    rollout["rollout_path"] for target in request["targets"] for rollout in target["rollouts"]
                }
                with s.connection(home / "state_5.sqlite") as db:
                    assert not db.execute(
                        "SELECT id FROM threads WHERE id IN (SELECT value FROM json_each(?))",
                        (json.dumps(sorted(selected)),),
                    ).fetchall()
            elif name == "commit-unknown":
                assert supplemental(home) == before_supplemental
                assert all(
                    all(not pathlib.Path(rollout["rollout_path"]).exists() for rollout in target["rollouts"])
                    for target in request["targets"]
                )
                assert set(outcome["committed_databases"]) == {
                    bound["path"]
                    for bound in request["databases"]
                    if pathlib.Path(bound["path"]).name != "state_5.sqlite"
                }
                with s.connection(home / "state_5.sqlite") as db:
                    assert db.execute(
                        "SELECT id FROM threads WHERE id=?", (manifest["target"],)
                    ).fetchone()
            else:
                assert outcome["removed_rollouts"] == outcome["committed_databases"] == []
                assert s.sql_snapshot(request) == before_sql
                assert s.history_snapshot(home) == before_files
            for identity, retained in retained_owners.items():
                assert s.owner_snapshot(request, identity) == retained
            if name in ("guardian-writer-held", "ordinary-whole-retained-guardian-writer"):
                assert "active writer" in outcome["detail"], outcome
            if name == "guardian-writer-released":
                assert b.command(peers[0], "append") == "appended", (
                    "associated parent writer must continue"
                )
            result = {
                "case": name,
                "outcome": outcome,
                "selected": sorted(selected),
                "retained_feedback": len(retained_rows(before_supplemental, selected)[TABLES[0]]),
                "retained_receipts": len(retained_rows(before_supplemental, selected)[TABLES[1]]),
                "actual_recorder_held": recorder_held,
                "actual_recorder_released": recorder_released,
                "native_binary_sha256": s.NATIVE_DIGEST,
            }
            RESULTS.append(result)
            print(json.dumps(result), flush=True)
        finally:
            if reader is not None:
                b.stop(reader)
            for peer in peers:
                b.stop(peer)


if __name__ == "__main__":
    for name in (
        "ordinary-child",
        "ordinary-other-source",
        "ordinary-contradictory-parent",
        "ordinary-whole",
        "ordinary-whole-retained-guardian-writer",
        "guardian-selected",
        "guardian-absent-parent",
        "feedback-underbound",
        "receipt-underbound",
        "retained-row-substitution",
        "late-supplemental-row",
        "relation-root-for-guardian",
        "relation-wrong-association",
        "relation-mixed",
        "relation-unknown",
        "relation-retired-field",
        "relation-root-extra-field",
        "relation-missing-parent",
        "relation-null-parent",
        "relation-missing-tag",
        "guardian-incoming-spawn",
        "guardian-sql-source-change",
        "guardian-header-association-change",
        "guardian-replaced-file",
        "guardian-self-association",
        "guardian-writer-held",
        "guardian-writer-released",
        "commit-unknown",
    ):
        case(name)
    receipt = os.environ.get("GC_NATIVE_SUPPLEMENTAL_RECEIPT")
    if receipt:
        pathlib.Path(receipt).write_text(
            json.dumps({"cases": RESULTS, "native_binary_sha256": s.NATIVE_DIGEST}, indent=2) + "\n"
        )
