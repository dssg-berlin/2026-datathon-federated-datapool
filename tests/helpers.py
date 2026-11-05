"""Helpers shared by the test layers."""

import asyncio
import csv
import json

import pandas as pd

from conftest import DATA_DIR
from src.sparql_client import DATASETS, SparqlClient, expand

CSV_FILES = {
    "einrichtung": "einrichtungSchema.csv",
    "adresse": "adresseSchema.csv",
    "angebot": "angebotSchema.csv",
    "traeger": "traegerSchema.csv",
}


def raw_rows(dataset: str) -> list[dict]:
    """Read a dataset with the csv module (independent of pandas/DataLoader)."""
    with open(DATA_DIR / CSV_FILES[dataset], newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def make_repo(einrichtung=(), adresse=(), angebot=(), traeger=()):
    """Build a DataRepository from small synthetic rows.

    Rows are dicts; columns not given are NaN. Column names come from the real CSVs.
    """
    from src.legacy.data_repository import DataRepository

    given = {
        "einrichtung": einrichtung,
        "adresse": adresse,
        "angebot": angebot,
        "traeger": traeger,
    }
    frames = {}
    for name, rows in given.items():
        with open(DATA_DIR / CSV_FILES[name], newline="", encoding="utf-8") as f:
            columns = next(csv.reader(f))
        frames[name] = pd.DataFrame(list(rows), columns=columns)
    return DataRepository(frames)


# ---- MCP in-memory client (sync wrappers) --------------------------------

def _server():
    import server

    return server.mcp


def _mcp_session(action):
    """Run action(session) against the real server over in-memory streams."""
    from mcp.shared.memory import create_client_server_memory_streams
    from mcp import ClientSession
    import anyio

    async def run():
        result_store: list = []
        lowlevel = _server()._lowlevel_server
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            read_client, write_client = client_streams
            read_server, write_server = server_streams
            async with anyio.create_task_group() as tg:
                tg.start_soon(lowlevel.run, read_server, write_server,
                              lowlevel.create_initialization_options())
                async with ClientSession(read_client, write_client) as session:
                    await session.initialize()
                    result_store.append(await action(session))
                    tg.cancel_scope.cancel()
        return result_store[0]

    return asyncio.run(run())


def mcp_list_tools():
    """List the server's tools over MCP."""

    async def action(session):
        return (await session.list_tools()).tools

    return _mcp_session(action)


def mcp_call(name: str, arguments: dict | None = None) -> tuple[bool, str]:
    """Call a tool on the real server in-process. Returns (is_error, text)."""

    async def action(session):
        result = await session.call_tool(name, arguments or {})
        text = "".join(part.text for part in result.content if getattr(part, "text", None))
        return result.is_error, text

    return _mcp_session(action)


def mcp_list_prompts():
    """List the server's prompts over MCP."""

    async def action(session):
        return (await session.list_prompts()).prompts

    return _mcp_session(action)


def mcp_get_prompt(name: str, arguments: dict | None = None) -> str:
    """Render a prompt over MCP and return the text of its messages."""

    async def action(session):
        result = await session.get_prompt(name, arguments or {})
        return "".join(
            message.content.text
            for message in result.messages
            if getattr(message.content, "text", None)
        )

    return _mcp_session(action)


_MCP_RESULTS: dict = {}


def mcp_call_once(name: str, arguments: dict | None = None) -> tuple[bool, str]:
    """mcp_call, but each (tool, arguments) runs only once per test session.

    The tools are read-only queries against the live endpoint; several tests check the
    same call's output, so they share one result instead of re-sending heavy queries.
    """
    import server

    # The protocol layer swaps in an offline client; keep its results apart from live ones.
    key = (server.sparql.endpoint, name, json.dumps(arguments or {}, sort_keys=True))
    if key not in _MCP_RESULTS:
        _MCP_RESULTS[key] = mcp_call(name, arguments)
    return _MCP_RESULTS[key]


class CachingSparqlClient(SparqlClient):
    """SparqlClient for tests: each query is sent once per client instance."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cache = {}

    def query(self, sparql: str) -> dict:
        if sparql not in self.cache:
            self.cache[sparql] = super().query(sparql)
        return self.cache[sparql]


def pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:300]


# ---- SPARQL parity: directory.ttl is the single source --------------------------

TTL_FILE = DATA_DIR / "directory.ttl"

TTL_PREFIXES = """\
@prefix schema: <http://schema.org/> .
@prefix org: <http://www.w3.org/ns/org#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
@prefix cdf: <https://civic-data.de/federated-directory#> .
"""


def load_graph(turtle: str | None = None):
    """The RDF source: directory.ttl, or a small Turtle snippet for edge cases."""
    import rdflib

    graph = rdflib.Graph()
    if turtle is None:
        graph.parse(TTL_FILE)
    else:
        graph.parse(data=TTL_PREFIXES + turtle, format="turtle")
    return graph


TRIG_PREFIXES = TTL_PREFIXES + """\
@prefix cdp: <https://civic-data.de/pipeline#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
"""


def load_dataset(trig: str | None = None):
    """The RDF source as a dataset, for queries that read named graphs (GRAPH <...>).

    rdflib refuses GRAPH on a plain Graph. Triples outside GRAPH blocks form the default
    graph (the finished directory), GRAPH blocks become named graphs, as on the endpoint.
    Without a snippet: directory.ttl as the default graph and no named graphs.
    """
    import rdflib

    dataset = rdflib.Dataset()
    if trig is None:
        dataset.parse(TTL_FILE, format="trig")  # Turtle is valid TriG: all in the default graph
    else:
        dataset.parse(data=TRIG_PREFIXES + trig, format="trig")
    return dataset


def graph_to_frames(graph) -> dict:
    """Tables for the old CSV code, derived from the RDF graph.

    One row per entity of each dataset class (DATASETS in src/sparql_client.py), one
    column per CSV column, value = the property's value as text, NaN when absent.
    A property with several values cannot be one CSV cell, so it raises.
    """
    import rdflib

    frames = {}
    for name, dataset in DATASETS.items():
        records = []
        rdf_class = rdflib.URIRef(expand(dataset["class"]))
        for subject in sorted(set(graph.subjects(rdflib.RDF.type, rdf_class)), key=str):
            record = {"iri": str(subject)}
            for column in dataset["columns"][1:]:
                values = list(graph.objects(subject, rdflib.URIRef(expand(column))))
                if len(values) > 1:
                    raise ValueError(f"{subject} has {len(values)} values for {column}")
                record[column] = str(values[0]) if values else float("nan")
            records.append(record)
        frames[name] = pd.DataFrame(records, columns=dataset["columns"], dtype=object)
    return frames


class LocalSparqlClient(SparqlClient):
    """SparqlClient that runs queries on an in-memory rdflib graph instead of HTTP."""

    def __init__(self, graph):
        super().__init__(endpoint="local://rdflib")
        self.graph = graph

    def query(self, sparql: str) -> dict:
        result = json.loads(self.graph.query(sparql).serialize(format="json"))
        if "results" in result:
            # rdflib quirk: GROUP BY over zero matches yields one empty row;
            # the SPARQL spec (and the live Fuseki endpoint) yield none.
            result["results"]["bindings"] = [
                b for b in result["results"]["bindings"] if b
            ]
        return result


def make_sources(turtle: str | None = None):
    """(DataRepository, LocalSparqlClient) over the same RDF data.

    The client runs the real .rq files on the graph; the old CSV code gets the tables
    derived from the same graph (graph_to_frames).
    """
    from src.legacy.data_repository import DataRepository

    graph = load_graph(turtle)
    return DataRepository(graph_to_frames(graph)), LocalSparqlClient(graph)


def normalize(value):
    """NaN -> None recursively, so pandas output compares with SPARQL output."""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, float) and value != value:
        return None
    return value
