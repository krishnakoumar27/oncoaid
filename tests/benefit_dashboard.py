"""Render and submit patient-specific benefit inputs in an isolated schema."""
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
import api

schema='benefit_ui_'+uuid4().hex
os.environ.update(ADMIN_EMAIL='benefit-ui@oncoaid.local',ADMIN_PASSWORD='benefit UI check phrase')
with psycopg.connect(autocommit=True) as root:
    root.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with patch.object(api,'connect',lambda:psycopg.connect(options=f'-c search_path={schema}',row_factory=dict_row)):
            with TestClient(api.app) as client:
                auth=client.post('/login',json={'email':'benefit-ui@oncoaid.local','password':'benefit UI check phrase'}).json()
                headers={'Authorization':'Bearer '+auth['token']}
                profile={'name':'UI Check','phone':'+910000000005','state':'Tamil Nadu','district':'Chennai','birth_date':'1980-01-01',
                         'hospital':'Recorded hospital','income':90000,'diagnosis':'Recorded diagnosis','stage':'II',
                         'planned_treatment':'Prescribed course','insurance':'none','consent':True}
                assert client.post('/patients',headers=headers,json=profile).status_code==201
                rev=client.get('/patients/1',headers=headers).json()['revision']
                assert client.post('/patients/1/registration',headers=headers,json={'revision':rev,'status':'verified'}).status_code==200
                rev=client.get('/patients/1',headers=headers).json()['revision']
                assert client.post('/patients/1/cost-plan',headers=headers,json={'revision':rev,'phases':[{'name':'Treatment','amount':'800000'}],'coverage':[]}).status_code==200
                def request(method,url,**kwargs):
                    parsed=urlsplit(url)
                    response=client.request(method,parsed.path+('?' + parsed.query if parsed.query else ''),headers=kwargs.get('headers'),json=kwargs.get('json'))
                    response.ok=response.is_success
                    return response
                with patch('requests.request',side_effect=request):
                    view=AppTest.from_file('/app/dashboard.py',default_timeout=25)
                    for key,value in auth.items():
                        view.session_state[key]=value
                    view.run()
                    assert not view.exception,[e.message for e in view.exception]
                    next(r for r in view.radio if r.label=='Case workspace').set_value('Funding options').run()
                    assert not view.exception,[e.message for e in view.exception]
                    block=next(e for e in view.expander if e.label=='PM-JAY')
                    for field,value in [('Expenses eligible under this scheme (₹)','700000'),
                                        ('Verified remaining benefit balance, if known (₹)','100000'),
                                        ('Verified package or case-specific grant amount, if known (₹)','70000'),
                                        ('Package / balance / funder reference','Verified package reference')]:
                        next(t for t in block.text_input if t.label==field).set_value(value)
                    block.checkbox[0].set_value(True)
                    next(b for b in block.button if b.label=='Save verified benefit inputs').click().run()
                    assert not view.exception,[e.message for e in view.exception]
                    overview=client.get('/patients/1/overview',headers=headers).json()
                    pm=next(r for r in overview['schemes'] if r['name']=='PM-JAY')['illustration']
                    assert pm['case_inputs_verified'] and float(pm['possible_cover'])==70000 and float(pm['patient_share'])==730000
                    next(r for r in view.radio if r.label=='Case workspace').set_value('Cost roadmap').run()
                    assert not view.exception,[e.message for e in view.exception]
                    choices=client.get('/schemes',headers=headers).json()
                    pm_id=next(r['id'] for r in choices if r['name']=='PM-JAY')
                    cm_id=next(r['id'] for r in choices if r['name']=='CMCHIS')
                    next(r for r in view.selectbox if r.label=='See remaining cost with this scheme').set_value(pm_id).run()
                    assert next(m for m in view.metric if m.label=='Scheme may pay').value=='₹70,000'
                    assert next(m for m in view.metric if m.label=='You may need after this scheme').value=='₹7,30,000'
                    next(r for r in view.selectbox if r.label=='See remaining cost with this scheme').set_value(cm_id).run()
                    assert next(m for m in view.metric if m.label=='Scheme may pay').value=='₹5,00,000'
                    assert next(m for m in view.metric if m.label=='You may need after this scheme').value=='₹3,00,000'
                    next(r for r in view.radio if r.label=='Workspace').set_value('Schemes').run()
                    assert not view.exception,[e.message for e in view.exception]
                    sid=next(s['id'] for s in client.get('/schemes',headers=headers).json() if s['name']=='PM-JAY')
                    next(s for s in view.selectbox if s.label=='Manage scheme').set_value(sid).run()
                    assert not view.exception,[e.message for e in view.exception]
                    assert next(s for s in view.selectbox if s.label=='Benefit calculation').value=='package_limit'
                    next(b for b in view.button if b.label=='Save scheme').click().run()
                    assert not view.exception,[e.message for e in view.exception]
                    assert not view.error,[e.value for e in view.error]
                    print('PASS: benefit comparison table, saved worker inputs, updated patient share and cap-only scheme editor forms')
    finally:
        root.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
