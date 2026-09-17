"""
Retrieval over the practice's own documents.

WHY THIS EXISTS, AND WHY IT IS URGENT
=====================================
Asked "what are your opening hours?", Sophia replied that the practice
opens at nine, opens on Saturday mornings, and closes on public holidays.
All three are wrong. It opens at eight, it does not open at weekends, and
it closes between one and two every weekday. She called no tool at all.

The system prompt already said, in capitals, that she knows nothing a
tool has not told her. That was not enough, and it was never going to be,
because there was no tool that could answer the question. Given a
question it cannot serve and no way to look it up, a model fills the gap.

The fix is not a firmer instruction. It is giving her somewhere to look,
so that "I do not know" and "let me take a message" are reachable
outcomes rather than dead ends.

THE APPROACH
============
Plain lexical scoring over the markdown in data/practice_info, split into
sections by heading. No embeddings, no vector store, no model call.

That is a deliberate choice for now, not a shortcut. The corpus is eight
small documents, the vocabulary is fixed and domain specific, and every
extra token spent on a retrieved passage comes out of an 8000 token per
minute budget. Day 3 swaps the scorer for embeddings if the evaluation
shows lexical matching missing real questions. The interface here does
not change when it does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from . import config

# Words that carry no signal when everything in the corpus is about one
# dental practice.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could do does for from had has have
    how i if in is it its me my of on or our so than that the their them then
    there these they this to us was we were what when where which who why will
    with would you your please tell know about need want like
    """.split()
)

# Question words mapped onto the vocabulary the documents actually use, so
# a caller asking "when do you shut" still reaches the hours document.
SYNONYMS = {
    # Question words. These must map onto words the documents really use,
    # not onto the abstract concept. "What is your address" failed because
    # the address section never contains the word "address", it contains
    # "Museum Street" and "Warrington".
    "where": "museum street warrington cheshire practice located",
    "address": "museum street warrington cheshire practice located",
    "located": "museum street warrington",
    "when": "hours opening times",
    # Hours and closure
    "shut": "closed closing hours",
    "close": "closed closing hours",
    "closed": "closed closing hours weekend",
    "open": "opening hours open",
    "opening": "opening hours",
    "hours": "opening hours open closed lunch",
    "time": "hours opening time",
    "times": "hours opening",
    "lunch": "lunch closed 1pm 2pm",
    "morning": "opening hours 8am",
    "afternoon": "opening hours 6pm lunch 2pm",
    "night": "out hours emergency 111 closed",
    "evening": "out hours closed 111",
    "weekend": "saturday sunday closed",
    "saturday": "saturday closed weekend",
    "sunday": "sunday closed weekend",
    # Money
    "cost": "fee charge price",
    "costs": "fee charge price",
    "price": "fee charge cost",
    "prices": "fees charges cost",
    "charge": "fee price cost",
    "pay": "fee charge payable deposit",
    # Appointments and attendance
    "cancel": "cancelling cancellation notice hours",
    "cancelling": "cancellation notice short",
    "cancellation": "cancellation notice short",
    "miss": "failure attend missed appointment",
    "missed": "failure attend missed appointment",
    "late": "late arrival discretion",
    "reminder": "reminders remembering responsibility",
    "reminders": "reminders remembering responsibility",
    # Joining
    "register": "registration registering joining new patient",
    "registered": "registration joining lapse three years",
    "registration": "registration lapse three years",
    "join": "joining registration new patient",
    "joining": "registration new patient waiting list",
    "waiting": "waiting list paused",
    # Clinical and urgent
    "emergency": "urgent emergency 999 out hours",
    "emergencies": "urgent emergency 999",
    "urgent": "urgent emergency same day 8am",
    "pain": "urgent toothache painkillers",
    "toothache": "urgent toothache severe",
    # Services
    "park": "parking car access",
    "parking": "parking car access ramp",
    "phone": "phone telephone 01925 number",
    "number": "phone telephone 01925 number",
    "filling": "fillings band 2 charge",
    "fillings": "fillings band 2 charge",
    "invisalign": "orthodontics orthodontic",
    "braces": "orthodontics orthodontic",
    "aligners": "orthodontics orthodontic",
    "implant": "implants",
    "implants": "implants nhs private",
    "nhs": "nhs band charge",
    "hygienist": "hygiene therapist direct access",
    "hygiene": "hygiene therapist direct access",
}


@dataclass(frozen=True)
class Passage:
    """One retrievable section of a practice document."""

    key: str
    title: str
    heading: str
    text: str

    @property
    def source(self) -> str:
        return f"{self.key}#{_slug(self.heading)}" if self.heading else self.key


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _tokenise(text: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in STOPWORDS]


def _query_terms(question: str) -> list[str]:
    """
    Turn a spoken question into terms the documents might actually contain.

    Order matters here, and getting it wrong was a real bug. Stripping
    stopwords first deleted "where" and "when" before they could be
    expanded, so "where are you?" arrived as an empty query and matched
    nothing. Expansion has to happen on the raw words, and stopwords are
    removed afterwards.
    """
    raw = re.findall(r"[a-z0-9]+", question.lower())

    expanded = list(raw)
    for word in raw:
        if word in SYNONYMS:
            expanded.extend(SYNONYMS[word].split())

    return [word for word in expanded if word not in STOPWORDS]


def _parse_front_matter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---"):
        return {}, raw
    _, _, rest = raw.partition("---\n")
    block, _, body = rest.partition("---\n")
    meta = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith(" "):
            name, _, value = line.partition(":")
            meta[name.strip()] = value.strip()
    return meta, body


@lru_cache(maxsize=1)
def load_passages() -> tuple[Passage, ...]:
    """
    Read every practice document once and split it into sections.

    Cached, because the files do not change while the process runs and a
    voice call cannot afford to re-read them on every question.
    """
    passages: list[Passage] = []

    for path in sorted(config.PRACTICE_INFO_DIR.glob("*.md")):
        meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
        key = meta.get("key", path.stem)
        title = meta.get("title", path.stem.replace("_", " "))

        heading = ""
        buffer: list[str] = []

        def flush() -> None:
            text = "\n".join(buffer).strip()
            if text:
                passages.append(Passage(key=key, title=title, heading=heading, text=text))

        for line in body.splitlines():
            if line.startswith("# "):
                flush()
                buffer = []
                heading = line[2:].strip()
            else:
                buffer.append(line)
        flush()

    return tuple(passages)


def search(question: str, limit: int = 3) -> list[Passage]:
    """
    The passages most likely to answer the question.

    Scoring is deliberately simple: how many of the question's meaningful
    words appear in the passage, with a bonus for words in the heading,
    normalised so a long passage does not win on length alone.
    """
    wanted = _query_terms(question)
    if not wanted:
        return []

    scored: list[tuple[float, Passage]] = []
    for passage in load_passages():
        body_words = set(_tokenise(passage.text))
        head_words = set(_tokenise(f"{passage.title} {passage.heading}"))

        hits = sum(1 for word in wanted if word in body_words)
        head_hits = sum(1 for word in wanted if word in head_words)
        if not hits and not head_hits:
            continue

        # The heading is weighted heavily because it is the closest thing
        # these documents have to a statement of what they are about.
        score = hits + (head_hits * 3)
        score /= 1 + (len(body_words) / 400)
        scored.append((score, passage))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [passage for _, passage in scored[:limit]]


# Words in a question that say nothing about its subject, so their absence
# from the documents means nothing either.
_GENERIC = frozenset(
    """
    much many offer offers provide provides accept accepts take takes have give get go goes
    come see book make help find anything something someone practice dentist dentists dental
    available possible sure still really just also some any other more best good okay hello
    thanks thank sorry well bring there here today tomorrow week soon long does doing done
    """.split()
)


def _uncovered_subject(question: str) -> str | None:
    """
    A specific word in the question that no document mentions, if any.

    A tester asked "do you offer Invisalign?" and the scorer returned a
    paragraph about registering, because "offer" matched. Sophia then said
    the practice offers Invisalign. When the question's subject appears
    nowhere in the practice information, the honest answer is not found.
    """
    vocabulary = _vocabulary()
    content = [
        word for word in re.findall(r"[a-z]+", question.lower())
        if len(word) >= 4 and word not in STOPWORDS and word not in _GENERIC and word not in SYNONYMS
    ]
    uncovered = [
        word for word in content
        if not {word, word.rstrip("s"), word + "s", word[:-3] + "y" if word.endswith("ies") else word} & vocabulary
    ]
    # Only when nothing specific in the question is covered. "Taking new NHS
    # patients" has one unusual word among covered ones and is answerable;
    # "accept Bupa" has nothing covered at all.
    if content and len(uncovered) == len(content):
        return uncovered[0]
    return None


_VOCABULARY: frozenset[str] | None = None


def _vocabulary() -> frozenset[str]:
    global _VOCABULARY
    if _VOCABULARY is None:
        words: set[str] = set()
        for passage in load_passages():
            words.update(re.findall(r"[a-z0-9]+", f"{passage.title} {passage.heading} {passage.text}".lower()))
        _VOCABULARY = frozenset(words)
    return _VOCABULARY


def answer(question: str, limit: int = 3, max_chars: int = 900) -> dict:
    """
    Retrieve, shaped for handing back to the model as a tool result.

    Returns the passages and their sources. Deliberately returns nothing
    rather than a weak match when the question is not covered, because an
    unrelated passage invites exactly the confident wrong answer this
    module exists to stop.
    """
    uncovered = _uncovered_subject(question)
    passages = [] if uncovered else search(question, limit=limit)

    if not passages:
        return {
            "found": False,
            "say": (
                "I do not have that in the practice information, and I do not want "
                "to give you the wrong answer. Shall I take a message for the team?"
            ),
        }

    budget = max_chars
    extracts = []
    for passage in passages:
        text = passage.text.strip()
        if len(text) > budget:
            text = text[:budget].rsplit("\n", 1)[0].strip()
        if not text:
            break
        extracts.append(
            {"source": passage.source, "heading": passage.heading, "text": text}
        )
        budget -= len(text)
        if budget <= 100:
            break

    return {"found": True, "extracts": extracts}
