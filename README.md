# OncoAid

OncoAid helps patients and social workers coordinate cancer funding applications, supporting documents, deadlines and treatment cost planning.

Patients use Telegram. Social workers use the Streamlit dashboard. FastAPI enforces account approval, case ownership and workflow rules. PostgreSQL stores case records, document versions and audit history. Sarvam reads uploaded documents and interprets patient questions.

## Start the application

Start Docker Desktop, then run from this directory:

```sh
python3 setup_local.py
docker compose up --build -d
```

Open http://localhost:8501 and click Admin or Worker. The shared accounts are created automatically; no password is requested in one-click mode. Use Switch account to choose the other role. This is a shared workspace, so use fictional patient information. Set ONE_CLICK_ACCESS=false to restore individually approved sign-in for private use.

## Connect Telegram

1. Open Telegram and find the verified @BotFather account.
2. Send /newbot and choose a name and available username for your OncoAid bot.
3. From this directory, run `python3 configure_telegram.py`. Paste the BotFather token into the hidden prompt.
4. Run `docker compose up --build -d`.
5. Open your bot's t.me link, press Start, record consent and tap Share my phone number.

The Telegram service uses persistent long polling. It needs outbound internet access. Configure a dedicated bot without an existing webhook. Store TELEGRAM_BOT_TOKEN on the API and Telegram services as a runtime secret when hosting online.

## Patient workflow

The bot asks for consent before reading patient details or documents. It accepts the patient's own Telegram contact to maintain one phone number per patient. A worker verifies an existing registration before linking it to a newly connected Telegram account.

1. Start the bot, consent to processing and share the patient's own phone number.
2. Send patient name, home state, hospital, annual household income, diagnosis, recorded stage, prescribed treatment/course and insurance status. A hospital summary or estimate can fill in available fields automatically through Sarvam OCR. The bot asks only for missing intake fields; unknown is accepted for stage, treatment and insurance.
3. Receive a treatment planning estimate and a numbered shortlist of active, sourced support pathways. The shortlist is screening, not a final eligibility decision. State and the catalog's known income rule are checked; enrollment, hospital eligibility and package details still need worker review.
4. Reply with a scheme number. OncoAid saves the scheme version, opens an application preparation record and creates a cover-letter draft for review. Patient selection does not submit an application or confirm a grant.
5. Reply with each checklist document number, then send its PDF or clear photo (up to 5 MB). The upload is attached to that checklist item. Earlier versions remain available. Received and verified are separate states.
6. When all checklist items are received, the application moves to worker review and a worker reminder is created. The patient receives a processing acknowledgment. If a worker rejects a document, /documents shows the reason and lets the patient upload a corrected version.

Use /status for registration/application progress and rejection reasons, /cost for financial planning, /schemes for available pathways before selection, /documents for the selected checklist, /details to update information, /worker to request personal help, and /stop to pause processing and patient reminders. Progress survives service restarts. Existing connected patients can send /start to continue from their saved information.

## Social-worker workflow

- Review incoming registrations and take ownership before editing.
- Use Patient actions to reject registration with a patient-facing reason, or remove a patient from active work by archiving. Archiving pauses reminders and document processing, preserves every record and can be reversed through View archived patients. Stopped reminders do not restart automatically.
- Confirm patient-provided and extracted details.
- Explore sourced support options and review official requirements.
- Maintain schemes and their validity dates. Existing applications retain their original scheme checklist.
- Upload and review document versions. Verification stops document reminders.
- Prepare application drafts, record submission references and record agency decisions. Rejection reasons are required.
- Plan treatment costs by phase using hospital estimates or current verified hospital tariffs.
- Record confirmed and possible support separately, avoiding overlapping allocations.
- Review patient messages, reminders and the audit timeline.

The amount to plan for uses confirmed support. Additional funding remains conditional until approved. Hospital quote scope, covered treatment, available benefits and payment timing need worker review.

## Patient language and navigation

New Telegram conversations start with language selection, followed by consent and phone sharing. Available choices are English, Tamil, Hindi, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi and Odia. Use `/language` to switch later. The preference is stored with the Telegram contact and removed by permanent patient deletion/reset.

Sarvam translates reply text into the selected language. Monetary amounts, arithmetic, percentages, menu numbers, URLs and slash commands stay outside the translation model and are restored exactly in their original positions. Replies keep their line breaks. Only explicitly listed public UI phrases are cached; patient text is not cached in the translation module. If translation is unavailable or adds numeric content, the original English message is returned with an availability notice.

- `/menu` or `menu`: open the short navigation menu.
- `/continue`, `continue`, `resume`, or `continue where left`: return to the saved intake, selected document or application review step.
- `/back` or `back`: return from an upload prompt to the checklist, from details to the scheme list, or to the menu. Previously entered data and files are retained.
- `/schemes`: compare three schemes per page; `/next` shows the next page.
- `/more NUMBER`: view a scheme's full conditions, calculation assumptions and source.
- `/cost`: see the selected scheme's projected payment and the remaining patient share.
- `/documents`, `/status`, `/worker` and `/stop` retain their existing functions. STOP also works while selecting a language.

Native navigation words are supported, including Tamil `மெனு`, `தொடரவும்`, `பின்` and Hindi `मेनू`, `जारी रखें`, `वापस`. Numeric choices also accept native digits. Slash commands work in every supported language and appear as tappable keyboard shortcuts.

After a file upload, the receipt gives only the number of files received and the next missing item. The final receipt says the team is reviewing the application. Full checklists appear on request. OCR completion prompts are translated, wait for other pending intake documents, and suppress duplicate prompts.

Financial replies emphasize **estimated treatment − scheme support = what the patient may need**. An unapproved-funding zero is no longer shown as the scheme's projected payment. In Cost roadmap, choosing a scheme changes the support and remaining-cost metrics using the same calculator as Telegram; recorded phase allocations are shown separately. Published ceilings remain conditional on eligibility, covered expenses and benefit availability.

## Permanently clear patient data

Administrators have two separate actions in addition to reversible archiving:

- **One patient:** Open Patients, select the case (turn on View archived patients when needed), expand Permanently delete patient, type `DELETE`, and click Permanently delete this patient.
- **All patient data:** Open Data management in the sidebar, review the record counts, type `RESET`, and click Permanently clear all patient data.

Permanent deletion removes documents and OCR results, application drafts and decisions, reminders, cost plans and cached estimates, patient audit entries, linked contacts, messaging history and conversation progress from OncoAid. The all-patient reset also clears contacts and messages that never completed registration. A counts-only administrative maintenance event remains, without patient identifiers. Staff logins, scheme catalog, treatment tariffs and channel configuration are kept.

Background processing and delivery coordinate with deletion, so a reset waits for in-flight work before removing its state. Bot polling offsets are retained to avoid replaying previously received messages. Record IDs are not reused. After deleting, send `/start` from the same Telegram account and complete consent and phone sharing again; it starts a fresh registration. No Docker volume removal or API-key reconfiguration is needed.

These actions erase application records, not chat messages already delivered to a person's Telegram app, external provider copies or separately created backups. Clear the chat history in Telegram itself if you want an empty conversation on screen for recording.

## Sarvam configuration

Set SARVAM_API_KEY in .env or as an API/processor/Telegram runtime secret. SARVAM_CHAT_MODEL defaults to sarvam-105b. Recreate services after changing environment values.

Extraction supports PDF, PNG and JPEG, with a 5 MB application limit. Sarvam's Extract service has a 20-page PDF limit. The document processor persists jobs and results. The Care overview displays progress and refreshes automatically.

LangChain retrieves active catalog entries for the case. Staff can request source-grounded verification questions. Treatment costs use a worker-reviewed plan first, then an extracted hospital quote, then an exact current hospital tariff × explicitly prescribed units. If those are unavailable, the patient journey can request a Sarvam planning range for the explicitly recorded treatment. Staff can request it from Care overview. The model uses general knowledge; there is no live internet price feed. The UI labels this basis and scope and shows assumptions. Vague treatment information or provider failure leaves the amount unknown instead of inventing a quote. Estimates are cached by treatment context for seven days to reduce credit use; changed context invalidates the cache.

Per-scheme comparisons now show **treatment cost − possible scheme support = remaining patient share**, plus percentage covered. The bot, selected-application `/cost` reply and dashboard use the same deterministic calculator. Each scheme is calculated separately; overlapping schemes are never added together automatically. Options are ordered by possible support, not by likelihood of approval.

The catalog supports percentage reimbursement, cashless package/annual limits, discretionary grant ceilings and individually assessed amounts. The sourced starting entries include PM-JAY and CMCHIS family-year ceilings of ₹5 lakh and the HMCPF one-time ceiling of ₹15 lakh. These are benefit ceilings, not predicted awards. When only a ceiling is known, the comparison is explicitly labelled a maximum-benefit scenario: it assumes the estimated expenses qualify and the full benefit remains available. Actual package rates, prior claims and individual sanction can lower support. An unknown benefit is shown as awaiting assessment, not ₹0. Zero is used only when calculated support or a verified remaining balance is actually zero.

In Funding options, the assigned worker can enter verified eligible expenses, remaining benefit balance and a package/case-specific grant amount with a reference. Save verified benefit inputs persists those values for the patient and updates subsequent Telegram replies and dashboard comparisons. The case must have a verified registration and reviewed cost plan first. Saved inputs are invalidated when the scheme version, treatment context or total estimate changes. Saving benefit inputs does not mark funding as approved; confirmed funding remains separately recorded in the cost plan.

Benefit sources: [CMCHIS treatment coverage](https://cmchistn.com/enrollment/treatment-covered), [PM-JAY benefit cover](https://www.rmlh.mohfw.gov.in/index1.aspx?langid=1&lev=2&lid=3619&lsid=2566), [Ministry update on HMCPF](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2154159&lang=1&reg=3). Rules checked 6 November 2026. Staff can edit sourced benefit rules in Schemes; existing application checklists keep their original snapshots. Verify each scheme's requirements and case-specific benefit before treating funding as confirmed.

## Hosting

See [HOSTING.md](HOSTING.md) and the supplied `render.yaml` for an online jury workspace. It starts a separate empty database and runs the dashboard, private API, OCR and Telegram workers in one Docker web instance.


Deploy the API, dashboard, processor and Telegram services with private PostgreSQL. Expose only the authenticated dashboard over HTTPS. Give the dashboard an API_URL pointing to the private API. The API initializes the schema before the workers start processing.

Back up PostgreSQL, which contains uploaded file versions and case history. Preserve the database volume across restarts.

The existing WhatsApp connector is disabled by default. TWILIO_OUTBOUND_MODE=live enables its outbound service when the account supports the required messaging features. The webhook gateway exposes only the signed incoming route.

## Verification

Run `docker compose exec -T api python -m unittest -v` for the financial calculation checks (including benefit caps, percentages, balances and unknown amounts) and `docker compose exec -T api python tests/journey_integration.py` for the patient-to-worker journey. The integration check creates and drops its own PostgreSQL schema and mocks provider calls; it does not send patient messages or change existing cases. Channel integration checks use isolated PostgreSQL schemas and mocked provider responses. Run `docker compose exec -T api python tests/reset_integration.py` to verify permanent deletion, same-account re-registration, reset permissions, retained configuration and in-flight delivery coordination; `docker compose exec -T api python tests/reset_dashboard.py` checks both dashboard deletion forms. These checks use disposable database schemas. Live Telegram verification requires a configured bot token.

[Telegram Bot API](https://core.telegram.org/bots/api) · [Sarvam Document AI](https://docs.sarvam.ai/api-reference/doc-ai/job/extract)

Run `docker compose exec -T api python tests/benefits_integration.py` for seeded benefit comparisons, selected-scheme Telegram replies, saved worker inputs, stale assessment handling and cap-only scheme editing. This uses a disposable schema and synthetic records.

Run `docker compose exec -T api python tests/multilingual_journey.py` for language-first consent, native navigation, short receipts and preserved progress, and `docker compose exec -T api python tests/benefit_dashboard.py` for live scheme selection in Cost roadmap. These checks use isolated synthetic records. `python -m unittest -v` includes exact-value translation safeguards.
