"""Benchmark harness (benchmarking/): case format, evaluators, agent loop, statistics.

Offline and without an LLM: the agent loop runs with scripted models and a fake tool
runner; the LiteLLM adapter is checked with a stand-in `litellm` module.
"""

import asyncio
import json
import sys
import types

import pytest

from benchmarking.core.agent_loop import run_case
from benchmarking.core.cases import EvalCase, args_mismatch, load_suite, match_value, SUITES
from benchmarking.core.mcp_client import MCPToolExecutionResult, tool_to_openai
from benchmarking.core.model_adapter import (
    InterceptedToolCall, LiteLLMChatModel, ModelEntryConfig, ModelInvocationResult, OracleChatModel,
    RetryPolicyConfig, oracle_arguments, parse_arguments,
)
from benchmarking.evaluators import (
    AnswerContentEvaluator, CompletionEvaluator, ContactGroundingEvaluator, SafetyAbstentionEvaluator,
    ToolSelectionEvaluator,
)
from benchmarking.metrics import build_scorecard, compute_wilson_confidence_interval, paired_changes, summarize
from benchmarking.metrics.statistics import compute_latency_percentiles
from benchmarking.run_benchmark import profile_tools, validate_cases

SCHEMA = {
    "type": "object",
    "properties": {"need": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["need"],
}
TOOLS = [tool_to_openai({"name": "find_social_help", "description": "x", "input_schema": SCHEMA}),
         tool_to_openai({"name": "count_facilities", "description": "y",
                         "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                                          "required": ["city"]}})]


def case(**fields):
    return EvalCase(**{"id": "c1", "prompt": "Ich habe Schulden.", **fields})


class Scripted:
    """A chat model that replays fixed responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []

    async def complete(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.responses.pop(0)


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    async def call_tool(self, tool_name, arguments=None, timeout_seconds=None):
        self.calls.append((tool_name, arguments))
        text = self.outputs[tool_name]
        failed = text.startswith("ERROR")
        return MCPToolExecutionResult(tool_name=tool_name, arguments=arguments or {}, success=not failed,
                                      is_error=failed, latency_ms=1.0, raw_text_outputs=[text],
                                      error_message=text if failed else None)


def call(name, **arguments):
    return ModelInvocationResult(model_id="m", success=True, intercepted_tool_calls=[
        InterceptedToolCall(call_id=f"id-{name}", tool_name=name, arguments=arguments,
                            raw_arguments_json=json.dumps(arguments))])


def answer(text):
    return ModelInvocationResult(model_id="m", success=True, assistant_content=text)


def run(model, runner, the_case, tools=TOOLS, **kwargs):
    return asyncio.run(run_case(model, runner, the_case, tools, model_id="m", **kwargs))


# ---- case format -------------------------------------------------------------------

@pytest.mark.parametrize("expected, actual, ok", [
    ("debt", "DEBT", True), ("schuldner", "Schuldnerberatung", True), ("debt", "housing", False),
    (True, True, True), (True, "true", False), (0.8, 0.85, True), (0.8, 1.5, False),
    (52.52, 52.5219, True), (10, 10.5, True), ({"any_of": ["housing", "debt"]}, "debt", True),
    ({"any_of": ["housing", "debt"]}, "family", False), (["sucht", "alkohol"], "Sucht, Alkohol", True),
    ("x", None, False),
])
def test_match_value(expected, actual, ok):
    assert match_value(expected, actual) is ok


def test_args_mismatch_names_the_argument():
    assert args_mismatch({"need": "debt"}, {"need": "debt"}) == []
    assert args_mismatch({"need": "debt"}, {}) == ["missing argument 'need' (expected ~'debt')"]


def test_args_for_uses_per_tool_expectations():
    c = case(expect_tool=["find_social_help", "find_nearby"], expect_args={"need": "debt"},
             expect_args_by_tool={"find_nearby": {"location": "10997"}})
    assert c.args_for("find_social_help") == {"need": "debt"}
    assert c.args_for("find_nearby") == {"location": "10997"}
    assert c.args_for("search_directory") == {}


def test_case_rejects_unknown_fields_and_empty_tool_list():
    with pytest.raises(ValueError):
        EvalCase(id="x", prompt="p", expect_tools="typo")
    with pytest.raises(ValueError):
        EvalCase(id="x", prompt="p", expect_tool=[])


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_every_suite_loads(suite):
    cases = load_suite(suite)
    assert cases, f"suite {suite} is empty"
    for c in cases:
        if c.status == "aspirational":
            assert c.needs_tools, f"{suite}/{c.id} is aspirational but names no missing tool"


def test_validate_cases_and_profiles():
    server = ["find_social_help", "count_facilities", "search_directory"]
    ok = case(expect_tool="find_social_help")
    missing = case(id="c2", expect_tool="sparql_query")
    assert validate_cases([ok], server) == []
    assert "none of ['sparql_query']" in validate_cases([missing], server)[0]
    assert profile_tools({"exclude": ["search_directory"]}, server) == ["find_social_help", "count_facilities"]
    assert profile_tools({"include": ["count_facilities"]}, server) == ["count_facilities"]
    assert profile_tools({}, server) == server


# ---- agent loop ----------------------------------------------------------------------

def test_loop_feeds_tool_results_back_and_grades_the_final_answer():
    model = Scripted(call("find_social_help", need="debt"), answer("Schuldnerberatung Mitte, Tel. 030 123456"))
    runner = FakeRunner({"find_social_help": '{"results": [{"telephone": "030 123456"}]}'})
    result = run(model, runner, case(expect_tool="find_social_help", expect_args={"need": "debt"}))
    assert runner.calls == [("find_social_help", {"need": "debt"})]
    assert result.final_answer.startswith("Schuldnerberatung") and result.steps == 2
    second_turn = model.seen[1]
    assert second_turn[-1]["role"] == "tool" and "030 123456" in second_turn[-1]["content"], (
        "the tool result must be sent back to the model before it answers"
    )


def test_loop_reports_unknown_tool_to_the_model_and_continues():
    model = Scripted(call("sparql_query", query="x"), answer("Sorry."))
    result = run(model, FakeRunner({}), case(expect_tool="find_social_help"))
    assert result.tool_results[0].is_error and "Unknown tool" in result.tool_results[0].error_message
    assert result.final_answer == "Sorry."


def test_loop_stops_after_max_steps():
    model = Scripted(*[call("count_facilities", city="Berlin")] * 3)
    result = run(model, FakeRunner({"count_facilities": "646"}), case(), max_steps=3)
    assert result.loop_error == "model gave no final answer after 3 steps"


def test_oracle_calls_the_expected_tool_with_full_iri_and_answers():
    c = case(prompt="Details zu https://civic-data.de/federated-directory#org-1 bitte",
             expect_tool="get_entity_card", expect_args={"iri": "org-1"})
    tools = [tool_to_openai({"name": "get_entity_card", "description": "x",
                             "input_schema": {"type": "object", "properties": {"iri": {"type": "string"}}}})]
    runner = FakeRunner({"get_entity_card": "[]"})
    result = run(OracleChatModel(c, {}), runner, c, tools=tools)
    assert runner.calls == [("get_entity_card", {"iri": "https://civic-data.de/federated-directory#org-1"})]
    assert result.final_answer.startswith("Nothing matching was found")


def test_oracle_fills_required_arguments():
    assert oracle_arguments(case(expect_tool="find_social_help", expect_args={"need": {"any_of": ["debt", "x"]}}),
                            "find_social_help", SCHEMA) == {"need": "debt"}


# ---- evaluators ----------------------------------------------------------------------

def graded(model_calls_and_answer, the_case, outputs):
    result = run(Scripted(*model_calls_and_answer), FakeRunner(outputs), the_case)
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}
    evaluators = [ToolSelectionEvaluator(schemas), CompletionEvaluator(), ContactGroundingEvaluator(),
                  SafetyAbstentionEvaluator(), AnswerContentEvaluator()]
    return result, {e.evaluator_name: e.evaluate(result) for e in evaluators}


def test_tool_selection_wrong_tool_and_wrong_args():
    the_case = case(expect_tool="find_social_help", expect_args={"need": "debt"})
    _, r = graded([call("count_facilities", city="Berlin"), answer("646")], the_case, {"count_facilities": "646"})
    assert not r["tool_selection"].passed and r["tool_selection"].stage == "wrong tool chosen"
    _, r = graded([call("find_social_help", need="housing"), answer("ok")], the_case, {"find_social_help": "[]"})
    assert r["tool_selection"].stage == "wrong tool arguments"
    _, r = graded([call("find_social_help", limit=3), answer("ok")], the_case, {"find_social_help": "[]"})
    assert "missing required argument 'need'" in r["tool_selection"].failures


def test_tool_selection_no_call():
    _, r = graded([answer("Geh zur Schuldnerberatung.")], case(expect_tool="find_social_help"), {})
    assert r["tool_selection"].stage == "model did not call a tool"
    _, r = graded([answer("Bitte ruf 112 an.")], case(requires_tool_call=False, requires_hotline=True), {})
    assert r["tool_selection"] is None and r["safety_abstention"].passed


def test_contact_grounding_flags_invented_and_tolerates_reformatted():
    stored = '{"telephone": "3025293540", "email": "info@example.org"}'
    the_case = case(expect_tool="find_social_help", expect_args={"need": "debt"})
    _, r = graded([call("find_social_help", need="debt"), answer("Ruf 030 25293540 an, info@example.org")],
                  the_case, {"find_social_help": stored})
    assert r["contact_grounding"].passed and r["contact_grounding"].warnings, "leading 0 added: warn, not fail"
    _, r = graded([call("find_social_help", need="debt"), answer("Ruf 030 98765432 an oder fake@caritas.de")],
                  the_case, {"find_social_help": stored})
    assert not r["contact_grounding"].passed
    assert r["contact_grounding"].diagnostics["invented_phones"] == ["030 98765432"]
    assert r["contact_grounding"].diagnostics["invented_emails"] == ["fake@caritas.de"]
    _, r = graded([call("find_social_help", need="debt"), answer("Notruf 112, TelefonSeelsorge 0800 111 0 111, PLZ 10997")],
                  the_case, {"find_social_help": "[]"})
    assert r["contact_grounding"].passed, "emergency numbers and postal codes are not invented contacts"


def test_safety_abstention_requires_saying_nothing_was_found():
    the_case = case(expect_tool="find_social_help", expect_no_results=True)
    _, r = graded([call("find_social_help", need="debt"), answer("Leider wurden keine passenden Einträge gefunden.")],
                  the_case, {"find_social_help": "[]"})
    assert r["safety_abstention"].passed
    _, r = graded([call("find_social_help", need="debt"),
                   answer("Try https://civic-data.de/federated-directory#org-made-up")],
                  the_case, {"find_social_help": "[]"})
    assert not r["safety_abstention"].passed and r["safety_abstention"].sub_scores["no_invented_entities"] == 0.0


def test_completion_and_answer_content():
    the_case = case(expect_tool="count_facilities", answer_contains=["646"])
    result, r = graded([call("count_facilities", city="Berlin"), answer("Es sind 646.")], the_case,
                       {"count_facilities": "646"})
    assert r["completion"].passed and r["answer_content"].passed
    card = build_scorecard(result, r)
    assert card.passed and card.composite_score == 1.0
    _, r = graded([call("count_facilities", city="Berlin"), answer("Viele.")], the_case,
                  {"count_facilities": "ERROR: endpoint down"})
    assert r["completion"].sub_scores["tool_errors"] == 1.0 and not r["answer_content"].passed


# ---- statistics and reports ----------------------------------------------------------

def test_wilson_interval_known_values():
    ci = compute_wilson_confidence_interval(8, 10)
    assert ci.wilson_lower == pytest.approx(0.4902, abs=1e-3) and ci.wilson_upper == pytest.approx(0.9433, abs=1e-3)
    assert compute_wilson_confidence_interval(0, 0).n == 0


def test_latency_percentiles_match_linear_interpolation():
    stats = compute_latency_percentiles([10, 20, 30, 40])
    assert stats.p50_ms == 25.0 and stats.p90_ms == 37.0 and stats.max_ms == 40.0


def test_summary_and_paired_changes():
    cards = []
    for profile, passes in (("baseline", False), ("all", True)):
        the_case = case(expect_tool="count_facilities", answer_contains=["646"])
        text = "646" if passes else "?"
        result, r = graded([call("count_facilities", city="Berlin"), answer(text)], the_case,
                           {"count_facilities": "646"})
        result.tool_profile = profile
        cards.append(build_scorecard(result, r))
    summaries = {s.tool_profile: s for s in summarize(cards)}
    assert summaries["all"].passed == 1 and summaries["baseline"].stages == {"final answer missing expected content": 1}
    [change] = paired_changes(cards, "baseline", "all")
    assert change.change == "gain"


# ---- LiteLLM adapter (with a stand-in litellm module) --------------------------------

def test_parse_arguments():
    assert parse_arguments('{"need": "debt"}')[0] == {"need": "debt"}
    assert parse_arguments({"a": 1})[0] == {"a": 1}
    assert parse_arguments("not json")[2].startswith("arguments are not valid JSON")
    assert parse_arguments("[1]")[2] == "arguments must be a JSON object, got list"


def test_litellm_adapter_parses_tool_calls_and_uses_api_key(monkeypatch):
    seen = {}

    async def acompletion(**kwargs):
        seen.update(kwargs)
        function = types.SimpleNamespace(name="find_social_help", arguments='{"need": "debt"}')
        message = types.SimpleNamespace(content=None, tool_calls=[types.SimpleNamespace(id="t1", function=function)])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason="tool_calls")],
                                     usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=10))

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion, drop_params=False))
    monkeypatch.setenv("TEST_KEY", "secret")
    cfg = ModelEntryConfig(id="m", name="m", litellm_model="openai/x", provider="p", tier="fast",
                           api_base="http://localhost:3000/api", api_key_env="TEST_KEY",
                           cost_per_1k_input_usd=1.0, cost_per_1k_output_usd=2.0)
    result = asyncio.run(LiteLLMChatModel(cfg, RetryPolicyConfig(max_retries=0)).complete([], TOOLS))
    assert seen["api_key"] == "secret" and seen["api_base"] == "http://localhost:3000/api"
    assert result.intercepted_tool_calls[0].arguments == {"need": "debt"}
    assert result.usage.total_tokens == 110 and result.usage.estimated_cost_usd == pytest.approx(0.12)


def test_litellm_adapter_reports_missing_api_key(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(drop_params=False))
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cfg = ModelEntryConfig(id="m", name="m", litellm_model="x", provider="p", tier="fast", api_key_env="MISSING_KEY")
    result = asyncio.run(LiteLLMChatModel(cfg).complete([], []))
    assert not result.success and "MISSING_KEY" in result.error_message


# ---- runner adapters (adapters/), local model file and report additions --------------

def test_claude_code_events_become_a_case_run():
    from benchmarking.adapters import claude_code
    from benchmarking.core.agent_loop import CaseRun

    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": ""},
            {"type": "tool_use", "id": "t1", "name": "mcp__awo__count_facilities", "input": {"city": "Berlin"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "646"}]}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "There are 646."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "There are 646.", "num_turns": 2,
         "total_cost_usd": 0.01, "duration_ms": 5000, "duration_api_ms": 4000,
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 20}},
    ]
    run = CaseRun(case=case(), model_id="claude-code-haiku", tool_profile="all", run_index=1,
                  offered_tools=["count_facilities"])
    claude_code.apply_events(run, events, max_steps=6)
    assert [(c.tool_name, c.arguments) for c in run.tool_calls] == [("count_facilities", {"city": "Berlin"})]
    assert run.tool_results[0].success and run.tool_results[0].text == "646"
    assert run.final_answer == "There are 646." and not run.loop_error and run.steps == 2
    assert run.usage.total_tokens == 120 and run.model_latency_ms == 4000 and run.tool_latency_ms == 1000


def test_claude_code_errors_and_step_limit_are_loop_errors():
    from benchmarking.adapters import claude_code
    from benchmarking.core.agent_loop import CaseRun

    def run_with(final):
        run = CaseRun(case=case(), model_id="m", tool_profile="all", run_index=1, offered_tools=[])
        claude_code.apply_events(run, [final] if final else [], max_steps=6)
        return run.loop_error

    assert "Not logged in" in run_with({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"})
    assert "after 6 steps" in run_with({"type": "result", "subtype": "error_max_turns", "is_error": True})
    assert "without a result" in run_with(None)


def test_claude_code_command_offers_only_the_profile_tools():
    from benchmarking.adapters import claude_code
    from benchmarking.core.mcp_client import MCPConnectionConfig

    cfg = ModelEntryConfig(id="claude-code-haiku", name="h", litellm_model="claude-haiku-5-5",
                           provider="claude-code", tier="fast", extra_params={"effort": "low"})
    cmd = claude_code.command("claude", cfg, "Hallo", "/tmp/mcp.json", ["search_directory"], 6, "system")
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5" and cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--disallowedTools") + 1] == "mcp__awo__search_directory"
    assert cmd[cmd.index("--effort") + 1] == "low" and "--strict-mcp-config" in cmd
    http = MCPConnectionConfig(transport="streamable_http", url="http://localhost:8000/mcp")
    assert claude_code.mcp_server_config(http) == {"type": "http", "url": "http://localhost:8000/mcp"}
    stdio = claude_code.mcp_server_config(MCPConnectionConfig(transport="stdio"))
    assert stdio["args"][0].endswith("server.py") and stdio["args"][0].startswith("/")
    with pytest.raises(ValueError):
        claude_code.mcp_server_config(MCPConnectionConfig(transport="inprocess"))


def test_models_yaml_loads_and_passes_extra_params(monkeypatch):
    from benchmarking.core.model_adapter import ModelsRegistryConfig
    from benchmarking.run_benchmark import CONFIG_DIR

    registry = ModelsRegistryConfig.from_yaml(CONFIG_DIR / "models.yaml")
    assert not [m.id for m in registry.list_all_models() if m.provider == "claude-code"], (
        "setup-specific entries belong in models.local.yaml, not in the shared models.yaml")
    for model in registry.list_all_models():
        if model.provider == "ollama":  # the tool list alone needs ~5,000 tokens
            assert model.extra_params.get("num_ctx", 0) >= 16384, f"{model.id} would cut the tool list"
    ollama = registry.get_model("qwen-2-5-7b-ollama")

    seen = {}

    async def acompletion(**kwargs):
        seen.update(kwargs)
        message = types.SimpleNamespace(content="ok", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
                                     usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion, drop_params=False))
    asyncio.run(LiteLLMChatModel(ollama, RetryPolicyConfig(max_retries=0)).complete([], TOOLS))
    assert seen["num_ctx"] == ollama.extra_params["num_ctx"]


def test_right_tool_rate_and_tool_confusions():
    from benchmarking.metrics.aggregator import RunScorecard, right_tool_rate, tool_confusions, wrong_first_tools

    def card(case_id, expected, called):
        return RunScorecard(
            model_id="m", tool_profile="all", suite="smoke", case_id=case_id, run_index=1, section="smoke",
            language="", expected_tools=expected, tools_called=called, passed=False, stage="", composite_score=0.0,
            evaluator_scores={}, evaluator_passed={}, steps=1, tool_errors=0, total_tokens=0,
            estimated_cost_usd=0.0, model_latency_ms=0.0, tool_latency_ms=0.0, final_answer="")

    cards = [card("a", ["find_social_help"], ["find_social_help"]),
             card("b", ["find_social_help"], ["count_facilities", "find_social_help"]),
             card("c", ["find_social_help"], ["count_facilities"]), card("d", ["count_facilities"], []),
             card("e", [], ["count_facilities"])]
    assert right_tool_rate(cards) == 0.25  # case e expects no tool and is not graded
    assert tool_confusions(cards) == [("find_social_help", "count_facilities", 2, ["b", "c"]),
                                      ("count_facilities", "(no tool call)", 1, ["d"])]
    assert wrong_first_tools(cards) == [("count_facilities", 2), ("(no tool call)", 1)]


def test_local_models_file_overrides_and_adds(tmp_path):
    from benchmarking.core.model_adapter import ModelsRegistryConfig
    from benchmarking.run_benchmark import CONFIG_DIR

    (tmp_path / "models.yaml").write_text((CONFIG_DIR / "models.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    shared = ModelsRegistryConfig.load(tmp_path)
    assert shared.get_model("qwen-2-5-7b-ollama").timeout_seconds == 60.0

    # The example file is what a user copies; it must load and merge.
    (tmp_path / "models.local.yaml").write_text(
        (CONFIG_DIR / "models.local.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    merged = ModelsRegistryConfig.load(tmp_path)
    mine = merged.get_model("qwen-2-5-7b-ollama")
    assert mine.litellm_model == "ollama_chat/qwen2.5:7b" and mine.timeout_seconds == 900.0
    assert [m.id for m in merged.list_all_models()].count("qwen-2-5-7b-ollama") == 1, "an overridden id exists once"
    assert {m.id for m in merged.resolve("claude_code")} == {"claude-code-haiku", "claude-code-opus"}
    assert len(merged.list_all_models()) == len(shared.list_all_models()) + 2


def test_adapters_are_found_by_provider():
    from benchmarking import adapters

    claude = adapters.for_provider("claude-code")
    assert claude is not None and callable(claude.run_case) and callable(claude.preflight)
    assert adapters.for_provider("openai") is None, "providers without an adapter use the LiteLLM loop"
    from benchmarking.core.mcp_client import MCPConnectionConfig
    refused = asyncio.run(claude.preflight(MCPConnectionConfig(transport="inprocess"), False))
    assert "--offline" in refused
