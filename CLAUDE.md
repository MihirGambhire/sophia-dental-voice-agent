# Sophia: project instructions

Sophia is an AI voice receptionist for a UK dental practice. Unofficial
concept demo, built on free tiers and open source.

Read this before changing anything. It holds the decisions that are
already settled, so they do not get re-opened by accident.

---

## 1. Standing rules

1. **No em dashes or en dashes** in any prose, anywhere: README, docs,
   commit messages, UI copy, code comments. Use commas, colons, full
   stops or plain hyphens. Keep the writing natural and human.
2. **Zero cost.** Free tiers, free credits and open source only. If
   something paid would make a large quality difference, stop and ask
   first. Never add a paid phone line.
3. **Check versions and model names against official docs** before
   starting each phase. APIs and free tier limits move. Everything here
   was verified on 16 September 2026.
4. **Fake data only.** Invented patients, invented clinician names, real
   role structure. Label everything as an unofficial concept demo. Do not
   copy practice website text verbatim, rewrite facts in our own words.
5. **Business logic lives in code, never in the model.** The model
   understands the caller and picks a tool. Python and SQLite enforce
   every rule: availability, fees, eligibility, verification, policy.
6. **Keep it shippable.** Finish inbound completely before starting
   outbound. If time runs short, cut stretch goals, never tests, never
   the safety screen, never the demo.
7. **Prefer simple and reliable over clever.** Comment the reasoning
   where a decision is not obvious, especially where a naive
   implementation would be subtly wrong.

---

## 2. Build plan

| Day | Goal | Status |
|---|---|---|
| 1 | Repo, clinic data, SQLite schema and seed | Done |
| 2 | Text agent, tool calling against SQLite | Done |
| 3 | Safety screen, FAQ retrieval, policy edge cases | Done |
| 4 | Voice layer, browser call end to end | Done |
| 5 | Voice polish: interruptions, latency, British voice | Done |
| 6 | Scenario eval suite, latency measurement, outbound flow | Done, latency figures pending |
| 7 | README, architecture diagram, demo video | README done, video next |
| 8 | Final checks | |

Cut order if behind: optional dashboard, then outbound recall calls
(keep the day before reminder), then hygienist direct access, then the
finer NHS and private distinctions.

---

## 3. Settled decisions

| Decision | Choice | Why |
|---|---|---|
| Agent name | Sophia | Warm, professional, easy for speech recognition |
| Direction | Inbound first, small outbound flow second | Inbound is the harder and more useful half. Outbound reuses the same tools |
| Country | UK | Changes escalation numbers, privacy law, date format, timezone and vocabulary |
| Domain | Dental practice, NHS and private | Nationally documented access pressure, and the market already buys this |
| Modelled on | Museum Street Dental Practice, Warrington | Strongest documented phone pressure of the practices compared. Unofficial demo only |
| Delivery | Browser voice call, no phone number | A real number costs money and proves nothing extra |
| Cost | Entirely free stack | Hard constraint |

Already considered and rejected, do not revisit: NHS GP surgeries,
physiotherapy, hospital outpatients, community pharmacy, mental health
clinics, veterinary.

---

## 4. The practice being modelled

All of this is public information, rewritten. Re-check before publishing.

### Hours
Monday to Friday, 08:00 to 18:00, closed 13:00 to 14:00 for lunch.
Closed weekends.

### Contact and escalation
- Practice: 01925 630221
- Address: 11-13 Museum Street, Warrington, Cheshire, WA1 1JA
- Out of hours dental, Liverpool, Wirral and Cheshire: 0161 476 9651
- NHS urgent care, and anyone without a dentist: 111
- Life threatening emergency: 999

### How patients book, which is where the gaps are
- NHS appointments cannot be booked online or by email. Phone only.
- Private examinations and hygiene can be booked online, with a deposit
  for exams and the full fee for hygiene, non refundable on a no show or
  a cancellation inside 24 hours.
- Urgent appointments: a limited number each day, released by phone from
  8am on the day. Open to existing patients and to people with no regular
  dentist. Deals with the immediate problem only.
- New NHS patients: capacity limited, the online waiting list is
  currently paused because demand is high. NHS.uk and the practice
  website disagree on this, so Sophia answers honestly about limited
  capacity rather than picking a side.
- Registration lapses after more than three years without attending.
- The practice no longer sends reminders to book check ups. This is the
  justification for the outbound flow.

### NHS charges, England, from 1 April 2026
| Band | Charge |
|---|---|
| Urgent | £27.90 |
| Band 1 | £27.90 |
| Band 2 | £76.60 |
| Band 3 | £332.10 |

No cosmetic treatment on the NHS. No implants on the NHS.

### Private fees
Examinations: new patient £85, routine £50, review £40.
Emergency consultation: £85 new or lapsed, £50 existing.
Extraction £145 routine, from £325 surgical, £40 post extraction dressing.
Hygiene by length: £55 / £75 / £95 / £135 for 20 / 30 / 40 / 60 minutes.
Hygienist direct access £140.
Root canal with the special interest dentist: £85 consultation, from £450
incisor or canine, from £470 premolar, from £595 molar. With a general
dentist, £320 incisor or canine, excluding the restoration.

Fillings, imaging, implants, whitening, crowns, orthodontics and dentures
are not modelled. For anything not in the price table, take a message.

### Attendance policy
- Cancel with at least 24 hours notice. Less than that is a Short Notice
  Cancellation.
- Not attending without telling the practice is a Failure to Attend.
  Future appointments are cancelled and a new course of treatment may be
  needed.
- Within a rolling 24 months: existing patients are discharged at 2
  failures or 3 short notice cancellations. New patients at 1 failure or
  2 short notice cancellations. One failure counts as two short notice
  cancellations.
- Extenuating circumstances are considered and discharge can be appealed.

---

## 5. Scope

**Inbound, v1:** greeting with the AI and recording notice, safety
screening, urgent same day appointments respecting the 8am release,
honest answers on NHS availability, routine and hygiene booking,
cancelling and rescheduling with the 24 hour warning, checking an
appointment after verification, FAQ answers from retrieval, registering a
caller new to the practice as a private patient (never an NHS check up,
the NHS waiting list is paused), and taking a message.

**Outbound, phase 2:** day before reminders, and recall calls for
patients approaching the three year lapse. If someone else answers,
reveal nothing. Never leave details on a voicemail.

**Out of scope:** real phone numbers, practice management system
integration, taking payment, treatment plans, clinical advice, assessing
NHS fee exemptions, multiple languages.

---

## 6. Safety and privacy

**Clinical.** Sophia never diagnoses, never recommends treatment, never
advises on medication. Medication questions go to a pharmacist or 111.

**999 red flags**, verified against nhs.uk on 16 September 2026: severe
swelling of the mouth, lips, throat or neck together with difficulty
breathing or difficulty opening one or both eyes; heavy bleeding that
will not stop; serious injury to the face, mouth, jaw or teeth; any head
or face injury causing loss of consciousness, vomiting or double vision.
Also tell them not to drive themselves. Implement as deterministic
keyword rules first, then a classifier, running before any booking tool.

**Same day, not 999:** severe toothache not controlled by painkillers,
facial swelling without breathing difficulty, continued bleeding after an
extraction, a tooth knocked out or broken by injury.

**Privacy.** Full name, date of birth and postcode before revealing or
changing anything. A failed check must not say which part was wrong. Do
not store call audio. Transcripts hold fake data only. In production this
would need a data processing agreement, UK hosting and clinical
oversight, since health data is special category data under UK GDPR.

**Honesty.** Sophia says she is an AI assistant in the greeting and
whenever asked. She never invents availability, fees or policy. When the
data is missing, she takes a message.

---

## 7. Technical

**Stack:** Python 3.11+, FastAPI, Pipecat with call audio over a
WebSocket (WebRTC failed for remote callers, see the engineering log),
Gemini `gemini-3.5-flash-lite` by default, falling back in order to
`gemini-3-flash-preview`, `gemini-3.1-flash-lite` and Groq
`openai/gpt-oss-120b` when a model is out of quota (`LLM_FALLBACKS`), Deepgram `nova-3` en-GB for speech to text
and `aura-athena-en` for text to speech, SQLite, keyword retrieval over
markdown (embeddings were dropped, the reason is in `requirements.txt`),
pytest plus a live scenario suite in `evals/`.

**Free tier protection:** keep the system prompt short, keep practice
facts in retrieval rather than the prompt, keep the tool surface around a
dozen, validate every tool call in code, and stay under the Gemini free tier of 15 requests a minute.

**Layout:**
```
data/practice_info/   FAQ markdown for retrieval
data/seed/            clinicians, appointment types, fake patients
src/sophia/
  config.py     constants and model names
  clock.py      opening hours, 8am release, BST, spoken UK dates
  db.py         schema and seeder
  policies.py   business rules as pure functions
  tools.py      what the model may call, and what it may not
  schemas.py    tool definitions
  prompts.py    system prompt
  agent_text.py conversation loop
  safety.py     emergency screening and the medicine rule
  knowledge.py  keyword retrieval over data/practice_info
  providers.py  Gemini and Groq adapters, retries
  outbound.py   reminder and recall calls
  voice_app.py  Pipecat pipeline, turn taking, web server
  sessions.py   per tester database copy and transcript
  demo.py       the demo patient list shown to testers
  web_audio.py  PCM wire format for the WebSocket
evals/          live scenario suite, framework and scenarios
scripts/        init_db, chat, check_setup, list_models, run_evals
tests/          564 tests, no API key needed
web/            call page
```

**Times** are stored as local wall clock. The reasoning is written at the
top of `clock.py`. Note that Python treats subtraction between two aware
datetimes sharing a tzinfo object as wall clock arithmetic, so elapsed
time comparisons convert to UTC first. There is a test for this.

---

## 8. Evaluation targets, Day 6

Scripted scenarios with expected tool calls, reported as a pass rate:
existing NHS patient books a check up; new private patient books the £85
examination; NHS availability answered honestly; urgent call at 07:45
versus 09:00; urgent with nothing left; a 999 red flag with no booking
attempted; cancelling 30 hours ahead versus 5 hours ahead; a new patient
one cancellation from discharge; a lapsed registration treated as new; a
wrong date of birth revealing nothing; a medication question refused
safely; lunch hour and weekend requests; outbound where someone else
answers. Plus per turn latency, median and p90.

When a scenario fails, decide first whether the cause was retrieval, tool
logic, or model generation. Fix the actual layer, not the prompt.

---

## 9. Assumptions, recorded in the README

- The practice website gives both 8am and 9am for the urgent line. Using
  8am.
- "New patient" in the attendance policy is undefined publicly. Assuming
  less than 12 months at the practice.
- Number of hygiene therapists unknown. Modelling two.
- Provider on the CQC register shows as ICS Dental Limited. Re-check
  before publishing.
