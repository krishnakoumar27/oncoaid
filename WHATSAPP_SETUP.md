# Connect Twilio WhatsApp to OncoAid

The app has a signed inbound webhook, an NLP conversation handler, document ingestion, OCR completion replies and patient status/cost queries. Setup needs your Twilio account settings and a public HTTPS URL.

## 1. Join your Sandbox

In the new Twilio Console, open Home → Try it out → Communications → Send a WhatsApp message. Follow the displayed phone-linking steps. The legacy Console uses the WhatsApp Sandbox page. From the phone you will use as the patient, send the displayed join phrase to the displayed Sandbox number. Use your Console's values.

## 2. Configure the local app

Add these names to the existing local .env file. Keep the token in that file.

```dotenv
TWILIO_ACCOUNT_SID=AC...your_account_sid
TWILIO_AUTH_TOKEN=your_account_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:+...your_sandbox_number
TWILIO_WEBHOOK_URL=https://your-public-host/webhooks/twilio/whatsapp
```

Signature verification needs the account Auth Token and the exact webhook URL, including its path. Copy the values from your own account. The Sarvam key remains in SARVAM_API_KEY. Recreate services after changing environment values:

```sh
docker compose up --build -d
```

The local WhatsApp gateway listens on 127.0.0.1:8080 and forwards only the signed webhook route. The dashboard remains at localhost:8501.

## 3. Get a public HTTPS address

For local development, use ngrok with your own account:

```sh
ngrok http 8080
```

Copy its HTTPS forwarding address and append /webhooks/twilio/whatsapp. Set the full address in TWILIO_WEBHOOK_URL in .env, then run docker compose up -d again.

For an online deployment, expose the gateway's port 8080 over HTTPS, with the API, processor and database on private networking. Configure the gateway upstream to the private API hostname if it differs from api:8000. The processor needs the same Twilio and Sarvam environment values as the API.

## 4. Configure Twilio's webhook

In the new Console, open Try out WhatsApp → Inbound. Under Auto-Reply settings, choose Custom and add the full HTTPS webhook URL, then save. If a method selector is shown, choose POST. In the legacy Console, use Sandbox settings → Sandbox configuration → When a message comes in, then choose POST and save. The configured address must exactly match TWILIO_WEBHOOK_URL. The endpoint acknowledges with HTTP 204, and the processor prepares the conversation reply. In live mode it sends through Twilio. Disabled outbound service stores delivery state without sending.

## 5. Test the journey

1. Send Hi. OncoAid introduces itself and asks for consent to store the case and use Sarvam to read documents.
2. Reply YES. Send a hospital summary or estimate as a PDF, PNG or JPEG under 5 MB.
3. The document appears in the worker dashboard and starts OCR. When reading finishes, OncoAid sends a completion reply and at most one bundled request for missing core details.
4. Ask a free-text question such as “Can you tell me where my application stands?” or “How much should I arrange?” Sarvam maps this to the status or reviewed-cost workflow.
5. The assigned worker verifies the extracted details, prepares applications and saves the reviewed cost plan. Send COST again to see the plan.
6. Send WORKER to create a dashboard follow-up. Send STOP to withdraw processing consent.

Use one phone per patient and send one attachment per message. Incoming patient details are suggestions awaiting worker review. The chatbot asks for a document early and accepts several details in one text message.

For an existing manually created patient, first send Hi from that patient's registered phone. The assigned worker can then link the verified sender under Registration. Send YES again afterward.

## Reply and reminder limits

Replies are sent within the customer-service window. Messages that have aged outside that window are marked needs_template. Business-initiated reminders outside the window require an approved WhatsApp template. Scheduled reminder delivery is a separate follow-up to the conversation connector. Check Twilio logs for actual delivery. The dashboard lists sent, failed and ambiguous send attempts. Ambiguous sends are not retried automatically to avoid duplicate messages.

Sandbox participants must rejoin after their sandbox session expires. For a production rollout, use Twilio WhatsApp Self Sign-up to register your own approved sender.

## Troubleshooting

- No reply: check api and processor logs and the Twilio message log. Confirm all four Twilio settings are loaded into both containers.
- HTTP 403: check the Auth Token and exact public URL. Never disable signature verification.
- Upload fails: use a supported file under 5 MB. Media fetching accepts Twilio's authenticated message-media endpoint and constrained temporary-storage redirects.
- NLP unavailable: upload a document or use STATUS, COST or WORKER. Check Sarvam access and credits.
- Existing phone: have the assigned worker link the contact after verifying the current sender.
- Duplicate webhook requests are stored once using MessageSid.

[Twilio Sandbox](https://www.twilio.com/docs/whatsapp/sandbox) · [WhatsApp quickstart](https://www.twilio.com/docs/whatsapp/quickstart) · [Webhook signature validation](https://www.twilio.com/docs/usage/webhooks/webhooks-security)

## Account compatibility

Some Twilio account flows require an approved ContentSid for outgoing messages. Configure an account that supports the desired messaging features before enabling outbound delivery. Telegram is the primary patient channel.
