"""
Run the scenario suite against the real model and write a report.

    python scripts/run_evals.py
    python scripts/run_evals.py --repeat 3
    python scripts/run_evals.py --only opening_hours,red_flag_999
    python scripts/run_evals.py --list

Uses real API quota and takes several minutes. Results are written to
docs/EVAL_RESULTS.md, which is meant to be committed: it is the evidence
behind any claim about how well Sophia behaves.
"""

import argparse
import dataclasses
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.framework import is_daily_quota, retry_delay_seconds, run_scenario, write_report  # noqa: E402
from evals.scenarios import SCENARIOS, by_id  # noqa: E402
from sophia import config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Sophia's scenario suite against the real model.")
    parser.add_argument("--repeat", type=int, default=1, help="runs per scenario")
    parser.add_argument("--only", help="comma separated scenario ids")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    # Gemini free tier: 15 requests a minute per model, and one turn can be
    # several requests. A pause between turns keeps most runs under it.
    parser.add_argument("--turn-pause", type=float, default=5.0, help="seconds between turns")
    parser.add_argument("--rate-limit-retries", type=int, default=3)
    parser.add_argument("--out", default=str(ROOT / "docs" / "EVAL_RESULTS.md"))
    # Off by default: the suite should measure one model, not whichever
    # mix of models happened to have quota left. The report lists the
    # models used when fallback is turned on.
    parser.add_argument("--fallback", action="store_true",
                        help="allow the model fallback chain, as the live demo does")
    args = parser.parse_args()

    if not args.fallback:
        config.LLM = dataclasses.replace(config.LLM, fallbacks="none")

    if args.list:
        for scenario in SCENARIOS:
            print(f"  {scenario.id:<28} {scenario.title}")
        return

    chosen = SCENARIOS
    if args.only:
        lookup = by_id()
        unknown = [name for name in args.only.split(",") if name not in lookup]
        if unknown:
            print(f"Unknown scenario ids: {unknown}. Use --list.")
            sys.exit(2)
        chosen = [lookup[name] for name in args.only.split(",")]

    models = ", then ".join(f"{model} via {provider}" for provider, model in config.LLM.chain())
    print(f"\nRunning {len(chosen)} scenario(s) x {args.repeat} against {models}\n")

    results = []
    started = time.time()
    out_of_quota = False
    for scenario in chosen:
        if out_of_quota:
            break
        for attempt in range(args.repeat):
            t0 = time.time()
            result = run_scenario(scenario, turn_pause=args.turn_pause)
            # A quota stop says nothing about Sophia. Wait as long as the
            # provider asks, then rerun the whole scenario from a fresh
            # database rather than resuming a half finished conversation.
            for _ in range(args.rate_limit_retries):
                if not result.incomplete or is_daily_quota(result.error):
                    break
                # The quota is a rolling 60 second window, so a shorter wait just
                # walks straight back into it. The first full run did exactly that.
                wait = max(60.0, retry_delay_seconds(result.error)) if result.rate_limited else 15.0
                why = "rate limited" if result.rate_limited else "network error"
                print(f"  [WAIT] {scenario.title}: {why}, retrying in {wait:.0f}s")
                time.sleep(wait)
                result = run_scenario(scenario, turn_pause=args.turn_pause)
            results.append(result)
            if is_daily_quota(result.error):
                # No retry helps until the daily reset, so stop now and
                # keep the report honest about how little was run.
                print(f"  [STOP] Daily quota for {config.LLM.model} is used up. Stopping the run.")
                out_of_quota = True
                break
            mark = "PASS" if result.passed else ("SKIP" if result.incomplete else "FAIL")
            suffix = f" (run {attempt + 1})" if args.repeat > 1 else ""
            print(f"  [{mark}] {scenario.title}{suffix}  {time.time() - t0:.0f}s")
            if result.error:
                print(f"         did not complete: {result.error[:160]}")
            for check in result.checks:
                if not check.passed:
                    print(f"         - {check.name} ({check.layer}): {check.detail[:140]}")

    limited = sum(result.incomplete for result in results)
    counted = len(results) - limited
    passed = sum(result.passed for result in results)
    if out_of_quota and counted == 0:
        # Writing a report of nothing would overwrite the last real one.
        print(f"\nNo runs completed, so {args.out} was left unchanged.\n")
        sys.exit(1)
    write_report(results, Path(args.out), config.LLM.provider, config.LLM.model, args.repeat)
    print(f"\n{passed} of {counted} runs passed in {time.time() - started:.0f}s"
          + (f", {limited} excluded after repeated rate limits or network errors" if limited else ""))
    print(f"Report: {args.out}\n")


if __name__ == "__main__":
    main()
