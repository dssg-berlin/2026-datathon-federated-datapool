"""Offline checks on cases.json, so a broken eval case fails in CI, not on the eval runner.

No LLM and no network: tools are listed from server.py, and each case's expected arguments
are run on the offline server (data/directory.ttl) to prove they are a valid call.
"""

import json
from pathlib import Path

import pytest

from tests.helpers import LocalSparqlClient, load_dataset, mcp_call, mcp_list_tools

CASES = json.loads((Path(__file__).parent / "cases.json").read_text())
CASES_PER_TOOL = 2


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in mcp_list_tools()}


@pytest.fixture(scope="module")
def offline_server():
    import server

    live = server.sparql
    # A dataset, not a graph: rdflib refuses the provenance tools' GRAPH queries on a graph.
    server.sparql = LocalSparqlClient(load_dataset())
    yield
    server.sparql = live


def test_eval_case_ids_are_unique():
    ids = [case["id"] for case in CASES]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicated, f"cases.json has duplicate ids: {duplicated}"


def test_every_tool_has_two_eval_cases(tools):
    counts = {name: sum(case["expect_tool"] == name for case in CASES) for name in tools}
    short = {name: count for name, count in counts.items() if count < CASES_PER_TOOL}
    assert not short, (
        f"tool(s) with fewer than {CASES_PER_TOOL} eval cases in tests/evals/cases.json: {short}"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_eval_case_expects_a_valid_call(tools, offline_server, case):
    assert case["prompt"].strip(), f"case '{case['id']}' has an empty prompt"
    assert case["expect_tool"] in tools, (
        f"case '{case['id']}' expects tool '{case['expect_tool']}', which server.py does not "
        f"expose; tools: {sorted(tools)}"
    )
    properties = tools[case["expect_tool"]].input_schema.get("properties", {})
    expected = case.get("expect_args", {})
    unknown = sorted(set(expected) - set(properties))
    assert not unknown, (
        f"case '{case['id']}' expects argument(s) {unknown} that '{case['expect_tool']}' "
        f"does not take; it takes {sorted(properties)}"
    )
    is_error, text = mcp_call(case["expect_tool"], expected)
    assert not is_error, (
        f"case '{case['id']}': calling {case['expect_tool']}({expected}) fails, so no model "
        f"could pass it: {text[:300]}"
    )
