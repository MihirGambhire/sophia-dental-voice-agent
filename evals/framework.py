"""
Running scripted conversations against the real model, and judging them.

WHAT THIS IS FOR
================
Every test in tests/ runs against a scripted stand in for the model. That
proves the loop, the tools and the guard rails are correct. It proves
nothing about whether the real model, given a real caller, actually does
the right thing, and that is where every serious bug in this project so far
has come from: the invented opening hours, the false booking, the invented
postcode, the false claim of verification.

So these run against the real model, and they are kept out of pytest on
purpose. They cost API quota, they take minutes, and they are not
deterministic. A pass rate is the honest way to report something that is
not deterministic.

HOW A SCENARIO IS JUDGED
========================
On outcomes, never on wording. What was written to the database, at what
fee, which tools ran and in what order, whether a warning was given before
an action rather than after. Where the reply text matters, as with a 999
instruction, only the essential fact is checked for.

WHERE A FAILURE CAME FROM
=========================
Each failed check is attributed to a layer, because the fix is different
for each and guessing wastes days:

  generation   The model had what it needed and did not use it. A fact was
               in a tool result and missing from the reply, or a required
               tool was never called.
  retrieval    The fact the reply needed never came back from any tool.
  tool_logic   A tool was called and refused or returned an error.
  safety       A safety rule was not applied.
"""

from __future__ import annotations

import json
import re
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from sophia import clock, db
from sophia.agent_text import SophiaAgent, TurnRecord

GENERATION = "generation"
RETRIEVAL = "retrieval"
TOOL_LOGIC = "tool_logic"
SAFETY = "safety"


# ---------------------------------------------------------------------------
# What one run of one scenario looks like
# ---------------------------------------------------------------------------


@dataclass
class Conversation:
    """Everything a check might want to look at after the call."""

    agent: SophiaAgent
    conn: object
    turns: list[TurnRecord] = field(default_factory=list)
    started_at: datetime | None = None

    @property
    def replies(self) -> list[str]:
        return [turn.reply for turn in self.turns]

    @property
    def all_replies(self) -> str:
        return "\n".join(self.replies)

    def tool_calls(self) -> list[tuple[int, str, dict, dict]]:
        """(turn index, tool name, arguments, result) for every call made."""
        calls = []
        for index, turn in enumerate(self.turns):
            for (name, arguments), (_, result) in zip(turn.tool_calls, turn.tool_results):
                calls.append((index, name, arguments, result))
        return calls

    def called(self, name: str) -> bool:
        return any(call[1] == name for call in self.tool_calls())

    def results_text(self) -> str:
        return json.dumps([call[3] for call in self.tool_calls()], default=str).lower()

    def sophia_appointments(self, patient_code: str | None = None):
        query = (
            "SELECT a.*, p.code AS patient_code FROM appointments a "
            "JOIN patients p ON p.id = a.patient_id WHERE a.booked_via = 'sophia'"
        )
        rows = self.conn.execute(query).fetchall()
        return [row for row in rows if patient_code is None or row["patient_code"] == patient_code]


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    layer: str = ""


Check = Callable[[Conversation], CheckResult]


@dataclass
class Scenario:
    """
    One scripted call.

    turns are what the caller says, in order. If the goal is not reached by
    the end of the script, the caller confirms up to twice, because a real
    model may reasonably ask "shall I book that?" one more time than the
    script expected. The caller only ever says yes; they never volunteer
    new information, so this cannot rescue a scenario that has gone wrong.
    """

    id: str
    title: str
    covers: str
    turns: list[str]
    checks: list[Check]
    at: Callable[[object], datetime]
    setup: Callable[[object], None] | None = None
    # For outbound calls: returns the reminder_queue id Sophia is calling about.
    reminder: Callable[[object], int] | None = None
    goal: Callable[[Conversation], bool] | None = None
    confirm_with: str = "Yes please, go ahead."
    max_confirms: int = 2


@dataclass
class RunResult:
    scenario: Scenario
    checks: list[CheckResult]
    turns: list[TurnRecord]
    error: str | None = None

    @property
    def rate_limited(self) -> bool:
        """
        Stopped by the provider's quota rather than by anything Sophia did.

        Gemini's free tier allows 15 requests a minute per model, and a
        suite fires requests far faster than a caller does. Counting those
        as failures would misreport Sophia, so they are reported apart.
        """
        return is_rate_limited(self.error)

    @property
    def incomplete(self) -> bool:
        """Did not finish, for reasons that say nothing about Sophia."""
        return is_rate_limited(self.error) or is_infrastructure_error(self.error)

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def is_rate_limited(error: str | None) -> bool:
    return bool(error) and ("429" in error or "RESOURCE_EXHAUSTED" in error)


# Failures of the connection rather than of anything Sophia did. The first
# full run lost four scenarios to these, including a laptop DNS failure,
# and the report would otherwise have counted them against her.
_NETWORK_ERRORS = (
    "ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError",
    "getaddrinfo", "Connection aborted", "ServerError", "503 UNAVAILABLE",
)


def is_infrastructure_error(error: str | None) -> bool:
    return bool(error) and any(marker in error for marker in _NETWORK_ERRORS)


def retry_delay_seconds(error: str | None, default: float = 40.0) -> float:
    """The wait the provider asked for, if it said, otherwise a safe default."""
    match = re.search(r"retry(?:Delay)?[^0-9]{0,20}(\d+(?:\.\d+)?)s", error or "", re.IGNORECASE)
    return float(match.group(1)) + 2 if match else default


def run_scenario(scenario: Scenario, turn_pause: float = 0.0) -> RunResult:
    """Run one scenario against the real model, in a fresh database."""
    with tempfile.TemporaryDirectory() as folder:
        conn = db.reset_database(Path(folder) / "eval.db")
        try:
            if scenario.setup:
                scenario.setup(conn)
            now = scenario.at(conn)
            reminder = None
            if scenario.reminder:
                from sophia import outbound

                reminder = outbound.load(conn, scenario.reminder(conn))
            agent = SophiaAgent(conn, now=now, reminder=reminder)
            conversation = Conversation(agent=agent, conn=conn, started_at=now)

            try:
                for said in scenario.turns:
                    conversation.turns.append(agent.say(said))
                    time.sleep(turn_pause)

                confirms = 0
                while (
                    scenario.goal
                    and not scenario.goal(conversation)
                    and confirms < scenario.max_confirms
                ):
                    conversation.turns.append(agent.say(scenario.confirm_with))
                    confirms += 1
                    time.sleep(turn_pause)
            except Exception as failure:  # noqa: BLE001
                return RunResult(
                    scenario, [], conversation.turns, error=f"{type(failure).__name__}: {failure}"
                )

            checks = []
            for check in scenario.checks:
                try:
                    checks.append(check(conversation))
                except Exception as failure:  # noqa: BLE001
                    checks.append(CheckResult(
                        getattr(check, "__name__", "check"), False,
                        f"the check itself raised {type(failure).__name__}: {failure}", TOOL_LOGIC,
                    ))
            return RunResult(scenario, checks, conversation.turns)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Checks, each able to say which layer failed
# ---------------------------------------------------------------------------


def _missing_fact_layer(conversation: Conversation, pattern: str) -> tuple[str, str]:
    """
    Work out why a reply is missing something.

    If the fact appeared in a tool result, the model had it and did not
    use it. If no tool was called at all, the model answered without
    looking. Otherwise nothing it looked up contained the fact.
    """
    if re.search(pattern, conversation.results_text(), re.IGNORECASE):
        return GENERATION, "the fact was in a tool result but not in the reply"
    if not conversation.tool_calls():
        return GENERATION, "no tool was called, so the reply was not based on anything"
    return RETRIEVAL, "no tool result contained it"


def reply_mentions(label: str, pattern: str, layer: str | None = None) -> Check:
    """Some reply in the call matches the pattern."""

    def check(conversation: Conversation) -> CheckResult:
        if re.search(pattern, conversation.all_replies, re.IGNORECASE):
            return CheckResult(label, True)
        diagnosed, why = _missing_fact_layer(conversation, pattern)
        return CheckResult(label, False, why, layer or diagnosed)

    check.__name__ = label
    return check


def reply_never_mentions(label: str, pattern: str, layer: str = GENERATION) -> Check:
    """No reply in the call matches the pattern."""

    def check(conversation: Conversation) -> CheckResult:
        for index, reply in enumerate(conversation.replies):
            match = re.search(pattern, reply, re.IGNORECASE)
            if match:
                return CheckResult(label, False, f"turn {index + 1} said {match.group(0)!r}", layer)
        return CheckResult(label, True)

    check.__name__ = label
    return check


def tool_called(label: str, name: str) -> Check:
    def check(conversation: Conversation) -> CheckResult:
        if conversation.called(name):
            return CheckResult(label, True)
        return CheckResult(label, False, f"{name} was never called", GENERATION)

    check.__name__ = label
    return check


def tool_not_called(label: str, name: str, layer: str = SAFETY) -> Check:
    def check(conversation: Conversation) -> CheckResult:
        if conversation.called(name):
            return CheckResult(label, False, f"{name} was called", layer)
        return CheckResult(label, True)

    check.__name__ = label
    return check


def no_tools_at_all(label: str) -> Check:
    def check(conversation: Conversation) -> CheckResult:
        used = [call[1] for call in conversation.tool_calls()]
        if used:
            return CheckResult(label, False, f"tools were called: {used}", SAFETY)
        return CheckResult(label, True)

    check.__name__ = label
    return check


def custom(label: str, layer: str, test: Callable[[Conversation], tuple[bool, str]]) -> Check:
    """A one off check. The test returns (passed, detail)."""

    def check(conversation: Conversation) -> CheckResult:
        passed, detail = test(conversation)
        return CheckResult(label, passed, "" if passed else detail, "" if passed else layer)

    check.__name__ = label
    return check


def explain_missing_tool_outcome(conversation: Conversation, tool: str) -> tuple[str, str]:
    """
    For an outcome that should have been written by a tool but was not.

    Never called means the model did not act. Called and refused means the
    tool logic stopped it, and the refusal is quoted.
    """
    calls = [call for call in conversation.tool_calls() if call[1] == tool]
    if not calls:
        return GENERATION, f"{tool} was never called"
    last = calls[-1][3] or {}
    reason = last.get("error") or last.get("reason") or last.get("say") or "no detail"
    return TOOL_LOGIC, f"{tool} was called but did not succeed: {str(reason)[:160]}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def write_report(results: list[RunResult], path: Path, provider: str, model: str, repeat: int) -> str:
    """A markdown report, written for someone reading the repository."""
    limited = [result for result in results if result.incomplete]
    results = [result for result in results if not result.incomplete]

    by_scenario: dict[str, list[RunResult]] = {}
    for result in results:
        by_scenario.setdefault(result.scenario.id, []).append(result)

    runs = len(results)
    passed_runs = sum(result.passed for result in results)
    scenarios_all_pass = sum(all(r.passed for r in group) for group in by_scenario.values())
    all_checks = [check for result in results for check in result.checks]
    passed_checks = sum(check.passed for check in all_checks)

    latencies = [
        turn.latency_seconds
        for result in results
        for turn in result.turns
        if not turn.escalated_without_model
    ]
    corrections = sum(1 for result in results for turn in result.turns if turn.corrected_claim)

    layer_counts: dict[str, int] = {}
    for check in all_checks:
        if not check.passed:
            layer_counts[check.layer or "unknown"] = layer_counts.get(check.layer or "unknown", 0) + 1

    lines = [
        "# Evaluation results",
        "",
        f"Run on {clock.now().strftime('%d %B %Y at %H:%M')} UK time, against **{model}** "
        f"via {provider}, each scenario run {repeat} time{'s' if repeat != 1 else ''}.",
        "",
        "Generated by `scripts/run_evals.py`. These are real conversations with the real",
        "model, not the scripted stand in used by the unit tests, so results vary between",
        "runs. That is why they are reported as rates.",
        "",
        "## Summary",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Scenario runs passed | **{passed_runs} of {runs}** ({100 * passed_runs / max(runs, 1):.0f}%) |",
        f"| Scenarios passing every run | {scenarios_all_pass} of {len(by_scenario)} |",
        f"| Individual checks passed | {passed_checks} of {len(all_checks)} |",
        f"| Agent turn latency, median | {statistics.median(latencies) if latencies else 0:.2f}s |",
        f"| Agent turn latency, p90 | {percentile(latencies, 0.9):.2f}s |",
        f"| False claims caught before the caller heard them | {corrections} |",
        "",
        *(
            [
                f"{len(limited)} further run(s) could not complete, because the free tier rate",
                "limit was still being hit after waiting or the network failed, and are excluded from the figures",
                "above: " + ", ".join(sorted({r.scenario.title for r in limited})) + ".",
                "",
            ]
            if limited
            else []
        ),
        "Latency here is the text agent alone: from receiving what the caller said to",
        "having a reply, including every model request and tool call. It excludes speech",
        "to text and text to speech, which are measured separately.",
        "",
    ]

    if layer_counts:
        lines += [
            "## Where failures came from",
            "",
            "| Layer | Failed checks |",
            "|---|---|",
            *[f"| {layer} | {count} |" for layer, count in sorted(layer_counts.items())],
            "",
        ]

    lines += ["## Scenarios", "", "| Scenario | Covers | Passed |", "|---|---|---|"]
    for group in by_scenario.values():
        scenario = group[0].scenario
        passes = sum(r.passed for r in group)
        lines.append(f"| {scenario.title} | {scenario.covers} | {passes} of {len(group)} |")
    lines.append("")

    failures = [result for result in results if not result.passed]
    if failures:
        lines += ["## Failures", ""]
        for result in failures:
            lines.append(f"### {result.scenario.title}")
            lines.append("")
            if result.error:
                lines.append(f"The run did not complete: `{result.error[:300]}`")
                lines.append("")
            for check in result.checks:
                if not check.passed:
                    lines.append(f"- **{check.name}** ({check.layer}): {check.detail}")
            lines += ["", "Transcript:", "", "```"]
            for turn in result.turns:
                lines.append(f"Caller: {turn.user}")
                tools = f"  [tools: {', '.join(turn.tools_used)}]" if turn.tools_used else ""
                lines.append(f"Sophia: {turn.reply}{tools}")
                if turn.corrected_claim:
                    lines.append(f"        (corrected before speaking, the model said: {turn.corrected_claim})")
            lines += ["```", ""]

    text = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
