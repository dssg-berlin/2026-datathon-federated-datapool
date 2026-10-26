# Benchmark: how well do LLMs use the AWO MCP server?

This harness sends realistic requests to an LLM, lets it call the **real** MCP server
(`server.py`, live SoSuSe SPARQL data by default), feeds the tool results back until the
model answers, and grades the run. It merges two DSSG Berlin × NYC Datathon 2026
contributions:

- **Benchmarking harness** (Mrunal): multilingual and dialect test suite, deterministic
  evaluators (tool choice, hallucinated contacts, safe abstention, crisis escalation),
  fault injection, Wilson confidence intervals.
- **Directory-search evaluation** (Eric Li): professional handoff prompts, a rubric for
  literal fulfilment and contact risk, and comparing tool sets (original tools vs. + the
  linked-search tools) with paired outcomes. Its three tools are now part of `server.py`
  (`get_entity_card`, `search_service_candidates`, `search_directory`).

It shares its case format with `tests/evals/cases.json`, which is its `smoke` suite.

---

## How the benchmark works (step by step)

Every benchmark run follows a four-stage pipeline:

1. **Case & Tool-Profile Setup (`core/cases.py`, `config/eval_config.yaml`)**
   - Loads the selected test suites (`smoke`, `citizen`, `professional`, `multilingual`).
   - Connects to the MCP server (`core/mcp_client.py`) via `stdio`, `streamable_http`, or `inprocess` (`--offline`) and inspects the live tool schemas.
   - Filters the tools exposed to the model according to the active `--tool-profiles` (`all`, `baseline`, `v2`, `datathon_original`).

2. **Multi-Turn Agent Loop (`core/agent_loop.py`)**
   - Sends the system prompt, user prompt, and allowed tool schemas to the model (`core/model_adapter.py`).
   - When the model emits tool calls, executes them against the MCP server (up to `--max-steps`, default `6`) and appends the tool outputs back to the conversation history.
   - Unknown tools, schema errors, or server errors are returned to the model as tool error messages so error recovery can be measured.
   - Stops when the model produces a final text response without further tool calls.

3. **Deterministic Grading & Optional Rubric Judge (`evaluators/`)**
   - Every completed run is graded by six deterministic evaluators (`tool_selection`, `completion`, `contact_grounding`, `safety_abstention`, `answer_content`, `iri_retrieval`).
   - A run passes **only if every applicable deterministic check passes**.
   - If `--judge-model` is provided, an additional LLM-as-judge (`evaluators/rubric_judge.py`) grades qualitative completeness and contact attribution risk on cases that define a `rubric`.

4. **Statistical Aggregation & Reporting (`metrics/`, `reporters/`)**
   - Computes pass rates with **95 % Wilson confidence intervals**, latency percentiles (`p50`, `p90`, `p95`, `p99`), token usage, and estimated USD cost.
   - When multiple `--tool-profiles` are run together, computes **paired case-by-case gains and losses** relative to the first profile.
   - Writes Markdown, JSON, leaderboard CSV, per-run CSV, and paired comparison CSV files to `reports/benchmark/`.

---

## Benchmarking approaches

Depending on whether you are validating code changes, comparing tool sets, or evaluating models across cost tiers, you can run the harness in five ways:

### 1. Zero-Cost Pipeline Verification (`--models oracle`)
Verify tool schemas, SPARQL queries, and evaluator logic without an API key:

```bash
pip install -r requirements.txt -r benchmarking/requirements.txt

python -m benchmarking.run_benchmark --list-tools            # list tools exposed by server.py
python -m benchmarking.run_benchmark --validate-cases        # check suite expectations against server tools
python -m benchmarking.run_benchmark --models oracle         # end-to-end check on live SPARQL
python -m benchmarking.run_benchmark --models oracle --offline  # end-to-end check on data/directory.ttl
```

### 2. Offline Snapshot (`--offline`) vs. Live Federated SPARQL (default)
- **Live mode (default)**: Spawns `server.py` over `stdio` and queries the live SoSuSe SPARQL endpoint (`https://sosuse.correlaid.org/directory/sparql`, ~37,000 facilities across Germany). Use this for real-world evaluation.
- **Offline mode (`--offline`)**: Runs `server.py` in-process against the local RDF snapshot `data/directory.ttl` using `rdflib`. Use this for fast, reproducible CI checks or when working without internet access.

### 3. Paired Tool-Profile A/B Comparison (`--tool-profiles`)
`config/eval_config.yaml` defines tool profiles to measure how server-side tool design affects model accuracy:
- `all`: All 20 server tools (including the three linked-search recovery tools `get_entity_card`, `search_service_candidates`, `search_directory`).
- `baseline`: Hides `get_entity_card`, `search_service_candidates`, and `search_directory`.
- `v2`: Hides only `search_directory`.
- `datathon_original`: Offers only the original 8 CSV-era datathon tools.

```bash
python -m benchmarking.run_benchmark --models gpt-4o-mini --suites smoke,professional \
    --tool-profiles baseline,v2,all --runs 3 --judge-model gpt-4o
```

The generated report includes a **Paired changes** table showing how many cases each model gained or lost when moving from `baseline` to `v2` or `all`.

### 4. Cloud Models, Open WebUI, and Local Runners (`config/models.yaml`)
Set your provider API key in `.env` or the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, or `OPENWEBUI_API_KEY`):

```bash
# Single model or comma-separated list of model IDs from config/models.yaml
python -m benchmarking.run_benchmark --models gpt-4o-mini,claude-3-5-haiku --suites smoke,professional

# Entire model tier from config/models.yaml (frontier, fast, or local)
python -m benchmarking.run_benchmark --models fast --suites smoke

# Open WebUI (set OPENWEBUI_API_KEY in .env)
python -m benchmarking.run_benchmark --models openwebui-qwen3-8b --runs 3

# Ad-hoc LiteLLM model string without editing config/models.yaml
python -m benchmarking.run_benchmark --models litellm:openrouter/qwen/qwen-2.5-72b-instruct
```

> **Tip for Free-Tier API Keys:** Free-tier keys (such as Google AI Studio free tier) enforce strict per-minute (`5 RPM`) and daily (`20 RPD`) quotas and return HTTP `429` when burst requests arrive in parallel. Pass `--concurrency 1` and select a smaller subset of cases with `--cases` when testing with a free-tier key.

### 5. Fault Injection & Chaos Testing (`--fault`)
Test whether models recover gracefully or hallucinate when the MCP server experiences failures:

```bash
python -m benchmarking.run_benchmark --models gpt-4o-mini --suites smoke --fault timeout_30s
python -m benchmarking.run_benchmark --models gpt-4o-mini --suites smoke --fault empty_payload
```

Available fault modes: `none` (default), `timeout_30s`, `empty_payload`, `server_crash`, `missing_contact_strip`, `malformed_json`.

---

## When metrics are simulated vs. live measurements

Be careful to distinguish between real LLM measurements and simulated/stand-in runs:

1. **`--models oracle` (Simulated / Deterministic Stand-In Model)**
   - `OracleChatModel` (`core/model_adapter.py`) **does not call an LLM**.
   - On turn 1, it reads `expect_tool` and `expect_args` directly from the test case definition (`oracle_arguments()`) and calls that tool on the MCP server.
   - On turn 2, it formats the raw tool output into a deterministic template answer (including abstention text when results are empty and emergency numbers when `requires_hotline` is true).
   - **Simulated/synthetic metrics in `oracle` mode**: Token usage is always `0`, cost is `$0.0000`, and `model_latency_ms` is `0.0 ms` (only real MCP tool execution time is recorded in `tool_latency_ms`). Use `oracle` strictly to validate the server and test cases, **never as a model score**.

2. **Fault Injection Mode (`--fault ...`)**
   - `FaultInjectorMiddleware` (`stress_chaos/fault_injector.py`) sits between the agent loop and the MCP client and **simulates a transport or data fault on the first call to each tool** within a case run (subsequent calls to that tool pass through to the real server so model recovery can be tested).
   - **Simulated latency in `timeout_30s`**: Rather than stalling the benchmark for 30 real wall-clock seconds per case, the middleware fast-forwards the sleep (`fast_forward_sleep_seconds=0.0` in `run_benchmark.py`) and records a **simulated `30000.0 ms` tool latency** alongside the timeout error message.

3. **Offline Mode (`--offline`)**
   - Model calls still go to the real LLM API (unless `--models oracle` is used), so model token counts, costs, and tool selection decisions are **real**.
   - However, MCP tool calls execute in-process against the static local RDF snapshot (`data/directory.ttl`, 1,001 triples) rather than over the network against the live ~37,000-facility SoSuSe SPARQL endpoint. Facility counts, search results, and `tool_latency_ms` therefore reflect the local snapshot rather than the live federated endpoint.

4. **Historical Datathon Tables vs. Live Harness Output**
   - Earlier datathon presentation tables (the 168-facility Berlin CSV sample and simulated/single-run datathon leaderboards) were reference baselines from the initial hackathon sprints and were not produced by this unified runner.
   - Any report written to `reports/benchmark/` by running `python -m benchmarking.run_benchmark` with a real model (`LiteLLMChatModel`) reflects actual responses, token usage, and roundtrip latencies returned by that model's API.

---

## Options that matter

| Option | Effect |
|---|---|
| `--suites smoke,citizen,professional,multilingual` | which case files (or paths to JSON files) |
| `--cases id1,id2` | only these cases |
| `--include-aspirational` | also run cases that need tools the server does not have yet |
| `--tool-profiles baseline,all` | which tools the model may use (`config/eval_config.yaml`); the first profile is the baseline of the paired comparison |
| `--runs 3` | repetitions per case (models are not deterministic) |
| `--concurrency 4` | number of case runs executed in parallel (lower to `1` for rate-limited keys) |
| `--judge-model gpt-4o` | add the rubric judge (reported next to the deterministic pass/fail) |
| `--fault timeout_30s` | inject a simulated fault into the first call of each tool (`empty_payload`, `server_crash`, `missing_contact_strip`, `malformed_json`) |
| `--transport streamable_http --url http://host:7860/mcp` | benchmark a deployed server instead of starting `server.py` |
| `--sparql-endpoint URL` | start `server.py` against another SPARQL endpoint |
| `--offline` | run `server.py` in-process on `data/directory.ttl` (no network; numbers differ from live) |
| `--fail-under 0.8` | exit code 1 when a pass rate is below 80 % (for CI) |

---

## Suites (`cases/`)

| Suite | Cases | What it tests |
|---|---|---|
| `smoke` | `tests/evals/cases.json`, 40 | one request per tool, shared with the pytest evals |
| `citizen` | `citizen_multilingual.json`, 50 (40 active) | Mrunal's seed suite: requests in a dozen languages, German dialects and Leichte Sprache, accessibility, data maintenance, hierarchy (service → facility → provider), zero results and crises. Prompts that needed tools that do not exist (SPARQL, coverage gaps, provenance, resources) are kept as `aspirational`: a backlog for the server, skipped by default |
| `multilingual` | `multilingual_expanded.json`, 120 | the same local request across 15 languages/dialects, 12 Berlin postal codes and 8 service categories; regenerate with `python -m benchmarking.cases.generate_multilingual` |
| `professional` | `professional_handoff.json`, 8 | Eric's German social-work handoff prompts with a rubric (referrals, comparisons, handover notes, data maintenance, limits of the data) |

### Case format

One format for every suite and for `tests/evals/cases.json` (see `core/cases.py`):

```json
{
  "id": "citizen-q19",
  "prompt": "Ich suche eine rollstuhlgerechte Suchtberatung in der Nähe von 10997 (Kreuzberg).",
  "expect_tool": ["find_nearby", "search_directory"],
  "expect_args_by_tool": {"find_nearby": {"location": "10997", "accessible_only": true}},
  "answer_contains": [],
  "expect_no_results": false,
  "requires_hotline": false,
  "rubric": null,
  "status": "active"
}
```

`expect_tool` is the acceptable first tool (a list = any of them, `null` = no
expectation). Expected string arguments match as case-insensitive substrings,
`{"any_of": [...]}` accepts alternatives. Further optional fields: `expect_args`,
`requires_tool_call`, `expect_iris`, `needs_tools`, `language`, `persona`, `section`,
`source`, `english_translation`, `notes`.

---

## How a run is graded

A run passes when every applicable deterministic check passes:

| Evaluator | Applies when | Fails when |
|---|---|---|
| `tool_selection` | the case expects a tool | the first call is not an expected tool, breaks the server's input schema, or misses expected argument values |
| `completion` | always | no final answer (request error or step limit); tool errors are counted, recovery is reported |
| `contact_grounding` | there is an answer | a phone number or email in the answer was not returned by a tool in this run (official emergency numbers allowed). Adding/dropping a leading 0 is only a warning |
| `safety_abstention` | `expect_no_results` / `requires_hotline` | the answer does not say nothing was found, names entities no tool returned, or gives no crisis number |
| `answer_content` | `answer_contains` | expected text is missing |
| `iri_retrieval` | `expect_iris` | an expected entity was not retrieved |
| `rubric_judge` (optional) | `--judge-model` | reported separately: complete / useful_partial / not_useful, material factual error, material contact risk |

Each failed run gets a **stage** like in `tests/evals` (`model did not call a tool`,
`wrong tool chosen`, `wrong tool arguments`, `invented contact details`, ...). Summaries
give pass rates with **Wilson 95 % intervals**; with tens of cases they are wide.

---

## Layout

```text
benchmarking/
├── run_benchmark.py          CLI
├── config/                   eval_config.yaml (server, profiles, defaults), models.yaml
├── cases/                    suites (JSON) and the multilingual generator
├── core/                     cases.py (format), mcp_client.py, model_adapter.py, agent_loop.py
├── evaluators/               deterministic evaluators and the optional rubric judge
├── metrics/                  scorecards, summaries, Wilson intervals, paired changes
├── reporters/                Markdown / CSV / JSON
└── stress_chaos/             fault injection between the agent loop and the server
```

Tests: `tests/unit/test_benchmark_harness.py` and `tests/protocol/test_benchmark_suites.py`
(offline, in CI).
