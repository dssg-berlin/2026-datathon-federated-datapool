# Benchmark snapshots

Committed benchmark runs, kept as a reference. Each folder holds the full output of one run
(`benchmark-*.md` report, `benchmark-*.json` with every run and answer, leaderboard and
per-run CSV; see [Reports and snapshots](../README.md#reports-and-snapshots)).

## 2026-11-11: before and after the tool cleanup

Does removing overlapping tools and rewriting the tool descriptions make models choose the
right tool? The same 92 cases (suites `smoke`, `citizen`, `professional`, one run per case)
were run against two versions of the server, each in its own Docker container with live
data from the SoSuSe endpoint:

| Folder | Server | Tools |
|---|---|---|
| [`2026-11-11_before-tool-cleanup/`](2026-11-11_before-tool-cleanup/) | commit `fac363e` (datathon tool list) | 22 |
| [`2026-11-11_after-tool-cleanup/`](2026-11-11_after-tool-cleanup/) | commit `c6aee78` (cleaned-up tool list, server instructions) | 19 |

Three models: `qwen2.5:7b` on a local Ollama (16k context), and Claude Haiku 5.5 and Claude
Opus 5.5 through headless Claude Code.

### Result

| Model | Cases passed | Right tool on the first call | Right tool and arguments | Tokens used |
|---|---|---|---|---|
| qwen2.5:7b | 59 → **78** of 92 (64% → 85%) | 62 → **83** of 90 (69% → 92%) | 68% → 91% | 1.10M → 0.92M |
| Claude Haiku 5.5 | 76 → **88** of 92 (83% → 96%) | 77 → **87** of 90 (86% → 97%) | 86% → 97% | 3.49M → 3.17M |
| Claude Opus 5.5 | 82 → **89** of 92 (89% → 97%) | 87 → **90** of 90 (97% → 100%) | 96% → 99% | 4.30M → 3.48M |

A case passes when every check passes: tool choice and arguments, a final answer, no
invented phone numbers or email addresses, saying so when nothing was found, crisis
numbers, expected content. 90 of the 92 cases expect a tool.

Cases passed per suite:

| Model | smoke (44) | citizen (40) | professional (8) |
|---|---|---|---|
| qwen2.5:7b | 38 → 39 | 18 → 33 | 3 → 6 |
| Claude Haiku 5.5 | 35 → 41 | 33 → 39 | 8 → 8 |
| Claude Opus 5.5 | 41 → 44 | 33 → 37 | 8 → 8 |

Cases that changed: qwen 22 gained and 3 lost, Haiku 15 gained and 3 lost, Opus 8 gained
and 1 lost.

### What was wrong, and what changed it

1. **The tool list did not fit into the model's context.** Ollama's default context window
   is 4,096 tokens; the 22 tool definitions alone were about 5,000. Ollama cuts the prompt
   without an error, so the model saw less than half of the tools and, in a first test,
   called none. All numbers above are measured with a 16k context. A local setup that keeps
   the default (Ollama, Open WebUI) measures the cut, not the tools; this is the first thing
   to check when tool choice is poor.
2. **`search_facilities` attracted requests for help.** With the old descriptions, qwen
   called `search_facilities(city)` in 19 cases where a person asked for help with a need
   in a city ("where can I get debt counselling in Berlin?"). The old description was one
   line ("Search for facilities in a city"). The new one says what it is for and names the
   tools to use for a need. After: 0 such calls.
3. **Duplicate tools.** `search_services` / `search_service_candidates`,
   `get_awo_details` / `get_entity_card` and `compare_facilities` /
   `get_facility_details` answered the same requests, and two eval prompts for the IRI
   tools were practically the same question. These pairs cost each model a few cases (Haiku
   most, 9 of its 44 smoke cases failed before). The three duplicates were removed.
4. **`find_social_help` was used for topics it does not cover** (Haiku: 6 wrong calls
   before, 1 after). Its description now lists the six needs and says where to go for
   anything else.
5. **No crisis numbers and invented contacts.** The server now sends MCP instructions
   (search in German, never invent contact details, emergency numbers first). Invented
   contact details went from 1 and 3 cases to 0 for Haiku and Opus; qwen still invents
   them in 3 cases.

Tools the models called first by mistake (runs):

| Model | Before | After |
|---|---|---|
| qwen2.5:7b | `search_facilities` 19, `count_facilities` 3, `search_services` 2, `get_entity_card` 2, `search_directory` 1, `get_awo_details` 1 | `find_social_help` 2, five others 1 each |
| Claude Haiku 5.5 | `find_social_help` 6, `get_entity_card` 2, `search_facilities` 2, three others 1 each | `count_facilities` 2, `find_social_help` 1 |
| Claude Opus 5.5 | `count_facilities` 1, `search_service_candidates` 1, `generate_statistics` 1 | none |

### How to read these numbers

- **One run per case and 92 cases.** The 95% intervals are wide (qwen after: 76%–91%).
  The direction is clear for all three models; single percentage points are not.
- **The final descriptions were tuned on these cases.** A first cleaned-up version scored
  74 of 92 with qwen; its failures led to the refinements that are measured here. Prompts
  that are not in the suites will do somewhat worse than these numbers.
- **The expected tool changed for the removed tools.** Six smoke cases that expected a
  removed tool now expect its replacement, and the removed names were dropped from the
  lists of accepted tools in about 25 citizen and professional cases. The prompts are
  unchanged.
- **Claude ran through Claude Code**, not through the API: Claude Code runs the tool loop
  with its own defaults. See [Claude through Claude Code](../README.md#6-claude-through-claude-code-subscription-login-no-api-key).
- **qwen and long tool results.** Results of several thousand tokens (a whole city, the
  statistics) may not fit next to the tool list in 16k tokens, and Ollama then cuts the
  conversation.
- **Live data.** The directory changes; `answer_contains` checks can break without any
  change to the server.

### Reproduce

```bash
docker compose up -d --build                      # the current server on :8000
ollama serve & ollama pull qwen2.5:7b
python -m benchmarking.run_benchmark --models qwen-2-5-7b-ollama \
    --suites smoke,citizen,professional --transport streamable_http --concurrency 1 \
    --output-dir benchmarking/snapshots/<date>_<label>/qwen2.5-7b

claude auth login                                 # once
python -m benchmarking.run_benchmark --models claude-code-haiku \
    --suites smoke,citizen,professional --transport streamable_http --concurrency 3 \
    --output-dir benchmarking/snapshots/<date>_<label>/claude-haiku
```

The "before" runs used an image built from `fac363e` (`docker build -t awo-mcp:before .`,
`docker run -d -p 8001:8000 awo-mcp:before`, `--url http://localhost:8001/mcp`) and the
harness and cases of commit `76dfe2c`, which still expect the old tools. On an Apple M1 with 16 GB the qwen run takes 1.5 to 2
hours, a Claude run 10 to 20 minutes.
