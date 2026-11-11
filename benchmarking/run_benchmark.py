"""Benchmark LLMs on the AWO MCP server (live SPARQL data by default).

Examples (from the repository root):

    python -m benchmarking.run_benchmark --validate-cases          # suites vs. the server's tools
    python -m benchmarking.run_benchmark --models oracle            # pipeline check, no API key
    python -m benchmarking.run_benchmark --models gpt-4o-mini --suites smoke,professional
    python -m benchmarking.run_benchmark --models openwebui-qwen3-8b --tool-profiles baseline,all --runs 3
    python -m benchmarking.run_benchmark --models fast --judge-model gpt-4o --fault timeout_30s
    python -m benchmarking.run_benchmark --transport streamable_http --url http://localhost:8000/mcp

Reports go to reports/benchmark/ (git-ignored): Markdown, JSON, leaderboard CSV, runs CSV
and, with several tool profiles, a paired comparison CSV.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarking.core.agent_loop import run_case  # noqa: E402
from benchmarking.core import claude_code  # noqa: E402
from benchmarking.core.cases import EvalCase, load_suites  # noqa: E402
from benchmarking.core.mcp_client import MCPBenchmarkClient, MCPConnectionConfig  # noqa: E402
from benchmarking.core.model_adapter import (  # noqa: E402
    LiteLLMChatModel, ModelEntryConfig, ModelsRegistryConfig, OracleChatModel, RetryPolicyConfig,
)
from benchmarking.evaluators import (  # noqa: E402
    AnswerContentEvaluator, CompletionEvaluator, ContactGroundingEvaluator, IriRetrievalEvaluator,
    SafetyAbstentionEvaluator, ToolSelectionEvaluator,
)
from benchmarking.metrics import RunScorecard, build_scorecard, paired_changes, summarize  # noqa: E402
from benchmarking.reporters import markdown_report, print_report, write_reports  # noqa: E402
from benchmarking.stress_chaos import FaultInjectionConfig, FaultInjectorMiddleware, FaultMode  # noqa: E402

CONFIG_DIR = REPO_ROOT / "benchmarking" / "config"


def load_env_file() -> None:
    """Read .env like the pytest conftest does (API keys, OPENWEBUI_*, SPARQL_ENDPOINT)."""
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def connection_config(config: dict[str, Any], args: argparse.Namespace) -> MCPConnectionConfig:
    mcp = config.get("mcp", {})
    stdio = mcp.get("stdio", {})
    command = stdio.get("command", "python")
    return MCPConnectionConfig(
        transport=args.transport or mcp.get("transport", "stdio"),
        command=sys.executable if command == "python" else command,
        args=stdio.get("args", ["server.py"]),
        cwd=str(REPO_ROOT),
        env=({**os.environ, "SPARQL_ENDPOINT": args.sparql_endpoint} if args.sparql_endpoint else None),
        url=args.url or mcp.get("streamable_http", {}).get("url", "http://localhost:8000/mcp"),
        offline=args.offline,
        tool_timeout_seconds=float(mcp.get("tool_timeout_seconds", 60)),
    )


def profile_tools(profile: dict[str, Any] | None, server_tools: list[str]) -> list[str]:
    profile = profile or {}
    if "include" in profile:
        return [t for t in server_tools if t in profile["include"]]
    return [t for t in server_tools if t not in profile.get("exclude", [])]


def validate_cases(cases: list[EvalCase], server_tools: list[str]) -> list[str]:
    """Problems that make an active case unrunnable against this server."""
    problems = []
    for case in cases:
        missing = [t for t in case.expected_tools if t not in server_tools]
        if case.status == "active" and case.expected_tools and len(missing) == len(case.expected_tools):
            problems.append(f"{case.suite}/{case.id}: none of {case.expected_tools} exists on the server")
        unknown_args = [t for t in case.expect_args_by_tool if t not in case.expected_tools]
        if unknown_args:
            problems.append(f"{case.suite}/{case.id}: expect_args_by_tool for tools not in expect_tool: {unknown_args}")
        if case.status == "aspirational" and not case.needs_tools:
            problems.append(f"{case.suite}/{case.id}: aspirational case without needs_tools")
    return problems


def build_parser(defaults: dict[str, Any]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark LLMs on the AWO MCP server.")
    parser.add_argument("--models", default=defaults.get("models", "oracle"),
                        help="ids from models.yaml, tiers (frontier, fast, local), 'all', 'oracle', or litellm:<model>")
    parser.add_argument("--suites", default=",".join(defaults.get("suites", ["smoke"])),
                        help="suite names (smoke, citizen, multilingual, professional) or JSON paths")
    parser.add_argument("--cases", default="", help="only these case ids (comma-separated)")
    parser.add_argument("--include-aspirational", action="store_true",
                        help="also run cases that need tools the server does not have yet")
    parser.add_argument("--tool-profiles", default=",".join(defaults.get("tool_profiles", ["all"])),
                        help="profiles from eval_config.yaml; with several, the first is the baseline for paired changes")
    parser.add_argument("--runs", type=int, default=int(defaults.get("runs", 1)), help="repetitions per case")
    parser.add_argument("--concurrency", type=int, default=int(defaults.get("concurrency", 4)))
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--judge-model", default="", help="enable the rubric judge with this model")
    parser.add_argument("--fault", default="none", choices=[m.value for m in FaultMode],
                        help="inject this fault into the first call of every tool in a run")
    parser.add_argument("--transport", choices=["stdio", "streamable_http", "inprocess"], default=None)
    parser.add_argument("--url", default=None, help="MCP URL for streamable_http")
    parser.add_argument("--sparql-endpoint", default=None, help="SPARQL_ENDPOINT for a stdio server")
    parser.add_argument("--offline", action="store_true",
                        help="inprocess transport on data/directory.ttl instead of the live endpoint")
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "reports" / "benchmark"))
    parser.add_argument("--validate-cases", action="store_true", help="check the suites against the server and exit")
    parser.add_argument("--list-tools", action="store_true", help="print the server's tools and exit")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="exit with 1 when a (model, profile) pass rate is below this fraction")
    parser.add_argument("--quiet", action="store_true", help="do not print the report")
    return parser


async def benchmark(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.offline:
        args.transport = "inprocess"
    registry = ModelsRegistryConfig.from_yaml(CONFIG_DIR / "models.yaml")
    retry = RetryPolicyConfig.model_validate(config.get("retry_policy", {}))
    suites = [s.strip() for s in args.suites.split(",") if s.strip()]
    cases = load_suites(suites, include_aspirational=args.include_aspirational or args.validate_cases)
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",")}
        cases = [c for c in cases if c.id in wanted]
    conn = connection_config(config, args)

    async with MCPBenchmarkClient(conn) as client:
        server_tools = client.tool_names
        schemas = {name: client.input_schema(name) for name in server_tools}

        if args.list_tools:
            for tool in client.tools:
                print(f"{tool.name}: {(tool.description or '').strip().splitlines()[0]}")
            return 0

        if args.validate_cases:
            problems = validate_cases(cases, server_tools)
            active = sum(c.status == "active" for c in cases)
            print(f"{len(cases)} cases ({active} active, {len(cases) - active} aspirational) in {', '.join(suites)}; "
                  f"server has {len(server_tools)} tools")
            for problem in problems:
                print("  PROBLEM", problem)
            return 1 if problems else 0

        if not cases:
            print("no cases selected", file=sys.stderr)
            return 2

        profiles = [p.strip() for p in args.tool_profiles.split(",") if p.strip()]
        known_profiles = config.get("tool_profiles", {"all": {}})
        unknown = [p for p in profiles if p not in known_profiles]
        if unknown:
            print(f"unknown tool profile(s) {unknown}; defined: {sorted(known_profiles)}", file=sys.stderr)
            return 2
        offered = {p: profile_tools(known_profiles[p], server_tools) for p in profiles}

        models: list[ModelEntryConfig] = registry.resolve(args.models)
        fault = FaultMode(args.fault)
        if any(m.provider == claude_code.PROVIDER for m in models):
            # Claude Code connects to the MCP server itself (see core/claude_code.py).
            problem = ("--offline and --fault do not work with Claude Code models"
                       if conn.transport == "inprocess" or fault != FaultMode.NONE else await claude_code.preflight())
            if problem:
                print(problem, file=sys.stderr)
                return 2
        judge = None
        if args.judge_model:
            from benchmarking.evaluators.rubric_judge import RubricJudgeEvaluator

            judge = RubricJudgeEvaluator(LiteLLMChatModel(registry.get_model(args.judge_model), retry))

        evaluators = [ToolSelectionEvaluator(schemas), CompletionEvaluator(), ContactGroundingEvaluator(),
                      SafetyAbstentionEvaluator(), AnswerContentEvaluator(), IriRetrievalEvaluator()]
        max_steps = args.max_steps or int(config.get("agent", {}).get("max_steps", 6))
        semaphore = asyncio.Semaphore(max(1, args.concurrency))

        async def one(model_cfg: ModelEntryConfig, profile: str, case: EvalCase, run_index: int) -> RunScorecard:
            async with semaphore:
                if model_cfg.provider == claude_code.PROVIDER:
                    run = await claude_code.run_case_claude_code(
                        model_cfg, conn, case, offered[profile], server_tools,
                        tool_profile=profile, run_index=run_index, max_steps=max_steps)
                else:
                    model = (OracleChatModel(case, schemas) if model_cfg.tier == "oracle"
                             else LiteLLMChatModel(model_cfg, retry))
                    runner = client if fault == FaultMode.NONE else FaultInjectorMiddleware(
                        FaultInjectionConfig(mode=fault, fast_forward_sleep_seconds=0.0), downstream_client=client)
                    tools = client.openai_tools(only=offered[profile])
                    run = await run_case(model, runner, case, tools, model_id=model_cfg.id, tool_profile=profile,
                                         run_index=run_index, max_steps=max_steps)
                results = {ev.evaluator_name: ev.evaluate(run) for ev in evaluators}
                verdict = await judge.evaluate(run) if judge else None
                card = build_scorecard(run, results, verdict)
                mark = "PASS" if card.passed else f"FAIL ({card.stage})"
                print(f"[{model_cfg.id} / {profile}] {case.suite}/{case.id} #{run_index}: {mark}", file=sys.stderr)
                return card

        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cards = await asyncio.gather(*[
            one(m, p, c, r) for m in models for p in profiles for c in cases for r in range(1, args.runs + 1)
        ])

    summaries = summarize(list(cards))
    paired = [change for p in profiles[1:] for change in paired_changes(list(cards), profiles[0], p)]
    server = conn.url if conn.transport == "streamable_http" else (
        "server.py on data/directory.ttl (offline)" if conn.offline else
        f"server.py ({conn.transport}) on {args.sparql_endpoint or os.environ.get('SPARQL_ENDPOINT', 'the live SoSuSe endpoint')}")
    meta = {
        "started_at": started, "server": server, "suites": suites, "cases": len(cases), "runs_per_case": args.runs,
        "tool_profiles": profiles, "offered_tools": offered, "models": [m.id for m in models],
        "judge_model": args.judge_model or None, "fault": None if fault == FaultMode.NONE else fault.value,
        "oracle_only": all(m.tier == "oracle" for m in models), "max_steps": max_steps,
    }
    paths = write_reports(args.output_dir, meta, summaries, list(cards), paired)
    if not args.quiet:
        print_report(markdown_report(meta, summaries, list(cards), paired))
    print("reports: " + ", ".join(str(p) for p in paths.values()), file=sys.stderr)

    if args.fail_under is not None and any(s.pass_rate < args.fail_under for s in summaries):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Windows consoles cannot print every character
        except (AttributeError, ValueError):
            pass
    load_env_file()
    config = load_config(CONFIG_DIR / "eval_config.yaml")
    args = build_parser(config.get("defaults", {})).parse_args(argv)
    return asyncio.run(benchmark(args, config))


if __name__ == "__main__":
    raise SystemExit(main())
