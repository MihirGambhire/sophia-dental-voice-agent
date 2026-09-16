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
| `providers.py` | Groq and Gemini behind one interface |
| `safety.py` | Deterministic clinical screening, ahead of the model |
| Tests | 328, all passing without an API key |

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

### 15. Gemini rejected a conversation it had just produced

**Symptom.** `400 INVALID_ARGUMENT: Function call is missing a
thought_signature in functionCall parts`, on the second request of a
conversation, replaying tool calls Gemini itself had returned.

**Cause.** Gemini 3 attaches an encrypted `thought_signature` to function
call parts, carrying its own reasoning state. The documented rule for
stateless conversations is that the model's parts must be resent **exactly
as received**. The adapter was rebuilding them from the OpenAI style
message, which produces something that looks equivalent and is refused.

**Fix.** Cache the `Content` objects Gemini returns, keyed by tool call id,
and replay those objects rather than reconstructing them.

**Lesson.** When translating between two APIs, the difference that breaks
you is rarely the request shape. It is the state one of them expects you
to carry back untouched.

---

### 16. Half the Gemini models were unavailable

**Symptom.** `503 UNAVAILABLE: This model is currently experiencing high
demand`, on the model named in the config.

**Cause.** Genuine capacity pressure, not a bug. But a demo that depends on
one model name is a demo that fails on the day.

**Fix.** Probed eight flash variants directly. Three returned 503,
`gemini-3.6-flash` took 32 seconds for a single tool call, and
`gemini-3.5-flash-lite` answered in 0.85 seconds with tool calling intact.
Defaulted to that.

Measured on the same five turn booking conversation:

| Provider | Median | Max | Notes |
|---|---|---|---|
| Groq, gpt-oss-120b | 46s | 69s | stalling on the token per minute cap |
| Gemini, flash-lite | 3.25s | 10.2s | no stalls |

Both wrote the appointment correctly at the NHS fee of £27.90.

**Lesson.** Pick a model by measuring it on your own workload, on the day.
Benchmarks and documentation describe somebody else's.

---

### 17. A test failure printed both API keys

**Symptom.** A failing test's traceback included the full `repr` of the
settings object, which contained the Groq and Gemini keys in plain text.

**Cause.** A dataclass puts every field in its `repr` unless told not to.
Any exception anywhere near config would print them.

**Fix.** `repr=False` on every key field, plus a test that fails if a
configured key ever appears in a repr again.

**Lesson.** This is how keys reach log files, screenshots and pasted
tracebacks. It was found by accident, through an unrelated test failing,
which is not a strategy.

---

### 18. The safety screen missed "I can't really breathe"

**Symptom.** A test asserting that an emergency phrased with hedging is
still an emergency came back as merely urgent.

**Cause.** The pattern required "can't" and "breathe" to be adjacent. One
word between them, and a 999 case was silently downgraded to an ordinary
same day appointment.

**Fix.** Allow up to three words between the difficulty and the thing being
difficult, across breathing, swallowing, speaking and opening an eye.
Frightened people hedge. "I can't really breathe very well" and "it's
probably nothing but I'm struggling a bit to breathe" are the same
emergency as "I can't breathe".

**Why this is the worst near miss in the log.** Every other bug here costs
time or money. This one sends someone with a compromised airway away with
a Thursday appointment. It was caught only because the test was written in
the caller's words rather than a clinician's.

**Lesson.** Write safety tests in the voice of a frightened person playing
it down, not in the voice of a clinician being precise. The second kind
passes and means nothing.

---

### 19. The voice was American

**Symptom.** None. Nothing failed. The Day 1 config named
`aura-2-thalia-en` as the text to speech voice, which was a placeholder
written from memory, and nothing in the project would ever have objected.

**Cause.** Thalia is an American voice. A demo about a Warrington dental
practice, built carefully around UK dates, UK postcodes, NHS bands and
British English phrasing, would have opened its mouth and sounded like
somebody in California.

**Fix.** Queried the Deepgram models endpoint. It lists four British
voices. `aura-2-pandora-en` is the newer generation and reads calm and
smooth, which suits someone who has to talk to people in pain.

**Lesson.** The bugs that cost you the room are not always the ones that
raise an exception. This one would have been noticed by the interviewer
in the first three seconds of the demo video, and by nobody before that.

---

### 20. A test helper patched the wrong kind of method

**Symptom.** Twelve voice tests failing with `noop() takes 2 positional
arguments but 3 were given`.

**Cause.** The helper stubbed the parent class's `process_frame` to skip
pipeline bookkeeping. Patching a method on the class replaces an unbound
function, so the stub also receives `self`. The stub only accepted two
arguments.

**Fix.** Give the stub a `self` parameter.

**Lesson.** Small, but a good reminder that the instinct on a wall of red
is to doubt the code under test. Here the code was fine and the scaffolding
was wrong, which is the third time that has happened in this log.

---

### 21. The shared link worked for me and nobody else

**Symptom.** A public tunnel link worked on the machine running the
server. A friend opening the same link saw "Call ended" the moment they
pressed Start call.

**Cause.** The server logs showed every remote attempt going from ICE
checking straight to closed, never connected. The tunnel carries HTTP
only. WebRTC audio is peer to peer UDP that never goes through it. With
the server behind one home router and the caller behind another, and only
STUN configured, there was no route between them.

**What did not work.** The free relay every tutorial still lists,
openrelay.metered.ca with the credentials openrelayproject, is retired
and its hostname no longer resolves. Three other free relays produced no
relay candidates at all when tested with relay-only ICE.

**Fix.** A free Metered account, whose relay issues short lived
credentials from a REST endpoint, fetched by the server and served to the
browser from /api/ice so both ends agree. Verified by forcing the browser
to relay only, which forbids the local network path entirely, and
confirming the connection still reached connected. The winning pair was
relay on the browser side and peer reflexive on the server side.

**Also fixed.** Failing to connect at all and a call that connected and
then ended both said "Call ended", which is what sent the first round of
debugging to the friend's browser rather than the network.

**Lesson.** Testing a shared link from the machine that serves it proves
nothing about sharing it. The relay-only test simulates a remote caller
from the local machine, which is the only honest check short of a second
network.

---

### 22. Three fixes to WebRTC, and the right answer was to stop using it

**Symptom.** After adding a relay, a phone on mobile data still could not
connect, and the fix after that did not help either.

**What each round found.** First, the office network blocks outbound UDP
on ports 80 and 443, and the server's WebRTC library only ever tries the
first relay address, which Metered list as UDP. Probed directly, UDP timed
out at exactly five seconds and TCP connected in 0.1 seconds. Fixed the
ordering. Second, the page only waited 2.5 seconds for relay addresses
before sending its offer. Fixed. Third, the logs from the next phone
attempt showed the server's relay refusing to bind a channel to the
phone, a Jio mobile IPv6 address, with 401 Unauthorized, inside a
library with limited relay support.

**The step back.** Every fix could only be tested from the machine running
the server, which was the one case that already worked. Meanwhile the web
page itself had reached every caller, every time, over HTTPS through the
tunnel. The failing part was the only part taking a different road.

**Fix.** Carry the call audio over a WebSocket on that same road. Raw
16 kHz PCM both ways, a serializer of about thirty lines, and an audio
worklet in the page that resamples the microphone. No direct path, no
relay, no ICE. If the page loads, the call connects.

**Verified from outside the server, not just beside it.** Through the
public tunnel: the WebSocket opened in one second, Sophia's greeting began
arriving after 3.6 seconds, and a synthesised spoken question, sent as
microphone audio, was transcribed word for word and answered correctly
using search_practice_info.

**The cost.** Slightly more latency than peer to peer, and TCP retransmits
lost packets rather than skipping them. The microphone is also muted while
Sophia speaks, so a phone speaker cannot feed her own voice back to her,
which means she cannot be interrupted yet.

**Lesson.** When three careful fixes to one layer have not worked, check
whether that layer needs to exist. The evidence was there from the start:
the page never failed.

---

### 23. The first real phone tests: seven bugs in one transcript

Friends tested the shared link on their phones. One transcript showed a
raw SQLite error on screen, Sophia answering half sentences, the same
exchange repeated seven times, and Sophia telling a caller "I've got you
verified now" for details that matched nobody. Each was traced separately.

**1. No database tool had ever worked in a voice call.** The connection
was opened on the event loop thread and used from the worker thread that
runs each turn, which SQLite refuses. Reproduced four out of four times.
My previous public link test had asked about opening hours, the one
question that never touches the database, which is why it passed. Fixed
with a connection shared across threads, made safe by a lock that allows
one turn at a time.

**2. Every fragment of speech got its own reply.** Speech to text
finalises on short pauses, so "thirteenth April two thousand and" was
answered before the year arrived, and Sophia accepted half a date of
birth. Turns are now joined until the caller has been silent for 1.2
seconds, and voice activity or interim words hold the turn open.

**3. She claimed a verification that never happened.** The prompt already
forbade it. Her reply is now checked against the call state, and a claim
of verification or of a booking that the state contradicts is replaced
before the caller hears it.

**4. The transcript repeated itself.** The page polled every 0.7 seconds
and a request over the tunnel from a phone takes longer, so overlapping
polls rendered the same lines twice. The server's own record had no
duplicates. One poll at a time now.

**5. The caller saw the exception, and the logs did not.** Failures now go
to the server log in full and the caller gets a sentence.

**6. Calls dropped after about a minute.** At the exact second a call
died, the tunnel logged "failed to accept QUIC stream: timeout". The
tunnel reaches Cloudflare over QUIC, which is UDP, which this network
blocks, and each drop of that link killed every call on it. Running the
tunnel with `--protocol http2` over TCP fixed it; the next two minute test
call held throughout.

**7. The model invented a postcode.** Captured directly, three runs
before the caller had given one, the model passed "UNKNOWN", then
"placeholder", then WA1 1LZ, a plausible postcode it made up, and the
caller was told no record existed. The tool now refuses a postcode the
caller never actually said, checked against the caller's own words with
one character of tolerance, because speech to text also heard "W A 1,
2 N F" as "W one two n f". Separately it heard "Hollis" as "Hollies", so
the name now only has to be very close, and only when exactly one record
fits: twins share a date of birth and an address.

**Verified end to end over the public link**, with synthesised speech as
the microphone: the misheard surname, the invented postcode, and the
dropped letter all occurred, and the booking was still written to the
database at the correct NHS fee.

**Lesson.** The earlier test chose a question that avoided the broken
layer. A test is only as good as the path it exercises, so the end to end
test now completes a booking, which touches every layer at once.

---

### 24. Letting callers interrupt, without Sophia interrupting herself

**Symptom.** Testers could not talk over Sophia. The microphone was muted
while she spoke, deliberately, so a phone speaker could not feed her voice
back in. But everyone tries to cut in on a phone call within a minute.

**The real problem was echo, not interruption.** Stopping her is one call,
`broadcast_interruption()`, which clears her queued speech, and the
serializer already told the browser to drop audio it had queued. The hard
part is that with the microphone open, her own voice returning through a
speaker looks exactly like a caller interrupting. Browser echo
cancellation varies between phones, so it is not relied on.

**Fix.** Everything transcribed while she speaks, and for 2.5 seconds
after, is compared with the words she is actually saying. Mostly her words
means echo: ignored for interruption, and never turned into a caller turn.
A genuine interruption needs two words that are not hers, or one of the
short words people cut in with, so a cough or "mm" does not stop her. The
greeting is registered too, because it is spoken from outside the
processor and its echo would otherwise be the first thing a caller "said".
The agent is also told which reply was cut off, so it does not assume the
caller heard a fee they never heard.

**Verified live over the public link.** Her greeting was fed back into the
call as it arrived, 350 milliseconds late and at half volume, to imitate a
phone speaker. The logs show all six fragments were transcribed and each
was rejected as echo: no interruption, no reply to herself. Then a caller
talked over her answer about opening hours; the browser was told to stop
playback 0.96 seconds later, and she answered the new question correctly.

**Lesson.** Checking the logs mattered. A passing echo test could have
meant the echo was too quiet to be transcribed at all, in which case the
filter proved nothing. It was transcribed in full, and rejected.

---

### 25. The first scenario suite blamed the model for a tool bug

**Symptom.** Every test so far had run against a scripted stand in for
the model, so a suite of scripted calls against the real one was built,
judged on outcomes and attributing each failure to a layer. In one early
run a caller asked for "something in the afternoon, after two o'clock",
and Sophia said there were no afternoon appointments. The afternoons were
almost empty. The report put it down to generation.

**Cause.** The slot search had no time of day filter, and it spread
results across days by taking each day's earliest free slot. It could
only ever return mornings. Sophia repeated what her tool told her,
accurately.

**Fix.** `find_available_slots` takes `after_time` and `before_time`. The
check now attributes the failure by whether an afternoon slot was ever
offered to the model: offered and ignored is generation, never offered is
retrieval. It passed live after the fix.

**Lesson.** Attribution rules are code, and they were wrong here in the
most tempting direction. The transcript, not the label, is the evidence.

---

### 26. The suite failed Sophia for speaking like a person

**Symptom.** Two failures in the first runs. She quoted "twenty-seven
pounds ninety" and was marked as not quoting £27.90. Later, in the first
full three run evaluation, she warned that a cancellation was "less than
twenty-four hours away" and "a short-notice cancellation", and failed for
not saying "24" and "short notice".

**Cause.** The checks were written for text on a screen. Her prompt tells
her to say numbers the way a person would, because every reply is spoken.

**Fix.** Numeric patterns accept words and hyphens throughout. In that
full evaluation Sophia passed 38 of 39 completed runs, and the one failure
was this check.

**The same fix exposed a worse problem in the other direction.** A leak
check matching only "11 am" would pass "eleven in the morning", which is a
real leak of a patient's appointment. A check that fails wrongly costs a
look at a transcript; one that passes wrongly hides exactly what it
exists to catch. Leak checks now share one set of patterns covering
spoken days, ordinals and times, with a test that phrases a leak in words.

---

### 27. Quota and network errors were being scored as failures

**Symptom.** Runs stopped partway with a 429, and on another occasion a
laptop DNS failure cost four runs. Both appeared in the pass rate as
failures, and retrying after the provider's suggested delay walked
straight back into the limit.

**Cause.** Gemini's free tier allows 15 requests a minute per model, and a
single turn with tool calls is several requests. The window is rolling,
so a wait shorter than a minute is not a real wait.

**Fix.** Rate limited and network failed runs wait at least a minute (15
seconds for network), rerun the whole scenario from a fresh database, and
are reported separately as incomplete. The report counts only runs that
finished. The provider adapter also retries 503 and 429 responses with a
short backoff during a live call.

**Then the daily limit.** The final rerun was rate limited on every
attempt, including after full minute waits. A single probe request showed
why: the quota hit was `GenerateRequestsPerDayPerProjectPerModel-FreeTier`,
500 requests a day, used up by a day of live phone testing plus the
earlier full evaluation. No wait shorter than the daily reset helps, so the
run was stopped rather than left to spend hours writing a report of
skipped runs.

**Lesson.** The same limits apply to the live demo: a handful of people
talking to Sophia at once can reach the minute limit, and a busy day of
testing reaches the daily one. Both are written into the README
limitations rather than left for a tester to discover. The runner should
also tell the two apart and stop at once on a daily limit.

---

### 28. The medicine rule was the one rule still living in the prompt

**Symptom.** None in testing. Found by reading the code against the
project's own principle. The only thing preventing medicine advice was a
line in the system prompt, while `safety.py` already had a medication
detector and a scripted refusal that nothing called.

**Fix.** Sophia's reply is checked before the caller hears it. A dose, a
schedule, or a recommendation of a named medicine replaces the reply with
the scripted refusal and a referral to a pharmacist or 111. A medicine
question answered without that referral gets it added. The match is on
Sophia's own words, so "I can't advise on ibuprofen" passes and "you could
take ibuprofen" does not.

At the same time, `chromadb` and `sentence-transformers` were removed from
`requirements.txt`. Retrieval had become keyword matching and neither was
imported anywhere, but anyone cloning the repo would still have
downloaded about 2.5 GB, mostly PyTorch.

**Lesson.** A principle stated in the README is only as true as the last
place someone forgot it. Worth auditing for, not just following.

---

### 29. Outbound calls: nothing to leak, rather than a rule not to

**Problem.** When Sophia rings a patient, whoever answers might not be
the patient. The usual approach is a prompt line saying "do not reveal
the appointment", and this project has watched the model ignore lines
like that more than once.

**Design.** The model is never told. Its call context holds the patient's
name, so it can ask for them, and nothing else. Appointment details only
reach it after `verify_patient` succeeds for that exact patient. A test
searches everything sent to the model on the first turn for the
appointment's day, time and clinician, and finds none of them.

Two more guarantees are in code. A household member who passes the
identity check with their own details is refused like any mismatch,
because they are not the patient being called. A patient decision such as
"confirmed" cannot be recorded without verification, while "wrong person"
and "call back later" can.

**Lesson.** The strongest privacy control is information the model does
not have. It cannot be argued out of it, and it cannot say it by accident.

---

### 30. Falling back to other models, and what the fallbacks revealed

**Problem.** With the daily quota gone, every caller heard an apology
until the reset at midnight Pacific time. Adding keys from other accounts
was considered and rejected: Google's API terms forbid circumventing
usage limits, and a key rotation scheme in a public repo says the wrong
thing about the person who wrote it. But each Gemini model has its own
quota, so a list of models tried in order is within the rules.

**Choosing the list.** Every candidate was sent the same real request:
Sophia's prompt and tools, "what time do you open on Monday", then a tool
result. Two models the models endpoint lists, gemini-2.5-flash and
flash-lite, answered 404. gemma-4-31b-it took 50 seconds. gemini-3.8-flash
returned 503. gemini-3-flash-preview and gemini-3.1-flash-lite both called
the right tool and answered correctly, in 1.4 to 3.7 seconds a request.
Groq was fastest at 0.6 seconds and goes last, for the reason below.

**Design.** A daily quota moves to the next model at once and rests that
model until the reset, because retrying only adds silence on the line. A
per minute limit or a 503 rests it briefly. Resting is shared across
calls, so only the first request finds out. A bad request is raised, not
passed along, because trying another model would disguise a bug as a
quota problem. The reset time is computed in UTC, for the same reason as
entry 3.

**Bug found live: Gemini could not take over from Groq.** With Groq first
in the list, Groq hit its token per minute cap on turn two, Gemini
rejected the conversation with a 400 for missing thought signatures, and
the call ended. Groq's tool calls have no signatures, and Gemini refuses
rebuilt function call parts without one. The older code assumed Gemini
only objected once it had issued a signature, and a unit test encoded
that assumption. Tool calls made by another provider are now described to
Gemini as plain text, which carries no signature to check. The rerun
switched between Groq and Gemini five times across two turns without an
error. Switching between Gemini models needed nothing: Google documents
that the backend handles thought signatures from a previous model.

**What the fallbacks revealed.** Running a real call on them showed
behaviour the primary had been hiding:

- Turns took 9 to 33 seconds, against a median near 3 on the primary.
- A fallback sent `funding: "private"` for an NHS patient who had said
  nothing about paying, and quoted £50 for a £27.90 check up. Funding
  decides the fee, and the fee is supposed to be decided in code. The tool
  now ignores a funding override unless the caller's own words asked for
  it, the same grounding used for postcodes in entry 23.
- Asked for "something in the afternoon please", with one afternoon slot
  on offer, a fallback booked it without asking. Nothing in code requires
  an explicit yes before a booking, unlike a cancellation. Not changed
  yet: a guard on words like "please" would not have caught this one, and
  a confirmation step costs every caller an extra turn.

**Lesson.** A stronger model covers for gaps in the rules around it. The
funding gap existed from day two and no test or live call found it until
a weaker model walked into it. Running the agent on a worse model is a
cheap way to find where the code is trusting the model more than it says
it does.

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

**Be asymmetric where the costs are asymmetric.** The safety screen is
deliberately over cautious, because a false alarm wastes an evening and a
miss can kill. Its tests are written in the words a frightened caller
actually uses, and they check the misses far harder than the false alarms.

**Run it.** 205 passing tests did not catch the dead model, the false
booking, the throttling or the invented opening hours. Every one of those
came from a live run, and three of the four were found by accident while
checking something else.
