"""Compare two benchmark results (for example before and after a change to the server).

Input is the `benchmark-*.json` files the benchmark writes; a folder is searched
recursively, so a snapshot folder with one subfolder per model is one side of the
comparison. Only what both sides share is compared: the same model, tool profile and case.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from benchmarking.metrics.aggregator import RunScorecard, right_tool, wrong_first_tools

Group = tuple[str, str]  # (model id, tool profile)


def load_results(path: str | Path) -> tuple[list[RunScorecard], list[dict[str, Any]]]:
    """(runs, metas) of one benchmark-*.json, or of every one below a folder."""
    path = Path(path)
    files = [path] if path.is_file() else sorted(path.rglob("benchmark-*.json"))
    if not files:
        raise FileNotFoundError(f"no benchmark-*.json in {path}")
    runs: dict[tuple, RunScorecard] = {}
    metas = []
    for file in files:  # a later file wins when a run was repeated
        data = json.loads(file.read_text(encoding="utf-8"))
        metas.append(data["meta"])
        for raw in data["runs"]:
            card = RunScorecard.model_validate(raw)
            runs[(card.model_id, card.tool_profile, card.suite, card.case_id, card.run_index)] = card
    return list(runs.values()), metas


def _share(flags: list[bool]) -> str:
    return f"{sum(flags)}/{len(flags)} ({sum(flags) / len(flags) * 100:.0f}%)" if flags else "–"


def _change(before: str, after: str) -> str:
    return before if before == after else f"{before} → {after}"


def _rates(cards: list[RunScorecard]) -> dict[str, str]:
    def graded(name: str) -> list[bool]:
        return [c.evaluator_passed[name] for c in cards if name in c.evaluator_passed]

    return {
        "passed": _share([c.passed for c in cards]),
        "right tool": _share([ok for ok in map(right_tool, cards) if ok is not None]),
        "tool call": _share(graded("tool_selection")),
        "invented contacts": _share([not ok for ok in graded("contact_grounding")]),
        "tokens": f"{sum(c.total_tokens for c in cards):,}",
    }


def _offered_tools(metas: list[dict[str, Any]]) -> set[str]:
    return {tool for meta in metas for tools in meta.get("offered_tools", {}).values() for tool in tools}


def _describe(label: str, metas: list[dict[str, Any]]) -> str:
    servers = sorted({str(meta.get("server")) for meta in metas})
    started = sorted(str(meta.get("started_at")) for meta in metas)
    return (f"- **{label}**: {len(_offered_tools(metas))} tools · server {', '.join(servers)} · "
            f"runs started {started[0]}" + (f" to {started[-1]}" if len(started) > 1 else ""))


def comparison_markdown(before: list[RunScorecard], after: list[RunScorecard],
                        before_metas: list[dict[str, Any]], after_metas: list[dict[str, Any]],
                        labels: tuple[str, str] = ("before", "after")) -> str:
    def grouped(cards: list[RunScorecard]) -> dict[Group, dict[tuple[str, str], list[RunScorecard]]]:
        groups: dict[Group, dict[tuple[str, str], list[RunScorecard]]] = defaultdict(lambda: defaultdict(list))
        for card in cards:
            groups[(card.model_id, card.tool_profile)][(card.suite, card.case_id)].append(card)
        return groups

    b_groups, a_groups = grouped(before), grouped(after)
    shared = sorted(set(b_groups) & set(a_groups))
    lines = [f"# Benchmark comparison: {labels[0]} → {labels[1]}", "",
             _describe(labels[0], before_metas), _describe(labels[1], after_metas)]
    removed = sorted(_offered_tools(before_metas) - _offered_tools(after_metas))
    added = sorted(_offered_tools(after_metas) - _offered_tools(before_metas))
    if removed:
        lines.append("- tools removed: " + ", ".join(f"`{t}`" for t in removed))
    if added:
        lines.append("- tools added: " + ", ".join(f"`{t}`" for t in added))
    left_out = sorted(set(b_groups) ^ set(a_groups))
    if left_out:
        lines.append("- only on one side, not compared: " + ", ".join(f"{m} / {p}" for m, p in left_out))
    if not shared:
        return "\n".join(lines + ["", "The two results share no model and tool profile."]) + "\n"

    # Per group: the cases both sides ran.
    pairs: dict[Group, tuple[list[RunScorecard], list[RunScorecard], list[tuple[str, str]]]] = {}
    for group in shared:
        cases = sorted(set(b_groups[group]) & set(a_groups[group]))
        pairs[group] = ([c for case in cases for c in b_groups[group][case]],
                        [c for case in cases for c in a_groups[group][case]], cases)
        skipped = len(set(b_groups[group]) ^ set(a_groups[group]))
        if skipped:
            lines.append(f"- {group[0]} / {group[1]}: {skipped} case(s) ran on one side only and are left out")

    metrics = ["passed", "right tool", "tool call", "invented contacts", "tokens"]
    lines += ["", "Each cell is " + f"{labels[0]} → {labels[1]}" + ". Passed = every check passed; right tool = the first "
              "tool call was an expected tool; tool call = right tool with valid, expected arguments; invented "
              "contacts = answers with a phone number or email address no tool returned.", "",
              "## Result", "", "| model | tool profile | cases | " + " | ".join(metrics) + " |",
              "|---|---|---|" + "---|" * len(metrics)]
    for group, (b_cards, a_cards, cases) in pairs.items():
        b_rates, a_rates = _rates(b_cards), _rates(a_cards)
        lines.append(f"| {group[0]} | {group[1]} | {len(cases)} | "
                     + " | ".join(_change(b_rates[m], a_rates[m]) for m in metrics) + " |")

    lines += ["", "## Per suite", "", "| model | tool profile | suite | cases | passed | right tool |", "|---|---|---|---|---|---|"]
    for group, (b_cards, a_cards, cases) in pairs.items():
        for suite in sorted({suite for suite, _ in cases}):
            b_rates = _rates([c for c in b_cards if c.suite == suite])
            a_rates = _rates([c for c in a_cards if c.suite == suite])
            lines.append(f"| {group[0]} | {group[1]} | {suite} | {sum(s == suite for s, _ in cases)} | "
                         f"{_change(b_rates['passed'], a_rates['passed'])} | "
                         f"{_change(b_rates['right tool'], a_rates['right tool'])} |")

    lines += ["", "## Cases that changed", ""]
    for group, (_, _, cases) in pairs.items():
        gained, lost = [], []
        for case in cases:
            b_runs, a_runs = b_groups[group][case], a_groups[group][case]
            b_share = sum(c.passed for c in b_runs) / len(b_runs)
            a_share = sum(c.passed for c in a_runs) / len(a_runs)
            if a_share > b_share:
                gained.append(case[1])
            elif a_share < b_share:
                stage = next((c.stage for c in a_runs if not c.passed), "")
                lost.append(f"`{case[1]}` ({stage})" if stage else f"`{case[1]}`")
        lines.append(f"- **{group[0]} / {group[1]}**: {len(gained)} gained, {len(lost)} lost.")
        if lost:
            lines.append("  - lost: " + ", ".join(lost))
        if gained:
            lines.append("  - gained: " + ", ".join(f"`{case}`" for case in gained))

    lines += ["", "## Tools called first by mistake", "",
              f"| model | tool profile | {labels[0]} | {labels[1]} |", "|---|---|---|---|"]
    for group, (b_cards, a_cards, _) in pairs.items():
        def wrong(cards: list[RunScorecard]) -> str:
            return ", ".join(f"`{name}` {count}" for name, count in wrong_first_tools(cards)) or "none"
        lines.append(f"| {group[0]} | {group[1]} | {wrong(b_cards)} | {wrong(a_cards)} |")
    return "\n".join(lines) + "\n"
