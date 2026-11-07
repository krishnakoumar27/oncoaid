import os
import sys
sys.path.insert(0, "/app")
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import patch
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
import api

schema='ui_check_'+uuid4().hex
os.environ.update(ADMIN_EMAIL='ui@oncoaid.local',ADMIN_PASSWORD='ui check passphrase')
with psycopg.connect(autocommit=True) as root:
    root.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with patch.object(api,'connect',lambda:psycopg.connect(options=f'-c search_path={schema}',row_factory=dict_row)):
            with TestClient(api.app) as client:
                auth=client.post('/login',json={'email':'ui@oncoaid.local','password':'ui check passphrase'}).json()
                headers={'Authorization':'Bearer '+auth['token']}
                profile={'name':'UI Check','phone':'+910000000003','consent':True,'state':'Tamil Nadu','hospital':'Hospital','income':90000,'diagnosis':'Recorded cancer','planned_treatment':'Prescribed regimen'}
                assert client.post('/patients',headers=headers,json=profile).status_code==201
                with api.connect() as db:
                    from psycopg.types.json import Jsonb
                    db.execute("UPDATE patients SET profile=%s WHERE id=1",(Jsonb({"name":"Registration pending","consent":True}),))
                    source=db.execute("SELECT * FROM schemes WHERE details->>'published'='true' LIMIT 1").fetchone()
                    snapshot={**source["details"],"name":source["name"],"revision":source["revision"]}
                    db.execute("INSERT INTO applications(patient_id,scheme_id,snapshot,status,draft) VALUES (1,%s,%s,'under_review','Review-required preparation draft')",(source["id"],Jsonb(snapshot)))
                def request(method,url,**kwargs):
                    response = client.request(method,urlsplit(url).path+('?' + urlsplit(url).query if urlsplit(url).query else ''),headers=kwargs.get('headers'),json=kwargs.get('json'))
                    response.ok=response.is_success
                    return response
                with patch('requests.request',side_effect=request):
                    view=AppTest.from_file('/app/dashboard.py',default_timeout=20)
                    for key,value in auth.items():
                        view.session_state[key]=value
                    view.run()
                    assert not view.exception, [e.message for e in view.exception]
                    buttons=[b.label for b in view.button]
                    assert 'Reject patient registration' in buttons and 'Remove patient (archive)' in buttons
                    for section in ['Registration','Documents','Applications','Cost roadmap','Funding options','Patient messages','Reminders','Timeline']:
                        case_radio=next(r for r in view.radio if r.label=='Case workspace')
                        case_radio.set_value(section).run()
                        assert not view.exception, (section,[e.message for e in view.exception])
                    case=client.get('/patients/1',headers=headers).json()
                    assert client.post('/patients/1/archive',headers=headers,json={'revision':case['revision'],'reason':'UI archive check'}).status_code==200
                    view.toggle[0].set_value(True).run()
                    assert not view.exception, [e.message for e in view.exception]
                    assert 'Restore patient' in [b.label for b in view.button]
                    field=next(x for x in view.text_input if x.label=='Type DELETE to confirm')
                    field.set_value('DELETE')
                    next(b for b in view.button if b.label=='Permanently delete this patient').click().run()
                    assert not view.exception, [e.message for e in view.exception]
                    assert not client.get('/patients?archived=true',headers=headers).json()
                    assert view.session_state['token']==auth['token']
                    assert client.post('/patients',headers=headers,json=profile).status_code==201
                    next(r for r in view.radio if r.label=='Workspace').set_value('Data management').run()
                    assert not view.exception
                    next(b for b in view.button if b.label=='Permanently clear all patient data').click().run()
                    assert view.error and len(client.get('/patients',headers=headers).json())==1
                    next(x for x in view.text_input if x.label=='Type RESET to clear all patient data').set_value('RESET')
                    next(b for b in view.button if b.label=='Permanently clear all patient data').click().run()
                    assert not view.exception, [e.message for e in view.exception]
                    assert all(n==0 for n in client.get('/admin/patient-data',headers=headers).json().values())
                    assert view.session_state['token']==auth['token']
                    print('PASS: dashboard sections, archived patient permanent deletion, required reset confirmation, all-patient reset form and preserved admin session')
    finally:
        root.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
