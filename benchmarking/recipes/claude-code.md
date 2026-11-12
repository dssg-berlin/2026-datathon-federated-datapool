# Recipe: Claude through Claude Code (subscription login, no API key)

Runs each case as a headless [Claude Code](https://code.claude.com/docs/en/setup) session
(`claude -p`) that gets only this server's tools and the benchmark's system prompt. Claude
Code signs in with a Claude subscription, so no `ANTHROPIC_API_KEY` is needed. The adapter
is `adapters/claude_code.py`.

```bash
claude auth login                  # once; opens the browser
docker compose up -d --build       # the server under test on http://localhost:8000/mcp

cp benchmarking/config/models.local.example.yaml benchmarking/config/models.local.yaml
python -m benchmarking.run_benchmark --models claude-code-haiku,claude-code-opus \
    --suites smoke,citizen,professional --transport streamable_http --concurrency 3
```

- **Where the CLI is found:** `CLAUDE_CODE_BIN`, then `claude` on the `PATH`, then inside
  the Claude desktop app (macOS).
- **Differences from an API model.** Claude Code runs the tool loop and talks to the MCP
  server itself, so `--offline` and `--fault` do not apply, tool results are not cut by
  the harness, and thinking and effort are Claude Code's defaults (set
  `extra_params: {effort: low}` on the model entry to change the effort). Compare such
  numbers with API runs with that in mind.
- **Usage.** The runs count against the plan's usage limits; the report's cost column is
  what the API would have charged. About 10 to 20 minutes per model for the three default
  suites.
