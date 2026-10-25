# CLAUDE.md

Guide for working on this repo: what the app is, how it's built, how to run and test it, the decisions behind it, and what's still open. The README is the user-facing setup guide; this file is the working context.

## What this is

**AI Recruiter**: a single-recruiter web app (FastAPI, server-rendered) for registering candidates, reaching out to them through an **Outreach Agent** (email/SMS), and having an **Interview Agent** interview them over a text chat. Both agents use OpenRouter models. The Outreach Agent hands off to the Interview Agent with a briefing paragraph.

Built from a written brief in 7 milestones, each run and tested before the next:
1. Skeleton, models, migrations, auth, candidates/jobs CRUD, resume parsing
2. Outreach Agent, console adapters, conversation history, send modes, safety rails
3. SMTP/IMAP and Twilio adapters, inbound handling
4. Handoff: structured action → briefing → interview + token → link sent
5. Interview chat page + Interview Agent (streaming, transcript, edge cases)
6. Post-interview summary + Interviews page
7. Docker, seed data, README, test pass

## Working with the user

- They want a plan approved before large work, then one step at a time, each **run and tested** before moving on.
- **Ask before anything that changes scope.** Otherwise pick sensible defaults, say what they were, and keep going.
- Be upfront about anything unreliable or unverified.
- They often ask questions and then choose; offer fixes and wait for a yes rather than building unasked.

## Principles (from the brief; keep them true)

- **Humans decide.** The AI never rejects, hires or advances anyone. Scores are recommendations only. The only automatic state change is *stopping* contact on opt-out.
- **Transparency.** The AI says it's an AI in outreach (email signature; prefix on the first SMS) and in the interview, and the candidate is told the interview is recorded (shown on the page plus a consent checkbox).
- **Fairness.** No use or inference of protected attributes; all four prompts say so.
- **Privacy.** Keys only in env vars. Never log resume text or transcripts (the console adapters print message bodies on purpose, for dev). Deleting a candidate removes everything, including the resume file.
- **Show uncertainty.** Resume parse status and warnings appear in the UI and are passed to the agents. Model failures flag the candidate instead of failing silently.
- **Activity log.** Every AI action and every recruiter action goes into `ActivityLog` for the candidate.

## Commands

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/alembic upgrade head                 # migrate (run from repo root; DB path is relative)
.venv/bin/python -m scripts.seed               # 1 job + 3 candidates; safe to run twice
.venv/bin/uvicorn app.main:app --reload        # http://localhost:8000
.venv/bin/pytest                               # 88 tests, all external services faked
TEST_DATABASE_URL=postgresql+psycopg://... .venv/bin/pytest   # same suite on Postgres (wipes that DB)

.venv/bin/python run_public.py                 # Cloudflare quick tunnel + set BASE_URL + start app
.venv/bin/python run_public.py --local         # no tunnel; BASE_URL=http://localhost:8000 + app
.venv/bin/python run_public.py --no-app        # tunnel only
.venv/bin/python -m scripts.check_email <addr> [--send-only|--read-only] [--timeout N]
.venv/bin/python -m scripts.hash_password      # bcrypt hash for RECRUITER_PASSWORD_HASH
docker compose up --build                      # app + Postgres 16 (not yet run: Docker isn't installed here)
```

New Alembic migration: `.venv/bin/alembic revision --autogenerate -m "..."`, then **replace `app.models.UTCDateTime(timezone=True)` with `sa.DateTime(timezone=True)`** in the generated file so migrations don't import app code. SQLite uses batch mode automatically (`alembic/env.py`).

## Stack and key choices

| Area | Choice | Why |
|---|---|---|
| Web | FastAPI, Jinja2, HTMX, Tailwind Play CDN (pinned `3.4.16`), HTMX `2.0.4` from unpkg | No JS framework, no build step (brief) |
| DB | SQLAlchemy 2.x + Alembic; SQLite in dev, Postgres in prod | Schema verified on both |
| Jobs | APScheduler `BackgroundScheduler`, in-process; every job re-reads the DB | No job store needed; restart-safe |
| LLM | `openai` SDK pointed at OpenRouter; models from env only | Brief: never hard-code model names |
| Agent actions | JSON validated with Pydantic, retried once with the error, then flag | More portable across OpenRouter models than tool calling |
| Email | SMTP to send + IMAP polling to receive (one mailbox) | Inbound without a public URL or webhook |
| SMS | Twilio, inbound webhook with signature check | Standard; needs a public URL |
| Live chat | SSE (`sse-starlette`) + fetch POSTs; small vanilla JS client; JSON polling fallback | Reconnects automatically; polling for tunnels that buffer SSE (WebSockets rejected by the user as added complexity) |
| Interview voice | Gnani REST STT + TTS via `httpx`, proxied by the server; optional | Key stays server-side; audio never stored |
| Auth | Single recruiter, bcrypt hash in env, signed session cookie, CSRF token | Multi-user is out of scope |
| No agent framework | Plain Python; no LangChain | One model call per turn, fixed flow; prompts stay visible and editable |

Installed versions are newer than you may expect: FastAPI 0.162, Starlette 1.10, `openai` 3.37, SQLAlchemy 2.1, pytest 9, pytest-asyncio 1.4. Starlette 1.x needs `TemplateResponse(request, name, ctx)`. Iterating `app.routes` yields `_IncludedRouter` objects that have no `.path`.

## Layout

```
app/
  main.py            app factory, session middleware, exception handlers, router includes, lifespan (scheduler)
  config.py          pydantic-settings Settings (cached by get_settings(); restart to pick up .env changes)
  db.py              engine (SQLite FK pragma on), SessionLocal, get_db
  models.py          all tables; UTCDateTime type always returns aware UTC datetimes
  auth.py            verify_credentials, require_login, csrf_protect (header X-CSRF-Token or form csrf_token)
  templating.py      templates + context processor (csrf_token, settings, flashes); localdt filter; flash()
  scheduler.py       jobs: flush_queue, poll_inbox, reminders, check_interviews (all every 60s)
  adapters/          email (base Protocols, console, smtp_imap), sms (base, console, twilio); get_*/set_adapters()
  services/
    llm.py           OpenRouterLLM (complete/stream), get_llm/set_llm, load_prompt ({{var}}), extract_json, complete_structured
    outreach_agent.py OutreachDecision schema, _build_messages, add_disclosure, apply_decision, start_outreach, reply_to_candidate, mark_opted_out
    messaging.py     submit_outbound → dispatch (safety rails) → _send_now; approve_and_send, discard, flush_queued
    safety.py        is_opt_out (keywords; ignores quoted text), quiet_hours_until, effective_send_mode, daily limit
    inbound.py       record_inbound (dedupe, thread matching, opt-out), process_inbound, poll_inbox
    handoff.py       generate_briefing, finalize_interview, schedule_manually, reschedule_interview, send_interview_link, send_due_reminders
    app_settings.py  runtime settings in the app_settings table (send mode, quiet hours, daily limit) over env defaults
    candidates.py    store_resume, delete_candidate (removes the file), search
    resume_parser.py pypdf / python-docx → (text, ok|partial|failed, warnings)
    notify.py        notify_recruiter: flag + activity + optional email to RECRUITER_NOTIFY_EMAIL
    speech.py        GnaniSpeech (STT /stt/v3, TTS /api/v1/tts/inference via httpx), get_speech/set_speech
    activity.py      log_activity
  interview/
    engine.py        transport-agnostic core: window_state, MarkerFilter, plan_turn, build_messages, finish_turn, end
    channel.py       InterviewChannel Protocol, Broker (in-memory pub/sub), TextChannel (SSE events)
    session.py       start, candidate_says, retry, run_agent_turn (per-interview lock + _pending set), end_interview
    summary.py       Evaluation schema, generate_summary, request_summary (thread pool; RUN_INLINE for tests)
    monitor.py       check_interviews: no-show + abandoned/overrun → incomplete
  routers/           auth, candidates, jobs, outreach (+review queue, settings, simulate inbound), interviews (recruiter),
                     interview_public (/i/{token}...), webhooks (/webhooks/twilio/sms)
  templates/         base, login, candidates/*, jobs/*, interviews/*, interview/page.html (candidate), review, settings, partials/*
  static/            app.css, interview.js (chat client)
prompts/             outreach.md, briefing.md, interview.md, summary.md (re-read on every call; no restart needed)
scripts/             seed.py, hash_password.py, check_email.py
run_public.py        tunnel/local launcher
tests/               conftest (env, migrations, autouse fakes, fixtures), fakes.py (FakeLLM), one file per area
```

## Data model

- **Job**: title, description, required_skills.
- **Candidate**: contact fields, links, `preferred_channel`, `timezone` (empty = `APP_TIMEZONE`), resume path/filename/text/parse status/warnings, `send_mode_override` (None/auto/review), `agent_paused`, `opted_out`, `needs_attention` + `attention_reason`.
- **Engagement**: candidate ↔ job (+ channel, status `active | interview_scheduled | opted_out | closed`). Created when outreach starts.
- **Message**: candidate, engagement, direction, channel, subject, body, status (`pending_review | queued | sent | failed | received | discarded`), `ai_generated`, `approval` (`auto | human_approved | human_written | none`; `none` = system message or inbound), `hold_reason`, `provider_message_id` (email Message-ID / Twilio SID), `in_reply_to`.
- **Interview**: candidate, job, engagement, `token` (`secrets.token_urlsafe(32)`), `scheduled_at`, status (`scheduled | in_progress | completed | incomplete | no_show | cancelled`), briefing, channel, started/ended/last_activity, `question_count`, `end_reason`, `reminder_sent_at`.
- **TranscriptEntry** (seq, role agent/candidate/system, content), **InterviewEvaluation** (summary, strengths/concerns as `[{point, evidence}]`, `suggested_score` 1–5, reasoning, error), **ActivityLog**, **AppSetting**.
- Everything hangs off Candidate with `ON DELETE CASCADE` plus ORM cascades. Jobs have no delete route on purpose: deleting a job would cascade to interviews.

## How the main flows work

**Outreach.** `start_outreach` → `_build_messages` builds one request: the system prompt (`prompts/outreach.md`, filled with company, channel, the candidate's local time and the allowed interview window) plus one user message with `<job>`, `<candidate_profile>`, `<resume>` (cut at 12k chars) and `<conversation>` (every sent/queued/received message in the engagement, oldest first; quoted email text stripped). The model has no memory; the DB is the memory, rebuilt on every call. The model returns `OutreachDecision {action, subject, reply, interview_start, reason}` → `apply_decision`.

**Send pipeline** (`messaging.dispatch`), in order:
1. Opted out → hold.
2. For `ai` messages only: paused → hold; review mode → hold; over the daily cap → hold.
3. Quiet hours → `queued` (sent later by `flush_queued`).
4. Otherwise send now.

Message kinds: `ai`, `human` (skips step 2), `system` (skips step 2). Interview links skip quiet hours when waiting would bring them within an hour of the start. Reminders always skip quiet hours.

**Disclosure.** Every AI email gets an "AI Recruiting Assistant, {company}" signature. The first AI SMS gets the `[AI assistant for {company}]` prefix (unless it already says "AI") and "Reply STOP to opt out."

**Inbound.**
- Email: `poll_inbox` reads UNSEEN mail from the last 7 days with `BODY.PEEK`, matches the sender to a candidate, and marks it seen only after processing. Mail from unknown senders is left unread (an in-memory skip set stops re-processing).
- SMS: the webhook validates the Twilio signature against `BASE_URL + path`, records the message, and runs the agent reply in `BackgroundTasks`.
- `record_inbound` dedupes by provider id, matches the engagement by thread headers and falls back to the most recent one (same channel preferred), and applies keyword opt-out before any model call. Auto-replies (Auto-Submitted, Precedence, OOO subjects) are recorded but never answered.
- So a candidate who starts a **new email** (no thread headers) from the same address lands in their most recent engagement. The agent's reply (`messaging._thread_info`) sets `In-Reply-To` to that new email, `References` to every email in the engagement, and the subject to `Re: <original outreach subject>`.

**Handoff.** On `finalize_interview` with a time 15 min to 30 days ahead (naive times are read in the candidate's timezone):
1. `generate_briefing` (BRIEFING_MODEL; falls back to a job-only briefing and flags the candidate on failure).
2. Create the Interview with a token.
3. Send one message: the agent's reply + the link text (+ the disclosure).

If a *scheduled* interview already exists for that candidate and job, it is rescheduled instead. Manual scheduling (candidate page) and recruiter reschedules send `system` messages.

**Interview.**
- `window_state`: opens `GRACE_BEFORE` minutes before the start; can be started until `GRACE_AFTER` minutes after.
- The page has a consent checkbox, then `POST /start` → `session.start` → background `run_agent_turn`.
- Each turn: `plan_turn` (wrap up when the question count or time limit is reached) → `build_messages` (system prompt with briefing; transcript as assistant/user; candidate text wrapped in `<candidate_message>` with tags and end marker stripped; a final system note) → stream through `MarkerFilter` (removes `[[END_INTERVIEW]]`) → `TextChannel` events (`agent_start`, `chunk` with offset, `agent_done`, `agent_error`, `candidate`, `status`) → `finish_turn` saves the entry and counts it as a question if it contains `?`.
- It ends on the marker or after the wrap-up turn → summary.
- Reconnecting: the SSE `snapshot` event carries entries + partial text + `needs_turn`, and the client auto-calls `/retry` if the candidate spoke last.
- Chunk offsets prevent duplicated text after a reconnect.
- `_pending` refuses a second candidate message while the agent is replying (409).
- **Polling fallback.** If no SSE `snapshot` arrives within 5 s (or the URL has `?transport=poll`), the client closes the `EventSource` and polls `GET /i/{token}/poll?after=<last seq>` every 1 s while generating, 2 s otherwise. It returns the snapshot shape (new entries only; `partial` is the whole turn so far). The candidate's own bubble is shown optimistically and reused when the entry comes back. `needs_turn` on the first poll auto-retries; on later polls it shows the error + Retry button. Known glitch: a poll landing exactly as a turn finishes can show the reply twice for about a second.
- **Voice (optional, Gnani; shown only when `GNANI_API_KEY` is set).** The mic records with `MediaRecorder`, the browser converts the recording to 16 kHz mono WAV (browsers record WebM/MP4, which Gnani doesn't accept) and posts it to `POST /i/{token}/transcribe` (in progress only, ≤3 MB/~55 s). The text goes **into the input box** for the candidate to edit and send; it never enters the transcript on its own. "Read replies aloud" calls `POST /i/{token}/speak/{seq}`, which only speaks saved *agent* entries (so it can't be used as free TTS). The text transcript remains the only record. User decisions: en-IN only, no "dictated" flag on transcript entries.

**Monitor** (every 60s):
- No-show when scheduled and more than `NO_SHOW_MINUTES` past the start.
- Incomplete when in progress with no activity for `ABANDON_MINUTES`, or running more than 2× `MAX_MINUTES`.
- Both notify the recruiter. Incomplete still gets a partial summary.

## Testing conventions

- `tests/conftest.py` sets env vars **before** importing the app (temp SQLite, `RUN_SCHEDULER=false`, `BASE_URL=http://testserver`, test password `testpass123`), runs Alembic downgrade/upgrade once per session, and wipes all tables after each test.
- The autouse `fakes` fixture: `FakeLLM` (`queue()` for complete calls, dicts become JSON; `queue_stream()` for interview turns; callables receive the messages), fresh console email/SMS adapters with in-memory `outbox`, `FakeSpeech` (`fakes.speech`: set `.transcript` or `.error`; records `.calls`), `summary.RUN_INLINE = True`, and the interview session locks reset.
- `GNANI_API_KEY` is blanked in conftest so a real key in `.env` never reaches tests; voice tests turn it on with a `voice_on` fixture (monkeypatches the settings object). `GnaniSpeech` takes an optional `httpx` transport, so request shapes are tested with `httpx.MockTransport`.
- `quiet_off` / `candidate_job` fixtures; `set_runtime(db, send_mode=..., quiet_hours_start=..., max_per_day=...)`.
- Interview tests call `session` functions directly with a `RecordingChannel` and `await wait_idle(id)`.
- Required by the brief and present: `tests/test_handoff.py::test_full_handoff_flow` and `tests/test_interview.py::test_full_interview_with_mocked_model`.
- Lint used (not a project dependency): `ruff check app tests scripts --select F,E9,B --ignore B008` (B008 is FastAPI's `Depends` pattern).

## Environment on this machine

- Python 3.12, project venv `.venv` (pip; uv is not installed). Docker is **not** installed. `cloudflared` is at `~/.local/bin/cloudflared`.
- Local login from `.env`: `recruiter` / `changeme123` (dev only; change it before exposing the app through a tunnel).
- `.env` now has an OpenRouter key and models, and SMTP/IMAP pointed at IITB (`smtp-auth.iitb.ac.in:587`, `imap.iitb.ac.in`). SMTP login and send were confirmed by the user, but a test email to Gmail didn't arrive; not yet diagnosed (spam, delay, or held by IITB).
- `GNANI_API_KEY` is **not yet in `.env`** (the user said they'd add one). Next step agreed in principle: a small live check of STT and TTS, then a browser test.
- The local machine's timezone is IST. Quiet hours (21:00–08:00 in the candidate's timezone) will queue messages at night, which is expected.
- Postgres testing used `pgserver` in a scratch venv (not part of the project).
- Not a git repository yet.

## Verified vs not verified

- Verified: the full test suite on SQLite and Postgres; real server smoke tests of every page; live SSE streaming on localhost (including reconnect snapshots); a Cloudflare quick tunnel serving the app; `run_public.py` (tunnel and `--local`, clean shutdown).
- **Not verified:** a live OpenRouter run of either agent (a key was only added recently); docker-compose; real IMAP reading; Twilio against the real service; Gnani STT/TTS against the real API (request shapes follow docs.gnani.ai and are tested with a mock transport) and the browser voice code (no JS runtime here).

## Known issues and open decisions (proposed to the user, waiting on a yes)

1. **SSE doesn't pass through Cloudflare quick tunnels.** Tested: no events reached the client in 10s. Fix built (user chose polling over WebSockets): `GET /i/{token}/poll?after=<seq>` returns the snapshot as JSON; `interview.js` switches to polling every 1–2 s if no `snapshot` arrives within 5 s, or always with `?transport=poll`. Endpoint is tested; the JS has **not yet been run in a browser or through a tunnel**.
2. **Downtime causes unfair statuses.** After the server is down across an interview's start time, the first monitor run marks it no-show; an in-progress interview interrupted for more than 15 minutes becomes incomplete; the time limit counts downtime. Proposed: count only time since the app started (record a boot time; use `max(scheduled_at, boot)` and the same for activity).
3. **A summary in progress is lost on shutdown** (in-memory thread pool). Proposed: a scheduler job that generates missing evaluations for completed/incomplete interviews.
4. **The Outreach Agent after handoff.**
   - (a) The agent isn't told the interview's status. Proposed: an `<interviews>` block in its context.
   - (b) It can book a *new* interview after one was completed, incomplete or a no-show (`active_interview` only checks `scheduled`). Proposed: flag the recruiter instead.
   - (c) Opting out doesn't cancel a scheduled interview, and `mark_opted_out` only updates `active` engagements, not `interview_scheduled` ones. Proposed: cancel scheduled interviews on opt-out.
5. Company details (`COMPANY_NAME`, `RECRUITER_NAME`, `EMAIL_FROM_NAME`) live only in `.env`. Offered: make them editable on the Settings page.
6. **Email from an unknown address is silently ignored.** If a candidate writes from a different address than the one on file, `poll_inbox` leaves it unread and nobody is told. Offered: flag the recruiter (or forward to `RECRUITER_NOTIFY_EMAIL`) when unknown mail looks related to the company or a job.
7. **New (unthreaded) emails from a candidate in two engagements go to the most recent one**, so the agent may answer about the wrong job. Not yet proposed as a fix.

## Known limitations (documented in the README)

- **Single process only:** the in-memory broker, `_pending`/locks and the scheduler. The Docker CMD runs one uvicorn worker. Not deployable to serverless platforms like Vercel without refactoring (cron endpoints, a DB-backed chat, object storage); Render, Railway, Fly or a VPS with the existing Dockerfile are the recommended hosts.
- Quick-tunnel URLs change on every run, so links already sent stop working; reschedule to re-send them. `BASE_URL` is read at startup.
- `SESSION_HTTPS_ONLY=true` breaks login over plain http://localhost.
- Opt-out keyword matching is deliberately broad (false positives just pause the agent and flag the candidate).
- Two near-simultaneous inbound messages can produce two agent replies (capped by the daily limit).
- No OCR for scanned PDFs. No summarisation of long outreach histories (the full thread is resent every call).
- Twilio doesn't retry inbound webhooks, so SMS received while the server is down are lost to the app.
- In `.env`, keep `RECRUITER_PASSWORD_HASH='...'` single-quoted; Compose and python-dotenv would otherwise expand the `$` signs.
