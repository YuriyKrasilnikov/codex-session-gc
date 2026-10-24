"""Wire-6 physical manifests and bounded header admission on disposable native homes.

Public native creation/revert produces the positive lineage. Synthetic changes
below are explicit controls, never claims about the stock encoder or live Core.
"""
import hashlib
import concurrent.futures
import importlib.util
import json
import os
import pathlib
import struct
import tempfile
import time
import uuid

from compression import zstd

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("physical_batch", ROOT / "native/helper/tests/closed_work_batch.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
s = b.s


def native_json(mode, home, *arguments):
    process = b.native(mode, home, *arguments)
    assert process.returncode == 0, (mode, process.stdout, process.stderr)
    return json.loads(process.stdout)


def prepare_and_release(home, identity):
    """Project the latest physical key through real prepare_fork, then drop it."""
    child = s.spawn([s.FIXTURE, "fork-peer", home, identity], stdin=b.subprocess.PIPE,
                    stdout=b.subprocess.PIPE, stderr=b.subprocess.PIPE, text=True)
    try:
        prepared = json.loads(child.stdout.readline())
        assert prepared["stage"] == "prepared", prepared
        child.stdin.write("abort\n")
        child.stdin.flush()
        assert json.loads(child.stdout.readline())["stage"] == "aborted"
        assert child.wait(timeout=20) == 0, child.stderr.read()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=20)


def bind_stat(rollout):
    stat = pathlib.Path(rollout["rollout_path"]).stat()
    rollout["physical_identity"] = dict(dev=stat.st_dev, ino=stat.st_ino, size=stat.st_size,
        mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)


def run_case(name):
    with tempfile.TemporaryDirectory(prefix="gc-wire6-physical-") as temporary:
        directory = pathlib.Path(temporary)
        manifest, unrelated, request, aid, uid, payload = b.prepare(directory)
        home = pathlib.Path(manifest["home"])
        source = native_json("create-reverted", home)
        identity, current = source["id"], pathlib.Path(source["path"])
        assert source["rollout_id"] != identity
        prepare_and_release(home, identity)
        before_read = native_json("read-lineage", home, identity)
        assert before_read["readable"] and before_read["source_marker_present"], before_read
        request.update(version=6, cutoff="2100-01-01T00:00:00Z", deletion_scope=[identity],
            ownership_scope=dict(root_thread_id=identity, thread_ids=[identity]),
            guard_paths=sorted([str(home / ".tmp/rollout-maintenance.lock"),
                str(home / "thread-writer-locks/.coordination.lock"),
                str(home / "thread-writer-locks" / f"{identity}.lock")]))
        retained = None
        retained_guardian_files = None
        if name in ("retained-reverted-guardian", "retained-reverted-guardian-selected-prefix",
                    "retained-compressed-current-guardian"):
            retained = native_json("create-reverted", home)
            guardian_source = {"subagent": {"other": "guardian"}}
            guardian_paths = sorted((home / "sessions").rglob(f"*{retained['id']}*.jsonl"))
            assert len(guardian_paths) == 2, guardian_paths
            for path in guardian_paths:
                b.edit_header(path, lambda meta: meta.update(
                    source=guardian_source, parent_thread_id=identity))
                if path != pathlib.Path(retained["path"]):
                    b.edit_header(path, lambda meta: meta.update(
                        model_provider="historical-provider", agent_role="historical-role"))
            if name == "retained-reverted-guardian-selected-prefix":
                b.edit_header(pathlib.Path(retained["path"]), lambda meta: meta.update(history_base=dict(
                    thread_id=identity, end_ordinal_exclusive=1, end_byte_offset=1)))
            if name == "retained-compressed-current-guardian":
                guardian_current = pathlib.Path(retained["path"])
                with guardian_current.open("ab") as stream:
                    stream.write(b"\n" * 8192)
                old = time.time() - 8 * 86400
                os.utime(guardian_current, (old, old))
                produced = native_json("compress-stock", home, str(guardian_current))
                assert produced["status"] == "published" and produced["process_exit_is_runtime_drain_fence"]
                compressed_current = pathlib.Path(str(guardian_current) + ".zst")
                assert not guardian_current.exists() and compressed_current.is_file()
                guardian_paths = [compressed_current if path == guardian_current else path
                    for path in guardian_paths]
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET source=? WHERE id=?",
                    (json.dumps(guardian_source), retained["id"]))
            request["guard_paths"] = sorted(set(request["guard_paths"]) | {
                str(home / "thread-writer-locks" / f"{retained['id']}.lock")})
            retained_guardian_files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in guardian_paths}
        if name == "retained-independent-paginated-fork":
            retained = native_json("create-paginated", home)
            b.edit_header(pathlib.Path(retained["path"]), lambda meta: meta.update(forked_from_id=identity))
        if name == "stock-compressed-current":
            with current.open("ab") as stream:
                stream.write(b"\n" * 8192)
            old = time.time() - 8 * 86400
            os.utime(current, (old, old))
            produced = native_json("compress-stock", home, str(current))
            assert produced["status"] == "published" and produced["process_exit_is_runtime_drain_fence"]
            assert not current.exists() and pathlib.Path(str(current) + ".zst").is_file()
        peers = []
        if name == "unrelated-recorders-append-during-header":
            try:
                for owner, path in ((manifest["main"], manifest["main_path"]),
                                    (manifest["target"], manifest["target_path"]),
                                    (unrelated["id"], unrelated["path"])):
                    peers.append(b.peer("writer", home, owner, path))
            except BaseException:
                for process in peers:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=20)
                raise
        b.refresh(request)
        target = request["targets"][0]
        assert len(target["rollouts"]) == 2, target
        assert target["current_rollout_path"] == str(current)
        latest = next(item for item in target["rollouts"] if item["rollout_id"] == source["rollout_id"])
        original = next(item for item in target["rollouts"] if item["rollout_id"] == identity)
        assert latest["history_prefix"]["provider_rollout_id"] == identity
        with s.connection(home / "thread_history_1.sqlite") as db:
            keys = {row[0] for row in db.execute("SELECT thread_id FROM thread_history_projection_state")}
            assert {identity, source["rollout_id"]} <= keys
        expected = "confirmed"
        detail = None
        preserved_temporary = None
        if name in ("materialization-nonempty", "materialization-empty"):
            preserved_temporary = current.parent / ".rollout-materialize-controlled"
            preserved_temporary.write_bytes(b"unpublished decoded history" if name.endswith("nonempty") else b"")
            if name.endswith("nonempty"):
                expected = "refused"
        elif name in ("selected-ctime-changed", "selected-inode-replaced"):
            expected = "refused"
            old_stat = current.stat()
            if name == "selected-ctime-changed":
                os.chmod(current, old_stat.st_mode ^ 0o100)
                assert current.stat().st_ctime_ns != old_stat.st_ctime_ns
            else:
                replacement = directory / "replacement"
                replacement.write_bytes(current.read_bytes())
                os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
                os.replace(replacement, current)
                assert current.stat().st_ino != old_stat.st_ino
        elif name == "selected-sql-pointer-changed":
            expected = "refused"
            with s.sqlite_context(home / "state_5.sqlite") as db:
                db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(original["rollout_path"]), identity))
        elif name == "selected-duplicate-representation":
            expected = "refused"
            pathlib.Path(str(current) + ".zst").write_bytes(zstd.compress(current.read_bytes()))
        elif name == "retained-reverted-guardian-selected-prefix":
            expected, detail = "refused", "retained history directly requires a selected physical provider"
        elif name in ("omitted-original", "additional-owned-file"):
            expected, detail = "refused", "additional or conflicting physical rollouts"
            if name == "omitted-original":
                target["rollouts"].remove(original)
            else:
                additional = current.with_name(f"rollout-2026-06-01T00-00-00-{identity}_{uuid.uuid4()}.jsonl")
                with current.open("rb") as stream:
                    header = stream.readline(1048577)
                additional.write_bytes(header)
        elif name == "retained-original-provider":
            expected, detail = "refused", "retained history directly requires a selected physical provider"
            retained = native_json("create-paginated", home)
            b.edit_header(pathlib.Path(retained["path"]), lambda meta: meta.update(history_base=dict(
                thread_id=identity, end_ordinal_exclusive=1, end_byte_offset=1)))
        elif name == "retained-cache-key-collision":
            expected, detail = "refused", "retained ownership collides"
            owner = source["rollout_id"]
            collision = current.with_name(f"rollout-2026-06-01T00-00-00-{owner}_{uuid.uuid4()}.jsonl")
            with current.open("rb") as stream:
                header = json.loads(stream.readline(1048577))
            header["payload"].update(id=owner, history_base=None)
            collision.write_text(json.dumps(header) + "\n")
        elif name == "history-effect-overrun":
            expected, detail = "refused", "physical SQL union effect keys changed"
            bound = next(item for item in request["databases"] if item["path"].endswith("thread_history_1.sqlite"))
            bound["rows"]["thread_history_projection_state"] = []
        elif name.startswith("header-"):
            with current.open("rb") as stream:
                header = stream.readline(1048577)
            header_case = name.removeprefix("header-")
            compressed = True
            if header_case == "concatenated":
                middle = len(header) // 2
                encoded = zstd.compress(header[:middle]) + zstd.compress(header[middle:])
            elif header_case == "skippable":
                encoded = struct.pack("<II", 0x184D2A50, 3) + b"abc" + zstd.compress(header)
            elif header_case == "unread-tail":
                encoded = zstd.compress(header) + b"unread malformed tail"
            elif header_case == "physical-cap":
                encoded = struct.pack("<II", 0x184D2A50, 2 * 1024 * 1024) + b"x" * (2 * 1024 * 1024) + zstd.compress(header)
                expected, detail = "refused", "physical metadata prefix budget exhausted"
            elif header_case == "window-cap":
                encoded = zstd.compress(header + b"x" * (4 * 1024 * 1024),
                    options={zstd.CompressionParameter.window_log: 22})
                expected, detail = "refused", None
            elif header_case in ("output-cap", "plain-limit"):
                meta = json.loads(header)
                meta["payload"]["fixture_padding"] = ""
                minimal = json.dumps(meta, separators=(",", ":")).encode() + b"\n"
                meta["payload"]["fixture_padding"] = "x" * (1048576 - len(minimal) + (header_case == "output-cap"))
                header = json.dumps(meta, separators=(",", ":")).encode() + b"\n"
                compressed = header_case != "plain-limit"
                encoded = zstd.compress(header) if compressed else header
                if header_case == "output-cap":
                    expected, detail = "refused", "exceeds the bounded header limit"
            else:
                raise AssertionError(name)
            path = pathlib.Path(str(current) + ".zst") if compressed else current
            path.write_bytes(encoded)
            if compressed:
                current.unlink()
            latest.update(rollout_path=str(path), representation="zstd" if compressed else "plain",
                header_sha256=list(hashlib.sha256(header).digest()))
            bind_stat(latest)
        before_sql, before_files = s.sql_snapshot(request), s.history_snapshot(home)
        retained_sql = s.owner_snapshot(request, unrelated["id"])
        retained_guardian_sql = (s.owner_snapshot(request, retained["id"])
            if retained_guardian_files is not None else None)
        if name in ("retained-reverted-guardian", "retained-compressed-current-guardian"):
            retained_read = native_json("read-lineage", home, retained["id"])
            assert retained_read["readable"] and retained_read["source_marker_present"], retained_read
        progress = None
        if peers:
            control = directory / "control"
            control.mkdir()
            request["fixture_control"] = dict(point="during_namespace_header", directory=str(control), action="wait")
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(s.dispatch, request, aid, uid, payload)
                    deadline = time.monotonic() + 15
                    try:
                        while not (control / "ready").exists():
                            assert time.monotonic() < deadline
                            if pending.done():
                                raise AssertionError(pending.result())
                            time.sleep(0.005)
                        for process in peers:
                            assert b.command(process, "append") == "appended"
                        progress = dict(actual_recorders=3, append_acknowledged_before_header_recheck=True)
                    finally:
                        (control / "continue").write_text("continue")
                    outcome = pending.result(timeout=30)
                for process in peers:
                    assert b.command(process, "append") == "appended"
                progress["append_acknowledged_after_cleanup"] = True
                retained_sql = s.owner_snapshot(request, unrelated["id"])
            finally:
                for process in peers:
                    try:
                        if process.poll() is None:
                            b.stop(process)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=20)
        else:
            outcome = s.dispatch(request, aid, uid, payload)
        assert outcome["status"] == expected, (name, outcome)
        if preserved_temporary is not None:
            assert preserved_temporary.read_bytes() == (b"unpublished decoded history" if name.endswith("nonempty") else b"")
        if expected == "refused":
            if detail is not None:
                assert detail in outcome["detail"], (name, outcome)
            assert outcome["removed_rollouts"] == outcome["committed_databases"] == []
            assert s.sql_snapshot(request) == before_sql and s.history_snapshot(home) == before_files
        else:
            assert set(outcome["removed_rollouts"]) == {item["rollout_path"] for item in target["rollouts"]}
            assert all(not pathlib.Path(path).exists() for path in outcome["removed_rollouts"])
            with s.connection(home / "thread_history_1.sqlite") as db:
                for table in ("thread_items", "thread_realtime_items", "thread_turns", "thread_history_projection_state"):
                    assert not db.execute(f"SELECT rowid FROM {table} WHERE thread_id IN (?,?)", (identity, source["rollout_id"])).fetchall()
            with s.connection(home / "state_5.sqlite") as db:
                assert db.execute("SELECT id FROM threads WHERE id=?", (identity,)).fetchone() is None
            if retained is not None:
                after_read = native_json("read-lineage", home, retained["id"])
                assert after_read["readable"] and after_read["source_marker_present"], after_read
        if retained_guardian_files is not None:
            assert s.owner_snapshot(request, retained["id"]) == retained_guardian_sql
            assert {path: hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
                for path in retained_guardian_files} == retained_guardian_files
        assert s.owner_snapshot(request, unrelated["id"]) == retained_sql
        return dict(case=name, outcome=outcome, source=source, precleanup_read=before_read,
            cache_effect_keys=sorted({identity, source["rollout_id"]}), unrelated_writer_progress=progress, native_binary_sha256=s.NATIVE_DIGEST)


if __name__ == "__main__":
    results = []
    names = ("actual-reverted", "stock-compressed-current", "retained-independent-paginated-fork",
                 "materialization-nonempty", "materialization-empty", "selected-ctime-changed",
                 "selected-inode-replaced", "selected-sql-pointer-changed", "selected-duplicate-representation",
                 "retained-reverted-guardian", "retained-reverted-guardian-selected-prefix",
                 "retained-compressed-current-guardian", "omitted-original", "additional-owned-file", "retained-original-provider",
                 "retained-cache-key-collision", "history-effect-overrun", "header-concatenated",
                 "header-skippable", "header-unread-tail", "header-physical-cap", "header-window-cap",
                 "header-output-cap", "header-plain-limit")
    if os.environ.get("GC_NATIVE_PHYSICAL_CONTROLLED") == "1":
        names = ("unrelated-recorders-append-during-header",)
    for name in names:
        result = run_case(name)
        results.append(result)
        print(json.dumps(result), flush=True)
    receipt = os.environ.get("GC_NATIVE_PHYSICAL_RECEIPT")
    if receipt:
        pathlib.Path(receipt).write_text(json.dumps(dict(cases=results, native_binary_sha256=s.NATIVE_DIGEST), indent=2) + "\n")
