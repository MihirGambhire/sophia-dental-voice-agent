# Safety screen, tested on held out cases

Run on 20 September 2026 at 08:31 UK time by `scripts/run_safety_redteam.py`.

These sentences were written by people who had never seen the rules, the code or the practice data. They were asked for emergencies and for sentences that sound alarming but are not, and told to write the way a caller actually talks. Expected outcomes follow NHS guidance, not the writer's guess.

## Result

- Real emergencies caught: **5 of 5**
- Non emergencies left alone: **5 of 5**

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
