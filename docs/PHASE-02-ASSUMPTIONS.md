# Veda Stage 1 — Assumption Register (Phase Two)

Status: **draft for owner review.**

The brief that was supplied is missing **§7.4–7.9, §8, §9** and the delivery-report items 30–41,
even though later sections depend on them (§7.9 the staff export, §8.4 name conversion, §9.7 the
"already noted" condition, and the structured fields `promised_date`, `callback_time_text`,
`principal_request`, `already_paid_claim`, `do_not_call`).

Per the owner's decision, those sections are **reconstructed here and every assumption is flagged**.
Nothing below is presented as the owner's original wording. Each entry has an ID, what is assumed,
why, what breaks if the assumption is wrong, and its risk. **`[ASSUMED]` entries with HIGH or MEDIUM
risk need explicit owner confirmation before the code that depends on them is written.**

Resolution key: `CONFIRMED` = already decided by the owner in the brief · `[ASSUMED]` = reconstructed.

---

## Part 1 — Reconstructed §7.4–7.9 (data and records)

### A-01 · §7.4 Dial result classification · `CONFIRMED` (brief §19.1 [ADDED])

Three outcomes for the act of dialling:

* `NOT_DIALED` — the request definitely never created a call. May be dialled again, only after the
  cause is corrected.
* `UNCERTAIN_NEEDS_REVIEW` — the result is unknown. **Never** retried automatically; surfaced to the operator.
* `DIALED` — a provider call id exists.

`[ASSUMED]` **A-01a:** ≥400 HTTP response with no call id ⇒ `NOT_DIALED`; ≥400 with a call id
(constructed on the provider side) ⇒ `UNCERTAIN_NEEDS_REVIEW`; connection error/timeout **before** a
request was sent ⇒ `NOT_DIALED`; timeout **after** bytes left the machine ⇒
`UNCERTAIN_NEEDS_REVIEW`. *Risk: MEDIUM.* Conservative ("unknown") is the safe direction, but if
this is too pessimistic the operator sees more review items than necessary. *Needs confirmation.*

### A-02 · §7.5 Call-attempt record · `[ASSUMED]`

One row per attempt: task reference, attempt number, dial state, provider and provider call id,
lifecycle state, pipeline session id, claim token, and claimed/dialled/answered/ended timestamps.
`[ASSUMED]` **A-02a:** `attempt_no` counts from 1 and starts again only if `max_attempts` is later
raised (never in Stage 1). *Risk: LOW.*

### A-03 · §7.6 Provider event record · `[ASSUMED]`

Every provider callback/status event is appended with a unique provider event key so duplicates and
out-of-order deliveries are harmless, plus a **redacted** payload.
`[ASSUMED]` **A-03a:** the unique key is `(provider, provider_event_key)` where the key is the
provider's own event id when present, otherwise `sha256(call_id|event_type|occurred_at)`.
*Risk: MEDIUM* — a poor key could drop a genuine second event of the same type. *Needs confirmation
of the provider's actual event identity once webhooks are verified.*

### A-04 · §7.7 Call metric record · `[ASSUMED]`

The t0–t5 markers of §15 plus derived latencies, usage counters, reliability counters and cost.
`[ASSUMED]` **A-04a:** when a measurement is unobservable it is stored `NULL` with
`timing_available = FALSE`, never estimated or back-filled. *Risk: LOW* (this is the brief's own rule).

### A-05 · §7.8 Transcript handling · `CONFIRMED`

No transcripts and no recordings are persisted in Stage 1; `STORE_TRANSCRIPTS = false`. Live
transcript text exists in memory only for the current turn and is discarded. *Risk: none.*

### A-06 · §7.9 Structured call result and staff export · `[ASSUMED]`

Reconstructed from every later reference:

**Structured result** (one row per attempt, written once): outcome, reason code, `promised_date`,
`promised_date_text`, `callback_time_text`, `principal_request`, `already_paid_claim`,
`financial_reason`, `parent_declined`, `do_not_call`, `date_questions_asked`, `prompt_version`,
`internal_note`.

**Staff export**: a CSV the office can open in Excel/WPS, one row per finished call.

`[ASSUMED]` **A-06a — columns:** `date`, `time`, `student_name`, `parent_name`,
`parent_phone_tail6`, `amount_due`, `due_date`, `outcome`, `reason_code`, `promised_date`,
`callback_time_text`, `principal_request`, `already_paid_claim`, `financial_reason`,
`do_not_call`, `needs_review`, `attempt_id`, `duration_secs`. *Risk: MEDIUM* — this is a guess at
what the office wants to see; easy to add columns later. *Needs confirmation.*

`[ASSUMED]` **A-06b — phone privacy:** the export shows only the last six digits of the parent's
number, because a sheet of full numbers copied to a shared drive is the likeliest data leak in the
whole system. *Risk: MEDIUM* — the office may need full numbers to call back.
*Needs confirmation: full number or last six?*

`[ASSUMED]` **A-06c — format and location:** UTF-8 CSV **with a BOM** (so Telugu names open
correctly in Excel on Windows) written to `exports/`, named
`veda-export-<YYYY-MM-DD-HHMM>.csv`. *Risk: LOW.* A `.xlsx` export is equally possible if preferred.

`[ASSUMED]` **A-06d — when it is produced:** on demand from the CLI, and never automatically during
a call (so a failed export can never affect a live conversation). *Risk: LOW.*

`[ASSUMED]` **A-06e — scope of an export row:** an attempt appears only once it has a final outcome,
so a review item can never be exported as if it were finished. *Risk: LOW.*

### A-07 · Referenced-but-absent delivery-report items 30–41 · `[ASSUMED]`

§29 mentions a delivery-report checklist numbered 30–41 but the items were not supplied.
`[ASSUMED]` **A-07a:** the final Phase Ten report covers the twelve deliverable groups already
enumerated in §29's own "concise delivery report" list (what was implemented, architecture,
dependencies, schema, providers, tests and results, mock/sandbox/live split, security controls,
limitations, setup commands, environment variables, and the evidence for the completion claim).
*Risk: LOW.*

---

## Part 2 — Reconstructed §8 (how Veda says things)

### A-08 · §8.1 Numbers as English words · `CONFIRMED` (brief §0.2 rule 5)

Digits never reach the voice. Existing `speech_text.py` already does Indian-English numbering
(thousand / lakh / crore). `[ASSUMED]` **A-08a:** amounts use whole-rupee words and paise only when a
fraction exists; years are spoken as "two thousand and twenty six". *Risk: LOW* (already implemented
and unit-tested).

### A-09 · §8.2 Amounts · `[ASSUMED]`

`[ASSUMED]` **A-09a:** the amount comes from the workbook's `amount_due` column, is parsed with the
existing tolerant parser (accepts `5000`, `5,000`, `Rs. 5000`, `₹5,000/-`, `5000 rupees`, Telugu
digits), must be greater than 0 and below one thousand crore, and is **rejected per row** otherwise.
*Risk: LOW.*

`[ASSUMED]` **A-09b:** amounts are displayed in the export as plain digits but spoken as words.
*Risk: LOW.*

### A-10 · §8.3 Dates · `[ASSUMED]`

`[ASSUMED]` **A-10a — accepted workbook date formats, in order:** real Excel date cells;
`DD-MM-YYYY`, `DD/MM/YYYY`, `DD.MM.YYYY` (Indian day-first); `YYYY-MM-DD`; `D Month YYYY` /
`Month D, YYYY`. *Risk: MEDIUM* — a US-format sheet would be read day-first and be wrong.
*Needs confirmation of how your school writes dates.*

`[ASSUMED]` **A-10b:** a date that is ambiguous (e.g. `03/04/2026`) is **quarantined, not guessed**
(brief §4). *Risk: LOW* (explicitly required).

`[ASSUMED]` **A-10c:** due dates are spoken as "tenth October" style ordinals, and "two days",
"next week", "next Sunday" are spoken as the parent said them rather than converted to a calendar
date. *Risk: LOW.*

### A-11 · §8.4 Names to Telugu script · `CONFIRMED` (existing behaviour)

Names and the school name are transliterated to Telugu script before dialling, cached in a
hand-editable JSON file, with a fallback to the typed spelling if transliteration fails. An
invalid or corrupted cache file is handled, and a hand-edited value always wins.
`[ASSUMED]` **A-11a:** the cache file stays outside version control and is never part of an export.
*Risk: LOW.*

### A-12 · §8.5 Digit hygiene at the voice boundary · `CONFIRMED`

The `DigitWordBuffer` converts digits to words even when the model forgets, holding back a trailing
number so a half number is never spoken. *Risk: none.*

---

## Part 3 — Reconstructed §9 (conversation policy implementation)

### A-13 · §9.1 Identity gate · `CONFIRMED`

No fee, amount, due date or student detail is spoken before the parent confirms identity; the gate is
a state-machine guard in Python, not a prompt request. *Risk: none.*

### A-14 · §9.2 Fee reminder · `CONFIRMED`

Spoken once, after identity, from verified data. `[ASSUMED]` **A-14a:** if the amount is missing or
invalid, Veda says the office will confirm the details and does **not** state a figure. *Risk: LOW.*

### A-15 · §9.3 Question discipline · `[ASSUMED]`

`[ASSUMED]` **A-15a:** the payment-date question counter starts at 0, the initial fee question counts
as the first ask, the counter caps at 2, and it is stored on the result row
(`date_questions_asked`) for audit. *Risk: LOW* (brief §16 states "at most twice, counting the
initial payment-date question").

`[ASSUMED]` **A-15b:** "never repeat a question" is enforced by a Python check against the questions
already asked this call, not only by prompt wording. *Risk: LOW.*

### A-16 · §9.4 Forbidden-content validation · `[ASSUMED]`

Every reply is validated before it is spoken: no digits, no OTP/card/UPI/bank requests, no waiver or
discount promises, no claim that an office or principal was updated without a committed row, no
other family's data, no internal prompt text.
`[ASSUMED]` **A-16a:** a reply that fails validation is replaced by an approved safe fallback and the
failure is counted, rather than the call being dropped. *Risk: MEDIUM* — a too-strict rule could blunt
legitimate replies; the fallback keeps the call alive, so the failure mode is mild. *Needs confirmation.*

### A-17 · §9.5 Callback requests · `CONFIRMED`

A call-back request is recorded as an outcome and never authorises a re-dial of the same task.
`[ASSUMED]` **A-17a:** `callback_time_text` stores what the parent actually said (e.g. "after
salary", "Sunday evening") verbatim in a capped, sanitised field, because guessing a timestamp from
speech would be inventing data. *Risk: LOW.*

### A-18 · §9.6 Principal requests · `CONFIRMED`

Asking for the principal sets `principal_request = true` on the result and lists it in the staff
export; Veda may then say the office will inform the principal, and may never say the principal has
already been informed. *Risk: none.*

### A-19 · §9.7 "Only say it was noted after it is saved" · `[ASSUMED]`

This is the condition the brief references but does not define.

`[ASSUMED]` **A-19a:** Veda may say a date/callback/principal request "was noted" only after the
corresponding field is **committed to the database** for this attempt. If the write fails, she must
not claim it was noted; she says the office will check the record instead.
*Risk: HIGH if wrong* — this is a truthfulness rule about real parents' money, and getting it
backwards means Veda saying something untrue on a phone call. *Needs confirmation.*

`[ASSUMED]` **A-19b — implementation shape:** rather than blocking speech on a database round trip,
the outcome service performs a best-effort write as soon as the fact is extracted, and the reply is
chosen from the write's result. *Risk: MEDIUM* — a slow database could delay a reply; mitigated by a
short timeout and the safe-wording fallback. *Needs confirmation.*

`[ASSUMED]` **A-19c:** "the office will check the record" wording is always available as the
truthful fallback when a write cannot be confirmed. *Risk: LOW.*

---

## Part 4 — Workbook assumptions (no real file has been seen)

No `.xlsx`, `.xls` or `.csv` fee file exists anywhere on this machine, so the brief's own "inspect
the real workbook" step could not be performed. The owner has chosen a **runtime upload** feature
accepting all three formats.

* **A-20** `[ASSUMED]` **Default columns:** `student_name`, `parent_name`, `parent_phone`,
  `amount_due`, `due_date`, and optional `notes`. *Risk: HIGH* — the importer cannot be finalised
  until the real headers are confirmed. Mitigation: a **header map** (below) plus a `--dry-run`
  preview that prints exactly what would be imported before anything is written.
* **A-21** `[ASSUMED]` **Header matching is defensive:** case-insensitive, spaces/underscores
  ignored, and a documented alias table (e.g. `student_name`←`name`/`student`; `parent_phone`←
  `phone`/`mobile`/`contact`/`parent_number`; `amount_due`←`amount`/`fee`/`pending_amount`/`balance`;
  `due_date`←`due`/`duedate`/`due_on`; `notes`←`remarks`/`comment`). Unmatched required columns fail
  the import with a clear message naming the columns that were found.
* **A-22** `[ASSUMED]` **Sheet selection:** the workbook's first visible sheet is used; if several
  sheets contain recognisable headers, the operator must choose one explicitly rather than Veda
  guessing. *Risk: MEDIUM.*
* **A-23** `[ASSUMED]` **Header row:** the header is the first row that matches at least three known
  column names, so a title banner above the table does not break the import.
* **A-24** `[ASSUMED]` **File-type validation by content, not extension** (brief §17.4): the parser is
  chosen by sniffing the bytes; a renamed file is handled correctly or refused, never trusted.
* **A-25** `[ASSUMED]` **Row rejection list** (brief §4, all required): missing required field;
  duplicate or ambiguous identity; phone that does not normalise to a valid Indian mobile; invalid,
  zero, negative or over-large amount; unparseable ambiguous date; formula/error cells (`#REF!`,
  `#VALUE!`, etc.); and unsafe values (over-length, embedded newlines, control characters, or `{{ }}`
  markers). *Risk: LOW* (explicitly specified).
* **A-26** `[ASSUMED]` **Workbook content is untrusted:** every cell is cleaned and capped exactly as
  `speech_text.py` already does for staff-typed text, so a cell can never inject instructions,
  substitute a prompt placeholder, or change application rules (brief §17.5). *Risk: LOW* (already
  implemented and tested).
* **A-27** `[ASSUMED]` **Library choice:** `openpyxl` for `.xlsx`, `xlrd` for legacy `.xls`, the
  standard library `csv` for `.csv` (with BOM handling). `pandas` is deliberately **not** added — a
  large dependency with no benefit here. *Risk: LOW.*

---

## Part 5 — Conflicts resolved elsewhere, listed here for completeness

| Conflict | Resolution | Where |
|---|---|---|
| `saaras:v4` (brief) vs `saaras:v3` (working code) | keep `v3` until a live comparison proves `v4` | Architecture §11 |
| `SARVAM_CHAT_MODEL` vs `SARVAM_LLM_MODEL` | keep `SARVAM_LLM_MODEL`, accept the other as an alias | Architecture §11 |
| `OPENER_PAUSE_SECS` 0.7 s (brief) vs 0.35 s (measured) | expose the key, default to the measured 0.35 s | Architecture §11 |
| Openings non-interruptible vs §11.3 barge-in | opener wins; barge-in applies after it | Architecture §4 |
| Persist transcripts vs §17.8 | do not persist; `STORE_TRANSCRIPTS=false` | Data model §1 |
| Exotel webhooks vs status polling | polling is verified; webhooks stay disabled until their signature mechanism is confirmed | Architecture §8 |

---

## Summary — items needing your confirmation

| ID | Question to the owner | Risk |
|---|---|---|
| A-06a | Are those the right columns for the office export? | MEDIUM |
| A-06b | Should the export show the full parent number or the last six digits? | MEDIUM |
| A-09a/A-10a | How does your school's sheet write amounts and dates (day-first or month-first)? | MEDIUM |
| A-16a | If a reply fails the safety check, replace it with a safe line and continue (proposed)? | MEDIUM |
| A-19a | Confirm: Veda may say something "was noted" only after it is saved to the database? | **HIGH** |
| A-20–A-23 | Confirm the workbook's real sheet name and column headings (send the file when ready) | **HIGH** |
| A-01a | Confirm what should count as "definitely not dialled" versus "result unknown" | MEDIUM |

The five LOW-risk items (A-02, A-04, A-06c/d/e, A-08, A-12, A-27) are safe defaults that can be
changed later without redesign and do not need a decision now.
