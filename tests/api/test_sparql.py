"""SPARQL API layer: do the tools work against the live endpoint?

Integration tests: real .rq files, real SparqlClient, real endpoint, and the real MCP
server (in-memory client). Logical equivalence with the old CSV code is checked offline
in tests/unit/test_sparql_parity.py; here we check consistency on the live data.

Needs network access to the SPARQL endpoint ($SPARQL_ENDPOINT or the CorrelAid
default). Tests that need it are skipped when the endpoint is unreachable.
Expected values come from a differently shaped query counted in Python, not from
the code under test.
"""

import json

import pytest

from src.sparql_client import SparqlClient, load_query
from tests.helpers import CachingSparqlClient, mcp_call_once

pytestmark = pytest.mark.api

# vista WIGWAM Connect, Berlin (present in the live directory and in data/directory.ttl)
KNOWN_FACILITY_IRI = "https://civic-data.de/federated-directory#org-229a4e0b1081"


@pytest.fixture(scope="module")
def client():
    client = CachingSparqlClient()
    try:
        assert client.query("ASK {}")["boolean"] is True
    except Exception as exc:  # noqa: BLE001 - any failure means "unreachable"
        pytest.skip(f"SPARQL endpoint {client.endpoint} unreachable: {exc!r}")
    return client


def expected_city_count(client: SparqlClient, city: str) -> int:
    """Fetch facility/locality pairs broadly, then match the city in Python."""
    query = load_query("list_facility_cities_by_city_substring", city=city)
    rows = client.query(query)["results"]["bindings"]
    return len({
        r["facility"]["value"]
        for r in rows
        if r["city"]["value"].casefold() == city.casefold()
    })


# ---- live endpoint -----------------------------------------------------------

@pytest.mark.parametrize("city", ["Berlin", "München", "Köln"])
def test_count_matches_independent_query(client, city):
    expected = expected_city_count(client, city)
    actual = client.count_facilities(city)
    assert expected > 0, f"sanity: no facilities found for {city} on {client.endpoint}"
    assert actual == expected, (
        f"count_facilities({city!r}) returned {actual}, expected {expected}"
    )


def test_city_match_is_case_insensitive(client):
    counts = {c: client.count_facilities(c) for c in ("Berlin", "berlin", "BERLIN")}
    assert len(set(counts.values())) == 1, f"case changes the count: {counts}"


def test_unknown_city_returns_zero(client):
    assert client.count_facilities("Atlantis-Gibtsnicht") == 0


def test_query_injection_does_not_widen_match(client):
    city = 'x") || true || ("'
    assert client.count_facilities(city) == 0, (
        "a quote in the city name changed the FILTER: escaping in load_query failed"
    )


def test_mcp_count_facilities_returns_reference_count(client):
    is_error, text = mcp_call_once("count_facilities", {"city": "Berlin"})
    assert not is_error, f"count_facilities returned an MCP error: {text[:300]}"
    expected = expected_city_count(client, "Berlin")
    assert text.strip() == str(expected), (
        f"count_facilities('Berlin') returned {text!r}, expected {expected}"
    )


# ---- live: every tool ----------------------------------------------------------

CITY_KEYS = {"iri", "name", "street", "postal_code", "city", "telephone", "email", "homepage"}
DETAIL_KEYS = {
    "iri", "name", "alternate_name", "telephone", "email", "homepage",
    "category", "service_description",
}
SERVICE_KEYS = {"iri", "schema:name", "schema:category", "schema:audience"}
DATASET_NAMES = {"einrichtung", "adresse", "angebot", "traeger"}


@pytest.fixture(scope="module")
def statistics(client):
    return client.generate_statistics()


@pytest.fixture(scope="module")
def completeness(client):
    return client.check_completeness()


def test_search_facilities_matches_count_and_reference(client):
    results = client.search_facilities("Berlin")
    assert len(results) == client.count_facilities("Berlin")
    reference = {
        r["facility"]["value"]
        for r in client.query(load_query("list_facility_cities_by_city_substring", city="berlin"))["results"]["bindings"]
        if r["city"]["value"].casefold() == "berlin"
    }
    assert {r["iri"] for r in results} == reference


def test_search_facilities_result_shape_and_city(client):
    results = client.search_facilities("berlin")
    assert results
    for facility in results:
        assert set(facility) == CITY_KEYS, f"unexpected keys {sorted(facility)}"
        assert facility["city"].casefold() == "berlin", f"{facility['iri']} is in {facility['city']!r}"


def test_get_facility_details_known_facility(client):
    results = client.get_facility_details("drehscheibe")
    assert results, "no facility named like 'Drehscheibe' on the endpoint"
    for detail in results:
        assert DETAIL_KEYS <= set(detail) <= DETAIL_KEYS | {"address", "provider"}
        assert "drehscheibe" in detail["name"].casefold()
    known = {d["iri"]: d for d in results}.get(
        "https://civic-data.de/federated-directory#org-00416a783a9b"
    )
    assert known is not None, "facility org-00416a783a9b (Drehscheibe) not returned"
    assert known["address"]["city"] == "Berlin"
    assert known["provider"]["name"].startswith("SPI")


def test_get_facility_details_unknown_name_is_empty(client):
    assert client.get_facility_details("Atlantis-Gibtsnicht-XYZ") == []


def test_search_services_every_result_matches(client):
    results = client.search_services("Suchtberatung")
    assert results
    assert len({r["iri"] for r in results}) == len(results), "a service is listed twice"
    for service in results:
        assert set(service) == SERVICE_KEYS
        fields = [service["schema:name"], service["schema:category"], service["schema:audience"]]
        assert any("suchtberatung" in (f or "").casefold() for f in fields), service


def test_detect_duplicates_structure(client):
    duplicates = client.detect_duplicates()
    assert set(duplicates) <= DATASET_NAMES
    for dataset, groups in duplicates.items():
        for group in groups:
            assert group["occurrences"] == len(group["iris"]) >= 2, (dataset, group)
            assert "iri" not in group["row"]


def test_check_completeness_consistent(client, completeness, statistics):
    assert set(completeness) == DATASET_NAMES
    for dataset, info in completeness.items():
        assert info["rows"] == statistics["dataset_sizes"][dataset]
        assert info["columns_detail"]["iri"]["missing"] == 0
        assert info["missing_cells"] == sum(c["missing"] for c in info["columns_detail"].values())
        assert info["total_cells"] == info["rows"] * info["columns"]
        for column, detail in info["columns_detail"].items():
            assert 0 <= detail["missing"] <= detail["total"], (dataset, column, detail)
            assert 0 <= detail["missing_percentage"] <= 100, (dataset, column, detail)


def test_generate_statistics_consistent(client, statistics):
    sizes = statistics["dataset_sizes"]
    assert set(sizes) == DATASET_NAMES and all(n > 0 for n in sizes.values()), sizes
    links = statistics["facility_relationships"]
    assert links["with_address"] + links["without_address"] == sizes["einrichtung"]
    assert links["with_provider"] + links["without_provider"] == sizes["einrichtung"]
    assert statistics["facilities_by_city"]["Berlin"] == client.count_facilities("Berlin")
    assert sum(statistics["facilities_by_provider"].values()) >= links["with_provider"]
    assert sum(statistics["services_by_category"].values()) <= sizes["angebot"]


def test_compare_facilities_equals_two_detail_lookups(client):
    result = client.compare_facilities("Drehscheibe", "Metropolitankapitel")
    assert [side["search_name"] for side in result] == ["Drehscheibe", "Metropolitankapitel"]
    assert result[0]["matches"] == client.get_facility_details("Drehscheibe")
    assert result[1]["matches"] == client.get_facility_details("Metropolitankapitel")
    assert all(side["matches"] for side in result)


# ---- live: provenance (named graphs) -----------------------------------------------

DREHSCHEIBE = "https://civic-data.de/federated-directory#org-00416a783a9b"


@pytest.fixture(scope="module")
def multi_source_facility(client):
    """A facility described by records from several sources (IRIs change between snapshots)."""
    rows = client.select("list_multi_source_facilities")
    assert rows, f"sanity: no facility with records from 2+ sources on {client.endpoint}"
    return rows[0]["facility"]


@pytest.fixture(scope="module")
def provenance(client, multi_source_facility):
    return client.explain_provenance(multi_source_facility)


def test_explain_provenance_sources_match_reference(client, multi_source_facility, provenance):
    reference = {
        row["record"]
        for row in client.select("list_source_records_by_iri", iri=multi_source_facility)
    }
    assert provenance["found"] is True
    assert {s["record"] for s in provenance["sources"]} == reference, (
        f"explain_provenance({multi_source_facility}) lists other source records than sources-of"
    )
    assert len({s["source"] for s in provenance["sources"]}) >= 2


def test_explain_provenance_values_are_consistent(provenance):
    sources = {s["source"] for s in provenance["sources"]}
    for field in provenance["fields"]:
        supplied = {source for value in field["values"] for source in value["sources"]}
        assert supplied <= sources, (field["field"], supplied - sources)
        for value in field["values"]:
            assert value["sources"] or value["in_directory"], (
                f"{field['field']}: {value['value']!r} has neither a source nor a directory entry"
            )
        if not supplied:
            assert field["status"] == "not_from_own_sources", field
        elif len(supplied) == 1:
            assert field["status"] == "single_source", field
        else:
            assert field["status"] in {"sources_agree", "sources_differ"}, field


def test_explain_provenance_telephones_match_facility_details(client, provenance):
    """The kept phone numbers are the ones get_facility_details shows (another query)."""
    details = {d["iri"]: d for d in client.get_facility_details(provenance["name"])}
    telephone = details[provenance["iri"]]["telephone"]
    kept = {
        value["value"]
        for field in provenance["fields"] if field["field"] == "telephone"
        for value in field["values"] if value["in_directory"]
    }
    assert kept == (set(telephone.split("; ")) if telephone else set())


def test_explain_provenance_shows_inherited_homepage(client):
    result = client.explain_provenance(DREHSCHEIBE, field="homepage")
    assert result["found"] is True
    [homepage] = result["fields"]
    assert homepage["status"] == "not_from_own_sources", homepage
    [value] = homepage["values"]
    inherited = value["inherited_from"]
    assert inherited, f"homepage {value['value']!r} of {DREHSCHEIBE} has no inherited_from"
    assert inherited["name"].startswith("SPI") and "AWO" in inherited["source"], inherited


def test_explain_provenance_resolves_exact_name(client):
    result = client.explain_provenance("Drehscheibe Kinder - und Jugendpolitik")
    assert result["iri"] == DREHSCHEIBE


def test_explain_provenance_quote_in_name_matches_nothing(client):
    result = client.explain_provenance('x") || true || ("')
    assert result["found"] is False and result["candidates"] == []


def test_list_sources_live(client):
    sources = client.list_sources()["sources"]
    labels = {s["source"] for s in sources}
    assert {"Caritas", "Sozialplattform"} <= labels, labels
    assert any("DHS" in label for label in labels) and any("AWO" in label for label in labels)
    for source in sources:
        assert source["source_records"] > 0 and source["harvested_at"], source


# ---- live: MCP server returns exactly what the client returns ----------------------

MCP_CASES = {
    "count_facilities": ({"city": "Berlin"}, "count_facilities", ("Berlin",)),
    "search_facilities": ({"city": "Berlin"}, "search_facilities", ("Berlin",)),
    "get_facility_details": ({"name": "Drehscheibe"}, "get_facility_details", ("Drehscheibe",)),
    "search_services": ({"query": "Suchtberatung"}, "search_services", ("Suchtberatung",)),
    "find_social_help": ({"need": "debt", "limit": 3}, "find_help", ("debt", 3)),
    "data_quality_report": ({"organisation": ""}, "data_quality_report", ("",)),
    "detect_duplicates": ({}, "detect_duplicates", ()),
    "check_completeness": ({}, "check_completeness", ()),
    "generate_statistics": ({}, "generate_statistics", ()),
    "compare_facilities": (
        {"facility_1": "Drehscheibe", "facility_2": "Metropolitankapitel"},
        "compare_facilities", ("Drehscheibe", "Metropolitankapitel"),
    ),
    "find_nearby": (
        {"location": "10115", "radius_km": 1, "query": "Beratung"},
        "find_nearby", ("10115", 1.0, "Beratung"),
    ),
    "get_awo_details": (
        {"iri": KNOWN_FACILITY_IRI}, "get_awo_details", (KNOWN_FACILITY_IRI,),
    ),
    "get_entity_card": (
        {"iri": KNOWN_FACILITY_IRI}, "get_entity_card", (KNOWN_FACILITY_IRI,),
    ),
    "search_service_candidates": (
        {"query": "Schuldner", "limit": 5}, "search_service_candidates", ("Schuldner", 5),
    ),
    "search_directory": (
        {"query": "Schuldner Kreuzberg"}, "search_directory", ("Schuldner Kreuzberg",),
    ),
    "count_facilities_per_postal_code": (
        {"postal_code_prefix": "10", "order": "lowest_per_10k", "limit": 5},
        "count_facilities_per_postal_code", ("10", "lowest_per_10k", 5),
    ),
    "facilities_per_inhabitant_by_region": (
        {"level": "region", "order": "lowest_per_10k", "postal_code_prefix": "", "limit": 5},
        "facilities_per_inhabitant_by_region", ("region", "lowest_per_10k", "", 5, 10_000),
    ),
    "explain_provenance": ({"facility": DREHSCHEIBE}, "explain_provenance", (DREHSCHEIBE,)),
    "list_sources": ({}, "list_sources", ()),
}


@pytest.mark.parametrize("tool", sorted(MCP_CASES))
def test_mcp_tool_returns_client_result(client, tool):
    arguments, method, args = MCP_CASES[tool]
    is_error, text = mcp_call_once(tool, arguments)
    assert not is_error, f"{tool}({arguments}) returned an MCP error: {text[:300]}"
    assert json.loads(text) == getattr(client, method)(*args), (
        f"{tool}({arguments}) over MCP differs from SparqlClient.{method}{args}"
    )



# ---- find_nearby / get_awo_details --------------------------------------------

def expected_nearby(client: SparqlClient, latitude: float, longitude: float, radius_km: float) -> set:
    """Facilities within radius_km: a plain coordinate query, distances in Python."""
    from src.sparql_client import haversine_km

    delta = radius_km / 100  # generous: 1/100 degree is >= 0.7 km at German latitudes
    query = f"""
        PREFIX schema: <http://schema.org/>
        PREFIX org: <http://www.w3.org/ns/org#>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT DISTINCT ?facility ?lat ?lon WHERE {{
            ?facility a org:OrganizationalUnit ; schema:address ?a .
            ?a a schema:PostalAddress ; schema:latitude ?lat ; schema:longitude ?lon .
            FILTER(xsd:double(STR(?lat)) > {latitude - delta * 2}
                   && xsd:double(STR(?lat)) < {latitude + delta * 2}
                   && xsd:double(STR(?lon)) > {longitude - delta * 3}
                   && xsd:double(STR(?lon)) < {longitude + delta * 3})
        }}"""
    return {
        row["facility"]["value"]
        for row in client.query(query)["results"]["bindings"]
        if haversine_km(latitude, longitude, float(row["lat"]["value"]), float(row["lon"]["value"]))
        <= radius_km
    }


def test_postal_code_origin_is_where_the_postal_code_is(client):
    origin = client.resolve_location("10115")
    assert origin and origin["matched_by"] == "postal_code", f"10115 did not resolve: {origin}"
    assert 52.4 < origin["latitude"] < 52.6 and 13.3 < origin["longitude"] < 13.5, (
        f"10115 (Berlin-Mitte) resolved to {origin}, outside central Berlin"
    )


@pytest.mark.parametrize("location, radius_km", [("10115", 1.0), ("48.1374,11.5755", 1.5)])
def test_find_nearby_matches_independent_distance_check(client, location, radius_km):
    result = client.find_nearby(location, radius_km=radius_km, limit=50)
    origin = result["origin"]
    expected = expected_nearby(client, origin["latitude"], origin["longitude"], radius_km)
    assert expected, f"reference query found no facilities near {location}; endpoint data changed?"
    assert result["total_found"] == len(expected), (
        f"find_nearby({location!r}, {radius_km}) found {result['total_found']}, "
        f"independent check found {len(expected)}"
    )
    found = [item["iri"] for item in result["results"]]
    assert set(found) <= expected, f"results outside the radius: {sorted(set(found) - expected)[:5]}"
    distances = [item["distance_km"] for item in result["results"]]
    assert distances == sorted(distances), f"results not nearest first: {distances[:10]}"


def test_find_nearby_query_only_narrows(client):
    everything = client.find_nearby("10115", radius_km=1, limit=50)
    sucht = client.find_nearby("10115", radius_km=1, query="Sucht", limit=50)
    assert 0 < sucht["total_found"] <= everything["total_found"], (
        f"query='Sucht' found {sucht['total_found']} of {everything['total_found']} near 10115"
    )


def test_get_awo_details_known_facility(client):
    details = client.get_awo_details(KNOWN_FACILITY_IRI)
    assert details and details["name"] == "vista WIGWAM Connect", f"got {pretty_details(details)}"
    assert details["address"] and "Berlin" in details["address"], details["address"]
    assert details["map_url"].startswith("https://www.google.com/maps/"), details["map_url"]


def test_get_awo_details_agrees_with_find_nearby(client):
    nearest = client.find_nearby("10115", radius_km=1, limit=1)["results"][0]
    details = client.get_awo_details(nearest["iri"])
    assert details and details["name"] == nearest["name"], (
        f"get_awo_details({nearest['iri']}) name {details and details['name']!r} != {nearest['name']!r}"
    )
    assert details["address"] == nearest["address"]


def test_get_awo_details_unknown_iri(client):
    assert client.get_awo_details(KNOWN_FACILITY_IRI + "-does-not-exist") is None


# ---- get_entity_card / search_service_candidates / search_directory ---------------

def test_entity_card_agrees_with_get_awo_details(client):
    card = client.get_entity_card(KNOWN_FACILITY_IRI)
    details = client.get_awo_details(KNOWN_FACILITY_IRI)
    assert card["status"] == "ok" and card["entity_type"] == "facility"
    assert card["fields"]["name"] == details["name"]
    assert card["fields"]["opening_hours"] == details["opening_hours"]
    street = card["relationships"]["address"]["entity"]["fields"]["street"]
    assert street and street in details["address"], f"card street {street!r} not in {details['address']!r}"


def test_service_candidates_total_equals_search_services(client):
    candidates = client.search_service_candidates("Schuldner", limit=25)
    services = client.search_services("Schuldner")
    assert candidates["total"] == len(services) > 0, (
        f"search_service_candidates total {candidates['total']} != search_services {len(services)}"
    )
    assert {c["service_id"] for c in candidates["candidates"]} <= {s["iri"] for s in services}


def test_service_candidates_follow_links_to_existing_offices(client):
    page = client.search_service_candidates("Suchtberatung", limit=10)["candidates"]
    resolved = [c for c in page if c["provider"]["status"] == "resolved"]
    assert resolved, "no service in the first page links to a facility or provider"
    card = client.get_entity_card(resolved[0]["provider"]["id"])
    assert card["status"] == "ok" and card["entity_type"] == resolved[0]["provider"]["type"]


def test_directory_telephone_filter_partitions_the_matches(client):
    everything = client.search_directory("Schuldner Berlin")
    with_phone = client.search_directory("Schuldner Berlin", has_telephone=True)
    without = client.search_directory("Schuldner Berlin", has_telephone=False)
    assert everything["total"] > 0
    assert with_phone["total"] + without["total"] == everything["total"]
    assert everything["missing_counts"]["telephone"] == without["total"]
    assert sum(p["count"] for p in everything["by_provider"]) == everything["total"]


def test_directory_more_terms_only_narrow(client):
    broad = client.search_directory("Schuldner")
    narrow = client.search_directory("Schuldner Kreuzberg")
    assert 0 < narrow["total"] <= broad["total"]
    for hit in narrow["hits"]:
        terms = {t for m in hit["matched_fields"] for t in m["matched_terms"]}
        assert terms == {"schuldner", "kreuzberg"}, f"{hit['name']} matched only {terms}"


def pretty_details(details):
    return json.dumps(details, ensure_ascii=False)[:300] if details else details
