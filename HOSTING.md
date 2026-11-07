# Put OncoAid online for the jury

The supplied `render.yaml` creates one always-on Docker web service and a separate managed PostgreSQL database in Singapore. `cloud_start.py` starts the private FastAPI service, waits for schema initialization, then starts Streamlit, the document processor and Telegram polling. Only Streamlit is exposed publicly. PostgreSQL stores uploads and case history, so restarts retain them.

This deployment starts empty. It does not copy patients, uploaded documents or settings from your laptop. One-click Admin and Worker access is enabled. Every visitor can choose either shared account, including Admin actions that remove data. Use fictional patient information for this public workspace. Audit entries identify the selected shared account, not each individual visitor.

## Deploy

1. Create an empty GitHub repository, for example `oncoaid`. A private repository works if you grant Render access to it.
2. Unzip `oncoaid-web-source.zip` and upload the contents of its `oncoaid` folder to the **repository root**. `render.yaml`, `Dockerfile`, `cloud_start.py`, `api.py` and `dashboard.py` must be at the root. The prepared zip excludes `.env`, API keys, caches and local database data. Do not upload your local `.env` separately.
3. On your laptop, stop the local bot before moving the same Telegram token online:
   ```sh
   docker compose stop telegram processor
   ```
   Run this from the application directory. Telegram polling must run in only one deployment for this bot. Local and online PostgreSQL databases cannot coordinate their bot locks with each other.
4. Sign in to [Render](https://dashboard.render.com/). Choose **New → Blueprint**, connect GitHub, and select the repository. Use `render.yaml` as the Blueprint path.
5. Render asks for two secret values: `SARVAM_API_KEY` and `TELEGRAM_BOT_TOKEN`. Copy the values from your existing local settings into those fields. No Admin or Worker password is required from you; accounts are selected with buttons. The Blueprint generates an internal bootstrap secret automatically.
6. Review the paid service/database plans and the estimated charge, then create the Blueprint. The smallest always-on plans are selected in the file. The 512 MB web instance is suitable for light jury traffic; increase its memory if the dashboard reports out-of-memory restarts under concurrent use.
7. Wait until the database and web service are healthy. Open the web service's **onrender.com** URL. Choose **Admin** or **Worker**. Once Telegram connects, **Open patient bot** appears in the sidebar.
8. Share the dashboard URL and the bot's `https://t.me/your_bot_username` link with the jury. The laptop and ngrok can remain off after the online deployment is running.

A Blueprint deployment requires access to your GitHub and Render accounts and review of its billing estimate. The supplied files are deployment-ready; they do not create an account, start a billable cloud service or produce a live URL by themselves.

## Jury journey

1. Open the dashboard and choose Worker.
2. Open the patient bot. Send `/start`, choose a language, record consent and share your own contact.
3. Upload a fictional hospital summary/estimate, or enter the requested details.
4. Compare treatment cost, possible scheme support and the remaining share. Choose a scheme, then upload each numbered checklist document.
5. Return to the dashboard. Take ownership with Assign to me, verify the intake and documents, and review the preparation draft.
6. Choose Switch account, then Admin to inspect scheme settings, timeline and data management.
7. Use the admin Data management reset when you want an empty patient workspace again. It preserves staff, schemes and channel configuration.

All visitors share the same case list and accounts; coordinate resets so one person does not erase another person's in-progress review.

## Hosting settings

The file currently uses Render's plan IDs `0.5c-512mb` for the web service and `0.1c-256mb` for PostgreSQL, with 5 GB database storage. Review current prices in Render before deploying. Free web instances sleep after 15 minutes without inbound traffic, pausing the co-hosted Telegram and OCR workers. Free PostgreSQL expires after 30 days. The always-on paid configuration avoids those limitations; it is not a high-availability production healthcare deployment.

Render provides the public HTTPS URL. Keep one web instance for this small shared workspace. The app's API binds to `127.0.0.1:8000`; the dashboard binds to Render's `PORT`. Database connections use the private connection URL wired by the Blueprint. External database connections are disabled through `ipAllowList: []`.

If you later switch to private real-patient use, set `ONE_CLICK_ACCESS=false` and restore individually approved staff sign-in before adding real records.

## Troubleshooting

- **Bot conflict / missing replies:** stop the laptop's Telegram service and any other deployments using this token. No ngrok webhook is required for the Telegram polling service.
- **Language/OCR unavailable:** confirm the Sarvam secret is set on the web service and that the account has credits; redeploy after correcting it.
- **No bot link:** check the web service's Telegram logs and verify the bot token. A dedicated polling bot must not have an existing webhook.
- **Database startup failure:** confirm the Blueprint database is healthy. `DATABASE_URL` is populated by the Blueprint; don't replace it with your laptop's connection settings.
- **Render cannot locate Dockerfile:** move the application files to the GitHub repository root. If using a larger repository instead, adjust `dockerContext` and `dockerfilePath` in `render.yaml` to the application's folder.
- **Stop cloud charges after judging:** remove the web service and database from Render when you no longer need them. Removing only the Blueprint does not necessarily delete its resources. Export any needed records before deleting the database.

Official references: [Blueprint setup](https://render.com/docs/infrastructure-as-code), [Blueprint fields](https://render.com/docs/blueprint-spec), [Docker deployment](https://render.com/docs/docker), [Compute plans](https://render.com/docs/compute-plans), [Pricing](https://render.com/pricing), [Free service limits](https://render.com/docs/free).
