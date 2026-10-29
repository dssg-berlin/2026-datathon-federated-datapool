# SPARQL queries

All MCP tools in `server.py` query the live **Federated directory of social support
services in Germany** (SoSuSe, run by CorrelAid) over SPARQL. They no longer read the
local CSVs in `data/`. Every query goes through the custom `SparqlClient` class in
`src/sparql_client.py`. Tools never build query strings themselves.

## The API

| | |
|---|---|
| Endpoint | `https://sosuse.correlaid.org/directory/sparql` (override with the `SPARQL_ENDPOINT` environment variable) |
| Protocol | SPARQL 1.1 over HTTP: `GET ?query=<urlencoded>` |
| Result format | `application/sparql-results+json` (also CSV/TSV, Turtle for CONSTRUCT/DESCRIBE) |
| Auth | none, public |
| Health check | `ASK {}` → `{"boolean": true}` |

The endpoint has a query UI with built-in examples (graph
`urn:directory:webapp/content/query-examples.ttl`, e.g. "Einrichtungen nach Name suchen",
"Einrichtungen im Kartenausschnitt", "Quellen einer Entität").

### Data model (default graph, ~475k triples)

All entities use IRIs under `https://civic-data.de/federated-directory#` (prefix `cdf:`).

| Class | Count | Main properties |
|---|---|---|
| `org:OrganizationalUnit` (facility, *Einrichtung*) | ~37,000 | `schema:name`, `schema:address`, `schema:telephone`, `schema:email`, `foaf:homepage`, `schema:description`, `schema:alternateName`, `schema:openingHours`, `schema:category`, `schema:availableService`, `schema:audience`, `schema:parentOrganization`, `schema:mainEntityOfPage`, `dct:modified` |
| `schema:PostalAddress` | ~26,000 | `schema:streetAddress`, `schema:postalCode`, `schema:addressLocality`, `schema:addressCountry`, `schema:latitude`, `schema:longitude` |
| `schema:Service` (offer, *Angebot*) | ~4,000 | `schema:name`, `schema:category`, `schema:provider` (→ facility), `schema:audience`, `schema:openingHours`, `schema:areaServed` |
| `schema:Organization` (provider, *Träger*) | 4 | `schema:name` |

Prefixes: `schema: <http://schema.org/>`, `org: <http://www.w3.org/ns/org#>`,
`foaf: <http://xmlns.com/foaf/0.1/>`, `dct: <http://purl.org/dc/terms/>`.

Named graphs hold the pipeline behind the directory (`urn:directory:data/pipeline/*`,
`urn:directory:data/provenance.ttl`, `urn:directory:config/*`). Query them with
`GRAPH <…> { … }` if you need to know where a record came from.

### What is possible

* Any read query: `SELECT`, `ASK`, `CONSTRUCT`, `DESCRIBE`, with aggregates (`COUNT`,
  `GROUP BY`), property paths (`schema:address/schema:addressLocality`), `FILTER`,
  `REGEX`, `CONTAINS`, `LCASE`, `OPTIONAL`, `GRAPH`.
* Counting, listing and joining facilities ↔ addresses ↔ services across all providers.
* Geographic queries via the `schema:latitude` / `schema:longitude` literals (bounding-box `FILTER`s).
* Provenance: which source a record came from (named graphs, used by `explain_provenance`
  and `list_sources`, see "Provenance: the named graphs" below).

### What is not possible / limitations

* **Read-only.** Treat the endpoint as read-only; the MCP never sends updates.
* **Not AWO-only.** The directory federates all providers (mostly Caritas and DHS). AWO is a
  small part, so all numbers are much larger than with the old AWO CSVs (e.g. Berlin: 646
  facilities instead of 151). Only 39 facilities
  have a `schema:parentOrganization`, so filtering by provider is limited.
* **City names are not normalised.** `addressLocality` contains values like `Berlin-Mitte`,
  `Berlin- Tempelhof` or `Berl Berlin`. The exact match in `count_facilities_by_city.rq`
  counts only `Berlin` (any case). Districts and typos are not included.
* **Data quality:** not every facility has an address (~37.3k facilities, ~37.0k with one),
  some addresses lack coordinates, and several facilities may share one address.
* **Big answers.** `generate_statistics` returns ~5,800 cities and `check_completeness` /
  `detect_duplicates` scan all ~67k entities (5–15 s). An empty name/query in
  `get_facility_details` / `search_services` matches everything (as in the CSV version).
* **No paging in the client yet.** Large `list_*` queries return everything at once. Add
  `LIMIT`/`OFFSET` in the query file when a tool needs paging.
* **`SparqlClient.query` parses JSON only**, so it is meant for `SELECT` and `ASK`.
  `CONSTRUCT`/`DESCRIBE` return RDF and would need a separate method.
* **Live dependency:** tools and the live tests in `tests/api/` fail (or skip) when the
  endpoint is down or slow (client timeout: 30 s).

## How a query runs: `SparqlClient`

```
MCP tool (server.py)              count_facilities(city)
  → SparqlClient method           sparql.count_facilities(city)
    → load_query(name, **params)  reads sparql_queries/count_facilities_by_city.rq,
                                  fills {{city}} with an escaped literal
    → SparqlClient.query(sparql)  GET <endpoint>?query=…, Accept: sparql-results+json
  ← parsed bindings → int / list / dict returned to the tool
```

* `load_query(query_name, **params)` loads `sparql_queries/<query_name>.rq` and replaces every
  `{{param}}` with the escaped value (`escape_literal`: `\`, `"`, newlines, tabs). A missing
  parameter raises `ValueError`.
* `SparqlClient(endpoint=None, timeout=30)` uses `$SPARQL_ENDPOINT` or the default endpoint.
* `SparqlClient.query(sparql)` runs any SPARQL string and returns the parsed JSON.
* `SparqlClient.select(query_name, **params)` = `load_query` + `query` + `rows()`: a list of
  `{variable: value}` dicts (unbound / empty → `None`).
* One method per tool turns the rows into the tool's result (same shape as the old CSV
  version). `DATASETS` maps the four datasets (`einrichtung`, `adresse`, `angebot`,
  `traeger`) to their RDF class and the CSV column list.

Placeholders are only safe **inside double-quoted string literals** (`"{{city}}"`). Never
put one where an IRI, number, variable or keyword belongs (e.g. `LIMIT {{n}}`). For those,
validate the value in Python first.

## Query files: naming schema

One query per file in `sparql_queries/`:

```
<action>_<entity>[_by_<param>][_per_<group>].rq
```

| Part | Meaning | Values |
|---|---|---|
| `action` | what the query returns | `count` (numbers), `list` (rows), `get` (one item), `ask` (yes/no) |
| `entity` | the main thing queried, plural | `facilities`, `services`, `entities`, `facility_details`, `duplicate_facilities`, … |
| `_by_<param>` | the input it filters on: **must name every `{{param}}`** in the file | `by_city`, `by_name`, `by_query`, `by_city_substring` |
| `_per_<group>` | the result is grouped (one row per group, uses `GROUP BY`) | `per_city`, `per_class`, `per_category` |

Examples: `count_facilities_by_city` = one number, filtered by `{{city}}`.
`count_facilities_per_city` = one count per city, no input.
`list_duplicate_facilities` = rows, no input, no grouping in the name.

All lower snake case. Each file starts with a header comment:

```sparql
# <what it returns, one line>
# Params: {{city}}          (or: none)
# Used by: <method / tool / test that uses it>
```

Apart from placeholders, files are plain SPARQL and can be pasted into the endpoint's
query UI. `test_query_files_follow_naming_convention` in `tests/unit/test_query_files.py` fails
for any `.rq` file that breaks the schema, takes a `{{param}}` its name does not mention,
is named `_per_` without a `GROUP BY`, or lacks the header.

### Text matching (same as Python `str.casefold()`)

Inputs are casefolded in Python (`city.casefold()`) before they go into the query. The data
side is normalised in SPARQL with `REPLACE(LCASE(STR(?x)), "ß", "ss")`. For German text
this equals `casefold()` (`GIESSEN` matches `Gießen`). City matches are exact, name and
service matches are substrings (`CONTAINS`, no regex). Statistics trim values with
`REPLACE(?x, "^\\s+|\\s+$", "")` like pandas `str.strip()`.

## Query files

| File | Returns | Used by (tool) |
|---|---|---|
| `count_facilities_by_city.rq` | `?count` of facilities in a city | `count_facilities` |
| `list_facilities_by_city.rq` | facility + address + contact rows for a city | `search_facilities` |
| `list_facilities_by_postal_code.rq` | facility + address + contact rows for a postal code | `search_by_postal_code` |
| `list_providers.rq` | providers with homepage and address | `list_providers`, `records_to_fix_first` |
| `list_facility_details_by_name.rq` | facility details, address and provider for a name substring | `get_facility_details`, `compare_facilities` |
| `list_facility_quality_fields_by_city.rq` | every field the data quality ranking checks, for the facilities of a city | `records_to_fix_first` |
| `list_services_by_query.rq` | services whose name/category/audience contains the query | `search_services` |
| `list_help_services_by_terms.rq` | services whose name/category matches any need term, with provider facility, address and contacts | `find_help` (tool `find_social_help`) |
| `list_facility_quality_flags.rq` | every facility with filled-field flags, phone, address parts, provider name and source link | `data_quality_report` |
| `list_duplicate_facilities.rq` | groups of facilities identical in all CSV columns except the IRI | `detect_duplicates` |
| `list_duplicate_addresses.rq` | same for addresses | `detect_duplicates` |
| `list_duplicate_services.rq` | same for services | `detect_duplicates` |
| `list_duplicate_providers.rq` | same for providers | `detect_duplicates` |
| `count_entities_per_class.rq` | number of entities per dataset class | `check_completeness`, `generate_statistics` |
| `count_filled_properties_per_class.rq` | entities with a non-blank value, per class and property | `check_completeness` |
| `count_facilities_per_city.rq` | facilities per (trimmed) city | `generate_statistics` |
| `count_facilities_per_postal_code.rq` | facilities per (trimmed) postal code; joined in Python with `data/einwohner_plz_de.csv` | `count_facilities_per_postal_code`, `facilities_per_inhabitant_by_region` |
| `count_facilities_per_postal_code_and_city.rq` | facilities per (trimmed) postal code and city | `facilities_per_inhabitant_by_region` (the towns that name a region) |
| `count_facilities_per_category.rq` | facilities per category | `generate_statistics` |
| `count_facilities_per_provider.rq` | facilities per provider name | `generate_statistics` |
| `count_facility_links.rq` | total / with address / with provider | `generate_statistics` |
| `count_services_per_category.rq` | services per category | `generate_statistics` |
| `count_services_per_audience.rq` | services per audience | `generate_statistics` |
| `list_facility_cities_by_city_substring.rq` | `?facility ?city` rows whose city contains the text | tests only (independent reference) |
| `list_facilities_by_bbox.rq` | facilities with coordinates inside a box, plus contacts, hours, audience, offers and provider | `find_nearby` |
| `get_postal_code_location_by_postal_code.rq` | average coordinates (and count) of the addresses with a postal code | `find_nearby` (search origin) |
| `get_facility_details_by_iri.rq` | one facility by IRI: address, contacts, hours, accessibility, offers, provider, source link | `get_awo_details` |
| `list_entity_properties_by_iri.rq` | every property of one entity (any class) with its type; `?value_is_iri` marks links | `get_entity_card` |
| `list_linked_services_by_iri.rq` | services whose `schema:provider` is the given facility/provider | `get_entity_card` |
| `list_service_candidates_by_query.rq` | services matching name/category/audience, with the linked facility or provider, its type, name, postal code and locality | `search_service_candidates` |
| `list_directory_facilities_by_term.rq` | facilities where a term occurs in their own text, their address or a linked service, with every field `search_directory` filters on or summarises | `search_directory` |

**Numbers and IRIs as inputs.** Placeholders may only be string literals, and the naming
check allows one `_by_` parameter. So `list_facilities_by_bbox.rq` takes a single
`{{bbox}}` string (`"south,west,north,east"`, built and validated by `bounding_box()` in
Python) and splits it with `STRBEFORE`/`STRAFTER` + `xsd:double`. `get_facility_details_by_iri.rq`
turns `{{iri}}` into an IRI with `IRI("{{iri}}")`; `get_awo_details` only accepts IRIs under
`cdf:` without spaces or IRI-breaking characters. `find_nearby` ranks and filters the box in
Python: exact distance (haversine), comma-separated terms, and accessibility.

**Entity cards and linked search.** `get_entity_card`, `search_service_candidates` and
`search_directory` come from the datathon "directory-search" contribution (originally a
patch for the CSV version). `get_entity_card` reads all properties of one entity with a
generic query and follows links in Python (at most two levels: service → facility →
address/provider), so missing, dangling, mistyped and multiple links are reported instead
of dropped. `search_directory` sends only its longest term to SPARQL
(`list_directory_facilities_by_term.rq`); every other term, the provider filter, the
telephone filter, paging and the summaries over all matches are applied in Python. It
needs one term of at least 3 characters so it never scans the whole directory.
| `list_facilities_by_name.rq` | up to 11 facilities whose name contains the text, exact matches first | `explain_provenance` (name lookup) |
| `list_directory_values_by_iri.rq` | an entity's literal values in the directory, whether its own source records supply them, and the parent record they may be inherited from | `explain_provenance` |
| `list_value_sources_by_iri.rq` | the literal values the entity's source records supply, with source label and whether the directory kept them | `explain_provenance` |
| `list_sources.rq` | sources with label, coverage, record count and harvest observations | `list_sources` |
| `list_source_records_by_iri.rq` | an entity's source records and their source ("sources-of") | tests only (independent reference) |
| `list_multi_source_facilities.rq` | up to 5 facilities with records from 2+ sources | tests only (picks a live entity) |

### Provenance: the named graphs

`explain_provenance` and `list_sources` are the only tools that read the named graphs. They
follow the endpoint's own example queries ("sources-of", "competing-values",
"source-status"), but without RDF 1.2 triple terms (`<<( … )>>`), so they also run offline
on rdflib (`tests/helpers.py:load_dataset`).

| Graph | Used for |
|---|---|
| `urn:directory:data/pipeline/matches.ttl` | `?entity cdp:hasMember ?record`: the source records merged into an entity |
| `urn:directory:data/pipeline/mapped.ttl` | each record's `cdp:fromSource` and its values, before merging |
| `urn:directory:config/federation.ttl` | source labels and declared coverage |
| `urn:directory:data/ingest/ingest-log.ttl` | harvest times |

A value counts as supplied by a source when one of the entity's records carries exactly
that RDF term. A directory value that no own record carries was inherited from a parent
(facility → `schema:parentOrganization`, service → `schema:provider`, reported when a parent
record carries the same term), or corrected, combined or geocoded (reported without a
source). The `provenance.ttl` annotations would also explain those cases, but need triple
terms; they are not used. Entity IRIs can change between snapshots, so the live tests
pick their entity with `list_multi_source_facilities.rq`.

### How each tool maps to the old CSV logic

The CSV datasets and the RDF use the same IRIs and properties: a CSV row is an entity, a
filled cell is a triple, an empty cell is a missing triple. `tests/unit/test_sparql_parity.py`
runs these queries on `data/directory.ttl` and checks that the results are identical to the old
`DataRepository` run on tables derived from the same file.

| Tool | Old CSV logic | SPARQL equivalent |
|---|---|---|
| `count_facilities` / `search_facilities` | facility's address must exist in `adresse`; locality casefold == city | `schema:address` → `?a a schema:PostalAddress`; casefold-equal filter |
| `get_facility_details` | name (NaN → "") casefold contains query; address/provider attached only if the link resolves | `COALESCE(name, "")` + `CONTAINS`; `OPTIONAL` blocks that require `a schema:PostalAddress` / `a schema:Organization` |
| `search_services` | name, category or audience contains the query | `OPTIONAL` + `COALESCE` + three `CONTAINS` joined with `||` |
| `check_completeness` | per column: NaN or whitespace-only = missing | rows − entities with a value matching `\S` (IRIs always count as filled) |
| `generate_statistics` | `value_counts()` of trimmed, non-empty values; relationship = link resolves | `GROUP BY` trimmed value, `FILTER(?value != "")`; link counted when its target has the right class |
| `compare_facilities` | `get_facility_details` twice | same |

**Intentional differences**

* **Duplicates ignore the IRI.** The CSV version compared whole rows *including* the IRI. In
  RDF an IRI is one entity, so that check could never find anything. `detect_duplicates` now
  reports entities whose values are identical in every other column, and adds their `iris`.
* **Several values per property.** RDF allows several emails/phones/homepages per facility
  (~150 facilities live). They are returned joined with `"; "` (a CSV cell holds one value).
* **Types.** Postal codes and phone numbers are returned as text, as stored (pandas turned
  them into numbers and dropped leading zeros). Coordinates are floats as before.
* **Order.** Lists are ordered by IRI (the CSV version used file order). Statistics are
  ordered by count, then value.

## Adding a query-backed tool

1. Write the `.rq` file following the naming schema, with the header, and add it to the table above.
2. Add a `SparqlClient` method that calls `self.select("<query_name>", ...)` and returns a plain value.
3. Add an `@mcp.tool()` in `server.py` that calls the method, and register it in `SMOKE_ARGS`.
4. Add offline tests in `tests/unit/` (run the query on `data/directory.ttl` or a small Turtle
   snippet via `LocalSparqlClient(load_graph(...))`), live tests in `tests/api/`, and
   describe them in `TESTS.md`.
