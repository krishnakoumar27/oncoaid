"""OncoAid's staff API. Case writes and audit events share one transaction."""
import base64
import hashlib
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from catalog import CATALOG
from funding_sources import seed_sources
from costs import calculate_plan
from intelligence import (SarvamError, SchemeRetriever, document_result, enabled, funding_scenario,
                          scheme_documents, scheme_guidance, submit_document)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]
IST = ZoneInfo("Asia/Kolkata")

TRANSITIONS = {
    "draft": ["awaiting_documents", "under_review", "internally_rejected"],
    "awaiting_documents": ["under_review", "internally_rejected"],
    "under_review": ["awaiting_documents", "ready_for_submission", "internally_rejected"],
    "ready_for_submission": ["under_review", "submitted", "internally_rejected"],
    "submitted": ["approved", "agency_rejected"],
    "approved": [],
    "agency_rejected": [],
    "internally_rejected": [],
}
EDITABLE_APPLICATIONS = {"draft", "awaiting_documents", "under_review"}


def connect():
    return psycopg.connect(os.environ.get("DATABASE_URL", ""), row_factory=dict_row)


def database(request: Request):
    with connect() as connection:
        from maintenance import transaction_guard
        destructive = request.url.path == '/admin/patient-data/reset' or request.url.path.endswith('/permanent-delete')
        try:
            transaction_guard(connection, exclusive=destructive)
        except psycopg.errors.LockNotAvailable:
            fail(409, "A background operation is finishing. Please try the reset again shortly.")
        yield connection


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def one_click_enabled():
    return os.getenv('ONE_CLICK_ACCESS', 'false').strip().lower() in {'1', 'true', 'yes'}


def shared_account_email(role):
    return (os.getenv('ADMIN_EMAIL', 'admin@oncoaid.local') if role == 'admin'
            else os.getenv('WORKER_EMAIL', 'worker@oncoaid.local')).strip().lower()


def fail(status, message):
    raise HTTPException(status, message)


def audit(db, actor, action, detail, patient=None):
    db.execute(
        "INSERT INTO audit(staff_id,patient_id,action,detail) VALUES (%s,%s,%s,%s)",
        (actor, patient, action, Jsonb(detail)),
    )


@asynccontextmanager
async def lifespan(_):
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(783311)")
        db.execute(Path(__file__).with_name("schema.sql").read_text())
        if not db.execute("SELECT 1 FROM staff WHERE role='admin' LIMIT 1").fetchone():
            email = os.environ.get("ADMIN_EMAIL", "admin@oncoaid.local").lower().strip()
            password = os.environ.get("ADMIN_PASSWORD", "")
            if one_click_enabled() and not password:
                password = secrets.token_urlsafe(32)
            if len(password) < 12 or len(password) > 128:
                raise RuntimeError("Set ADMIN_PASSWORD to 12–128 characters before first startup.")
            admin = db.execute(
                "INSERT INTO staff(email,password,role) VALUES (%s,%s,'admin') RETURNING id",
                (email, password_hash(password)),
            ).fetchone()
            audit(db, admin["id"], "admin_bootstrapped", {})
        if one_click_enabled():
            for role in ('admin', 'worker'):
                account = db.execute('''INSERT INTO staff(email,password,role) VALUES (%s,%s,%s)
                    ON CONFLICT (email) DO NOTHING RETURNING id''',
                    (shared_account_email(role), password_hash(secrets.token_urlsafe(32)), role)).fetchone()
                if account:
                    audit(db, account['id'], 'shared_account_created', {'role': role})
        for name, kind, state in CATALOG:
            db.execute(
                "INSERT INTO schemes(name,details) VALUES (%s,%s) ON CONFLICT (name) DO NOTHING",
                (name, Jsonb({"kind": kind, "state": state, "published": False, "archived": False,
                              "source": "", "requirements": [], "opens": None, "closes": None,
                              "guidance": ""})),
            )
        seed_sources(db, audit)
    yield


app = FastAPI(title="OncoAid Staff API", lifespan=lifespan)
DB = Annotated[psycopg.Connection, Depends(database, scope="function")]
bearer = HTTPBearer(auto_error=False)


def current_user(db: DB, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if credentials is None:
        fail(401, "Sign in first.")
    user = db.execute(
        """SELECT staff.id,email,role,sessions.access_mode FROM staff JOIN sessions ON staff.id=sessions.staff_id
           WHERE token=%s AND expires>now()""", (token_hash(credentials.credentials),),
    ).fetchone()
    if not user or user["role"] not in {"admin", "worker"}:
        fail(401, "Session expired or account access is unavailable.")
    if user['access_mode'] == 'one_click' and not one_click_enabled():
        fail(401, 'Shared account access has been disabled. Sign in with an individual account.')
    return user


Staff = Annotated[dict, Depends(current_user)]


def admin_only(user):
    if user["role"] != "admin":
        fail(403, "Administrator access is required.")


class Credentials(BaseModel):
    email: Name
    password: Password


@app.get('/access-options')
def access_options(db: DB):
    accounts = []
    if one_click_enabled():
        for role, label in (('admin', 'Admin'), ('worker', 'Worker')):
            available = bool(db.execute('SELECT 1 FROM staff WHERE email=%s AND role=%s',
                                      (shared_account_email(role), role)).fetchone())
            accounts.append({'role': role, 'label': label, 'available': available})
    return {'one_click': one_click_enabled(), 'accounts': accounts}


class AccountSelection(BaseModel):
    role: Literal['admin', 'worker']


@app.post('/access/select')
def select_account(data: AccountSelection, db: DB):
    if not one_click_enabled():
        fail(403, 'One-click account access is disabled.')
    user = db.execute('SELECT id,email,role FROM staff WHERE email=%s AND role=%s',
                      (shared_account_email(data.role), data.role)).fetchone()
    if not user:
        fail(403, 'This shared account is unavailable. Ask the administrator to check its role.')
    return new_session(db, user, 'shared_account_signed_in')


def new_session(db, user, action='signed_in'):
    token = secrets.token_urlsafe(32)
    db.execute('DELETE FROM sessions WHERE expires<=now() AND staff_id=%s', (user['id'],))
    db.execute('INSERT INTO sessions(token,staff_id,access_mode) VALUES (%s,%s,%s)',
               (token_hash(token), user['id'], 'one_click' if action == 'shared_account_signed_in' else 'password'))
    audit(db, user['id'], action, {'role': user['role']})
    return {'token': token, 'id': user['id'], 'email': user['email'], 'role': user['role'],
            'one_click': one_click_enabled()}


@app.get("/health")
def health(db: DB):
    db.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/signup", status_code=201)
def signup(data: Credentials, db: DB):
    email = data.email.lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        fail(422, "Enter a valid email.")
    row = db.execute(
        """INSERT INTO staff(email,password,role) VALUES (%s,%s,'pending')
           ON CONFLICT (email) DO NOTHING RETURNING id""",
        (email, password_hash(data.password)),
    ).fetchone()
    if not row:
        fail(409, "An account already exists.")
    audit(db, row["id"], "worker_signed_up", {})
    return {"status": "pending"}


@app.post("/login")
def login(data: Credentials, db: DB):
    user = db.execute("SELECT * FROM staff WHERE email=%s", (data.email.lower(),)).fetchone()
    stored = user["password"] if user else "00" * 16 + ":" + "00" * 64
    candidate = password_hash(data.password, bytes.fromhex(stored.split(":")[0]))
    if not secrets.compare_digest(candidate, stored) or not user:
        fail(401, "Email or password is incorrect.")
    if user["role"] not in {"admin", "worker"}:
        fail(403, "This account needs administrator approval.")
    return new_session(db, user)


@app.post("/logout")
def logout(db: DB, user: Staff, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    db.execute("DELETE FROM sessions WHERE token=%s", (token_hash(credentials.credentials),))
    audit(db, user["id"], "signed_out", {})
    return {"ok": True}


@app.get("/staff")
def list_staff(db: DB, user: Staff):
    admin_only(user)
    return db.execute("SELECT id,email,role FROM staff ORDER BY email").fetchall()


class RoleChange(BaseModel):
    role: Literal["worker", "disabled"]


@app.post("/staff/{staff_id}")
def change_role(staff_id: int, data: RoleChange, db: DB, user: Staff):
    admin_only(user)
    target = db.execute("SELECT role FROM staff WHERE id=%s FOR UPDATE", (staff_id,)).fetchone()
    if not target or target["role"] == "admin":
        fail(404, "Worker account not found.")
    db.execute("UPDATE staff SET role=%s WHERE id=%s", (data.role, staff_id))
    db.execute("DELETE FROM sessions WHERE staff_id=%s", (staff_id,))
    audit(db, user["id"], "staff_role_changed", {"staff": staff_id, "from": target["role"], "to": data.role})
    return {"ok": True}


class Profile(BaseModel):
    name: Name
    phone: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+[1-9][0-9]{7,14}$")]
    state: Text = ""
    district: Text = ""
    birth_date: date | None = None
    diagnosis: Text = ""
    stage: Text = ""
    planned_treatment: Text = ""
    treatment_units: Text = ""
    hospital: Text = ""
    income: int | None = Field(default=None, ge=0, le=10**9)
    insurance: Text = ""
    address: Text = ""
    consent: bool = False


class Change(BaseModel):
    revision: int = Field(ge=1)
    reason: Text = ""


class ProfileEdit(Change):
    profile: Profile


def case_lock(db, user, patient_id, revision, require_owner=True):
    patient = db.execute("SELECT * FROM patients WHERE id=%s FOR UPDATE", (patient_id,)).fetchone()
    if not patient:
        fail(404, "Patient not found.")
    if patient["archived_at"]:
        fail(409, "Restore this archived case before editing.")
    if require_owner and patient["owner"] != user["id"]:
        fail(403, "Take ownership of this case before editing.")
    if patient["revision"] != revision:
        fail(409, "This case changed. Refresh before saving.")
    db.execute("UPDATE patients SET revision=revision+1 WHERE id=%s", (patient_id,))
    return patient


@app.get("/patients")
def patients(db: DB, user: Staff, archived: bool = False):
    return db.execute(
        """SELECT p.id,p.phone,p.status,p.owner,p.revision,p.archived_at,p.profile->>'name' AS name,s.email AS assigned_to
           FROM patients p LEFT JOIN staff s ON s.id=p.owner WHERE (p.archived_at IS NOT NULL)=%s ORDER BY p.id DESC""", (archived,)
    ).fetchall()


@app.post("/patients", status_code=201)
def create_patient(data: Profile, db: DB, user: Staff):
    row = db.execute(
        """INSERT INTO patients(phone,profile,owner) VALUES (%s,%s,%s)
           ON CONFLICT (phone) DO NOTHING RETURNING id""",
        (data.phone, Jsonb(data.model_dump(mode="json", exclude={"phone"})), user["id"]),
    ).fetchone()
    if not row:
        fail(409, "This number is already registered.")
    audit(db, user["id"], "registration_created", {"source": "manual"}, row["id"])
    return row


@app.post("/patients/{patient_id}/claim")
def claim(patient_id: int, data: Change, db: DB, user: Staff):
    row = case_lock(db, user, patient_id, data.revision, require_owner=False)
    if row["owner"] and row["owner"] != user["id"] and not data.reason:
        fail(422, "Enter a reason for the handoff.")
    db.execute("UPDATE patients SET owner=%s WHERE id=%s", (user["id"], patient_id))
    audit(db, user["id"], "assignment_changed", {"from": row["owner"], "to": user["id"],
                                                   "reason": data.reason}, patient_id)
    return {"ok": True}


@app.post("/patients/{patient_id}/profile")
def update_profile(patient_id: int, data: ProfileEdit, db: DB, user: Staff):
    row = case_lock(db, user, patient_id, data.revision)
    if row["phone"] != data.profile.phone:
        fail(422, "The registered phone number is fixed.")
    profile = data.profile.model_dump(mode="json", exclude={"phone"})
    changed = [key for key in profile if profile[key] != row["profile"].get(key)]
    if changed:
        status = "pending" if row["status"] == "verified" else row["status"]
        db.execute("UPDATE patients SET profile=%s,status=%s WHERE id=%s",
                   (Jsonb(profile), status, patient_id))
        audit(db, user["id"], "profile_changed", {"changes": {key: {"from": row["profile"].get(key), "to": profile[key]} for key in changed}, "status": status}, patient_id)
    return {"ok": True}


class Registration(Change):
    status: Literal["pending", "needs_information", "verified", "rejected"]


@app.post("/patients/{patient_id}/registration")
def registration(patient_id: int, data: Registration, db: DB, user: Staff):
    row = case_lock(db, user, patient_id, data.revision)
    profile = row["profile"]
    if data.status == "verified":
        required = ("state", "district", "birth_date", "diagnosis", "hospital", "insurance", "consent")
        missing = [key for key in required if not profile.get(key)]
        if profile.get("income") is None:
            missing.append("income")
        if missing:
            fail(422, "Complete these fields first: " + ", ".join(missing))
        if date.fromisoformat(profile["birth_date"]) > datetime.now(IST).date():
            fail(422, "Date of birth cannot be in the future.")
    if data.status in {"rejected", "needs_information"} and not data.reason:
        fail(422, "Record a reason the patient can understand.")
    db.execute("UPDATE patients SET status=%s,registration_reason=%s WHERE id=%s", (data.status, data.reason, patient_id))
    if data.status == "rejected":
        db.execute("UPDATE reminders SET status='stopped' WHERE patient_id=%s AND audience='patient' AND status IN ('pending','queued')", (patient_id,))
        db.execute("UPDATE telegram_messages m SET state='cancelled' FROM telegram_contacts c WHERE m.bot_id=c.bot_id AND m.chat_id=c.chat_id AND c.patient_id=%s AND m.state='ready'", (patient_id,))
    audit(db, user["id"], "registration_changed",
          {"from": row["status"], "to": data.status, "reason": data.reason}, patient_id)
    return {"ok": True}


class Archive(Change):
    archived: bool = True


@app.post("/patients/{patient_id}/archive")
def archive_patient(patient_id: int, data: Archive, db: DB, user: Staff):
    row = db.execute("SELECT * FROM patients WHERE id=%s FOR UPDATE", (patient_id,)).fetchone()
    if not row:
        fail(404, "Patient not found.")
    if row["owner"] != user["id"] and user["role"] != "admin":
        fail(403, "Only the assigned worker or an administrator can archive or restore a case.")
    if row["revision"] != data.revision:
        fail(409, "This case changed. Refresh before saving.")
    if not data.reason:
        fail(422, "Record a reason for archiving or restoring this case.")
    db.execute("UPDATE patients SET archived_at=CASE WHEN %s THEN now() ELSE NULL END,revision=revision+1 WHERE id=%s", (data.archived, patient_id))
    if data.archived:
        db.execute("UPDATE reminders SET status='stopped' WHERE patient_id=%s AND status IN ('pending','queued')", (patient_id,))
        db.execute("UPDATE telegram_messages m SET state='cancelled' FROM telegram_contacts c WHERE m.bot_id=c.bot_id AND m.chat_id=c.chat_id AND c.patient_id=%s AND m.state='ready'", (patient_id,))
    audit(db, user["id"], "patient_archived" if data.archived else "patient_restored", {"reason": data.reason}, patient_id)
    return {"ok": True}


class PermanentDelete(BaseModel):
    revision: int = Field(ge=1)
    confirmation: str


def erase_patient_records(db, patient_id=None):
    """Delete child rows before parents in one transaction; preserve bot cursors."""
    counts = {}
    if patient_id is None:
        # Includes contacts/messages that never completed registration.
        for table in ('telegram_messages', 'telegram_contacts', 'whatsapp_messages', 'whatsapp_contacts',
                      'reminders', 'documents', 'applications'):
            counts[table] = db.execute(f'DELETE FROM {table}').rowcount
        counts['audit'] = db.execute('DELETE FROM audit WHERE patient_id IS NOT NULL').rowcount
        counts['patients'] = db.execute('DELETE FROM patients').rowcount
    else:
        patient = db.execute('SELECT phone FROM patients WHERE id=%s', (patient_id,)).fetchone()
        phone = patient['phone']
        counts['telegram_messages'] = db.execute('''DELETE FROM telegram_messages m USING telegram_contacts c
            WHERE m.bot_id=c.bot_id AND m.chat_id=c.chat_id AND (c.patient_id=%s OR c.phone=%s)''', (patient_id, phone)).rowcount
        counts['telegram_contacts'] = db.execute('DELETE FROM telegram_contacts WHERE patient_id=%s OR phone=%s', (patient_id, phone)).rowcount
        counts['whatsapp_messages'] = db.execute('''DELETE FROM whatsapp_messages m USING whatsapp_contacts c
            WHERE m.phone=c.phone AND (c.patient_id=%s OR c.phone=%s)''', (patient_id, phone)).rowcount
        counts['whatsapp_contacts'] = db.execute('DELETE FROM whatsapp_contacts WHERE patient_id=%s OR phone=%s', (patient_id, phone)).rowcount
        for table in ('reminders', 'documents', 'audit', 'applications'):
            counts[table] = db.execute(f'DELETE FROM {table} WHERE patient_id=%s', (patient_id,)).rowcount
        counts['patients'] = db.execute('DELETE FROM patients WHERE id=%s', (patient_id,)).rowcount
    return counts


@app.post('/patients/{patient_id}/permanent-delete')
def permanently_delete_patient(patient_id: int, data: PermanentDelete, db: DB, user: Staff):
    admin_only(user)
    if data.confirmation != 'DELETE':
        fail(422, 'Type DELETE to confirm permanent patient deletion.')
    row = db.execute('SELECT revision FROM patients WHERE id=%s FOR UPDATE', (patient_id,)).fetchone()
    if not row:
        fail(404, 'Patient not found.')
    if row['revision'] != data.revision:
        fail(409, 'This case changed. Refresh before deleting.')
    counts = erase_patient_records(db, patient_id)
    # No name, phone, chat ID or patient ID survives in this maintenance event.
    audit(db, user['id'], 'patient_permanently_deleted', {'deleted': counts})
    return {'ok': True, 'deleted': counts}


class PatientDataReset(BaseModel):
    confirmation: str


@app.get('/admin/patient-data')
def patient_data_counts(db: DB, user: Staff):
    admin_only(user)
    return {table: db.execute(f'SELECT count(*) AS count FROM {table}').fetchone()['count']
            for table in ('patients', 'applications', 'documents', 'reminders', 'telegram_contacts',
                          'telegram_messages', 'whatsapp_contacts', 'whatsapp_messages')}


@app.post('/admin/patient-data/reset')
def reset_patient_data(data: PatientDataReset, db: DB, user: Staff):
    admin_only(user)
    if data.confirmation != 'RESET':
        fail(422, 'Type RESET to confirm permanent deletion of all patient data.')
    counts = erase_patient_records(db)
    audit(db, user['id'], 'patient_data_reset', {'deleted': counts})
    return {'ok': True, 'deleted': counts}


class Scheme(BaseModel):
    name: Name
    kind: Literal["central", "state", "organization"]
    state: Text = ""
    source: Text = ""
    requirements: list[Name] = Field(default_factory=list, max_length=30)
    opens: date | None = None
    closes: date | None = None
    guidance: Text = ""
    published: bool = False
    archived: bool = False
    revision: int | None = None
    coverage_rate: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = None
    coverage_cap: Annotated[int, Field(gt=0, le=10**9)] | None = None
    coverage_kind: Literal['percentage', 'package_limit', 'grant_limit', 'case_by_case'] | None = None
    coverage_source: Text = ''
    coverage_checked: date | None = None
    coverage_notes: Text = ''

    @property
    def has_coverage(self):
        return self.coverage_rate is not None or self.coverage_cap is not None


def validity(details):
    today = datetime.now(IST).date().isoformat()
    if details["archived"]:
        return "archived"
    if not details["published"]:
        return "unverified"
    if details["closes"] and today > details["closes"]:
        return "expired"
    if details["opens"] and today < details["opens"]:
        return "upcoming"
    return "active"


@app.get("/schemes")
def schemes(db: DB, user: Staff):
    rows = db.execute("SELECT * FROM schemes ORDER BY name").fetchall()
    return [{**row, "validity": validity(row["details"])} for row in rows]


@app.post("/schemes")
def save_scheme(data: Scheme, db: DB, user: Staff, scheme_id: int | None = None):
    if data.opens and data.closes and data.opens > data.closes:
        fail(422, "Closing date must be on or after opening date.")
    if len(set(data.requirements)) != len(data.requirements):
        fail(422, "Checklist items must be unique.")
    if data.published and (not data.source.startswith("https://") or not urlsplit(data.source).hostname
                           or not data.requirements or not data.guidance):
        fail(422, "Activation requires an HTTPS source, eligibility notes and a document checklist.")
    from benefits import coverage_kind
    benefit = coverage_kind(data.model_dump())
    if benefit == 'percentage' and data.coverage_rate is None:
        fail(422, 'Percentage benefits need a coverage rate.')
    if benefit in {'package_limit', 'grant_limit'} and data.coverage_cap is None:
        fail(422, 'Package and grant limits need a maximum benefit amount.')
    if benefit != 'percentage' and data.coverage_rate is not None:
        fail(422, 'A percentage applies only to a percentage benefit.')
    if benefit == 'case_by_case' and data.has_coverage:
        fail(422, 'Choose a percentage, package limit or grant limit for numeric benefit rules.')
    benefit_source = data.coverage_source or data.source
    if data.has_coverage and (not benefit_source.startswith('https://') or not urlsplit(benefit_source).hostname):
        fail(422, 'Numeric benefits require an HTTPS source for the coverage rule.')
    if data.coverage_checked and data.coverage_checked > datetime.now(IST).date():
        fail(422, 'Coverage verification date cannot be in the future.')
    details = data.model_dump(mode="json", exclude={"name", "revision"})
    details['coverage_kind'] = benefit
    if scheme_id is None:
        row = db.execute(
            """INSERT INTO schemes(name,details) VALUES (%s,%s)
               ON CONFLICT (name) DO NOTHING RETURNING id,revision""",
            (data.name, Jsonb(details)),
        ).fetchone()
        if not row:
            fail(409, "That scheme name is already in the catalog.")
    else:
        previous = db.execute('SELECT details FROM schemes WHERE id=%s', (scheme_id,)).fetchone()
        if previous and previous['details'].get('benefit_seed_version'):
            details['benefit_seed_version'] = previous['details']['benefit_seed_version']
        row = db.execute(
            """UPDATE schemes SET details=%s,revision=revision+1 WHERE id=%s AND revision=%s AND name=%s
               RETURNING id,revision""", (Jsonb(details), scheme_id, data.revision, data.name),
        ).fetchone()
        if not row:
            fail(409, "Refresh the catalog before editing. Existing names are fixed.")
    audit(db, user["id"], "scheme_saved", {**row, "name": data.name, "details": details})
    return row


class NewApplication(Change):
    scheme_id: int


@app.post("/patients/{patient_id}/applications")
def create_application(patient_id: int, data: NewApplication, db: DB, user: Staff):
    patient = case_lock(db, user, patient_id, data.revision)
    if patient["status"] != "verified":
        fail(409, "Verify the patient registration first.")
    scheme = db.execute("SELECT * FROM schemes WHERE id=%s FOR SHARE", (data.scheme_id,)).fetchone()
    if not scheme or validity(scheme["details"]) != "active":
        fail(409, "Choose an active scheme.")
    state = scheme["details"]["state"]
    if state and state.casefold() != patient["profile"]["state"].casefold():
        fail(422, "This scheme is listed for a different state.")
    if db.execute(
        """SELECT 1 FROM applications WHERE patient_id=%s AND scheme_id=%s
           AND status NOT IN ('agency_rejected','internally_rejected')""", (patient_id, data.scheme_id)
    ).fetchone():
        fail(409, "An application for this scheme already exists.")
    snapshot = {**scheme["details"], "name": scheme["name"], "revision": scheme["revision"]}
    row = db.execute(
        """INSERT INTO applications(patient_id,scheme_id,snapshot) VALUES (%s,%s,%s) RETURNING id""",
        (patient_id, data.scheme_id, Jsonb(snapshot)),
    ).fetchone()
    audit(db, user["id"], "application_created", {"application": row["id"], "scheme": scheme["name"]},
          patient_id)
    return row


def application_for(db, patient_id, application_id):
    row = db.execute("SELECT * FROM applications WHERE id=%s AND patient_id=%s",
                     (application_id, patient_id)).fetchone()
    if not row:
        fail(404, "Application not found for this case.")
    return row


def latest_documents(db, application_id):
    return db.execute(
        """SELECT DISTINCT ON (requirement) id,requirement,status,reason FROM documents
           WHERE application_id=%s ORDER BY requirement,id DESC""", (application_id,),
    ).fetchall()


def stop_reminders(db, user, patient_id, application_id, requirement=None):
    rows = db.execute(
        """UPDATE reminders SET status='stopped' WHERE patient_id=%s AND application_id=%s
           AND status IN ('pending','queued') AND (%s::text IS NULL OR requirement=%s) RETURNING id""",
        (patient_id, application_id, requirement, requirement),
    ).fetchall()
    for row in rows:
        audit(db, user["id"], "reminder_stopped", row, patient_id)


class ApplicationUpdate(Change):
    status: str
    reference: Text = ""


@app.post("/patients/{patient_id}/applications/{application_id}/status")
def update_application(patient_id: int, application_id: int, data: ApplicationUpdate, db: DB, user: Staff):
    patient = case_lock(db, user, patient_id, data.revision)
    record = application_for(db, patient_id, application_id)
    if data.status not in TRANSITIONS[record["status"]]:
        fail(409, "This status change is unavailable.")
    if data.status in {"internally_rejected", "agency_rejected"} and not data.reason:
        fail(422, "A rejection reason is required.")
    if data.status in {"ready_for_submission", "submitted"}:
        if patient["status"] != "verified":
            fail(409, "Reverify the registration before proceeding.")
        verified = {d["requirement"] for d in latest_documents(db, application_id) if d["status"] == "verified"}
        missing = set(record["snapshot"]["requirements"]) - verified
        if missing:
            fail(409, "Verify these documents first: " + ", ".join(sorted(missing)))
    if data.status == "submitted" and not data.reference:
        fail(422, "Enter the submission reference.")
    reference = data.reference or record["reference"]
    db.execute("UPDATE applications SET status=%s,reason=%s,reference=%s WHERE id=%s",
               (data.status, data.reason, reference, application_id))
    if data.status in {"submitted", "approved", "internally_rejected", "agency_rejected"}:
        stop_reminders(db, user, patient_id, application_id)
    audit(db, user["id"], "application_status_changed",
          {"application": application_id, "from": record["status"], "to": data.status,
           "reason": data.reason, "reference": reference}, patient_id)
    return {"ok": True}


class Upload(Change):
    requirement: Name = "Supporting document"
    filename: Name
    content: str = Field(max_length=7_000_000)
    auto_extract: bool = False


@app.post("/patients/{patient_id}/applications/{application_id}/documents")
def upload(patient_id: int, application_id: int, data: Upload, db: DB, user: Staff):
    case_lock(db, user, patient_id, data.revision)
    record = application_for(db, patient_id, application_id)
    if record["status"] not in EDITABLE_APPLICATIONS:
        fail(409, "Move the application back to review to change its checklist.")
    if data.requirement not in record["snapshot"]["requirements"]:
        fail(422, "Select a document from the application checklist.")
    return store_document(db, user, patient_id, data, application_id)


@app.post("/patients/{patient_id}/documents", status_code=201)
def upload_case_document(patient_id: int, data: Upload, db: DB, user: Staff):
    case_lock(db, user, patient_id, data.revision)
    return store_document(db, user, patient_id, data)


def store_document(db, user, patient_id, data, application_id=None):
    if data.auto_extract:
        profile = db.execute("SELECT profile FROM patients WHERE id=%s", (patient_id,)).fetchone()["profile"]
        if not profile.get("consent"):
            fail(422, "Record patient consent before automatic extraction.")
    try:
        binary = base64.b64decode(data.content, validate=True)
    except ValueError:
        fail(422, "File data is invalid.")
    if not binary or len(binary) > 5 * 1024 * 1024:
        fail(422, "Upload a file of 5 MB or less.")
    filename = data.filename.replace("\\", "/").split("/")[-1]
    signatures = {".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff",
                  ".jpeg": b"\xff\xd8\xff"}
    signature = signatures.get(Path(filename).suffix.lower())
    if not signature or not binary.startswith(signature):
        fail(422, "Upload a PDF, PNG or JPEG file.")
    row = db.execute(
        """INSERT INTO documents(patient_id,application_id,requirement,filename,content) VALUES (%s,%s,%s,%s,%s)
           RETURNING id""", (patient_id, application_id, data.requirement, filename, binary),
    ).fetchone()
    if data.auto_extract:
        db.execute("UPDATE documents SET ai_authorized=true WHERE id=%s", (row["id"],))
    audit(db, user["id"], "document_uploaded",
          {"application": application_id, "document": row["id"], "requirement": data.requirement}, patient_id)
    return row


class DocumentReview(Change):
    status: Literal["verified", "rejected"]


@app.post("/patients/{patient_id}/applications/{application_id}/documents/{document_id}")
def review_document(patient_id: int, application_id: int, document_id: int, data: DocumentReview,
                    db: DB, user: Staff):
    case_lock(db, user, patient_id, data.revision)
    record = application_for(db, patient_id, application_id)
    if record["status"] not in EDITABLE_APPLICATIONS:
        fail(409, "Document review is unavailable in this application status.")
    doc = next((d for d in latest_documents(db, application_id) if d["id"] == document_id), None)
    if not doc or doc["status"] != "received":
        fail(409, "Only the latest received version can be reviewed.")
    if data.status == "rejected" and not data.reason:
        fail(422, "Explain how to correct the document.")
    db.execute("UPDATE documents SET status=%s,reason=%s WHERE id=%s",
               (data.status, data.reason, document_id))
    if data.status == "verified":
        stop_reminders(db, user, patient_id, application_id, doc["requirement"])
    audit(db, user["id"], "document_reviewed",
          {"application": application_id, "document": document_id, "status": data.status,
           "reason": data.reason}, patient_id)
    return {"ok": True}


@app.get("/documents/{document_id}")
def download_document(document_id: int, db: DB, user: Staff):
    row = db.execute(
        """SELECT content,patient_id FROM documents WHERE id=%s""", (document_id,),
    ).fetchone()
    if not row:
        fail(404, "Document not found.")
    audit(db, user["id"], "document_downloaded", {"document": document_id}, row["patient_id"])
    return Response(row["content"], media_type="application/octet-stream",
                    headers={"Content-Disposition": "attachment", "Cache-Control": "no-store"})


class Draft(Change):
    text: Text


@app.post("/patients/{patient_id}/applications/{application_id}/draft")
def draft(patient_id: int, application_id: int, data: Draft, db: DB, user: Staff):
    case_lock(db, user, patient_id, data.revision)
    record = application_for(db, patient_id, application_id)
    if record["status"] not in EDITABLE_APPLICATIONS:
        fail(409, "Reopen review before changing the draft.")
    db.execute("UPDATE applications SET draft=%s WHERE id=%s", (data.text, application_id))
    audit(db, user["id"], "draft_saved", {"application": application_id}, patient_id)
    return {"ok": True}


class Phase(BaseModel):
    name: Name
    amount: Annotated[str, StringConstraints(min_length=1, max_length=24)]


class Allocation(BaseModel):
    phase: Name
    source: Name
    status: Literal["potential", "confirmed"]
    amount: Annotated[str, StringConstraints(min_length=1, max_length=24)]


class CostPlan(Change):
    phases: list[Phase] = Field(min_length=1, max_length=30)
    coverage: list[Allocation] = Field(max_length=100)


@app.post("/patients/{patient_id}/cost-plan")
def save_cost_plan(patient_id: int, data: CostPlan, db: DB, user: Staff):
    patient = case_lock(db, user, patient_id, data.revision)
    inputs = data.model_dump(mode="json", include={"phases", "coverage"})
    try:
        plan = calculate_plan(**inputs)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        fail(422, "Check phase names, amounts and coverage. Allocations cannot exceed phase costs.")
    db.execute("UPDATE patients SET cost_plan=%s WHERE id=%s", (Jsonb(inputs), patient_id))
    audit(db, user["id"], "cost_plan_saved", {"total": str(plan["total_cost"]), "previous": patient["cost_plan"], "updated": inputs}, patient_id)
    return plan


class Reminder(Change):
    application_id: int | None = None
    requirement: Name | None = None
    audience: Literal["patient", "worker"]
    message: Name
    due: datetime
    repeat_days: int | None = Field(default=None, ge=1, le=365)


@app.post("/patients/{patient_id}/reminders")
def add_reminder(patient_id: int, data: Reminder, db: DB, user: Staff):
    patient = case_lock(db, user, patient_id, data.revision)
    if data.due.tzinfo is None:
        fail(422, "Provide the reminder time with a timezone.")
    if data.audience == "patient" and not patient["profile"]["consent"]:
        fail(422, "Record consent before planning patient messages.")
    if data.application_id is not None:
        record = application_for(db, patient_id, data.application_id)
        if record["status"] not in {*EDITABLE_APPLICATIONS, "ready_for_submission"}:
            fail(409, "The application is no longer accepting document reminders.")
        if data.requirement and data.requirement not in record["snapshot"]["requirements"]:
            fail(422, "Choose a checklist item from this application.")
        if any(d["requirement"] == data.requirement and d["status"] == "verified"
               for d in latest_documents(db, data.application_id)):
            fail(409, "The document is already verified.")
    elif data.requirement:
        fail(422, "Document reminders require an application.")
    row = db.execute(
        """INSERT INTO reminders(patient_id,application_id,requirement,audience,message,due,repeat_days)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (patient_id, data.application_id, data.requirement, data.audience, data.message,
         data.due, data.repeat_days),
    ).fetchone()
    audit(db, user["id"], "reminder_planned",
          {**row, "audience": data.audience, "due": data.due.isoformat()}, patient_id)
    return row


@app.post("/patients/{patient_id}/reminders/{reminder_id}/complete")
def complete_reminder(patient_id: int, reminder_id: int, data: Change, db: DB, user: Staff):
    case_lock(db, user, patient_id, data.revision)
    row = db.execute(
        """UPDATE reminders SET status='stopped' WHERE id=%s AND patient_id=%s AND status IN ('pending','queued')
           RETURNING id""", (reminder_id, patient_id),
    ).fetchone()
    if not row:
        fail(404, "Pending reminder not found.")
    audit(db, user["id"], "reminder_closed", row, patient_id)
    return row


@app.get("/reminders/due")
def due_reminders(db: DB, user: Staff):
    return db.execute(
        """SELECT r.id,r.patient_id,r.message,r.due,p.profile->>'name' AS patient
           FROM reminders r JOIN patients p ON p.id=r.patient_id
           WHERE r.audience='worker' AND r.status='pending' AND r.due<=now()
           ORDER BY r.due"""
    ).fetchall()


@app.get("/audit")
def audit_log(db: DB, user: Staff, patient_id: int | None = None):
    return db.execute(
        """SELECT a.id,a.created_at,a.action,a.detail,s.email AS actor
           FROM audit a LEFT JOIN staff s ON s.id=a.staff_id
           WHERE a.patient_id IS NOT DISTINCT FROM %s ORDER BY a.id DESC LIMIT 200""",
        (patient_id,),
    ).fetchall()


@app.get("/patients/{patient_id}")
def case_detail(patient_id: int, db: DB, user: Staff):
    row = db.execute(
        """SELECT p.*,s.email AS assigned_to FROM patients p LEFT JOIN staff s ON s.id=p.owner
           WHERE p.id=%s""", (patient_id,),
    ).fetchone()
    if not row:
        fail(404, "Patient not found.")
    applications = db.execute("SELECT * FROM applications WHERE patient_id=%s ORDER BY id DESC",
                              (patient_id,)).fetchall()
    for record in applications:
        record["next_statuses"] = TRANSITIONS[record["status"]]
        record["documents"] = db.execute(
            """SELECT id,requirement,filename,status,reason,created_at,ai_result,ai_error,ai_authorized,
                      (ai_job IS NOT NULL) AS ai_started FROM documents
               WHERE application_id=%s ORDER BY id DESC""", (record["id"],),
        ).fetchall()
    reminders = db.execute("SELECT * FROM reminders WHERE patient_id=%s ORDER BY due",
                           (patient_id,)).fetchall()
    plan = calculate_plan(**row["cost_plan"]) if row["cost_plan"] else None
    case_documents = db.execute(
        """SELECT id,requirement,filename,created_at,ai_result,ai_error,ai_authorized,(ai_job IS NOT NULL) AS ai_started
           FROM documents WHERE patient_id=%s AND application_id IS NULL ORDER BY id DESC""",
        (patient_id,),
    ).fetchall()
    contact = db.execute("SELECT consent,patient_id,suggested_fields FROM whatsapp_contacts WHERE phone=%s",
                         (row["phone"],)).fetchone()
    communications = db.execute(
        """SELECT sid,status,error,created_at,reply FROM whatsapp_messages WHERE phone=%s
           ORDER BY created_at DESC LIMIT 15""",(row["phone"],),
    ).fetchall()
    telegram_contact = db.execute(
        "SELECT patient_id,consent,suggested_fields,journey,language_code FROM telegram_contacts WHERE phone=%s LIMIT 1",
        (row["phone"],),
    ).fetchone()
    telegram_messages = db.execute(
        """SELECT 'Telegram' AS channel,m.state AS status,m.reply,m.error,m.created_at
           FROM telegram_messages m JOIN telegram_contacts c USING(bot_id,chat_id)
           WHERE c.patient_id=%s ORDER BY m.created_at DESC LIMIT 20""",(patient_id,),
    ).fetchall()
    return {**row, "telegram_contact":telegram_contact,"telegram_messages":telegram_messages,"whatsapp_contact": contact, "communications": communications, "applications": applications, "reminders": reminders, "calculated_plan": plan,
            "documents": case_documents, "sarvam_enabled": enabled()}


class ExtractionRequest(Change):
    consent_to_process: bool


@app.post("/patients/{patient_id}/documents/{document_id}/extract")
def extract_document(patient_id: int, document_id: int, data: ExtractionRequest, db: DB, user: Staff):
    patient = case_lock(db, user, patient_id, data.revision)
    if not patient["profile"].get("consent") or not data.consent_to_process:
        fail(422, "Record patient consent and confirm sending this document to Sarvam.")
    row = db.execute(
        "SELECT filename,content,ai_job FROM documents WHERE patient_id=%s AND id=%s",
        (patient_id, document_id),
    ).fetchone()
    if not row:
        fail(404, "Document not found.")
    if not enabled():
        fail(503, "Sarvam key is missing from the API service. Recreate the API and processor containers.")
    db.execute(
        "UPDATE documents SET ai_authorized=true,ai_result=%s,ai_error=NULL WHERE id=%s",
        (Jsonb({"status": "queued"} if not row["ai_job"] else {"status": "pending"}), document_id),
    )
    audit(db, user["id"], "document_extraction_authorized", {"document": document_id}, patient_id)
    return {"status": "queued"}


@app.post("/patients/{patient_id}/documents/{document_id}/extract/refresh")
def refresh_extraction(patient_id: int, document_id: int, data: Change, db: DB, user: Staff):
    patient = case_lock(db, user, patient_id, data.revision)
    if not patient["profile"].get("consent"):
        fail(422, "Patient consent is required.")
    row = db.execute("SELECT ai_job,ai_result FROM documents WHERE patient_id=%s AND id=%s",
                     (patient_id, document_id)).fetchone()
    if not row or not row["ai_job"]:
        fail(404, "No extraction job found.")
    if row["ai_result"]["status"] in {"completed", "partially_completed", "failed", "rejected", "error"}:
        return row["ai_result"]
    try:
        result = document_result(row["ai_job"])
    except SarvamError as exc:
        fail(502, str(exc))
    if result != row["ai_result"]:
        db.execute("UPDATE documents SET ai_result=%s WHERE id=%s", (Jsonb(result), document_id))
        audit(db, user["id"], "document_extraction_updated",
              {"document": document_id, "status": result["status"]}, patient_id)
    return result


def active_sources(db, patient):
    state = patient["profile"].get("state", "").casefold()
    rows = db.execute("SELECT * FROM schemes ORDER BY name").fetchall()
    return [row for row in rows if validity(row["details"]) == "active"
            and row["details"]["state"].casefold() in {"", state}]


def funding_patient(db, patient_id):
    patient = db.execute("SELECT * FROM patients WHERE id=%s", (patient_id,)).fetchone()
    if not patient:
        fail(404, "Patient not found.")
    return patient


@app.get("/patients/{patient_id}/funding-sources")
def funding_sources(patient_id: int, db: DB, user: Staff):
    patient = funding_patient(db, patient_id)
    rows = active_sources(db, patient)
    indexed = {row["id"]: row for row in rows}
    profile = patient["profile"]
    query = " ".join(profile.get(key) or "" for key in ("state", "diagnosis", "insurance"))
    documents = SchemeRetriever(sources=scheme_documents(rows)).invoke(query)
    return [{**indexed[doc.metadata["scheme_id"]], "excerpt": doc.page_content} for doc in documents]


@app.post("/patients/{patient_id}/funding-sources/{scheme_id}/guidance")
def funding_guidance(patient_id: int, scheme_id: int, db: DB, user: Staff):
    patient = funding_patient(db, patient_id)
    if patient["owner"] != user["id"]:
        fail(403, "Take ownership before requesting AI guidance.")
    rows = [row for row in active_sources(db, patient) if row["id"] == scheme_id]
    if not rows:
        fail(404, "Active funding source not found for this state.")
    document = scheme_documents(rows)[0]
    try:
        questions = scheme_guidance(document)
    except SarvamError as exc:
        fail(502, str(exc))
    audit(db, user["id"], "funding_guidance_generated", document.metadata, patient_id)
    return {"questions": questions, "source": document.metadata}


class ScenarioRequest(BaseModel):
    scheme_id: int
    scheme_revision: int
    case_revision: int
    covered_expenses: Annotated[str, StringConstraints(min_length=1, max_length=24)]
    verified_requirements: bool
    remaining_balance: Annotated[str, StringConstraints(max_length=24)] | None = None
    case_amount: Annotated[str, StringConstraints(max_length=24)] | None = None
    evidence: Text = ''
    save_assessment: bool = False


@app.post("/patients/{patient_id}/funding-scenario")
def estimate_funding(patient_id: int, data: ScenarioRequest, db: DB, user: Staff):
    patient = funding_patient(db, patient_id)
    if patient["revision"] != data.case_revision:
        fail(409, "The case changed. Refresh before calculating.")
    if not data.verified_requirements or patient["status"] != "verified":
        fail(422, "Verify the registration and check the scheme requirements first.")
    if not patient["cost_plan"]:
        fail(422, "Save a hospital cost estimate first.")
    row = next((item for item in active_sources(db, patient) if item["id"] == data.scheme_id), None)
    if not row or row["revision"] != data.scheme_revision:
        fail(409, "This scheme changed or is no longer active. Refresh the catalog.")
    from benefits import project_benefit, assessment_context
    from patient_journey import patient_facts
    from costs import amount
    if (data.case_amount is not None or data.remaining_balance is not None) and not data.evidence:
        fail(422, 'Record the package, benefit balance or sanction reference used to check these amounts.')
    plan = calculate_plan(**patient["cost_plan"])
    try:
        scenario = project_benefit(plan['total_cost'], row['details'], data.covered_expenses,
                                   data.remaining_balance, data.case_amount)
    except (ValueError, ArithmeticError):
        fail(422, "Enter a valid amount of eligible expenses within the hospital estimate.")
    if scenario is None:
        fail(422, 'No numeric benefit rule is published. Enter a case-specific amount checked with the funder.')
    if data.save_assessment:
        case_lock(db, user, patient_id, data.case_revision)
        record = {'scheme_revision': row['revision'], 'total': format(plan['total_cost'], '.2f'),
                  'context': assessment_context(patient_facts(db, patient)),
                  'eligible_expenses': format(amount(data.covered_expenses), '.2f'),
                  'remaining_balance': format(amount(data.remaining_balance), '.2f') if data.remaining_balance is not None else None,
                  'case_amount': format(amount(data.case_amount), '.2f') if data.case_amount is not None else None,
                  'evidence': data.evidence, 'reviewed_by': user['id'], 'reviewed_at': datetime.now(IST).isoformat()}
        db.execute('UPDATE patients SET funding_assessments=funding_assessments || %s WHERE id=%s',
                   (Jsonb({str(row['id']): record}), patient_id))
        audit(db, user['id'], 'scheme_benefit_inputs_verified', {'scheme': row['id'], 'assessment': record}, patient_id)
    return {**scenario, "scheme": row["name"],
            "confirmed_only_patient_share": plan["cash_conservative"]}


class TreatmentPrice(BaseModel):
    treatment: Name
    hospital: Name
    unit_price: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    source: Name
    checked: date
    expires: date


@app.get("/treatment-prices")
def treatment_prices(db: DB, user: Staff):
    return db.execute("SELECT * FROM treatment_prices ORDER BY checked DESC,id DESC").fetchall()


@app.post("/treatment-prices", status_code=201)
def add_treatment_price(data: TreatmentPrice, db: DB, user: Staff):
    from costs import amount
    today = datetime.now(IST).date()
    if not data.source.startswith("https://") or not urlsplit(data.source).hostname:
        fail(422, "Provide an HTTPS hospital tariff or quote source.")
    if data.checked > today or data.expires < today or data.expires < data.checked:
        fail(422, "Provide a current verification and validity date.")
    try:
        price = amount(data.unit_price)
        if not 0 < price < 10**10:
            raise ValueError
    except (ValueError, ArithmeticError):
        fail(422, "Enter a positive tariff in rupees.")
    row = db.execute(
        """INSERT INTO treatment_prices(treatment,hospital,unit_price,source,checked,expires)
           VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
        (data.treatment,data.hospital,price,data.source,data.checked,data.expires),
    ).fetchone()
    if not row:
        fail(409, "This tariff is already recorded.")
    audit(db,user["id"],"treatment_price_recorded",{**row,**data.model_dump(mode="json")})
    return row


@app.post("/patients/{patient_id}/planning-estimate")
def generate_planning_estimate(patient_id: int, data: Change, db: DB, user: Staff):
    from patient_journey import budget, patient_facts
    patient = case_lock(db, user, patient_id, data.revision)
    if not patient["profile"].get("consent"):
        fail(422, "Record patient consent before generating an AI planning estimate.")
    return budget(db, patient, patient_facts(db, patient), generate=True)


@app.get("/patients/{patient_id}/overview")
def automatic_overview(patient_id: int, db: DB, user: Staff):
    from patient_journey import patient_facts, budget, comparison_schemes
    patient = funding_patient(db, patient_id)
    documents = db.execute(
        "SELECT id,filename,ai_result,ai_error,ai_authorized FROM documents WHERE patient_id=%s ORDER BY id DESC",
        (patient_id,),
    ).fetchall()
    completed = [doc for doc in documents if (doc["ai_result"] or {}).get("fields")]
    facts = {key: {"value": value, "document": doc["id"]}
             for doc in reversed(completed) for key, value in doc["ai_result"]["fields"].items()
             if isinstance(value, str) and value}
    summaries = [{"document": doc["id"], "text": doc["ai_result"]["fields"].get("clinical_summary", "")}
                 for doc in completed if doc["ai_result"]["fields"].get("clinical_summary")]
    estimate = budget(db, patient, patient_facts(db, patient))
    cost = estimate["high"]
    candidates = []
    for row in comparison_schemes(db, patient, patient_facts(db, patient), estimate):
        details = row["details"]
        scenario = row['projection']
        candidates.append({"id": row["id"], "name": row["name"], "source": details["source"],
                           "guidance": details["guidance"], "status": "Potential support — verify eligibility",
                           "coverage_rate": details.get("coverage_rate"), "coverage_cap": details.get("coverage_cap"),
                           "coverage_kind": details.get('coverage_kind'), "illustration": scenario})
    return {"facts": facts, "summaries": summaries, "estimate": cost, "estimate_low": estimate["low"],
            "cost_basis": estimate["basis"], "cost_scope": estimate.get("scope", ""),
            "cost_assumptions": estimate["assumptions"], "tariff": {"source": estimate["source"]} if estimate.get("source") else None,
            "confirmed_support": estimate["confirmed"],
            "prepare_now": max(0, cost-estimate["confirmed"]) if cost is not None else None, "schemes": candidates,
            "processing": [{"document": doc["id"], "authorized": doc["ai_authorized"],
                            "status": (doc["ai_result"] or {}).get("status", "not_started"),
                            "error": doc["ai_error"]} for doc in documents]}


@app.post("/patients/{patient_id}/whatsapp/link")
def link_whatsapp(patient_id: int, data: ExtractionRequest, db: DB, user: Staff):
    patient=case_lock(db,user,patient_id,data.revision)
    if not data.consent_to_process or not patient["profile"].get("consent"):
        fail(422,"Confirm consent and verify that the current WhatsApp sender is this patient.")
    contact=db.execute("SELECT * FROM whatsapp_contacts WHERE phone=%s FOR UPDATE",(patient["phone"],)).fetchone()
    if not contact:
        fail(409,"Ask the patient to send a message to OncoAid first.")
    if contact["patient_id"] not in (None,patient_id):
        fail(409,"This WhatsApp contact is linked to another case.")
    db.execute("UPDATE whatsapp_contacts SET patient_id=%s,consent=true WHERE phone=%s",
               (patient_id,patient["phone"]))
    audit(db,user["id"],"whatsapp_linked",{},patient_id)
    return {"ok":True}


from whatsapp import router as whatsapp_router
app.include_router(whatsapp_router)



@app.post("/patients/{patient_id}/telegram/link")
def link_telegram(patient_id: int, data: ExtractionRequest, db: DB, user: Staff):
    patient=case_lock(db,user,patient_id,data.revision)
    if not data.consent_to_process or not patient["profile"].get("consent"):
        fail(422,"Confirm patient consent and verify the current Telegram sender.")
    contacts=db.execute("SELECT * FROM telegram_contacts WHERE phone=%s FOR UPDATE",(patient["phone"],)).fetchall()
    if len(contacts)!=1:
        fail(409,"Ask the patient to consent and share their own contact with the bot first.")
    contact=contacts[0]
    if contact["patient_id"] not in (None,patient_id) or not contact["consent"]:
        fail(409,"The Telegram contact needs a consent or identity check.")
    db.execute("UPDATE telegram_contacts SET patient_id=%s WHERE bot_id=%s AND chat_id=%s",
               (patient_id,contact["bot_id"],contact["chat_id"]))
    audit(db,user["id"],"telegram_linked",{},patient_id)
    return {"ok":True}


@app.get("/messaging-connection")
def messaging_connection(db: DB,user: Staff):
    bots=db.execute("SELECT username,connected_at FROM telegram_bots ORDER BY connected_at DESC LIMIT 1").fetchall()
    return {"telegram_configured":bool(os.getenv("TELEGRAM_BOT_TOKEN","").strip()),"bots":bots}
