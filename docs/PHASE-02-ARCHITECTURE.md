# Veda Stage 1 — Architecture (Phase Two)

Status: **draft for owner review.** Documents only; no code changed.

Companions: `PHASE-02-DATA-MODEL.md` (schema), `PHASE-02-ASSUMPTIONS.md` (reconstructed brief
sections + flagged assumptions), `PHASE-01-AUDIT.md` (audit).

---

## 1. Goals and non-goals

**Goals.** One authorized school operator can (a) hand Veda the school's own fee workbook in
`.xlsx`, `.xls` or `.csv` form, (b) see exactly which rows were accepted and which were
quarantined and why, (c) preview eligible reminder tasks without dialling, (d) place one
explicitly authorized real call, (e) read durable outcomes and performance metrics, and (f) export
the office-facing result sheet. Conversations run in Telugu mixed with common English, driven by
deterministic Python with the model used only for understanding and wording.

**Non-goals (Stage 1).** No frontend. No Redis. No LangGraph. No multi-tenant SaaS features. No
persisted transcripts or recordings. No automatic batch dialling. No promise of a second attempt
on the same reminder task.

**Hard rules this architecture exists to protect** (brief §0.2):

1. Fee details are never spoken before identity is confirmed.
2. One outbound attempt per reminder task, enforced by the database, never retried blindly.
3. The model performs no actions; Python records outcomes, dates, callbacks and requests.
4. Veda says something was noted only after the row is committed.
5. Telugu words in Telugu script; names converted before the call; numbers and amounts as English words.
6. The opening is scripted, pausable, non-interruptible, and the identity question always plays.
7. Every external call has a timeout; nothing stays stuck.
8. Replies are one to two sentences; a question is never repeated; the date question is asked at most twice.
9. Secrets live only in the environment.
10. Dry-run never dials; real dialling needs an explicit flag.
11. Each phase stops for evidence and approval.
12. Nothing is called "tested" unless it actually ran.

---

## 2. Component view

```
                 ┌───────────────────────────── CLI (veda/cli/*) ─────────────────────────────┐
                 │ import_file · preview · dial · status · outcomes · export · metrics ·         │
                 │ validate_audio · serve                                                        │
                 └───────────────┬─────────────────────────────────────────────────────────────┘
                                 │
   ingestion/                    │            app/                                    integrations/
 ┌──────────────────┐  tasks     │   ┌──────────────────────┐    ┌──────────────┐    ┌─────────────────┐
 │ document_parser  │──────────► │   │ task_service         │───►│ repository   │───►│ asyncpg pool    │
 │ validation       │            │   │ call_controller      │    │ (SQL, tx)    │    │ PostgreSQL      │
 │ deduplication    │◄── reasons │   │ conversation_         │    └──────┬───────┘    └─────────────────┘
 │ staff_export     │            │   │   controller          │           │
 └──────────────────┘            │   │ response_service      │    ┌──────▼───────┐
                                 │   │ outcome_service       │    │ audio_cache  │
                                 │   └──────────┬───────────┘    │ response_cache│
                                 │              │                 └──────────────┘
                                 │   ┌──────────▼───────────────────────────────────┐
                                 │   │ voice/  (Pipecat)                            │
                                 │   │ transport → OpenerGate → STT → recorders     │
                                 │   │ → user aggregator(VAD) → LLM → TextFilter    │
                                 │   │ → TTS → transport.out → assistant aggregator │
                                 │   └──────────┬───────────────────────────────────┘
                                 │              │
                                 │   ┌──────────▼───────────┐   ┌──────────────────┐
                                 │   │ exotel/ dial + events│   │ sarvam/ STT LLM  │
                                 │   │ (verified REST calls)│   │ TTS (existing)   │
                                 │   └──────────────────────┘   └──────────────────┘
```

The pipeline order above is **fixed and already proven** in `pipeline.py`; Phase Six adds to it
rather than rearranging it.

---

## 3. Module responsibilities

### Existing modules — reused, not rewritten

| Module | Responsibility kept |
|---|---|
| `config.py` | Single validated source of settings; `.env` wins over shell; placeholder-school refusal; stream-URL rules; UTF-8 console |
| `speech_text.py` | Indian-English number words (lakh/crore), amount parsing, `DigitWordBuffer`, prompt-text hygiene (`{{ }}` stripping, length caps) |
| `name_speech.py` | English→Telugu-script names with a hand-editable cache and safe fallback |
| `sarvam_stt.py` | `saaras:v3-realtime` raw 8 kHz PCM → final transcripts, per-message isolation, auto-reconnect |
| `sarvam_tts.py` | `bulbul:v3` streaming speech, config-fallback, digit-safe text, pre-render path |
| `sarvam_llm.py` | Prompt assembly with one-pass `{{placeholder}}` rendering and leftover refusal |
| `pipeline.py` | Pipeline construction, `OpenerGate`, `TextFilter`, `HangupSignal` |
| `recorder.py` | Turn/​mark recording, latency-first/transcript-last atomic writes |
| `server.py` | FastAPI WebSocket endpoint, token-before-accept, `/health`, call and idle limits |
| `make_call.py` | CLI dialling and input validation (becomes a thin wrapper over `cli/dial.py`) |
| `check.py` | Pre-flight configuration/Sarvam/tunnel probe (never dials) |

### New modules (added alongside, per approved additive layout)

| Module | Responsibility |
|---|---|
| `veda/domain/enums.py` | The **three separate** state models (§4) — no fourth, no mixing |
| `veda/domain/models.py` | Frozen dataclasses: `ReminderTask`, `CallAttempt`, `CallResult`, `CallMetric`, `StudentFact` |
| `veda/domain/events.py` | Typed internal events (`CallAnswered`, `GreetingFinished`, `IdentityAnswered`, `UserTurnFinal`, `InterruptionDetected`, `SilenceTimeout`, `CallbackRequested`, `ParentHungUp`, `ProviderError`) |
| `veda/domain/policies.py` | Pure, testable rules: identity gate, date-question counter, DNC, forbidden-content checks, outcome selection |
| `veda/app/task_service.py` | Import→task creation, atomic claiming, dry-run eligibility listing |
| `veda/app/call_controller.py` | Call lifecycle: claim, dial, timers, start/stop pipeline, finalize once |
| `veda/app/conversation_controller.py` | Explicit Python state machine for the conversation phase |
| `veda/app/response_service.py` | Deterministic rule → approved template → LLM fallback → policy validation |
| `veda/app/outcome_service.py` | Single writer of the final outcome, structured result fields and metrics |
| `veda/integrations/database/` | asyncpg pool, repositories, transaction helper, Alembic migrations |
| `veda/integrations/exotel/` | Dial client, status polling, event validation, `NOT_DIALED` / `UNCERTAIN_NEEDS_REVIEW` classification |
| `veda/ingestion/document_parser.py` | `.xlsx` (openpyxl), `.xls` (xlrd), `.csv` (stdlib) → uniform row stream |
| `veda/ingestion/validation.py` | Per-row validation and quarantine reasons (never dials a rejected row) |
| `veda/ingestion/deduplication.py` | Identity normalisation and duplicate/ambiguous detection |
| `veda/ingestion/staff_export.py` | §7.9 CSV export (promised dates, callbacks, principal requests, DNC) |
| `veda/cache/audio_cache.py` | Static audio metadata + local PCM files, validation, invalidation |
| `veda/cache/response_cache.py` | Approved text templates only — never personalised fee audio |
| `veda/observability/metrics.py` | t0–t5 timings, usage and cost aggregation, percentiles |
| `veda/observability/audit.py` | Privacy-safe structured logging with correlation IDs |

**Boundary rule.** Business rules never live inside audio-frame callbacks; `pipeline.py` emits
events, `app/conversation_controller.py` decides.

---

## 4. The three state models (never one enum)

### 4.1 `CallLifecycleState` — the phone line

`CREATED → DIALING → RINGING → ANSWERED → IN_PROGRESS → ENDING → ENDED`
plus terminal-by-exception paths: any state `→ ENDED` on provider failure, timeout, cancellation
or hangup. `ENDED` is terminal; nothing may transition out of it.

### 4.2 `ConversationPhase` — what Veda is doing

`PLAYING_GREETING → WAITING_FOR_IDENTITY_RESPONSE → PLAYING_FEE_REMINDER → LISTENING →
FINALIZING_TRANSCRIPT → CLASSIFYING_INTENT → CHECKING_RESPONSE_CACHE → (GENERATING_RESPONSE →
SYNTHESIZING_RESPONSE) | (SYNTHESIZING_RESPONSE) → PLAYING_RESPONSE → WAITING_FOR_PARENT_TURN →
LISTENING` and, at the end, `CLOSING`. `LISTENING` and `WAITING_FOR_PARENT_TURN` are one wait
seen from two points; they will be collapsed to `LISTENING` in implementation to avoid a state
that never differs (recorded as a deliberate simplification under brief §10's "avoid a separate
state for every trivial operation").

### 4.3 `Outcome` + reason code — set exactly once

| Outcome | Reason codes |
|---|---|
| `COMPLETED` | `date_promised`, `no_date_given`, `already_paid`, `principal_requested`, `financial_reason`, `parent_declined` |
| `NO_RESPONSE` | `ring_timeout`, `parent_silent` |
| `VOICEMAIL` | — (only when reliably detectable; see §7 assumptions) |
| `CALLBACK_REQUESTED` | — (an outcome, **not** permission to dial again) |
| `WRONG_NUMBER`, `NOT_PARENT`, `DO_NOT_CALL` | — |
| `UNCERTAIN_NEEDS_REVIEW` | `dial_result_unknown` |
| `FAILED`, `CANCELLED` | sanitised category |

The outcome is written once; the database rejects any later change (see data model §6).

### 4.4 Transition table (shape used for every phase transition)

Every transition in `conversation_controller.py` records: current phase, triggering event, guard
conditions, side effects, next phase, timeout behaviour, failure handling, and whether it is
terminal. Guards are pure functions in `policies.py` so they are unit-testable with no I/O.

---

## 5. Call lifecycle, end to end

1. **Import** — workbook parsed, each row validated, duplicates detected; accepted rows become
   `student` + `reminder_task` rows; rejected rows are quarantined with reasons. Nothing dials.
2. **Preview** — `preview` lists eligible tasks (status, phone tail, amount words, opening lines)
   with no provider contact at all.
3. **Claim** — `call_controller` atomically claims one task (`FOR UPDATE SKIP LOCKED` +
   `dial_state` flip). The database cap (data model §5) makes a second attempt impossible.
4. **Pre-render** — the two opening lines are rendered from cache if present, else synthesised
   once and cached. Failure falls back to the live opener and never blocks the call.
5. **Dial** — `exotel/` posts the call with Basic auth and `StreamUrl`. The result is classified:
   `NOT_DIALED` (definitely no call exists → may be dialled later after the cause is fixed) or
   `UNCERTAIN_NEEDS_REVIEW` (unknown → never retried automatically, surfaced for review).
6. **Answer** — the transport delivers the handshake; the internal attempt is matched to the
   provider call id; `ANSWERED`.
7. **Opening** — the two lines play in order with the configured pause between them. Parent audio
   is buffered and not interpreted; the gate opens when the identity question has finished.
8. **Conversation** — per turn: finalise transcript → classify → deterministic rule or cache →
   otherwise LLM → policy validation → TTS → audio. Interruptions clear the outgoing queue.
9. **Close** — Veda ends the call herself after her goodbye, or the parent hangs up, or a timeout
   fires.
10. **Finalize (idempotent)** — outcome once, structured result fields, metrics, correlation ids.
    Safe to call again after a crash without changing the outcome.

---

## 6. Event-driven decision points (§10.3)

The decision layer is invoked only for: call answered, greeting finished, identity response
finalised, parent turn finalised, safe intent identified, LLM response ready, parent interruption,
silence timeout reached, callback requested, parent hung up, provider error. Never per audio frame.

---

## 7. Latency instrumentation (§15), mapped to what is actually observable

| Marker | Meaning | Observable today | Action |
|---|---|---|---|
| `t0` | last relevant parent audio frame reaches the server | yes (input frame time) | add mark `last_input_audio` |
| `t1` | end-of-turn detection | yes (`user_stopped_speaking`) | exists |
| `t2` | final STT transcript | yes (`user_final`) | exists |
| `t3` | first LLM token | yes (`llm_first_token`) | exists |
| `t4` | first usable TTS audio chunk | yes (`first_tts_audio`) | exists |
| `t5` | first outgoing audio handed to the transport | **not marked today** | add mark `first_outgoing_audio` |

`t5` is deliberately named "handed to the transport". Nothing in this system may claim
parent-audible playback; the provider gives no evidence of it. Cache-hit paths record cache
timings and must not fabricate LLM or TTS numbers. Missing provider data is recorded as
unavailable, never estimated.

---

## 8. Provider interfaces

```python
class TelephonyClient(Protocol):
    async def dial(self, *, to_e164: str, stream_url: str, time_limit_secs: int) -> DialResult: ...
    async def status(self, provider_call_id: str) -> ProviderStatus: ...
    async def hangup(self, provider_call_id: str) -> None: ...

@dataclass(frozen=True)
class DialResult:
    state: DialState          # NOT_DIALED | UNCERTAIN_NEEDS_REVIEW | DIALED
    provider_call_id: str | None
    detail: str               # sanitised, never a credential
```

STT, LLM and TTS stay as the existing Pipecat `FrameProcessor` adapters; Phase Four wraps them
with timeouts and error categories rather than replacing them.

**Exotel facts actually verified so far:** the REST dial in `make_call.py` (`POST
/v1/Accounts/{sid}/Calls/connect`, Basic auth, `From`/`CallerId`/`StreamUrl`/`StreamType=bidirectional`/
`TimeLimit`) and status polling (`GET .../Calls/{sid}.json`) are exercised by the project's live
configuration. **Not verified:** Exotel webhook/callback signatures and payload semantics. Stage 1
therefore keeps **status polling as the primary, verified path** and treats webhooks as an
unverified path that must not be enabled until its signature mechanism is confirmed against the
provider's documentation. No signature algorithm will be invented.

---

## 9. Concurrency and resources

Bounded simultaneous calls (`MAX_SIMULTANEOUS_CALLS`), bounded provider requests, bounded audio
buffers with an explicit overflow policy, per-call isolated state, structured cancellation with
cleanup in `finally`, and database-safe claiming. No transaction is held open across a provider
request. The no-duplicate-call rule is enforced by storage, never by an in-process lock alone.

---

## 10. Cache design (§13)

**Cached (audio):** the two fixed opening lines, a small rotating set of closing lines, and the
silence check-in. **Nothing else** — §13.7 exists because reusing audio for ordinary replies makes
calls sound scripted.

**Response cache (text):** approved templates with placeholders, populated only from validated
current data. A matching intent never authorises reusing a previously generated personalised fee
sentence.

**Cache key** (audio), all components required:
`content_sha256 · language · voice · model · pace · temperature · audio_format · sample_rate ·
template_version`

**Lookup order:** metadata exists → path resolves inside the approved cache directory → file
exists, readable, non-empty → checksum and format valid → else reject, fall back to live TTS,
record a cache-miss/validation-failure metric. A cache failure must never expose another family's
audio or block the call.

---

## 11. Configuration additions (validated at startup, bounds enforced)

Kept as-is: every existing key in `config.py`, including `SARVAM_LLM_MODEL`, `saaras:v3`,
`STORE_TRANSCRIPTS` defaulting to `false`, `MAX_ATTEMPTS_PER_TASK=1`, `STREAM_TOKEN`, `HOST=127.0.0.1`.

Added:

| Key | Default | Range / rule |
|---|---|---|
| `DATABASE_URL` | — | required in production mode; asyncpg DSN |
| `DB_POOL_MIN` / `DB_POOL_MAX` | 1 / 5 | min ≥ 1, max ≤ 20 |
| `DB_STATEMENT_TIMEOUT_MS` | 10000 | 100–120000 |
| `SCHOOL_TIMEZONE` | `Asia/Kolkata` | valid IANA name |
| `MAX_SIMULTANEOUS_CALLS` | 2 | 1–10 |
| `RING_TIMEOUT_SECS` | 45 | 10–180 |
| `STT_FINALIZE_TIMEOUT_SECS` | 1.5 | 0.3–5 |
| `LLM_REQUEST_TIMEOUT_SECS` | 20 | 5–60 |
| `TTS_TIMEOUT_SECS` | 20 | 5–60 |
| `MAX_RESPONSE_CHARS` | 320 | 60–600 |
| `MAX_CHUNK_BUFFER_CHARS` | 400 | 100–1000 |
| `OPENER_PAUSE_SECS` | **0.35** | 0–2 (see conflict note below) |
| `IMPORT_DIR` / `EXPORTS_DIR` / `AUDIO_CACHE_DIR` | `imports/`, `exports/`, `audio_cache/` | must resolve inside the app directory |
| `DIAL_DRY_RUN` | `true` | real dialling additionally requires `--real-dial` |
| `ENV` | `development` | `development` \| `production` |
| `LOG_LEVEL` | `INFO` | DEBUG–ERROR |
| `TRANSCRIPT_RETENTION_DAYS` | 0 | 0 = nothing stored (Stage 1) |

**Conflict to report.** Brief §10.1 [ADDED] names the pause `OPENER_PAUSE_SECS` with a default of
`0.7 s`; the working code uses a hard-coded `OPENER_GAP_SECS = 0.35`. Both cannot be true. Phase Two
proposes to expose it as `OPENER_PAUSE_SECS` and **default it to the measured 0.35 s**, because
0.35 was tuned against real calls and 0.7 is an untested suggestion. The owner can raise it in
`.env` without a code change. Flagged for confirmation.

**Deliberate non-change.** `SARVAM_STT_MODEL` stays `saaras:v3`; `saaras:v4` is only adopted after a
documented live comparison.

---

## 12. Test boundaries

| Layer | Location | Rules |
|---|---|---|
| Unit | `veda/tests/unit/` | pure functions only — policies, counters, formatting, state transitions; no network, no DB |
| Integration | `veda/tests/integration/` | real PostgreSQL (test database), mocked providers; migrations applied from clean |
| Security | `veda/tests/security/` | forged/duplicate events, unauthorised dialling, malformed uploads, path traversal, injection, log leakage, prompt injection, cross-family data access |
| Provider sandbox | `veda/tests/sandbox/` | only runs with explicit credentials; never during ordinary test runs |
| Live | documented manual procedure | never automated; never dials during test runs |

Labels are mandatory in reporting: **mock**, **sandbox**, **live**. Existing 183 root tests keep
running; the failing opener-cache test is fixed in Phase Seven where that path is built out.
Missing coverage identified in the audit (`server.py`, `pipeline.py`, `make_call.py`) is added in
Phases Three to Six, and the two inaccurate docstring/README claims are corrected.

---

## 13. Stage 2 integration boundaries

Business logic stays transport-free so a frontend can be added without touching the controller:
`task_service` (create/list/claim), `outcome_service` (read results/metrics), `staff_export`,
`import` validation results, and `conversation_controller` transitions are all callable from an API
layer later. Stage 1 adds no frontend, no Redis, no multi-tenant platform, and no cloud audio
storage.

---

## 14. Deliberate deviations with reasons

1. **Keep `saaras:v3`** rather than the brief's `saaras:v4` — v4 is untested; §4.4 forbids silently
   replacing a working configuration.
2. **Keep `SARVAM_LLM_MODEL`** as the primary key, accept `SARVAM_CHAT_MODEL` as a documented alias —
   the brief's §4.5 [ADDED] note requires the working `.env` not to break.
3. **Keep status polling** as the primary call-status path — it is verified; webhook signatures are not.
4. **`OPENER_PAUSE_SECS` default 0.35**, not 0.7 — measured vs suggested (see §11).
5. **Collapse `WAITING_FOR_PARENT_TURN` into `LISTENING`** — brief §10 permits avoiding states that
   never differ.
6. **Keep the flat root modules** and add packages alongside — the lowest-risk route to Stage 1.

## 15. Phase Two acceptance criteria

* Three state models defined separately, with every brief-named state assigned to exactly one. ✅ §4
* Outcome set-once and no-duplicate-attempt rules expressed as **database** constraints. ✅ data model §5–6
* Schema documented before implementation, normalised, with FKs, unique constraints, indexes and
  timezone-aware timestamps. ✅ data model
* Provider interfaces and the verified-vs-unverified split stated. ✅ §8
* Configuration list with defaults and validation ranges. ✅ §11
* Cache metadata composition and invalidation rules. ✅ §10
* Test boundaries and labelling rules. ✅ §12
* Every reconstructed brief section flagged as an assumption. ✅ assumptions doc
* No behaviour change to the working system. ✅ (documents only)
