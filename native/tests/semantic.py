"""Shared current-native fixture oracles and exact schema-2 journal dispatch."""
from __future__ import annotations
from contextlib import contextmanager
import atexit, hashlib, json, os, pathlib, sqlite3, subprocess, sys, time, uuid
sys.dont_write_bytecode=True
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from codex_session_gc.recovery import journal as j
from codex_session_gc.execution.process_identity import OWNED_MARKER_ENV, observe_owned_child
BIN=pathlib.Path(os.environ.get('GC_NATIVE_BINARY',str(ROOT/'native/build/debug/codex-session-gc-native')))
FIXTURE=pathlib.Path(os.environ.get('GC_NATIVE_FIXTURE',str(ROOT/'native/build/debug/gc-native-fixture')))
with BIN.open('rb') as binary: NATIVE_DIGEST=hashlib.file_digest(binary,'sha256').hexdigest()
CHILDREN=[]
def spawn(*args,**kwargs):
 child=subprocess.Popen(*args,**kwargs); CHILDREN.append(child); return child
def reap_owned_children():
 for child in CHILDREN:
  if child.poll() is None: child.kill()
  child.wait(timeout=15)
atexit.register(reap_owned_children)
TABLES = (
 'threads', 'thread_dynamic_tools', 'thread_spawn_edges', 'thread_attachments',
 'guardian_review_feedback', 'thread_read_receipts',
 'logs', 'thread_goals', 'stage1_outputs', 'jobs', 'thread_items',
 'thread_realtime_items', 'thread_turns', 'thread_history_projection_state',
 'queued_items', 'queued_thread_revisions',
)
@contextmanager
def sqlite_context(*args,**kwargs):
 db=sqlite3.connect(*args,**kwargs)
 try:
  with db: yield db
 finally:
  db.close()

def connection(path):
 return sqlite_context(f'file:{path}?mode=ro',uri=True)

def sql_snapshot(req):
 snapshots={}
 for bound in req['databases']:
  with connection(bound['path']) as db:
   schema=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name").fetchall()
   tables={}
   for kind,name,_,_ in schema:
    if kind=='table':
     quoted='"'+name.replace('"','""')+'"'
     tables[name]=db.execute(f'SELECT * FROM {quoted} ORDER BY rowid').fetchall()
   snapshots[bound['path']]=(schema,tables)
 return snapshots

def history_snapshot(home):
 home=pathlib.Path(home); result={}
 paths=[home/'session_index.jsonl']
 for directory in ('sessions','archived_sessions'):
  paths.extend((home/directory).rglob('*'))
 for path in paths:
  if path.is_symlink(): result[str(path)]=('symlink',os.readlink(path))
  elif path.is_file(): result[str(path)]=('file',hashlib.sha256(path.read_bytes()).hexdigest())
  elif path.is_dir(): result[str(path)]=('directory',)
 return result

def owner_snapshot(req,identity):
 result={}
 columns={'board_templates':('board',""),'guardian_review_feedback':('thread_id',""),'thread_read_receipts':('thread_id',""),'threads':('id',""),'queued_items':('thread_id',""),'thread_goals':('thread_id',""),'thread_goal_continuation_deferrals':('thread_id',""),'jobs':('job_key'," AND kind='memory_stage1'"),'stage1_outputs':('thread_id',""),'logs':('thread_id',""),'thread_items':('thread_id',""),'thread_realtime_items':('thread_id',""),'thread_turns':('thread_id',""),'thread_history_projection_state':('thread_id',"")}
 for bound in req['databases']:
  with connection(bound['path']) as db:
   tables={r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
   for table,(column,extra) in columns.items():
    if table in tables: result[(bound['path'],table)]=db.execute(f'SELECT * FROM {table} WHERE {column}=?{extra} ORDER BY rowid',[identity]).fetchall()
 return result




def dispatch(req,aid,uid,payload):
 raw=json.dumps(req,separators=(',',':')).encode(); request=j.Payload('application/vnd.codex-session-gc.legacy-native-request+json',1,raw)
 receipt=pathlib.Path(req['journal'])
 launch=j.LaunchId(uuid.uuid4())
 j.record_launch_intent(receipt,aid,j.LaunchIntent(launch,1,j.LaunchPurpose.APPLY,request))
 env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONDONTWRITEBYTECODE='1'); env[OWNED_MARKER_ENV]=str(uuid.uuid4())
 process=spawn([sys.executable,'-m','codex_session_gc.execution.owned_launch',str(BIN)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
 identity=None
 for _ in range(100):
  identity=observe_owned_child(process.pid,env)
  if identity is not None: break
  time.sleep(.005)
 assert identity is not None,'owned launcher incarnation unavailable'
 j.register_launch(receipt,aid,j.LaunchRegistration(launch,1,identity.incarnation()))
 j.record_dispatch_intent(receipt,aid,uid,request,payload,identity.incarnation())
 stdout,stderr=process.communicate(b'codex-session-gc:grant-v1\n'+raw,timeout=30)
 j.record_launch_closure(receipt,aid,launch,1,j.TerminalKind.CONFIRMED,j.Payload('fixture/native-process-exit',1,str(process.returncode).encode()))
 outcome=json.loads(stdout); assert not stderr,stderr.decode()
 assert process.returncode=={'confirmed':0,'refused':1,'partial':2,'unknown':3}[outcome['status']],(process.returncode,outcome)
 assert outcome['request_sha256']==list(hashlib.sha256(raw).digest()),outcome
 evidence=stdout
 j.record_terminal_evidence(receipt,aid,uid,request.digest,j.TerminalKind(outcome['status']),j.Payload('fixture/native-outcome',1,evidence))
 try: j.record_dispatch_intent(receipt,aid,uid,request,payload,payload)
 except j.TransitionError: pass
 else: raise AssertionError('dispatch journal admitted a repeat')
 return outcome
