"""
Talk to Sophia in the terminal.

    python scripts/chat.py

Options:
    --show-tools     print every tool call and its result
    --at "2026-09-21 07:45"
                     pretend it is a particular moment, which is how you
                     demonstrate the 8am urgent release, the lunch hour,
                     or a weekend without waiting for one
    --fresh          rebuild the database first

Type 'quit' to leave.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sophia import clock, config, db, prompts  # noqa: E402
from sophia.agent_text import SophiaAgent  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Talk to Sophia in the terminal.")
    parser.add_argument("--show-tools", action="store_true", help="print tool calls")
    parser.add_argument("--at", help='pretend the time is this, e.g. "2026-09-21 07:45"')
    parser.add_argument("--fresh", action="store_true", help="rebuild the database first")
    args = parser.parse_args()

    if args.fresh or not config.DB_PATH.exists():
        print("Building a fresh database.")
        db.reset_database().close()

    pinned = None
    if args.at:
        pinned = clock.to_aware(datetime.strptime(args.at, "%Y-%m-%d %H:%M"))

    conn = db.connect()

    try:
        agent = SophiaAgent(conn, now=pinned)
    except (RuntimeError, NotImplementedError) as problem:
        print(f"\n{problem}\n")
        return

    now = agent.now
    print("=" * 70)
    print(f"  {config.PRACTICE_NAME}")
    print(f"  {config.DISCLAIMER}")
    print("=" * 70)
    print(f"  Time:  {clock.spoken_datetime(now)}")
    print(f"  Open:  {'yes' if clock.is_within_opening_hours(now) else 'no'}")
    print(f"  Model: {config.LLM.model}")
    print("=" * 70)
    print(f"\nSophia: {prompts.GREETING}\n")

    while True:
        try:
            said = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not said:
            continue
        if said.lower() in {"quit", "exit", "bye"}:
            break

        try:
            turn = agent.say(said)
        except Exception as failure:  # noqa: BLE001
            print(f"\n[error] {failure}\n")
            continue

        if args.show_tools and turn.tool_calls:
            for name, arguments in turn.tool_calls:
                print(f"  [tool] {name}({json.dumps(arguments, default=str)})")

        print(f"\nSophia: {turn.reply}\n")
        if args.show_tools:
            print(f"  [{turn.latency_seconds:.1f}s]\n")

    conn.close()
    print("Call ended.")


if __name__ == "__main__":
    main()
