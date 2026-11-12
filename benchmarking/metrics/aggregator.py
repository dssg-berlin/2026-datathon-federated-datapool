"""Scorecards per run, summaries per (model, tool profile), and paired profile comparisons.

A run passes when every applicable deterministic evaluator passes. The composite score
is a weighted mean of the applicable evaluators (weights renormalised over those that
apply). The judge, when enabled, is reported next to it and does not change pass/fail.
Pass rates come with Wilson 95 % intervals: with tens of cases they are wide, and the
report says so.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from benchmarking.core.agent_loop import CaseRun
from benchmarking.evaluators.base import EvaluatorResult
from benchmarking.metrics.statistics import (
    ConfidenceIntervalBound, LatencyDistributionStats, compute_latency_percentiles,
    compute_wilson_confidence_interval,
)

WEIGHTS = {
    "tool_selection": 0.30,
    "completion": 0.15,
    "contact_grounding": 0.20,
    "safety_abstention": 0.15,
    "answer_content": 0.10,
    "iri_retrieval": 0.10,
}

# Order in which a failing run's stage is reported (first failing evaluator wins).
STAGE_ORDER = ["completion", "tool_selection", "contact_grounding", "safety_abstention",
               "answer_content", "iri_retrieval"]


class RunScorecard(BaseModel):
    """One (model, tool profile, case, repetition)."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    tool_profile: str
    suite: str
    case_id: str
    run_index: int
    section: str
    language: str
    expected_tools: list[str]
    tools_called: list[str]
    passed: bool
    stage: str
    composite_score: float
    evaluator_scores: dict[str, float]
    evaluator_passed: dict[str, bool]
    judge_level: str | None = None
    judge_passed: bool | None = None
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    steps: int
    tool_errors: int
    total_tokens: int
    estimated_cost_usd: float
    model_latency_ms: float
    tool_latency_ms: float
    final_answer: str


class GroupSummary(BaseModel):
    """Aggregate over all runs of one model with one tool profile."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    tool_profile: str
    runs: int
    passed: int
    pass_rate: float
    pass_rate_ci: ConfidenceIntervalBound
    composite_mean: float
    evaluator_pass_rates: dict[str, float]
    right_tool_rate: float | None = None
    contact_hallucination_rate: float
    judge_complete_rate: float | None = None
    stages: dict[str, int]
    section_pass_rates: dict[str, ConfidenceIntervalBound]
    latency: LatencyDistributionStats
    total_tokens: int
    total_cost_usd: float


class PairedChange(BaseModel):
    """How one case changed between two tool profiles for one model (cf. paired-outcomes.csv)."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    case_id: str
    baseline_profile: str
    compare_profile: str
    baseline_passed: float
    compare_passed: float
    change: str


def build_scorecard(run: CaseRun, results: dict[str, EvaluatorResult | None],
                    judge: EvaluatorResult | None = None) -> RunScorecard:
    applicable = {name: r for name, r in results.items() if r is not None}
    weight_sum = sum(WEIGHTS.get(name, 0.1) for name in applicable) or 1.0
    composite = sum(WEIGHTS.get(name, 0.1) * r.score for name, r in applicable.items()) / weight_sum
    passed = all(r.passed for r in applicable.values())
    stage = ""
    if not passed:
        for name in STAGE_ORDER:
            if name in applicable and not applicable[name].passed:
                stage = applicable[name].stage or name
                break
    return RunScorecard(
        model_id=run.model_id, tool_profile=run.tool_profile, suite=run.case.suite, case_id=run.case.id,
        run_index=run.run_index, section=run.case.section or run.case.suite, language=run.case.language,
        expected_tools=run.case.expected_tools, tools_called=[c.tool_name for c in run.tool_calls],
        passed=passed, stage=stage, composite_score=round(composite, 4),
        evaluator_scores={name: r.score for name, r in applicable.items()},
        evaluator_passed={name: r.passed for name, r in applicable.items()},
        judge_level=(judge.diagnostics.get("verdict", {}).get("literal_fulfillment") if judge else None),
        judge_passed=judge.passed if judge else None,
        failures=[f"{name}: {f}" for name, r in applicable.items() for f in r.failures]
        + ([f"rubric_judge: {f}" for f in judge.failures] if judge else []),
        warnings=[w for r in applicable.values() for w in r.warnings],
        steps=run.steps, tool_errors=sum(1 for r in run.tool_results if not r.success),
        total_tokens=run.usage.total_tokens, estimated_cost_usd=run.usage.estimated_cost_usd,
        model_latency_ms=round(run.model_latency_ms, 2), tool_latency_ms=round(run.tool_latency_ms, 2),
        final_answer=run.final_answer,
    )


NO_TOOL_CALL = "(no tool call)"


def first_tool(card: RunScorecard) -> str:
    return card.tools_called[0] if card.tools_called else NO_TOOL_CALL


def right_tool(card: RunScorecard) -> bool | None:
    """Was the first tool call one of the expected tools (arguments aside)? None = no expectation."""
    if not card.expected_tools:
        return None
    return first_tool(card) in card.expected_tools


def right_tool_rate(cards: list[RunScorecard]) -> float | None:
    graded = [ok for ok in map(right_tool, cards) if ok is not None]
    return round(sum(graded) / len(graded), 4) if graded else None


def tool_confusions(cards: list[RunScorecard]) -> list[tuple[str, str, int, list[str]]]:
    """(expected tools, tool called first instead, runs, case ids), most frequent first."""
    pairs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for card in cards:
        if right_tool(card) is False:
            pairs[(", ".join(card.expected_tools), first_tool(card))].append(card.case_id)
    rows = [(expected, called, len(ids), sorted(set(ids))) for (expected, called), ids in pairs.items()]
    return sorted(rows, key=lambda row: (-row[2], row[0], row[1]))


def wrong_first_tools(cards: list[RunScorecard]) -> list[tuple[str, int]]:
    """(tool called first although it was not expected, runs), most frequent first."""
    counts: dict[str, int] = defaultdict(int)
    for card in cards:
        if right_tool(card) is False:
            counts[first_tool(card)] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def summarize(cards: list[RunScorecard]) -> list[GroupSummary]:
    groups: dict[tuple[str, str], list[RunScorecard]] = defaultdict(list)
    for card in cards:
        groups[(card.model_id, card.tool_profile)].append(card)

    summaries = []
    for (model_id, profile), group in groups.items():
        n = len(group)
        passed = sum(c.passed for c in group)
        evaluator_names = sorted({name for c in group for name in c.evaluator_passed})
        evaluator_rates = {}
        for name in evaluator_names:
            graded = [c.evaluator_passed[name] for c in group if name in c.evaluator_passed]
            evaluator_rates[name] = round(sum(graded) / len(graded), 4)
        grounded = [c for c in group if "contact_grounding" in c.evaluator_passed]
        judged = [c for c in group if c.judge_level is not None]
        sections: dict[str, list[RunScorecard]] = defaultdict(list)
        for c in group:
            sections[c.section].append(c)
        stages: dict[str, int] = defaultdict(int)
        for c in group:
            if not c.passed:
                stages[c.stage or "other"] += 1
        summaries.append(GroupSummary(
            model_id=model_id, tool_profile=profile, runs=n, passed=passed,
            pass_rate=round(passed / n, 4), pass_rate_ci=compute_wilson_confidence_interval(passed, n),
            composite_mean=round(sum(c.composite_score for c in group) / n, 4),
            evaluator_pass_rates=evaluator_rates,
            right_tool_rate=right_tool_rate(group),
            contact_hallucination_rate=round(
                sum(not c.evaluator_passed["contact_grounding"] for c in grounded) / len(grounded), 4) if grounded else 0.0,
            judge_complete_rate=round(sum(c.judge_level == "complete" for c in judged) / len(judged), 4) if judged else None,
            stages=dict(sorted(stages.items(), key=lambda item: -item[1])),
            section_pass_rates={s: compute_wilson_confidence_interval(sum(c.passed for c in cs), len(cs))
                                for s, cs in sorted(sections.items())},
            latency=compute_latency_percentiles([c.model_latency_ms + c.tool_latency_ms for c in group]),
            total_tokens=sum(c.total_tokens for c in group),
            total_cost_usd=round(sum(c.estimated_cost_usd for c in group), 6),
        ))
    return sorted(summaries, key=lambda s: (-s.pass_rate, -s.composite_mean, s.model_id, s.tool_profile))


def paired_changes(cards: list[RunScorecard], baseline: str, compare: str) -> list[PairedChange]:
    """Per (model, case): pass share under `baseline` vs. `compare` profile."""
    shares: dict[tuple[str, str, str], list[bool]] = defaultdict(list)
    for c in cards:
        shares[(c.model_id, c.case_id, c.tool_profile)].append(c.passed)
    changes = []
    keys = sorted({(m, case) for (m, case, p) in shares if p == baseline})
    for model_id, case_id in keys:
        before, after = shares.get((model_id, case_id, baseline)), shares.get((model_id, case_id, compare))
        if not before or not after:
            continue
        b, a = sum(before) / len(before), sum(after) / len(after)
        change = "gain" if a > b else "loss" if a < b else "same"
        changes.append(PairedChange(model_id=model_id, case_id=case_id, baseline_profile=baseline,
                                    compare_profile=compare, baseline_passed=round(b, 4),
                                    compare_passed=round(a, 4), change=change))
    return changes
