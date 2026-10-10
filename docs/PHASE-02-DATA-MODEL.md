# Veda Stage 1 — PostgreSQL Data Model (Phase Two)

Status: **draft for owner review.** Design only; no migration written yet (that is Phase Three).

PostgreSQL is the **durable source of truth** for tasks, attempts, outcomes and metrics. The model
language is not authoritative for fee facts or any application action.

Conventions applied throughout:

* every timestamp is `TIMESTAMPTZ`, stored in UTC, defaulted to `now()`;
* money is stored as `NUMERIC(12,2)` with an explicit ISO currency code (never a float);
* identifiers are `BIGINT GENERATED ALWAYS AS IDENTITY`;
* every table has `created_at`; mutable tables also have `updated_at`;
* phone numbers are stored once, normalised to E.164 (`^\+[1-9][0-9]{7,14}$`), and are the only
  place a full number is held;
* free text is length-capped by `CHECK` constraints, mirroring `speech_text.py` caps;
* no column anywhere stores a secret, a credential, an API key or raw audio.

---

## 1. Entity overview

```
school ─┬─ student ──── reminder_task ──── call_attempt ─┬─ call_result
        │                     ▲                          └─ call_metric
        ├─ import_batch ──────┤
        ├─ response_template  │
        ├─ audio_asset        │
        └─ do_not_call        │
                              └── import_row ── (source of student/reminder_task)
provider_event (append-only, idempotent)
```

Deliberately **absent**: any transcript or recording table. Brief §17.8 makes Stage 1 store call
outcomes and performance data only. Adding a transcript table later requires a separate written
owner approval, a stated purpose and retention period, access controls, deletion behaviour and
updated privacy documentation — it will not be added silently.

---

## 2. Identity and configuration

```sql
CREATE TABLE school (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 2 AND 100),
    timezone      TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    languages     TEXT[] NOT NULL DEFAULT ARRAY['te-IN'],
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT school_name_unique UNIQUE (name),
    CONSTRAINT school_name_not_placeholder CHECK (
        lower(btrim(name)) NOT IN ('the school','school','your school','our school','xxx')
    )
);
```

The placeholder guard mirrors `config.py::PLACEHOLDER_SCHOOL_NAMES` at the storage layer too:
a school can never be persisted under the name Veda once spoke by accident.

```sql
CREATE TABLE school_setting (          -- approved template versions, operational switches
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id   BIGINT NOT NULL REFERENCES school(id) ON DELETE RESTRICT,
    key         TEXT NOT NULL CHECK (length(key) <= 60),
    value       TEXT NOT NULL CHECK (length(value) <= 400),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT school_setting_key_unique UNIQUE (school_id, key)
);
```

No provider secret is ever stored in `school_setting`; secrets live only in environment
configuration (brief §7.1).

```sql
CREATE TABLE student (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id            BIGINT NOT NULL REFERENCES school(id) ON DELETE RESTRICT,
    student_name         TEXT NOT NULL CHECK (length(btrim(student_name)) BETWEEN 1 AND 80),
    parent_name          TEXT CHECK (length(parent_name) <= 80),
    parent_phone_e164    TEXT NOT NULL
        CHECK (parent_phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    student_notes        TEXT CHECK (length(student_notes) <= 400),
    -- normalised keys used only for duplicate/ambiguity detection
    student_name_key     TEXT NOT NULL CHECK (length(student_name_key) BETWEEN 1 AND 80),
    parent_phone_tail10  CHAR(10) NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT student_identity_unique UNIQUE (school_id, parent_phone_e164, student_name_key)
);

CREATE INDEX student_phone_idx      ON student (parent_phone_e164);
CREATE INDEX student_phone_tail_idx ON student (parent_phone_tail10);
CREATE INDEX student_name_key_idx   ON student (school_id, student_name_key);
```

Identical `(student, phone)` pairs collapse to one row, which is what makes re-importing the same
workbook safe. Ambiguity is *reported*, not resolved by guessing.

```sql
CREATE TABLE do_not_call (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id         BIGINT NOT NULL REFERENCES school(id) ON DELETE RESTRICT,
    parent_phone_e164 TEXT NOT NULL CHECK (parent_phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    reason            TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 200),
    source_attempt_id BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT do_not_call_phone_unique UNIQUE (school_id, parent_phone_e164)
);
```

Any future task creation for this phone is refused by the application, and the constraint makes a
duplicate entry impossible. (This is the storage half of the `Do Not Call` outcome.)

---

## 3. Import

```sql
CREATE TABLE import_batch (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id      BIGINT NOT NULL REFERENCES school(id) ON DELETE RESTRICT,
    original_name  TEXT NOT NULL CHECK (length(original_name) <= 200),
    stored_path    TEXT NOT NULL CHECK (length(stored_path) <= 400),
    source_format  TEXT NOT NULL CHECK (source_format IN ('xlsx','xls','csv')),
    file_sha256    CHAR(64) NOT NULL,
    rows_total     INTEGER NOT NULL DEFAULT 0 CHECK (rows_total >= 0),
    rows_accepted  INTEGER NOT NULL DEFAULT 0 CHECK (rows_accepted >= 0),
    rows_rejected  INTEGER NOT NULL DEFAULT 0 CHECK (rows_rejected >= 0),
    imported_by    TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT import_batch_sha_unique UNIQUE (school_id, file_sha256)
);
```

Re-importing a byte-identical file is refused with "this workbook was already imported", which is
the first line of duplicate-call defence (brief §3 requirement: duplicate reminder imports).

```sql
CREATE TABLE import_row (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_batch_id   BIGINT NOT NULL REFERENCES import_batch(id) ON DELETE CASCADE,
    row_number        INTEGER NOT NULL CHECK (row_number >= 1),
    raw_payload       JSONB NOT NULL,      -- redacted: no full phone number stored here
    validation_status TEXT NOT NULL CHECK (validation_status IN ('accepted','quarantined')),
    reasons           TEXT[] NOT NULL DEFAULT '{}',
    student_id        BIGINT REFERENCES student(id) ON DELETE SET NULL,
    reminder_task_id  BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT import_row_number_unique UNIQUE (import_batch_id, row_number)
);

CREATE INDEX import_row_status_idx ON import_row (import_batch_id, validation_status);
```

`raw_payload` holds the row **redacted** (phone reduced to its last four digits, notes truncated)
so the office can see why a row failed without keeping a second copy of family data. Every row gets
a verdict; a rejected row is never dialled (brief §4).

---

## 4. Reminder tasks

```sql
CREATE TABLE reminder_task (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id          BIGINT NOT NULL REFERENCES school(id) ON DELETE RESTRICT,
    student_id         BIGINT NOT NULL REFERENCES student(id) ON DELETE RESTRICT,
    reminder_type      TEXT NOT NULL DEFAULT 'fee' CHECK (reminder_type IN ('fee')),
    reminder_period    TEXT NOT NULL CHECK (length(btrim(reminder_period)) BETWEEN 1 AND 40),
    -- verified fee facts, snapshotted at import so a later workbook edit cannot change what was said
    amount_due         NUMERIC(12,2) NOT NULL CHECK (amount_due > 0 AND amount_due < 10000000000),
    currency           CHAR(3) NOT NULL DEFAULT 'INR',
    due_date           DATE,
    max_attempts       SMALLINT NOT NULL DEFAULT 1 CHECK (max_attempts BETWEEN 1 AND 3),
    task_status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (task_status IN ('pending','claimed','dialed','finished','cancelled','needs_review')),
    source_import_row  BIGINT REFERENCES import_row(id) ON DELETE SET NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reminder_task_business_key UNIQUE (school_id, student_id, reminder_type, reminder_period)
);

CREATE INDEX reminder_task_status_idx ON reminder_task (task_status, created_at);
CREATE INDEX reminder_task_student_idx ON reminder_task (student_id);
```

`reminder_task_business_key` is what makes importing the same term twice produce one task per
student, not two calls. `amount_due`/`due_date` live here (not on `student`) because a fee
obligation is a point-in-time fact: a snapshot here means a later workbook correction cannot
retroactively change what Veda already said on an earlier call.

---

## 5. Attempts, and the one-call rule enforced by the database

```sql
CREATE TABLE call_attempt (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reminder_task_id   BIGINT NOT NULL REFERENCES reminder_task(id) ON DELETE RESTRICT,
    attempt_no         SMALLINT NOT NULL CHECK (attempt_no >= 1),
    -- was a call actually created at the provider?
    dial_state         TEXT NOT NULL CHECK (dial_state IN ('NOT_DIALED','DIALED','UNCERTAIN_NEEDS_REVIEW')),
    provider           TEXT NOT NULL DEFAULT 'exotel' CHECK (provider IN ('exotel','twilio')),
    provider_call_id   TEXT CHECK (length(provider_call_id) <= 80),
    lifecycle_state    TEXT NOT NULL DEFAULT 'CREATED' CHECK (lifecycle_state IN
        ('CREATED','DIALING','RINGING','ANSWERED','IN_PROGRESS','ENDING','ENDED')),
    pipeline_session_id UUID,
    claim_token        UUID NOT NULL,
    claimed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    dialed_at          TIMESTAMPTZ,
    answered_at        TIMESTAMPTZ,
    ended_at           TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT call_attempt_number_unique UNIQUE (reminder_task_id, attempt_no)
);

CREATE UNIQUE INDEX call_attempt_provider_id_unique
    ON call_attempt (provider, provider_call_id) WHERE provider_call_id IS NOT NULL;
```

### The cap: `MAX_ATTEMPTS_PER_TASK`, enforced in the database

```sql
CREATE OR REPLACE FUNCTION enforce_attempt_cap() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cap      SMALLINT;
    realised INTEGER;
BEGIN
    SELECT max_attempts INTO cap FROM reminder_task WHERE id = NEW.reminder_task_id FOR UPDATE;

    -- A row that definitely never created a call does not consume the allowance,
    -- because it may be corrected and dialled later (brief §19.1).
    SELECT count(*) INTO realised
      FROM call_attempt
     WHERE reminder_task_id = NEW.reminder_task_id
       AND dial_state <> 'NOT_DIALED'
       AND id <> COALESCE(NEW.id, -1);

    IF realised >= cap THEN
        RAISE EXCEPTION 'attempt cap reached for reminder_task % (cap %, realised %)',
              NEW.reminder_task_id, cap, realised
              USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER call_attempt_cap
    BEFORE INSERT OR UPDATE OF dial_state, reminder_task_id ON call_attempt
    FOR EACH ROW EXECUTE FUNCTION enforce_attempt_cap();
```

Why a trigger and not only a Python check: brief §0.2 rule 2 requires the one-attempt rule to be
enforced **in the database, not only in Python**. Two processes claiming the same task at the same
moment — or a future Stage 2 endpoint — cannot both dial. `NOT_DIALED` rows are the single,
documented exception, exactly as §7.4/§19.1 require (`NOT_DIALED` may be re-dialled after the
cause is fixed; `UNCERTAIN_NEEDS_REVIEW` never is).

### Claiming atomically

```sql
-- one statement, no in-memory lock involved
WITH next AS (
    SELECT t.id
      FROM reminder_task t
      JOIN student s ON s.id = t.student_id
     WHERE t.school_id = $1
       AND t.task_status = 'pending'
       AND NOT EXISTS (SELECT 1 FROM do_not_call d
                        WHERE d.school_id = t.school_id
                          AND d.parent_phone_e164 = s.parent_phone_e164)
     ORDER BY t.created_at
     FOR UPDATE OF t SKIP LOCKED
     LIMIT 1
)
UPDATE reminder_task t
   SET task_status = 'claimed', updated_at = now()
  FROM next
 WHERE t.id = next.id
RETURNING t.id;
```

`FOR UPDATE ... SKIP LOCKED` plus the cap trigger is the whole duplicate-call defence, and it works
across processes and restarts.

---

## 6. Outcome — written exactly once

```sql
CREATE TABLE call_result (
    call_attempt_id     BIGINT PRIMARY KEY REFERENCES call_attempt(id) ON DELETE RESTRICT,
    outcome             TEXT NOT NULL CHECK (outcome IN (
                            'COMPLETED','NO_RESPONSE','VOICEMAIL','CALLBACK_REQUESTED',
                            'WRONG_NUMBER','NOT_PARENT','DO_NOT_CALL',
                            'UNCERTAIN_NEEDS_REVIEW','FAILED','CANCELLED')),
    reason_code         TEXT CHECK (reason_code IS NULL OR reason_code IN (
                            'date_promised','no_date_given','already_paid','principal_requested',
                            'financial_reason','parent_declined','ring_timeout','parent_silent',
                            'dial_result_unknown','provider_error','pipeline_error',
                            'internal_cancel','configuration_error')),
    -- structured results required by the brief §7.9 / §18.1
    promised_date       DATE,
    promised_date_text  TEXT CHECK (length(promised_date_text) <= 60),
    callback_time_text  TEXT CHECK (length(callback_time_text) <= 120),
    principal_request   BOOLEAN NOT NULL DEFAULT FALSE,
    already_paid_claim  BOOLEAN NOT NULL DEFAULT FALSE,
    financial_reason    BOOLEAN NOT NULL DEFAULT FALSE,
    parent_declined     BOOLEAN NOT NULL DEFAULT FALSE,
    do_not_call         BOOLEAN NOT NULL DEFAULT FALSE,
    date_questions_asked SMALLINT NOT NULL DEFAULT 0 CHECK (date_questions_asked BETWEEN 0 AND 2),
    prompt_version      TEXT NOT NULL CHECK (length(prompt_version) <= 40),
    internal_note       TEXT CHECK (length(internal_note) <= 300),   -- sanitised category only
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT call_result_completed_needs_reason CHECK (
        outcome <> 'COMPLETED' OR reason_code IS NOT NULL
    ),
    CONSTRAINT call_result_principal_needs_completed CHECK (
        NOT principal_request OR outcome = 'COMPLETED'
    )
);

CREATE OR REPLACE FUNCTION forbid_outcome_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.outcome <> OLD.outcome OR NEW.reason_code IS DISTINCT FROM OLD.reason_code THEN
        RAISE EXCEPTION 'the final outcome of attempt % is already set (% / %)',
              OLD.call_attempt_id, OLD.outcome, OLD.reason_code
              USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER call_result_outcome_immutable
    BEFORE UPDATE ON call_result FOR EACH ROW EXECUTE FUNCTION forbid_outcome_change();
```

`PRIMARY KEY (call_attempt_id)` gives exactly one outcome row per attempt; the trigger makes the
outcome itself immutable. Both halves of "a final outcome can never be overwritten" are therefore
storage guarantees, not conventions (brief §10 [ADDED], §18.1).

The `principal_request` and `do_not_call` checks encode two rules from §16: Veda may promise to
inform the principal only when that request is really recorded, and a DNC request must accompany a
terminal outcome rather than being dropped.

---

## 7. Metrics

```sql
CREATE TABLE call_metric (
    call_attempt_id        BIGINT PRIMARY KEY REFERENCES call_attempt(id) ON DELETE RESTRICT,
    -- raw observable markers, in the brief's t0..t5 order (UTC)
    t0_last_input_audio    TIMESTAMPTZ,
    t1_turn_end_detected   TIMESTAMPTZ,
    t2_stt_final           TIMESTAMPTZ,
    t3_llm_first_token     TIMESTAMPTZ,
    t4_first_tts_audio     TIMESTAMPTZ,
    t5_first_outgoing_audio TIMESTAMPTZ,
    timing_available       BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = provider gave no evidence
    -- derived, computed in Python from monotonic deltas and stored for reporting
    turn_detection_ms      INTEGER CHECK (turn_detection_ms    IS NULL OR turn_detection_ms    >= 0),
    stt_finalize_ms        INTEGER CHECK (stt_finalize_ms      IS NULL OR stt_finalize_ms      >= 0),
    llm_first_token_ms     INTEGER CHECK (llm_first_token_ms   IS NULL OR llm_first_token_ms   >= 0),
    tts_startup_ms         INTEGER CHECK (tts_startup_ms       IS NULL OR tts_startup_ms       >= 0),
    outgoing_transport_ms  INTEGER CHECK (outgoing_transport_ms IS NULL OR outgoing_transport_ms >= 0),
    response_start_ms      INTEGER CHECK (response_start_ms    IS NULL OR response_start_ms    >= 0),
    cache_lookup_ms        INTEGER CHECK (cache_lookup_ms      IS NULL OR cache_lookup_ms      >= 0),
    -- usage
    audio_cache_hits       INTEGER NOT NULL DEFAULT 0 CHECK (audio_cache_hits   >= 0),
    audio_cache_misses     INTEGER NOT NULL DEFAULT 0 CHECK (audio_cache_misses >= 0),
    cache_validation_failures INTEGER NOT NULL DEFAULT 0 CHECK (cache_validation_failures >= 0),
    tts_characters         INTEGER NOT NULL DEFAULT 0 CHECK (tts_characters >= 0),
    llm_input_tokens       INTEGER CHECK (llm_input_tokens  IS NULL OR llm_input_tokens  >= 0),
    llm_output_tokens      INTEGER CHECK (llm_output_tokens IS NULL OR llm_output_tokens >= 0),
    stt_audio_seconds      NUMERIC(10,2) CHECK (stt_audio_seconds IS NULL OR stt_audio_seconds >= 0),
    provider_duration_secs INTEGER CHECK (provider_duration_secs IS NULL OR provider_duration_secs >= 0),
    provider_cost_minor    BIGINT CHECK (provider_cost_minor IS NULL OR provider_cost_minor >= 0),
    cost_currency          CHAR(3),
    -- reliability counters
    stt_failures           INTEGER NOT NULL DEFAULT 0,
    llm_failures           INTEGER NOT NULL DEFAULT 0,
    tts_failures           INTEGER NOT NULL DEFAULT 0,
    pipeline_startup_failures INTEGER NOT NULL DEFAULT 0,
    cancellations          INTEGER NOT NULL DEFAULT 0,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Percentiles (p50/p95) are computed by query when the sample is large enough; nothing is invented
when the provider reports nothing. A cache-hit path leaves the LLM/TTS columns `NULL` instead of
fabricating a value, and sets `timing_available` accordingly.

---

## 8. Provider events (deduplication)

```sql
CREATE TABLE provider_event (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider          TEXT NOT NULL CHECK (provider IN ('exotel','twilio')),
    provider_event_key TEXT NOT NULL CHECK (length(provider_event_key) <= 160),
    provider_call_id  TEXT CHECK (length(provider_call_id) <= 80),
    call_attempt_id   BIGINT REFERENCES call_attempt(id) ON DELETE SET NULL,
    event_type        TEXT NOT NULL CHECK (length(event_type) <= 60),
    occurred_at       TIMESTAMPTZ,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload_redacted  JSONB NOT NULL DEFAULT '{}'::jsonb,   -- no personal data, no secrets
    processed_at      TIMESTAMPTZ,
    CONSTRAINT provider_event_key_unique UNIQUE (provider, provider_event_key)
);
```

The unique key is what makes duplicate and out-of-order webhooks harmless: a repeat insert conflicts
and is skipped, so a provider event can never be processed twice.

---

## 9. Audio cache metadata and approved templates

```sql
CREATE TABLE audio_asset (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('opener_line_1','opener_line_2','closing','silence_check')),
    content_sha256   CHAR(64) NOT NULL,
    text             TEXT NOT NULL CHECK (length(text) <= 400),
    language         TEXT NOT NULL DEFAULT 'te-IN',
    voice            TEXT NOT NULL,
    model            TEXT NOT NULL,
    pace             NUMERIC(4,2) NOT NULL,
    temperature      NUMERIC(4,2),
    audio_format     TEXT NOT NULL DEFAULT 'pcm_s16le',
    sample_rate      INTEGER NOT NULL CHECK (sample_rate = 8000),
    template_version TEXT NOT NULL CHECK (length(template_version) <= 20),
    storage_key      TEXT NOT NULL CHECK (length(storage_key) <= 200),
    byte_size        BIGINT NOT NULL CHECK (byte_size > 0),
    duration_secs    NUMERIC(8,3) NOT NULL CHECK (duration_secs > 0),
    validation_status TEXT NOT NULL DEFAULT 'unvalidated'
        CHECK (validation_status IN ('unvalidated','valid','invalid','missing')),
    validated_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT audio_asset_variant_unique UNIQUE (
        content_sha256, language, voice, model, pace, temperature,
        audio_format, sample_rate, template_version
    )
);
```

The unique constraint is the concrete form of "do not reuse audio when any relevant property
differs". Changing voice, model, pace, rate, format or template version therefore produces a new
row instead of silently serving stale speech, and `content_sha256` ties the row to the exact words.

```sql
CREATE TABLE response_template (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key          TEXT NOT NULL CHECK (length(key) <= 60),
    language     TEXT NOT NULL DEFAULT 'te-IN',
    version      TEXT NOT NULL CHECK (length(version) <= 20),
    body         TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 600),
    placeholders TEXT[] NOT NULL DEFAULT '{}',
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT response_template_key_unique UNIQUE (key, language, version)
);
```

Templates carry placeholders only; every placeholder is populated from validated current data, and
a template match never authorises reusing a previous personalised fee sentence (brief §13.1, §13.3).

---

## 10. Retention and deletion

| Data | Retention | Deletion |
|---|---|---|
| `call_result`, `call_metric` | per school policy; default 24 months | `DELETE` by `created_at`; the export exists first |
| `provider_event` | 90 days | payload already redacted; rows purged by `received_at` |
| `import_row.raw_payload` | 90 days | set to `'{}'`, row and verdict kept |
| `import_batch`, `student`, `reminder_task` | while the term is in use | deleted only with an explicit school instruction |
| `audio_asset` | while the voice/model/template is current | invalidated then unlinked; metadata row marked `invalid` |
| `call_attempt` | never deleted while its task exists | `ON DELETE RESTRICT` prevents orphan history |
| transcripts / recordings | **not stored at all** in Stage 1 | n/a |

Deletion is performed by a documented command, not by hand-written SQL in production. No legal
compliance claim is made; the brief forbids claiming compliance without a documented assessment.

---

## 11. What this model deliberately does *not* do

* It does not support multiple attempts by default: `max_attempts` is 1 and the trigger enforces it.
  Raising it is an owner decision and a data change, not a code change.
* It does not store transcripts or recordings, and adds no column that could hold one.
* It does not store provider secrets, and no table has a column that could.
* It does not introduce Redis, a queue table or cross-tenant machinery.
* It does not treat "call answered" as "fee discussed": `Completed` requires a reason code, and
  business success is a separately defined metric (`outcome = 'COMPLETED' AND reason_code IN
  ('date_promised')` for a confirmed payment date).

## 12. Acceptance criteria for Phase Three implementation

* `alembic upgrade head` from an empty database succeeds, and `alembic downgrade base` reverses it.
* The cap trigger rejects a second `DIALED` attempt for a task with `max_attempts = 1`.
* A concurrent claim test shows two workers never claim the same task.
* Re-importing a byte-identical workbook is refused; re-importing the same term creates no second task.
* `UPDATE call_result SET outcome = ...` on an existing row is rejected.
* A duplicate `provider_event` insert conflicts instead of double-processing.
* Every constraint above has a test that provokes it, including the ones expected to fail.
