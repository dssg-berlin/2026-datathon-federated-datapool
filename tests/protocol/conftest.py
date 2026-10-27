"""Run the MCP protocol tests offline: the server's SPARQL client reads data/directory.ttl."""

import pytest

from tests.helpers import LocalSparqlClient, load_dataset


@pytest.fixture(scope="package", autouse=True)
def offline_server():
    import server

    live = server.sparql
    # A dataset, not a graph: the provenance tools query named graphs (there are none
    # offline, so they find no sources), and rdflib refuses GRAPH on a plain graph.
    server.sparql = LocalSparqlClient(load_dataset())
    yield
    server.sparql = live
