"""The benchmark against the real server, offline: suites match the server's tools, and an
oracle run goes through the MCP client, agent loop, evaluators and reports end to end."""

import asyncio
import json

import pytest

from benchmarking.core.cases import SUITES, load_suites
from benchmarking.run_benchmark import main, validate_cases
from tests.helpers import mcp_list_tools


@pytest.fixture(scope="module")
def server_tools():
    return [tool.name for tool in mcp_list_tools()]


def test_every_active_case_can_be_run_on_this_server(server_tools):
    cases = load_suites(sorted(SUITES), include_aspirational=True)
    problems = validate_cases(cases, server_tools)
    assert not problems, "benchmark cases that cannot run on server.py:\n  " + "\n  ".join(problems)


def test_aspirational_cases_really_need_missing_tools(server_tools):
    cases = load_suites(sorted(SUITES), include_aspirational=True)
    for case in cases:
        if case.status == "aspirational":
            existing = [t for t in case.needs_tools if t in server_tools]
            assert not existing, (
                f"{case.suite}/{case.id} waits for {existing}, which server.py now has: make it active"
            )


def test_oracle_benchmark_runs_end_to_end_offline(tmp_path):
    code = main(["--offline", "--models", "oracle", "--suites", "smoke",
                 "--cases", "find-help-debt,entity-card-by-iri,service-candidates-with-office",
                 "--tool-profiles", "baseline,all", "--concurrency", "1", "--quiet",
                 "--output-dir", str(tmp_path)])
    assert code == 0
    [report] = tmp_path.glob("benchmark-*.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    passed = {(r["tool_profile"], r["case_id"]): r["passed"] for r in data["runs"]}
    assert passed[("all", "find-help-debt")] and passed[("all", "entity-card-by-iri")]
    assert passed[("baseline", "find-help-debt")]
    assert not passed[("baseline", "entity-card-by-iri")], "baseline profile must hide get_entity_card"
    assert {p["change"] for p in data["paired"]} == {"gain", "same"}
    assert list(tmp_path.glob("leaderboard-*.csv")) and list(tmp_path.glob("paired-*.csv"))


def test_interrupted_run_keeps_its_finished_runs(tmp_path):
    args = ["--offline", "--models", "oracle", "--suites", "smoke", "--cases", "find-help-debt,count-berlin",
            "--concurrency", "1", "--quiet", "--output-dir", str(tmp_path)]
    assert main(args) == 0
    assert not list(tmp_path.glob("runs-partial-*.jsonl")), "a finished run removes its partial file"

    # What an interrupted run leaves behind: the meta line and the runs finished so far.
    [report] = tmp_path.glob("benchmark-*.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    partial = tmp_path / "runs-partial-test.jsonl"
    partial.write_text("\n".join([json.dumps({"meta": data["meta"], "planned_runs": 2}),
                                  json.dumps(data["runs"][0])]) + "\n", encoding="utf-8")
    recovered = tmp_path / "recovered"
    assert main(["--report-from", str(partial), "--quiet", "--output-dir", str(recovered)]) == 0
    [markdown] = recovered.glob("benchmark-*.md")
    assert "Interrupted run: 1 of 2 runs finished" in markdown.read_text(encoding="utf-8")
    [recovered_json] = recovered.glob("benchmark-*.json")
    assert len(json.loads(recovered_json.read_text(encoding="utf-8"))["runs"]) == 1

    # A finished run's JSON can be turned into reports again (e.g. after the report format changed).
    again = tmp_path / "again"
    assert main(["--report-from", str(report), "--quiet", "--output-dir", str(again)]) == 0
    [again_json] = again.glob("benchmark-*.json")
    assert json.loads(again_json.read_text(encoding="utf-8"))["runs"] == data["runs"]


def test_stopped_run_writes_the_report_itself(tmp_path, monkeypatch):
    """Ctrl+C or `kill` cancels the run: the cases that finished are reported, exit code 130."""
    import benchmarking.run_benchmark as run_benchmark

    real, calls = run_benchmark.run_case, []

    async def stopped_on_second_case(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise asyncio.CancelledError()
        return await real(*args, **kwargs)

    monkeypatch.setattr(run_benchmark, "run_case", stopped_on_second_case)
    code = main(["--offline", "--models", "oracle", "--suites", "smoke", "--cases", "find-help-debt,count-berlin",
                 "--concurrency", "1", "--quiet", "--output-dir", str(tmp_path)])
    assert code == 130
    [markdown] = tmp_path.glob("benchmark-*.md")
    assert "Interrupted run: 1 of 2 runs finished" in markdown.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("runs-partial-*.jsonl")), "the partial file is removed once the report exists"


def test_unknown_model_points_to_the_local_models_file(tmp_path, capsys):
    code = main(["--offline", "--models", "no-such-model", "--suites", "smoke", "--output-dir", str(tmp_path)])
    assert code == 2 and "models.local.yaml" in capsys.readouterr().err
