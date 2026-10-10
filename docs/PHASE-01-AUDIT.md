# Veda Stage 1 — Phase One Report (Repository Audit)

Status: **audit complete, awaiting approval to start Phase Two**
Date: 2026-10-10
Phase One is audit-only. **No existing file was modified.** The only file added is this report.

---

## 0. Scope of this phase, and what was deliberately not done

Done: inspected the real project, ran its real tests, recorded exact dependency versions,
mapped reusable code, listed security findings, listed conflicts between the brief and the
code, and proposed a target architecture with acceptance criteria.

Not done (and not claimed): no code was written, no refactor started, no call was placed,
no database was created, no credentials were read into this report, no test was "fixed".

---

## 1. Where the code actually is

| Location | What it is | Verdict |
|---|---|---|
| `Desktop/veda/veda/` | The working "talking core": 13 modules, 8 test files, `.venv`, live `.env` | **This is the project. Extend it in place.** |
| `Desktop/veda stage 1 ( freebuff)/` | The agent's thread workspace. Empty except `.freebuff/project-id` | Not used for code (owner chose in-place build) |
| `Desktop/voice-agent s-3/` | Older Plivo-based attempt, mock-mode default, "does not place a real phone call" | **Ignore.** Not the working core |
| `Desktop/veda.zip`, `Desktop/veda(exotel).zip` | Snapshots of `veda/veda` dated 2026-10-06 | Reference only |
| `Desktop/school files/` | Manuals, architecture PDFs, frontend mockups, **plaintext API-key files** | Docs only; secrets need consolidating |

The brief's description of "the existing project" (file-based pending-call store, JSON
transcripts and timing files, command-line dialer, custom Sarvam STT/TTS adapters, name
conversion to Telugu script, English number words, tested conversation prompt) matches
`Desktop/veda/veda/` exactly.

`Desktop/veda/veda/` is **not** a git repository (no `.git`). There is currently no
version-control safety net for the working system.

---

## 2. Verified baseline (evidence, not a claim)

Command actually run, in `Desktop/veda/veda/`:

```
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Actual result:

```
Ran 183 tests in 0.699s
FAILED (failures=1)
```

* Python **3.13.14** (`py -0p` shows only 3.13 installed).
* **1 failing test:** `tests/test_tts.py::SynthesizeTest::test_audio_is_collected_for_the_cached_opener`.
  **Root cause: the test stub, not the product.** `sarvam_tts.py::synthesize()` reads the
  socket with `await connection.recv()`, but `tests/stubs.py::FakeConnection` only implements
  `__aiter__`/`__anext__`, so the read raises and the method returns empty bytes. The real
  `websockets` connection does have `.recv()`, so the live path is unaffected. This code path
  only runs when `OPENER_PRERENDER=true`, which is off by default.
  Fix belongs to Phase Seven (opener cache), where the pre-render path is actually built out.

### Coverage gaps found (the code's own docstring overclaims)

* `server.py` says "the offline tests can drive the whole accept/refuse/save path". **No test
  file imports `pipeline` or calls `handle_stream` / `create_app`.** There is no
  `test_server.py`, no `test_pipeline.py`, no `test_make_call.py`.
* The dialer's `normalise_phone`, `ensure_callable` and `find_transcript` are untested.
* All 183 tests live in: `test_config` (39), `test_prompt` (31), `test_call_store` (22),
  `test_recorder` (21), `test_speech_text` (21), `test_tts` (20), `test_name_speech` (15),
  `test_stt` (14).
* Action: correct the overclaim in `server.py`/`README.md`, and add the missing coverage in
  Phase Three onward rather than repeating the claim.

---

## 3. Dependency versions actually installed (from `pip freeze`)

`pipecat-ai==1.12.0`, `fastapi==0.142.2`, `uvicorn==0.54.0`, `websockets==14.2`,
`httpx==0.28.1`, `sarvamai==0.1.28`, `loguru==0.7.3`, `python-dotenv==1.2.4`,
`numpy==2.5.3`, `pydantic==2.13.5`, `pipecat-ai-prebuilt==1.3.0`.

**Absent and needed later:** `openpyxl` (`.xlsx`), `xlrd` (legacy `.xls`), `SQLAlchemy`,
`alembic`, `asyncpg`. `requirements.txt` pins `pipecat-ai==1.12.0` and will be extended,
not upgraded blindly.

## 4. Environment facts for the database phase

* **PostgreSQL is not installed** (`psql`/`pg_config` not found); **Docker is not installed**.
* `cloudflared 2026.3+` present (reported version `2026.9.3`); Git 2.50.1 present.
* Per the brief's Appendix A.19, use **asyncpg**, never psycopg3 async, on Windows.

---

## 5. Security findings

1. **Rotate `STREAM_TOKEN` now.** During the audit, one command printed `EXOTEL_STREAM_URL`
   in full, which contains the token query value. That value is now exposed and must be
   treated as compromised. It is deliberately **not repeated** here. Rotating it means: generate
   a new value, put it in `.env`, and make the new `EXOTEL_STREAM_URL` end with
   `?token=<the new value>`.
2. **Credentials are scattered in plaintext on this machine**: `veda/veda/.env` (live Exotel,
   Twilio and Sarvam credentials), `school files/ai voice agent architure/api keys.txt`,
   `school files/medicaer api keys file.txt`, `school files/voice-agent s-3/.env`, plus
   recovery-code text files on the Desktop. None were opened. They need consolidating into one
   location, and any credential that has ever been shared or zipped must be rotated.
3. **No version control** on the working project (see §1).
4. **Already correct and worth preserving**: server binds `127.0.0.1` only; the stream token is
   verified *before* `websocket.accept()`; `/health` returns only `{ok, service, active_calls}`
   and never the tunnel address; `Settings.safe_summary()` masks every secret; `.gitignore`
   excludes `.env`, `transcripts/`, `.pending_calls/`, `opener_cache/` and the name cache.

---

## 6. Appendix A pitfalls: already handled in the code

P1 (TranscriptionFrame timestamp + per-message try/except + auto-reconnect) — `sarvam_stt.py`;
P2 (idle + max call duration enforced by the server) — `server.py::_limit_call`;
P3 (three recorder positions) — `recorder.py` + `pipeline.py`;
P4 (latency file first, transcript last, atomic) — `recorder.py::save`;
P10/P11/P12/P13 (one pending file per call, no "oldest pending" fallback, dialer removes only
its own file, never guess among several transcripts) — `call_store.py`, `make_call.py`;
P15 (no demo values; refuse to dial without a real school name) — `config.py`, `make_call.py`;
P16/P29 (stream URL rules, doubled scheme, sample-rate 8000) — `config.py`;
P17/P30 (token before accept, `127.0.0.1`, health leaks nothing) — `server.py`;
P25 (digits → English words) — `speech_text.py`; P27 (names → Telugu script) — `name_speech.py`;
P31 (UTF-8 console) — `config.py::configure_console`.

**Still open in the code:** P7/P8/P9-class tuning is configurable but only proven by live calls;
P18 (200 ms packets, queue-clear on barge-in) is implemented at packet size but the
queue-clear behaviour is unverified live; P20 (frames before `StartFrame` are dropped) is
handled by the opener-gate design but unverified live; P21 (mocks never prove a voice product)
is respected by labelling everything below honestly.

---

## 7. Conflicts between the brief and the working code (resolved to the brief's [ADDED] rules)

| Topic | Brief | Working code | Resolution |
|---|---|---|---|
| LLM model key | `SARVAM_CHAT_MODEL=sarvam-105b-conversations` | `SARVAM_LLM_MODEL` | Keep `SARVAM_LLM_MODEL`; accept `SARVAM_CHAT_MODEL` as a documented alias |
| STT model | `saaras:v4` | `saaras:v3` + `-realtime` suffix | **Keep `v3`.** `v4` is unverified; switching only after a documented live test |
| Openings vs barge-in | §11.3 barge-in everywhere | `OpenerGate` makes the opener non-interruptible | Opener wins (§10.2 [ADDED]); barge-in applies to every reply *after* the opening |
| Persisting transcripts | §17.8 optional pilot feature | Always writes transcript JSON | **`STORE_TRANSCRIPTS=false` default**; outcome + metrics persisted, transcript text not |
| Pending calls | PostgreSQL tables | one JSON file per call | Replace storage with DB tables, **keep the exact claim/refusal semantics** |
| Behavioural rules | prompt-only in §16 | prompt + code guards | Keep both; add the §12.4 deterministic guards (identity gate, date-question counter, stop/DNC, wrong number, silence, forbidden-content check) |

## 8. Gaps in the brief itself (raised, not silently filled)

1. Sections **7.4–7.9, 8 and 9 were not included** in the brief, yet §7.9 (the staff export),
   §8.4 (name conversion), §9.7 (when Veda may say something was noted) and the structured
   result fields (`promised_date`, `callback_time_text`, `principal_request`,
   `already_paid_claim`, `do_not_call`) are referenced later. §29 also refers to delivery-report
   items 30–41 that were not included. **Owner decision: reconstruct them and flag every
   assumption** in an assumption register (Phase Two), each entry clearly marked for review.
2. **No fee workbook exists anywhere** on this machine, so §0.3's "inspect the real workbook
   during Phase One" cannot be performed. Owner decision: **build a workbook-upload feature**
   so the school's own fee file can be supplied at run time, accepting **`.xlsx`, `.xls` and
   `.csv`**, validated by content rather than file extension.
3. §0.3 already resolves language (Telugu + common English only), input format (Excel),
   environment (Windows/PowerShell), attempts (`MAX_ATTEMPTS_PER_TASK=1`, no same-day retry),
   and transcripts (do not persist). These are treated as closed.

---

## 9. Proposed target architecture

The existing modules sit flat at the project root and are proven. Two options:

**Option A — extend in place, add namespaced packages alongside (RECOMMENDED).**
Keep `config.py`, `speech_text.py`, `name_speech.py`, `sarvam_stt.py`, `sarvam_tts.py`,
`sarvam_llm.py`, `pipeline.py`, `recorder.py`, `server.py`, `make_call.py`, `check.py`
untouched at the root, and add:

```
veda/domain/        enums.py (3 separate models), models.py, events.py, policies.py
veda/app/           call_controller.py, conversation_controller.py, task_service.py,
                    response_service.py, outcome_service.py
veda/integrations/  database/ (asyncpg pool, repositories, Alembic), exotel/ (dial + events)
veda/ingestion/     document_parser.py (.xlsx/.xls/.csv), validation.py, deduplication.py,
                    staff_export.py
veda/cache/         audio_cache.py (opener, rotating closings, silence check-in only),
                    response_cache.py
veda/cli/           import_file, preview, dial, status, outcomes, export, metrics,
                    validate_audio, serve
veda/observability/ metrics.py, audit.py
migrations/         Alembic revisions
docs/               this report, assumptions register, runbook, schema, threat model
```

**Option B — restructure everything into an `app/` package.** Cleaner in the abstract, but it
moves every proven file and rewrites every import, which is exactly the "large unreviewed
rewrite" §29 forbids. **Recommendation: Option A.**

### The three state models stay three (per §10 [ADDED])

* `CallLifecycleState`: CREATED, DIALING, RINGING, ANSWERED, IN_PROGRESS, ENDING, ENDED
* `ConversationPhase`: PLAYING_GREETING, WAITING_FOR_IDENTITY_RESPONSE, PLAYING_FEE_REMINDER,
  LISTENING, FINALIZING_TRANSCRIPT, CLASSIFYING_INTENT, CHECKING_RESPONSE_CACHE,
  GENERATING_RESPONSE, SYNTHESIZING_RESPONSE, PLAYING_RESPONSE, WAITING_FOR_PARENT_TURN, CLOSING
* `Outcome` (+ reason): COMPLETED (date_promised / no_date_given / already_paid /
  principal_requested / financial_reason / parent_declined), NO_RESPONSE (ring_timeout /
  parent_silent), VOICEMAIL, CALLBACK_REQUESTED, WRONG_NUMBER, NOT_PARENT, DO_NOT_CALL,
  UNCERTAIN_NEEDS_REVIEW, FAILED, CANCELLED. Set exactly once, never overwritten.

### Reuse map

| Existing | Becomes |
|---|---|
| `call_store.py` pending files | `reminder_task` + `call_attempt` tables (same refusal semantics; `UNIQUE` constraint enforces one attempt per task) |
| `recorder.py` transcript/latency JSON | `call_outcome` + `call_metric` rows; transcript text dropped unless a future explicit approval |
| `make_call.py` CLI | `veda/cli/dial.py` (dry-run default, explicit `--real-dial` flag, task ids from DB) |
| `sarvam_*` adapters, `pipeline.py`, `name_speech.py`, `speech_text.py`, `server.py` | Reused as-is where possible; wrapped, not rewritten |

---

## 10. Phase plan with acceptance criteria

Every phase ends with: files changed, exact commands run, real output, what was mocked vs live,
open blockers, risks — then a stop for approval.

| Phase | Deliverable | Acceptance criteria |
|---|---|---|
| 1 (**done**) | This report | Audit reported; blocking questions resolved |
| 2 | Architecture + data model + assumption register | 3 separate state models; schema with `UNIQUE(task_id)` on attempts; outcome set-once rule; provider interfaces; config list; register of every reconstructed §7.4–9 assumption |
| 3 | Persistence + workbook upload | Alembic migrates a clean DB; `.xlsx`/`.xls`/`.csv` import with per-row validation and quarantine; duplicate tasks rejected by constraint; atomic attempt creation proven under concurrency; **plus exact PowerShell PostgreSQL install steps for the owner** |
| 4 | Provider integration | Exotel dial + event validation against the real API contract; STT/LLM/TTS adapters wrapped and error/timeout tested; Pipecat transport compatibility stated honestly (mocks labelled) |
| 5 | Conversation controller | Identity gate, date-question counter, stop/DNC, wrong number, silence, forbidden-content checks all in deterministic Python with tests; LLM cannot trigger any action |
| 6 | Real-time voice pipeline | Non-interruptible opener + identity question always plays; VAD activation order; turn finalization; barge-in with queue clear; chunking; bounded buffers; cancellation |
| **milestone** | One authorized real call | Dry-run preview first; fake data only; dial → opener → identity gate → one parent turn → reply → terminal outcome + metric row persisted. If not achievable, report **BLOCKED**, never faked |
| 7 | Audio cache | Metadata in Postgres, bytes on disk; invalidation; validated fallback; fixed opener/closings/check-in only; cache metrics. Also fixes the 1 currently failing test |
| 8 | Call control + recovery | DB-level one-attempt rule proven; ring timeout; idempotent finalization; restart reconciliation with no blind redial |
| 9 | Security + observability | Secret handling, stream authentication, operational-endpoint protection, DB permissions, filesystem safety, privacy-safe logs, latency/cost metrics, security tests with residual risks documented (no "zero vulnerabilities" claims) |
| 10 | Verification + delivery | Full test run; clean-state migration; startup/shutdown; recovery; Windows runbook (copy-and-paste PowerShell); final report separating mocked from live evidence |

---

## 11. Blocking questions — resolved by the owner on 2026-10-10

1. **Where to build:** in place, in `Desktop/veda/veda`. (Tooling confirmed able to edit there.)
2. **PostgreSQL:** owner installs it using PowerShell steps that this project will provide;
   DB-backed integration tests stay blocked until it exists.
3. **Fee workbook:** build a runtime upload feature accepting `.xlsx`, `.xls` and `.csv`.
4. **Missing brief sections 7.4–7.9, 8, 9:** reconstruct and flag every assumption.

## 12. Still open (not blocking Phase Two)

* The real fee workbook's actual headers and sheet names (needed before the importer is finalised).
* Whether §7.9's staff export should be CSV, and where it should be written (Phase Two proposes CSV
  in an `exports/` folder and flags it as an assumption).
* Rotation of the exposed `STREAM_TOKEN` (owner action) and consolidation of the scattered
  plaintext credential files.

---

## Approval gate

Phase One is complete. **Phase Two will not begin until the owner approves it.** Phase Two writes
design documents and the assumption register only — no behaviour change to the working system.
