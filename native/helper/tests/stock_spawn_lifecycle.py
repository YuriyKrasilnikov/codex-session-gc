"""Observe stock spawn and archive ownership in a fresh disposable home.

Observe persistent producer lifecycle, native guard admission, and child cleanup.
Each producer mode and turn outcome must be run and evaluated separately.
The supplied root must be a fresh task-owned /tmp/gc-root-closure-* directory.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import patch

STOCK_SHA = 'b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8'
MAX_TRACE = 16 * 1024 * 1024
MAX_RPC_LINE = 4 * 1024 * 1024
MAX_HTTP_BODY = 4 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(value: object, message: str) -> None:
    if not value:
        raise RuntimeError(message)


@contextlib.contextmanager
def defer_sigint():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None and not process.stdin.closed:
        with contextlib.suppress(OSError):
            process.stdin.close()
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=12)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class Trace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.size = 0
        self.lock = threading.Lock()

    def add(self, item: dict[str, object]) -> None:
        encoded = (json.dumps(item, sort_keys=True, separators=(',', ':')) + '\n').encode()
        with self.lock:
            if self.size + len(encoded) > MAX_TRACE:
                raise RuntimeError('trace byte bound exceeded')
            with self.path.open('ab') as output:
                output.write(encoded)
            self.size += len(encoded)


class Stub(HTTPServer):
    allow_reuse_address = False

    def __init__(self, trace: Trace, mode: str, sibling: bool, wake: bool) -> None:
        self.mode = mode
        self.wake = wake
        self.sleep_sent = False
        self.error = None
        self.spawn_limit = 2 if sibling else 1
        self.spawn_count = 0
        super().__init__(('127.0.0.1', 0), Handler)
        self.trace = trace
        self.spawn_sent = False
        self.cancel_started = threading.Event()
        self.cancel_release = threading.Event()
        self.count = 0

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def advertises_spawn(value: object) -> bool:
    if isinstance(value, dict):
        if value.get('name') == 'multi_agent_v1' and isinstance(value.get('tools'), list):
            return any(isinstance(tool, dict) and tool.get('name') == 'spawn_agent' for tool in value['tools'])
        return any(advertises_spawn(item) for item in value.values())
    if isinstance(value, list):
        return any(advertises_spawn(item) for item in value)
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.connection.settimeout(10)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            require(0 < length <= MAX_HTTP_BODY, 'model request exceeds body bound')
            body = json.loads(self.rfile.read(length))
            require(isinstance(body, dict), 'model request is not an object')
            self.server.count += 1
            require(self.server.count <= 40, 'model request count exceeded')
            self.server.trace.add({'event': 'stub_tools', 'tools': body.get('tools', [])})
            request_input = body.get('input', [])
            self.server.trace.add({'event': 'stub_input', 'input': request_input})
            encoded = json.dumps(request_input)
            if 'INJECT-TURN-ERROR' in encoded:
                self.send_error(500, 'scripted local response failure')
                self.server.trace.add({'event': 'stub_response', 'action': 'scripted_error'})
                return
            if 'INJECT-TURN-CANCEL' in encoded:
                self.server.cancel_started.set()
                require(self.server.cancel_release.wait(15), 'cancel response release timed out')
            child_input = any(isinstance(value, dict) and (value.get('role') == 'user' or value.get('type') == 'agent_message')
                              and 'CHILD-INERT' in json.dumps(value.get('content', ''))
                              for value in request_input)
            if self.server.wake and child_input and not self.server.sleep_sent:
                require(any(isinstance(tool, dict) and tool.get('name') == 'clock'
                            and any(t.get('name') == 'sleep' for t in tool.get('tools', []) if isinstance(t, dict))
                            for tool in body.get('tools', [])), 'stock did not advertise clock.sleep')
                self.server.sleep_sent = True
                item = {'type': 'function_call', 'call_id': 'fixture-sleep', 'namespace': 'clock',
                        'name': 'sleep', 'arguments': json.dumps({'duration_ms': 60000})}
                action = 'clock.sleep'
            elif self.server.spawn_count < self.server.spawn_limit and 'SPAWN-PARENT' in encoded and not child_input:
                tools = body.get('tools', [])
                if self.server.mode == 'v1':
                    require(advertises_spawn(tools), 'stock request did not advertise multi_agent_v1.spawn_agent')
                    item = {'type': 'function_call', 'call_id': 'fixture-spawn', 'namespace': 'multi_agent_v1', 'name': 'spawn_agent', 'arguments': json.dumps({'message': 'CHILD-INERT'})}
                else:
                    require(any(isinstance(tool, dict) and tool.get('name') == 'collaboration' and any(isinstance(t, dict) and t.get('name') == 'spawn_agent' for t in tool.get('tools', [])) for tool in tools), 'stock request did not advertise collaboration.spawn_agent')
                    item = {'type': 'function_call', 'call_id': 'fixture-spawn', 'namespace': 'collaboration', 'name': 'spawn_agent', 'arguments': json.dumps({'task_name': 'child', 'message': 'CHILD-INERT'})}
                self.server.spawn_count += 1
                item['call_id'] = 'fixture-spawn-' + str(self.server.spawn_count)
                if self.server.mode == 'v2':
                    item['arguments'] = json.dumps({'task_name': 'child' + str(self.server.spawn_count), 'message': 'CHILD-INERT'})
                self.server.spawn_sent = True
                action = 'spawn_agent'
            else:
                item = {'type': 'message', 'role': 'assistant', 'id': 'fixture-message', 'content': [{'type': 'output_text', 'text': 'inert done'}]}
                action = 'inert_message'
            events = [{'type': 'response.created', 'response': {'id': 'fixture-response', 'status': 'in_progress'}}, {'type': 'response.output_item.done', 'item': item}, {'type': 'response.completed', 'response': {'id': 'fixture-response', 'status': 'completed', 'output': [item]}}]
            data = (''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
            self.server.trace.add({'event': 'stub_response', 'ordinal': self.server.count, 'action': action, 'request_bytes': length})
        except (OSError, ValueError, RuntimeError) as error:
            if (isinstance(error, BrokenPipeError) and self.server.cancel_started.is_set()
                    and self.server.cancel_release.is_set()):
                self.server.trace.add({'event': 'stub_cancel_disconnect', 'type': type(error).__name__})
            else:
                self.server.error = str(error)
                self.server.trace.add({'event': 'stub_error', 'type': type(error).__name__, 'message': str(error)})
                with contextlib.suppress(OSError):
                    self.send_error(400)


class Rpc:
    def __init__(self, stock: Path, home: Path, root: Path, trace: Trace) -> None:
        self.trace = trace
        self.pending = bytearray()
        self.events = []
        self.sequence = 0
        self.total = 0
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home), 'TMPDIR': str(root), 'NO_PROXY': '*', 'OTEL_SDK_DISABLED': 'true', 'DO_NOT_TRACK': '1'}
        self.process = subprocess.Popen([str(stock), 'app-server', '--stdio'], cwd=home, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        selector = None
        try:
            selector = selectors.DefaultSelector()
            require(self.process.stdout is not None and self.process.stdin is not None, 'missing RPC pipes')
            selector.register(self.process.stdout, selectors.EVENT_READ)
            self.selector = selector
        except BaseException:
            if selector is not None:
                selector.close()
            stop(self.process)
            raise

    def send(self, value: dict[str, object]) -> None:
        self.trace.add({'event': 'rpc_send', 'message': value})
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, object], seconds: float = 25) -> dict[str, object]:
        self.sequence += 1
        identifier = self.sequence
        self.send({'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while b'\n' in self.pending:
                line, _, rest = self.pending.partition(b'\n')
                self.pending = bytearray(rest)
                if not line:
                    continue
                self.total += len(line)
                require(self.total <= MAX_TRACE, 'RPC byte bound exceeded')
                msg = json.loads(line)
                require(isinstance(msg, dict), 'non-object RPC message')
                self.trace.add({'event': 'rpc_receive', 'message': msg})
                if 'method' in msg:
                    self.events.append(msg)
                if msg.get('id') == identifier:
                    return msg
            require(len(self.pending) <= MAX_RPC_LINE, 'RPC line exceeds bound')
            if not self.selector.select(max(0, deadline - time.monotonic())):
                break
            assert self.process.stdout is not None
            data = os.read(self.process.stdout.fileno(), 65536)
            require(bool(data), 'app-server ended before response')
            self.pending.extend(data)
        raise TimeoutError('RPC response deadline exceeded: ' + method)

    def close(self) -> None:
        self.selector.close()
        stop(self.process)


def closure(home: Path, parent: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect((home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0) as db:
        db.execute('PRAGMA query_only=ON')
        rows = db.execute(
            'WITH RECURSIVE scope(id) AS (SELECT ? UNION SELECT child_thread_id '
            'FROM thread_spawn_edges JOIN scope ON parent_thread_id=scope.id) '
            'SELECT parent_thread_id,child_thread_id,status FROM thread_spawn_edges '
            'WHERE parent_thread_id IN (SELECT id FROM scope) ORDER BY 1,2 LIMIT 101',
            (parent,),
        ).fetchall()
    require(len(rows) <= 100, 'spawn closure exceeds observation bound')
    return rows


def observe_threads(home: Path, identifiers: tuple[str, str]) -> list[dict[str, object]] | None:
    with sqlite3.connect((home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0) as db:
        db.execute('PRAGMA query_only=ON')
        rows = db.execute('SELECT id,source,rollout_path FROM threads WHERE id IN (?,?)', identifiers).fetchall()
    if len(rows) < 2:
        return None
    require(len(rows) == 2, 'ambiguous stock thread rows')
    observed = []
    for identifier, source, raw_path in rows:
        raw = Path(raw_path)
        require(raw.is_absolute() and raw.is_relative_to(home), 'rollout path is outside the disposable home')
        require(not raw.is_symlink(), 'rollout path is a symlink')
        try:
            path = raw.resolve(strict=True)
        except FileNotFoundError:
            return None
        require(path.is_relative_to(home.resolve(strict=True)) and path.is_file(), 'rollout is outside the disposable home')
        with path.open('rb') as stream:
            first_line = stream.readline(1024 * 1024 + 1)
        require(first_line.endswith(b'\n') and len(first_line) <= 1024 * 1024, 'stock header unavailable')
        envelope = json.loads(first_line)
        require(envelope.get('type') == 'session_meta' and envelope.get('payload', {}).get('id') == identifier, 'stock header identity differs')
        observed.append({'id': identifier, 'sql_source': source, 'header_source': envelope['payload'].get('source')})
    return observed


def drain(rpc: Rpc, seconds: float) -> None:
    result = rpc.request('thread/loaded/list', {}, seconds=max(0.1, min(5, seconds)))
    require('result' in result, 'loaded thread observation failed while waiting')


def guard_try(fixture: Path, home: Path, identifier: str, trace: Trace) -> int:
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home)}
    process = None
    try:
        with defer_sigint():
            process = subprocess.Popen([str(fixture), 'try-guard', str(home), identifier], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            trace.add({'event': 'guard_try_started', 'thread': identifier, 'pid': process.pid})
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=12)
            trace.add({'event': 'guard_try_timeout_reaped', 'thread': identifier, 'pid': process.pid, 'exit': process.returncode})
            raise
        require(len(stdout) + len(stderr) <= 65536, 'guard response exceeds bound')
        trace.add({'event': 'guard_try_reaped', 'thread': identifier, 'pid': process.pid, 'exit': process.returncode, 'stdout': stdout.decode(errors='replace'), 'stderr': stderr.decode(errors='replace')})
        require(process.returncode != 0 and b'already has an active writer' in stderr, 'loaded stock writer ownership was not observed')
        return process.returncode
    except BaseException:
        if process is not None and process.returncode is None:
            stop(process)
            trace.add({'event': 'guard_try_cleanup', 'thread': identifier, 'pid': process.pid, 'exit': process.returncode})
        raise


def hold(fixture: Path, home: Path, identifier: str) -> subprocess.Popen[bytes]:
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home)}
    process = subprocess.Popen([str(fixture), 'hold-guard', str(home), identifier], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    selector = None
    try:
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        require(bool(selector.select(15)), 'guard readiness timeout')
        require(process.stdout.readline(128) == b'ready\n', 'guard was not acquired')
        return process
    except BaseException:
        stop(process)
        raise
    finally:
        if selector is not None:
            selector.close()


def apply_child(native: Path, fixture: Path, home: Path, root: Path,
                parent: str, child: str, trace: Trace, siblings: tuple[str, ...] = ()) -> dict[str, object]:
    # The shared direct harness constructs fresh physical and SQL incidence
    # bounds independently from the disposable producer's actual stored data.
    os.environ['GC_NATIVE_BINARY'] = str(native)
    os.environ['GC_NATIVE_FIXTURE'] = str(fixture)
    path = Path(__file__).with_name('closed_work_batch.py')
    spec = importlib.util.spec_from_file_location('stock_closed_work_batch', path)
    assert spec is not None and spec.loader is not None
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    semantic = batch.s
    aid, uid = semantic.j.AttemptId(uuid4()), semantic.j.UnitId(uuid4())
    payload = semantic.j.Payload('fixture/stock-producer', 1, b'disposable stock lifecycle')
    journal = root / ('receipt-' + str(uuid4()) + '.sqlite')
    semantic.j.create(journal, semantic.j.StartEnvelope(aid, payload, payload, payload, payload),
                      (semantic.j.Unit(uid, 0, payload),))
    ownership = sorted((parent, child, *siblings))
    request = dict(version=6, journal=str(journal), attempt_id=str(aid.value),
                   unit_id=str(uid.value), codex_home=str(home), sqlite_home=str(home),
                   provider='inert', cutoff='2099-01-01T00:00:00Z',
                   ownership_scope=dict(root_thread_id=parent, thread_ids=ownership),
                   deletion_scope=[child], targets=[], databases=[], guard_paths=sorted([
                       str(home / '.tmp/rollout-maintenance.lock'),
                       str(home / 'thread-writer-locks/.coordination.lock'),
                       *[str(home / 'thread-writer-locks' / (identity + '.lock'))
                         for identity in ownership]]))
    batch.refresh(request)
    clean_env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home),
                 'TMPDIR': str(root), 'NO_PROXY': '*', 'OTEL_SDK_DISABLED': 'true',
                 'DO_NOT_TRACK': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    with patch.dict(os.environ, clean_env, clear=True):
        outcome = semantic.dispatch(request, aid, uid, payload)
    trace.add({'event': 'native_apply', 'outcome': outcome,
               'native_sha256': semantic.NATIVE_DIGEST})
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('v1', 'v2'), required=True)
    parser.add_argument('--turn-outcome', choices=('clean', 'error', 'cancel'), default='clean')
    parser.add_argument('--sibling', action='store_true')
    parser.add_argument('--wake', action='store_true', help='request V2 child durable sleep')
    parser.add_argument('--expect-native', choices=('confirmed', 'old-closed-refusal'), default='confirmed')
    parser.add_argument('--stock', type=Path, required=True)
    parser.add_argument('--stock-sha256', required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--native', type=Path, required=True)
    parser.add_argument('--native-sha256', required=True)
    parser.add_argument('--fixture-sha256', required=True, help='expected SHA-256 of the exact guard fixture under test')
    args = parser.parse_args()
    for path in (args.root, args.stock, args.fixture, args.native):
        require(path.is_absolute(), 'all paths must be absolute')
    root = args.root.resolve(strict=True)
    require(root.name.startswith('gc-root-closure-') and root.parent == Path('/tmp')
            and not any(root.iterdir()), 'root must be an empty task-owned probe directory')
    require(args.stock_sha256 == STOCK_SHA and digest(args.stock) == args.stock_sha256
            and digest(args.fixture) == args.fixture_sha256
            and digest(args.native) == args.native_sha256, 'qualified executable identity differs')
    home = root / 'home'
    require(not home.exists(), 'probe home already exists')
    home.mkdir(mode=0o700)
    trace = Trace(root / 'trace.jsonl')
    signal_state = {'cleaning': False, 'interrupted': False}
    def interrupt(_signum, _frame):
        signal_state['interrupted'] = True
        if not signal_state['cleaning']:
            raise KeyboardInterrupt('SIGINT requested bounded probe shutdown')
    signal.signal(signal.SIGINT, interrupt)
    trace.add({'event': 'controller_started', 'pid': os.getpid(),
               'script_sha256': digest(Path(__file__)), 'stock_sha256': args.stock_sha256,
               'fixture_sha256': args.fixture_sha256, 'native_sha256': args.native_sha256,
               'mode': args.mode, 'turn_outcome': args.turn_outcome,
               'sibling': args.sibling, 'wake': args.wake,
               'expect_native': args.expect_native})
    require(not args.wake or args.mode == 'v2', 'wake scenario requires v2')
    server = Stub(trace, args.mode, args.sibling, args.wake)
    server_thread = threading.Thread(target=server.serve_forever, name='owned-loopback-stub')
    try:
        server_thread.start()
    except BaseException:
        if server_thread.is_alive():
            server.shutdown()
        server.server_close()
        if server_thread.ident is not None:
            server_thread.join(timeout=15)
        raise
    rpc = None
    guards: list[subprocess.Popen[bytes]] = []
    status = 'failed'
    try:
        (home / 'config.toml').write_text('model="local-inert"\nmodel_provider="inert"\n[features]\nplugins=false\nlocal_thread_store_compression=false\nbackground_paginated_rollout_migration=false\nmulti_agent=true\nmulti_agent_v2=' + ('true' if args.mode == 'v2' else 'false') + '\n[features.sleep_tool]\nmode="always_on"\n[model_providers.inert]\nname="Inert"\nbase_url="http://127.0.0.1:' + str(server.server_port) + '/v1"\nwire_api="responses"\nrequest_max_retries=0\nstream_max_retries=0\n')
        with defer_sigint():
            rpc = Rpc(args.stock, home, root, trace)
            trace.add({'event': 'app_server_started', 'pid': rpc.process.pid})
        response = rpc.request('initialize', {'clientInfo': {'name': 'disposable-spawn-witness', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
        require('result' in response, 'initialize failed')
        rpc.send({'method': 'initialized', 'params': {}})
        start = {'model': 'local-inert', 'modelProvider': 'inert', 'cwd': str(home), 'ephemeral': False, 'historyMode': 'legacy', 'approvalPolicy': 'never', 'sandbox': 'read-only'}
        parent_response = rpc.request('thread/start', start)
        parent = str(UUID(parent_response['result']['thread']['id']))
        other_response = rpc.request('thread/start', start)
        other = str(UUID(other_response['result']['thread']['id']))
        require(parent != other, 'unrelated thread is not distinct')
        require('result' in rpc.request('turn/start', {'threadId': parent, 'input': [{'type': 'text', 'text': 'SPAWN-PARENT'}]}), 'parent turn was not started')
        deadline = time.monotonic() + 45
        child_rows: list[tuple[str, str, str]] = []
        while time.monotonic() < deadline:
            drain(rpc, deadline - time.monotonic())
            require(server.error is None, 'scripted endpoint: ' + str(server.error))
            if (home / 'state_5.sqlite').exists():
                child_rows = closure(home, parent)
            if len(child_rows) >= (2 if args.sibling else 1):
                break
            time.sleep(0.2)
        require(server.spawn_count == (2 if args.sibling else 1)
                and len(child_rows) == server.spawn_count
                and all(row[0] == parent for row in child_rows), 'stock producer did not establish expected descendant edges')
        child = str(UUID(child_rows[0][1]))
        siblings = tuple(str(UUID(row[1])) for row in child_rows[1:])
        require(child not in (parent, other), 'child identity is not distinct')
        deadline = time.monotonic() + 20
        observations = None
        while time.monotonic() < deadline:
            drain(rpc, deadline - time.monotonic())
            observations = observe_threads(home, (parent, child))
            if observations is not None:
                break
            time.sleep(0.2)
        require(observations is not None, 'stock thread materialization deadline exceeded')
        require(len(closure(home, parent)) == server.spawn_limit, 'unexpected spawn closure')
        trace.add({'event': 'materialized_threads', 'rows': observations})
        trace.add({'event': 'before_archive_closure', 'rows': closure(home, parent)})
        if args.wake:
            deadline = time.monotonic() + 25
            while not any(event.get('method') == 'item/started'
                          and event.get('params', {}).get('threadId') == child
                          and event['params'].get('item', {}).get('id') == 'fixture-sleep'
                          for event in rpc.events):
                require(time.monotonic() < deadline, 'V2 durable sleep not observed')
                drain(rpc, deadline - time.monotonic())
            require(server.sleep_sent, 'scripted sleep was not sent')
        deadline = time.monotonic() + 25
        while not any(event.get('method') == 'turn/completed'
                      and event.get('params', {}).get('threadId') == parent
                      and event['params']['turn']['status'] == 'completed' for event in rpc.events):
            require(time.monotonic() < deadline, 'parent clean turn completion not observed')
            drain(rpc, deadline - time.monotonic())
        if args.turn_outcome != 'clean':
            marker = 'INJECT-TURN-ERROR' if args.turn_outcome == 'error' else 'INJECT-TURN-CANCEL'
            turn_response = rpc.request('turn/start', {'threadId': parent, 'input': [{'type': 'text', 'text': marker}]})
            require('result' in turn_response, 'outcome turn was not started')
            turn_id = turn_response['result']['turn']['id']
            if args.turn_outcome == 'cancel':
                require(server.cancel_started.wait(10), 'scripted cancellation request did not reach stub')
                interrupted = rpc.request('turn/interrupt', {'threadId': parent, 'turnId': turn_id})
                require('result' in interrupted, 'turn interruption was not accepted')
                server.cancel_release.set()
            expected_status = 'failed' if args.turn_outcome == 'error' else 'interrupted'
            deadline = time.monotonic() + 25
            while not any(event.get('method') == 'turn/completed'
                          and event.get('params', {}).get('threadId') == parent
                          and event['params']['turn']['id'] == turn_id
                          and event['params']['turn']['status'] == expected_status for event in rpc.events):
                require(time.monotonic() < deadline, 'requested turn outcome not observed')
                drain(rpc, deadline - time.monotonic())
        guard_try(args.fixture, home, parent, trace)
        guard_try(args.fixture, home, child, trace)
        for sibling in siblings:
            guard_try(args.fixture, home, sibling, trace)
        busy_outcome = apply_child(args.native, args.fixture, home, root, parent, child, trace, siblings)
        require(busy_outcome['status'] == 'refused' and
                'active writer' in busy_outcome.get('detail', ''),
                'loaded producer did not refuse for writer ownership')
        require('result' in rpc.request('turn/start', {'threadId': other, 'input': [{'type': 'text', 'text': 'UNRELATED-INERT'}]}), 'unrelated turn was not started')
        archive = rpc.request('thread/archive', {'threadId': parent})
        trace.add({'event': 'archive_result', 'value': archive})
        require('result' in archive, 'stock archive failed')
        deadline = time.monotonic() + 25
        while True:
            loaded = rpc.request('thread/loaded/list', {})
            require('result' in loaded, 'loaded thread observation failed')
            loaded_ids = loaded['result']['data']
            if all(identity not in loaded_ids for identity in (parent, child, *siblings)):
                require(other in loaded_ids, 'unrelated stock thread did not remain loaded')
                break
            require(time.monotonic() < deadline, 'archive did not unload the selected threads')
            time.sleep(0.2)
        after = closure(home, parent)
        trace.add({'event': 'after_archive_closure', 'rows': after})
        require(after == [(parent, identity, 'open') for identity in (child, *siblings)], 'expected stock open edges after archive')
        # Archived sessions cannot resume even without a writer. Unarchive the
        # retained root through the public producer before testing guard order.
        require('result' in rpc.request('thread/unarchive', {'threadId': parent}),
                'retained root could not be unarchived for resume test')
        with sqlite3.connect((home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True) as db:
            archive_flags = {identity: db.execute('SELECT archived FROM threads WHERE id=?', (identity,)).fetchone()
                             for identity in (parent, child, *siblings)}
        require(all(flag == (0,) for flag in archive_flags.values()),
                'root unarchive did not restore whole producer closure')
        require(closure(home, parent) == after, 'unarchive changed producer edges')
        with defer_sigint():
            guards.append(hold(args.fixture, home, parent))
            trace.add({'event': 'guard_started', 'pid': guards[-1].pid, 'role': 'parent'})
        with defer_sigint():
            guards.append(hold(args.fixture, home, child))
            trace.add({'event': 'guard_started', 'pid': guards[-1].pid, 'role': 'child'})
        require(all(p.poll() is None for p in guards), 'guards were not simultaneously held')
        require(closure(home, parent) == after, 'spawn closure changed while guards held')
        trace.add({'event': 'simultaneous_guards', 'parent': parent, 'child': child, 'held': True})
        # A separate public server attempts cold resume while both guards are held.
        # Only an explicit active-writer refusal proves this bounded observation.
        contender = None
        try:
            with defer_sigint():
                contender = Rpc(args.stock, home, root, trace)
                trace.add({'event': 'contender_started', 'pid': contender.process.pid})
            require('result' in contender.request('initialize', {'clientInfo': {'name': 'guarded-resume', 'version': '1'}, 'capabilities': {'experimentalApi': True}}), 'contender initialize failed')
            contender.send({'method': 'initialized', 'params': {}})
            reply = contender.request('thread/resume', {'threadId': parent}, 8)
            require('result' not in reply and 'active writer' in str(reply.get('error', '')).lower(),
                    'guarded resume did not refuse for writer ownership')
            trace.add({'event': 'guarded_resume_refused', 'reply': reply})
        finally:
            if contender is not None:
                contender.close()
                trace.add({'event': 'contender_reaped', 'pid': contender.process.pid,
                           'exit': contender.process.returncode})
        loaded = rpc.request('thread/loaded/list', {})
        trace.add({'event': 'loaded_after', 'value': loaded})
        for guard in reversed(guards):
            stop(guard)
            trace.add({'event': 'guard_reaped', 'pid': guard.pid, 'exit': guard.returncode})
        guards.clear()
        # No status is written by the test: these are the producer's Open edges.
        require(closure(home, parent) == after, 'producer edges changed before apply')
        if siblings:
            # Isolate the sibling's ownership as a separate native negative
            # control. Public V2 cold resume of a child without its root refuses;
            # this fixture guard is not a loaded-stock-sibling observation.
            sibling_guard = None
            try:
                with defer_sigint():
                    sibling_guard = hold(args.fixture, home, siblings[0])
                    trace.add({'event': 'sibling_guard_started', 'pid': sibling_guard.pid})
                sibling_busy = apply_child(args.native, args.fixture, home, root, parent, child, trace, siblings)
                require(sibling_busy['status'] == 'refused' and siblings[0] in sibling_busy.get('detail', '')
                        and 'active writer' in sibling_busy.get('detail', ''),
                        'unselected sibling writer did not refuse deletion')
            finally:
                if sibling_guard is not None:
                    stop(sibling_guard)
                    trace.add({'event': 'sibling_guard_reaped', 'pid': sibling_guard.pid,
                               'exit': sibling_guard.returncode})
        selected_paths = [path for namespace in ('sessions', 'archived_sessions')
                          for path in (home / namespace).rglob('*')
                          if path.is_file() and child in path.name
                          and (path.name.endswith('.jsonl') or path.name.endswith('.jsonl.zst'))]
        require(bool(selected_paths), 'selected physical rollout paths missing before apply')
        trace.add({'event': 'selected_paths_before', 'paths': [str(path) for path in selected_paths]})
        child_before = observe_threads(home, (parent, child))
        require(child_before is not None, 'selected stock history missing before apply')
        outcome = apply_child(args.native, args.fixture, home, root, parent, child, trace, siblings)
        if args.expect_native == 'old-closed-refusal':
            require(outcome['status'] == 'refused' and 'closed' in outcome.get('detail', '').lower(),
                    'old native did not show its Closed-marker refusal')
            require(outcome.get('removed_rollouts') == [] and outcome.get('committed_databases') == [],
                    'old native reported effects')
            require(closure(home, parent) == after
                    and observe_threads(home, (parent, child)) == child_before,
                    'old native changed the selected relation or histories')
            status = 'old_closed_refusal_observed'
            return
        require(outcome['status'] == 'confirmed', 'native did not confirm stopped Open child')
        require(all(not path.exists() for path in selected_paths), 'selected physical rollout remains')
        with sqlite3.connect((home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True) as db:
            remaining = {row[0] for row in db.execute('SELECT id FROM threads WHERE id IN (?,?,?)',
                                                       (parent, child, other))}
        require(remaining == {parent, other}, 'selected or retained SQL identity mismatch')
        require(closure(home, parent) == [(parent, sibling, 'open') for sibling in siblings], 'selected edge remains or sibling changed')
        require('result' in rpc.request('thread/read', {'threadId': parent, 'includeTurns': True}),
                'retained parent is not readable')
        require('result' in rpc.request('thread/read', {'threadId': other, 'includeTurns': True}),
                'unrelated thread is not readable')
        for sibling in siblings:
            require('result' in rpc.request('thread/read', {'threadId': sibling, 'includeTurns': True}),
                    'retained sibling is not readable')
        missing_resume = rpc.request('thread/resume', {'threadId': child})
        require('result' not in missing_resume, 'deleted child resumed successfully')
        trace.add({'event': 'deleted_resume_refused', 'reply': missing_resume})
        resumed_parent = rpc.request('thread/resume', {'threadId': parent})
        require('result' in resumed_parent, 'retained parent did not resume')
        # Observe an unrelated assistant turn finish after the removal, not just
        # that the server accepted a start request.
        unrelated_turn = rpc.request('turn/start', {'threadId': other, 'input': [{'type': 'text', 'text': 'UNRELATED-AFTER-APPLY'}]})
        require('result' in unrelated_turn, 'unrelated turn after apply did not start')
        unrelated_id = unrelated_turn['result']['turn']['id']
        deadline = time.monotonic() + 25
        while not any(event.get('method') == 'turn/completed'
                      and event.get('params', {}).get('threadId') == other
                      and event['params']['turn']['id'] == unrelated_id
                      and event['params']['turn']['status'] == 'completed'
                      for event in rpc.events):
            require(time.monotonic() < deadline, 'unrelated turn after apply did not complete')
            drain(rpc, deadline - time.monotonic())
        status = 'observed'
    except BaseException as error:
        trace.add({'event': 'failure', 'type': type(error).__name__, 'message': str(error)})
        raise
    finally:
        signal_state['cleaning'] = True
        server.cancel_release.set()
        try:
            try:
                errors = []
                for guard in reversed(guards):
                    try:
                        stop(guard)
                        trace.add({'event': 'guard_reaped', 'pid': guard.pid, 'exit': guard.returncode})
                    except BaseException as error:
                        errors.append(error)
                if errors:
                    raise errors[0]
            finally:
                if rpc is not None:
                    rpc.close()
                    trace.add({'event': 'app_server_reaped', 'pid': rpc.process.pid, 'exit': rpc.process.returncode})
                    require(rpc.process.returncode == 0, 'app-server did not exit cleanly')
        finally:
            try:
                server.shutdown()
            finally:
                server.server_close()
                server_thread.join(timeout=15)
                trace.add({'event': 'shutdown', 'status': status, 'server_thread_alive': server_thread.is_alive(), 'app_exit': None if rpc is None else rpc.process.returncode, 'interrupted': signal_state['interrupted']})
                require(not server_thread.is_alive(), 'loopback server did not terminate')
    if signal_state['interrupted']:
        raise KeyboardInterrupt('probe was interrupted')
    print(json.dumps({'status': status, 'trace_sha256': digest(trace.path), 'model': 'scripted_loopback', 'stock_sha256': STOCK_SHA, 'fixture_sha256': args.fixture_sha256, 'native_sha256': args.native_sha256, 'mode': args.mode, 'turn_outcome': args.turn_outcome, 'scope': 'stock clean archive, guarded admission and direct native child-only apply'}))


if __name__ == '__main__':
    main()
