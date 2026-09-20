"""
Run the held out safety cases and report how the screen did.

Two numbers matter, and they are not symmetric. A missed emergency is a
caller who needed an ambulance and was offered a Thursday. A false alarm
is a caller who was told to ring 999 about a filling, which wastes their
evening and costs trust. The first must be zero. The second is allowed to
be small, and every one of them is printed so the cost is visible.

    python scripts/run_safety_redteam.py

Writes docs/SAFETY_REDTEAM.md and prints the summary. No API key needed:
the screen is deterministic Python, not a model.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.redteam_safety import CASES, Case  # noqa: E402
from sophia import clock, safety  # noqa: E402

REPORT = ROOT / "docs" / "SAFETY_REDTEAM.md"


def _verdict(case: Case) -> tuple[bool, safety.Level]:
    """What the screen decided, and whether it matched the guidance."""
    screening = safety.screen(case.said)
    said_999 = screening.level is safety.Level.EMERGENCY
    return said_999 == case.expected_999, screening.level


def main() -> int:
    misses: list[Case] = []
    false_alarms: list[Case] = []
    levels: dict[str, safety.Level] = {}

    for case in CASES:
        agreed, level = _verdict(case)
        levels[case.ref] = level
        if not agreed:
            (misses if case.expected_999 else false_alarms).append(case)

    real = [case for case in CASES if case.expected_999]
    fake = [case for case in CASES if not case.expected_999]
    caught = len(real) - len(misses)
    quiet = len(fake) - len(false_alarms)

    lines = [
        "# Safety screen, tested on held out cases",
        "",
        f"Run on {clock.now():%d %B %Y at %H:%M} UK time by "
        "`scripts/run_safety_redteam.py`.",
        "",
        "These sentences were written by people who had never seen the rules,"
        " the code or the practice data. They were asked for emergencies and"
        " for sentences that sound alarming but are not, and told to write the"
        " way a caller actually talks. Expected outcomes follow NHS guidance,"
        " not the writer's guess.",
        "",
        "## Result",
        "",
        f"- Real emergencies caught: **{caught} of {len(real)}**",
        f"- Non emergencies left alone: **{quiet} of {len(fake)}**",
        "",
        "A miss is a caller who needed an ambulance and was offered an"
        " appointment. A false alarm is a caller told to ring 999 about a"
        " filling. The screen is deliberately tuned to prefer the second.",
        "",
    ]

    def _table(title: str, cases: list[Case], blank: str) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not cases:
            lines.extend([blank, ""])
            return
        lines.append("| Ref | What the caller said | Screen said | Note |")
        lines.append("|---|---|---|---|")
        for case in cases:
            note = case.note or ""
            lines.append(
                f"| {case.ref} | {case.said} | `{levels[case.ref].value}` | {note} |"
            )
        lines.append("")

    _table("Missed emergencies", misses, "None. Every emergency was caught.")
    _table("False alarms", false_alarms, "None on this set.")

    lines.extend(["## Every case", "", "| Ref | Expected | Screen said | What the caller said |", "|---|---|---|---|"])
    for case in CASES:
        expected = "999" if case.expected_999 else "not 999"
        lines.append(f"| {case.ref} | {expected} | `{levels[case.ref].value}` | {case.said} |")
    lines.append("")

    REPORT.write_text("\n".join(lines), encoding="utf-8")

    print(f"Emergencies caught:   {caught} of {len(real)}")
    print(f"Non emergencies kept: {quiet} of {len(fake)}")
    for case in misses:
        print(f"  MISSED  {case.ref}: {case.said}")
    for case in false_alarms:
        print(f"  ALARMED {case.ref}: {case.said}")
    print(f"Report written to {REPORT.relative_to(ROOT)}")
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
