# Recipe: a local model with Ollama

Runs the benchmark against a model on a local [Ollama](https://ollama.com), without an API
key.

```bash
ollama serve                       # skip if the Ollama app is already running
ollama pull qwen2.5:7b             # once, 4.7 GB
docker compose up -d --build       # the server under test on http://localhost:8000/mcp

cp benchmarking/config/models.local.example.yaml benchmarking/config/models.local.yaml
python -m benchmarking.run_benchmark --models qwen-2-5-7b-ollama \
    --suites smoke,citizen,professional \
    --transport streamable_http --concurrency 1
```

The entry in `models.local.yaml` replaces the shared `qwen-2-5-7b-ollama` with the tag
that is installed (`qwen2.5:7b`) and a timeout that suits a laptop. Change
`litellm_model` to `ollama_chat/<any tag from "ollama list">` for another model.

- **Context size.** Keep `extra_params: {num_ctx: 16384}` (or more). With Ollama's default
  of 4,096 tokens the tool list is cut and the numbers measure the cut, not the tools.
- **`--concurrency 1`.** Ollama answers one request at a time and reuses the evaluated tool
  list between consecutive requests; parallel cases only wait and break that reuse.
- **Duration.** About 35 to 70 s per case on an Apple M1 with 16 GB (cases with large tool
  results take minutes): 1.5 to 2 hours for the three default suites. Keep the machine
  awake, e.g. `caffeinate -i python -m benchmarking.run_benchmark ...` on macOS.
- Leave out `--transport streamable_http` to start `server.py` directly instead of using
  the container.
