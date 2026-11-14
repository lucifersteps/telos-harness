"""Retention requires durable delivery and preserves pending/uncertain writes."""
import json
import sqlite3
from types import SimpleNamespace
from context_retention import retention_status


def test_delivery_gate_pending_uncertain_and_schedules():
    db=sqlite3.connect(':memory:')
    db.executescript('''
    CREATE TABLE messages(id TEXT,session TEXT,delivered INTEGER,reply TEXT);
    CREATE TABLE drafts(session TEXT,origin TEXT,result TEXT);
    CREATE TABLE proactive_events(session TEXT,input_id TEXT,state TEXT,message TEXT,observation TEXT);
    CREATE TABLE schedule_outbox(session TEXT,seq INTEGER,key TEXT);
    ''')
    pending={'origin':'pending'}
    channel=SimpleNamespace(known_ref=lambda s:None,business=SimpleNamespace(db=lambda:db,pending=lambda s:pending))
    ids=['done','unsent','pending','uncertain','queued','sent','quiet','timer','desktop']
    for key,delivered in [('done',1),('unsent',0),('pending',1),('uncertain',1),('timer',1)]:
        db.execute('INSERT INTO messages VALUES (?,?,?,?)',(key,'s',delivered,'reply'))
    db.execute('INSERT INTO drafts VALUES (?,?,?)',('s','uncertain',json.dumps({'status':'unknown'})))
    for key,state in [('queued','queued'),('sent','sent'),('quiet','quiet')]:
        db.execute('INSERT INTO proactive_events VALUES (?,?,?,?,?)',('s',key,state,'notice','observation'))
    db.execute('INSERT INTO schedule_outbox VALUES (?,?,?)',('s',8,'timer'))
    turns=[dict(turn=i,seq=i,inputs=[key],users=[] if key in {'queued','sent','quiet','timer'} else [key]) for i,key in enumerate(ids,1)]
    result=retention_status(channel,{'session':'s','turns':turns})
    assert result['eligible']==[1,6,7,8]
    assert [r['turn'] for r in result['recent']]==[7,8]
    pending.clear()
    assert 3 in retention_status(channel,{'session':'s','turns':turns})['eligible']
    assert db.execute('SELECT count(*) FROM messages').fetchone()[0]==5
    db.close()


def test_unresolved_ledgers_block_retirement_until_settled():
    db=sqlite3.connect(':memory:')
    db.executescript("""
    CREATE TABLE messages(id TEXT,session TEXT,delivered INTEGER,reply TEXT);
    CREATE TABLE drafts(session TEXT,origin TEXT,result TEXT);
    CREATE TABLE proactive_events(session TEXT,input_id TEXT,state TEXT,message TEXT,observation TEXT);
    CREATE TABLE schedule_outbox(session TEXT,seq INTEGER,key TEXT);
    INSERT INTO messages VALUES ('human','s',1,'已解释当前情况');
    INSERT INTO drafts VALUES ('s','human','{}');
    INSERT INTO proactive_events VALUES ('s','timer','queued','提醒',NULL);
    """)
    pending={}
    channel=SimpleNamespace(known_ref=lambda s:None,business=SimpleNamespace(db=lambda:db,pending=lambda s:pending))
    turns=[dict(turn=1,seq=10,inputs=['human'],users=['human']),dict(turn=2,seq=20,inputs=['timer'],users=[])]
    def eligible():return retention_status(channel,{'session':'s','turns':turns})['eligible']
    for state in ['executing','unknown','failed','partial']:
        db.execute('UPDATE drafts SET result=?',(json.dumps({'status':state}),))
        assert 1 not in eligible()
    db.execute('UPDATE drafts SET result=?',(json.dumps({'status':'saved','saved':True}),))
    assert 1 in eligible()
    pending.update(origin='human',confirmation_key='confirm')
    assert 1 not in eligible()
    pending.clear()
    for state in ['running','queued','unknown','failed']:
        db.execute('UPDATE proactive_events SET state=?',(state,))
        assert 2 not in eligible()
    db.execute("UPDATE proactive_events SET state='sent'")
    assert eligible()==[1,2]
    db.execute("UPDATE messages SET delivered=0")
    assert 1 not in eligible()
    db.close()
