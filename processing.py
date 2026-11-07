"""Persistent document processing. Each document is serialized across workers."""
import logging
import time

from psycopg.types.json import Jsonb

from api import audit, connect
from intelligence import SarvamError, document_result, enabled, submit_document

logging.basicConfig(level=logging.INFO)


def process_once():
    if not enabled():
        return
    with connect() as db:
        rows = db.execute(
            """SELECT d.id FROM documents d JOIN patients p ON p.id=d.patient_id
               WHERE d.ai_authorized AND p.archived_at IS NULL AND p.status<>'rejected' AND p.profile->>'consent'='true'
               AND (d.ai_result IS NULL OR d.ai_result->>'status' IN ('queued','pending','running'))
               ORDER BY d.id LIMIT 20"""
        ).fetchall()
    for row in rows:
        with connect() as db:
            from maintenance import transaction_guard
            transaction_guard(db)
            if not db.execute("SELECT pg_try_advisory_lock(783312,%s) AS locked", (row["id"],)).fetchone()["locked"]:
                continue
            try:
                doc = db.execute(
                    """SELECT d.*,p.owner FROM documents d JOIN patients p ON p.id=d.patient_id
                       WHERE d.id=%s AND d.ai_authorized AND p.archived_at IS NULL AND p.status<>'rejected' AND p.profile->>'consent'='true'""", (row["id"],),
                ).fetchone()
                if not doc:
                    continue
                if not doc["ai_job"]:
                    job = submit_document(doc["filename"], doc["content"])
                    result = {"status": "pending"}
                    db.execute("UPDATE documents SET ai_job=%s,ai_result=%s WHERE id=%s",
                               (job, Jsonb(result), doc["id"]))
                else:
                    result = document_result(doc["ai_job"])
                    db.execute("UPDATE documents SET ai_result=%s,ai_error=NULL WHERE id=%s",
                               (Jsonb(result), doc["id"]))
                if result != doc["ai_result"]:
                    if result.get("status") in {"completed","partially_completed"} and doc.get("whatsapp_sid"):
                        from whatsapp import document_completed
                        document_completed(db,doc,result)
                    if result.get("status") in {"completed","partially_completed"} and doc.get("telegram_key"):
                        from telegram_channel import document_completed as telegram_document_completed
                        telegram_document_completed(db,doc,result)
                    audit(db, doc["owner"], "automatic_extraction_updated",
                          {"document": doc["id"], "status": result["status"]}, doc["patient_id"])
            except SarvamError as exc:
                db.execute("UPDATE documents SET ai_result=%s,ai_error=%s WHERE id=%s",
                           (Jsonb({"status": "error"}), str(exc), row["id"]))
            finally:
                db.execute("SELECT pg_advisory_unlock(783312,%s)", (row["id"],))


if __name__ == "__main__":
    while True:
        try:
            from whatsapp import process_messages
            process_messages()
            process_once()
        except Exception:
            logging.exception("Document processing cycle failed")
        time.sleep(8)
