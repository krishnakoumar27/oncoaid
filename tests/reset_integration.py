"""Permanent deletion and re-registration checks in a disposable PostgreSQL schema.

Run: docker compose exec -T api python tests/reset_integration.py
"""
import os
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient
import api
import telegram_channel as tg
from maintenance import delivery_guard

schema = 'reset_check_' + uuid4().hex
os.environ.update(ADMIN_EMAIL='reset@oncoaid.local', ADMIN_PASSWORD='reset check phrase')
with psycopg.connect(autocommit=True) as root:
    root.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with patch.object(api, 'connect', lambda: psycopg.connect(options=f'-c search_path={schema}',row_factory=dict_row)):
            with TestClient(api.app) as client:
                auth = client.post('/login',json={'email':'reset@oncoaid.local','password':'reset check phrase'}).json()
                headers = {'Authorization':'Bearer '+auth['token']}
                with api.connect() as db:
                    worker = db.execute("INSERT INTO staff(email,password,role) VALUES ('reset-worker@oncoaid.local',%s,'worker') RETURNING id",(api.password_hash('worker check phrase'),)).fetchone()['id']
                    for pid in (1,2):
                        p = db.execute('INSERT INTO patients(phone,profile,owner,archived_at) VALUES (%s,%s,%s,CASE WHEN %s THEN now() ELSE NULL END) RETURNING id',
                                       (f'+91000000000{pid}',Jsonb({'name':'Private Patient','consent':True}),worker,pid==1)).fetchone()['id']
                        source = db.execute('SELECT * FROM schemes LIMIT 1').fetchone()
                        app = db.execute('INSERT INTO applications(patient_id,scheme_id,snapshot,draft) VALUES (%s,%s,%s,%s) RETURNING id',(p,source['id'],Jsonb({'name':source['name'],'requirements':['Identity proof']}),'Private application details')).fetchone()['id']
                        db.execute("INSERT INTO documents(patient_id,application_id,requirement,filename,content,ai_result,ai_authorized,ai_job) VALUES (%s,%s,'Identity proof','private.pdf',%s,%s,true,'private-job')",(p,app,b'%PDF-private',Jsonb({'status':'completed','fields':{'name':'Private Patient'}})))
                        db.execute("INSERT INTO reminders(patient_id,application_id,audience,message,due) VALUES (%s,%s,'patient','Private reminder',now())",(p,app))
                        api.audit(db,worker,'private_case_event',{'private':'details'},p)
                    db.execute("INSERT INTO telegram_bots(id,username,next_offset) VALUES (123,'reset_bot',9000)")
                    for chat,pid,phone in ((456,1,'+910000000001'),(789,2,'+910000000002'),(999,None,'+910000000009')):
                        db.execute('INSERT INTO telegram_contacts(bot_id,chat_id,patient_id,phone,consent,suggested_fields,journey) VALUES (123,%s,%s,%s,true,%s,%s)',(chat,pid,phone,Jsonb({'diagnosis':'Private diagnosis'}),Jsonb({'phase':'processing','application_id':pid})))
                        db.execute("INSERT INTO telegram_messages(key,bot_id,chat_id,payload,state,reply) VALUES (%s,123,%s,%s,'ready','Private reply')",(f'old:{chat}',chat,Jsonb({'text':'Private message'})))
                        db.execute('INSERT INTO whatsapp_contacts(phone,patient_id,consent) VALUES (%s,%s,true)',(phone,pid))
                        db.execute("INSERT INTO whatsapp_messages(sid,phone,payload) VALUES (%s,%s,%s)",(f'old:{chat}',phone,Jsonb({'Body':'Private message'})))
                    db.execute("INSERT INTO treatment_prices(treatment,hospital,unit_price,source,checked,expires) VALUES ('Course','Hospital',1000,'https://example.org/tariff',CURRENT_DATE,CURRENT_DATE+7)")
                    preserved = {t:db.execute(f'SELECT count(*) AS n FROM {t}').fetchone()['n'] for t in ('staff','schemes','treatment_prices')}
                worker_auth = client.post('/login',json={'email':'reset-worker@oncoaid.local','password':'worker check phrase'}).json()
                worker_headers = {'Authorization':'Bearer '+worker_auth['token']}
                path='/patients/1/permanent-delete'
                assert client.post(path,json={'revision':1,'confirmation':'DELETE'}).status_code == 401
                assert client.post(path,headers=worker_headers,json={'revision':1,'confirmation':'DELETE'}).status_code == 403
                assert client.post(path,headers=headers,json={'revision':1,'confirmation':'delete'}).status_code == 422
                assert client.post(path,headers=headers,json={'revision':99,'confirmation':'DELETE'}).status_code == 409
                assert client.post(path,headers=headers,json={'revision':1,'confirmation':'DELETE'}).status_code == 200
                with api.connect() as db:
                    for table in ('patients','applications','documents','reminders'):
                        assert db.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 1
                    assert not db.execute('SELECT 1 FROM audit WHERE patient_id=1').fetchone()
                    assert not db.execute('SELECT 1 FROM telegram_contacts WHERE chat_id=456').fetchone()
                    assert not db.execute("SELECT 1 FROM whatsapp_contacts WHERE phone='+910000000001'").fetchone()
                    assert db.execute('SELECT next_offset FROM telegram_bots WHERE id=123').fetchone()['next_offset'] == 9000
                # The next Telegram update creates a clean contact, with no old consent or journey.
                def incoming(update_id,payload):
                    update={'update_id':update_id,'message':{'chat':{'id':456,'type':'private'},'from':{'id':456,'is_bot':False},**payload}}
                    with api.connect() as db, patch.object(tg,'telegram',return_value=[update]):
                        tg.receive_updates(db,{'id':123})
                    with api.connect() as db, patch.object(tg,'telegram',return_value={'message_id':update_id}),patch.object(tg.time,'sleep'):
                        tg.process_inbox(db,123)
                    with api.connect() as db:
                        return db.execute('SELECT reply FROM telegram_messages WHERE key=%s',(f'123:{update_id}',)).fetchone()['reply']
                assert 'Choose your language' in incoming(9001,{'text':'/start'})
                assert '/yes' in incoming(9002,{'text':'English'})
                assert 'Share my phone' in incoming(9003,{'text':'YES'})
                assert 'full name' in incoming(9004,{'contact':{'user_id':456,'phone_number':'910000000001'}})
                with api.connect() as db:
                    new = db.execute("SELECT * FROM patients WHERE phone='+910000000001'").fetchone()
                    assert new['id'] > 2 and new['archived_at'] is None and new['status']=='pending'
                    assert not db.execute('SELECT 1 FROM applications WHERE patient_id=%s',(new['id'],)).fetchone()
                    c = db.execute('SELECT * FROM telegram_contacts WHERE chat_id=456').fetchone()
                    assert c['suggested_fields']=={} and c['journey']['phase']=='intake'
                reset='/admin/patient-data/reset'
                assert client.post(reset,headers=worker_headers,json={'confirmation':'RESET'}).status_code == 403
                assert client.post(reset,headers=headers,json={'confirmation':''}).status_code == 422
                # An in-flight send must finish before deletion commits.
                held, release = Event(), Event()
                def hold_delivery():
                    with api.connect() as db, delivery_guard(db):
                        held.set()
                        assert release.wait(20)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    delivery=pool.submit(hold_delivery)
                    assert held.wait(10)
                    wipe=pool.submit(client.post,reset,headers=headers,json={'confirmation':'RESET'})
                    assert not Event().wait(.2) and not wipe.done()
                    release.set()
                    assert wipe.result(timeout=40).status_code == 200
                    delivery.result(timeout=10)
                assert all(n==0 for n in client.get('/admin/patient-data',headers=headers).json().values())
                assert client.get('/patients',headers=worker_headers).status_code == 200, 'Worker login must survive reset'
                with api.connect() as db:
                    for table,count in preserved.items():
                        assert db.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == count
                    assert db.execute('SELECT next_offset FROM telegram_bots WHERE id=123').fetchone()['next_offset'] == 9005
                    assert not db.execute('SELECT 1 FROM audit WHERE patient_id IS NOT NULL').fetchone()
                    log=db.execute("SELECT detail FROM audit WHERE action='patient_data_reset'").fetchone()['detail']
                    assert set(log)=={'deleted'}, 'Maintenance record must contain counts only'
                assert 'Choose your language' in incoming(9005,{'text':'hi'})
                assert client.post(reset,headers=headers,json={'confirmation':'RESET'}).status_code == 200
                print('PASS: admin-only deletion, typed confirmation, stale-case protection, child-data erasure, unrelated-case preservation, same-account re-registration, complete patient reset including unlinked chats, preserved staff/schemes/tariffs/bot cursor, maintenance audit and in-flight delivery coordination')
    finally:
        root.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
