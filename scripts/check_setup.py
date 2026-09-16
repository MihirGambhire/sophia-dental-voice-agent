"""
Check that everything is wired up before trying to run Sophia.

    python scripts/check_setup.py

Checks the timezone database, the project database, the API key and,
if a key is present, makes one small live call to confirm the model
answers and that tool calling works. It never prints your key.
"""

import sys
from pathlib import Path

# Windows consoles default to cp1252, which cannot print everything a model
# might emit. Without this a single unusual character ends the session.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sophia import clock, config, db  # noqa: E402

TICK = "  [ok]  "
CROSS = "  [--]  "


def main() -> None:
    problems = []

    print("\nSophia setup check\n" + "-" * 50)

    # 1. Timezone data, which Windows does not ship.
    try:
        clock.now()
        print(f"{TICK}Timezone {config.TIMEZONE_NAME} available")
    except Exception as failure:  # noqa: BLE001
        print(f"{CROSS}Timezone problem: {failure}")
        problems.append("Run: pip install tzdata")

    # 2. The database.
    if config.DB_PATH.exists():
        conn = db.connect()
        counts = db.summary(conn)
        conn.close()
        print(
            f"{TICK}Database found: {counts['patients']} patients, "
            f"{counts['clinicians']} clinicians, {counts['appointment_types']} appointment types"
        )
    else:
        print(f"{CROSS}No database at {config.DB_PATH}")
        problems.append("Run: python scripts/init_db.py")

    # 3. The key. Only ever report its shape, never its value.
    key = config.LLM.api_key
    if not key:
        print(f"{CROSS}No API key for provider '{config.LLM.provider}'")
        problems.append(
            "Add your key to the .env file in the project root. "
            "Get a free one at https://console.groq.com"
        )
    else:
        print(f"{TICK}API key present for '{config.LLM.provider}' ({len(key)} characters)")
        if config.LLM.provider == "groq" and not key.startswith("gsk_"):
            print("         note: Groq keys normally begin with gsk_, check you pasted the whole thing")

    print(f"{TICK}Model configured: {config.LLM.model}")

    # 4. One real call, only if there is a key.
    if key and not problems:
        print("\n  Making one small live call to check the model answers...")
        try:
            from sophia.agent_text import SophiaAgent

            conn = db.connect()
            agent = SophiaAgent(conn)
            turn = agent.say("What are your opening hours?")
            conn.close()

            print(f"{TICK}Model replied in {turn.latency_seconds:.1f}s")
            if turn.tools_used:
                print(f"{TICK}Tool calling works: {', '.join(turn.tools_used)}")
            print(f'\n  Sophia said: "{turn.reply[:200]}"')
        except ModuleNotFoundError as missing:
            print(f"{CROSS}Missing package: {missing.name}")
            problems.append(
                f"You are running the wrong Python. '{missing.name}' is installed "
                "in the project virtual environment, not system wide. "
                r"Run it directly: .venv\Scripts\python.exe scripts/check_setup.py"
            )
        except Exception as failure:  # noqa: BLE001
            text = str(failure)
            print(f"{CROSS}Live call failed: {text[:200]}")
            if "model_not_found" in text or "does not exist" in text:
                problems.append(
                    f"The model '{config.LLM.model}' is not available to this "
                    "account. Model names change, so list what you actually have: "
                    r".venv\Scripts\python.exe scripts/list_models.py"
                )
            elif "401" in text or "invalid_api_key" in text.lower():
                problems.append("The API key was rejected. Check you pasted all of it.")
            elif "429" in text or "rate" in text.lower():
                problems.append(
                    "Rate limited. The free tier allows 8000 tokens per minute. "
                    "Wait a minute and try again."
                )
            else:
                problems.append("Check the key is valid and that you have internet access.")

    print("-" * 50)
    if problems:
        print("\nTo fix:")
        for problem in problems:
            print(f"  - {problem}")
        print()
        sys.exit(1)

    print("\nAll good. Try: python scripts/chat.py --show-tools\n")


if __name__ == "__main__":
    main()
