# AWO MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude Desktop,
Claude Code, any MCP client) answer questions about social support facilities and services
in Germany: "How many facilities are in Berlin?", "Find addiction counselling services",
"How complete is the data?".

The data comes **live** from the *Federated directory of social support services in
Germany* (SoSuSe), a public SPARQL endpoint run by CorrelAid:
`https://sosuse.correlaid.org/directory/sparql`.

> **History.** The project started at a datathon on a local CSV export of AWO data
> (`data/*.csv`). All tools now query the live directory instead. The directory federates
> several providers (mostly Caritas and DHS, AWO is a small part), so numbers are much larger
> than in the CSV days (e.g. Berlin: 646 facilities instead of 151).

![AWO MCP demo with Claude Desktop](assets/awo_mcp_claude_demo.gif)

---

## How it works

```text
AI assistant (Claude Desktop, Claude Code, ...)
   │  MCP over stdio
server.py               20 MCP tools; each calls one SparqlClient method and returns JSON
   │
src/sparql_client.py    loads a query file, fills in the parameters, sends it,
   │                    turns the result rows into the tool's answer
sparql_queries/*.rq     one SPARQL query per file
   │  HTTP GET, application/sparql-results+json
SoSuSe SPARQL endpoint
```

Tools never build query strings themselves. User input only reaches a query through
`load_query()`, which escapes it as a SPARQL string literal. Details: [QUERIES.md](QUERIES.md).

## Repository layout

| Path | What it is |
|---|---|
| `server.py` | The MCP server. Defines the tools and the `find_help` prompt. Tool docstrings are what the LLM reads to choose a tool, so keep them descriptive. |
| `src/sparql_client.py` | `SparqlClient`: one method per tool, plus `load_query()` and the `DATASETS` definition (RDF class and columns of facilities, addresses, services, providers). |
| `src/fix_first.py` | Ranking logic behind `records_to_fix_first` (buckets, scores, provider batches). Works on a pandas table that `SparqlClient.facility_repository()` builds from one query. |
| `sparql_queries/` | The SPARQL queries, one `.rq` file each, with a fixed naming scheme and header ([QUERIES.md](QUERIES.md)). You can paste them into the endpoint's web UI. |
| `src/legacy/` | The old pandas/CSV implementation. **Not used by the server.** Kept as the reference that the offline parity tests compare the SPARQL queries against. |
| `data/directory.ttl` | RDF snapshot of the directory. The offline tests run the real queries on it, so CI needs no network. |
| `data/*Schema.csv` | The original AWO CSV export. Used only by the tests; its headers define the `DATASETS` columns. |
| `tests/` | Five test layers (unit, MCP protocol, live API, LLM connection, LLM tool-calling evals) and a live test dashboard. See [TEST.md](TEST.md). |
| `benchmarking/` | LLM benchmark against the real server: multilingual, professional and smoke case suites, an agent loop over MCP, deterministic evaluators, optional rubric judge, tool-profile comparisons and leaderboards. See [benchmarking/README.md](benchmarking/README.md). |
| `.github/workflows/` | CI: offline tests on every push, live API tests and LLM evals on manual trigger. |
| `doc/` | Datathon presentations. |

## Tools

| Tool | What it returns | Example question |
|---|---|---|
| `count_facilities(city)` | Number of facilities in a city | "How many facilities are in Berlin?" |
| `search_facilities(city)` | Facilities in a city with address and contact data | "List the facilities in Berlin with their contact info" |
| `get_facility_details(name)` | Details, address and provider of facilities whose name contains `name` | "Tell me about the facility Drehscheibe" |
| `compare_facilities(facility_1, facility_2)` | `get_facility_details` for both names, side by side | "Compare Drehscheibe and Metropolitankapitel" |
| `search_services(query)` | Services whose name, category or audience contains `query` | "Which services are there around Suchtberatung?" |
| `find_social_help(need, limit, postal_code_prefix)` | Offers for a need (debt, housing, family, elderly, addiction, migration) with address and contacts; missing contacts are reported as not on record | "Ich habe Schulden, wo bekomme ich Hilfe?" |
| `data_quality_report(organisation)` | Data quality scorecard per organisation (fill rate of phone, email, website, hours, category, address), what to improve, and a draft message to the organisation | "How good is the Caritas data and what should they fix?" |
| `generate_statistics()` | Dataset sizes; facilities per city, category and provider; services per category and audience; how many facilities have an address or provider | "Give me statistics about the directory" |
| `check_completeness()` | Missing values per dataset and column | "Which columns have missing values?" |
| `detect_duplicates()` | Groups of entries that are identical except for their IRI | "Are there duplicate entries?" |
| `list_providers()` | All provider organisations with homepage and address | "Which providers are in the directory?" |
| `search_by_postal_code(postal_code)` | Facilities with a five-digit postal code, with address and contact data | "Which facilities are in 10115?" |
| `count_facilities_per_postal_code(postal_code_prefix, order, limit)` | Facilities, inhabitants and facilities per 10,000 inhabitants per postal code, including codes without any facility; `order` is `most_facilities`, `highest_per_10k` or `lowest_per_10k`. Inhabitants come from `data/einwohner_plz_de.csv` and are estimates (a municipality's population spread evenly over its codes) | "Which postal codes starting with 13 have the fewest facilities per inhabitant?" |
| `facilities_per_inhabitant_by_region(level, order, postal_code_prefix, limit, min_inhabitants)` | Regions compared by facilities per 10,000 inhabitants: `level` is `zone` (first postcode digit), `region` (two digits, default) or `area` (three digits). Each region is named by its towns with the most facilities and carries inhabitants per facility and its density relative to the average. Regions under `min_inhabitants` (default 10,000) are counted but not ranked | "Where in Germany is the ratio of organisations to inhabitants the lowest?" |
| `records_to_fix_first(city, ...)` | Facility records of a city ranked by how much fixing them matters, with reasons, provider batches and a projected coverage gain | "Which Berlin records should we fix first?" |
| `find_nearby(location, radius_km, query, accessible_only, limit)` | Facilities within a radius of a postal code or `"latitude,longitude"`, nearest first, with distance, contacts and opening hours. `query` takes comma-separated terms and matches any of them | "Addiction counselling near 13353?" |
| `get_awo_details(iri)` | Address, map link, contacts, opening hours, accessibility, offers, provider and source link for one facility | "What are the opening hours of this facility?" |
| `get_entity_card(iri)` | Every recorded field of a facility, service, provider or address, the missing fields, syntax-only quality notes (e.g. a phone without leading 0) and typed links (service → facility → address/provider) | "What exactly does the directory record for this entry?" |
| `search_service_candidates(query, limit, offset)` | Services matching name/category/audience, each with the facility or provider that runs it and its postal code and locality; paged | "Which debt counselling services are there, and which office runs each?" |
| `search_directory(query, provider, has_telephone, limit, offset)` | Facilities where every term matches their own text, address or a linked service; provider and phone filters; paged hits, with totals and missing-field counts over all matches | "How many debt counselling facilities in Kreuzberg have a phone number?" |
| `explain_provenance(facility, field=None)` | For one entry (name, IRI or ID): its source records with detail pages, and per field which sources supplied each value, whether they agree and which values the directory kept or inherited | "Which sources list this facility's phone number, and do they agree?" |
| `list_sources()` | The federated sources with declared coverage, record counts and harvest dates | "Which organisations' data is in the directory, and how current is it?" |

Matching is case-insensitive (`GIESSEN` finds `Gießen`). City matches are **exact**, so
`Berlin` does not include `Berlin-Mitte`. Name and service searches are substring matches.

### Where does a value come from?

The directory merges records from several sources (Caritas, DHS, Sozialplattform, AWO,
Destatis). `explain_provenance` reads the pipeline's named graphs on the same endpoint and
shows, per value, which source supplied it. Live example (harvest of 2026-10-06):
*Caritas-Zentrum Kaiserslautern - Schuldnerberatung* has two phone numbers. `+4963136120222`
comes from DHS and Sozialplattform, `+4963136120225` from Caritas. DHS and Sozialplattform
also give different opening hours, and the directory keeps only Sozialplattform's. The
homepage of *Drehscheibe Kinder - und Jugendpolitik* is not in its own AWO record but
inherited from its Träger, so it may lead to a head office.
## Prompts

| Prompt | What it does |
|---|---|
| **Find help near me** (`find_help(situation, location)`) | Takes a need in plain words ("I drink too much") and a postal code or place. Explains the kinds of help that exist and the usual first step, recommends 3–5 nearby places (who each is for, address, directions, hours, and a 1–5 relevance rating), lists who to contact first, and notes what the directory doesn't cover. Puts emergency numbers first if someone is in danger, and never invents contact details. |

Ratings measure how good a first step a place is for the stated need, based on the directory
data (fit, distance, reachability, access), not the quality of the service.

## Setup

Requires Python 3.11+.

```bash
git clone git@github.com:istiyaksiddiquee/awo-mcp-production.git
cd awo-mcp-production
conda create -n awo-mcp python=3.11   # or: python3.11 -m venv .venv
conda activate awo-mcp                # or: source .venv/bin/activate
pip install -r requirements.txt       # the server needs `mcp` and `pandas`
pip install -r requirements-dev.txt   # only for running the tests
```

## Connecting an assistant

The server speaks MCP over stdio: the assistant starts `python server.py` itself, so you
don't run it by hand. Use **absolute paths** to the Python interpreter of your environment
(`which python` / `where python` inside the activated environment) and to `server.py`.

### Claude Desktop

Settings → Developer → Edit Config, then add:

```json
{
  "mcpServers": {
    "awo": {
      "command": "/Users/<you>/miniconda3/envs/awo-mcp/bin/python",
      "args": ["/path/to/awo-mcp-production/server.py"]
    }
  }
}
```

On Windows the paths look like `C:\\Users\\<you>\\miniconda3\\envs\\awo-mcp\\python.exe`
and `C:\\path\\to\\awo-mcp-production\\server.py`. Restart Claude Desktop completely. The
server then shows up under Settings → Developer with a green status.

### Claude Code

```bash
claude mcp add awo -- /path/to/python /path/to/awo-mcp-production/server.py
```

### Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `SPARQL_ENDPOINT` | `https://sosuse.correlaid.org/directory/sparql` | SPARQL endpoint the tools query |

The server reads this from its process environment only, not from `.env` (`.env` is for
the tests). To set it for Claude Desktop, add `"env": {"SPARQL_ENDPOINT": "..."}` to the
server entry above.

## Testing

```bash
pytest -m "not llm and not api"   # offline unit + MCP protocol tests (~8 s, what CI runs)
pytest -m api tests               # every tool against the live endpoint (~1 min)
```

`pytest` opens a live dashboard in your browser (`--no-dashboard` to turn it off). The LLM
evals need a running Open WebUI and are described with everything else in [TEST.md](TEST.md).

## Known limitations

* **Live dependency.** If the endpoint is down or slow (client timeout: 30 s), the tools
  return an MCP error.
* **City names are not normalised** in the source data (`Berlin-Mitte`, `Berlin- Tempelhof`,
  ...). The exact city match counts none of these.
* **Large answers.** `generate_statistics` lists ~5,800 cities, and `check_completeness` /
  `detect_duplicates` scan all ~67k entities (5–15 s). There is no paging yet.
* **Few provider links.** Only a few dozen facilities have a `schema:parentOrganization`,
  so per-provider statistics are thin.
* **Provenance is limited to what the pipeline records.** Values that were corrected,
  combined or geocoded have no source record; `explain_provenance` reports them without a
  source instead of guessing. Entity IRIs can change between directory snapshots.

More on the data model and the endpoint: [QUERIES.md](QUERIES.md).

## Troubleshooting

**The server doesn't show up in Claude Desktop.** Check the config for JSON errors
(trailing commas) and make sure both paths exist. Logs: `~/Library/Logs/Claude/mcp*.log`
(macOS), `%APPDATA%\Claude\logs\mcp*.log` (Windows).

**The server fails to start.** Run it by hand with the same interpreter to see the error:
`/path/to/python server.py`. It should print `AWO MCP server starting...` and then wait for
input (stop it with Ctrl+C). A `ModuleNotFoundError` means the dependencies aren't
installed in that environment.

**Tools return errors.** Check that the endpoint is reachable:
`curl 'https://sosuse.correlaid.org/directory/sparql?query=ASK%20%7B%7D'` should return
`{"head":{},"boolean":true}` (or similar).

## Contributing

Work on a feature branch and open a pull request against `dev`. CI runs the offline tests
on every push. To add a tool, follow "Adding a query-backed tool" in
[QUERIES.md](QUERIES.md) and "Testing a new feature" in [TESTS.md](TESTS.md). 

Once your feature is fully integrated into dev and tested, version it, follwing Semantic Versioning (semver). Main should always be a very stable version, ready for production or benchmarking.

## License

MIT
