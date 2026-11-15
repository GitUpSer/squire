"""Commit-before-ack inbox and outbox; never repeat uncertain agent/tool execution."""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

class ScopeError(ValueError): pass
class QueueFull(RuntimeError): pass

class Store:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS inbox (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
              payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              received REAL NOT NULL, started REAL, finished REAL, failure TEXT);
            CREATE TABLE IF NOT EXISTS outbox (
              response_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt REAL NOT NULL DEFAULT 0, operation_id TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tools (
              operation_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,tool_name TEXT NOT NULL,
              argument_digest TEXT NOT NULL,state TEXT NOT NULL,started REAL NOT NULL,finished REAL);
            CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY CHECK(id=1),pid INTEGER,generation INTEGER,ready INTEGER,model_ready INTEGER,updated REAL);
            ''')
        os.chmod(self.path,0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=20)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally: db.close()

    def accept(self,events,config):
        if not isinstance(events,list) or not 1<=len(events)<=20: raise ScopeError("invalid event batch")
        accepted=[]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            pending=db.execute("SELECT COUNT(*) FROM inbox WHERE state IN ('pending','running','uncertain')").fetchone()[0]
            for event in events:
                allowed={"event_id","conversation_id","tenant_id","runtime_id","chat_type","sender_id","provider_message_id","timestamp","payload"}
                if not isinstance(event,dict) or set(event)!=allowed: raise ScopeError("invalid event shape")
                if (event["conversation_id"],event["tenant_id"],event["runtime_id"],event["chat_type"])!=(config.conversation_id,config.tenant_id,config.runtime_id,config.kind): raise ScopeError("event outside runtime scope")
                if any(not isinstance(event[key],str) or not 1<=len(event[key])<=200 for key in ("event_id","sender_id","provider_message_id")): raise ScopeError("invalid event identity")
                payload=event["payload"]
                if not isinstance(payload,dict) or set(payload)-{"text","reply_to","mentions","media"}: raise ScopeError("invalid event payload")
                if not isinstance(payload.get("text",""),str) or len(payload.get("text",""))>32000: raise ScopeError("invalid text")
                encoded=json.dumps(event,sort_keys=True,separators=(",",":"));fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
                previous=db.execute("SELECT fingerprint FROM inbox WHERE event_id=?",(event["event_id"],)).fetchone()
                if previous:
                    if previous["fingerprint"]!=fingerprint: raise ScopeError("event identity reused with different payload")
                else:
                    if pending>=200: raise QueueFull("runtime inbox full")
                    db.execute("INSERT INTO inbox(event_id,fingerprint,payload,received) VALUES(?,?,?,?)",(event["event_id"],fingerprint,encoded,time.time()));pending+=1
                accepted.append(event["event_id"])
        return accepted

    def recover(self):
        with self.db() as db:
            rows=db.execute("SELECT event_id FROM inbox WHERE state='running'").fetchall()
            db.execute("UPDATE inbox SET state='uncertain',failure='process_interrupted' WHERE state='running'")
            db.execute("UPDATE tools SET state='uncertain' WHERE state='running'")
            return [row[0] for row in rows]

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            # One uncertain operation blocks later execution until explicit reconciliation.
            if db.execute("SELECT 1 FROM inbox WHERE state IN ('running','uncertain') LIMIT 1").fetchone(): return None
            row=db.execute("SELECT event_id,payload FROM inbox WHERE state='pending' ORDER BY sequence LIMIT 1").fetchone()
            if not row:return None
            db.execute("UPDATE inbox SET state='running',started=? WHERE event_id=?",(time.time(),row[0]))
            return json.loads(row[1])

    def finish(self,event_id,success):
        with self.db() as db:
            db.execute("UPDATE inbox SET state=?,finished=?,failure=? WHERE event_id=? AND state='running'",("done" if success else "uncertain",time.time(),None if success else "execution_outcome_unknown",event_id))

    def resolve(self,event_id):
        with self.db() as db:
            return db.execute("UPDATE inbox SET state='reconciled',finished=? WHERE event_id=? AND state='uncertain'",(time.time(),event_id)).rowcount==1

    def enqueue(self,payload):
        key=payload["response_key"];encoded=json.dumps(payload,sort_keys=True,separators=(",",":"))
        with self.db() as db:
            previous=db.execute("SELECT payload FROM outbox WHERE response_key=?",(key,)).fetchone()
            if previous and previous[0]!=encoded: raise ScopeError("response key reused")
            db.execute("INSERT OR IGNORE INTO outbox(response_key,payload,created) VALUES(?,?,?)",(key,encoded,time.time()))
        return key

    def outbox(self):
        with self.db() as db:
            row=db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 1",(time.time(),)).fetchone()
            return dict(row) if row else None

    def outbox_result(self,key,operation_id=None,terminal=False):
        with self.db() as db:
            db.execute("UPDATE outbox SET state=?,operation_id=?,attempts=attempts+1,next_attempt=? WHERE response_key=?",("accepted" if operation_id else "cancelled" if terminal else "pending",operation_id,time.time()+10,key))

    def status(self,generation):
        with self.db() as db:
            row=db.execute("SELECT * FROM runtime WHERE id=1").fetchone()
            uncertain=db.execute("SELECT COUNT(*) FROM inbox WHERE state='uncertain'").fetchone()[0]
            alive=False
            if row and row["generation"]==generation:
                try: os.kill(row["pid"],0);alive=True
                except (ProcessLookupError,PermissionError): pass
            return {"ready":bool(alive and row["ready"]),"model_ready":bool(alive and row["ready"] and row["model_ready"]),"deployment_generation":generation,"uncertain":uncertain}

    def mark_runtime(self,generation,ready,model_ready):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO runtime VALUES(1,?,?,?,?,?)",(os.getpid(),generation,int(ready),int(model_ready),time.time()))

    def active_event(self):
        with self.db() as db:
            row=db.execute("SELECT event_id FROM inbox WHERE state='running' LIMIT 1").fetchone()
            return row[0] if row else None

    def tool_start(self,tool_name,args,call_id):
        event_id=self.active_event()
        if not event_id:return None
        digest=hashlib.sha256(json.dumps(args,sort_keys=True,default=str).encode()).hexdigest()
        operation=hashlib.sha256((event_id+":"+str(call_id)+":"+tool_name+":"+digest).encode()).hexdigest()
        with self.db() as db:
            prior=db.execute("SELECT state FROM tools WHERE operation_id=?",(operation,)).fetchone()
            if prior:return False
            db.execute("INSERT INTO tools VALUES(?,?,?,?,?,?,NULL)",(operation,event_id,tool_name,digest,"running",time.time()))
        return operation

    def tool_finish(self,tool_name,args,call_id):
        event_id=self.active_event()
        if not event_id:return
        digest=hashlib.sha256(json.dumps(args,sort_keys=True,default=str).encode()).hexdigest()
        operation=hashlib.sha256((event_id+":"+str(call_id)+":"+tool_name+":"+digest).encode()).hexdigest()
        with self.db() as db:db.execute("UPDATE tools SET state='done',finished=? WHERE operation_id=?",(time.time(),operation))
