"""
List the models your API key can actually use.

    python scripts/list_models.py

Worth running before trusting any model name, including one from the
provider's own documentation. On 16 September 2026 Groq's docs listed
llama-3.3-70b-versatile as a production model, and the API returned 404
for it, because it had been retired. This endpoint is the only source of
truth.
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sophia import config  # noqa: E402


def main() -> None:
    if config.LLM.provider != "groq":
        print(f"Only the groq provider is supported here, not '{config.LLM.provider}'.")
        sys.exit(1)

    if not config.LLM.groq_api_key:
        print("No GROQ_API_KEY in .env")
        sys.exit(1)

    from groq import Groq

    models = Groq(api_key=config.LLM.groq_api_key).models.list().data
    rows = sorted(
        (m.id, getattr(m, "context_window", 0), getattr(m, "owned_by", ""))
        for m in models
    )

    print(f"\n{len(rows)} models available to this key:\n")
    width = max(len(row[0]) for row in rows)
    for model_id, context, owner in rows:
        marker = " <- currently configured" if model_id == config.LLM.model else ""
        print(f"  {model_id.ljust(width)}  context={context:<7} {owner}{marker}")

    if config.LLM.model not in [row[0] for row in rows]:
        print(
            f"\n  WARNING: the configured model '{config.LLM.model}' is NOT in this "
            "list.\n  Change GROQ_MODEL in your .env to one of the above."
        )
    print()


if __name__ == "__main__":
    main()
