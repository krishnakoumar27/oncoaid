"""Persistent patient intake, financial planning and application preparation.

Channel data remains unverified until a social worker accepts it. Selecting a
scheme creates a preparation record, never a submission or funding approval.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from psycopg.types.json import Jsonb

from costs import calculate_plan
from benefits import patient_projection
from intelligence import SarvamError, SchemeRetriever, funding_scenario, scheme_documents, treatment_planning_range

INTAKE = [
    ("name", "What is the patient's full name?"),
    ("state", "Which Indian state does the patient live in?"),
    ("hospital", "Which hospital is providing treatment?"),
    ("income", "What is the total annual household income in rupees? For example: 90000. Enter 0 if there is no income."),
    ("diagnosis", "What cancer diagnosis is written on the hospital record? You can upload that record instead."),
    ("stage", "What cancer stage has the hospital recorded? Reply unknown if it has not been confirmed."),
    ("planned_treatment", "What treatment has the doctor prescribed? Include the regimen or procedure name and number of cycles if known. You can upload the treatment plan instead."),
    ("insurance", "Does the patient have insurance or an existing health scheme? Give its name, or reply none or unknown."),
]
UNKNOWN = {"unknown", "not known", "not sure", "i don't know", "not confirmed"}
STATE_ALIASES = {"tn": "Tamil Nadu", "tamilnadu": "Tamil Nadu", "mh": "Maharashtra",
                 "ka": "Karnataka", "ap": "Andhra Pradesh", "tg": "Telangana", "ts": "Telangana",
                 "wb": "West Bengal", "dl": "Delhi", "up": "Uttar Pradesh", "mp": "Madhya Pradesh",
                 "kl": "Kerala", "gj": "Gujarat", "rj": "Rajasthan", "pb": "Punjab", "od": "Odisha"}


def money(value):
    return f"₹{Decimal(str(value)):,.0f}"


def set_journey(db, contact, journey):
    previous = contact.get("journey") or {}
    if previous == journey:
        return
    db.execute("UPDATE telegram_contacts SET journey=%s WHERE bot_id=%s AND chat_id=%s",
               (Jsonb(journey), contact["bot_id"], contact["chat_id"]))
    contact["journey"] = journey
    from api import audit
    audit(db, None, "patient_journey_changed", {"from": previous, "to": journey, "channel": "telegram"}, contact["patient_id"])


def patient_facts(db, patient, contact=None):
    facts = dict(patient["profile"])
    docs = db.execute("SELECT ai_result FROM documents WHERE patient_id=%s ORDER BY id", (patient["id"],)).fetchall()
    for doc in docs:
        result = doc["ai_result"] or {}
        if result.get("status") in {"completed", "partially_completed"}:
            facts.update({k: v for k, v in result.get("fields", {}).items() if isinstance(v, str) and v.strip()})
    if facts.get("annual_income_inr"):
        try:
            income = Decimal(facts["annual_income_inr"].replace(",", ""))
            if income.is_finite() and income == int(income) and 0 <= income <= 10**9:
                facts["income"] = int(income)
        except (ValueError, ArithmeticError):
            pass
    contacts = [contact] if contact else db.execute(
        "SELECT suggested_fields FROM telegram_contacts WHERE patient_id=%s ORDER BY bot_id,chat_id", (patient["id"],)).fetchall()
    for source in contacts:
        facts.update(source["suggested_fields"])
    # Staff-verified fields take priority over unreviewed extraction and messages.
    if patient["status"] == "verified":
        facts.update({k: v for k, v in patient["profile"].items() if v is not None and v != ""})
    return facts


def shortlist(db, facts):
    from api import validity
    state = str(facts.get("state", "")).casefold()
    rows = []
    for row in db.execute("SELECT * FROM schemes ORDER BY name").fetchall():
        details = row["details"]
        if validity(details) != "active" or details["state"].casefold() not in {"", state}:
            continue
        if row["name"] == "CMCHIS" and facts.get("income") is not None:
            try:
                if int(facts["income"]) >= 120000:
                    continue
            except (ValueError, TypeError):
                pass
        rows.append(row)
    ranked = SchemeRetriever(sources=scheme_documents(rows), limit=max(1, len(rows))).invoke(
        " ".join(str(facts.get(k) or "") for k in ("state", "diagnosis", "planned_treatment", "insurance")))
    indexed = {r["id"]: r for r in rows}
    return [indexed[d.metadata["scheme_id"]] for d in ranked]


def budget(db, patient, facts, generate=False):
    """Evidence first; opt-in generation only at explicit journey/worker actions."""
    from costs import amount
    from api import IST
    base = {"confirmed": 0, "low": None, "high": None, "basis": "Hospital estimate needed", "assumptions": []}
    if patient["cost_plan"]:
        plan = calculate_plan(**patient["cost_plan"])
        return {**base, "low": plan["total_cost"], "high": plan["total_cost"],
                "confirmed": plan["confirmed_coverage"], "basis": "Worker-reviewed treatment estimate"}
    try:
        quoted = amount(facts.get("estimate_total_inr", ""))
        if quoted > 0:
            return {**base, "low": quoted, "high": quoted,
                    "basis": "Unreviewed hospital quote: " + str(facts.get("estimate_scope") or "scope and date need worker verification")}
    except (ValueError, ArithmeticError):
        pass
    context = {k: str(facts.get(k) or "")[:400] for k in
               ("diagnosis", "stage", "planned_treatment", "treatment_units", "hospital", "state")}
    units = context["treatment_units"]
    if units.isascii() and units.isdecimal() and 0 < int(units) <= 1000:
        today = datetime.now(IST).date()
        tariff = db.execute("""SELECT * FROM treatment_prices WHERE lower(treatment)=lower(%s)
                            AND lower(hospital)=lower(%s) AND checked<=%s AND expires>=%s
                            ORDER BY checked DESC,id DESC LIMIT 1""",
                            (context["planned_treatment"], context["hospital"], today, today)).fetchone()
        if tariff:
            total = tariff["unit_price"] * int(units)
            return {**base, "low": total, "high": total,
                    "basis": "Current stored hospital tariff × prescribed units; treatment component only",
                    "source": tariff["source"]}
    fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    cached = patient.get("planning_estimate") or {}
    now = datetime.now(timezone.utc)
    if cached.get("fingerprint") == fingerprint and cached.get("expires", "") > now.isoformat():
        return {**base, **cached["budget"]}
    if not generate or not context["planned_treatment"] or context["planned_treatment"].casefold() in UNKNOWN:
        return base
    try:
        result = treatment_planning_range(context)
    except SarvamError:
        return {**base, "basis": "The estimate service is temporarily unavailable. Your worker can add a hospital quote."}
    estimate = {**base, "low": result.get("low_inr"), "high": result.get("high_inr"),
                "basis": "Sarvam AI planning range from model knowledge; not live pricing or a hospital quote",
                "scope": result.get("scope", ""), "assumptions": result.get("assumptions", []),
                "missing": result.get("missing", [])}
    record = {"fingerprint": fingerprint, "generated": now.isoformat(),
              "expires": (now + timedelta(days=7)).isoformat(), "budget": estimate}
    db.execute("UPDATE patients SET planning_estimate=%s WHERE id=%s", (Jsonb(record), patient["id"]))
    from api import audit
    audit(db, None, "planning_range_generated", record, patient["id"])
    return estimate


def comparison_schemes(db, patient, facts, estimate):
    rows = shortlist(db, facts)
    for row in rows:
        row['projection'] = patient_projection(patient, row, estimate['high'], facts) if estimate['high'] is not None else None
    # Compare one scheme at a time; a maximum ceiling does not imply approval likelihood.
    return sorted(rows, key=lambda r: (r['projection'] is not None,
                                      r['projection']['possible_cover'] if r['projection'] else 0), reverse=True)


def patient_scheme_name(name):
    return 'HMCPF cancer fund' if name.startswith("Health Minister's Cancer Patient Fund") else name


def benefit_lines(estimate, scenario, detailed=False, compact=False):
    if scenario is None:
        return ['Scheme support: needs worker review.']
    high = estimate['high']
    label = 'May pay up to' if scenario['upper_bound'] else 'May pay'
    lines = [f"{label}: {money(scenario['possible_cover'])} ({scenario['percent']}%)",
             f"You may need: {money(scenario['patient_share'])}" if compact else
             f"You may need: {money(high)} − {money(scenario['possible_cover'])} = {money(scenario['patient_share'])}"]
    if detailed:
        lines += scenario['assumptions']
        if scenario.get('notes'):
            lines += [scenario['notes']]
        lines += ['Benefit source: ' + scenario['source']]
    return lines


def choices_message(db, contact, patient, generate=True, page=0):
    facts = patient_facts(db, patient, contact)
    estimate = budget(db, patient, facts, generate=generate)
    rows = comparison_schemes(db, patient, facts, estimate)
    low, high = estimate['low'], estimate['high']
    lines = []
    if high is not None:
        lines += ['Estimated treatment: ' + (money(high) if low == high else f'{money(low)}–{money(high)}')]
    else:
        lines += ['We need a hospital estimate to calculate your remaining cost.']
    lines += ['Support options:']
    page = max(0, min(page, max(0, (len(rows)-1)//3)))
    for index, row in enumerate(rows[page*3:page*3+3], page*3+1):
        lines += [f"\n{index}. {patient_scheme_name(row['name'])}"]
        if high is not None:
            lines += benefit_lines(estimate, row['projection'], compact=True)
        elif row['details'].get('coverage_cap'):
            lines += [f"May pay up to: {money(row['details']['coverage_cap'])}, subject to review."]
        else:
            lines += ['Support amount: needs worker review.']
    if rows:
        lines += ['\nThese are estimates if eligible and approved. Actual support may be lower. Compare one scheme at a time.']
        if low != high:
            lines += ['The calculations use the higher treatment estimate.']
        lines += ['Reply with a scheme number to choose. /more NUMBER explains a scheme.']
        if len(rows)>3:
            lines += ['/next shows more options. /back goes back.']
    else:
        lines += ['Your worker can help find support. Send /worker.']
    journey = dict(contact.get('journey') or {})
    journey.update(phase='schemes', options=[{'id':r['id'],'revision':r['revision']} for r in rows],
                   scheme_page=page, prompt_field=None, view=None)
    set_journey(db, contact, journey)
    return '\n'.join(lines)


def selected_cost_message(db, contact, patient, application):
    from api import validity
    facts = patient_facts(db, patient, contact)
    estimate = budget(db, patient, facts, generate=True)
    if estimate['high'] is None:
        return 'Please send the hospital estimate. We will calculate what you may need to pay. /continue resumes your application.'
    scheme = db.execute('SELECT * FROM schemes WHERE id=%s', (application['scheme_id'],)).fetchone()
    scenario = patient_projection(patient, scheme, estimate['high'], facts) if scheme and validity(scheme['details'])=='active' else None
    low,high=estimate['low'],estimate['high']
    lines = [patient_scheme_name(application['snapshot']['name']),
             'Estimated treatment: ' + (money(high) if low==high else f'{money(low)}–{money(high)}')]
    lines += benefit_lines(estimate, scenario)
    lines += ['If eligible and approved. Your worker will confirm the final amount and payment dates.',
              '/continue resumes your application. /schemes compares support.']
    return '\n'.join(lines)


def scheme_details_message(db, contact, patient, number=None):
    from api import validity
    application=application_for_contact(db,contact)
    if number is not None:
        options=(contact.get('journey') or {}).get('options',[])
        if not 1<=number<=len(options):
            return 'Use /more followed by a scheme number from your list.'
        scheme_id=options[number-1]['id']
    elif application:
        scheme_id=application['scheme_id']
    else:
        return 'For scheme rules and sources, send /more followed by its number. Example: /more 1.'
    row=db.execute('SELECT * FROM schemes WHERE id=%s',(scheme_id,)).fetchone()
    if not row or validity(row['details'])!='active':
        return 'This scheme has changed. Send /schemes for current options.'
    facts=patient_facts(db,patient,contact)
    estimate=budget(db,patient,facts)
    lines=[row['name'],row['details']['guidance']]
    if estimate['high'] is not None:
        lines += benefit_lines(estimate,patient_projection(patient,row,estimate['high'],facts),detailed=True)
        lines += ['Treatment estimate basis: '+estimate['basis']]
    lines += ['Scheme source: '+row['details']['source'],'/back returns. /continue resumes your saved step.']
    set_journey(db,contact,{**contact.get('journey',{}),'view':'scheme_details'})
    return '\n'.join(lines)


def intake_message(db, contact, patient):
    facts = patient_facts(db, patient, contact)
    missing = [(k, q) for k, q in INTAKE if facts.get(k) is None or facts.get(k) == "" or facts.get(k) == "Registration pending"]
    if not missing:
        return choices_message(db, contact, patient)
    key, question = missing[0]
    journey = dict(contact.get("journey") or {})
    journey.update(phase="intake", prompt_field=key)
    set_journey(db, contact, journey)
    return question + "\nYou may send a document instead. /menu shows help."


def save_details(db, contact, fields):
    from api import audit
    if fields:
        if "state" in fields:
            fields["state"] = STATE_ALIASES.get(fields["state"].casefold().strip(), fields["state"])
        db.execute("UPDATE telegram_contacts SET suggested_fields=suggested_fields || %s WHERE bot_id=%s AND chat_id=%s",
                   (Jsonb(fields), contact["bot_id"], contact["chat_id"]))
        contact["suggested_fields"] = {**contact["suggested_fields"], **fields}
        db.execute("UPDATE patients SET revision=revision+1 WHERE id=%s", (contact["patient_id"],))
        audit(db, None, "patient_details_suggested", {"channel": "telegram", "fields": fields}, contact["patient_id"])


def direct_answer(text, contact, fields):
    """Fallback for a short answer to a known field, without depending on NLP."""
    key = (contact.get("journey") or {}).get("prompt_field")
    if not key or key in fields or not text or text.startswith("/"):
        return fields
    if text.casefold() in UNKNOWN:
        return {**fields, key: "unknown"} if key in {"stage", "planned_treatment", "insurance"} else fields
    if key == "income":
        value = re.sub(r"[₹,\s]", "", text)
        if value.isascii() and value.isdecimal() and 0 <= int(value) <= 10**9:
            return {**fields, key: int(value)}
        return fields
    # Don't misfile a message the NLP identified as a bundle of other fields.
    if not fields and len(text) <= 200 and "\n" not in text and ":" not in text and not text.isdecimal():
        return {key: text}
    return fields


def application_for_contact(db, contact):
    app_id = (contact.get("journey") or {}).get("application_id")
    return db.execute("SELECT * FROM applications WHERE id=%s AND patient_id=%s FOR UPDATE",
                      (app_id, contact["patient_id"])).fetchone() if app_id else None


def documents_message(db, contact, application, compact=False):
    from api import latest_documents, audit, EDITABLE_APPLICATIONS
    if application["status"] not in EDITABLE_APPLICATIONS:
        from whatsapp import status_reply
        return status_reply(db, contact["patient_id"]) + "\nYour worker can help with any next steps. Send /worker to request help."
    latest = latest_documents(db, application["id"])
    by_requirement = {d["requirement"]: d for d in latest}
    requirements = application["snapshot"]["requirements"]
    complete = all(r in by_requirement and by_requirement[r]["status"] in {"received", "verified"} for r in requirements)
    journey = dict(contact.get("journey") or {})
    if complete:
        if application["status"] in {"draft", "awaiting_documents"}:
            db.execute("UPDATE applications SET status='under_review' WHERE id=%s", (application["id"],))
            db.execute("INSERT INTO reminders(patient_id,application_id,audience,message,due) VALUES (%s,%s,'worker',%s,now())",
                       (contact["patient_id"], application["id"], "Application documents received. Verify registration, documents and preparation draft."))
            audit(db, None, "application_received_for_review", {"application": application["id"]}, contact["patient_id"])
        journey.update(phase="processing", requirement=None)
        set_journey(db, contact, journey)
        return 'Thank you. All files received. Our team is reviewing your application. We will contact you if needed. /status checks progress.'
    if compact:
        received=sum(r in by_requirement and by_requirement[r]['status'] in {'received','verified'} for r in requirements)
        index, requirement=next((i,r) for i,r in enumerate(requirements,1) if r not in by_requirement or by_requirement[r]['status']=='rejected')
        set_journey(db,contact,{**journey,'phase':'documents','requirement':None})
        return f"File received. {received} of {len(requirements)} files received.\nNext: {index}. {requirement}.\nReply {index} to upload it. /documents shows all files."
    lines = [f"Documents for {application['snapshot']['name']}:"]
    for index, requirement in enumerate(requirements, 1):
        doc = by_requirement.get(requirement)
        state = "Verified" if doc and doc["status"] == "verified" else "Received" if doc and doc["status"] == "received" else "Missing"
        lines += [f"{index}. {requirement} — {state}"]
        if doc and doc["status"] == "rejected":
            lines += ["Correction needed: " + doc["reason"]]
    journey.update(phase="documents")
    set_journey(db, contact, journey)
    return "\n".join(lines) + "\nReply with a document number, then send a PDF or clear photo (up to 5 MB). /back goes back."


def select_scheme(db, contact, patient, number):
    from api import audit, validity
    options = (contact.get("journey") or {}).get("options", [])
    if not 1 <= number <= len(options):
        return "Please choose a number from the scheme list, or send /schemes to refresh it."
    choice = options[number-1]
    row = db.execute("SELECT * FROM schemes WHERE id=%s FOR SHARE", (choice["id"],)).fetchone()
    if (not row or row["revision"] != choice["revision"] or validity(row["details"]) != "active"
            or row["id"] not in {s["id"] for s in shortlist(db, patient_facts(db, patient, contact))}):
        return "This option has changed. Let's check the current options.\n\n" + choices_message(db, contact, patient)
    existing = db.execute("SELECT * FROM applications WHERE patient_id=%s AND scheme_id=%s AND status NOT IN ('internally_rejected','agency_rejected') ORDER BY id DESC LIMIT 1 FOR UPDATE",
                          (patient["id"], row["id"])).fetchone()
    if existing and existing["status"] not in {"draft", "awaiting_documents", "under_review"}:
        return "Your application for this scheme is already with the team or agency. Send /status for progress."
    if existing:
        application = existing
    else:
        facts = patient_facts(db, patient, contact)
        snapshot = {**row["details"], "name": row["name"], "revision": row["revision"]}
        estimate = budget(db, patient, facts)
        financial = "Hospital estimate and case-specific support amount still need verification."
        if estimate["high"] is not None:
            financial = (f"Planning estimate: {money(estimate['low'])}–{money(estimate['high'])}. "
                         f"Basis: {estimate['basis']}. Confirmed support: {money(estimate['confirmed'])}. "
                         f"Conservative amount to plan for: {money(max(0, estimate['high']-estimate['confirmed']))}.")
            scenario = patient_projection(patient, row, estimate['high'], facts)
            financial += "\n" + "\n".join(benefit_lines(estimate, scenario, detailed=True))
        draft = (f"APPLICATION PREPARATION — STAFF REVIEW REQUIRED\nScheme: {row['name']}\n"
                 f"Patient: {facts.get('name', 'To verify')}\nRegistered phone: {patient['phone']}\n"
                 f"State: {facts.get('state', 'To verify')}\nHospital: {facts.get('hospital', 'To verify')}\n"
                 f"Annual household income (INR): {facts.get('income', 'To verify')}\n"
                 f"Recorded diagnosis: {facts.get('diagnosis', 'To verify')}\nStage: {facts.get('stage', 'To verify')}\n"
                 f"Prescribed treatment: {facts.get('planned_treatment', 'To verify')}\n"
                 f"Insurance: {facts.get('insurance', 'To verify')}\n\nFinancial planning:\n{financial}\n"
                 f"Scheme source: {snapshot['source']}\n"
                 "\nPlease assess this request for financial assistance under the scheme's current rules. "
                 "Patient-supplied and extracted information, eligibility, hospital estimate and documents require verification. "
                 "No assistance amount or clinical outcome is assured. This cover letter is not an official completed agency form.\n"
                 "\nChecklist:\n" + "\n".join('- ' + r for r in snapshot["requirements"]))
        application = db.execute("INSERT INTO applications(patient_id,scheme_id,snapshot,status,draft) VALUES (%s,%s,%s,'awaiting_documents',%s) RETURNING *",
                                 (patient["id"], row["id"], Jsonb(snapshot), draft)).fetchone()
        db.execute("UPDATE patients SET revision=revision+1 WHERE id=%s", (patient["id"],))
        audit(db, None, "patient_scheme_selected", {"scheme": row["id"], "scheme_revision": row["revision"], "application": application["id"]}, patient["id"])
    set_journey(db, contact, {**contact["journey"], "phase": "documents", "application_id": application["id"], "requirement": None})
    return "Scheme selected. Please send these files:\n\n" + documents_message(db, contact, application)


def numbered_document(db, contact, application, number):
    from api import EDITABLE_APPLICATIONS, latest_documents
    if application["status"] not in EDITABLE_APPLICATIONS:
        return "This application is already being handled by the team or agency. Send /status or /worker for help."
    requirements = application["snapshot"]["requirements"]
    if not 1 <= number <= len(requirements):
        return "Please choose a document number from this checklist.\n\n" + documents_message(db, contact, application)
    requirement = requirements[number-1]
    latest = next((d for d in latest_documents(db, application["id"]) if d["requirement"] == requirement), None)
    if latest and latest["status"] == "verified":
        return "That document has already been verified. Please choose an outstanding item.\n\n" + documents_message(db, contact, application)
    set_journey(db, contact, {**contact["journey"], "phase": "documents", "requirement": requirement})
    return f"Please upload {requirement} now as a PDF or clear photo (up to 5 MB). Send /documents to change the checklist item."
