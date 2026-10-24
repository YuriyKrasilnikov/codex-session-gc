"""Isolated wire-6/schema-61 closed-work effects through native storage and journal APIs."""

import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("native_semantic", ROOT / "native/tests/semantic.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


def native(mode, home, *args):
    return subprocess.run(
        [s.FIXTURE, mode, home, *args], capture_output=True, text=True, timeout=20
    )


def peer(mode, home, identity, path=None):
    args = [s.FIXTURE, mode, home, identity]
    if path is not None:
        args.append(path)
    process = s.spawn(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert process.stdout.readline().strip() == "ready", process.stderr.read()
    return process


def command(process, text):
    process.stdin.write(text + "\n")
    process.stdin.flush()
    return process.stdout.readline().strip()


def stop(process):
    process.stdin.write("exit\n")
    process.stdin.flush()
    assert process.wait(timeout=20) == 0, process.stderr.read()


def bounds(home, selected, root, history_ids=None):
    """Independent fixture oracle: physical row incidence, not native helper internals."""
    values = sorted(selected)
    marks = ",".join("?" for _ in values)
    result = []
    for path in sorted(home.glob("*.sqlite")):
        with s.connection(path) as db:
            names = {
                row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            }
            rows = {}
            for table in s.TABLES:
                if table not in names:
                    continue
                if table == "thread_spawn_edges":
                    predicate = f"parent_thread_id IN ({marks}) OR child_thread_id IN ({marks})"
                    args = values * 2
                elif table == "jobs":
                    predicate = f"kind='memory_stage1' AND job_key IN ({marks})"
                    args = values
                else:
                    column = "id" if table == "threads" else "thread_id"
                    keys = sorted(history_ids) if history_ids is not None and table in (
                        "thread_items", "thread_realtime_items", "thread_turns", "thread_history_projection_state") else values
                    predicate = f"{column} IN ({','.join('?' for _ in keys)})"
                    args = keys
                rows[table] = [
                    row[0]
                    for row in db.execute(
                        f'SELECT rowid FROM "{table}" WHERE {predicate} ORDER BY rowid', args
                    )
                ]
            if "thread_goal_continuation_deferrals" in names:
                rows["thread_goal_continuation_deferrals"] = [
                    row[0]
                    for row in db.execute(
                        f"SELECT rowid FROM thread_goal_continuation_deferrals WHERE thread_id IN ({marks}) ORDER BY rowid",
                        values,
                    )
                ]
            board = "channels" in names
            if board:
                for table in (
                    "board_templates",
                    "channels",
                    "posts",
                    "subscriptions",
                    "subscription_opt_outs",
                    "deleted_boards",
                ):
                    rows[table] = (
                        [
                            row[0]
                            for row in db.execute(
                                f'SELECT rowid FROM "{table}" WHERE board=? ORDER BY rowid', [root]
                            )
                        ]
                        if root in selected
                        else []
                    )
            if not rows:
                raise AssertionError((path, names))
            queue = "queued_items" in rows
            if queue:
                rows["sqlite_sequence"] = [
                    row[0]
                    for row in db.execute(
                        "SELECT rowid FROM sqlite_sequence WHERE name='queued_thread_revisions' ORDER BY rowid"
                    )
                ]
            result.append(
                dict(
                    path=str(path),
                    rows=rows,
                    queue_bookkeeping=queue,
                    board_tombstone=root if board and root in selected else None,
                )
            )
    return result


def stored_source(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def capture_targets(home, selected):
    """Independent synthetic manifest oracle; never reads beyond the first line."""
    with s.connection(home / "state_5.sqlite") as db:
        db.row_factory = s.sqlite3.Row
        result = []
        for identity in selected:
            row = db.execute("SELECT * FROM threads WHERE id=?", [identity]).fetchone()
            current = pathlib.Path(row["rollout_path"])
            rollouts = []
            current_meta = {}
            for namespace in ("sessions", "archived_sessions"):
                for path in sorted((home / namespace).rglob("*")):
                    if not path.is_file() or path.name[28:64] != identity:
                        continue
                    compressed = path.name.endswith(".jsonl.zst")
                    if not compressed and not path.name.endswith(".jsonl"):
                        continue
                    name = path.name.removesuffix(".zst").removesuffix(".jsonl")
                    rollout_id = name[-36:]
                    with path.open("rb") as stream:
                        st = os.fstat(stream.fileno())
                        if compressed:
                            from compression import zstd
                            try:
                                with zstd.open(stream, "rb") as decoded:
                                    header = decoded.readline(1048577)
                            except zstd.ZstdError:
                                header = b""
                        else:
                            header = stream.readline(1048577)
                    try:
                        meta = json.loads(header)["payload"]
                    except (ValueError, KeyError, TypeError):
                        meta = {}  # Intentional malformed-header refusal fixtures.
                    base = meta.get("history_base")
                    fork = meta.get("forked_from_id")
                    rollouts.append(dict(rollout_id=rollout_id,
                        representation="zstd" if compressed else "plain", rollout_path=str(path),
                        physical_identity=dict(dev=st.st_dev, ino=st.st_ino, size=st.st_size,
                            mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns),
                        header_sha256=list(hashlib.sha256(header).digest()),
                        history_mode=meta.get("history_mode", row["history_mode"]),
                        history_prefix=None if base is None else dict(provider_rollout_id=base["thread_id"],
                            end_ordinal_exclusive=base["end_ordinal_exclusive"], end_byte_offset=base["end_byte_offset"]),
                        logical_fork=None if fork is None else dict(forked_from_id=fork,
                            forked_from_ordinal_exclusive=meta.get("forked_from_ordinal_exclusive"))))
                    if str(path).removesuffix(".zst") == str(current).removesuffix(".zst"):
                        current_meta = meta
            parent = db.execute("SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id=?", (identity,)).fetchone()
            result.append(dict(thread_id=identity, current_rollout_path=str(current),
                current_rollout_id=current.name.removesuffix(".zst").removesuffix(".jsonl")[-36:],
                rollouts=sorted(rollouts, key=lambda item: item["rollout_id"]),
                parent_relation=(
                    {"kind": "guardian_association", "associated_parent_thread_id": current_meta.get("parent_thread_id")}
                    if stored_source(row["source"]) == {"subagent": {"other": "guardian"}}
                    else {"kind": "spawn", "parent_thread_id": parent[0]}
                    if parent else {"kind": "root"}),
                source=stored_source(row["source"]), agent_role=row["agent_role"],
                provider=row["model_provider"], updated_at_ms=row["updated_at_ms"], archived=bool(row["archived"])))
        return result


def upgrade(home):
    """Explicit deployed DDL and checksums, independent of upstream migrations."""
    contract = json.loads((ROOT / "src/codex_session_gc/execution/closed_work_contract.json").read_text())
    schema = next(item for item in contract["databases"] if item["kind"] == "state_5.sqlite")
    with s.sqlite_context(home / "state_5.sqlite") as db:
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_schema")}
        versions = {row[0] for row in db.execute("SELECT version FROM _sqlx_migrations")}
        for kind, name, table, ddl in schema["schema"]:
            if name not in existing:
                db.execute(ddl)
        for version, checksum, success in schema["migrations"]:
            if version not in versions:
                db.execute("INSERT INTO _sqlx_migrations(version,description,installed_on,success,checksum,execution_time) VALUES(?,?,CURRENT_TIMESTAMP,?,?,0)",
                    (version, "isolated deployed schema", success, bytes(checksum)))


def prepare(directory, schema_version=61):
    home = directory / "home"
    made = native("create-batch", home)
    assert made.returncode == 0, made.stderr
    manifest = json.loads(made.stdout)
    if schema_version == 61:
        upgrade(home)
    member = native("add-member", home)
    assert member.returncode == 0, member.stderr
    unrelated = json.loads(member.stdout)
    with s.sqlite_context(home / "state_5.sqlite") as db:
        db.execute(
            "INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'closed')",
            (manifest["main"], manifest["target"]),
        )
    # Populate native history-cache tables with isolated fixture-owned rows.
    with s.sqlite_context(home / "thread_history_1.sqlite") as db:
        for identity in (manifest["main"], manifest["target"], unrelated["id"]):
            db.execute(
                "INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status) VALUES(?,?,?,?)",
                (identity, "turn", 1, "completed"),
            )
            db.execute(
                "INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json) VALUES(?,?,?,?,?,?)",
                (identity, "turn", "item", 1, 1780272000000, "{}"),
            )
            db.execute(
                "INSERT INTO thread_realtime_items(thread_id,item_id,rollout_ordinal,created_at_ms,item_type,item_json) VALUES(?,?,?,?,?,?)",
                (identity, "rt", 2, 1780272000000, "fixture", "{}"),
            )
            db.execute(
                "INSERT INTO thread_history_projection_state VALUES(?,?,?)", (identity, 0, 3)
            )
    aid, uid = s.j.AttemptId(uuid.uuid4()), s.j.UnitId(uuid.uuid4())
    payload = s.j.Payload("fixture/native-observation", 1, b"isolated current native producer")
    receipt = directory / "journal.sqlite"
    s.j.create(
        receipt,
        s.j.StartEnvelope(aid, payload, payload, payload, payload),
        (s.j.Unit(uid, 0, payload),),
    )
    ownership = sorted((manifest["main"], manifest["target"]))
    request = dict(
        version=6,
        journal=str(receipt),
        attempt_id=str(aid.value),
        unit_id=str(uid.value),
        codex_home=str(home),
        sqlite_home=str(home),
        provider="openai",
        cutoff="2026-08-01T00:00:00Z",
        ownership_scope=dict(root_thread_id=manifest["main"], thread_ids=list(ownership)),
        deletion_scope=list(ownership),
        targets=[],
        databases=[],
        guard_paths=sorted(
            [
                str(home / ".tmp/rollout-maintenance.lock"),
                str(home / "thread-writer-locks/.coordination.lock"),
                *[str(home / "thread-writer-locks" / f"{identity}.lock") for identity in ownership],
            ]
        ),
    )
    return manifest, unrelated, request, aid, uid, payload


def refresh(request):
    home = pathlib.Path(request["codex_home"])
    request["targets"] = capture_targets(home, request["deletion_scope"])
    request["databases"] = bounds(
        home, set(request["deletion_scope"]), request["ownership_scope"]["root_thread_id"],
        set(request["deletion_scope"]) | {rollout["rollout_id"] for target in request["targets"] for rollout in target["rollouts"]}
    )


def edit_header(path, edit):
    lines = path.read_text().splitlines(keepends=True)
    header = json.loads(lines[0])
    edit(header["payload"])
    lines[0] = json.dumps(header, separators=(",", ":")) + "\n"
    path.write_text("".join(lines))


def run_case(name):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="cw-batch-") as temporary:
        directory = pathlib.Path(temporary)
        manifest, unrelated, request, aid, uid, payload = prepare(directory)
        home = pathlib.Path(manifest["home"])
        root, child = manifest["main"], manifest["target"]
        sibling = None
        if name in ("child-only", "child-board", "busy-sibling"):
            result = native("add-member", home)
            assert result.returncode == 0, result.stderr
            sibling = json.loads(result.stdout)
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute(
                    "INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'closed')",
                    (root, sibling["id"]),
                )
            request["ownership_scope"]["thread_ids"].append(sibling["id"])
            request["guard_paths"].append(
                str(home / "thread-writer-locks" / f"{sibling['id']}.lock")
            )
            request["guard_paths"].sort()
        board_process = None
        held = []
        memory = None
        if name in ("child-only", "child-board", "busy-sibling", "retained-child"):
            request["deletion_scope"] = [child] if name != "retained-child" else [root]
        if name == "board-root":
            initialized = native("initialize-memory-v2", home)
            assert initialized.returncode == 0, initialized.stderr
        if name.startswith("board-") or name == "child-board":
            board_process = peer("board-handle", home, root)
            assert json.loads(command(board_process, "write"))["write_ok"]
            # These isolated persisted rows exercise every bounded board-data table;
            # the cached native handle independently tests permanent late-write refusal.
            with s.sqlite_context(home / "agent_message_board_1.sqlite") as db:
                db.execute("INSERT INTO subscriptions VALUES(?,?,?)", (root, "write", child))
                db.execute(
                    "INSERT INTO posts(board,id,channel,root,author,timestamp,body_search,payload,request_id,request) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        root,
                        "fixture-post",
                        "write",
                        "fixture-post",
                        root,
                        0,
                        "fixture",
                        "{}",
                        "fixture-request",
                        "{}",
                    ),
                )
        if name.startswith("board-") or name == "child-board":
            other_board = peer("board-handle", home, unrelated["id"])
            assert json.loads(command(other_board, "write"))["write_ok"]
            stop(other_board)
            with s.sqlite_context(home / "agent_message_board_1.sqlite") as db:
                for identity in (root, unrelated["id"]):
                    db.execute("INSERT INTO board_templates VALUES(?,?)",
                               (identity, "\x00opaque:not-json:" + identity))
                    db.execute("UPDATE channels SET description=? WHERE board=?",
                               ("\x00opaque-description:" + identity, identity))
                if name == "board-late-template":
                    db.execute("DELETE FROM board_templates WHERE board=?", (root,))
        for case, mode in (
            ("active-goal", "add-active-goal"),
            ("queued-work", "add-pending"),
            ("running-memory", "add-running-job"),
            ("expired-memory", "add-expired-job"),
            ("phase2-output", "add-output"),
        ):
            if name == case:
                result = native(mode, home, child)
                assert result.returncode == 0, result.stderr
        if name == "phase2-output":
            result = native("select-memory", home, child)
            assert result.returncode == 0, result.stderr
        if name == "superseded-memory":
            result = native("supersede-memory", home, child)
            assert result.returncode == 0, result.stderr
            memory = json.loads(result.stdout)
        if name in ("archived", "archive-mismatch"):
            old = pathlib.Path(manifest["target_path"])
            archive = home / "archived_sessions" / old.name
            old.rename(archive)
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute(
                    "UPDATE threads SET rollout_path=?,archived=1 WHERE id=?", (str(archive), child)
                )
            manifest["target_path"] = str(archive)
        if name == "fork":
            edit_header(
                pathlib.Path(manifest["main_path"]),
                lambda meta: meta.update(forked_from_id=unrelated["id"]),
            )
        if name == "unknown-source":
            edit_header(
                pathlib.Path(manifest["main_path"]),
                lambda meta: meta.update(source="opaque-header-source"),
            )
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute(
                    "UPDATE threads SET source=? WHERE id=?",
                    (json.dumps("opaque-sql-source"), root),
                )
        if name == "provider-variety":
            edit_header(
                pathlib.Path(manifest["target_path"]),
                lambda meta: meta.update(model_provider="fixture-other-provider"),
            )
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute(
                    "UPDATE threads SET model_provider=? WHERE id=?",
                    ("fixture-other-provider", child),
                )
        if name == "paginated":
            edit_header(
                pathlib.Path(manifest["target_path"]),
                lambda meta: meta.update(history_mode="paginated"),
            )
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", [child])
        if name in ("retained-legacy-fork", "retained-paginated-fork", "retained-unqualified-fork"):

            def retained_fork(meta):
                meta["forked_from_id"] = child
                if name == "retained-paginated-fork":
                    meta["history_mode"] = "paginated"
                if name == "retained-unqualified-fork":
                    meta["id"] = str(uuid.uuid4())

            edit_header(pathlib.Path(unrelated["path"]), retained_fork)
        if name == "retained-parent":
            edit_header(
                pathlib.Path(unrelated["path"]), lambda meta: meta.update(parent_thread_id=child)
            )
        if name == "retained-source-parent":
            source = {"subagent": {"thread_spawn": {"parent_thread_id": child, "depth": 1}}}
            edit_header(pathlib.Path(unrelated["path"]), lambda meta: meta.update(source=source))
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute(
                    "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), unrelated["id"])
                )
        if name == "retained-provider":
            edit_header(
                pathlib.Path(unrelated["path"]),
                lambda meta: meta.update(
                    history_mode="paginated",
                    history_base=dict(
                        thread_id=child,
                        end_ordinal_exclusive=2,
                        end_byte_offset=pathlib.Path(manifest["target_path"]).stat().st_size,
                    ),
                ),
            )
        if name == "compressed-sibling":
            pathlib.Path(manifest["target_path"] + ".zst").write_bytes(
                b"fixture unsupported sibling"
            )
        if name in ("busy-root", "busy-child", "busy-sibling"):
            busy = (
                root if name == "busy-root" else sibling["id"] if name == "busy-sibling" else child
            )
            held.append(peer("hold-guard", home, busy))
        peers = []
        if name.startswith("heavy-"):
            with s.sqlite_context(home / "logs_2.sqlite") as db:
                db.execute("DELETE FROM logs WHERE target='fixture' AND ts<=?", [1780272000])
            if name == "heavy-sql-progress":
                peers = [peer("metadata", home, unrelated["id"], unrelated["path"])]
        if name.startswith("held-"):
            # Native diagnostic maintenance is independent; exclude only the old
            # isolated fixture diagnostic before peer startup and bound capture.
            with s.sqlite_context(home / "logs_2.sqlite") as db:
                db.execute("DELETE FROM logs WHERE target='fixture' AND ts<=?", [1780272000])
            peers = [
                peer("writer", home, unrelated["id"], unrelated["path"]),
                peer("metadata", home, unrelated["id"], unrelated["path"]),
                peer("metadata", home, unrelated["id"], unrelated["path"]),
            ]
            assert command(peers[1], "index-name") == "named"
        if name == "whole-work":
            name_peer = peer("metadata", home, root, manifest["main_path"])
            assert command(name_peer, "index-name") == "named"
            stop(name_peer)
        refresh(request)
        if name == "board-late-template":
            with s.sqlite_context(home / "agent_message_board_1.sqlite") as db:
                db.execute("INSERT INTO board_templates VALUES(?,?)", (root, "late:not-json"))
        if name == "board-missing-template-bound":
            next(b for b in request["databases"] if "board_templates" in b["rows"])["rows"].pop("board_templates")
        if name == "board-wrong-template-bound":
            next(b for b in request["databases"] if "board_templates" in b["rows"])["rows"]["board_templates"] = []
        if name in ("board-old-schema", "board-extra-trigger"):
            with s.sqlite_context(home / "agent_message_board_1.sqlite") as db:
                if name == "board-old-schema":
                    db.execute("DROP TABLE board_templates")
                    db.execute("ALTER TABLE channels DROP COLUMN description")
                else:
                    db.execute("CREATE TRIGGER unqualified_board_trigger AFTER DELETE ON board_templates BEGIN SELECT 1; END")
        if name == "archive-mismatch":
            next(target for target in request["targets"] if target["thread_id"] == child)[
                "archived"
            ] = False
        if name == "closure-change":
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute(
                    "INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'open')",
                    (root, unrelated["id"]),
                )
        if name == "later-target-digest":
            request["targets"][-1]["rollouts"][0]["header_sha256"][0] ^= 1
        if name == "bound-overrun":
            next(bound for bound in request["databases"] if "threads" in bound["rows"])["rows"][
                "threads"
            ] = []
        if name == "identity-mismatch":
            request["targets"][0]["source"] = "exec"
        if name == "unknown-source":
            next(target for target in request["targets"] if target["thread_id"] == root)[
                "source"
            ] = "opaque-request-source"
        if name == "unsupported-control":
            control = directory / "control"
            control.mkdir()
            request["fixture_control"] = dict(
                point="before_effects", directory=str(control), action="fail"
            )
        if name == "missing-board-bound":
            board_process = peer("board-handle", home, root)
        temporary_namespace = home / ".tmp"
        maintenance_lock = None
        if name in ("absent-temp", "absent-temp-invalid-guard", "temp-file", "temp-symlink"):
            if temporary_namespace.exists():
                shutil.rmtree(temporary_namespace)
            if name == "absent-temp-invalid-guard":
                request["guard_paths"].remove(str(temporary_namespace / "rollout-maintenance.lock"))
            elif name == "temp-file":
                temporary_namespace.write_bytes(b"preserved unrelated file")
            elif name == "temp-symlink":
                outside = directory / "unrelated-directory"
                outside.mkdir()
                temporary_namespace.symlink_to(outside, target_is_directory=True)
        if name == "maintenance-busy":
            temporary_namespace.mkdir(exist_ok=True)
            maintenance_lock = (temporary_namespace / "rollout-maintenance.lock").open("a+b")
            fcntl.flock(maintenance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        zero = None
        zero_stat = None
        if name.startswith("zero-"):
            zero_id = str(uuid.uuid4())
            zero_directory = home / (
                "archived_sessions" if name == "zero-provider-after" else "sessions"
            )
            zero_directory.mkdir(exist_ok=True)
            zero = zero_directory / f"rollout-2026-06-01T00-00-00-{zero_id}.jsonl"
            zero.touch()
            if name == "zero-owned":
                zero.unlink()
                zero = pathlib.Path(unrelated["path"])
                zero.write_bytes(b"")
                retained_member = json.loads(native("add-member", home).stdout)
                with s.sqlite_context(home / "state_5.sqlite") as db:
                    db.execute(
                        "INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'closed')",
                        (unrelated["id"], retained_member["id"]),
                    )
            elif name == "zero-selected":
                pathlib.Path(manifest["target_path"]).write_bytes(b"")
            elif name == "zero-hardlink":
                os.link(zero, home / "zero-alias-outside-namespace")
            elif name == "zero-symlink":
                zero.unlink()
                zero.symlink_to(directory / "empty-outside-namespace")
                (directory / "empty-outside-namespace").touch()
            elif name == "zero-duplicate":
                other = home / "archived_sessions"
                other.mkdir(exist_ok=True)
                (other / zero.name).touch()
            elif name == "zero-nonempty":
                zero.write_bytes(b"not metadata\n")
            elif name in ("zero-provider-before", "zero-provider-after"):
                provider = pathlib.Path(unrelated["path"])
                if name == "zero-provider-before":
                    archive = home / "archived_sessions"
                    archive.mkdir(exist_ok=True)
                    moved = archive / provider.name
                    provider.rename(moved)
                    provider = moved
                    unrelated["path"] = str(moved)
                edit_header(
                    provider,
                    lambda meta: meta.update(
                        history_mode="paginated",
                        history_base=dict(
                            thread_id=zero_id, end_ordinal_exclusive=1, end_byte_offset=1
                        ),
                    ),
                )
            if not zero.is_symlink():
                zero_stat = zero.stat()
        held_sql = None
        if name == "sqlite-busy":
            held_sql = s.sqlite3.connect(home / "state_5.sqlite")
            held_sql.execute("BEGIN IMMEDIATE")
        before_sql = s.sql_snapshot(request)
        before_files = s.history_snapshot(home)
        retained_fork_bytes = pathlib.Path(unrelated["path"]).read_bytes()
        names_path = home / "session_index.jsonl"
        names_before = names_path.read_bytes() if names_path.exists() else None
        retained = s.owner_snapshot(request, unrelated["id"])
        parent_before = s.owner_snapshot(request, root)
        sibling_before = s.owner_snapshot(request, sibling["id"]) if sibling else None
        board_before = None
        templates_before = None
        if board_process:
            with s.connection(home / "agent_message_board_1.sqlite") as db:
                board_before = db.execute(
                    "SELECT * FROM channels WHERE board=? ORDER BY rowid", [root]
                ).fetchall()
                if name != "board-old-schema":
                    templates_before = db.execute("SELECT * FROM board_templates ORDER BY board").fetchall()
                unrelated_board = db.execute(
                    "SELECT * FROM channels WHERE board=? ORDER BY rowid", [unrelated["id"]]
                ).fetchall()
        progress = {}
        if name.startswith("heavy-") or name in ("zero-growth", "zero-replacement"):
            control = directory / "control"
            control.mkdir()
            request["fixture_control"] = dict(
                point=("after_history_walk" if name.startswith("zero-") else "during_heavy_reads"),
                directory=str(control),
                action="wait",
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                pending = executor.submit(s.dispatch, request, aid, uid, payload)
                deadline = time.monotonic() + 25
                while not (control / "ready").exists():
                    assert time.monotonic() < deadline
                    if pending.done():
                        raise AssertionError(pending.result())
                    time.sleep(0.01)
                barrier = time.monotonic()
                try:
                    for identity in request["ownership_scope"]["thread_ids"]:
                        busy = native("try-guard", home, identity)
                        assert busy.returncode != 0, busy
                    if name == "heavy-sql-progress":
                        acknowledgment = executor.submit(command, peers[0], "state-name")
                        progress["sql_acknowledgment"] = json.loads(
                            acknowledgment.result(timeout=5)
                        )
                        assert progress["sql_acknowledgment"]["state_changed"], progress
                        with s.connection(home / "state_5.sqlite") as db:
                            assert db.execute(
                                "SELECT name FROM threads WHERE id=?", [unrelated["id"]]
                            ).fetchone() == ("MAIN state preserved",)
                        progress["unrelated_sql_commit_before_release"] = True
                        retained = s.owner_snapshot(request, unrelated["id"])
                    elif name == "heavy-closure-change":
                        with s.sqlite_context(home / "state_5.sqlite") as db:
                            db.execute(
                                "INSERT INTO thread_spawn_edges(parent_thread_id,child_thread_id,status) VALUES(?,?,'closed')",
                                (root, unrelated["id"]),
                            )
                    elif name == "heavy-effect-change":
                        added = native("add-pending", home, child)
                        assert added.returncode == 0, added.stderr
                    elif name == "heavy-file-change":
                        path = pathlib.Path(request["targets"][-1]["rollouts"][0]["rollout_path"])
                        previous = path.stat()
                        replacement = path.with_name(path.name + ".replacement")
                        replacement.write_bytes(path.read_bytes())
                        replacement.replace(path)
                        assert (path.stat().st_dev, path.stat().st_ino) != (
                            previous.st_dev,
                            previous.st_ino,
                        )
                        progress["same_content_new_physical_identity"] = True
                    elif name == "zero-growth":
                        zero.write_bytes(b"new history arrived\n")
                    elif name == "zero-replacement":
                        replacement = directory / "replacement-zero"
                        replacement.touch()
                        replacement.replace(zero)
                    before_sql = s.sql_snapshot(request)
                    before_files = s.history_snapshot(home)
                finally:
                    progress["controlled_heavy_phase_ms"] = round(
                        (time.monotonic() - barrier) * 1000, 3
                    )
                    (control / "continue").write_text("continue")
                outcome = pending.result(timeout=30)
            for process in peers:
                stop(process)
        elif name.startswith("held-"):
            point = {
                "held-before": "before_effects",
                "held-physical": "after_physical",
                "held-commit": "after_first_commit",
            }[name]
            control = directory / "control"
            control.mkdir()
            request["fixture_control"] = dict(point=point, directory=str(control), action="wait")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(s.dispatch, request, aid, uid, payload)
                deadline = time.monotonic() + 25
                while not (control / "ready").exists():
                    assert time.monotonic() < deadline
                    if pending.done():
                        raise AssertionError(pending.result())
                    time.sleep(0.01)
                barrier = time.monotonic()
                for identity, path in (
                    (root, manifest["main_path"]),
                    (child, manifest["target_path"]),
                ):
                    result = native("resume", home, identity, path)
                    assert result.returncode != 0 and "active writer" in result.stderr, result
                peers[0].stdin.write("append\n")
                peers[0].stdin.flush()
                peers[2].stdin.write("state-name\n")
                peers[2].stdin.flush()
                assert command(peers[1], "index-name") == "named"
                deadline = time.monotonic() + 5
                while b"MAIN continues" not in pathlib.Path(unrelated["path"]).read_bytes():
                    assert time.monotonic() < deadline, "unrelated physical append did not progress"
                    time.sleep(0.01)
                progress["physical_append_visible_ms"] = round(
                    (time.monotonic() - barrier) * 1000, 3
                )
                (control / "continue").write_text("continue")
                outcome = pending.result(timeout=30)
                assert peers[0].stdout.readline().strip() == "appended"
                progress["native_flush_ack_ms"] = round((time.monotonic() - barrier) * 1000, 3)
                progress["sql_acknowledgment"] = json.loads(peers[2].stdout.readline())
                progress["controlled_phase_ms"] = round((time.monotonic() - barrier) * 1000, 3)
                assert progress["sql_acknowledgment"]["state_changed"], progress
            for process in peers:
                stop(process)
        else:
            if name in ("partial-physical", "partial-commit"):
                control = directory / "control"
                control.mkdir()
                request["fixture_control"] = dict(
                    point="after_physical" if name == "partial-physical" else "after_first_commit",
                    directory=str(control),
                    action="fail",
                )
            outcome = s.dispatch(request, aid, uid, payload)
        for process in held:
            stop(process)
        if held_sql is not None:
            held_sql.close()
        if maintenance_lock is not None:
            maintenance_lock.close()
        if name == "absent-temp":
            assert temporary_namespace.is_dir() and not temporary_namespace.is_symlink(), outcome
            assert (temporary_namespace / "rollout-maintenance.lock").is_file()
        elif name == "absent-temp-invalid-guard":
            assert not temporary_namespace.exists()
        elif name == "temp-file":
            assert temporary_namespace.read_bytes() == b"preserved unrelated file"
        elif name == "temp-symlink":
            assert temporary_namespace.is_symlink()
            assert list(outside.iterdir()) == []
        refused = {
            "board-late-template", "board-missing-template-bound", "board-wrong-template-bound",
            "board-old-schema", "board-extra-trigger", "sqlite-busy",
            "absent-temp-invalid-guard",
            "temp-file",
            "temp-symlink",
            "maintenance-busy",
            "heavy-closure-change",
            "heavy-effect-change",
            "heavy-file-change",
            "busy-root",
            "busy-child",
            "busy-sibling",
            "closure-change",
            "bound-overrun",
            "later-target-digest",
            "identity-mismatch",
            "unknown-source",
            "retained-child",
            "retained-provider",
            "retained-unqualified-fork",
            "retained-parent",
            "retained-source-parent",
            "compressed-sibling",
            "archive-mismatch",
            "running-memory",
            "expired-memory",
            "phase2-output",
            "missing-board-bound",
            "unsupported-control",
            "zero-selected",
            "zero-hardlink",
            "zero-symlink",
            "zero-duplicate",
            "zero-nonempty",
            "zero-provider-before",
            "zero-provider-after",
            "zero-growth",
            "zero-replacement",
        }
        partial = {"partial-physical", "partial-commit"}
        expected = "refused" if name in refused else "partial" if name in partial else "confirmed"
        assert outcome["status"] == expected, (name, outcome)
        if expected == "refused":
            assert outcome["removed_rollouts"] == [] and outcome["committed_databases"] == []
            if name == "busy-sibling":
                assert request["deletion_scope"] == [child]
                assert set(request["ownership_scope"]["thread_ids"]) == {root, child, sibling["id"]}
                assert "active writer" in outcome["detail"] and sibling["id"] in outcome["detail"], outcome
            if name == "unsupported-control":
                assert not (directory / "control" / "ready").exists()
            assert s.sql_snapshot(request) == before_sql, (name, "SQL changed")
            assert s.history_snapshot(home) == before_files, (name, "physical history changed")
        elif expected == "confirmed":
            if zero is not None:
                assert zero.read_bytes() == b""
                after_zero = zero.stat()
                assert (
                    after_zero.st_dev,
                    after_zero.st_ino,
                    after_zero.st_size,
                    after_zero.st_mtime_ns,
                    after_zero.st_ctime_ns,
                ) == (
                    zero_stat.st_dev,
                    zero_stat.st_ino,
                    zero_stat.st_size,
                    zero_stat.st_mtime_ns,
                    zero_stat.st_ctime_ns,
                )
                if name == "zero-owned":
                    with s.connection(home / "state_5.sqlite") as db:
                        assert db.execute(
                            "SELECT status FROM thread_spawn_edges WHERE parent_thread_id=? AND child_thread_id=?",
                            (unrelated["id"], retained_member["id"]),
                        ).fetchone() == ("closed",)
            assert {rollout["rollout_path"] for target in request["targets"] for rollout in target["rollouts"]} == set(
                outcome["removed_rollouts"]
            )
            assert outcome["preserved_name_index"] == str(home / "session_index.jsonl")
            if names_before is not None:
                assert names_path.read_bytes().startswith(names_before), (
                    "historical names were rewritten"
                )
            if name == "whole-work":
                assert root.encode() in names_path.read_bytes(), (
                    "selected root historical name was lost"
                )
            for target in request["targets"]:
                assert all(not pathlib.Path(rollout["rollout_path"]).exists() for rollout in target["rollouts"])
                assert not any(s.owner_snapshot(request, target["thread_id"]).values()), (
                    name,
                    target,
                )
            after_bounds = bounds(home, set(request["deletion_scope"]), root)
            for bound in after_bounds:
                for table, rows in bound["rows"].items():
                    if table == "sqlite_sequence":
                        continue
                    if table == "deleted_boards" and root in request["deletion_scope"]:
                        assert len(rows) == 1
                    else:
                        assert not rows, (name, table, rows)
            assert pathlib.Path(unrelated["path"]).is_file()
            if name == "retained-legacy-fork":
                assert pathlib.Path(unrelated["path"]).read_bytes() == retained_fork_bytes
                sample = native("sample-history", home, unrelated["path"])
                assert sample.returncode == 0, sample.stderr
            if not name.startswith("held-"):
                assert s.owner_snapshot(request, unrelated["id"]) == retained
            if name == "retained-legacy-fork":
                resumed = native("resume", home, unrelated["id"], unrelated["path"])
                assert resumed.returncode == 0, resumed.stderr
            if root not in request["deletion_scope"]:
                assert s.owner_snapshot(request, root) == parent_before
                result = native("resume", home, root, manifest["main_path"])
                assert result.returncode == 0, result.stderr
                if sibling:
                    assert s.owner_snapshot(request, sibling["id"]) == sibling_before
                    result = native("resume", home, sibling["id"], sibling["path"])
                    assert result.returncode == 0, result.stderr
            if not board_process:
                assert not (home / "agent_message_board_1.sqlite").exists()
            if name.startswith("held-"):
                assert (home / "session_index.jsonl").read_bytes().count(
                    b"MAIN name preserved"
                ) >= 2
            if memory:
                result = native("complete-memory", home, child, memory["old_token"])
                assert result.returncode == 0 and json.loads(result.stdout) == {"applied": False}, (
                    result
                )
                assert not any(s.owner_snapshot(request, child).values())
                sample = native("sample-history", home, manifest["target_path"])
                assert sample.returncode != 0, sample
                progress["missing_native_rollout_sample_failed"] = True
                progress["superseded_native_completion_applied"] = False
        else:
            assert outcome["removed_rollouts"], outcome
            assert bool(outcome["committed_databases"]) == (name == "partial-commit"), outcome
        if board_process:
            if expected == "confirmed":
                with s.connection(home / "agent_message_board_1.sqlite") as db:
                    channels = db.execute(
                        "SELECT * FROM channels WHERE board=? ORDER BY rowid", [root]
                    ).fetchall()
                    tombstone = db.execute(
                        "SELECT board FROM deleted_boards WHERE board=?", [root]
                    ).fetchall()
                    assert (
                        db.execute(
                            "SELECT * FROM channels WHERE board=? ORDER BY rowid", [unrelated["id"]]
                        ).fetchall()
                        == unrelated_board
                    )
                    assert channels == ([] if root in request["deletion_scope"] else board_before)
                    assert bool(tombstone) == (root in request["deletion_scope"])
                    assert db.execute("SELECT * FROM board_templates ORDER BY board").fetchall() == [
                        row for row in templates_before if row[0] != root or root not in request["deletion_scope"]
                    ]
                late = json.loads(command(board_process, "late-write"))
                assert late["write_ok"] == (root not in request["deletion_scope"]), late
                progress["cached_board_late_write"] = late
            stop(board_process)
        if name in ("busy-root", "busy-child", "busy-sibling", "maintenance-busy", "sqlite-busy"):
            # A separately journaled, freshly observed invocation after ownership is released.
            retry_aid, retry_uid = s.j.AttemptId(uuid.uuid4()), s.j.UnitId(uuid.uuid4())
            retry = dict(request, journal=str(directory / "released-journal.sqlite"),
                         attempt_id=str(retry_aid.value), unit_id=str(retry_uid.value))
            refresh(retry)
            s.j.create(pathlib.Path(retry["journal"]),
                       s.j.StartEnvelope(retry_aid, payload, payload, payload, payload),
                       (s.j.Unit(retry_uid, 0, payload),))
            released = s.dispatch(retry, retry_aid, retry_uid, payload)
            assert released["status"] == "confirmed", released
            assert set(released["removed_rollouts"]) == {
                r["rollout_path"] for t in retry["targets"] for r in t["rollouts"]
            }
            assert s.owner_snapshot(retry, unrelated["id"]) == retained
            assert pathlib.Path(unrelated["path"]).read_bytes() == retained_fork_bytes
            progress["after_ownership_release"] = released
        return dict(
            case=name,
            outcome=outcome,
            elapsed_seconds=round(time.monotonic() - started, 3),
            progress=progress,
            native_binary_sha256=s.NATIVE_DIGEST,
            scope="Isolated native LocalThreadStore/storage fixtures; not Core/app-server or product online qualification",
        )


if __name__ == "__main__":
    cases = [
        "absent-temp",
        "absent-temp-invalid-guard",
        "temp-file",
        "temp-symlink",
        "maintenance-busy",
        "zero-preserved",
        "zero-owned",
        "zero-selected",
        "zero-hardlink",
        "zero-symlink",
        "zero-duplicate",
        "zero-nonempty",
        "zero-provider-before",
        "zero-provider-after",
        "zero-growth",
        "zero-replacement",
        "whole-work",
        "heavy-sql-progress",
        "heavy-closure-change",
        "heavy-effect-change",
        "heavy-file-change",
        "child-only",
        "board-root",
        "board-late-template", "board-missing-template-bound", "board-wrong-template-bound",
        "board-old-schema", "board-extra-trigger", "sqlite-busy",
        "child-board",
        "active-goal",
        "queued-work",
        "archived",
        "fork",
        "provider-variety",
        "superseded-memory",
        "busy-root",
        "busy-child",
        "busy-sibling",
        "closure-change",
        "bound-overrun",
        "later-target-digest",
        "identity-mismatch",
        "unknown-source",
        "retained-child",
        "retained-provider",
        "retained-legacy-fork",
        "retained-paginated-fork",
        "retained-unqualified-fork",
        "retained-parent",
        "retained-source-parent",
        "paginated",
        "compressed-sibling",
        "archive-mismatch",
        "running-memory",
        "expired-memory",
        "phase2-output",
        "missing-board-bound",
        "partial-physical",
        "partial-commit",
        "held-before",
        "held-physical",
        "held-commit",
    ]
    if os.environ.get("GC_NATIVE_CASES"):
        cases = os.environ["GC_NATIVE_CASES"].split(",")
    for case in cases:
        print(json.dumps(run_case(case), separators=(",", ":")), flush=True)
