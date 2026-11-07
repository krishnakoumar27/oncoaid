"""Streamlit screens for OncoAid staff."""
import base64
import os
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
import streamlit as st

from costs import amount
from benefits import coverage_kind

API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
IST = ZoneInfo("Asia/Kolkata")


def call(path, method="GET", body=None, binary=False):
    headers = {}
    if st.session_state.get("token"):
        headers["Authorization"] = "Bearer " + st.session_state.token
    try:
        response = requests.request(method, API + path, json=body, headers=headers, timeout=55)
        if response.ok:
            return response.content if binary else response.json()
        detail = response.json().get("detail", "Request failed.")
        if isinstance(detail, list):
            detail = " · ".join(
                f"{'.'.join(map(str, item['loc'][1:]))}: {item['msg']}" for item in detail
            )
        st.error(detail)
        if response.status_code == 401 and st.session_state.get("token"):
            st.session_state.clear()
            st.rerun()
    except (requests.RequestException, ValueError):
        st.error("Could not reach the OncoAid service. Check that the API and database are running.")
    return None


def write(path, body, message="Saved."):
    if call(path, "POST", body) is not None:
        st.session_state.notice = message
        st.rerun()


def label(value):
    return value.replace("_", " ").capitalize()


def rupees(value):
    number = Decimal(str(value))
    whole, fraction = f"{number:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return "₹" + ",".join(groups + [tail]) + (f".{fraction}" if fraction != "00" else "")


def local_date(value):
    return date.fromisoformat(value) if value else None


def login():
    access = call('/access-options')
    if access is None:
        return
    if access['one_click']:
        st.write('Choose an account')
        st.caption('Shared workspace: anyone with access can select Admin or Worker. Use fictional patient information.')
        columns = st.columns(2)
        for column, account in zip(columns, access['accounts']):
            if column.button(account['label'], use_container_width=True, disabled=not account['available']):
                result = call('/access/select', 'POST', {'role': account['role']})
                if result:
                    st.session_state.clear()
                    st.session_state.update(result)
                    st.rerun()
        return
    mode = st.radio("Account", ["Sign in", "Request worker access"], horizontal=True)
    with st.form("account"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password", help="Use at least 12 characters.")
        if st.form_submit_button(mode):
            path = "/login" if mode == "Sign in" else "/signup"
            result = call(path, "POST", {"email": email, "password": password})
            if result and mode == "Sign in":
                st.session_state.update(result)
                st.rerun()
            if result and mode != "Sign in":
                st.success("Access requested. Ask an administrator to approve the account.")


def profile_form(case=None):
    profile = dict(case["profile"]) if case else {}
    selected_document = None
    if case and case.get("whatsapp_contact") and case["whatsapp_contact"]["suggested_fields"]:
        if st.checkbox("Prefill details suggested through WhatsApp", key=f"wa-prefill-{case['id']}-{case['revision']}"):
            suggestions = case["whatsapp_contact"]["suggested_fields"]
            profile.update({k:v for k,v in suggestions.items() if k != "phone"})
            st.caption("Review these patient-provided details before saving.")
    if case and case.get("telegram_contact") and case["telegram_contact"]["suggested_fields"]:
        if st.checkbox("Prefill details provided through Telegram",key=f"tg-prefill-{case['id']}-{case['revision']}"):
            profile.update(case["telegram_contact"]["suggested_fields"])
            st.caption("Check the patient-provided details before saving.")
    if case:
        extracted = {doc["id"]: doc for doc in all_documents(case) if (doc.get("ai_result") or {}).get("fields")}
        if extracted:
            selected_document = st.selectbox(
                "Prefill from a document you have reviewed", [None, *extracted],
                format_func=lambda key: extracted[key]["filename"] if key else "Keep saved details",
                key=f"profile-source-{case['id']}-{case['revision']}",
            )
        if selected_document:
            values = extracted[selected_document]["ai_result"]["fields"]
            for key in ("name", "state", "district", "address", "hospital", "diagnosis", "stage", "insurance", "planned_treatment", "treatment_units"):
                if values.get(key):
                    profile[key] = values[key]
            try:
                birth_date = date.fromisoformat(values["birth_date"])
                if date(1900, 1, 1) <= birth_date <= datetime.now(IST).date():
                    profile["birth_date"] = birth_date.isoformat()
            except (KeyError, ValueError):
                pass
            income = values.get("annual_income_inr", "")
            if income.isascii() and income.isdecimal() and 0 <= int(income) <= 10**9:
                profile["income"] = int(income)
            st.caption("Check the suggested values against the document before saving.")
    prefix = f"profile-{case['id']}-{case['revision']}-{selected_document}" if case else "new-profile"
    with st.form(prefix):
        name = st.text_input("Patient name", profile.get("name", ""))
        phone = st.text_input("Registered phone, with country code", case["phone"] if case else "",
                              disabled=bool(case), placeholder="+91…")
        birth_date = st.date_input("Date of birth", local_date(profile.get("birth_date")),
                                   min_value=date(1900, 1, 1), max_value=datetime.now(IST).date())
        state = st.text_input("State", profile.get("state", ""))
        district = st.text_input("District", profile.get("district", ""))
        address = st.text_input("Address, if needed", profile.get("address", ""))
        hospital = st.text_input("Treating hospital", profile.get("hospital", ""))
        diagnosis = st.text_input("Diagnosis from hospital records", profile.get("diagnosis", ""))
        stage = st.text_input("Stage, if recorded", profile.get("stage", ""))
        planned_treatment = st.text_input("Prescribed treatment or regimen", profile.get("planned_treatment", ""))
        treatment_units = st.text_input("Prescribed number of cycles or units, if recorded", profile.get("treatment_units", ""))
        income = st.number_input("Annual household income (₹)", min_value=0, step=1, value=profile.get("income"))
        insurance = st.text_input("Insurance status", profile.get("insurance", ""), placeholder="None / insured / unknown")
        consent = st.checkbox("Patient consent recorded", value=profile.get("consent", False))
        if st.form_submit_button("Save profile" if case else "Create registration"):
            data = {"name": name, "phone": phone, "state": state, "district": district, "address": address,
                    "birth_date": birth_date.isoformat() if birth_date else None, "diagnosis": diagnosis,
                    "stage": stage, "planned_treatment": planned_treatment, "treatment_units": treatment_units,
                    "hospital": hospital, "income": income, "insurance": insurance,
                    "consent": consent}
            if case:
                write(f"/patients/{case['id']}/profile", {"revision": case["revision"], "profile": data})
            else:
                write("/patients", data, "Registration created.")


def timeline(patient_id=None):
    path = "/audit" if patient_id is None else f"/audit?patient_id={patient_id}"
    events = call(path) or []
    if not events:
        st.caption("No activity recorded.")
    for event in events:
        when = datetime.fromisoformat(event["created_at"]).astimezone(IST).strftime("%d %b %Y, %H:%M IST")
        with st.expander(f"{when} · {label(event['action'])} · {event['actor'] or 'System'}"):
            st.json(event["detail"])


def schemes_page():
    catalog = call("/schemes") or []
    validity = st.selectbox("Show", ["All", "Active", "Unverified", "Upcoming", "Expired", "Archived"])
    visible = [s for s in catalog if validity == "All" or s["validity"] == validity.lower()]
    st.dataframe([{"Scheme": s["name"], "State": s["details"]["state"] or "—",
                   "Status": label(s["validity"]), "Closes": s["details"]["closes"] or "—",
                   "Version": s["revision"]} for s in visible], hide_index=True)
    choices = {s["id"]: s for s in catalog}
    choice = st.selectbox("Manage scheme", [None, *choices],
                          format_func=lambda key: "Add new scheme" if key is None else choices[key]["name"])
    scheme = choices.get(choice)
    details = scheme["details"] if scheme else {}
    with st.form(f"scheme-{choice}-{scheme['revision'] if scheme else 0}"):
        name = st.text_input("Scheme name", scheme["name"] if scheme else "", disabled=scheme is not None)
        kinds = ["central", "state", "organization"]
        kind = st.selectbox("Category", kinds, index=kinds.index(details.get("kind", "organization")))
        state = st.text_input("State restriction, if applicable", details.get("state", ""))
        source = st.text_input("Official source URL", details.get("source", ""))
        guidance = st.text_area("Eligibility and coverage notes", details.get("guidance", ""))
        checklist = st.text_area("Required documents, one per line", "\n".join(details.get("requirements", [])))
        opens = st.date_input("Opens (optional)", local_date(details.get("opens")))
        closes = st.date_input("Closes (optional, inclusive)", local_date(details.get("closes")))
        published = st.checkbox("Verified and available for new applications", details.get("published", False))
        archived = st.checkbox("Archive scheme", details.get("archived", False))
        kinds = ['case_by_case', 'percentage', 'package_limit', 'grant_limit']
        benefit_kind = st.selectbox('Benefit calculation', kinds, index=kinds.index(coverage_kind(details)),
                                    format_func=lambda x: {'case_by_case':'Case-specific amount', 'percentage':'Percentage reimbursement', 'package_limit':'Cashless package / annual benefit limit', 'grant_limit':'Discretionary grant ceiling'}[x])
        rate = st.number_input("Coverage rate (only for percentage benefits)", min_value=0.01, max_value=100.0,
                               value=details.get("coverage_rate"))
        cap = st.number_input("Published benefit ceiling (₹)", min_value=1, max_value=10**9,
                              value=details.get("coverage_cap"))
        benefit_source = st.text_input('Official benefit rule source URL', details.get('coverage_source') or details.get('source', ''))
        benefit_checked = st.date_input('Benefit rule verified on', local_date(details.get('coverage_checked')))
        benefit_notes = st.text_area('Benefit scope and conditions', details.get('coverage_notes', ''))
        if st.form_submit_button("Save scheme"):
            body = {"name": name, "kind": kind, "state": state, "source": source, "guidance": guidance,
                    "requirements": [x.strip() for x in checklist.splitlines() if x.strip()],
                    "opens": opens.isoformat() if opens else None, "closes": closes.isoformat() if closes else None,
                    "published": published, "archived": archived, "revision": scheme["revision"] if scheme else None,
                    "coverage_kind": benefit_kind, "coverage_rate": rate if benefit_kind == 'percentage' else None,
                    "coverage_cap": cap if benefit_kind != 'case_by_case' else None,
                    "coverage_source": benefit_source, "coverage_checked": benefit_checked.isoformat() if benefit_checked else None,
                    "coverage_notes": benefit_notes}
            write(f"/schemes?scheme_id={choice}" if scheme else "/schemes", body)
    with st.expander("Catalog and account audit"):
        timeline()


def cost_page(case, writable):
    plan = case['calculated_plan']
    overview = call(f"/patients/{case['id']}/overview")
    if overview:
        scheme_comparison(overview)
        options = {row['id']:row for row in overview['schemes']}
        if options and overview['estimate'] is not None:
            selected = st.selectbox('See remaining cost with this scheme', list(options),
                                    format_func=lambda key:options[key]['name'], key=f"cost-scheme-{case['id']}")
            projection=options[selected]['illustration']
            columns=st.columns(3)
            columns[0].metric('Estimated treatment',rupees(overview['estimate']))
            columns[1].metric('Scheme may pay',rupees(projection['possible_cover']) if projection else 'Needs review')
            columns[2].metric('You may need after this scheme',rupees(projection['patient_share']) if projection else 'Needs review')
            st.caption('If eligible and approved. This uses the upper treatment estimate. Actual package costs and available benefit need verification.')
            if projection:
                summary=(f"Estimated treatment: {rupees(projection['total'])}. "
                         f"{options[selected]['name']} may cover {rupees(projection['possible_cover'])} ({projection['percent']}%). "
                         f"You may need {rupees(projection['patient_share'])} after this support, if eligible and approved. "
                         'Your worker will confirm the amount and payment dates with you.')
                st.write(summary)
                st.download_button('Download patient summary',summary,file_name='oncoaid-cost-summary.txt')
    if plan:
        with st.expander('Recorded funding allocations by treatment phase'):
            st.caption('Funding approval: '+(rupees(plan['confirmed_coverage'])+' confirmed' if amount(plan['confirmed_coverage'])>0 else 'pending'))
            st.dataframe([{'Phase':row['phase'],'Estimate':rupees(row['cost']),
                           'Confirmed allocation':rupees(row['confirmed']),'Pending allocation':rupees(row['potential']),
                           'Unfunded before further approvals':rupees(row['cash_conservative'])} for row in plan['phases']],hide_index=True)
    if not writable:
        return
    inputs = case["cost_plan"] or {"phases": [{"name": "Treatment", "amount": ""}], "coverage": []}
    phases = [{"name": row["name"], "amount": str(row["amount"])} for row in inputs["phases"]]
    allocations = [{**row, "amount": str(row["amount"])} for row in inputs["coverage"]]
    with st.expander("Enter or update hospital estimate", expanded=not plan):
        quotes = {doc["id"]: doc for doc in all_documents(case)
                  if (doc.get("ai_result") or {}).get("fields", {}).get("estimate_total_inr")}
        quote = st.selectbox(
            "Prefill a reviewed hospital quote", [None, *quotes],
            format_func=lambda key: quotes[key]["filename"] if key else "Use saved phases or enter amounts",
            key=f"cost-source-{case['id']}-{case['revision']}",
        ) if quotes else None
        if quote:
            values = quotes[quote]["ai_result"]["fields"]
            try:
                total = amount(values["estimate_total_inr"])
                lines = values.get("estimated_phases", [])
                if not lines or sum(amount(row["amount"]) for row in lines) != total:
                    lines = [{"name": "Hospital estimate", "amount": str(total)}]
                phases = lines
                allocations = []
                st.caption("Check what this quote covers before saving. Enter any missing phases and re-enter "
                           "applicable funding allocations.")
                if values.get("estimate_scope"):
                    st.write(values["estimate_scope"])
            except (ValueError, ArithmeticError):
                st.warning("The extracted amount needs a manual correction.")
        st.caption("Use hospital estimates. Allocate each covered expense once across funding sources.")
        key = f"{case['id']}-{case['revision']}-{quote}"
        with st.form(f"cost-{key}"):
            st.write("Treatment phases")
            phase_rows = st.data_editor(phases, num_rows="dynamic", key=f"phases-{key}",
                                        column_config={"name": "Phase", "amount": "Estimate in rupees"})
            st.write("Funding allocations")
            allocation_rows = st.data_editor(
                allocations or [{"phase": "", "source": "", "status": "potential", "amount": ""}],
                num_rows="dynamic", key=f"coverage-{key}",
                column_config={"phase": "Phase", "source": "Scheme or insurer", "amount": "Amount in rupees",
                               "status": st.column_config.SelectboxColumn(options=["confirmed", "potential"])},
            )
            if st.form_submit_button("Calculate and save"):
                allocation_rows = [r for r in allocation_rows if r.get("phase") or r.get("source") or r.get("amount")]
                write(f"/patients/{case['id']}/cost-plan",
                      {"revision": case["revision"], "phases": phase_rows, "coverage": allocation_rows})


def application_page(case, writable):
    path = f"/patients/{case['id']}/applications"
    revision = case["revision"]
    if writable and case["status"] == "verified":
        catalog = call("/schemes") or []
        state = case["profile"]["state"].casefold()
        options = {s["id"]: s for s in catalog if s["validity"] == "active"
                   and s["details"]["state"].casefold() in {"", state}}
        if options:
            with st.form(f"new-app-{case['id']}-{revision}"):
                choice = st.selectbox("Available schemes", list(options), format_func=lambda k: options[k]["name"])
                if st.form_submit_button("Start application"):
                    write(path, {"revision": revision, "scheme_id": choice})
            st.caption("Check the scheme's eligibility notes and source before applying.")
            st.write(options[choice]["details"]["guidance"])
            st.write(options[choice]["details"]["source"])
        else:
            st.caption("Activate a sourced scheme in the catalog to start an application.")
    for record in case["applications"]:
        app_path = f"{path}/{record['id']}"
        key = f"a{record['id']}-r{revision}"
        snapshot = record["snapshot"]
        with st.expander(f"{snapshot['name']} · {label(record['status'])}", expanded=True):
            st.caption(f"Scheme version {snapshot['revision']} · {snapshot['source']}")
            st.write(snapshot["guidance"])
            if record["reason"]:
                st.write(record["reason"])
            if record["reference"]:
                st.write("Agency reference", record["reference"])
            docs = record["documents"]
            editable = writable and record["status"] in {"draft", "awaiting_documents", "under_review"}
            for requirement in snapshot["requirements"]:
                versions = [d for d in docs if d["requirement"] == requirement]
                st.write(f"**{requirement}** · {label(versions[0]['status']) if versions else 'Missing'}")
                for index, doc in enumerate(versions):
                    col1, col2 = st.columns([3, 2])
                    col1.write(f"{doc['filename']} · {label(doc['status'])}" + (" · earlier version" if index else ""))
                    if doc["reason"]:
                        col1.caption(doc["reason"])
                    if st.checkbox("Show extracted details", key=f"show-extraction-{doc['id']}"):
                        extraction_panel(case, doc, writable)
                    if col2.button("Load file for download", key=f"load-{doc['id']}"):
                        content = call(f"/documents/{doc['id']}", binary=True)
                        if content is not None:
                            col2.download_button("Download", content, file_name=doc["filename"],
                                                 key=f"dl-{doc['id']}")
                if editable and versions and versions[0]["status"] == "received":
                    doc_id = versions[0]["id"]
                    with st.form(f"review-{doc_id}-{key}"):
                        decision = st.selectbox("Document decision", ["verified", "rejected"], format_func=label)
                        reason = st.text_input("Correction required if rejected")
                        if st.form_submit_button("Save document decision"):
                            write(f"{app_path}/documents/{doc_id}", {"revision": revision,
                                                                    "status": decision, "reason": reason})
            if editable:
                with st.form(f"upload-{key}"):
                    requirement = st.selectbox("Checklist item", snapshot["requirements"])
                    file = st.file_uploader("Upload new version (up to 5 MB)", type=["pdf", "png", "jpg", "jpeg"])
                    auto_extract = st.checkbox("Automatically read this document with Sarvam (patient authorization recorded)",
                                           value=case["profile"].get("consent",False))
                    if st.form_submit_button("Save document"):
                        if file is None or file.size > 5 * 1024 * 1024:
                            st.error("Choose a PDF or image up to 5 MB.")
                        else:
                            write(f"{app_path}/documents", {"revision": revision, "requirement": requirement,
                                  "filename": file.name, "content": base64.b64encode(file.getvalue()).decode(),
                                  "auto_extract": auto_extract})
            profile = case["profile"]
            template = (f"Preparation draft for {snapshot['name']}\n\n"
                        f"Please assess financial support for {profile.get('name', 'To verify')}, receiving care at "
                        f"{profile.get('hospital', 'To verify')}. The documented diagnosis is {profile.get('diagnosis', 'To verify')}.\n\n"
                        f"Required documents:\n" + "\n".join(f"- {x}" for x in snapshot["requirements"]))
            if editable:
                with st.form(f"draft-{key}"):
                    draft = st.text_area("Application cover letter draft", record["draft"] or template, height=180)
                    if st.form_submit_button("Save draft"):
                        write(f"{app_path}/draft", {"revision": revision, "text": draft})
            if record["draft"]:
                st.download_button("Download saved draft", record["draft"], key=f"draft-download-{key}",
                                   file_name=f"oncoaid-application-{record['id']}.txt")
            if writable and record["next_statuses"]:
                with st.form(f"status-{key}"):
                    status = st.selectbox("Move to", record["next_statuses"], format_func=label)
                    reason = st.text_input("Patient-facing reason (required for rejection)")
                    reference = st.text_input("Agency reference", value=record["reference"])
                    if st.form_submit_button("Update status"):
                        write(f"{app_path}/status", {"revision": revision, "status": status,
                                                    "reason": reason, "reference": reference})


def reminders_page(case, writable):
    path = f"/patients/{case['id']}/reminders"
    if case.get("communications"):
        with st.expander("WhatsApp delivery history"):
            history=[{**row,"status":("not_sent" if row["status"]=="preview" else row["status"])}
                     for row in case["communications"]]
            st.dataframe(history,hide_index=True)
    for reminder in case["reminders"]:
        due = datetime.fromisoformat(reminder["due"]).astimezone(IST).strftime("%d %b %Y, %H:%M IST")
        status = ("Waiting for patient messaging connection" if not case.get("telegram_contact") else "Scheduled") if reminder["audience"] == "patient" and reminder["status"] == "pending" else label(reminder["status"])
        st.write(f"{due} · {label(reminder['audience'])} · {status} · {reminder['message']}")
        if reminder["repeat_days"]:
            st.caption(f"Repeat every {reminder['repeat_days']} days")
        if writable and reminder["status"] in {"pending","queued"} and st.button("Close reminder", key=f"close-{reminder['id']}-{case['revision']}"):
            write(f"{path}/{reminder['id']}/complete", {"revision": case["revision"]})
    if not writable:
        return
    apps = {a["id"]: a for a in case["applications"]
            if a["status"] in {"draft", "awaiting_documents", "under_review", "ready_for_submission"}}
    application_id = st.selectbox("Link reminder to application", [None, *apps],
                                  format_func=lambda k: apps[k]["snapshot"]["name"] if k else "Case follow-up",
                                  key=f"reminder-app-{case['id']}")
    requirements = apps[application_id]["snapshot"]["requirements"] if application_id else []
    with st.form(f"reminder-{case['id']}-{case['revision']}-{application_id}"):
        requirement = st.selectbox("Document to follow up", [None, *requirements],
                                   format_func=lambda k: k or "General follow-up")
        audience = st.selectbox("For", ["worker", "patient"], format_func=label)
        message = st.text_input("Reminder message")
        day = st.date_input("Due date")
        hour = st.time_input("Due time (IST)", time(10, 0))
        interval = st.number_input("Repeat every N days (0 for once)", min_value=0, max_value=365, step=1)
        if st.form_submit_button("Save reminder"):
            write(path, {"revision": case["revision"], "application_id": application_id,
                         "requirement": requirement, "audience": audience, "message": message,
                         "due": datetime.combine(day, hour, tzinfo=IST).isoformat(), "repeat_days": interval or None})


def clear_patient_session(message):
    # Remove cached patient fields, previews and widget values from this staff session.
    identity = {key: st.session_state[key] for key in ('token', 'id', 'email', 'role') if key in st.session_state}
    st.session_state.clear()
    st.session_state.update(identity)
    st.session_state.notice = message
    st.rerun()


def permanent_delete_panel(case):
    if st.session_state.role != 'admin':
        return
    with st.expander('Permanently delete patient'):
        st.write(f"Delete {case['profile'].get('name', 'this patient')} · {case['phone']}")
        st.caption('Permanently removes this patient, uploaded documents, applications, reminders, patient history and linked Telegram/WhatsApp conversation state. This cannot be undone. The same account can then register again.')
        with st.form(f"permanent-delete-{case['id']}-{case['revision']}"):
            confirmation = st.text_input('Type DELETE to confirm', key=f"delete-confirm-{case['id']}")
            if st.form_submit_button('Permanently delete this patient'):
                result = call(f"/patients/{case['id']}/permanent-delete", 'POST',
                              {'revision': case['revision'], 'confirmation': confirmation.strip()})
                if result is not None:
                    clear_patient_session('Patient permanently deleted. Send /start to the bot to register again.')


def data_management_page():
    st.subheader('Patient data management')
    st.write('Clear patient records to start a new registration and application journey from the beginning.')
    counts = call('/admin/patient-data')
    if counts is None:
        return
    st.dataframe([{'Record type': label(key), 'Count': value} for key, value in counts.items()], hide_index=True)
    st.warning('This permanently deletes all active and archived patients, documents, applications, reminders, patient audit history and Telegram/WhatsApp conversation state. It cannot be undone.')
    st.caption('Staff accounts and logins, schemes, treatment tariffs, bot connection and API settings are kept. A maintenance entry records who performed the reset and the deleted record counts without patient identifiers.')
    with st.form('reset-patient-data'):
        confirmation = st.text_input('Type RESET to clear all patient data')
        if st.form_submit_button('Permanently clear all patient data'):
            result = call('/admin/patient-data/reset', 'POST', {'confirmation': confirmation.strip()})
            if result is not None:
                clear_patient_session('All patient data has been cleared. Send /start to the Telegram bot to begin again.')


def patients_page():
    with st.expander("New patient registration"):
        profile_form()
    archived = st.toggle("View archived patients", value=False)
    rows = call("/patients?archived=" + str(archived).lower()) or []
    if not rows:
        st.info("No archived cases." if archived else "No active cases. New patient registrations will appear here.")
        return
    search = st.text_input("Find by name or phone").strip().casefold()
    choices = {r["id"]: r for r in rows if search in f"{r['name']} {r['phone']}".casefold()}
    if not choices:
        st.info("No matching cases.")
        return
    patient_id = st.selectbox("Patient case", list(choices),
                              format_func=lambda k: f"{choices[k]['name']} · {choices[k]['phone']} · {label(choices[k]['status'])}")
    case = call(f"/patients/{patient_id}")
    if case is None:
        return
    writable = case["owner"] == st.session_state.id and not case["archived_at"]
    st.caption(f"Registration: {label(case['status'])} · Assigned to {case['assigned_to'] or 'nobody'}")
    if case["registration_reason"]:
        st.info("Registration note: " + case["registration_reason"])
    permanent_delete_panel(case)
    if case["archived_at"]:
        st.info("Archived case. Documents, applications and history have been retained. Restore the case to resume work; stopped reminders remain stopped.")
        if case["owner"] == st.session_state.id or st.session_state.role == "admin":
            with st.form(f"restore-{patient_id}-{case['revision']}"):
                reason = st.text_input("Reason for restoring this patient")
                if st.form_submit_button("Restore patient"):
                    write(f"/patients/{patient_id}/archive", {"revision": case["revision"], "archived": False, "reason": reason}, "Patient restored.")
        timeline(patient_id)
        return
    if not writable:
        with st.form(f"claim-{patient_id}-{case['revision']}"):
            reason = st.text_input("Reason for taking over the case")
            if st.form_submit_button("Assign to me"):
                write(f"/patients/{patient_id}/claim", {"revision": case["revision"], "reason": reason})
    if writable or st.session_state.role == "admin":
        with st.expander("Patient actions"):
            if writable:
                with st.form(f"reject-{patient_id}-{case['revision']}"):
                    reason = st.text_input("Reason for rejecting registration", help="The patient can read this reason in Telegram.")
                    if st.form_submit_button("Reject patient registration", disabled=case["status"] == "rejected"):
                        write(f"/patients/{patient_id}/registration", {"revision": case["revision"], "status": "rejected", "reason": reason}, "Registration rejected with reason recorded.")
            with st.form(f"archive-{patient_id}-{case['revision']}"):
                st.caption("Remove this patient from active work. The full case and audit history are retained, and reminders are stopped.")
                reason = st.text_input("Reason for removal")
                if st.form_submit_button("Remove patient (archive)"):
                    write(f"/patients/{patient_id}/archive", {"revision": case["revision"], "archived": True, "reason": reason}, "Patient archived.")
    if case.get("telegram_contact"):
        journey = case["telegram_contact"].get("journey") or {}
        phase_labels = {"intake": "Collecting patient details", "schemes": "Patient choosing a support pathway", "documents": "Collecting application documents", "processing": "Documents received · worker review pending"}
        st.caption("Patient progress: " + phase_labels.get(journey.get("phase"), "Registration started"))
    sections = ["Care overview", "Registration", "Documents", "Applications", "Cost roadmap", "Funding options", "Patient messages", "Reminders", "Timeline"]
    if st.session_state.get(f"view-{patient_id}") not in sections:
        st.session_state[f"view-{patient_id}"] = sections[0]
    view = st.radio("Case workspace", sections, horizontal=True, key=f"view-{patient_id}")
    if view == "Care overview":
        care_overview(case, writable)
    elif view == "Registration":
        if writable:
            with st.expander("Patient details", expanded=case["status"] == "pending"):
                profile_form(case)
            if case.get("whatsapp_contact") and not case["whatsapp_contact"]["patient_id"]:
                with st.form(f"link-wa-{patient_id}-{case['revision']}"):
                    confirm = st.checkbox("I verified the WhatsApp sender and recorded patient consent")
                    if st.form_submit_button("Link WhatsApp to this case"):
                        write(f"/patients/{patient_id}/whatsapp/link",
                              {"revision":case["revision"],"consent_to_process":confirm})
            if case.get("telegram_contact") and not case["telegram_contact"]["patient_id"]:
                with st.form(f"link-tg-{patient_id}-{case['revision']}"):
                    confirm=st.checkbox("I verified the Telegram sender and recorded patient consent")
                    if st.form_submit_button("Connect Telegram to this case"):
                        write(f"/patients/{patient_id}/telegram/link",
                              {"revision":case["revision"],"consent_to_process":confirm})
            with st.form(f"verify-{patient_id}-{case['revision']}"):
                statuses = ["pending", "needs_information", "verified", "rejected"]
                decision = st.selectbox("Registration decision", statuses, index=statuses.index(case["status"]),
                                        format_func=label)
                reason = st.text_input("Reason for rejection or missing information")
                if st.form_submit_button("Save registration decision"):
                    write(f"/patients/{patient_id}/registration",
                          {"revision": case["revision"], "status": decision, "reason": reason})
        else:
            st.write(case["profile"])
    elif view == "Documents":
        documents_page(case, writable)
    elif view == "Funding options":
        funding_page(case, writable)
    elif view == "Applications":
        application_page(case, writable)
    elif view == "Cost roadmap":
        cost_page(case, writable)
    elif view == "Patient messages":
        patient_messages(case)
    elif view == "Reminders":
        reminders_page(case, writable)
    else:
        timeline(patient_id)



def all_documents(case):
    return case["documents"] + [doc for app in case["applications"] for doc in app["documents"]]


def extraction_panel(case, doc, writable):
    key = f"{doc['id']}-{case['revision']}"
    path = f"/patients/{case['id']}/documents/{doc['id']}/extract"
    result = doc.get("ai_result") or {}
    if doc.get("ai_error"):
        st.error(doc["ai_error"])
    if result:
        st.caption("Extraction status: " + label(result["status"]))
    if result.get("fields"):
        st.caption("Check these suggestions against the original. Extraction leaves the saved profile and "
                   "verification status as they are.")
        for field, value in result["fields"].items():
            if isinstance(value, list):
                st.write(label(field))
                st.dataframe(value, hide_index=True)
            else:
                st.text(f"{label(field)}: {value}")
        if result.get("annotations"):
            if st.checkbox("Show source annotations", key=f"annotations-{key}"):
                st.json(result["annotations"])
    if not writable:
        return
    if not case["sarvam_enabled"]:
        st.caption("Sarvam is unavailable until its key is configured on the server.")
        return
    if not doc["ai_started"] or result.get("status") in {"failed","rejected","error"}:
        consent = st.checkbox("Patient authorized sending this document to Sarvam", key=f"ai-consent-{key}")
        if st.button("Start extraction", key=f"ai-start-{key}", disabled=not consent or not case["profile"]["consent"]):
            write(path, {"revision": case["revision"], "consent_to_process": consent}, "Extraction submitted.")
    elif result.get("status") in {"pending", "running"}:
        if st.button("Refresh extraction", key=f"ai-refresh-{key}"):
            write(path + "/refresh", {"revision": case["revision"]}, "Extraction status refreshed.")


def documents_page(case, writable):
    st.caption("Add a hospital estimate, intake form or supporting record before starting an application.")
    if writable:
        with st.form(f"case-document-{case['id']}-{case['revision']}"):
            kind = st.selectbox("Document type", ["Hospital estimate", "Intake form", "Treatment summary",
                                                 "Income proof", "Insurance document", "Other"])
            file = st.file_uploader("PDF, PNG or JPEG (up to 5 MB)", type=["pdf", "png", "jpg", "jpeg"])
            auto_extract = st.checkbox("Automatically read this document with Sarvam (patient authorization recorded)",
                                       value=case["profile"].get("consent",False))
            if st.form_submit_button("Save document"):
                if file is None or file.size > 5 * 1024 * 1024:
                    st.error("Choose a PDF or image up to 5 MB.")
                else:
                    write(f"/patients/{case['id']}/documents", {
                        "revision": case["revision"], "requirement": kind, "filename": file.name,
                        "content": base64.b64encode(file.getvalue()).decode(), "auto_extract": auto_extract,
                    })
    for doc in case["documents"]:
        with st.expander(f"{doc['requirement']} · {doc['filename']} · #{doc['id']}"):
            if st.button("Load original for download", key=f"case-load-{doc['id']}"):
                content = call(f"/documents/{doc['id']}", binary=True)
                if content is not None:
                    st.download_button("Download original", content, file_name=doc["filename"],
                                       key=f"case-dl-{doc['id']}")
            extraction_panel(case, doc, writable)


def scheme_comparison(overview):
    st.write('**Treatment cost − possible scheme support = remaining patient share**')
    rows = []
    for scheme in overview['schemes']:
        projection = scheme['illustration']
        rows.append({'Scheme': scheme['name'],
                     'Treatment estimate': rupees(overview['estimate']) if overview['estimate'] is not None else 'Needed',
                     'Possible support if approved': rupees(projection['possible_cover']) if projection else 'Awaiting assessment',
                     'Covered (%)': str(projection['percent']) + '%' if projection else 'Not assessed',
                     'Remaining at this benefit': rupees(projection['patient_share']) if projection else 'Awaiting assessment',
                     'Basis': 'Worker-verified inputs' if projection and projection.get('case_inputs_verified') else 'Published ceiling scenario' if projection and projection['upper_bound'] else 'Published percentage' if projection else 'Case-by-case'})
    if rows:
        st.dataframe(rows, hide_index=True)
        st.caption('One scheme at a time, using the upper treatment estimate. Ceiling scenarios assume eligible expenses and available benefit; actual awards may be smaller. Do not add overlapping benefits.')


def funding_page(case, writable):
    st.caption("These sources are ranked for review using the saved case details. Check official eligibility "
               "and the covered treatment with the scheme desk.")
    overview = call(f"/patients/{case['id']}/overview")
    if overview:
        scheme_comparison(overview)
    rows = call(f"/patients/{case['id']}/funding-sources")
    if not rows:
        st.info("There are no active, sourced scheme entries for this case. Review the scheme catalog.")
        return
    for row in rows:
        details = row["details"]
        key = f"{case['id']}-{case['revision']}-{row['id']}-{row['revision']}"
        with st.expander(row["name"]):
            st.write(details["guidance"])
            st.write("Required documents")
            for requirement in details["requirements"]:
                st.text(requirement)
            st.link_button("Official source", details["source"])
            guidance_key = f"guidance-{key}"
            if writable and case["sarvam_enabled"] and st.button("Draft verification questions", key=f"generate-{guidance_key}"):
                st.session_state[guidance_key] = call(
                    f"/patients/{case['id']}/funding-sources/{row['id']}/guidance", "POST",
                )
            if guidance := st.session_state.get(guidance_key):
                st.caption("Suggested questions. Check against the linked source.")
                for question in guidance["questions"]:
                    st.text(question)
            if not case["calculated_plan"]:
                st.caption("Save a hospital estimate in Cost roadmap to calculate a scenario.")
                continue
            st.caption("Calculate this scheme by itself. Other benefits may cover the same expenses.")
            saved = (case.get('funding_assessments') or {}).get(str(row['id']), {})
            with st.form(f"scenario-{key}"):
                eligible = st.text_input("Expenses eligible under this scheme (₹)", saved.get('eligible_expenses', ''))
                balance = st.text_input('Verified remaining benefit balance, if known (₹)', saved.get('remaining_balance') or '', help='Leave blank if not yet checked. Enter 0 only when no balance remains.')
                case_amount = st.text_input('Verified package or case-specific grant amount, if known (₹)', saved.get('case_amount') or '')
                evidence = st.text_input('Package / balance / funder reference', saved.get('evidence', ''))
                verified = st.checkbox("I checked the patient's eligibility and the covered expenses against the source")
                preview = st.form_submit_button('Calculate scenario')
                save = st.form_submit_button('Save verified benefit inputs', disabled=not writable)
                if preview or save:
                    result = call(f"/patients/{case['id']}/funding-scenario", 'POST',
                        {'scheme_id': row['id'], 'scheme_revision': row['revision'], 'case_revision': case['revision'],
                         'covered_expenses': eligible, 'remaining_balance': balance.strip() or None,
                         'case_amount': case_amount.strip() or None, 'evidence': evidence,
                         'verified_requirements': verified, 'save_assessment': save})
                    st.session_state[f'scenario-result-{key}'] = result
                    if result is not None and save:
                        st.session_state.notice = 'Benefit inputs saved. Telegram and the comparison use these amounts until the treatment context, estimate or scheme changes.'
                        st.rerun()
            if result := st.session_state.get(f"scenario-result-{key}"):
                summary = (
                    f"The current hospital estimate is {rupees(result['total'])}. "
                    f"If approved, {result['scheme']} alone may cover {rupees(result['possible_cover'])} "
                    f"({result['percent']}% of the estimate), leaving {rupees(result['patient_share'])}. "
                    f"With confirmed support only, plan for {rupees(result['confirmed_only_patient_share'])}. "
                    "Ask the hospital which payments are due before funding is applied."
                )
                st.text(summary)
                if result['upper_bound']:
                    st.caption('Maximum-benefit scenario; this does not predict a grant or package approval.')
                for note in result['assumptions']:
                    st.caption(note)
                st.download_button("Download funding scenario", summary + "\nSource: " + result["source"],
                                   file_name="oncoaid-funding-scenario.txt", key=f"scenario-export-{key}")


@st.fragment(run_every="8s")
def care_overview(case, writable):
    overview = call(f"/patients/{case['id']}/overview")
    if overview is None:
        return
    st.write("We can take this one step at a time. These details help your social worker find support "
             "and plan the next steps with you.")
    if not case["sarvam_enabled"]:
        st.error("The running API has no Sarvam key. Reload its environment and the processor.")
    for row in overview["processing"]:
        st.caption(f"Document #{row['document']} · {label(row['status'])}")
        if row["error"]:
            st.error(row["error"])
    if not overview["summaries"] and overview["facts"]:
        for field in ("diagnosis","stage","hospital","planned_treatment","estimate_scope"):
            if field in overview["facts"]:
                st.text(f"{label(field)}: {overview['facts'][field]['value']}")
    for summary in overview["summaries"]:
        st.write(summary["text"])
        st.caption(f"From document #{summary['document']} · review against the original")
    if not overview["facts"]:
        st.info("Your document summary will appear here after processing. Check Documents for progress.")
    if overview["estimate"] is not None:
        st.metric("Treatment planning estimate", rupees(overview["estimate"]) if overview["estimate_low"] == overview["estimate"] else f"{rupees(overview['estimate_low'])} – {rupees(overview['estimate'])}")
        st.caption(overview["cost_basis"])
        if overview["cost_scope"]:
            st.caption("Scope: " + overview["cost_scope"])
        for assumption in overview["cost_assumptions"]:
            st.caption("Assumption: " + assumption)
        scheme_comparison(overview)
        st.caption('Funding approval: '+(rupees(overview['confirmed_support'])+' confirmed.' if amount(overview['confirmed_support'])>0 else 'pending. Your worker will confirm support and payment dates.'))
        if overview["tariff"]:
            st.link_button("Hospital tariff source",overview["tariff"]["source"])
        st.write("This gives us a starting point for planning. Your social worker can help check funding, "
                 "confirm the hospital's payment schedule and work through any gap with you.")
    else:
        st.info("Add a hospital quote or current tariff, or generate an indicative range from the recorded prescribed treatment.")
    if writable and case["sarvam_enabled"] and case["profile"].get("consent"):
        if st.button("Generate treatment planning range", key=f"planning-{case['id']}-{case['revision']}"):
            write(f"/patients/{case['id']}/planning-estimate", {"revision": case["revision"]}, "Planning estimate updated.")
        st.caption("Sarvam uses the recorded treatment to estimate a broad range. It has no live price feed; confirm scope and current prices with the hospital.")
    st.write("**Support options to explore**")
    for scheme in overview["schemes"]:
        st.write(scheme["name"])
        st.caption(scheme["status"])
        st.write(scheme["guidance"])
        st.link_button("Check scheme requirements",scheme["source"],key=f"overview-source-{scheme['id']}")
        if scheme["illustration"]:
            scenario=scheme["illustration"]
            st.write(f"{rupees(scenario['total'])} treatment − {rupees(scenario['possible_cover'])} possible support ({scenario['percent']}%) = {rupees(scenario['patient_share'])} remaining if this benefit applies.")
            if scenario['upper_bound']:
                st.caption('Maximum-benefit scenario, subject to verification and approval.')
            for note in scenario['assumptions']:
                st.caption(note)
            st.link_button('Benefit rule source', scenario['source'], key=f"benefit-source-{scheme['id']}")
    if not overview["schemes"]:
        st.info("Your worker can add sourced support options in Schemes.")
    st.caption("Support options can overlap. Confirm eligibility and covered treatment before combining benefits.")


def treatment_prices_page():
    st.caption("Record a current hospital tariff for an exact prescribed treatment and unit, such as one cycle. "
               "The overview looks up matching treatment and hospital names and multiplies by documented units.")
    rows=call("/treatment-prices") or []
    st.dataframe(rows,hide_index=True)
    with st.form("new-tariff"):
        treatment=st.text_input("Exact treatment or regimen")
        hospital=st.text_input("Hospital")
        price=st.text_input("Cost per prescribed unit (₹)")
        source=st.text_input("Hospital tariff source URL")
        checked=st.date_input("Verified on")
        expires=st.date_input("Valid through")
        if st.form_submit_button("Save current tariff"):
            write("/treatment-prices",{"treatment":treatment,"hospital":hospital,"unit_price":price,
                                      "source":source,"checked":checked.isoformat(),"expires":expires.isoformat()})



def patient_messages(case):
    connection=call("/messaging-connection")
    if connection and connection["bots"]:
        bot=connection["bots"][0]
        st.link_button("Open OncoAid on Telegram","https://t.me/"+bot["username"])
    elif connection and not connection["telegram_configured"]:
        st.info("Telegram needs to be connected by your administrator.")
    rows=case.get("telegram_messages") or []
    if rows:
        st.dataframe(rows,hide_index=True)
    else:
        st.info("Messages from the patient's Telegram conversation will appear here.")


st.set_page_config(page_title="OncoAid", layout="centered")
st.title("OncoAid")
if not st.session_state.get("token"):
    login()
    st.stop()
st.sidebar.write(st.session_state.email)
connection = call('/messaging-connection')
if connection and connection['bots']:
    st.sidebar.link_button('Open patient bot', 'https://t.me/' + connection['bots'][0]['username'])
if st.sidebar.button("Switch account"):
    call("/logout", "POST")
    st.session_state.clear()
    st.rerun()
sections = ["Patients", "Schemes", "Treatment prices", "Worker reminders"]
if st.session_state.role == "admin":
    sections.append("Account approvals")
    sections.append("Data management")
page = st.sidebar.radio("Workspace", sections)
if notice := st.session_state.pop("notice", None):
    st.success(notice)
if page == "Patients":
    patients_page()
elif page == "Schemes":
    schemes_page()
elif page == "Treatment prices":
    treatment_prices_page()
elif page == "Data management":
    data_management_page()
elif page == "Worker reminders":
    reminders = call("/reminders/due") or []
    if reminders:
        st.dataframe(reminders, hide_index=True)
    else:
        st.info("No worker reminders due.")
else:
    for account in call("/staff") or []:
        st.write(f"{account['email']} · {label(account['role'])}")
        if account["role"] != "admin":
            target = "disabled" if account["role"] == "worker" else "worker"
            if st.button("Disable" if target == "disabled" else "Approve worker", key=f"staff-{account['id']}"):
                write(f"/staff/{account['id']}", {"role": target})
