"""explain_provenance and list_sources: offline, on a small synthetic dataset.

PROVENANCE has the shape of the live endpoint's graphs (QUERIES.md, "Provenance"): the
finished directory in the default graph, source records in .../pipeline/mapped.ttl, their
cluster membership in .../pipeline/matches.ttl, source labels in config/federation.ttl and
harvest observations in .../ingest/ingest-log.ttl. Entities, names and values are made up.
The real .rq files run on it through LocalSparqlClient.
"""

import pytest

from src.sparql_client import PROVENANCE_LIMITS, entity_iri, load_query
from tests.helpers import LocalSparqlClient, load_dataset

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CDF ="https://civic-data.de/federated-directory#"
CDP = "https://civic-data.de/pipeline#"

PROVENANCE = """
# ---- finished directory (default graph) ----
cdf:org-aaa111 a org:OrganizationalUnit ;
    schema:name "Beratungsstelle Musterstadt" ;
    schema:telephone "+49 30 1111", "+49 30 2222" ;
    schema:email "Info@beratung.example", "info@beratung.example" ;
    schema:openingHours "Mo-Fr 9-12" ;
    schema:mainEntityOfPage "https://dhs.example/1" .

cdf:org-bbb222 a org:OrganizationalUnit ;
    schema:name "Jugendtreff Muster" ;
    foaf:homepage "http://traeger.example/" ;
    schema:parentOrganization cdf:traeger-ccc333 .

cdf:org-bbb223 a org:OrganizationalUnit ;
    schema:name "Jugendtreff Muster Nord" ;
    schema:alternateName "Nord" .

cdf:traeger-ccc333 a schema:Organization ;
    schema:name "Muster-Träger e.V." ;
    foaf:homepage "http://traeger.example/" .

cdf:org-ddd444 a org:OrganizationalUnit ;
    schema:name "Ohne Quelle" ;
    schema:telephone "+49 30 4444" .

GRAPH <urn:directory:data/pipeline/matches.ttl> {
    cdf:org-aaa111 cdp:hasMember cdp:caritas-1, cdp:dhs-1, cdp:sp-1 .
    cdf:org-bbb222 cdp:hasMember cdp:awo-2 .
    cdf:org-bbb223 cdp:hasMember cdp:awo-4 .
    cdf:traeger-ccc333 cdp:hasMember cdp:awo-3 .
}

GRAPH <urn:directory:data/pipeline/mapped.ttl> {
    cdp:caritas-1 cdp:fromSource cdp:caritasSource ;
        schema:name "Beratungsstelle Musterstadt" ;
        schema:telephone "+49 30 1111" ;
        schema:mainEntityOfPage "https://caritas.example/1" .
    cdp:dhs-1 cdp:fromSource cdp:dhsSource ;
        schema:name "Beratungsstelle Musterstadt" ;
        schema:telephone "+49 30 2222" ;
        schema:email "Info@beratung.example" ;
        schema:openingHours "Mo-Do 9-17" ;
        schema:mainEntityOfPage "https://dhs.example/1" .
    cdp:sp-1 cdp:fromSource cdp:sozialplattformSource ;
        schema:name "Beratungsstelle Musterstadt" ;
        schema:telephone "+49 30 2222" ;
        schema:email "info@beratung.example" ;
        schema:openingHours "Mo-Fr 9-12" .
    cdp:awo-2 cdp:fromSource cdp:awoSource ;
        schema:name "Jugendtreff Muster" .
    cdp:awo-3 cdp:fromSource cdp:awoSource ;
        schema:name "Muster-Träger e.V." ;
        foaf:homepage "http://traeger.example/" .
    cdp:awo-4 cdp:fromSource cdp:awoSource ;
        schema:name "Jugendtreff Muster Nord" .
}

GRAPH <urn:directory:config/federation.ttl> {
    cdp:caritasSource rdfs:label "Caritas" ; cdp:coverageLabel "Germany" .
    cdp:dhsSource rdfs:label "DHS" ; cdp:coverageLabel "Germany" .
    cdp:sozialplattformSource rdfs:label "Sozialplattform" ; cdp:coverageLabel "Germany" .
    cdp:awoSource rdfs:label "AWO" ; cdp:coverageLabel "Berlin-Mitte" .
}

GRAPH <urn:directory:data/ingest/ingest-log.ttl> {
    cdp:obs-awo-old cdp:ofSource cdp:awoSource ; prov:atTime "2026-09-01T00:00:00Z" .
    cdp:obs-awo cdp:ofSource cdp:awoSource ; prov:atTime "2026-10-06T05:20:42Z" ;
        cdp:staticCommittedAt "2026-06-30T12:31:01+02:00" .
    cdp:obs-caritas cdp:ofSource cdp:caritasSource ; prov:atTime "2026-10-06T05:28:15Z" .
    cdp:obs-dhs cdp:ofSource cdp:dhsSource ; prov:atTime "2026-10-06T05:35:27Z" .
    cdp:obs-sp cdp:ofSource cdp:sozialplattformSource ; prov:atTime "2026-10-06T05:20:41Z" .
}
"""


@pytest.fixture(scope="module")
def client():
    return LocalSparqlClient(load_dataset(PROVENANCE))


def field(result, name):
    matches = [f for f in result["fields"] if f["field"] == name]
    assert len(matches) == 1, f"field {name!r} appears {len(matches)} times in {result['fields']}"
    return matches[0]


class NoQueryClient(LocalSparqlClient):
    """Fails the test if any query is sent."""

    def query(self, sparql):
        raise AssertionError(f"no query should have been sent, got:\n{sparql}")


# ---- explain_provenance: values and their sources ------------------------------------

def test_provenance_lists_source_records_with_detail_pages(client):
    result = client.explain_provenance(CDF + "org-aaa111")
    assert result["found"] is True
    assert result["type"] == "facility"
    assert result["name"] == "Beratungsstelle Musterstadt"
    assert result["sources"] == [
        {"source": "Caritas", "record": CDP + "caritas-1",
         "record_name": "Beratungsstelle Musterstadt", "detail_page": "https://caritas.example/1"},
        {"source": "DHS", "record": CDP + "dhs-1",
         "record_name": "Beratungsstelle Musterstadt", "detail_page": "https://dhs.example/1"},
        {"source": "Sozialplattform", "record": CDP + "sp-1",
         "record_name": "Beratungsstelle Musterstadt", "detail_page": None},
    ]
    assert result["limits"] == PROVENANCE_LIMITS


def test_provenance_same_value_from_every_source_agrees(client):
    name = field(client.explain_provenance(CDF + "org-aaa111"), "name")
    assert name == {
        "field": "name",
        "status": "sources_agree",
        "values": [{"value": "Beratungsstelle Musterstadt", "in_directory": True,
                    "sources": ["Caritas", "DHS", "Sozialplattform"]}],
    }


def test_provenance_attributes_each_differing_value(client):
    telephone = field(client.explain_provenance(CDF + "org-aaa111"), "telephone")
    assert telephone == {
        "field": "telephone",
        "status": "sources_differ",
        "values": [
            {"value": "+49 30 2222", "in_directory": True, "sources": ["DHS", "Sozialplattform"]},
            {"value": "+49 30 1111", "in_directory": True, "sources": ["Caritas"]},
        ],
        "differs_only_in_case": False,
    }


def test_provenance_shows_values_the_directory_dropped(client):
    result = client.explain_provenance(CDF + "org-aaa111")
    assert field(result, "opening_hours")["values"] == [
        {"value": "Mo-Fr 9-12", "in_directory": True, "sources": ["Sozialplattform"]},
        {"value": "Mo-Do 9-17", "in_directory": False, "sources": ["DHS"]},
    ]
    assert field(result, "detail_page")["values"] == [
        {"value": "https://dhs.example/1", "in_directory": True, "sources": ["DHS"]},
        {"value": "https://caritas.example/1", "in_directory": False, "sources": ["Caritas"]},
    ]


def test_provenance_flags_case_only_differences(client):
    email = field(client.explain_provenance(CDF + "org-aaa111"), "email")
    assert email["status"] == "sources_differ"
    assert email["differs_only_in_case"] is True


def test_provenance_reports_value_inherited_from_parent(client):
    result = client.explain_provenance(CDF + "org-bbb222")
    assert field(result, "name")["status"] == "single_source"
    assert field(result, "homepage") == {
        "field": "homepage",
        "status": "not_from_own_sources",
        "values": [{
            "value": "http://traeger.example/",
            "in_directory": True,
            "sources": [],
            "inherited_from": {
                "iri": CDF + "traeger-ccc333",
                "name": "Muster-Träger e.V.",
                "source": "AWO",
                "record": CDP + "awo-3",
            },
        }],
    }


def test_provenance_entity_without_source_records(client):
    result = client.explain_provenance(CDF + "org-ddd444")
    assert result["found"] is True
    assert result["sources"] == []
    assert [f["status"] for f in result["fields"]] == ["not_from_own_sources"] * 2
    assert field(result, "telephone")["values"] == [
        {"value": "+49 30 4444", "in_directory": True, "sources": [], "inherited_from": None}
    ]


def test_provenance_field_filter(client):
    result = client.explain_provenance(CDF + "org-aaa111", field="telephone")
    assert [f["field"] for f in result["fields"]] == ["telephone"]
    empty = client.explain_provenance(CDF + "org-aaa111", field="accessibility")
    assert empty["fields"] == [{"field": "accessibility", "status": "no_value", "values": []}]


def test_provenance_services_and_description_need_to_be_asked_for():
    turtle = """
    cdf:org-eee555 a org:OrganizationalUnit ;
        schema:name "Lange Texte" ;
        schema:description "Eine lange Beschreibung" ;
        schema:availableService "Beratung, Begleitung" .
    """
    client = LocalSparqlClient(load_dataset(turtle))
    default = client.explain_provenance("org-eee555")
    assert [f["field"] for f in default["fields"]] == ["name"]
    asked = client.explain_provenance("org-eee555", field="services")
    assert [v["value"] for v in field(asked, "services")["values"]] == ["Beratung, Begleitung"]


# ---- explain_provenance: finding the entry --------------------------------------------

@pytest.mark.parametrize("facility", [
    CDF + "org-aaa111", "org-aaa111", "cdf:org-aaa111", "  org-aaa111  ",
])
def test_provenance_accepts_iri_and_short_id(client, facility):
    assert client.explain_provenance(facility)["iri"] == CDF + "org-aaa111"


def test_provenance_exact_name_wins_over_longer_names(client):
    # "Jugendtreff Muster" is also part of "Jugendtreff Muster Nord"
    assert client.explain_provenance("jugendtreff MUSTER")["iri"] == CDF + "org-bbb222"


def test_provenance_single_partial_name_match_is_used(client):
    assert client.explain_provenance("Musterstadt")["iri"] == CDF + "org-aaa111"


def test_provenance_ambiguous_name_returns_candidates(client):
    result = client.explain_provenance("Jugendtreff")
    assert result["found"] is False
    assert result["candidates"] == [
        {"iri": CDF + "org-bbb222", "name": "Jugendtreff Muster",
         "alternate_name": None, "street": None, "city": None},
        {"iri": CDF + "org-bbb223", "name": "Jugendtreff Muster Nord",
         "alternate_name": "Nord", "street": None, "city": None},
    ]
    assert result["more_candidates"] is False
    assert "iri" in result["hint"]


def test_provenance_unknown_name_and_iri(client):
    by_name = client.explain_provenance("Atlantis-Gibtsnicht")
    assert by_name["found"] is False and by_name["candidates"] == []
    by_iri = client.explain_provenance("org-fff999")
    assert by_iri == {
        "found": False, "query": "org-fff999", "iri": CDF + "org-fff999",
        "hint": by_iri["hint"],
    }
    assert "snapshot" in by_iri["hint"]


def test_provenance_quote_in_name_stays_inside_the_literal(client):
    result = client.explain_provenance('x") || true || ("')
    assert result["found"] is False and result["candidates"] == []


@pytest.mark.parametrize("facility", [
    CDF + 'org-aaa111> } SELECT * WHERE { ?s ?p ?o',
    CDF + "org aaa111",
    'https://civic-data.de/federated-directory#x") || true || ("',
    "https://example.org/org-aaa111",
    "cdf:../secret",
])
def test_provenance_rejects_malformed_iri_without_querying(facility):
    with pytest.raises(ValueError, match="not a directory IRI"):
        NoQueryClient(load_dataset("")).explain_provenance(facility)


@pytest.mark.parametrize("facility", ["", "   "])
def test_provenance_rejects_empty_input(facility):
    with pytest.raises(ValueError, match="facility"):
        NoQueryClient(load_dataset("")).explain_provenance(facility)


def test_provenance_rejects_unknown_field():
    with pytest.raises(ValueError, match="telephone"):
        NoQueryClient(load_dataset("")).explain_provenance("org-aaa111", field="fax")


def test_entity_iri():
    assert entity_iri("Caritas-Zentrum") is None
    assert entity_iri("org-0972c9e82c29") == CDF + "org-0972c9e82c29"
    assert entity_iri("service-00163aaaeddb") == CDF + "service-00163aaaeddb"
    assert entity_iri("traeger-e82cc1d98b19") == CDF + "traeger-e82cc1d98b19"


def test_iri_placeholder_is_escaped_in_provenance_queries():
    for query_name in ("list_value_sources_by_iri", "list_directory_values_by_iri"):
        query = load_query(query_name, iri='a"b')
        assert 'IRI("a\\"b")' in query, f"{query_name}.rq does not escape {{{{iri}}}}"


# ---- list_sources ------------------------------------------------------------------------

def test_list_sources_counts_records_and_keeps_newest_harvest(client):
    result = client.list_sources()
    assert result["sources"] == [
        {"source": "AWO", "coverage": "Berlin-Mitte", "source_records": 3,
         "harvested_at": "2026-10-06T05:20:42Z",
         "static_file_committed_at": "2026-06-30T12:31:01+02:00"},
        {"source": "Caritas", "coverage": "Germany", "source_records": 1,
         "harvested_at": "2026-10-06T05:28:15Z", "static_file_committed_at": None},
        {"source": "DHS", "coverage": "Germany", "source_records": 1,
         "harvested_at": "2026-10-06T05:35:27Z", "static_file_committed_at": None},
        {"source": "Sozialplattform", "coverage": "Germany", "source_records": 1,
         "harvested_at": "2026-10-06T05:20:41Z", "static_file_committed_at": None},
    ]
    assert result["notes"]


def test_list_sources_without_pipeline_graphs_is_empty():
    assert LocalSparqlClient(load_dataset("")).list_sources()["sources"] == []
