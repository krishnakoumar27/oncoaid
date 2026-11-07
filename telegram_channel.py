"""Telegram patient messaging with persistent polling and auditable document intake."""
import json
import logging
import os
import re
import time

import psycopg
import requests
from psycopg.types.json import Jsonb

from intelligence import SarvamError
from maintenance import transaction_guard, delivery_guard
from languages import (localize, command_for, normalize_text, language_choice, LANGUAGE_PROMPT,
                       LANGUAGE_KEYBOARD, NAV_KEYBOARD, CONSENT_KEYBOARD)
from whatsapp import status_reply, understand

log = logging.getLogger(__name__)
REMOVE_KEYBOARD = {"remove_keyboard": True}
CONTACT_KEYBOARD = {"keyboard": [[{"text": "Share my phone number", "request_contact": True}]],
                    "resize_keyboard": True, "one_time_keyboard": True}
INTRODUCTION = (
    "Welcome to OncoAid. We help with treatment funding.\n"
    "May we save your details and use Sarvam AI to read your documents for worker review?\n"
    "Reply /yes to agree or /no."
)


class TelegramError(Exception):
    """A sanitized error safe for logs and staff screens."""

    def __init__(self,message,code=None,retry_after=None):
        super().__init__(message)
        self.code=code
        self.retry_after=retry_after


def telegram(method, data=None, timeout=12):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramError("Telegram bot token is not configured.")
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}", json=data or {},
            timeout=(5, timeout), allow_redirects=False,
        )
        body = response.json()
        if not response.ok or not body.get("ok"):
            code = body.get("error_code", response.status_code)
            raise TelegramError(f"Telegram {method} failed (code {code}).",code=code,
                                retry_after=body.get("parameters",{}).get("retry_after"))
        return body["result"]
    except (requests.RequestException, ValueError) as exc:
        raise TelegramError(f"Telegram {method} connection failed.") from None


def receive_updates(db, bot):
    """Persist updates and the cursor together before acknowledging them to Telegram."""
    transaction_guard(db)
    row = db.execute("SELECT next_offset FROM telegram_bots WHERE id=%s FOR UPDATE", (bot["id"],)).fetchone()
    updates = telegram("getUpdates", {"offset": row["next_offset"], "timeout": 3,
                                     "allowed_updates": ["message"], "limit": 20}, timeout=8)
    offset = row["next_offset"]
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            continue
        offset = max(offset, update_id + 1)
        message = update.get("message") or {}
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        if (chat.get("type") != "private" or chat.get("id") != sender.get("id")
                or sender.get("is_bot") or not isinstance(chat.get("id"), int)):
            continue
        chat_id = chat["id"]
        db.execute("INSERT INTO telegram_contacts(bot_id,chat_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                   (bot["id"], chat_id))
        payload = {key: message[key] for key in ("text", "caption", "contact", "document", "photo")
                   if key in message}
        db.execute(
            """INSERT INTO telegram_messages(key,bot_id,chat_id,payload) VALUES (%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (f"{bot['id']}:{update_id}", bot["id"], chat_id, Jsonb(payload)),
        )
    db.execute("UPDATE telegram_bots SET next_offset=%s WHERE id=%s", (offset, bot["id"]))


def download_document(payload):
    document = payload.get("document")
    if document:
        if not isinstance(document, dict):
            raise TelegramError("Please send a PDF or clear photo.")
        file_id, size = document.get("file_id"), document.get("file_size", 0)
    else:
        photos = payload.get("photo") or []
        if not photos:
            raise TelegramError("Please send a hospital summary or estimate as a PDF or photo.")
        photo = photos[-1]
        file_id, size = photo.get("file_id"), photo.get("file_size", 0)
    if not file_id or size > 5 * 1024 * 1024:
        raise TelegramError("Please send a PDF or image of 5 MB or less.")
    metadata = telegram("getFile", {"file_id": file_id})
    path = metadata.get("file_path", "")
    if not path or not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path.split("/"):
        raise TelegramError("The document could not be opened.")
    if metadata.get("file_size", 0) > 5 * 1024 * 1024:
        raise TelegramError("Please send a file of 5 MB or less.")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        with requests.get(f"https://api.telegram.org/file/bot{token}/{path}", stream=True,
                          timeout=(5, 15), allow_redirects=False) as response:
            if response.status_code != 200:
                raise TelegramError("Telegram could not provide the document. Please resend it.")
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 5 * 1024 * 1024:
                    raise TelegramError("Please send a file of 5 MB or less.")
    except requests.RequestException:
        raise TelegramError("We could not download that file. Please resend it.") from None
    for suffix, signature in ((".pdf", b"%PDF-"), (".png", b"\x89PNG\r\n\x1a\n"), (".jpg", b"\xff\xd8\xff")):
        if content.startswith(signature):
            return "hospital-document" + suffix, bytes(content)
    raise TelegramError("Please send a PDF, PNG or JPEG hospital document.")


def respond(db, contact, message):
    """Language choice and safe localization wrap the channel's English workflow."""
    from api import audit
    payload=dict(message['payload'])
    text=normalize_text(payload.get('text') or payload.get('caption') or '')
    command=command_for(text)
    identity=(contact['bot_id'],contact['chat_id'])
    selecting=contact.get('language_selecting',False)
    if (command=='no' and not text.startswith('/') and contact['consent'] and not selecting
            and (contact.get('journey') or {}).get('prompt_field')=='insurance'):
        # A natural-language "no insurance" answer is not consent withdrawal.
        command='none'
        text='none'
    code=language_choice(text, numbered=selecting or not contact.get('language_code'))
    stopping=command in {'/stop','stop','no','withdraw'}
    if stopping:
        db.execute('UPDATE telegram_contacts SET language_selecting=false WHERE bot_id=%s AND chat_id=%s',identity)
    if not stopping and (command=='/language' or (not contact.get('language_code') and (not contact['patient_id'] or command in {'hi','hello','/start'})) or selecting):
        if not code:
            db.execute('UPDATE telegram_contacts SET language_selecting=true WHERE bot_id=%s AND chat_id=%s',identity)
            return LANGUAGE_PROMPT, LANGUAGE_KEYBOARD
    if code and (selecting or not contact.get('language_code')):
        old=contact.get('language_code')
        db.execute('UPDATE telegram_contacts SET language_code=%s,language_selecting=false WHERE bot_id=%s AND chat_id=%s',(code,*identity))
        contact.update(language_code=code,language_selecting=False)
        audit(db,None,'patient_language_changed',{'from':old,'to':code},contact['patient_id'])
        command='/continue'
    # Keep the original text for NLP unless it is a recognized navigation word.
    if command != text.casefold().strip('.! ') or command.startswith('/'):
        payload['text']=command
    else:
        payload['text']=text
    result,keyboard=_respond(db,contact,{**message,'payload':payload})
    language=contact.get('language_code')
    result=localize(result,language)
    if keyboard==CONTACT_KEYBOARD:
        keyboard={'keyboard':[[{'text':localize('Share my phone number',language),'request_contact':True}]],
                  'resize_keyboard':True,'one_time_keyboard':True}
    elif keyboard==REMOVE_KEYBOARD:
        allowed=db.execute('SELECT consent FROM telegram_contacts WHERE bot_id=%s AND chat_id=%s',identity).fetchone()
        keyboard=NAV_KEYBOARD if allowed and allowed['consent'] and contact.get('patient_id') else CONSENT_KEYBOARD if not allowed or not allowed['consent'] else REMOVE_KEYBOARD
    return result,keyboard


def _respond(db, contact, message):
    from api import audit
    payload = message["payload"]
    text = (payload.get("text") or payload.get("caption") or "").strip()
    command = text.casefold().strip(".!")
    command = command.split("@", 1)[0] if command.startswith("/") else command
    bot_id, chat_id = contact["bot_id"], contact["chat_id"]
    patient_id = contact["patient_id"]
    identity = (bot_id, chat_id)
    if command in {"/stop", "stop", "no", "withdraw"}:
        db.execute("UPDATE telegram_contacts SET consent=false WHERE bot_id=%s AND chat_id=%s", identity)
        db.execute("UPDATE telegram_messages SET state='cancelled' WHERE bot_id=%s AND chat_id=%s AND state='ready'", identity)
        if patient_id:
            db.execute("UPDATE patients SET profile=jsonb_set(profile,'{consent}','false'),revision=revision+1 WHERE id=%s",
                       (patient_id,))
            db.execute("UPDATE reminders SET status='stopped' WHERE patient_id=%s AND audience='patient' AND status IN ('pending','queued')",
                       (patient_id,))
            audit(db, None, "patient_consent_withdrawn", {"channel": "telegram"}, patient_id)
        return "Thank you for letting us know. Processing is paused. Your social worker can still help you. Reply YES if you want to resume.", REMOVE_KEYBOARD
    if not contact["consent"]:
        if command not in {"yes", "agree", "i agree"}:
            return INTRODUCTION, REMOVE_KEYBOARD
        db.execute("UPDATE telegram_contacts SET consent=true WHERE bot_id=%s AND chat_id=%s", identity)
        if patient_id:
            db.execute("UPDATE patients SET profile=jsonb_set(profile,'{consent}','true'),revision=revision+1 WHERE id=%s",
                       (patient_id,))
            audit(db, None, "patient_consent_recorded", {"channel": "telegram"}, patient_id)
            return "You can continue now. Send /continue to pick up where you left off.", REMOVE_KEYBOARD
        return "Thank you. Tap Share my phone number so we can keep one record for you and coordinate your applications.", CONTACT_KEYBOARD
    if not patient_id:
        number = payload.get("contact") or {}
        if number.get("user_id") != chat_id:
            return "Please tap Share my phone number to share your own Telegram contact.", CONTACT_KEYBOARD
        phone = str(number.get("phone_number", "")).strip()
        phone = "+" + phone.lstrip("+")
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
            return "That number could not be read. Please share your own contact using the button.", CONTACT_KEYBOARD
        other = db.execute("SELECT chat_id FROM telegram_contacts WHERE bot_id=%s AND phone=%s AND chat_id<>%s",
                           (bot_id, phone, chat_id)).fetchone()
        if other:
            return "This contact needs a social worker's help to connect. Please contact your worker.", REMOVE_KEYBOARD
        db.execute("UPDATE telegram_contacts SET phone=%s WHERE bot_id=%s AND chat_id=%s", (phone, *identity))
        patient = db.execute(
            """INSERT INTO patients(phone,profile) VALUES (%s,%s) ON CONFLICT (phone) DO NOTHING RETURNING id""",
            (phone, Jsonb({"name": "Registration pending", "consent": True})),
        ).fetchone()
        if not patient:
            return "Your contact is received. Please ask your social worker to connect Telegram to your existing registration. Then send /start.", REMOVE_KEYBOARD
        patient_id = patient["id"]
        db.execute("UPDATE telegram_contacts SET patient_id=%s WHERE bot_id=%s AND chat_id=%s",
                   (patient_id, *identity))
        audit(db, None, "patient_registered", {"channel": "telegram"}, patient_id)
        from patient_journey import intake_message
        contact["patient_id"] = patient_id
        patient = db.execute("SELECT * FROM patients WHERE id=%s", (patient_id,)).fetchone()
        return ("Phone saved.\n" + intake_message(db, contact, patient)), REMOVE_KEYBOARD
    from patient_journey import (intake_message, choices_message, application_for_contact,
                                 documents_message, numbered_document, select_scheme,
                                 direct_answer, save_details, set_journey)
    patient = db.execute("SELECT * FROM patients WHERE id=%s FOR UPDATE", (patient_id,)).fetchone()
    if patient["archived_at"]:
        return "Your record is currently inactive. Please contact your social worker to reopen it. Your previous information is retained securely.", REMOVE_KEYBOARD
    if patient["status"] == "rejected":
        return ("Your registration could not be accepted at this time. Reason: " + patient["registration_reason"] +
                " Please contact your social worker to discuss the next steps. We are here to help."), REMOVE_KEYBOARD
    if not patient["profile"].get("consent"):
        return "Please ask your social worker to check your recorded consent before we read more details.", REMOVE_KEYBOARD
    journey = contact.get("journey") or {}
    application = application_for_contact(db, contact)
    from navigation import navigate
    navigation = navigate(db,contact,patient,command)
    if navigation is not None:
        return navigation, REMOVE_KEYBOARD
    if payload.get("document") or payload.get("photo"):
        from api import EDITABLE_APPLICATIONS, latest_documents
        requirement = journey.get("requirement")
        if application and not requirement:
            return "Please choose the document number first so we can file it correctly.\n\n" + documents_message(db, contact, application), REMOVE_KEYBOARD
        if application and application["status"] not in EDITABLE_APPLICATIONS:
            return "Your application is already with the team or agency. Send /status or /worker for help.", REMOVE_KEYBOARD
        if application and requirement not in application["snapshot"]["requirements"]:
            return documents_message(db, contact, application), REMOVE_KEYBOARD
        if application and any(d["requirement"] == requirement and d["status"] == "verified" for d in latest_documents(db, application["id"])):
            return "This document is already verified. Please choose another checklist item with /documents.", REMOVE_KEYBOARD
        filename, content = download_document(payload)
        db.execute(
            """INSERT INTO documents(patient_id,application_id,requirement,filename,content,ai_authorized,telegram_key)
               VALUES (%s,%s,%s,%s,%s,true,%s) ON CONFLICT (telegram_key) DO NOTHING""",
            (patient_id, application["id"] if application else None, requirement or "Patient supporting document", filename, content, message["key"]),
        )
        db.execute("UPDATE patients SET revision=revision+1 WHERE id=%s", (patient_id,))
        audit(db, None, "patient_document_received", {"channel": "telegram", "application": application["id"] if application else None,
                                                      "requirement": requirement}, patient_id)
        if application:
            set_journey(db, contact, {**journey, "requirement": None})
            return documents_message(db, contact, application, compact=True), REMOVE_KEYBOARD
        return "File received. We are reading it. We will ask only for missing details.", REMOVE_KEYBOARD
    if command in {"/worker", "worker"}:
        db.execute("INSERT INTO reminders(patient_id,audience,message,due) VALUES (%s,'worker',%s,now())",
                   (patient_id, "Patient requested help through Telegram"))
        audit(db, None, "patient_requested_worker", {"channel": "telegram"}, patient_id)
        return "Your request is with the social-work team. You do not need to work through this alone.", REMOVE_KEYBOARD
    if command in {"/status", "status"}:
        return status_reply(db, patient_id), REMOVE_KEYBOARD
    if command in {"/schemes", "/cost", "cost", "costs"}:
        if application and command != '/schemes':
            from patient_journey import selected_cost_message
            return selected_cost_message(db, contact, patient, application), REMOVE_KEYBOARD
        return choices_message(db, contact, patient), REMOVE_KEYBOARD
    if command in {"/documents", "documents"}:
        if application:
            set_journey(db, contact, {**journey, "requirement": None})
            return documents_message(db, contact, application), REMOVE_KEYBOARD
        return "Please choose a support pathway first.\n\n" + intake_message(db, contact, patient), REMOVE_KEYBOARD
    if text.isascii() and text.isdecimal():
        if journey.get("phase") == "schemes":
            return select_scheme(db, contact, patient, int(text)), REMOVE_KEYBOARD
        if application and journey.get("phase") in {"documents", "processing"}:
            return numbered_document(db, contact, application, int(text)), REMOVE_KEYBOARD
    if command in {"hi", "hello", "/start", "/help", "help"}:
        if application:
            return documents_message(db, contact, application), REMOVE_KEYBOARD
        return intake_message(db, contact, patient), REMOVE_KEYBOARD
    if command == "/details":
        set_journey(db, contact, {**journey, "phase": "intake", "prompt_field": None})
        return "Send the details you want to update, for example State: Tamil Nadu; Annual income: 90000; Hospital: ... You can also upload a hospital record. Your worker will verify changes.", REMOVE_KEYBOARD
    try:
        intent, fields = understand(text)
    except SarvamError:
        intent, fields = "details", {}
    if intent in {'back','resume','menu','schemes','documents','language'} and not fields:
        route={'back':'/back','resume':'/continue','menu':'/menu','schemes':'/schemes','documents':'/documents','language':'/language'}[intent]
        return _respond(db,contact,{**message,'payload':{'text':route}}) if intent!='language' else ('Send /language to choose your language.', REMOVE_KEYBOARD)
    if intent == "status" and not fields:
        return status_reply(db, patient_id), REMOVE_KEYBOARD
    if intent == "worker" and not fields:
        db.execute("INSERT INTO reminders(patient_id,audience,message,due) VALUES (%s,'worker','Patient requested help through Telegram',now())", (patient_id,))
        audit(db, None, "patient_requested_worker", {"channel": "telegram"}, patient_id)
        return "Your request is with our social-work team. We will help you work through the next step.", REMOVE_KEYBOARD
    if intent == "costs" and not fields:
        from patient_journey import selected_cost_message
        return (selected_cost_message(db, contact, patient, application), REMOVE_KEYBOARD) if application else (choices_message(db, contact, patient), REMOVE_KEYBOARD)
    if journey.get("phase", "intake") == "intake":
        fields = direct_answer(text, contact, fields)
    save_details(db, contact, fields)
    if application:
        return (("Thank you. Your updated details are saved for worker review.\n\n" if fields else "") + documents_message(db, contact, application)), REMOVE_KEYBOARD
    return intake_message(db, contact, patient), REMOVE_KEYBOARD


def process_inbox(db, bot_id):
    from api import audit
    messages = db.execute(
        "SELECT key FROM telegram_messages WHERE bot_id=%s AND state='received' ORDER BY created_at LIMIT 20",
        (bot_id,),
    ).fetchall()
    db.commit()
    for item in messages:
        with db.transaction():
            transaction_guard(db)
            message = db.execute("SELECT * FROM telegram_messages WHERE key=%s AND state='received' FOR UPDATE",
                                 (item["key"],)).fetchone()
            if not message:
                continue
            contact = db.execute("SELECT * FROM telegram_contacts WHERE bot_id=%s AND chat_id=%s FOR UPDATE",
                                 (bot_id, message["chat_id"])).fetchone()
            try:
                with db.transaction():
                    reply, keyboard = respond(db, contact, message)
            except TelegramError as exc:
                reply, keyboard = str(exc), REMOVE_KEYBOARD
            db.execute("UPDATE telegram_messages SET state='ready',reply=%s,keyboard=%s WHERE key=%s",
                       (reply, Jsonb(keyboard), item["key"]))
            # Long funding lists are delivered in full, in durable numbered parts.
            # Never silently truncate the checklist, options or calculation basis.
            parts = split_reply(reply)
            if len(parts) > 1:
                db.execute("UPDATE telegram_messages SET reply=%s WHERE key=%s", (parts[0], item["key"]))
                for index, part in enumerate(parts[1:], 1):
                    db.execute("""INSERT INTO telegram_messages(key,bot_id,chat_id,payload,state,reply,keyboard)
                                  VALUES (%s,%s,%s,'{}','ready',%s,%s) ON CONFLICT DO NOTHING""",
                               (item["key"]+f":part:{index:03}", bot_id, message["chat_id"], part, Jsonb(keyboard)))
    with db.transaction():
        outbox = db.execute("SELECT key FROM telegram_messages WHERE bot_id=%s AND state='ready' AND retry_at<=now() ORDER BY created_at,key LIMIT 20",
                            (bot_id,)).fetchall()
    for item in outbox:
        with delivery_guard(db):
            with db.transaction():
                row = db.execute("SELECT * FROM telegram_messages WHERE key=%s AND state='ready' FOR UPDATE", (item["key"],)).fetchone()
                if not row:
                    continue
                reminder_id = row["payload"].get("reminder_id")
                if reminder_id:
                    valid=db.execute(
                        """SELECT 1 FROM reminders r JOIN patients p ON p.id=r.patient_id
                           JOIN telegram_contacts c ON c.patient_id=p.id
                           WHERE r.id=%s AND r.status IN ('pending','queued') AND c.bot_id=%s AND c.chat_id=%s
                           AND c.consent AND p.profile->>'consent'='true' AND p.archived_at IS NULL AND p.status<>'rejected'""",
                        (reminder_id,bot_id,row["chat_id"]),
                    ).fetchone()
                    if not valid:
                        db.execute("UPDATE telegram_messages SET state='cancelled' WHERE key=%s",(row["key"],))
                        continue
                db.execute("UPDATE telegram_messages SET state='sending' WHERE key=%s", (row["key"],))
            try:
                telegram("sendMessage", {"chat_id": row["chat_id"], "text": row["reply"][:4000],
                                        "reply_markup": row["keyboard"] or REMOVE_KEYBOARD})
                state, error = "sent", None
            except TelegramError as exc:
                state, error = ("ready" if exc.code==429 else "failed" if exc.code in {400,401,403} else "delivery_uncertain"), str(exc)
                if exc.code==429:
                    with db.transaction():
                        db.execute("UPDATE telegram_messages SET retry_at=now()+(%s * interval '1 second') WHERE key=%s",
                                   (max(1,min(int(exc.retry_after or 30),86400)),row["key"]))
            with db.transaction():
                db.execute("UPDATE telegram_messages SET state=%s,error=%s,delivered_at=CASE WHEN %s='sent' THEN now() END WHERE key=%s",
                           (state, error, state, row["key"]))
                reminder_id=row["payload"].get("reminder_id")
                if state=="sent" and reminder_id:
                    db.execute("UPDATE reminders SET status='completed' WHERE id=%s AND status='queued'",(reminder_id,))
            time.sleep(1.1)


def split_reply(reply, limit=3800):
    # Count UTF-16 units as Telegram does; also safe for supplementary characters.
    parts, current, units = [], [], 0
    for char in reply:
        width = 2 if ord(char) > 0xffff else 1
        if units + width > limit:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    parts.append("".join(current))
    return parts


def document_completed(db, doc, result):
    from patient_journey import intake_message
    contacts = db.execute("SELECT * FROM telegram_contacts WHERE patient_id=%s AND consent FOR UPDATE",
                          (doc["patient_id"],)).fetchall()
    patient = db.execute("SELECT * FROM patients WHERE id=%s FOR UPDATE", (doc["patient_id"],)).fetchone()
    if patient["archived_at"] or patient["status"] == "rejected" or not patient["profile"].get("consent"):
        return
    # Checklist receipts advance immediately; OCR completion must not reset that journey.
    if doc.get("application_id"):
        return
    for contact in contacts:
        if (contact.get("journey") or {}).get("application_id"):
            continue
        pending=db.execute("SELECT 1 FROM documents WHERE patient_id=%s AND application_id IS NULL AND id<>%s AND ai_authorized AND (ai_result IS NULL OR ai_result->>'status' IN ('queued','pending','running')) LIMIT 1",(doc['patient_id'],doc['id'])).fetchone()
        if pending:
            continue
        reply = "Document read.\n" + intake_message(db, contact, patient)
        import hashlib
        from patient_journey import set_journey
        notice=hashlib.sha256(reply.encode()).hexdigest()
        if (contact.get('journey') or {}).get('last_ocr_notice')==notice:
            continue
        set_journey(db,contact,{**contact.get('journey',{}),'last_ocr_notice':notice})
        reply=localize(reply,contact.get('language_code'))
        for index, part in enumerate(split_reply(reply)):
            db.execute(
            """INSERT INTO telegram_messages(key,bot_id,chat_id,payload,state,reply,keyboard)
               VALUES (%s,%s,%s,'{}','ready',%s,%s) ON CONFLICT DO NOTHING""",
                (f"document:{doc['id']}:{contact['bot_id']}:{contact['chat_id']}:{index:03}",
                 contact["bot_id"], contact["chat_id"], part, Jsonb(REMOVE_KEYBOARD)),
            )



def queue_reminders(db,bot_id):
    from api import audit
    transaction_guard(db)
    rows=db.execute(
        """SELECT r.*,c.chat_id,c.language_code FROM reminders r
           JOIN patients p ON p.id=r.patient_id
           JOIN telegram_contacts c ON c.patient_id=r.patient_id AND c.bot_id=%s
           WHERE r.audience='patient' AND r.status='pending' AND r.due<=now()
           AND c.consent AND p.profile->>'consent'='true' AND p.archived_at IS NULL AND p.status<>'rejected'
           ORDER BY r.due LIMIT 20 FOR UPDATE OF r""",(bot_id,),
    ).fetchall()
    for row in rows:
        key=f"reminder:{row['id']}:{row['due'].isoformat()}"
        db.execute(
            """INSERT INTO telegram_messages(key,bot_id,chat_id,payload,state,reply)
               VALUES (%s,%s,%s,%s,'ready',%s) ON CONFLICT DO NOTHING""",
            (key,bot_id,row["chat_id"],Jsonb({"reminder_id":row["id"]}),
             localize("A reminder from your care team: "+row["message"]+" For help, send /worker.",row.get('language_code'))),
        )
        if row["repeat_days"]:
            db.execute("UPDATE reminders SET due=now()+(%s * interval '1 day') WHERE id=%s",
                       (row["repeat_days"],row["id"]))
        else:
            db.execute("UPDATE reminders SET status='queued' WHERE id=%s",(row["id"],))
        audit(db,None,"patient_reminder_queued",{"reminder":row["id"],"channel":"telegram"},row["patient_id"])


def main():
    from api import connect
    logging.basicConfig(level=logging.INFO)
    while True:
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            time.sleep(10)
            continue
        try:
            with connect() as db:
                if not db.execute("SELECT pg_try_advisory_lock(783314) AS locked").fetchone()["locked"]:
                    time.sleep(5)
                    continue
                bot = telegram("getMe")
                hook = telegram("getWebhookInfo")
                if hook.get("url"):
                    raise TelegramError("This bot has a webhook configured. Use a dedicated polling bot or remove that webhook.")
                db.execute("INSERT INTO telegram_bots(id,username) VALUES (%s,%s) "
                           "ON CONFLICT(id) DO UPDATE SET username=excluded.username,connected_at=now()",
                           (bot["id"], bot["username"]))
                db.commit()
                while os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
                    with db.transaction():
                        receive_updates(db, bot)
                    with db.transaction():
                        queue_reminders(db,bot["id"])
                    process_inbox(db, bot["id"])
                    time.sleep(1)
        except (TelegramError, psycopg.Error):
            log.warning("Telegram connection unavailable. Check bot configuration and service connectivity.")
            time.sleep(10)


if __name__ == "__main__":
    main()
