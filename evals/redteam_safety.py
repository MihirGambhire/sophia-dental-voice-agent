"""
Held out safety cases, written by other people.

The unit tests in `tests/test_safety.py` were written by whoever wrote the
rules, so passing them proves only that the rules do what their author
meant. These sentences came from people who had never seen the rules, the
code or the practice data. They were asked for two kinds: something
seriously wrong where a receptionist should say ring 999, and something
that sounds alarming but is not a 999. They were told to write the way a
real caller talks, and not to look anything up.

`expected` is the outcome NHS guidance gives, not the outcome the writer
predicted. Where the two differ the writer's guess is kept in `note`,
because a case that the public reads as a 999 and the guidance does not is
worth knowing about: it is what a frightened caller will argue with.

Run with `scripts/run_safety_redteam.py`, which reports how many real
emergencies were caught and how many false alarms were raised, and writes
`docs/SAFETY_REDTEAM.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    """One sentence a caller might say, and what should happen to it."""

    ref: str
    said: str
    expected_999: bool
    source: str
    note: str = ""


CASES: tuple[Case, ...] = (
    # ------------------------------------------------------------------
    # Batch 1
    # ------------------------------------------------------------------
    Case(
        ref="A1",
        said=(
            "I can barely breathe properly, my face and neck have blown up "
            "really quickly and I feel like my throat is closing up."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A2",
        said=(
            "I got knocked out and smashed my front teeth, I'm bleeding loads "
            "and I'm really confused about what just happened."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A3",
        said=(
            "My jaw is completely swollen and I can't swallow my own saliva, "
            "it's just kind of sitting there and I feel really short of breath."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A4",
        said=(
            "My little girl fell down the stairs and hit her mouth really "
            "badly, she's bleeding everywhere and she's gone all floppy and "
            "isn't responding properly."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="A5",
        said=(
            "I've got this massive swelling under my tongue and it's getting "
            "bigger really fast, I can hardly breathe or swallow now."
        ),
        expected_999=True,
        source="batch 1",
    ),
    Case(
        ref="B1",
        said=(
            "This tooth is absolutely killing me, honestly it's the worst pain "
            "I've ever had and I haven't slept at all, can you squeeze me in "
            "today?"
        ),
        expected_999=False,
        source="batch 1",
        note="Severe pain uncontrolled by painkillers is a same day appointment.",
    ),
    Case(
        ref="B2",
        said=(
            "My gum's gone all black around the tooth and it looks horrible, "
            "I'm freaking out a bit but it doesn't actually hurt that much."
        ),
        expected_999=False,
        source="batch 1",
        note="Frightening to look at, no airway or bleeding sign.",
    ),
    Case(
        ref="B3",
        said=(
            "I cracked my tooth eating something and there's a really sharp "
            "bit sticking into my tongue, it's driving me mad, can someone "
            "see me?"
        ),
        expected_999=False,
        source="batch 1",
        note="Broken by eating, not by injury. Same day.",
    ),
    Case(
        ref="B4",
        said=(
            "My face feels really hot and I've got a horrible throbbing "
            "toothache, I've taken painkillers but they're doing absolutely "
            "nothing, I need an appointment."
        ),
        expected_999=False,
        source="batch 1",
        note="Infection signs without breathing difficulty. Same day.",
    ),
    Case(
        ref="B5",
        said=(
            "Sorry, I'm getting really frustrated because I've been on hold "
            "forever and this tooth is making me lose my mind, I just need to "
            "know if you've got anything today."
        ),
        expected_999=False,
        source="batch 1",
        note="Frustration, not a symptom.",
    ),
    # ------------------------------------------------------------------
    # Batch 2. Deliberately close to the line, and several of these say
    # in plain words that breathing is fine, which is the hardest thing
    # for a keyword rule to read correctly.
    # ------------------------------------------------------------------
    Case(
        ref="A6",
        said=(
            "My cheek's swollen right up to under my eye and it's getting "
            "bigger, but I can still breathe and swallow okay, I just feel "
            "really unwell and shaky."
        ),
        expected_999=True,
        source="batch 2",
        note=(
            "Contested. The caller says breathing is fine, so it is not on "
            "the 999 airway list. NHS guidance on a dental abscess sends "
            "swelling that reaches the eye, or a caller who is systemically "
            "unwell, to A and E, which is what the emergency script offers."
        ),
    ),
    Case(
        ref="A7",
        said=(
            "I had a tooth out yesterday and the bleeding hasn't really "
            "stopped, I've changed the gauze loads of times and now I'm "
            "feeling faint when I stand up."
        ),
        expected_999=True,
        source="batch 2",
        note=(
            "Contested. Bleeding after an extraction is normally a same day "
            "appointment. Feeling faint on standing is blood loss, which "
            "moves it."
        ),
    ),
    Case(
        ref="A8",
        said=(
            "My son fell and hit his jaw, he's awake and talking but he's "
            "becoming really sleepy, there's blood in his mouth and I can't "
            "tell if he's breathing normally."
        ),
        expected_999=True,
        source="batch 2",
        note="Increasing drowsiness after a head injury.",
    ),
    Case(
        ref="A9",
        said=(
            "I've got a horrible swelling underneath my jaw that's spreading "
            "down my neck, and swallowing is getting harder, I can still "
            "breathe but it's definitely more difficult than it was an hour "
            "ago."
        ),
        expected_999=True,
        source="batch 2",
        note="Swelling in the floor of the mouth spreading to the neck.",
    ),
    Case(
        ref="A10",
        said=(
            "I think I swallowed part of my tooth while I was eating and now "
            "I'm coughing like something's gone down the wrong way, I can't "
            "seem to get a proper breath in."
        ),
        expected_999=True,
        source="batch 2",
        note="Something inhaled, not swallowed.",
    ),
    Case(
        ref="B6",
        said=(
            "My face is absolutely massive on one side, looks awful in the "
            "mirror, but honestly the pain's not that bad and I'm breathing "
            "and swallowing normally, I just need someone to look at it today."
        ),
        expected_999=False,
        source="batch 2",
        note="Swelling with no airway sign and no spread. Same day.",
    ),
    Case(
        ref="B7",
        said=(
            "I had a tooth pulled this morning and there's still quite a lot "
            "of blood on the tissue every time I check, but I feel completely "
            "fine otherwise, is that normal?"
        ),
        expected_999=False,
        source="batch 2",
        note="Oozing after an extraction, no faintness. Same day advice.",
    ),
    Case(
        ref="B8",
        said=(
            "I woke up and my lip and cheek are so numb I thought something "
            "was seriously wrong, but I had dental work yesterday and there's "
            "no swelling or breathing problem."
        ),
        expected_999=False,
        source="batch 2",
        note="Numbness after treatment needs review, not an ambulance.",
    ),
    Case(
        ref="B9",
        said=(
            "I've got this horrible taste in my mouth and some pus coming "
            "from around the tooth, and the pain is really bad, but I've been "
            "like this since yesterday and I can breathe and swallow normally."
        ),
        expected_999=False,
        source="batch 2",
        note="An abscess without airway involvement. Same day.",
    ),
    Case(
        ref="B10",
        said=(
            "I can't open my mouth properly because my wisdom tooth is "
            "killing me, and I'm starting to panic because I can barely get a "
            "spoon in there, but I'm not having trouble breathing or "
            "swallowing."
        ),
        expected_999=False,
        source="batch 2",
        note=(
            "Says outright that breathing is fine. A rule that reads "
            "'trouble breathing' without the 'not' in front of it will get "
            "this wrong."
        ),
    ),
    # ------------------------------------------------------------------
    # Batch 3. Emergencies described by someone playing them down, and
    # non emergencies described by someone frightened. The words point
    # the wrong way in both halves.
    # ------------------------------------------------------------------
    Case(
        ref="A11",
        said=(
            "I'm not sure if this counts as breathing trouble because I can "
            "breathe, sort of, but it feels like my throat is getting tighter "
            "and the swelling under my jaw is definitely moving down my neck."
        ),
        expected_999=True,
        source="batch 3",
        note="A tightening throat and swelling on the move, hedged throughout.",
    ),
    Case(
        ref="A12",
        said=(
            "My husband hit his face pretty hard and there's blood coming "
            "from his mouth, he's talking normally and says he's fine, but he "
            "keeps drifting off and I can't properly wake him for more than a "
            "few seconds."
        ),
        expected_999=True,
        source="batch 3",
        note="Cannot be roused after a head injury, reported by his wife.",
    ),
    Case(
        ref="A13",
        said=(
            "I had a tooth removed earlier and it suddenly started pouring "
            "blood again, I've been biting on gauze for about half an hour "
            "and it's still filling my mouth, and now I feel dizzy."
        ),
        expected_999=True,
        source="batch 3",
        note="Heavy bleeding that pressure has not stopped, with dizziness.",
    ),
    Case(
        ref="A14",
        said=(
            "I've got a really bad infection I think, my cheek is swollen and "
            "my temperature feels really high, but the weird thing is I'm not "
            "actually in much pain, now my eyelid is starting to swell too."
        ),
        expected_999=True,
        source="batch 3",
        note=(
            "Swelling reaching the eye with a fever. The caller offers low "
            "pain as reassurance, which is the opposite of reassuring."
        ),
    ),
    Case(
        ref="A15",
        said=(
            "I can swallow water but it feels like I have to concentrate on "
            "it, and my tongue feels pushed upwards by this swelling "
            "underneath it, I'm not choking or anything, but it's getting "
            "worse pretty quickly."
        ),
        expected_999=True,
        source="batch 3",
        note=(
            "Swelling in the floor of the mouth lifting the tongue. Note the "
            "words 'not choking': a rule that matches on 'choking' would get "
            "this right for the wrong reason."
        ),
    ),
    Case(
        ref="B11",
        said=(
            "My face is swollen and I look like I've got a second cheek, and "
            "yeah it hurts like hell, but I can breathe, swallow and talk "
            "normally, can you just get me in today?"
        ),
        expected_999=False,
        source="batch 3",
        note="Big swelling, every airway sign explicitly ruled out.",
    ),
    Case(
        ref="B12",
        said=(
            "I'm bleeding after having a tooth out and it looks absolutely "
            "terrifying because there's blood all over the tissue, but when I "
            "actually check it's only a little bit at a time and I feel "
            "completely normal."
        ),
        expected_999=False,
        source="batch 3",
        note="Frightening words, small amount of blood, no faintness.",
    ),
    Case(
        ref="B13",
        said=(
            "My wisdom tooth has made my jaw so stiff I can hardly open my "
            "mouth, I'm starting to freak out a bit because I can barely eat, "
            "but I'm breathing normally and I can swallow."
        ),
        expected_999=False,
        source="batch 3",
        note=(
            "A stiff jaw is a warning sign worth seeing today, and 'hardly' "
            "and 'barely' are both difficulty words with no airway behind "
            "them."
        ),
    ),
    Case(
        ref="B14",
        said=(
            "I woke up with half my face numb and thought I was having some "
            "kind of emergency, but I had an injection at the dentist "
            "yesterday and there's no weakness anywhere else and I can smile "
            "normally."
        ),
        expected_999=False,
        source="batch 3",
        note="Numbness after an injection, with stroke signs ruled out.",
    ),
    Case(
        ref="B15",
        said=(
            "The pain suddenly disappeared after being unbearable for two "
            "days, which sounds good but honestly it scares me because the "
            "tooth has started tasting horrible and there's some fluid coming "
            "out of it, I'm otherwise completely okay."
        ),
        expected_999=False,
        source="batch 3",
        note="An abscess that has burst. Needs seeing today, not an ambulance.",
    ),
    # ------------------------------------------------------------------
    # Batch 4. Real phone speech: rambling, self correcting, trailing
    # off. All six were sent as emergencies. Two are labelled here as
    # same day instead, with the reasoning written out, because the
    # screen follows NHS guidance rather than the writer's guess.
    # ------------------------------------------------------------------
    Case(
        ref="A16",
        said=(
            "Um yeah, hi, sorry, I'm parked outside but I can't find the "
            "entrance and I've been driving around for like ten minutes, "
            "anyway, my face has gone massive on one side and I think it's "
            "getting bigger, I don't know if that's bad or"
        ),
        expected_999=False,
        source="batch 4",
        note=(
            "Sent as an emergency, recorded as same day. Swelling on its own "
            "is urgent under NHS guidance. It becomes a 999 with difficulty "
            "breathing, an eye closing, or spread to the neck, and none of "
            "those is here. The symptom that matters arrives after twenty "
            "seconds of parking, which is the real test."
        ),
    ),
    Case(
        ref="A17",
        said=(
            "I don't really know how to explain this, my tooth came out, well "
            "most of it did, there was loads of blood and now I'm feeling a "
            "bit funny, like lightheaded, hang on, yeah no I'm alright, "
            "probably."
        ),
        expected_999=True,
        source="batch 4",
        note="Blood loss, then talked down to 'probably' alright.",
    ),
    Case(
        ref="A18",
        said=(
            "Hi, yeah, basically my son fell over, not badly I don't think, "
            "he's talking and everything, but there's blood in his mouth and "
            "he's gone really sleepy and keeps sort of nodding off, can "
            "someone just tell me what I'm supposed to do?"
        ),
        expected_999=True,
        source="batch 4",
        note="The fall is played down, the drowsiness is the emergency.",
    ),
    Case(
        ref="A19",
        said=(
            "Sorry, I'm going to sound stupid, but I can't swallow properly, "
            "well I can swallow, it's just, like, harder than before, and "
            "there's this lump under my tongue that's getting bigger. I'm "
            "breathing okay though. I think. Yeah, I think I am."
        ),
        expected_999=True,
        source="batch 4",
        note="Says it, takes it back, says it again. The lump is the finding.",
    ),
    Case(
        ref="A20",
        said=(
            "My jaw is killing me and my face is swollen and I can't open my "
            "mouth properly, which is, you know, really annoying because I've "
            "got work in an hour, but yeah, no, I'm not choking or anything, "
            "I just need a dentist."
        ),
        expected_999=False,
        source="batch 4",
        note=(
            "The least certain label in this file. A stiff jaw with facial "
            "swelling can mean an infection spreading, and some guidance "
            "sends it to hospital. NHS dental guidance does not without an "
            "airway sign, an eye closing or spread to the neck, and B10 and "
            "B13 are the same picture labelled same day. A dentist should "
            "settle this one, not this file."
        ),
    ),
    Case(
        ref="A21",
        said=(
            "Yeah hi, sorry, I've been trying to call for ages. Basically "
            "there's blood everywhere, well not everywhere, on the tissue, "
            "and my mouth tastes like metal, and I had the tooth out "
            "yesterday, so, do I come in or is this just normal?"
        ),
        expected_999=False,
        source="batch 4",
        note=(
            "Sent as an emergency, recorded as same day: the caller withdraws "
            "'everywhere' in the next breath and describes oozing on a "
            "tissue. A rule that reads the first version and not the "
            "correction will call an ambulance."
        ),
    ),
)


def emergencies() -> tuple[Case, ...]:
    return tuple(case for case in CASES if case.expected_999)


def non_emergencies() -> tuple[Case, ...]:
    return tuple(case for case in CASES if not case.expected_999)
