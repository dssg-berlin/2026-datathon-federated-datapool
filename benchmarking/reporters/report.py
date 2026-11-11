"""Write the benchmark results: Markdown report, CSV leaderboard and runs, paired changes, JSON."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmarking.metrics.aggregator import (
    GroupSummary, PairedChange, RunScorecard, first_tool, right_tool, right_tool_rate, tool_confusions,
)


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def markdown_report(meta: dict[str, Any], summaries: list[GroupSummary], cards: list[RunScorecard],
                    paired: list[PairedChange]) -> str:
    lines = [
        "# MCP benchmark report", "",
        f"- run: {meta['started_at']} · server: {meta['server']} · suites: {', '.join(meta['suites'])}",
        f"- cases: {meta['cases']} · repetitions: {meta['runs_per_case']} · tool profiles: {', '.join(meta['tool_profiles'])}",
    ]
    if meta.get("oracle_only"):
        lines.append("- **Oracle run: a scripted stand-in called the expected tools. This checks the pipeline "
                     "and the server; it is not a model measurement.**")
    if meta.get("fault"):
        lines.append(f"- fault injection: `{meta['fault']}`")
    lines += [
        "", "Pass = every applicable deterministic check passed. 95 % Wilson interval in brackets; "
        "with few cases the intervals are wide, so small differences are not meaningful. "
        "Right tool = the first tool call was an expected tool; tool call = right tool with valid, "
        "expected arguments.", "",
        "| model | tool profile | pass | 95 % CI | composite | right tool | tool call | contact hallucination "
        "| judge complete | latency p50 / p95 s | tokens | cost $ |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        ci = s.pass_rate_ci
        lines.append(
            f"| {s.model_id} | {s.tool_profile} | {s.passed}/{s.runs} ({_pct(s.pass_rate)}) | "
            f"{_pct(ci.wilson_lower)}–{_pct(ci.wilson_upper)} | {s.composite_mean:.2f} | "
            f"{_pct(s.right_tool_rate)} | {_pct(s.evaluator_pass_rates.get('tool_selection'))} | "
            f"{_pct(s.contact_hallucination_rate)} | {_pct(s.judge_complete_rate)} | "
            f"{s.latency.p50_ms / 1000:.0f} / {s.latency.p95_ms / 1000:.0f} | {s.total_tokens} | {s.total_cost_usd:.4f} |"
        )

    lines += ["", "## Per suite", "", "| model | tool profile | suite | pass | right tool | tool call |",
              "|---|---|---|---|---|---|"]
    for s in summaries:
        group = [c for c in cards if (c.model_id, c.tool_profile) == (s.model_id, s.tool_profile)]
        for suite in sorted({c.suite for c in group}):
            runs = [c for c in group if c.suite == suite]
            graded = [c.evaluator_passed["tool_selection"] for c in runs if "tool_selection" in c.evaluator_passed]
            lines.append(
                f"| {s.model_id} | {s.tool_profile} | {suite} | {sum(c.passed for c in runs)}/{len(runs)} "
                f"({_pct(sum(c.passed for c in runs) / len(runs))}) | {_pct(right_tool_rate(runs))} | "
                f"{_pct(sum(graded) / len(graded) if graded else None)} |")

    lines += ["", "## Tool confusions", "",
              "Runs whose first tool call was not an expected tool: which tool the model took instead.", "",
              "| model | tool profile | expected | called first | runs | cases |", "|---|---|---|---|---|---|"]
    for s in summaries:
        group = [c for c in cards if (c.model_id, c.tool_profile) == (s.model_id, s.tool_profile)]
        for expected, called, count, case_ids in tool_confusions(group):
            lines.append(f"| {s.model_id} | {s.tool_profile} | {expected} | {called} | {count} | {', '.join(case_ids)} |")

    lines += ["", "## Where runs failed", "", "| model | tool profile | stage | runs |", "|---|---|---|---|"]
    for s in summaries:
        for stage, count in s.stages.items():
            lines.append(f"| {s.model_id} | {s.tool_profile} | {stage} | {count} |")

    lines += ["", "## Pass rate per section", ""]
    sections = sorted({section for s in summaries for section in s.section_pass_rates})
    header = [f"{s.model_id} / {s.tool_profile}" for s in summaries]
    lines += ["| section | " + " | ".join(header) + " |", "|---|" + "---|" * len(header)]
    for section in sections:
        cells = []
        for s in summaries:
            ci = s.section_pass_rates.get(section)
            cells.append(f"{ci.successes:.0f}/{ci.n}" if ci else "–")
        lines.append(f"| {section} | " + " | ".join(cells) + " |")

    if paired:
        lines += ["", "## Paired comparison between tool profiles", "",
                  "| model | from → to | gains | losses | same |", "|---|---|---|---|---|"]
        groups: dict[tuple, dict[str, int]] = {}
        for p in paired:
            key = (p.model_id, p.baseline_profile, p.compare_profile)
            groups.setdefault(key, {"gain": 0, "loss": 0, "same": 0})[p.change] += 1
        for (model_id, before, after), counts in groups.items():
            lines.append(f"| {model_id} | {before} → {after} | {counts['gain']} | {counts['loss']} | {counts['same']} |")
        changed = [p for p in paired if p.change != "same"]
        if changed:
            lines += ["", "Changed cases: " + ", ".join(f"{p.case_id} ({p.model_id}: {p.change})" for p in changed)]

    failures = [c for c in cards if not c.passed]
    if failures:
        lines += ["", "## Failed runs", ""]
        for c in failures:
            reason = "; ".join(c.failures[:3])
            lines.append(f"- **{c.model_id} / {c.tool_profile} / {c.case_id}** (run {c.run_index}): "
                         f"{c.stage} — {reason[:300]}")
    return "\n".join(lines) + "\n"


def write_reports(output_dir: str | Path, meta: dict[str, Any], summaries: list[GroupSummary],
                  cards: list[RunScorecard], paired: list[PairedChange]) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = {
        "markdown": out / f"benchmark-{stamp}.md",
        "json": out / f"benchmark-{stamp}.json",
        "leaderboard": out / f"leaderboard-{stamp}.csv",
        "runs": out / f"runs-{stamp}.csv",
    }
    paths["markdown"].write_text(markdown_report(meta, summaries, cards, paired), encoding="utf-8")
    paths["json"].write_text(json.dumps({
        "meta": meta,
        "summaries": [s.model_dump() for s in summaries],
        "runs": [c.model_dump() for c in cards],
        "paired": [p.model_dump() for p in paired],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    with paths["leaderboard"].open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["model", "tool_profile", "runs", "passed", "pass_rate", "ci_lower", "ci_upper",
                         "composite_mean", "right_tool_rate", "tool_selection_pass_rate", "contact_hallucination_rate",
                         "judge_complete_rate", "latency_p50_ms", "latency_p95_ms", "total_tokens", "total_cost_usd"])
        for s in summaries:
            writer.writerow([s.model_id, s.tool_profile, s.runs, s.passed, s.pass_rate, s.pass_rate_ci.wilson_lower,
                             s.pass_rate_ci.wilson_upper, s.composite_mean, s.right_tool_rate,
                             s.evaluator_pass_rates.get("tool_selection"),
                             s.contact_hallucination_rate, s.judge_complete_rate, s.latency.p50_ms, s.latency.p95_ms,
                             s.total_tokens, s.total_cost_usd])

    with paths["runs"].open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["model", "tool_profile", "suite", "case_id", "run", "section", "language", "passed",
                         "stage", "composite", "expected_tools", "first_tool", "right_tool", "tools_called", "judge_level", "steps",
                         "tool_errors", "tokens", "failures"])
        for c in cards:
            writer.writerow([c.model_id, c.tool_profile, c.suite, c.case_id, c.run_index, c.section, c.language,
                             c.passed, c.stage, c.composite_score, " ".join(c.expected_tools),
                             first_tool(c), "" if right_tool(c) is None else right_tool(c),
                             " ".join(c.tools_called), c.judge_level or "", c.steps, c.tool_errors,
                             c.total_tokens, " | ".join(c.failures)])

    if paired:
        paths["paired"] = out / f"paired-{stamp}.csv"
        with paths["paired"].open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["model", "case_id", "baseline_profile", "compare_profile",
                             "baseline_passed", "compare_passed", "change"])
            for p in paired:
                writer.writerow([p.model_id, p.case_id, p.baseline_profile, p.compare_profile,
                                 p.baseline_passed, p.compare_passed, p.change])
    return paths


def print_report(markdown: str) -> None:
    """Rich rendering when available, plain text otherwise."""
    try:
        from rich.console import Console
        from rich.markdown import Markdown

        Console().print(Markdown(markdown))
    except ImportError:
        print(markdown)
