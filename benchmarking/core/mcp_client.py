"""MCP client for the benchmark: connects to the AWO MCP server and runs its tools.

Transports:
- `stdio`: start `python server.py` (or any command) as a subprocess, like an MCP client app
- `streamable_http`: a hosted server, e.g. the Docker deployment at http://<host>:<port>/mcp
- `inprocess`: run server.py's MCP server in this process over memory streams. With
  `offline=True` its SPARQL client reads data/directory.ttl instead of the live endpoint
  (needs rdflib), which is what the harness self-tests use.

The tool list always comes from the server (`list_tools()`), so the benchmark tests the
tools that exist, with their real descriptions and input schemas.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from benchmarking.core.cases import REPO_ROOT

Transport = Literal["stdio", "streamable_http", "inprocess"]


class MCPConnectionConfig(BaseModel):
    """Where and how to reach the MCP server under test."""

    model_config = ConfigDict(extra="forbid")

    transport: Transport = "stdio"
    command: str = Field(default_factory=lambda: sys.executable)
    args: list[str] = Field(default_factory=lambda: ["server.py"])
    cwd: str = Field(default_factory=lambda: str(REPO_ROOT))
    env: dict[str, str] | None = None
    url: str = "http://localhost:8000/mcp"
    offline: bool = False
    tool_timeout_seconds: float = Field(default=60.0, gt=0)


class MCPToolExecutionResult(BaseModel):
    """Outcome of one tool call (also produced by the fault injector)."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any]
    success: bool
    is_error: bool = False
    latency_ms: float
    raw_text_outputs: list[str] = Field(default_factory=list)
    parsed_json_output: Any | None = None
    error_message: str | None = None
    timed_out: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.raw_text_outputs)


def tool_to_openai(tool: Any) -> dict[str, Any]:
    """An MCP tool (mcp 2.x `types.Tool` or a plain dict) as an OpenAI/LiteLLM function."""
    if isinstance(tool, dict):
        name, description = tool["name"], tool.get("description") or ""
        schema = tool.get("input_schema") or tool.get("inputSchema") or {}
    else:
        name, description = tool.name, tool.description or ""
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


@asynccontextmanager
async def _inprocess_streams(offline: bool):
    """Run server.py's MCP server in this process; yield the client's (read, write) streams."""
    import anyio
    from mcp.shared.memory import create_client_server_memory_streams

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import server  # noqa: PLC0415 - the server under test

    live_client = server.sparql
    if offline:
        from tests.helpers import LocalSparqlClient, load_graph  # noqa: PLC0415

        server.sparql = LocalSparqlClient(load_graph())
    lowlevel = server.mcp._lowlevel_server
    try:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    lowlevel.run, server_streams[0], server_streams[1],
                    lowlevel.create_initialization_options(),
                )
                try:
                    yield client_streams
                finally:
                    task_group.cancel_scope.cancel()
    finally:
        server.sparql = live_client


class MCPBenchmarkClient:
    """Async MCP session to the server under test (use as `async with`)."""

    def __init__(self, config: MCPConnectionConfig) -> None:
        self.config = config
        self._stack: AsyncExitStack | None = None
        self._session = None
        self.tools: list[Any] = []
        self.instructions: str = ""  # the server's instructions, as sent on initialize

    async def __aenter__(self) -> MCPBenchmarkClient:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        try:
            if self.config.transport == "stdio":
                params = StdioServerParameters(
                    command=self.config.command, args=self.config.args,
                    env=self.config.env, cwd=self.config.cwd,
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
            elif self.config.transport == "streamable_http":
                from mcp.client.streamable_http import streamable_http_client

                streams = await self._stack.enter_async_context(streamable_http_client(self.config.url))
                read, write = streams[0], streams[1]
            elif self.config.transport == "inprocess":
                read, write = await self._stack.enter_async_context(_inprocess_streams(self.config.offline))
            else:
                raise ValueError(f"unknown transport {self.config.transport!r}")

            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            initialized = await self._session.initialize()
            self.instructions = (getattr(initialized, "instructions", None) or "").strip()
            self.tools = list((await self._session.list_tools()).tools)
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._stack is not None:
            stack, self._stack, self._session = self._stack, None, None
            await stack.aclose()

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    def openai_tools(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        """The server's tools as OpenAI functions, optionally limited to `only`."""
        return [tool_to_openai(t) for t in self.tools if only is None or t.name in only]

    def input_schema(self, name: str) -> dict[str, Any] | None:
        for tool in self.tools:
            if tool.name == name:
                return tool_to_openai(tool)["function"]["parameters"]
        return None

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None = None, timeout_seconds: float | None = None,
    ) -> MCPToolExecutionResult:
        """Run one tool; errors and timeouts become results, never exceptions."""
        args = arguments or {}
        timeout = timeout_seconds or self.config.tool_timeout_seconds
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(self._session.call_tool(tool_name, arguments=args), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return MCPToolExecutionResult(
                tool_name=tool_name, arguments=args, success=False, is_error=True, timed_out=True,
                latency_ms=(time.perf_counter() - start) * 1000.0,
                error_message=f"Tool '{tool_name}' timed out after {timeout:.0f}s",
            )
        except Exception as exc:  # noqa: BLE001 - transport errors are part of the result
            return MCPToolExecutionResult(
                tool_name=tool_name, arguments=args, success=False, is_error=True,
                latency_ms=(time.perf_counter() - start) * 1000.0,
                error_message=f"{type(exc).__name__}: {exc}",
            )

        texts = [block.text for block in result.content if getattr(block, "text", None) is not None]
        is_error = bool(getattr(result, "is_error", False))
        return MCPToolExecutionResult(
            tool_name=tool_name, arguments=args, success=not is_error, is_error=is_error,
            latency_ms=round((time.perf_counter() - start) * 1000.0, 2),
            raw_text_outputs=texts, parsed_json_output=_parse_json(texts),
            error_message="\n".join(texts) if is_error else None,
        )


def _parse_json(texts: list[str]) -> Any | None:
    if len(texts) != 1:
        return None
    try:
        return json.loads(texts[0])
    except ValueError:
        return None
