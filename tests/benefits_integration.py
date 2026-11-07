"""Compare published benefits and persist verified case inputs in an isolated schema."""
import os
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient
import api
import telegram_channel as tg
from funding_sources import seed_sources

schema='benefit_check_'+uuid4().hex
os.environ.update(ADMIN_EMAIL='benefits@oncoaid.local',ADMIN_PASSWORD='benefits check phrase')
with psycopg.connect(autocommit=True) as root:
    root.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with patch.object(api,'connect',lambda:psycopg.connect(options=f'-c search_path={schema}',row_factory=dict_row)):
            with TestClient(api.app) as client:
                auth=client.post('/login',json={'email':'benefits@oncoaid.local','password':'benefits check phrase'}).json()
                client.headers.update({'Authorization':'Bearer '+auth['token']})
                profile={'name':'Benefit Check','phone':'+910000000004','state':'Tamil Nadu','district':'Chennai',
                         'birth_date':'1980-01-01','hospital':'Recorded hospital','income':90000,'diagnosis':'Recorded diagnosis',
                         'stage':'II','planned_treatment':'Prescribed course','insurance':'none','consent':True}
                assert client.post('/patients',json=profile).status_code==201
                def case(): return client.get('/patients/1').json()
                def post(path,data):
                    result=client.post('/patients/1'+path,json={'revision':case()['revision'],**data})
                    assert result.status_code==200,(path,result.text)
                    return result.json()
                post('/registration',{'status':'verified'})
                post('/cost-plan',{'phases':[{'name':'Prescribed course','amount':'800000'}],'coverage':[]})
                overview=client.get('/patients/1/overview').json()
                rows={r['name']:r for r in overview['schemes']}
                for name in ('PM-JAY','CMCHIS'):
                    result=rows[name]['illustration']
                    assert float(result['possible_cover'])==500000 and float(result['patient_share'])==300000
                    assert float(result['percent'])==62.5 and result['upper_bound']
                hm=next(r for r in overview['schemes'] if r['name'].startswith("Health Minister's"))
                assert hm['illustration']['upper_bound'] and hm['illustration']['coverage_kind']=='grant_limit'
                assert float(hm['illustration']['possible_cover'])==800000, 'Never project more than treatment cost'
                with api.connect() as db:
                    db.execute("INSERT INTO telegram_bots(id,username) VALUES (123,'benefit_bot')")
                    db.execute("INSERT INTO telegram_contacts(bot_id,chat_id,phone,patient_id,consent) VALUES (123,456,'+910000000004',1,true)")
                sequence=0
                def reply(text):
                    global sequence
                    sequence+=1
                    with api.connect() as db:
                        contact=db.execute('SELECT * FROM telegram_contacts WHERE bot_id=123 AND chat_id=456 FOR UPDATE').fetchone()
                        message={'payload':{'text':text},'key':f'benefit:{sequence}'}
                        return tg.respond(db,contact,message)[0]
                message=reply('/schemes')
                assert '500,000' in message and '300,000' in message and '62.50%' in message
                with api.connect() as db:
                    options=db.execute('SELECT journey FROM telegram_contacts').fetchone()['journey']['options']
                    number=next(i for i,r in enumerate(options,1) if r['id']==rows['PM-JAY']['id'])
                reply(str(number))
                message=reply('/cost')
                assert 'PM-JAY' in message and '500,000' in message and '300,000' in message
                scheme=next(s for s in client.get('/schemes').json() if s['name']=='PM-JAY')
                data={'scheme_id':scheme['id'],'scheme_revision':scheme['revision'],'case_revision':case()['revision'],
                      'covered_expenses':'600000','remaining_balance':'200000','case_amount':'150000',
                      'evidence':'Hospital package and remaining-benefit reference','verified_requirements':True,'save_assessment':True}
                response=client.post('/patients/1/funding-scenario',json=data)
                assert response.status_code==200,response.text
                assert float(response.json()['patient_share'])==650000
                message=reply('/cost')
                assert '150,000' in message and '650,000' in message
                new_overview=client.get('/patients/1/overview').json()
                pm=next(r for r in new_overview['schemes'] if r['name']=='PM-JAY')['illustration']
                assert pm['case_inputs_verified'] and float(pm['possible_cover'])==150000
                # A treatment change invalidates the saved case-specific assessment.
                profile['planned_treatment']='Changed prescribed course'
                post('/profile',{'profile':profile})
                pm=next(r for r in client.get('/patients/1/overview').json()['schemes'] if r['name']=='PM-JAY')['illustration']
                assert not pm['case_inputs_verified'] and float(pm['possible_cover'])==500000
                # Unknown case-by-case grants are not silently shown as no assistance.
                unknown={'name':'Unassessed trust','kind':'organization','published':True,'source':'https://example.org/trust',
                         'guidance':'Individual review required','requirements':['Application'],'coverage_kind':'case_by_case'}
                assert client.post('/schemes',json=unknown).status_code==200
                reply('/schemes')
                assert 'Scheme support: needs worker review' in reply('/next')
                # Cap-only rules can be saved without inventing a 100% reimbursement rate.
                scheme_body={'name':scheme['name'],'revision':scheme['revision'],**scheme['details']}
                result=client.post('/schemes?scheme_id='+str(scheme['id']),json=scheme_body)
                assert result.status_code==200,result.text
                with api.connect() as db:
                    before=db.execute('SELECT revision FROM schemes WHERE id=%s',(scheme['id'],)).fetchone()['revision']
                    seed_sources(db,api.audit)
                    after=db.execute('SELECT revision FROM schemes WHERE id=%s',(scheme['id'],)).fetchone()['revision']
                    assert before==after,'Seed migration must be idempotent and preserve staff edits'
                # Financial ranking must consider every screened source, not just the first five lexical matches.
                for index in range(6):
                    low={'name':f'Recorded diagnosis course {index}','kind':'organization','published':True,
                         'source':'https://example.org/low','guidance':'Tamil Nadu Recorded hospital Recorded diagnosis Changed prescribed course',
                         'requirements':['Application'],'coverage_rate':1}
                    assert client.post('/schemes',json=low).status_code==200
                high={'name':'Higher benefit foundation','kind':'organization','published':True,
                      'source':'https://example.org/high','guidance':'Case review required',
                      'requirements':['Application'],'coverage_kind':'grant_limit','coverage_cap':2000000}
                assert client.post('/schemes',json=high).status_code==200
                assert 'Higher benefit foundation' in reply('/schemes')
                print('PASS: seeded caps, cost-minus-support math, percentage comparisons, selected-application cost replies, verified patient inputs, context invalidation, unknown-vs-zero, cap-only editing and idempotent migration')
    finally:
        root.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
