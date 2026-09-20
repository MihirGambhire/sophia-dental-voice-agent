# Safety screen, tested on held out cases

Run on 20 September 2026 at 11:05 UK time by `scripts/run_safety_redteam.py`.

These sentences were written by people who had never seen the rules, the code or the practice data. They were asked for emergencies and for sentences that sound alarming but are not, and told to write the way a caller actually talks. Expected outcomes follow NHS guidance, not the writer's guess.

## Result

- Real emergencies caught: **18 of 18**
- Non emergencies left alone: **18 of 18**

A miss is a caller who needed an ambulance and was offered an appointment. A false alarm is a caller told to ring 999 about a filling. The screen is deliberately tuned to prefer the second.

## Missed emergencies

None. Every emergency was caught.

## False alarms

None on this set.

## Every case

| Ref | Expected | Screen said | What the caller said |
|---|---|---|---|
| A1 | 999 | `emergency_999` | I can barely breathe properly, my face and neck have blown up really quickly and I feel like my throat is closing up. |
| A2 | 999 | `emergency_999` | I got knocked out and smashed my front teeth, I'm bleeding loads and I'm really confused about what just happened. |
| A3 | 999 | `emergency_999` | My jaw is completely swollen and I can't swallow my own saliva, it's just kind of sitting there and I feel really short of breath. |
| A4 | 999 | `emergency_999` | My little girl fell down the stairs and hit her mouth really badly, she's bleeding everywhere and she's gone all floppy and isn't responding properly. |
| A5 | 999 | `emergency_999` | I've got this massive swelling under my tongue and it's getting bigger really fast, I can hardly breathe or swallow now. |
| B1 | not 999 | `urgent_same_day` | This tooth is absolutely killing me, honestly it's the worst pain I've ever had and I haven't slept at all, can you squeeze me in today? |
| B2 | not 999 | `routine` | My gum's gone all black around the tooth and it looks horrible, I'm freaking out a bit but it doesn't actually hurt that much. |
| B3 | not 999 | `urgent_same_day` | I cracked my tooth eating something and there's a really sharp bit sticking into my tongue, it's driving me mad, can someone see me? |
| B4 | not 999 | `routine` | My face feels really hot and I've got a horrible throbbing toothache, I've taken painkillers but they're doing absolutely nothing, I need an appointment. |
| B5 | not 999 | `routine` | Sorry, I'm getting really frustrated because I've been on hold forever and this tooth is making me lose my mind, I just need to know if you've got anything today. |
| A6 | 999 | `emergency_999` | My cheek's swollen right up to under my eye and it's getting bigger, but I can still breathe and swallow okay, I just feel really unwell and shaky. |
| A7 | 999 | `emergency_999` | I had a tooth out yesterday and the bleeding hasn't really stopped, I've changed the gauze loads of times and now I'm feeling faint when I stand up. |
| A8 | 999 | `emergency_999` | My son fell and hit his jaw, he's awake and talking but he's becoming really sleepy, there's blood in his mouth and I can't tell if he's breathing normally. |
| A9 | 999 | `emergency_999` | I've got a horrible swelling underneath my jaw that's spreading down my neck, and swallowing is getting harder, I can still breathe but it's definitely more difficult than it was an hour ago. |
| A10 | 999 | `emergency_999` | I think I swallowed part of my tooth while I was eating and now I'm coughing like something's gone down the wrong way, I can't seem to get a proper breath in. |
| B6 | not 999 | `routine` | My face is absolutely massive on one side, looks awful in the mirror, but honestly the pain's not that bad and I'm breathing and swallowing normally, I just need someone to look at it today. |
| B7 | not 999 | `routine` | I had a tooth pulled this morning and there's still quite a lot of blood on the tissue every time I check, but I feel completely fine otherwise, is that normal? |
| B8 | not 999 | `urgent_same_day` | I woke up and my lip and cheek are so numb I thought something was seriously wrong, but I had dental work yesterday and there's no swelling or breathing problem. |
| B9 | not 999 | `urgent_same_day` | I've got this horrible taste in my mouth and some pus coming from around the tooth, and the pain is really bad, but I've been like this since yesterday and I can breathe and swallow normally. |
| B10 | not 999 | `routine` | I can't open my mouth properly because my wisdom tooth is killing me, and I'm starting to panic because I can barely get a spoon in there, but I'm not having trouble breathing or swallowing. |
| A11 | 999 | `emergency_999` | I'm not sure if this counts as breathing trouble because I can breathe, sort of, but it feels like my throat is getting tighter and the swelling under my jaw is definitely moving down my neck. |
| A12 | 999 | `emergency_999` | My husband hit his face pretty hard and there's blood coming from his mouth, he's talking normally and says he's fine, but he keeps drifting off and I can't properly wake him for more than a few seconds. |
| A13 | 999 | `emergency_999` | I had a tooth removed earlier and it suddenly started pouring blood again, I've been biting on gauze for about half an hour and it's still filling my mouth, and now I feel dizzy. |
| A14 | 999 | `emergency_999` | I've got a really bad infection I think, my cheek is swollen and my temperature feels really high, but the weird thing is I'm not actually in much pain, now my eyelid is starting to swell too. |
| A15 | 999 | `emergency_999` | I can swallow water but it feels like I have to concentrate on it, and my tongue feels pushed upwards by this swelling underneath it, I'm not choking or anything, but it's getting worse pretty quickly. |
| B11 | not 999 | `urgent_same_day` | My face is swollen and I look like I've got a second cheek, and yeah it hurts like hell, but I can breathe, swallow and talk normally, can you just get me in today? |
| B12 | not 999 | `routine` | I'm bleeding after having a tooth out and it looks absolutely terrifying because there's blood all over the tissue, but when I actually check it's only a little bit at a time and I feel completely normal. |
| B13 | not 999 | `routine` | My wisdom tooth has made my jaw so stiff I can hardly open my mouth, I'm starting to freak out a bit because I can barely eat, but I'm breathing normally and I can swallow. |
| B14 | not 999 | `routine` | I woke up with half my face numb and thought I was having some kind of emergency, but I had an injection at the dentist yesterday and there's no weakness anywhere else and I can smile normally. |
| B15 | not 999 | `urgent_same_day` | The pain suddenly disappeared after being unbearable for two days, which sounds good but honestly it scares me because the tooth has started tasting horrible and there's some fluid coming out of it, I'm otherwise completely okay. |
| A16 | not 999 | `routine` | Um yeah, hi, sorry, I'm parked outside but I can't find the entrance and I've been driving around for like ten minutes, anyway, my face has gone massive on one side and I think it's getting bigger, I don't know if that's bad or |
| A17 | 999 | `emergency_999` | I don't really know how to explain this, my tooth came out, well most of it did, there was loads of blood and now I'm feeling a bit funny, like lightheaded, hang on, yeah no I'm alright, probably. |
| A18 | 999 | `emergency_999` | Hi, yeah, basically my son fell over, not badly I don't think, he's talking and everything, but there's blood in his mouth and he's gone really sleepy and keeps sort of nodding off, can someone just tell me what I'm supposed to do? |
| A19 | 999 | `emergency_999` | Sorry, I'm going to sound stupid, but I can't swallow properly, well I can swallow, it's just, like, harder than before, and there's this lump under my tongue that's getting bigger. I'm breathing okay though. I think. Yeah, I think I am. |
| A20 | not 999 | `urgent_same_day` | My jaw is killing me and my face is swollen and I can't open my mouth properly, which is, you know, really annoying because I've got work in an hour, but yeah, no, I'm not choking or anything, I just need a dentist. |
| A21 | not 999 | `routine` | Yeah hi, sorry, I've been trying to call for ages. Basically there's blood everywhere, well not everywhere, on the tissue, and my mouth tastes like metal, and I had the tooth out yesterday, so, do I come in or is this just normal? |
