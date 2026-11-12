"""Adapter: run a case through Claude Code in headless mode (`claude -p`) instead of a model API.

For Claude models without an API key: Claude Code signs in with a Claude subscription
(`claude auth login`), so the benchmark can measure Haiku or Opus on a Pro/Max/Team plan.
Each case is one headless session that gets only the MCP server's tools (no built-in
tools, no user or project settings) and the benchmark's system prompt. Claude Code runs
the tool loop itself; its event stream (`--output-format stream-json`) is turned into the
same `CaseRun` the LiteLLM agent loop produces, so the evaluators and reports are shared.

Differences from the API loop, to keep in mind when comparing numbers:
- Claude Code talks to the MCP server directly, so `--fault` and `--offline` do not apply.
- Model entries (provider "claude-code") go into config/models.local.yaml; see
  recipes/claude-code.md.
- Tool results are not cut to MAX_TOOL_RESULT_CHARS; Claude Code applies its own limits.
- Thinking and effort are Claude Code's defaults (set `extra_params: {effort: ...}` in
  models.yaml to change the effort).
- Tokens are counted, the cost is what the API would charge; a subscription is not billed
  per token, but the runs use up the plan's usage limits.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from benchmarking.core.agent_loop import CaseRun
from benchmarking.core.cases import EvalCase
from benchmarking.core.mcp_client import MCPConnectionConfig, MCPToolExecutionResult, _parse_json
from benchmarking.core.model_adapter import (
    DEFAULT_SYSTEM_PROMPT, InterceptedToolCall, ModelEntryConfig, TokenUsageMetrics,
)

PROVIDER = "claude-code"
MCP_SERVER_NAME = "awo"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# The CLI bundled with the Claude desktop app on macOS (newest version last).
DESKTOP_BUNDLE_GLOB = "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"


def find_cli() -> str | None:
    """CLAUDE_CODE_BIN, `claude` on PATH, or the CLI bundled with the Claude desktop app."""
    configured = os.environ.get("CLAUDE_CODE_BIN")
    if configured:
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundled = sorted(Path.home().glob(DESKTOP_BUNDLE_GLOB), key=lambda p: p.stat().st_mtime)
    return str(bundled[-1]) if bundled else None


async def preflight(conn: MCPConnectionConfig, fault_active: bool) -> str | None:
    """None when Claude Code can run, otherwise what the user has to do."""
    if conn.transport == "inprocess" or fault_active:
        return "--offline and --fault do not work with Claude Code models: Claude Code connects to the server itself"
    cli = find_cli()
    if cli is None:
        return ("Claude Code CLI not found. Install it (https://code.claude.com/docs/en/setup) "
                "or set CLAUDE_CODE_BIN to the `claude` executable.")
    try:
        process = await asyncio.create_subprocess_exec(
            cli, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        logged_in = json.loads(stdout.decode("utf-8", "replace")).get("loggedIn")
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        return f"could not run `{cli} auth status`: {exc}"
    if not logged_in:
        return f'Claude Code is not signed in. Run once:  "{cli}" auth login'
    return None


def mcp_server_config(conn: MCPConnectionConfig) -> dict[str, Any]:
    """The server under test as a Claude Code `--mcp-config` entry."""
    if conn.transport == "streamable_http":
        return {"type": "http", "url": conn.url}
    if conn.transport == "stdio":
        # Claude Code starts the server from another directory, so make script paths absolute.
        args = [str(Path(conn.cwd) / a) if (Path(conn.cwd) / a).is_file() else a for a in conn.args]
        entry: dict[str, Any] = {"type": "stdio", "command": conn.command, "args": args}
        if conn.env:
            entry["env"] = {k: v for k, v in conn.env.items() if k == "SPARQL_ENDPOINT"}
        return entry
    raise ValueError(f"Claude Code models need the stdio or streamable_http transport, not {conn.transport!r}")


def command(cli: str, cfg: ModelEntryConfig, prompt: str, mcp_config_path: str, hidden_tools: list[str],
            max_steps: int, system_prompt: str) -> list[str]:
    cmd = [
        cli, "-p", prompt,
        "--model", cfg.litellm_model,
        "--output-format", "stream-json", "--verbose",
        "--system-prompt", system_prompt,
        "--mcp-config", mcp_config_path, "--strict-mcp-config",
        "--tools", "",                      # no built-in tools: only the MCP server's
        "--allowedTools", f"mcp__{MCP_SERVER_NAME}",
        "--setting-sources", "",            # no user, project or local settings
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns", str(max_steps),
    ]
    if hidden_tools:
        cmd += ["--disallowedTools", ",".join(TOOL_PREFIX + name for name in hidden_tools)]
    if cfg.extra_params.get("effort"):
        cmd += ["--effort", str(cfg.extra_params["effort"])]
    return cmd


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return "" if content is None else str(content)


def apply_events(run: CaseRun, events: list[dict[str, Any]], max_steps: int) -> None:
    """Fill a CaseRun from Claude Code's stream-json events."""
    calls: list[InterceptedToolCall] = []
    results: dict[str, MCPToolExecutionResult] = {}
    last_text, final = "", None

    for event in events:
        message = event.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if event.get("type") == "assistant" and isinstance(content, list):
            run.steps += 1
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    last_text = block["text"]
                elif block.get("type") == "tool_use":
                    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                    calls.append(InterceptedToolCall(
                        call_id=str(block.get("id")), tool_name=str(block.get("name", "")).removeprefix(TOOL_PREFIX),
                        arguments=arguments, raw_arguments_json=json.dumps(arguments, ensure_ascii=False)))
        elif event.get("type") == "user" and isinstance(content, list):
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                call = next((c for c in calls if c.call_id == str(block.get("tool_use_id"))), None)
                text, is_error = _result_text(block.get("content")), bool(block.get("is_error"))
                results[str(block.get("tool_use_id"))] = MCPToolExecutionResult(
                    tool_name=call.tool_name if call else "", arguments=call.arguments if call else {},
                    success=not is_error, is_error=is_error, latency_ms=0.0, raw_text_outputs=[text],
                    parsed_json_output=_parse_json([text]), error_message=text if is_error else None)
        elif event.get("type") == "result":
            final = event

    run.tool_calls = calls
    run.tool_results = [
        results.get(call.call_id) or MCPToolExecutionResult(
            tool_name=call.tool_name, arguments=call.arguments, success=False, is_error=True, latency_ms=0.0,
            error_message="no tool result in the Claude Code event stream")
        for call in calls
    ]

    if final is None:
        run.loop_error = "Claude Code ended without a result event"
        return

    usage = final.get("usage") or {}
    prompt_tokens = sum(int(usage.get(key) or 0) for key in
                        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    completion_tokens = int(usage.get("output_tokens") or 0)
    run.usage = TokenUsageMetrics(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                                  total_tokens=prompt_tokens + completion_tokens,
                                  estimated_cost_usd=round(float(final.get("total_cost_usd") or 0.0), 6))
    run.model_latency_ms = float(final.get("duration_api_ms") or 0.0)
    run.tool_latency_ms = max(0.0, float(final.get("duration_ms") or 0.0) - run.model_latency_ms)
    run.steps = int(final.get("num_turns") or run.steps)

    if final.get("subtype") == "error_max_turns":
        run.loop_error = f"model gave no final answer after {max_steps} steps"
    elif final.get("is_error"):
        run.loop_error = f"model request failed: {final.get('result') or final.get('subtype')}"
    else:
        run.final_answer = str(final.get("result") or last_text or "")
        if not run.final_answer.strip():
            run.loop_error = "model gave no final answer (empty reply)"


async def run_case(
    cfg: ModelEntryConfig,
    conn: MCPConnectionConfig,
    case: EvalCase,
    offered: list[str],
    server_tools: list[str],
    *,
    tool_profile: str = "all",
    run_index: int = 1,
    max_steps: int = 6,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> CaseRun:
    run = CaseRun(case=case, model_id=cfg.id, tool_profile=tool_profile, run_index=run_index, offered_tools=offered)
    cli = find_cli()
    if cli is None:
        run.loop_error = "model request failed: Claude Code CLI not found"
        return run

    start = time.perf_counter()
    # An empty working directory: no CLAUDE.md, no project MCP servers, nothing to read.
    with tempfile.TemporaryDirectory(prefix="awo-bench-") as workdir:
        config_path = Path(workdir) / "mcp.json"
        config_path.write_text(json.dumps({"mcpServers": {MCP_SERVER_NAME: mcp_server_config(conn)}}), encoding="utf-8")
        hidden = [name for name in server_tools if name not in offered]
        process = await asyncio.create_subprocess_exec(
            *command(cli, cfg, case.prompt, str(config_path), hidden, max_steps, system_prompt),
            cwd=workdir, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # Load every tool definition up front, as the API loop does, instead of on demand.
            env={**os.environ, "ENABLE_TOOL_SEARCH": "false"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=cfg.timeout_seconds)
        except asyncio.CancelledError:  # the benchmark was interrupted: do not leave the session running
            process.kill()
            raise
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            run.loop_error = f"model request failed: Claude Code timed out after {cfg.timeout_seconds:.0f}s"
            run.wall_seconds = round(time.perf_counter() - start, 2)
            return run

    events = []
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    apply_events(run, events, max_steps)
    if not events:
        run.loop_error = f"model request failed: {stderr.decode('utf-8', 'replace').strip()[:300] or 'no output'}"
    run.wall_seconds = round(time.perf_counter() - start, 2)
    return run
