CREATE TABLE IF NOT EXISTS staff (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email text UNIQUE NOT NULL,
    password text NOT NULL,
    role text NOT NULL CHECK (role IN ('pending', 'worker', 'admin', 'disabled'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token text PRIMARY KEY,
    staff_id bigint REFERENCES staff NOT NULL,
    expires timestamptz NOT NULL DEFAULT now() + interval '8 hours'
);
CREATE TABLE IF NOT EXISTS patients (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    phone text UNIQUE NOT NULL,
    profile jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    owner bigint REFERENCES staff,
    revision integer NOT NULL DEFAULT 1,
    cost_plan jsonb
);
CREATE TABLE IF NOT EXISTS schemes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text UNIQUE NOT NULL,
    details jsonb NOT NULL,
    revision integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS applications (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_id bigint REFERENCES patients NOT NULL,
    scheme_id bigint REFERENCES schemes NOT NULL,
    snapshot jsonb NOT NULL,
    status text NOT NULL DEFAULT 'draft',
    reason text NOT NULL DEFAULT '',
    reference text NOT NULL DEFAULT '',
    draft text NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS documents (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    application_id bigint REFERENCES applications NOT NULL,
    requirement text NOT NULL,
    filename text NOT NULL,
    content bytea NOT NULL,
    status text NOT NULL DEFAULT 'received',
    reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reminders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_id bigint REFERENCES patients NOT NULL,
    application_id bigint REFERENCES applications,
    requirement text,
    audience text NOT NULL CHECK (audience IN ('worker', 'patient')),
    message text NOT NULL,
    due timestamptz NOT NULL,
    repeat_days integer,
    status text NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    staff_id bigint REFERENCES staff,
    patient_id bigint REFERENCES patients,
    action text NOT NULL,
    detail jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_patient_idx ON audit(patient_id, id DESC);
CREATE INDEX IF NOT EXISTS application_patient_idx ON applications(patient_id);
CREATE INDEX IF NOT EXISTS document_application_idx ON documents(application_id, id DESC);

-- Existing checklist documents keep their application links and version history.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS patient_id bigint REFERENCES patients;
UPDATE documents d SET patient_id=a.patient_id FROM applications a
WHERE d.application_id=a.id AND d.patient_id IS NULL;
ALTER TABLE documents ALTER COLUMN patient_id SET NOT NULL;
ALTER TABLE documents ALTER COLUMN application_id DROP NOT NULL;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS ai_job text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS ai_result jsonb;
CREATE INDEX IF NOT EXISTS document_patient_idx ON documents(patient_id, id DESC);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS ai_authorized boolean NOT NULL DEFAULT false;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS ai_error text;
CREATE TABLE IF NOT EXISTS treatment_prices (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    treatment text NOT NULL,
    hospital text NOT NULL,
    unit_price numeric(14,2) NOT NULL CHECK (unit_price>0),
    source text NOT NULL,
    checked date NOT NULL,
    expires date NOT NULL,
    UNIQUE(treatment,hospital,checked)
);

CREATE TABLE IF NOT EXISTS whatsapp_contacts (
    phone text PRIMARY KEY,
    patient_id bigint REFERENCES patients,
    consent boolean NOT NULL DEFAULT false,
    suggested_fields jsonb NOT NULL DEFAULT '{}',
    last_inbound timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS whatsapp_messages (
    sid text PRIMARY KEY,
    phone text NOT NULL REFERENCES whatsapp_contacts,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'received',
    reply text,
    outbound_sid text,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS whatsapp_sid text UNIQUE;
ALTER TABLE whatsapp_contacts ADD COLUMN IF NOT EXISTS followup_sent boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS telegram_bots (
    id bigint PRIMARY KEY,
    username text NOT NULL,
    next_offset bigint NOT NULL DEFAULT 0,
    connected_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS telegram_contacts (
    bot_id bigint NOT NULL REFERENCES telegram_bots,
    chat_id bigint NOT NULL,
    phone text,
    patient_id bigint REFERENCES patients,
    consent boolean NOT NULL DEFAULT false,
    suggested_fields jsonb NOT NULL DEFAULT '{}',
    followup_sent boolean NOT NULL DEFAULT false,
    PRIMARY KEY(bot_id,chat_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS telegram_contact_phone_idx ON telegram_contacts(bot_id,phone) WHERE phone IS NOT NULL;
CREATE TABLE IF NOT EXISTS telegram_messages (
    key text PRIMARY KEY,
    bot_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'received',
    reply text,
    keyboard jsonb,
    error text,
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (bot_id,chat_id) REFERENCES telegram_contacts
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS telegram_key text UNIQUE;
ALTER TABLE telegram_messages ADD COLUMN IF NOT EXISTS retry_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE patients ADD COLUMN IF NOT EXISTS archived_at timestamptz;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS planning_estimate jsonb;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS registration_reason text NOT NULL DEFAULT '';
ALTER TABLE telegram_contacts ADD COLUMN IF NOT EXISTS journey jsonb NOT NULL DEFAULT '{}';
ALTER TABLE patients ADD COLUMN IF NOT EXISTS funding_assessments jsonb NOT NULL DEFAULT '{}';
ALTER TABLE telegram_contacts ADD COLUMN IF NOT EXISTS language_code text;
ALTER TABLE telegram_contacts ADD COLUMN IF NOT EXISTS language_selecting boolean NOT NULL DEFAULT false;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS access_mode text NOT NULL DEFAULT 'password';
