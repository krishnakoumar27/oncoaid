"""Run inside the API container; all records live in a disposable DB schema.

    docker compose exec -T api python tests/journey_integration.py

Provider calls are mocked. No patient Telegram messages or real OCR jobs are sent.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient

import api
import telegram_channel as tg
import patient_journey as journey

schema = "journey_check_" + uuid4().hex
os.environ.update(ADMIN_EMAIL="journey@oncoaid.local", ADMIN_PASSWORD="journey check phrase")
with psycopg.connect(autocommit=True) as root:
    root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with patch.object(api, "connect", lambda: psycopg.connect(options=f"-c search_path={schema}", row_factory=dict_row)):
            with TestClient(api.app) as client:
                login = client.post("/login", json={"email":"journey@oncoaid.local", "password":"journey check phrase"}).json()
                client.headers.update({"Authorization":"Bearer " + login["token"]})
                with api.connect() as db:
                    db.execute("INSERT INTO telegram_bots(id,username) VALUES (123,'journey_bot')")
                    db.execute("INSERT INTO telegram_contacts(bot_id,chat_id) VALUES (123,456)")
                sequence = 0
                def incoming(payload):
                    global sequence
                    sequence += 1
                    key = f"test:{sequence:04}"
                    with api.connect() as db:
                        db.execute("INSERT INTO telegram_messages(key,bot_id,chat_id,payload) VALUES (%s,123,456,%s)", (key,Jsonb(payload)))
                    with api.connect() as db, patch.object(tg,"telegram",return_value={"message_id":sequence}), patch.object(tg.time,"sleep"):
                        tg.process_inbox(db,123)
                    with api.connect() as db:
                        rows = db.execute("SELECT reply FROM telegram_messages WHERE key=%s OR key LIKE %s ORDER BY key", (key,key+":part:%")).fetchall()
                        return "".join(r["reply"] for r in rows)
                def text(value):
                    return incoming({"text":value})
                def patient():
                    return client.get("/patients/1").json()
                def post(path, body, expected=200):
                    body = {"revision":patient()["revision"], **body}
                    result = client.post("/patients/1"+path,json=body)
                    assert result.status_code == expected, (path, result.status_code, result.text)
                    return result
                assert "Choose your language" in text("hi")
                assert "/yes" in text("English")
                assert "Share my phone" in text("YES")
                incoming({"contact":{"user_id":999,"phone_number":"910000000001"}})
                with api.connect() as db:
                    assert db.execute("SELECT count(*) AS n FROM patients").fetchone()["n"] == 0
                assert "full name" in incoming({"contact":{"user_id":456,"phone_number":"910000000001"}})
                # Short numeric annual income is usable even when the NLP provider is unavailable.
                with patch.object(tg,"understand",side_effect=api.SarvamError("offline")):
                    assert "state" in text("Patient Check")
                details = {"state":"Tamil Nadu", "hospital":"Care Hospital", "income":90000,
                           "diagnosis":"Breast cancer", "stage":"II", "planned_treatment":"AC chemotherapy, 4 cycles", "treatment_units":"4", "insurance":"none"}
                with api.connect() as db:
                    spec = {"kind":"organization","state":"","published":True,"archived":False,
                            "opens":None,"closes":None,"source":"https://example.org/support",
                            "guidance":"Income and approved hospital expenses must be verified.",
                            "requirements":["Identity proof","Hospital estimate"],"coverage_rate":50,"coverage_cap":400000}
                    sid = db.execute("INSERT INTO schemes(name,details) VALUES ('Case-specific support',%s) RETURNING id",(Jsonb(spec),)).fetchone()["id"]
                with patch.object(tg,"understand",return_value=("details",details)), patch.object(journey,"treatment_planning_range",return_value={"low_inr":700000,"high_inr":900000,"scope":"Four prescribed AC chemotherapy cycles","assumptions":["Hospital prices vary"]}) as forecast:
                    reply = text("Here are my details")
                    assert len(reply)<1200 and 'Confirmed support' not in reply
                    second_page=text('/next')
                    assert "700,000" in reply and "400,000" in second_page and "500,000" in reply
                    text("/schemes")
                    assert forecast.call_count == 1, "Identical treatment context should use the cached estimate"
                with api.connect() as db:
                    contact = db.execute("SELECT * FROM telegram_contacts").fetchone()
                    options = contact["journey"]["options"]
                    number = next(i for i,row in enumerate(options,1) if row["id"] == sid)
                    # New choices must reject a stale scheme version.
                    db.execute("UPDATE schemes SET revision=revision+1 WHERE id=%s",(sid,))
                assert "option has changed" in text(str(number))
                with api.connect() as db:
                    options = db.execute("SELECT journey FROM telegram_contacts").fetchone()["journey"]["options"]
                    number = next(i for i,row in enumerate(options,1) if row["id"] == sid)
                assert "Identity proof" in text(str(number))
                case = patient()
                application = case["applications"][0]
                aid = application["id"]
                assert application["status"] == "awaiting_documents" and case["status"] == "pending"
                assert "STAFF REVIEW REQUIRED" in application["draft"]
                with patch.object(tg,"download_document",return_value=("record.pdf",b"%PDF-1.4")) as download:
                    assert "choose the document number first" in incoming({"document":{"file_id":"f"}})
                    assert download.call_count == 0
                    assert "upload Identity proof" in text("1")
                    assert "received" in incoming({"document":{"file_id":"f"}})
                    text("2")
                    assert "team is reviewing" in incoming({"document":{"file_id":"f"}})
                case = patient()
                assert case["applications"][0]["status"] == "under_review"
                assert len(case["applications"][0]["documents"]) == 2
                assert any(r["audience"] == "worker" for r in case["reminders"])
                post("/claim", {})
                doc = patient()["applications"][0]["documents"][-1]
                post(f"/applications/{aid}/documents/{doc['id']}", {"status":"rejected","reason":"Please send a clearer image"})
                assert "clearer image" in text("/documents")
                with patch.object(tg,"download_document",return_value=("replacement.pdf",b"%PDF-1.4")):
                    text("1")
                    incoming({"document":{"file_id":"f2"}})
                assert len(patient()["applications"][0]["documents"]) == 3, "Previous versions must remain"
                post(f"/applications/{aid}/status", {"status":"ready_for_submission"}, 409)
                profile = {"name":"Patient Check","phone":"+910000000001",**details,
                           "birth_date":"1980-01-01","district":"Chennai","consent":True}
                post("/profile", {"profile":profile})
                post("/registration", {"status":"verified"})
                with api.connect() as db:
                    latest = api.latest_documents(db,aid)
                for doc in latest:
                    post(f"/applications/{aid}/documents/{doc['id']}", {"status":"verified"})
                post(f"/applications/{aid}/status", {"status":"ready_for_submission"})
                # Explicit rejection needs a reason and remains visible to the patient.
                post("/registration", {"status":"rejected","reason":""}, 422)
                post("/registration", {"status":"rejected","reason":"Please confirm the treating hospital"})
                assert "confirm the treating hospital" in text("/status")
                post("/archive", {"reason":"Duplicate registration review"})
                assert not client.get("/patients").json()
                assert len(client.get("/patients?archived=true").json()) == 1
                assert "inactive" in text("hi")
                post("/profile", {"profile":profile}, 409)
                assert len(patient()["applications"][0]["documents"]) == 3
                post("/archive", {"archived":False,"reason":"Case reviewed"})
                assert len(client.get("/patients").json()) == 1
                logs = client.get("/audit?patient_id=1").json()
                assert any(r["action"] == "patient_archived" for r in logs)
                assert any(r["action"] == "patient_restored" for r in logs)
                # OCR fills the intake and advances directly to options, using the quoted cost.
                with api.connect() as db:
                    pid2 = db.execute("INSERT INTO patients(phone,profile) VALUES ('+910000000002',%s) RETURNING id", (Jsonb({"name":"Registration pending","consent":True}),)).fetchone()["id"]
                    db.execute("INSERT INTO telegram_contacts(bot_id,chat_id,phone,patient_id,consent) VALUES (123,789,'+910000000002',%s,true)", (pid2,))
                    extracted = {"status":"completed","fields":{"name":"OCR Patient","state":"Tamil Nadu","hospital":"Care Hospital",
                                 "annual_income_inr":"90000","diagnosis":"Breast cancer","stage":"II","planned_treatment":"AC chemotherapy",
                                 "treatment_units":"4","insurance":"none","estimate_total_inr":"200000","estimate_scope":"Four prescribed cycles"}}
                    doc2 = db.execute("INSERT INTO documents(patient_id,requirement,filename,content,ai_authorized,ai_result,telegram_key) VALUES (%s,'Hospital summary','record.pdf',%s,true,%s,'ocrcheck') RETURNING *",
                                      (pid2,b"%PDF-1.4",Jsonb(extracted))).fetchone()
                    with patch.object(journey,"treatment_planning_range",side_effect=AssertionError("A hospital quote must take priority over AI")):
                        tg.document_completed(db,doc2,extracted)
                    contact2 = db.execute("SELECT journey FROM telegram_contacts WHERE chat_id=789").fetchone()["journey"]
                    assert contact2["phase"] == "schemes" and contact2["options"]
                    output = "".join(r["reply"] for r in db.execute("SELECT reply FROM telegram_messages WHERE chat_id=789 ORDER BY key").fetchall())
                    assert "200,000" in output and "CMCHIS" in output
                    assert "What is the patient's full name" not in output
                    # A time-expired offer cannot create a new application even from an old list.
                    contact_row = db.execute("SELECT * FROM telegram_contacts WHERE chat_id=789").fetchone()
                    index = next(i for i,o in enumerate(contact2["options"],1) if o["id"] == sid)
                    scheme = db.execute("SELECT * FROM schemes WHERE id=%s",(sid,)).fetchone()
                    changed = {**scheme["details"], "closes":"2020-01-01"}
                    db.execute("UPDATE schemes SET details=%s WHERE id=%s",(Jsonb(changed),sid))
                    reply2 = journey.select_scheme(db,contact_row,db.execute("SELECT * FROM patients WHERE id=%s",(pid2,)).fetchone(),index)
                    assert "option has changed" in reply2
                    assert db.execute("SELECT count(*) AS n FROM applications WHERE patient_id=%s",(pid2,)).fetchone()["n"] == 0
                # Unicode-safe multipart output must preserve every character.
                original = "🌷" * 5000 + "last option"
                parts = tg.split_reply(original)
                assert "".join(parts) == original
                assert all(len(part.encode("utf-16-le")) // 2 <= 3800 for part in parts)
                print("PASS: consent, own phone, intake fallback, conditional budget and cached range, stale choices, scheme selection, numbered uploads, worker handoff, document correction/version retention, verification gates, rejection, archive/restore, audit, multipart delivery, OCR-driven progression and expired-offer blocking")
    finally:
        root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
