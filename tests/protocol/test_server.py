"""MCP protocol layer: does the real server expose and run its tools correctly?

Uses an in-memory MCP client against server.py (no LLM, no network): conftest.py swaps
the server's SPARQL client for one that runs the real queries on data/directory.ttl. Result
correctness is checked in tests/unit/test_sparql_parity.py (offline) and tests/api/ (live).
When you add a tool, add an entry to SMOKE_ARGS below and two tests to test_tools_e2e.py.
"""

import asyncio
import json
import sys

import pytest

from tests.helpers import mcp_call, mcp_call_once, mcp_list_tools
from conftest import ROOT

# tool name -> valid arguments for a smoke call
SMOKE_ARGS = {
    "count_facilities": {"city": "Berlin"},
    "search_facilities": {"city": "Berlin"},
    "get_facility_details": {"name": "Drehscheibe"},
    "search_services": {"query": "Suchtberatung"},
    "find_social_help": {"need": "debt", "limit": 3},
    "data_quality_report": {"organisation": ""},
    "detect_duplicates": {},
    "check_completeness": {},
    "generate_statistics": {},
    "compare_facilities": {"facility_1": "Drehscheibe", "facility_2": "Metropolitankapitel"},
    "list_providers": {},
    "search_by_postal_code": {"postal_code": "10115"},
    "count_facilities_per_postal_code": {"postal_code_prefix": "10", "limit": 3},
    "facilities_per_inhabitant_by_region": {"level": "zone", "limit": 3},
    "records_to_fix_first": {"city": "Berlin", "n": 3},
    "find_nearby": {"location": "13353", "radius_km": 1, "query": "Sucht, Alkohol"},
    "get_awo_details": {"iri": "https://civic-data.de/federated-directory#org-229a4e0b1081"},
    "get_entity_card": {"iri": "https://civic-data.de/federated-directory#org-34e986356c61"},
    "search_service_candidates": {"query": "Schuldner", "limit": 3},
    "search_directory": {"query": "Schuldner", "has_telephone": True},
    "explain_provenance": {"facility": "https://civic-data.de/federated-directory#org-00416a783a9b"},
    "list_sources": {},
}
JSON_TOOLS = [t for t in SMOKE_ARGS if t != "count_facilities"]


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in mcp_list_tools()}


def test_baseline_tools_are_listed(tools):
    missing = sorted(set(SMOKE_ARGS) - set(tools))
    assert not missing, (
        f"tool(s) {missing} are no longer exposed by server.py; listed: {sorted(tools)}"
    )


def test_every_listed_tool_has_smoke_args(tools):
    untested = sorted(set(tools) - set(SMOKE_ARGS))
    assert not untested, (
        f"new tool(s) {untested} have no entry in SMOKE_ARGS "
        "(tests/protocol/test_server.py). Add valid arguments so they get tested, "
        "and add two eval cases in tests/evals/cases.json."
    )


@pytest.mark.parametrize("tool", sorted(SMOKE_ARGS))
def test_tool_metadata_is_llm_friendly(tools, tool):
    t = tools[tool]
    assert t.description and len(t.description.strip()) >= 20, (
        f"tool '{tool}' needs a real docstring (>= 20 chars): the LLM chooses tools "
        f"from it. Got: {t.description!r}"
    )
    schema = t.input_schema
    assert schema.get("type") == "object", f"'{tool}' input_schema.type is {schema.get('type')!r}"
    props = schema.get("properties", {})
    unknown = [r for r in schema.get("required", []) if r not in props]
    assert not unknown, f"'{tool}' requires {unknown} but does not define them in properties"
    assert set(SMOKE_ARGS[tool]) <= set(props), (
        f"'{tool}' schema properties {sorted(props)} do not include "
        f"the smoke args {sorted(SMOKE_ARGS[tool])}"
    )


@pytest.mark.parametrize("tool", sorted(SMOKE_ARGS))
def test_tool_call_succeeds(tool):
    is_error, text = mcp_call_once(tool, SMOKE_ARGS[tool])
    assert not is_error, f"call {tool}({SMOKE_ARGS[tool]}) returned an MCP error: {text[:300]}"
    assert text.strip(), f"call {tool}({SMOKE_ARGS[tool]}) returned empty output"


@pytest.mark.parametrize("tool", JSON_TOOLS)
def test_tool_returns_valid_json_without_nan(tool):
    _, text = mcp_call_once(tool, SMOKE_ARGS[tool])
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{tool} output is not valid JSON ({exc}); starts with: {text[:200]!r}")
    assert "NaN" not in text, (
        f"{tool} output contains a bare NaN (invalid JSON for many clients); "
        "a SparqlClient method returned a float NaN"
    )


def test_umlauts_are_not_escaped():
    _, text = mcp_call_once("get_facility_details", {"name": "Lübecker"})
    assert "Lübecker" in text, (
        "umlauts missing/escaped in tool output (to_json must use ensure_ascii=False); "
        f"output starts: {text[:200]!r}"
    )


def test_missing_required_argument_is_an_error():
    is_error, text = mcp_call("count_facilities", {})
    assert is_error, f"count_facilities() without 'city' should fail but returned: {text[:200]!r}"
    assert "city" in text, f"error should name the missing argument 'city', got: {text[:200]!r}"


def test_wrong_argument_type_is_an_error():
    is_error, text = mcp_call("count_facilities", {"city": {"not": "a string"}})
    assert is_error, f"count_facilities(city=<dict>) should fail but returned: {text[:200]!r}"


def test_explain_provenance_input_errors_reach_the_model():
    is_error, text = mcp_call("explain_provenance", {"facility": "https://example.org/x"})
    assert is_error, f"a foreign IRI should be an error, got: {text[:200]!r}"
    assert "not a directory IRI" in text, (
        f"the error should say what is wrong (mcp 2.x hides non-ToolError messages): {text[:200]!r}"
    )
    is_error, text = mcp_call("explain_provenance", {"facility": "org-00416a783a9b", "field": "fax"})
    assert is_error and "telephone" in text, f"error should list the valid fields: {text[:200]!r}"


def test_unknown_tool_is_an_error():
    is_error, text = mcp_call("does_not_exist", {})
    assert is_error, f"calling an unknown tool should be an error, got: {text[:200]!r}"


def test_server_starts_over_stdio():
    """Launch `python server.py` as a subprocess, like Claude Code / Open WebUI would."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=60)
                return [t.name for t in (await session.list_tools()).tools]

    try:
        names = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - surface any startup failure with context
        pytest.fail(f"`{sys.executable} server.py` did not start/handshake over stdio: {exc!r}")
    assert set(SMOKE_ARGS) <= set(names), f"stdio server lists {names}, missing baseline tools"
