# AI Recruiter

A web app for one recruiter to register candidates, reach out to them through an **Outreach Agent** (email/SMS), and have an **Interview Agent** interview them over text chat. Both agents run on OpenRouter models. Humans make every decision: the AI drafts, schedules, interviews and summarises, but never rejects, hires or advances anyone.

## Demo video

▶️ **[Watch the project walkthrough](docs/demo.mp4)** (MP4, 26 MB): a screen recording explaining the app and its flow. It opens in GitHub's file viewer, where you can play or download it.

**Stack:** Python 3.11+, FastAPI, Jinja2 + HTMX + Tailwind (no frontend build), SQLAlchemy 2 + Alembic (SQLite or PostgreSQL), APScheduler, OpenRouter via the `openai` SDK, SMTP/IMAP email, Twilio SMS, SSE for the live interview chat (with a polling fallback), optional Gnani speech-to-text and text-to-speech in the interview.

---

## Quick start (local, SQLite)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Edit `.env`:

1. `SECRET_KEY`: any long random string (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
2. `RECRUITER_PASSWORD_HASH`: run `.venv/bin/python -m scripts.hash_password` and paste the result **inside single quotes** (the hash contains `$` signs).
3. `OPENROUTER_API_KEY`, `OUTREACH_MODEL`, `INTERVIEW_MODEL` (see [OpenRouter](#openrouter)).
4. Optional: `GNANI_API_KEY` to turn on voice in the interview chat (see [Interview voice](#interview-voice-gnani)).

To change the login later, set `RECRUITER_USERNAME`, generate a new hash with the same script, put it in `RECRUITER_PASSWORD_HASH` (single-quoted), and **restart** the app: settings are read once at startup, and `--reload` doesn't pick up `.env` changes.

Then:

```bash
.venv/bin/alembic upgrade head          # create / migrate the database
.venv/bin/python -m scripts.seed        # optional: a sample job and 3 candidates
.venv/bin/uvicorn app.main:app --reload # http://localhost:8000
```

To let candidates open interview links from their own phones or laptops, start the app with `.venv/bin/python run_public.py` instead (see [Running on a public URL](#running-on-a-public-url-run_publicpy)).

Without email or SMS credentials the app uses **console adapters**: outgoing messages are printed in the server log instead of being sent, and each candidate page gets a **"simulate a candidate reply"** tool so you can try the whole flow (outreach → reply → interview agreed → link → interview) locally.

## Running with docker-compose (app + PostgreSQL)

```bash
cp .env.example .env    # fill in as above; DATABASE_URL is set by compose
docker compose up --build
```

The app runs migrations on start and listens on http://localhost:8000. Seed data: `docker compose exec app python -m scripts.seed`. Uploaded resumes are kept in the `uploads` volume and the database in `pgdata`.

> In `.env`, keep `RECRUITER_PASSWORD_HASH='...'` in single quotes; otherwise Compose tries to expand the `$` parts of the hash.

## Running on a public URL (`run_public.py`)

Candidates need to open interview links from their own devices, and Twilio needs a public URL for incoming SMS. `run_public.py` does the whole setup in one command, using a free [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) (no account needed):

```bash
.venv/bin/python run_public.py              # tunnel + app on port 8000
.venv/bin/python run_public.py --local      # no tunnel: BASE_URL=http://localhost:8000 + app
.venv/bin/python run_public.py --no-app     # tunnel only; start the app yourself afterwards
```

What it does:

1. Starts `cloudflared tunnel --url http://localhost:<port> --protocol http2`.
2. Waits for the `https://<random>.trycloudflare.com` address it prints.
3. Writes that address to `BASE_URL` in `.env`, leaving every other line as it is.
4. Starts the app (uvicorn, without `--reload`), so interview links and Twilio signature checks use the public URL.

Ctrl+C stops both the tunnel and the app. `--local` is also the way to undo the tunnel URL in `.env` when you go back to working locally.

| Option | Default | Purpose |
|---|---|---|
| `--port` | `8000` | Local port of the app |
| `--local` | | No tunnel; set `BASE_URL=http://localhost:<port>` and run the app |
| `--no-app` | | Only run the tunnel and update `.env` |
| `--cloudflared` | `~/.local/bin/cloudflared` | Path to the `cloudflared` binary ([download](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)) |
| `--env-file` | `./.env` | Env file to update |
| `--timeout` | `45` | Seconds to wait for the tunnel URL |
| `--verbose` | | Show all `cloudflared` output |

Things to know:

- **Change the login first.** The tunnel makes the recruiter login reachable from the internet; don't use the example password.
- **The URL changes on every run**, so interview links sent during an earlier run stop working. Reschedule those interviews to re-send the links.
- **The chat uses polling through the tunnel.** Quick tunnels block streamed events, so the interview page switches to polling after 5 seconds: replies appear about once a second instead of word by word.
- For a stable URL, use a real host (see [Known limitations](#known-limitations)), a named Cloudflare tunnel, or ngrok.

## Configuration

All settings are environment variables (or `.env`); `.env.example` lists every one with comments. The main ones:

| Variable | Purpose |
|---|---|
| `BASE_URL` | Public URL of the app. Used in interview links and Twilio signature checks. Use your tunnel URL in development. |
| `DATABASE_URL` | `sqlite:///./recruiter.db` or `postgresql+psycopg://user:pass@host:5432/db` |
| `OPENROUTER_API_KEY` | OpenRouter key |
| `OUTREACH_MODEL`, `INTERVIEW_MODEL` | OpenRouter model ids (required) |
| `BRIEFING_MODEL`, `SUMMARY_MODEL` | Optional; default to `OUTREACH_MODEL` and `INTERVIEW_MODEL` |
| `DEFAULT_SEND_MODE` | `auto` (default) or `review`. Can also be changed on the Settings page and per candidate. |
| `MAX_OUTBOUND_PER_CANDIDATE_PER_DAY`, `QUIET_HOURS_START/END` | Safety rails (also editable on the Settings page) |
| `SMTP_*`, `EMAIL_FROM`, `IMAP_*` | Email sending and receiving |
| `TWILIO_*` | SMS |
| `INTERVIEW_*` | Grace windows, no-show/abandon timeouts, time and question limits, reminder lead time |
| `GNANI_API_KEY`, `GNANI_LANGUAGE`, `GNANI_TTS_MODEL`, `GNANI_TTS_VOICE` | Optional voice in the interview chat (Gnani). The candidate can dictate an answer (it fills the text box for review; audio isn't stored) and have replies read aloud. Hidden when the key is empty. The microphone needs HTTPS or localhost. |
| `RECRUITER_NOTIFY_EMAIL` | Optional: also email the recruiter about no-shows and incomplete interviews |
| `RUN_SCHEDULER` | `true` in exactly one process |

### OpenRouter

1. Create a key at https://openrouter.ai/keys and set `OPENROUTER_API_KEY`.
2. Pick models at https://openrouter.ai/models and put their ids in `OUTREACH_MODEL` and `INTERVIEW_MODEL`. Model names are never hard-coded.
3. The Outreach Agent and the summary must return JSON. The app asks for JSON mode, validates the result with Pydantic, retries once with the error, and if it still fails, flags the candidate instead of sending anything. Choose models that follow JSON instructions reliably; most current frontier and mid-size models do. The Interview Agent uses streaming.

### Email: SMTP + IMAP

**Why SMTP/IMAP:** one mailbox does both directions, it works with any provider, and receiving replies needs **no public URL or webhook**: the scheduler polls the inbox every `IMAP_POLL_SECONDS`. Adding an API provider such as Resend or SendGrid later only takes a new class next to `app/adapters/email/smtp_imap.py`.

**How incoming mail is matched.** The sender address identifies the candidate (case-insensitive). The conversation is found from the `In-Reply-To`/`References` headers; if the candidate starts a **new email** instead of replying, it goes to their most recent conversation (same channel preferred). The agent's answer replies to that new email, references the whole thread, and keeps the subject `Re: <original subject>`, so most mail clients group it with the original thread.

Use a **dedicated mailbox** for the agent. The poller reads unread mail from the last 7 days and marks candidate replies as read; mail from unknown senders is left untouched (and isn't reported; see [Known limitations](#known-limitations)).

Example: Gmail / Google Workspace (needs 2-step verification and an [app password](https://myaccount.google.com/apppasswords)):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=recruiting-agent@yourdomain.com
SMTP_PASSWORD=<app password>
EMAIL_FROM=recruiting-agent@yourdomain.com
IMAP_HOST=imap.gmail.com
IMAP_USERNAME=recruiting-agent@yourdomain.com
IMAP_PASSWORD=<app password>
```

Other providers work the same way (Outlook/M365: `smtp.office365.com` / `outlook.office365.com`; Fastmail: `smtp.fastmail.com` / `imap.fastmail.com`). For port 465 the app uses implicit SSL automatically.

**Check your settings** before using real candidates:

```bash
.venv/bin/python -m scripts.check_email recruiting-agent@yourdomain.com   # send to the agent mailbox and read it back
.venv/bin/python -m scripts.check_email you@personal.com --timeout 300    # send to another inbox, then reply to it
.venv/bin/python -m scripts.check_email --read-only                        # just test the IMAP login
```

The script logs in to both servers, sends an email with a unique code, and checks the agent inbox until that email (or your reply to it) arrives. It prints a hint for common problems such as a missing app password or a wrong port. It doesn't mark anything as read and doesn't touch the database.

### SMS: Twilio

1. Buy a number in the Twilio console and set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` (E.164, e.g. `+14155550123`).
2. In the number's settings, set **"A message comes in"** to webhook `POST {BASE_URL}/webhooks/twilio/sms`.
3. Candidate phone numbers must be in E.164 format (the form checks this).

Incoming webhooks are verified with Twilio's signature, computed over `BASE_URL` + path, so `BASE_URL` must be exactly the public URL Twilio calls. Twilio also handles carrier-level STOP keywords itself; the app additionally detects stop/unsubscribe language and pauses the agent.

### Interview voice: Gnani

Optional. With `GNANI_API_KEY` set (create one at https://app.gnani.ai/voice/), the interview chat gets two extra controls; without it, they don't appear.

- **🎤 Speak.** The candidate records an answer (up to about 55 seconds). The browser converts it to 16 kHz mono WAV and sends it to the app, which passes it to Gnani's speech-to-text and returns the text **into the input box**. The candidate reviews or edits it and presses Send, so a mis-transcription never becomes their answer by itself.
- **Read replies aloud.** Each finished interviewer message is turned into speech by Gnani and played. The setting is remembered in the browser and is off by default.

Settings: `GNANI_LANGUAGE` (default `en-IN`, used for both directions), `GNANI_TTS_MODEL` (default `timbre-v2.5`) and `GNANI_TTS_VOICE` (default `Kaveri`; the voice must belong to the model's catalog, e.g. Kaveri, Trupti, Devika, Pranav, Shlok or Girish for English on `timbre-v2.5`).

The key stays on the server; audio is passed straight through and never stored or logged, and only the text transcript is recorded. The start screen tells the candidate this. The speak endpoint only reads out messages the interviewer actually sent, so it can't be used to turn arbitrary text into speech on your account. Microphone access needs HTTPS or `localhost`. Typing always works, and any voice failure falls back to a message asking the candidate to type.

### Receiving webhooks locally (tunnel)

Twilio needs a public HTTPS URL. The easiest way is [`run_public.py`](#running-on-a-public-url-run_publicpy), which starts a tunnel, sets `BASE_URL` and starts the app. To do it by hand with [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) or [ngrok](https://ngrok.com/):

```bash
cloudflared tunnel --url http://localhost:8000    # or: ngrok http 8000
```

Copy the https URL it prints into `BASE_URL` in `.env`, restart the app, and use `{BASE_URL}/webhooks/twilio/sms` in Twilio. Email needs no tunnel (IMAP polling).

Cloudflare quick tunnels don't pass through server-sent events, so the interview chat can't stream through them. The page detects this (no data within 5 seconds) and switches to polling automatically. Add `?transport=poll` to an interview link to force polling, for example to test it locally.

## How it works

```
Recruiter ──► Candidate page ──► "Reach out" (job + channel)
                                   │
                         Outreach Agent (OUTREACH_MODEL, JSON)
                                   │  action: none | finalize_interview | opt_out | flag_for_recruiter
                     send pipeline: opt-out → pause → send mode → daily limit → quiet hours
                                   │
                     Email (SMTP) / SMS (Twilio) / console
                                   │
         replies ◄── IMAP poller / Twilio webhook ──► stop words? → pause + flag
                                   │
                     finalize_interview (validated time)
                                   │
          Handoff: briefing (BRIEFING_MODEL) → Interview record + token → link sent
                                   │
          /i/{token}: waiting screen → consent → chat (SSE streaming, polling fallback)
                                   │            optional voice: mic → Gnani STT → input box;
                                   │                            replies → Gnani TTS → audio
          Interview Agent (INTERVIEW_MODEL): engine ⟷ InterviewChannel (text)
                                   │
          completed / incomplete → evaluation (SUMMARY_MODEL), recommendation only
```

- **Send modes.** Auto-send is the default. With "review before send" (globally on the Settings page or per candidate), AI drafts wait in the **Review queue**, where you can edit and approve or discard them. Each message is labelled *AI-generated*, *auto-sent*, *human-approved*, *written by recruiter* or *system*.
- **Safety rails, even in auto mode.** A rolling 24-hour cap per candidate (over the cap, AI drafts go to review). Quiet hours in the candidate's timezone (messages are queued and sent when they end; the one exception is an interview link that would otherwise arrive less than an hour before the interview). A **Pause agent** button on every candidate. Stop/unsubscribe/"not interested" language, detected by keywords before the model runs and also by the model's `opt_out` action, stops the agent, discards anything queued, and flags the candidate. Out-of-office auto-replies are recorded but never answered.
- **Handoff.** The agent's structured `finalize_interview` action, validated by Pydantic and limited to 15 minutes to 30 days ahead, triggers: briefing → interview record with a 256-bit URL token → link + time sent on the conversation's channel, in the same message as the agent's confirmation. A reminder goes out `INTERVIEW_REMINDER_MINUTES` before the start. A second agreement for the same job reschedules instead of duplicating. Recruiters can also schedule manually from the candidate page.
- **Interview.** The page unlocks `INTERVIEW_GRACE_BEFORE_MINUTES` before the start and can be started until `INTERVIEW_GRACE_AFTER_MINUTES` after. The candidate must confirm they understand it's an AI and recorded. Replies stream over SSE, or arrive by polling when SSE is blocked (see [the tunnel section](#receiving-webhooks-locally-tunnel)); with Gnani configured, the candidate can also speak answers and hear replies. Generation runs on the server independently of the connection, so a dropped connection loses nothing: reopening the link restores the transcript and any reply in progress. Question and time limits are enforced in code: the engine tells the agent to wrap up, and the interview ends after the closing message even if the model forgets the end marker. If nobody joins within `INTERVIEW_NO_SHOW_MINUTES`, the interview is marked **no show**; inactivity for `INTERVIEW_ABANDON_MINUTES` marks it **incomplete**. Both notify the recruiter.
- **Interviews page.** Filter by status and date, copy links, cancel (optionally telling the candidate), reschedule (re-sends the link), end early, and view transcripts, briefings and evaluations.
- **Activity log.** Every AI action (draft, send, handoff, briefing, interview start/end, summary, errors) and recruiter action is logged on the candidate page.

### Code map

| Path | What |
|---|---|
| `app/services/outreach_agent.py` | Outreach Agent: prompt building, `OutreachDecision` schema, applying actions |
| `app/services/messaging.py` | Outbound pipeline and safety rails |
| `app/services/inbound.py` | Inbound matching, opt-out, IMAP polling |
| `app/services/handoff.py` | Briefing, interview creation, link, reschedule, reminders |
| `app/interview/engine.py` | Interview Agent core (prompting, question flow, limits), transport-agnostic |
| `app/interview/channel.py` | `InterviewChannel` protocol + SSE `TextChannel` |
| `app/interview/session.py` | Connects engine and channel; turn locking, retry, ending |
| `app/interview/summary.py`, `monitor.py` | Post-interview evaluation; no-show and abandon checks |
| `app/routers/interview_public.py` | Candidate-facing `/i/{token}` routes: page, start, message, retry, SSE `events`, JSON `poll`, voice `transcribe` and `speak` |
| `app/static/interview.js` | Chat client: SSE with polling fallback, recording + WAV conversion, playback |
| `app/services/speech.py` | Gnani speech-to-text and text-to-speech client |
| `app/adapters/` | Email/SMS protocols, SMTP/IMAP, Twilio and console implementations |
| `prompts/` | `outreach.md`, `briefing.md`, `interview.md`, `summary.md` |

**Voice today vs. a voice channel.** The current voice feature only converts audio to text and back on the page; the interview itself is still a text conversation. A fully spoken interview (no text box) would implement `InterviewChannel` (for example, speak `agent_chunk` text through streaming TTS) and feed speech-to-text results into `session.candidate_says()`. The engine, prompts and summary would stay the same.

## Tuning prompts

Edit the Markdown files in `prompts/`; changes apply to the next model call, with no restart needed. Placeholders like `{{company_name}}` are filled in by the code; unknown placeholders are left as they are. Keep the JSON output sections of `outreach.md` and `summary.md` intact, because the code validates against them.

## Tests

```bash
.venv/bin/pytest
```

All external services are faked: model and speech (`tests/fakes.py`), SMTP, IMAP, Twilio and Gnani. A Gnani key in your `.env` is ignored by the tests. Notable tests:

- `tests/test_handoff.py`: outreach action → briefing → interview record → link sent.
- `tests/test_interview.py::test_full_interview_with_mocked_model`: a complete interview (start, streamed turns, limits, closing, transcript, evaluation), plus prompt-injection, retry-after-failure, time limit, no-show and abandon cases. `test_poll_fallback` covers the polling endpoint.
- `tests/test_speech.py`: the exact requests sent to Gnani (via a mock transport), error handling, and the `transcribe` / `speak` routes.

To run the same suite against PostgreSQL (the database is wiped):

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/recruiter_test .venv/bin/pytest
```

## Principles, as built

- **Humans decide.** Scores are labelled as recommendations everywhere. No code path rejects, advances or hires a candidate based on AI output; the only automatic state change is *stopping* contact on an opt-out.
- **Transparency.** Every AI email ends with an "AI Recruiting Assistant" signature. The first AI SMS is prefixed with `[AI assistant for <company>]` if the model didn't already say so. The interview page and the agent's opening both say it's an AI and that the chat is recorded. With voice on, the start screen also says recordings go to Gnani for transcription and aren't stored.
- **Fairness.** All four prompts forbid using or inferring protected attributes.
- **Privacy.** Keys only come from env vars. Logs never contain resume text or transcripts (the console adapters print message bodies on purpose, for development). Deleting a candidate removes their resume file and, by cascade, all messages, interviews, transcripts, evaluations and activity. Interview audio is never stored.
- **Showing uncertainty.** Resume parse status (*parsed / partially parsed / failed*) and warnings appear on the candidate page and are passed to the agents. Model failures flag the candidate instead of failing silently.

## Known limitations

- **Single process.** Live interview streaming uses an in-memory broker, and the scheduler runs in-process. Run one app process (the Docker image does). Transcripts are always saved, so a restart loses at most a reply that was mid-stream, and the page offers Retry. For a permanent URL, host the existing Dockerfile on Render, Railway, Fly or a VPS; serverless platforms such as Vercel would need refactoring.
- **Model-dependent behaviour.** Date resolution ("next Tuesday at 3"), staying on topic under prompt injection, and summary quality depend on the model. The code adds guards (time-window checks, untrusted-data wrapping, enforced limits, review mode), but these reduce risk rather than remove it.
- **Opt-out detection is deliberately broad.** "not interested in relocating" also pauses the agent and flags the candidate. You can resume it with one click.
- **Concurrent replies.** Two candidate messages arriving at nearly the same moment can produce two agent replies; the daily cap limits how far this can go.
- **Email from an unknown address is ignored.** If a candidate writes from an address that isn't on their record (say, a work address), the email stays unread in the mailbox and nobody is notified. Check the mailbox now and then, or update the candidate's email.
- **New emails during two conversations.** A candidate being contacted about two jobs who starts a new email (rather than replying in a thread) is matched to the most recent conversation, which may be the wrong job.
- **Polling is less smooth.** In polling mode, replies appear in chunks about once a second, and a reply can briefly show twice if a poll lands exactly as it finishes.
- **Voice quality.** Transcription accuracy (Gnani's models focus on Indian languages and accents) and read-aloud latency (speech starts after the whole reply is written) depend on the provider. Each read-aloud reply is one billable TTS call.
- **No OCR.** Scanned or image-only PDFs are flagged as *parsing failed*.
- **Tailwind Play CDN** is used to avoid a build step. For production, generate a static CSS file with the Tailwind standalone CLI.
- **Not tested against live providers in development:** a live OpenRouter run of the agents, the SMTP/IMAP and Twilio adapters, and Gnani speech are covered by tests with faked network layers only. The browser voice and polling code has not yet been run in a real browser.
