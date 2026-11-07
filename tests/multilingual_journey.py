"""Language selection, short receipts and navigation using synthetic records."""
import json
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

schema='language_check_'+uuid4().hex
os.environ.update(ADMIN_EMAIL='language@oncoaid.local',ADMIN_PASSWORD='language check phrase')
def translate(method,route,**kwargs):
    native='தமிழ்' if kwargs['json']['target_language_code']=='ta-IN' else 'हिन्दी'
    return {'translated_text':native+' '+kwargs['json']['input']}


with psycopg.connect(autocommit=True) as root:
    root.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with patch.object(api,'connect',lambda:psycopg.connect(options=f'-c search_path={schema}',row_factory=dict_row)),patch('languages.sarvam',side_effect=translate):
            with TestClient(api.app):
                with api.connect() as db:
                    db.execute("INSERT INTO telegram_bots(id,username) VALUES (123,'language_bot')")
                    db.execute('INSERT INTO telegram_contacts(bot_id,chat_id) VALUES (123,456)')
                sequence=0
                def message(payload):
                    global sequence
                    sequence+=1
                    key=f'language:{sequence}'
                    with api.connect() as db:
                        db.execute('INSERT INTO telegram_messages(key,bot_id,chat_id,payload) VALUES (%s,123,456,%s)',(key,Jsonb(payload)))
                    with api.connect() as db,patch.object(tg,'telegram',return_value={'message_id':sequence}),patch.object(tg.time,'sleep'):
                        tg.process_inbox(db,123)
                    with api.connect() as db:
                        result=db.execute('SELECT reply FROM telegram_messages WHERE key=%s',(key,)).fetchone()['reply']
                        assert not result.startswith('English ('),result
                        return result
                def text(value): return message({'text':value})
                def contact():
                    with api.connect() as db:
                        return db.execute('SELECT * FROM telegram_contacts WHERE bot_id=123 AND chat_id=456').fetchone()
                assert 'Choose your language' in text('hi')
                assert '/yes' in text('தமிழ்') and contact()['language_code']=='ta-IN'
                text('/yes')
                assert 'தமிழ்' in message({'contact':{'user_id':456,'phone_number':'910000000007'}})
                assert contact()['journey']['prompt_field']=='name'
                text('menu')
                assert contact()['journey']['view']=='menu' and contact()['journey']['prompt_field']=='name'
                assert 'full name' in text('தொடரவும்')
                text('/language')
                assert 'full name' in text('३') and contact()['language_code']=='hi-IN'
                text('/language')
                text('/stop')
                assert contact()['consent'] is False, 'STOP must work during language selection'
                text('/yes')
                with api.connect() as db:
                    pid=contact()['patient_id']
                    extract={'status':'completed','fields':{'estimate_total_inr':'800000','estimate_scope':'Recorded course'}}
                    db.execute("INSERT INTO documents(patient_id,requirement,filename,content,ai_result) VALUES (%s,'Hospital quote','quote.pdf',%s,%s)",(pid,b'%PDF-test',Jsonb(extract)))
                fields={'name':'Patient Check','state':'Tamil Nadu','hospital':'Recorded hospital','income':90000,
                        'diagnosis':'Recorded diagnosis','stage':'II','planned_treatment':'Prescribed course'}
                with patch.object(tg,'understand',return_value=('details',fields)):
                    reply=text('Here are my details')
                assert contact()['journey']['prompt_field']=='insurance'
                with patch.object(tg,'understand',side_effect=api.SarvamError('offline')):
                    reply=text('இல்லை')
                assert contact()['consent'] and contact()['suggested_fields']['insurance']=='none'
                assert len(reply)<1300 and 'Confirmed support' not in reply
                assert '500,000' in reply and '300,000' in reply
                with api.connect() as db:
                    pm=db.execute("SELECT id FROM schemes WHERE name='PM-JAY'").fetchone()['id']
                    options=contact()['journey']['options']
                    number=next(i for i,row in enumerate(options,1) if row['id']==pm)
                text(str(number))
                app_id=contact()['journey']['application_id']
                reply=text('/cost')
                assert '500,000' in reply and '300,000' in reply and 'Confirmed support' not in reply
                text('1')
                saved=contact()['journey']['requirement']
                text('/schemes')
                assert saved in text('/continue')
                assert contact()['journey']['phase']=='documents'
                text('menu')
                assert saved==contact()['journey']['requirement']
                assert saved in text('continue where left')
                text('/language')
                assert saved in text('English') and contact()['language_code']=='en-IN'
                with patch.object(tg,'download_document',return_value=('doc.pdf',b'%PDF-test')):
                    receipt=message({'document':{'file_id':'file'}})
                    assert len(receipt)<260 and 'Next:' in receipt and 'https://' not in receipt
                    assert 'funding agency' not in receipt
                    text('2')
                    assert 'Documents for' in text('back')
                    assert contact()['journey']['requirement'] is None
                    text('2')
                    message({'document':{'file_id':'file'}})
                    text('3')
                    receipt=message({'document':{'file_id':'file'}})
                    assert len(receipt)<180 and 'All files received' in receipt
                text('menu')
                assert 'All files received' in text('resume')
                assert contact()['journey']['application_id']==app_id
                # OCR completion in the preferred language is concise and duplicate notices are suppressed.
                with api.connect() as db:
                    pid2=db.execute('INSERT INTO patients(phone,profile) VALUES (%s,%s) RETURNING id',('+910000000008',Jsonb({'name':'Registration pending','consent':True}))).fetchone()['id']
                    db.execute("INSERT INTO telegram_contacts(bot_id,chat_id,patient_id,consent,language_code) VALUES (123,789,%s,true,'ta-IN')",(pid2,))
                    result={'status':'completed','fields':{'name':'OCR Patient'}}
                    doc=db.execute("INSERT INTO documents(patient_id,requirement,filename,content,ai_result,telegram_key) VALUES (%s,'Hospital summary','summary.pdf',%s,%s,'ocr-language') RETURNING *",(pid2,b'%PDF-test',Jsonb(result))).fetchone()
                    tg.document_completed(db,doc,result)
                    tg.document_completed(db,doc,result)
                    replies=db.execute('SELECT reply FROM telegram_messages WHERE chat_id=789').fetchall()
                    assert len(replies)==1 and len(replies[0]['reply'])<300 and 'தமிழ்' in replies[0]['reply']
                print('PASS: language-first consent, Tamil/Hindi selection, native numerals, STOP during language selection, translated replies, menu/back/resume without progress loss, dynamic scheme amounts, short upload receipts, language change during upload and deduplicated translated OCR prompts')
    finally:
        root.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
