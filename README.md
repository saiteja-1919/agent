# Veda — the school fee voice call

Veda is an AI voice agent that phones parents about a pending school fee. It talks in natural spoken Telugu mixed with English, finds out **when** the parent will pay, and saves a transcript with timing data for the office.

One command dials one parent. A transcript is saved for every call.

---

## What you need

- A Windows laptop with **Python 3.13** (already installed on this machine).
- An **Exotel** or **Twilio** account (the provider is chosen with `CALL_PROVIDER` in `.env`).
- A **Sarvam** API key with all four services enabled (speech, model, voice, transliteration).
- **Cloudflare tunnel** (cloudflared) so Exotel can reach your laptop from the internet.

---

## Setup (do this once)

```powershell
# 1. Create the virtual environment and install the libraries
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Create your settings file from the example
Copy-Item .env.example .env
```

Now open `.env` in Notepad and fill in every value:

| What | Where it comes from |
|---|---|
| `EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN` | Exotel dashboard → API |
| `EXOTEL_CALLER_ID` | Your ExoPhone, as digits (landline is fine) |
| `EXOTEL_STREAM_URL` | Your tunnel address (see below), always ends with `/v1/exotel/stream` |
| `SARVAM_API_KEY` | Sarvam dashboard |
| `SCHOOL_NAME` | **Required.** Veda refuses to call if this is empty |

**Rules for `.env`**
- Secrets live only in `.env`. Never copy them into any other file or chat message.
- If you set `STREAM_TOKEN`, the stream URL must end with `?token=<the same value>`.

---

## Every day (three steps)

### Step 1 — Start the tunnel

```powershell
cloudflared tunnel --url http://localhost:8000
```

Copy the `https://...` address it prints into `.env`:

```text
EXOTEL_STREAM_URL=wss://your-new-address.trycloudflare.com/v1/exotel/stream
```

The address changes every time the tunnel restarts — update `.env` whenever it does.

### Step 2 — Pre-flight check

```powershell
.\.venv\Scripts\python.exe check.py
```

It checks the configuration, Sarvam (voice, speech, names) and whether the tunnel is reachable. Fix anything marked `[fail]` before calling.

### Step 3 — Call one parent

```powershell
.\.venv\Scripts\python.exe make_call.py --to 98xxxxxxxx --name "Student Name" --amount 5000
```

Useful options:

```text
--parent-name "Ramesh"      the parent's name (optional)
--due-date "10th October"   the due date on record (optional)
--student-notes "..."       extra info the office wants remembered (optional)
--school-name "..."         overrides SCHOOL_NAME for this call
--check-names               preview how the names will be spoken, then STOP (no call)
--no-name-conversion        skip English-letters → Telugu conversion
--no-start-server           use a server that is already running
```

**Tip:** run with `--check-names` the first time for a new student. It shows the two opening lines and dials nothing.

The server starts by itself. You can also run it alone with `.\.venv\Scripts\python.exe server.py`.

---

## What gets saved

In the `transcripts` folder, two files per call:

- `call_<time>_<ref>.json` — who was called, the two opening lines, every turn of the conversation, and the `no_reply` / `interrupted` flags. **This file is written last.**
- `call_<time>_<ref>_latency.json` — how long each step took: stream parsed → pipeline built → client connected → first voice audio, plus the response latency of every reply. **This file is written first**, so you always get the timings even if the call is interrupted.

Other files the program keeps:

- `name_pronunciations.json` — how each name is spoken. Hand-editable: lower-case key → spoken spelling.
- `.pending_calls/` — one file per dialled call; removed automatically after the call (or after 15 minutes if the call never happened).
- `opener_cache/` — only used if you turn on pre-rendered openers.

---

## Exit codes of `make_call.py`

| Code | Meaning |
|---|---|
| 0 | The call finished and a transcript was saved |
| 1 | Exotel or the server reported an error |
| 2 | Wrong input or configuration — **nothing was dialled** |
| 3 | Nobody answered, or no conversation was recorded |
| 4 | The local server is not running |

---

## Switching between Exotel and Twilio

One line in `.env` decides who places the calls:

```text
CALL_PROVIDER=exotel     # or: twilio
```

Everything else (server, pipeline, transcripts, tests) is the same for both.

**When you use Twilio, fill these instead of the Exotel keys:**

```text
TWILIO_ACCOUNT_SID=AC...        # Twilio console -> Account info
TWILIO_AUTH_TOKEN=...           # Twilio console -> Account info
TWILIO_CALLER_ID=+15551234567   # your Twilio number, international form
```

`TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` must stay in `.env` — the voice
library reads them directly from there to hang up the call when Veda is done.

**The stream URL differs slightly** (the key name `EXOTEL_STREAM_URL` stays the
same for both providers):

| | path | with `STREAM_TOKEN` |
|---|---|---|
| Exotel | `/v1/exotel/stream` | `...?token=<value>` (query) |
| Twilio | `/v1/twilio/stream` | `.../v1/twilio/stream/<value>` (path) |

Twilio does not accept a query string in a Stream URL, so for Twilio the token
goes into the path. `check.py` and start-up validation both refuse a wrong
combination with a plain message.

**Important rules for calling Indian numbers with Twilio** (from Twilio's
official India guidelines):

- Outbound calls to India may only be made from **international (non-Indian)
  numbers** — your Twilio caller ID cannot be an Indian number.
- Voice OTP/2FA calls are not permitted and may be blocked.
- High volumes of short-duration calls can trigger automated carrier blocks.
- You must have the recipient's consent (TRAI rules); unconsented commercial
  calls can be blocked and your account can be terminated.

A live test call is the only way to prove the Twilio path works end to end.

---

## Tuning (all in `.env`)

| Setting | What it does | Good starting point |
|---|---|---|
| `SARVAM_TTS_VOICE` | The voice (lower case, e.g. `shubh`, `ritu`, `kavya`) | `shubh` |
| `SARVAM_TTS_PACE` | How fast it speaks (0.5–2.0) | `1.15` |
| `LLM_TEMPERATURE` | How lively the replies are (0.0–1.5) | `0.6` |
| `LLM_MAX_TOKENS` | Max words per reply (60–2000) | `300` |
| `VAD_STOP_SECS` | Silence before the parent is considered finished (0.3–1.5) | `0.35` |
| `VAD_MIN_VOLUME` | Lower this (e.g. `0.4`) if quiet parents are not heard (0.0–1.0) | `0.6` |
| `USER_TURN_STOP_TIMEOUT_SECS` | Extra wait before Veda starts replying (0.3–3.0) | `0.5` |
| `CALL_TIMEOUT_SECS` | Hard limit for one call (≥ 30) | `600` |
| `IDLE_TIMEOUT_SECS` | Silence before the call ends; `0` = never (0 or ≥ 10) | `60` |

Every setting is validated at start-up. A wrong value prints a clear message instead of failing later.

---

## Security

- The server listens on **127.0.0.1 only**. The outside world reaches it only through your tunnel.
- Set `STREAM_TOKEN`: only Exotel streams that carry your token are accepted, and it is checked **before** the connection is accepted.
- `/health` answers only `{"ok": true, ...}` — never your tunnel address, never a secret.
- Never share `.env`, and never paste API keys into chats or files.

---

## Troubleshooting

**`Configuration problems: ...` on start** — read the list; each line says which `.env` value is wrong.

**"Nothing was dialled. These settings are missing"** — fill those keys in `.env`.

**"No server is answering on 127.0.0.1:8000"** — start `server.py` yourself, or drop `--no-start-server`.

**"The call was not answered"** — the parent's phone was busy or off. Nothing was recorded; just try again.

**Call finished but "no transcript"** — check the server window for errors and send the terminal output as it is; do not describe it from memory.

**Names come out wrong** — run `--check-names`, then hand-edit the spoken spelling in `name_pronunciations.json`.

**Veda talks over the parent or replies too slowly** — raise `VAD_STOP_SECS` a little (e.g. `0.5`) for less cutting-in, lower it (e.g. `0.3`) for faster replies.

**Quiet parents are not heard** — lower `VAD_MIN_VOLUME` to about `0.4`.

**Tunnel stopped working** — the address changed. Restart cloudflared, paste the new address into `EXOTEL_STREAM_URL`.

**Telugu looks like boxes in the terminal** — the programs force UTF-8 themselves; if you still see boxes, use Windows Terminal instead of the old console.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

183 tests cover configuration, prompts, numbers, names, the recorders, speech/voice adapters, the pending-call store and the dialer's validation. They run offline — no network, no phone.

**What only a live phone call can prove:** the Exotel dial itself, real audio quality both ways, and whether the `from`/`to` numbers match the right parent. Run one test call to a real phone before using Veda with parents.
