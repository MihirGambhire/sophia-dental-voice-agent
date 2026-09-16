# Engineering log

A running record of what was built, what broke, and why.

The bugs are the useful part. Most of them were not visible in the code
and were only found by running the thing for real, which is the point of
keeping this. Every entry says what the symptom looked like, what the
cause actually turned out to be, and what changed as a result.

One habit runs through all of it, and it is worth naming because it
decided nearly every fix below: **work out which layer is failing before
changing anything.** A wrong answer from an agent looks the same whether
the cause is the prompt, the tool, the data or the transport. Guessing
usually means editing the prompt, which is the layer least likely to be
at fault and the only one that cannot be tested.

---

## Contents

- [Build log](#build-log)
- [Challenge log](#challenge-log)
- [Patterns worth keeping](#patterns-worth-keeping)

---

## Build log

### Day 1, 16 September 2026: foundations

| Step | Detail |
|---|---|
| Repo | Created outside OneDrive, since sync corrupts SQLite files and virtual environments |
| Schema | 9 tables: patients, clinicians, working hours, appointment types, appointments, urgent slots, attendance events, messages, reminder queue |
| Seed data | 6 invented clinicians, 22 appointment types with the real published fees, 12 fake patients |
| Knowledge base | 8 practice documents, rewritten in our own words rather than copied |
| Time layer | `clock.py`: opening hours, lunch closure, the 8am urgent release, UK spoken dates |
| Tests | 42, covering schema constraints, seed integrity and time handling |

Two design decisions made here that paid off later:

**Seed dates are stored as offsets from the build date**, not fixed dates,
so the demo never goes stale. The seeder also moves any appointment that
would land on a weekend, in the lunch hour, in the past, or on a day that
clinician does not work.

**Working hours are stored as a morning block and an afternoon block**
rather than one span with a lunch exception. The 1pm to 2pm closure then
becomes unbookable by construction, rather than by a rule somebody has to
remember to apply.

### Day 2: rules, tools, conversation

| Step | Detail |
|---|---|
| `policies.py` | Every business rule as a pure function, no database, no model |
| `tools.py` | Thirteen tools over the database, plus the refusals |
| `schemas.py` | Tool definitions sent to the model |
| `prompts.py` | System prompt, deliberately short and holding no facts |
| `agent_text.py` | The conversation loop |
| `knowledge.py` | Lexical retrieval over the practice documents |
| Tests | 205, then 240, all passing without an API key |

The safety argument lives in the tool layer, not the prompt. Every patient
tool raises before doing anything if the caller has not been verified, and
that is an `if` statement in Python rather than an instruction in a
prompt, so no amount of persuasion in the conversation gets past it.

---

## Challenge log

### 1. Every package version I reached for was stale

**Symptom.** Wrote `requirements.txt` from memory: groq 0.33, pytest 8.4,
chromadb 1.3.

**Cause.** Knowledge cutoff. Nothing more interesting than that.

**Fix.** Queried the PyPI JSON API for each package. Real answers were
groq 1.7.0, pytest 9.1.1, chromadb 1.5.9, sentence-transformers 6.0.1.
Every pin now carries the date it was checked.

**Lesson.** A version number recalled rather than looked up is a guess
wearing a costume. This one cost thirty seconds to check and would have
cost an hour to debug.

---

### 2. Windows has no timezone database

**Symptom.** `ZoneInfoNotFoundError: No time zone found with key Europe/London`,
on a machine where Python was fine and the code was correct.

**Cause.** Windows ships no IANA timezone database. Linux and macOS do, so
this never appears in most tutorials.

**Fix.** Added `tzdata` to requirements, with a comment saying why, and a
check in `scripts/check_setup.py` so the next person gets a useful error.

---

### 3. The British Summer Time bug

**Symptom.** A test asserting that 10am Saturday to 10am Sunday across the
October clock change is 25 hours returned 24.

**Cause.** This one is a genuine trap in Python. Subtracting two timezone
aware datetimes that share the same `tzinfo` object performs **wall clock**
arithmetic and ignores the timezone entirely. Both values were aware, both
were correct, and the subtraction was still wrong.

**Fix.** Convert both sides to UTC before subtracting.

**Why it mattered.** 24 hours is exactly the line between a normal
cancellation and a Short Notice Cancellation, and a patient who collects
enough of those is removed from the practice list. Every cancellation near
that boundary in late October would have been judged wrongly, in a way
nobody would notice until someone was wrongly discharged.

**Lesson.** The test that catches a real bug is usually the one written
for the case you assume already works. This was written expecting it to
pass.

---

### 4. Slots were all offered from the same morning

**Symptom.** Asked for availability, Sophia offered four times, all on one
day, ten minutes apart. Technically correct, conversationally useless.

**Cause.** The search loop stopped once it had collected enough individual
slots. A single morning yields dozens, so it never looked at a second day.
The spreading function that was meant to pick a range had only one day to
pick from.

**Fix.** Count days with availability rather than slots.

**Lesson.** A unit test of either function alone would have passed. The
bug only existed in the composition, which is where the conversation lives.

---

### 5. The public repo contained personal information

**Symptom.** The project context file, committed on Day 1, held interview
detail, names and employment history. The repository was then made public.

**Fix.** Rewrote both commits with `git filter-branch`, replaced the file
with a technical-only version, added the original to `.gitignore`, kept
the full copy outside the repository.

**What I got wrong, and checked rather than assumed.** After force
pushing, I verified whether the old content was actually gone. It was not.
GitHub still served the original file through its old commit SHA, because
force-pushed commits become unreachable but are not destroyed until
garbage collection, which may never run on its own. The only reliable
removal is deleting and recreating the repository.

**Lesson.** "I ran the command" is not "the thing is done". The check took
one API call.

---

### 6. The model in the provider's own documentation did not exist

**Symptom.** `404: The model llama-3.3-70b-versatile does not exist or you
do not have access to it.` The key was valid, and the model name came
from Groq's own documentation page, read the previous day.

**Cause.** The model had been retired. The documentation had not caught up.

**Fix.** Queried the live `/models` endpoint, which is the only source of
truth, and moved to `openai/gpt-oss-120b`. Added `scripts/list_models.py`
so this is checkable in one command, and taught `check_setup.py` to
recognise the error and suggest it.

**Lesson.** Documentation describes intent. The API describes reality.

---

### 7. Sophia said "I have booked you in" and booked nothing

**Symptom.** A four turn booking conversation ended with a confident
confirmation: day, time, clinician, fee. The appointments table had zero
new rows.

**Cause.** Two things at once. The model never called `book_appointment`,
and it had no way to know that nothing had been written, so its own
previous message was the only evidence it had, and it believed it.

**Fix.** A short call state block, regenerated every turn, that says
plainly what has and has not been booked. Plus an explicit prompt rule
that the tool must confirm before the outcome is described.

**Why this is the worst bug in the log.** Every other failure here is
visible to the caller at the time. This one is invisible. The patient
hangs up satisfied, does not turn up, and is recorded as a Failure to
Attend, which counts towards being removed from the practice. The system
would have harmed the person it was talking to.

**Related.** The same conversation quoted £50 to an NHS patient whose
correct charge is £27.90, because the type was chosen before the funding
was known.

---

### 8. Turns took 20 to 90 seconds

**Symptom.** Sporadic, worsening through a conversation. Early turns under
two seconds, later ones stalling for a minute.

**First guess, wrong.** These are reasoning models, so the reasoning must
be slow. Tested it: single calls at every reasoning effort returned in
under 1.1 seconds. Not the cause.

**Actual cause.** Read the rate limit headers. Groq's free tier allows
**8,000 tokens per minute**, and the tool schemas alone cost 1,719 tokens
on *every* request. Four requests exhausted the minute, after which the
SDK sat in backoff. The stalls were throttling.

**Fix.** Cut the schemas to what the model cannot infer from the name and
parameters, and compressed the system prompt. Fixed cost per request went
from 2,474 tokens to 1,578.

**Lesson.** The symptom said "slow model". The cause was in the transport
layer. Measuring before changing anything took one request; the wrong fix
would have been a week of prompt tuning.

---

### 9. My own fix made the problem worse

**Symptom.** After adding history trimming to save tokens, Sophia called
`verify_patient` on every single turn, having already verified the caller.

**Cause.** The trimming deleted old tool results outright. The model
therefore had no evidence the verification had happened, so it did it
again. Each redundant call is another full round trip resending the prompt
and the schemas, so a token saving measure was spending more tokens than
it saved, and causing the very stalls it was meant to prevent.

**Fix.** Carry forward the **conclusions** rather than the transcript. The
state block costs about forty tokens and removes the need for any of it.

**Lesson.** An optimisation that removes information the system depends on
is not an optimisation. Measure after, not just before.

---

### 10. A single character killed the conversation

**Symptom.** `UnicodeEncodeError: 'charmap' codec can't encode character
'‑'`, mid conversation, ending the session.

**Cause.** The model wrote "check-up" using a non breaking hyphen. Windows
consoles default to cp1252, which has no such character.

**Fix.** A normaliser over everything Sophia says, mapping typographic
characters to plain ones, plus UTF-8 output in every script. It also
enforces the project's no dashes writing rule, and will stop the speech
engine having to guess at these characters on Day 4.

**Lesson.** Model output is untrusted input. It will eventually contain
every character you did not plan for.

---

### 11. The setup checker blamed the wrong thing

**Symptom.** `Live call failed: No module named 'groq'`, followed by the
advice "Check the key is valid and that you have internet access."

**Cause.** A bare `except Exception` with one generic message. The real
problem was running system Python instead of the project virtual
environment.

**Fix.** Handle `ModuleNotFoundError` separately, and branch on the error
text for model-not-found, bad key and rate limited, each with the specific
command that fixes it.

**Lesson.** A diagnostic that confidently misdiagnoses is worse than no
diagnostic, because it sends you somewhere else.

---

### 12. Sophia invented the opening hours

**Symptom.** Asked "what are your opening hours?", she answered: nine am to
six pm, Saturdays nine to one, closed Sundays and public holidays. Three
facts, all wrong. The real hours are eight to six, weekdays only, closed
between one and two. **She called no tool at all.**

**Cause.** There was no tool that could answer the question. FAQ retrieval
was scheduled for a later day. The system prompt already said, in
capitals, that she knows nothing a tool has not told her, and that was
never going to be enough: given a question it cannot serve and no way to
look anything up, a model fills the gap.

**Fix.** Built `knowledge.py`, lexical retrieval over the practice
documents, and wired in a `search_practice_info` tool. Critically, it
returns **nothing** rather than a weak match when the question is not
covered, so "I do not know, shall I take a message" stays a reachable
outcome.

Verified live afterwards: correct hours including the lunch closure,
correct weekend answer with the out of hours numbers, correct address, and
a graceful refusal for something the practice does not do.

**Lesson, and the most important one here.** You cannot instruct a model
out of a capability gap. "Never guess" is not a behaviour, it is a wish,
unless there is somewhere to look and permission to come back empty. This
was found by running a setup check whose purpose was to test the API
connection, not the answer.

---

### 13. Three bugs inside the retrieval fix

Worth recording because the fix for a hallucination bug contained its own
small failures, all found by tests written before the code was trusted.

**Stopwords were stripped before synonym expansion.** "Where are you?"
tokenised to nothing at all, because "where", "are" and "you" are all
stopwords, so the question was deleted before it could be mapped to
"Museum Street". Expansion has to run on the raw words.

**Duplicate keys in the synonym dictionary.** `where`, `miss` and `missed`
were each defined twice. Python keeps the last silently, so half the
mappings never existed. No error, no warning.

**Synonyms mapped to concepts rather than to words in the documents.**
"Address" expanded to "address, located", and the address section of the
document contains neither word. It contains "Museum Street" and
"Warrington". Retrieval matches text, not meaning.

---

### 14. Two of my own tests were wrong, not the code

Both worth keeping, because the instinct on a red test is to change the
code.

**The jargon test.** Asserted that a warning to a patient with a missed
appointment contains the phrase "missed appointment". It did not, because
that patient is always one cancellation from discharge, so the warning
correctly takes the more serious branch. The code was right; the test
assumed the wrong branch.

**The line wrap test.** Asserted that retrieved text contains "failure to
attend". It contains "failure to\nattend", because the source markdown is
hard wrapped. Fixed by collapsing whitespace in the test helper.

---

## Patterns worth keeping

**Decide which layer is failing before changing anything.** Slow turns
looked like a slow model and were a rate limit. An invented answer looked
like a weak prompt and was a missing tool. In both cases the obvious fix
would have been prompt tuning, which is the one layer that cannot be
tested and was not at fault either time.

**Enforce in code what matters, instruct only what does not.**
Verification, the cancellation warning, opening hours and fees are all
enforced by Python that raises. The prompt covers tone and ordering. A
rule in a prompt is a preference; a rule in an `if` statement is a
guarantee.

**Make "I do not know" reachable.** Every tool that can come back empty
does, with wording that offers a message. A model with no permitted way to
fail will invent a success.

**Verify the claim, not the command.** The force push ran cleanly and did
not do what it appeared to. The model name came from official
documentation and did not exist. Both took one command to check.

**Run it.** 205 passing tests did not catch the dead model, the false
booking, the throttling or the invented opening hours. Every one of those
came from a live run, and three of the four were found by accident while
checking something else.
