"""Verify one-click account access, role rules and the actual account chooser UI."""
import os
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
import api

schema='click_access_'+uuid4().hex
settings={'ONE_CLICK_ACCESS':'true','ADMIN_EMAIL':'click-admin@oncoaid.local','WORKER_EMAIL':'click-worker@oncoaid.local','ADMIN_PASSWORD':''}
with psycopg.connect(autocommit=True) as root:
    root.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with patch.dict(os.environ,settings),patch.object(api,'connect',lambda:psycopg.connect(options=f'-c search_path={schema}',row_factory=dict_row)):
            with TestClient(api.app) as client:
                options=client.get('/access-options').json()
                assert options['one_click'] and len(options['accounts'])==2 and all(a['available'] for a in options['accounts'])
                admin=client.post('/access/select',json={'role':'admin'}).json()
                worker=client.post('/access/select',json={'role':'worker','email':admin['email'],'id':admin['id']}).json()
                assert worker['id']!=admin['id'] and worker['role']=='worker'
                ah={'Authorization':'Bearer '+admin['token']}; wh={'Authorization':'Bearer '+worker['token']}
                assert client.get('/staff',headers=wh).status_code==403
                assert client.post('/admin/patient-data/reset',headers=wh,json={'confirmation':'RESET'}).status_code==403
                assert client.get('/staff',headers=ah).status_code==200
                assert client.post('/access/select',json={'role':'disabled'}).status_code==422
                with patch.dict(os.environ,{'ONE_CLICK_ACCESS':'false'}):
                    assert not client.get('/access-options').json()['one_click']
                    assert client.post('/access/select',json={'role':'admin'}).status_code==403
                    assert client.get('/patients',headers=ah).status_code==401, 'Shared sessions must stop working when one-click mode is disabled'
                def request(method,url,**kwargs):
                    parsed=urlsplit(url)
                    response=client.request(method,parsed.path+('?' + parsed.query if parsed.query else ''),headers=kwargs.get('headers'),json=kwargs.get('json'))
                    response.ok=response.is_success
                    return response
                with patch('requests.request',side_effect=request):
                    view=AppTest.from_file('/app/dashboard.py',default_timeout=25)
                    view.run()
                    assert not view.exception,[e.message for e in view.exception]
                    assert not view.text_input, 'Account chooser must have no email/password inputs'
                    assert [b.label for b in view.button]==['Admin','Worker']
                    next(b for b in view.button if b.label=='Worker').click().run()
                    assert not view.exception and view.session_state['role']=='worker'
                    assert 'Data management' not in next(r for r in view.radio if r.label=='Workspace').options
                    next(b for b in view.button if b.label=='Switch account').click().run()
                    assert not view.exception and not view.text_input
                    next(b for b in view.button if b.label=='Admin').click().run()
                    assert not view.exception and view.session_state['role']=='admin'
                    assert 'Data management' in next(r for r in view.radio if r.label=='Workspace').options
                assert client.post('/staff/'+str(worker['id']),headers=ah,json={'role':'disabled'}).status_code==200
                assert client.post('/access/select',json={'role':'worker'}).status_code==403
                assert client.get('/patients',headers=wh).status_code==401
            with TestClient(api.app) as client:
                assert not next(a for a in client.get('/access-options').json()['accounts'] if a['role']=='worker')['available'], 'Startup must not re-enable a disabled worker'
            print('PASS: password-free account chooser, Admin/Worker switching, role boundaries, input spoofing protection, disabled workers and shared-session revocation when mode is disabled')
    finally:
        root.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
