"""End-to-end tests: two per feature (each MCP tool and the find_help prompt).

Every test calls the real server.py over MCP; the server runs its real .rq queries on
data/directory.ttl (conftest.py). Expected values are read directly from the graph's
triples with rdflib, not through the queries or SparqlClient, so a wrong query, a wrong
result shape or broken MCP plumbing all show up here.
"""

import csv
import json
import re
import urllib.parse
from collections import Counter
from contextlib import contextmanager

import pytest
from rdflib import Namespace, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.namespace import RDF, RDFS

from conftest import DATA_DIR
from src.fix_first import decode_source_link
from src.quality_report import UNKNOWN_ORIGIN
from src.sparql_client import DATASETS, HELP_NEEDS, expand, haversine_km
from tests.helpers import (
    LocalSparqlClient, load_dataset, load_graph, mcp_call, mcp_call_once, mcp_get_prompt,
    mcp_list_prompts, mcp_list_tools,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

SCHEMA = Namespace("http://schema.org/")
ORG = Namespace("http://www.w3.org/ns/org#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
CDF = "https://civic-data.de/federated-directory#"


# ---- reading the expected values straight from the graph ---------------------------

@pytest.fixture(scope="module")
def graph():
    return load_graph()


def value(graph, subject, predicate):
    found = graph.value(subject, predicate)
    return None if found is None else str(found)


def fold(text):
    """Case-insensitive like the queries: ß and ss match."""
    return (text or "").casefold().replace("ß", "ss")


def facilities(graph):
    return set(graph.subjects(RDF.type, ORG.OrganizationalUnit))


def address_of(graph, facility):
    """The facility's address, if it links to an existing PostalAddress."""
    for address in graph.objects(facility, SCHEMA.address):
        if (address, RDF.type, SCHEMA.PostalAddress) in graph:
            return address
    return None


def facilities_where(graph, address_predicate, wanted):
    return {
        str(facility)
        for facility in facilities(graph)
        if (address := address_of(graph, facility)) is not None
        and wanted(value(graph, address, address_predicate))
    }


def facilities_in_city(graph, city):
    return facilities_where(graph, SCHEMA.addressLocality, lambda found: fold(found) == fold(city))


def call_json(tool, arguments=None, once=True):
    is_error, text = (mcp_call_once if once else mcp_call)(tool, arguments or {})
    assert not is_error, f"{tool}({arguments}) returned an MCP error: {text[:300]}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pytest.fail(f"{tool}({arguments}) did not return JSON: {text[:300]!r}")


@contextmanager
def serving(turtle):
    """Let the server answer from a small Turtle snippet instead of directory.ttl."""
    import server

    previous = server.sparql
    server.sparql = LocalSparqlClient(load_graph(turtle))
    try:
        yield
    finally:
        server.sparql = previous


@contextmanager
def serving_named_graphs(trig):
    """Like serving, for a TriG snippet with named graphs (the provenance tools read them)."""
    import server

    previous = server.sparql
    server.sparql = LocalSparqlClient(load_dataset(trig))
    try:
        yield
    finally:
        server.sparql = previous


E2E_PER_FEATURE = 2


def test_every_feature_has_two_e2e_tests():
    features = [t.name for t in mcp_list_tools()] + [p.name for p in mcp_list_prompts()]
    tests = [name for name in globals() if name.startswith("test_e2e_")]
    counts = {f: sum(name.startswith(f"test_e2e_{f}_") for name in tests) for f in features}
    short = {feature: count for feature, count in counts.items() if count < E2E_PER_FEATURE}
    assert not short, (
        f"feature(s) with fewer than {E2E_PER_FEATURE} end-to-end tests: {short}. "
        "Add tests named test_e2e_<feature>_... to tests/protocol/test_tools_e2e.py."
    )


# ---- count_facilities ------------------------------------------------------------

def test_e2e_count_facilities_matches_graph(graph):
    expected = len(facilities_in_city(graph, "Berlin"))
    is_error, text = mcp_call_once("count_facilities", {"city": "Berlin"})
    assert not is_error and int(text) == expected > 0, (
        f"count_facilities(Berlin) returned {text!r}, the graph has {expected} facilities "
        "with an address in Berlin"
    )


def test_e2e_count_facilities_is_case_insensitive_and_exact(graph):
    counts = {city: int(mcp_call("count_facilities", {"city": city})[1])
              for city in ["BERLIN", "berlin-mitte", "Atlantis"]}
    expected = {
        "BERLIN": len(facilities_in_city(graph, "Berlin")),
        "berlin-mitte": len(facilities_in_city(graph, "Berlin-Mitte")),
        "Atlantis": 0,
    }
    assert counts == expected, (
        f"count_facilities per city {counts}, expected {expected} "
        "(case-insensitive, exact city: Berlin-Mitte is not part of Berlin)"
    )


# ---- search_facilities -----------------------------------------------------------

def test_e2e_search_facilities_returns_exactly_the_city_facilities(graph):
    results = call_json("search_facilities", {"city": "Berlin"})
    found = [result["iri"] for result in results]
    expected = facilities_in_city(graph, "Berlin")
    assert len(found) == len(set(found)), "search_facilities(Berlin) lists a facility twice"
    assert set(found) == expected, (
        f"search_facilities(Berlin): missing {sorted(expected - set(found))[:3]}, "
        f"unexpected {sorted(set(found) - expected)[:3]}"
    )


def test_e2e_search_facilities_fields_come_from_the_graph(graph):
    for result in call_json("search_facilities", {"city": "Berlin-Mitte"}):
        facility = URIRef(result["iri"])
        address = address_of(graph, facility)
        expected = {
            "name": value(graph, facility, SCHEMA.name),
            "street": value(graph, address, SCHEMA.streetAddress),
            "postal_code": value(graph, address, SCHEMA.postalCode),
            "city": value(graph, address, SCHEMA.addressLocality),
            "telephone": value(graph, facility, SCHEMA.telephone),
            "email": value(graph, facility, SCHEMA.email),
            "homepage": value(graph, facility, FOAF.homepage),
        }
        actual = {key: result[key] for key in expected}
        assert actual == expected, f"search_facilities(Berlin-Mitte) row for {facility}"


# ---- get_facility_details --------------------------------------------------------

def test_e2e_get_facility_details_partial_name_finds_every_match(graph):
    results = call_json("get_facility_details", {"name": "BERATUNG"}, once=False)
    expected = {
        str(facility) for facility in facilities(graph)
        if "beratung" in fold(value(graph, facility, SCHEMA.name))
    }
    found = {result["iri"] for result in results}
    assert found == expected and expected, (
        f"get_facility_details(BERATUNG): missing {sorted(expected - found)[:3]}, "
        f"unexpected {sorted(found - expected)[:3]}"
    )


def test_e2e_get_facility_details_includes_address_and_provider(graph):
    (result,) = call_json("get_facility_details", {"name": "Drehscheibe"})
    facility = URIRef(result["iri"])
    provider = graph.value(facility, SCHEMA.parentOrganization)
    address = address_of(graph, facility)
    assert result["address"]["postal_code"] == value(graph, address, SCHEMA.postalCode)
    assert result["address"]["city"] == value(graph, address, SCHEMA.addressLocality)
    assert result["provider"] == {
        "iri": str(provider),
        "name": value(graph, provider, SCHEMA.name),
        "homepage": value(graph, provider, FOAF.homepage),
    }, f"provider of {facility} differs from its schema:parentOrganization in the graph"


# ---- compare_facilities ----------------------------------------------------------

def test_e2e_compare_facilities_equals_two_detail_lookups():
    result = call_json("compare_facilities",
                       {"facility_1": "Drehscheibe", "facility_2": "Metropolitankapitel"})
    expected = [
        {"search_name": name, "matches": call_json("get_facility_details", {"name": name})}
        for name in ["Drehscheibe", "Metropolitankapitel"]
    ]
    assert result == expected, "compare_facilities differs from two get_facility_details calls"
    assert all(side["matches"] for side in result), "both sides should find a facility"


def test_e2e_compare_facilities_keeps_order_and_reports_unknown_as_empty():
    result = call_json("compare_facilities",
                       {"facility_1": "Gibt es nicht", "facility_2": "Drehscheibe"}, once=False)
    assert [side["search_name"] for side in result] == ["Gibt es nicht", "Drehscheibe"]
    assert result[0]["matches"] == [], f"unknown facility should match nothing: {result[0]}"
    assert result[1]["matches"], "Drehscheibe should still be found as the second facility"


# ---- search_services -------------------------------------------------------------

def test_e2e_search_services_matches_name_category_or_audience(graph):
    # "Medikament" often appears only in the audience, so all three fields are needed.
    results = call_json("search_services", {"query": "MEDIKAMENT"}, once=False)

    def matches(service, *predicates):
        return any("medikament" in fold(value(graph, service, p)) for p in predicates)

    services = set(graph.subjects(RDF.type, SCHEMA.Service))
    expected = {str(s) for s in services if matches(s, SCHEMA.name, SCHEMA.category, SCHEMA.audience)}
    audience_only = {str(s) for s in services
                     if matches(s, SCHEMA.audience) and not matches(s, SCHEMA.name, SCHEMA.category)}
    assert audience_only, "test data should contain services matching only by audience"
    found = [result["iri"] for result in results]
    assert len(found) == len(set(found)), "search_services(MEDIKAMENT) lists a service twice"
    assert set(found) == expected, (
        f"search_services(MEDIKAMENT): missing {sorted(expected - set(found))[:3]}, "
        f"unexpected {sorted(set(found) - expected)[:3]}"
    )


def test_e2e_search_services_no_match_and_quotes_stay_literal():
    for query in ["Gibt es nicht", '") || true || ("']:
        assert call_json("search_services", {"query": query}, once=False) == [], (
            f"search_services({query!r}) should find nothing"
        )


# ---- find_social_help ------------------------------------------------------------

def help_services(graph, need):
    terms = HELP_NEEDS[need]
    return {
        str(service) for service in graph.subjects(RDF.type, SCHEMA.Service)
        if any(term in fold(f"{value(graph, service, SCHEMA.name) or ''} "
                            f"{value(graph, service, SCHEMA.category) or ''}") for term in terms)
    }


def test_e2e_find_social_help_matches_the_need_with_phones_first(graph):
    result = call_json("find_social_help", {"need": "Addiction", "limit": 10}, once=False)
    expected = help_services(graph, "addiction")
    assert result["need"] == "addiction"
    assert result["total_matches"] == len(expected) > 0, (
        f"total_matches {result['total_matches']}, the graph has {len(expected)} services "
        f"whose name or category contains one of {HELP_NEEDS['addiction']}"
    )
    assert result["showing"] == len(result["results"]) == min(10, len(expected))
    for item in result["results"]:
        text = fold(f"{item['service']} {item['category']}")
        assert any(term in text for term in HELP_NEEDS["addiction"]), f"off-topic result: {item}"
    has_phone = [item["telephone"] is not None for item in result["results"]]
    assert has_phone == sorted(has_phone, reverse=True), "results with a phone number come first"


def test_e2e_find_social_help_postal_prefix_and_unknown_need():
    everywhere = call_json("find_social_help", {"need": "debt", "limit": 10}, once=False)
    near = call_json("find_social_help",
                     {"need": "debt", "limit": 10, "postal_code_prefix": "109"}, once=False)
    assert 0 < near["total_matches"] < everywhere["total_matches"], (
        f"prefix 109 should narrow debt help: {near['total_matches']} of "
        f"{everywhere['total_matches']}"
    )
    assert all(item["postal_code"].startswith("109") for item in near["results"]), near

    unknown = call_json("find_social_help", {"need": "pizza"}, once=False)
    assert "error" in unknown and unknown["valid_needs"] == list(HELP_NEEDS), unknown


# ---- data_quality_report ---------------------------------------------------------

def graph_origins(graph):
    """origin -> facilities, read from the triples: provider name, else source website, else unknown."""
    origins = {}
    for facility in facilities(graph):
        name = None
        for provider in graph.objects(facility, SCHEMA.parentOrganization):
            if (provider, RDF.type, SCHEMA.Organization) in graph and value(graph, provider, SCHEMA.name):
                name = value(graph, provider, SCHEMA.name).strip()
        if name is None:
            page = decode_source_link(value(graph, facility, SCHEMA.mainEntityOfPage))
            name = urllib.parse.urlparse(page).netloc if page else ""
        origins.setdefault(name or UNKNOWN_ORIGIN, set()).add(facility)
    return origins


def test_e2e_data_quality_report_overview_counts_every_facility_once(graph):
    result = call_json("data_quality_report", {}, once=False)
    origins = graph_origins(graph)
    assert result["total_facilities"] == len(facilities(graph))
    assert result["organisations"] == len(origins)
    shown = {item["organisation"]: item["facilities"] for item in result["overview"]}
    for name, count in shown.items():
        assert count == len(origins[name]), f"{name}: {count} facilities, the graph has {len(origins[name])}"
    sizes = [item["facilities"] for item in result["overview"]]
    assert sizes == sorted(sizes, reverse=True), "largest organisation first"
    assert all(0 <= item["score"] <= 100 for item in result["overview"])
    if len(origins) <= 10:
        assert sum(shown.values()) == len(facilities(graph)), "no facility lost or counted twice"


def test_e2e_data_quality_report_one_organisation_matches_graph_and_unknown_name_errors(graph):
    name, members = max(
        ((n, f) for n, f in graph_origins(graph).items() if n != UNKNOWN_ORIGIN), key=lambda item: len(item[1])
    )
    result = call_json("data_quality_report", {"organisation": name}, once=False)
    assert result["organisation"] == name and result["facilities"] == len(members)
    for field, predicate in [("telephone", SCHEMA.telephone), ("email", SCHEMA.email)]:
        missing = sum(1 for f in members if not any(str(v).strip() for v in graph.objects(f, predicate)))
        assert result["completeness"][field]["missing"] == missing, (
            f"{name}.{field}: {result['completeness'][field]['missing']} missing, the graph has {missing}"
        )
    assert result["draft_message"].startswith((f"Hello {name},", f"Hello team at {name},"))

    unknown = call_json("data_quality_report", {"organisation": "Atlantis-Gibtsnicht"}, once=False)
    assert "error" in unknown and unknown["known_organisations"], unknown


# ---- generate_statistics ---------------------------------------------------------

def test_e2e_generate_statistics_dataset_sizes_match_graph(graph):
    statistics = call_json("generate_statistics")
    expected = {
        name: len(set(graph.subjects(RDF.type, URIRef(expand(dataset["class"])))))
        for name, dataset in DATASETS.items()
    }
    assert statistics["dataset_sizes"] == expected, "dataset sizes differ from the graph"


def test_e2e_generate_statistics_city_counts_agree_with_count_facilities():
    by_city = call_json("generate_statistics")["facilities_by_city"]
    assert by_city, "facilities_by_city is empty"
    counts = {city: int(mcp_call_once("count_facilities", {"city": city})[1]) for city in by_city}
    assert counts == by_city, "facilities_by_city disagrees with count_facilities"


# ---- check_completeness ----------------------------------------------------------

def test_e2e_check_completeness_rows_match_dataset_sizes():
    completeness = call_json("check_completeness")
    sizes = call_json("generate_statistics")["dataset_sizes"]
    assert {name: report["rows"] for name, report in completeness.items()} == sizes
    for name, report in completeness.items():
        assert report["columns_detail"]["iri"]["missing"] == 0, f"{name}: an IRI is missing"
        assert report["missing_cells"] == sum(
            column["missing"] for column in report["columns_detail"].values()
        ), f"{name}: missing_cells is not the sum of its columns"


def test_e2e_check_completeness_missing_counts_match_graph(graph):
    report = call_json("check_completeness")["einrichtung"]["columns_detail"]
    for column in DATASETS["einrichtung"]["columns"][1:]:
        predicate = URIRef(expand(column))
        missing = sum(
            1 for facility in facilities(graph)
            if not any(str(v).strip() for v in graph.objects(facility, predicate))
        )
        assert report[column]["missing"] == missing, (
            f"einrichtung.{column}: {report[column]['missing']} missing, "
            f"the graph has {missing} facilities without a (non-blank) value"
        )


# ---- detect_duplicates -----------------------------------------------------------

def test_e2e_detect_duplicates_directory_is_clean():
    assert call_json("detect_duplicates") == {}, (
        "directory.ttl has no entries that are identical apart from their IRI"
    )


def test_e2e_detect_duplicates_finds_injected_duplicate():
    turtle = """
    cdf:org-a a org:OrganizationalUnit ; schema:name "Tafel" ; schema:telephone "030 1" .
    cdf:org-b a org:OrganizationalUnit ; schema:name "Tafel" ; schema:telephone "030 1" .
    cdf:org-c a org:OrganizationalUnit ; schema:name "Tafel" ; schema:telephone "030 2" .
    """
    with serving(turtle):
        result = call_json("detect_duplicates", once=False)
    assert list(result) == ["einrichtung"], f"only facilities have duplicates: {result}"
    (group,) = result["einrichtung"]
    assert group["occurrences"] == 2 and sorted(group["iris"]) == [CDF + "org-a", CDF + "org-b"]
    assert group["row"]["schema:name"] == "Tafel" and group["row"]["schema:telephone"] == "030 1"


# ---- list_providers --------------------------------------------------------------

def test_e2e_list_providers_lists_every_provider(graph):
    providers = call_json("list_providers")
    expected = {str(p) for p in graph.subjects(RDF.type, SCHEMA.Organization)
                if graph.value(p, SCHEMA.name) is not None}
    assert {provider["iri"] for provider in providers} == expected and expected
    names = [provider["name"] for provider in providers]
    assert names == sorted(names), f"providers should be sorted by name: {names}"


def test_e2e_list_providers_homepage_and_address_come_from_the_graph(graph):
    for provider in call_json("list_providers"):
        iri = URIRef(provider["iri"])
        address = graph.value(iri, SCHEMA.address)
        assert provider["homepage"] == value(graph, iri, FOAF.homepage), provider["iri"]
        assert provider["address"] == {
            "street": value(graph, address, SCHEMA.streetAddress),
            "postal_code": value(graph, address, SCHEMA.postalCode),
            "city": value(graph, address, SCHEMA.addressLocality),
        }, f"address of provider {iri}"


# ---- search_by_postal_code -------------------------------------------------------

def test_e2e_search_by_postal_code_returns_exactly_that_area(graph):
    results = call_json("search_by_postal_code", {"postal_code": "10115"})
    expected = facilities_where(graph, SCHEMA.postalCode, lambda found: found == "10115")
    assert {result["iri"] for result in results} == expected and expected
    assert {result["postal_code"] for result in results} == {"10115"}


def test_e2e_search_by_postal_code_partial_or_unknown_finds_nothing():
    for postal_code in ["1011", "101150", "99999", '10115" || "1']:
        assert call_json("search_by_postal_code", {"postal_code": postal_code}, once=False) == [], (
            f"search_by_postal_code({postal_code!r}) should find nothing (exact five digits)"
        )


# ---- count_facilities_per_postal_code --------------------------------------------

def census_inhabitants():
    with open(DATA_DIR / "einwohner_plz_de.csv", newline="", encoding="utf-8") as f:
        return {row["plz"]: int(row["population"]) for row in csv.DictReader(f)}


def test_e2e_count_facilities_per_postal_code_counts_match_graph_and_census(graph):
    result = call_json("count_facilities_per_postal_code", {"postal_code_prefix": "10", "limit": 100})
    inhabitants = census_inhabitants()
    in_prefix = facilities_where(graph, SCHEMA.postalCode, lambda found: (found or "").strip().startswith("10"))
    assert result["facilities"] == len(in_prefix) > 0
    assert result["showing"] == len(result["results"]) == result["postal_codes"], (
        "limit 100 should show every postal code starting with 10"
    )
    for row in result["results"]:
        code = row["postal_code"]
        expected = facilities_where(graph, SCHEMA.postalCode, lambda found: (found or "").strip() == code)
        assert row["facilities"] == len(expected), f"facility count of {code}"
        assert row["inhabitants"] == inhabitants.get(code), f"inhabitants of {code}"
    counts = [row["facilities"] for row in result["results"]]
    assert counts == sorted(counts, reverse=True), "most_facilities should sort by count, descending"
    assert result["postal_codes_without_facilities"] == counts.count(0) > 0


def test_e2e_count_facilities_per_postal_code_density_orders_and_unknown_order():
    lowest = call_json("count_facilities_per_postal_code", {"postal_code_prefix": "10", "order": "lowest_per_10k", "limit": 100})
    highest = call_json("count_facilities_per_postal_code", {"postal_code_prefix": "10", "order": "highest_per_10k", "limit": 100})
    densities = [row["facilities_per_10k_inhabitants"] for row in lowest["results"]]
    assert densities == sorted(densities) and densities[0] == 0
    assert [row["facilities_per_10k_inhabitants"] for row in highest["results"]] == sorted(densities, reverse=True)
    for row in highest["results"]:
        assert row["facilities_per_10k_inhabitants"] == round(row["facilities"] / row["inhabitants"] * 10_000, 2)
    unknown = call_json("count_facilities_per_postal_code", {"order": "pizza"}, once=False)
    assert "error" in unknown and "lowest_per_10k" in unknown["valid_orders"]


# ---- facilities_per_inhabitant_by_region -----------------------------------------

def graph_regions(graph, digits):
    """Region -> (facilities, Counter of towns), read from the triples."""
    regions = {}
    for facility in facilities(graph):
        address = address_of(graph, facility)
        code = (value(graph, address, SCHEMA.postalCode) or "").strip() if address is not None else ""
        if re.fullmatch(r"\d{5}", code):
            found, towns = regions.setdefault(code[:digits], (set(), Counter()))
            found.add(facility)
            if town := (value(graph, address, SCHEMA.addressLocality) or "").strip():
                towns[town] += 1
    return {region: (len(found), towns) for region, (found, towns) in regions.items()}


def test_e2e_facilities_per_inhabitant_by_region_counts_match_graph_and_census(graph):
    result = call_json(
        "facilities_per_inhabitant_by_region",
        {"level": "region", "order": "most_facilities", "postal_code_prefix": "1", "limit": 100},
    )
    inhabitants = census_inhabitants()
    expected = graph_regions(graph, 2)
    assert result["facilities"] == sum(count for count, _ in expected.values()) > 0
    assert result["inhabitants"] == sum(n for code, n in inhabitants.items() if code.startswith("1"))
    # 11 holds only Berlin's 11011 (18,996 inhabitants), above the 10,000 default.
    assert result["regions_below_min_inhabitants"] == 0
    assert "11" in [row["postal_code_prefix"] for row in result["results"]]
    assert result["showing"] == len(result["results"]) == result["regions"]
    for row in result["results"]:
        region = row["postal_code_prefix"]
        count, towns = expected.get(region, (0, Counter()))
        assert row["facilities"] == count, f"facility count of {region}"
        assert row["inhabitants"] == sum(n for code, n in inhabitants.items() if code.startswith(region))
        assert row["places"] == [town for town, _ in sorted(towns.items(), key=lambda t: (-t[1], t[0]))][:3]
    counts = [row["facilities"] for row in result["results"]]
    assert counts == sorted(counts, reverse=True), "most_facilities should sort by count, descending"


def test_e2e_facilities_per_inhabitant_by_region_density_and_bad_input():
    arguments = {"level": "region", "postal_code_prefix": "1", "limit": 100}
    lowest = call_json("facilities_per_inhabitant_by_region", arguments)
    highest = call_json("facilities_per_inhabitant_by_region", {**arguments, "order": "highest_per_10k"})
    densities = [row["facilities_per_10k_inhabitants"] for row in lowest["results"]]
    assert lowest["order"] == "lowest_per_10k" and densities == sorted(densities) and densities[0] == 0
    assert [row["facilities_per_10k_inhabitants"] for row in highest["results"]] == sorted(densities, reverse=True)
    average = lowest["facilities_per_10k_inhabitants"]
    assert average == round(lowest["facilities"] / lowest["inhabitants"] * 10_000, 2)
    for row in highest["results"]:
        assert row["facilities_per_10k_inhabitants"] == round(row["facilities"] / row["inhabitants"] * 10_000, 2)
        assert row["relative_to_average"] == round(row["facilities_per_10k_inhabitants"] / average, 2)
        if row["facilities"]:
            assert row["inhabitants_per_facility"] == round(row["inhabitants"] / row["facilities"])
    # The areas 175 (6,969 inhabitants) and 186 (7,687) are below the 10,000 default.
    areas = {"level": "area", "postal_code_prefix": "1", "limit": 100}
    default = call_json("facilities_per_inhabitant_by_region", areas, once=False)
    everything = call_json("facilities_per_inhabitant_by_region", {**areas, "min_inhabitants": 0}, once=False)
    assert default["regions_below_min_inhabitants"] == 2 and everything["regions_below_min_inhabitants"] == 0
    assert {"175", "186"} & {row["postal_code_prefix"] for row in default["results"]} == set()
    assert {"175", "186"} <= {row["postal_code_prefix"] for row in everything["results"]}
    for bad, key in [({"level": "state"}, "valid_levels"), ({"order": "pizza"}, "valid_orders")]:
        assert key in call_json("facilities_per_inhabitant_by_region", bad, once=False)
    too_long = call_json("facilities_per_inhabitant_by_region", {"level": "region", "postal_code_prefix": "48"}, once=False)
    assert "error" in too_long and too_long["valid_levels"] == ["area"]
    assert "error" in call_json("facilities_per_inhabitant_by_region", {"postal_code_prefix": "Berlin"}, once=False)


# ---- records_to_fix_first --------------------------------------------------------

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def test_e2e_records_to_fix_first_ranks_only_the_city(graph):
    result = call_json("records_to_fix_first", {"city": "Berlin", "n": 200}, once=False)
    summary, results = result["summary"], result["results"]
    total = len(facilities_in_city(graph, "Berlin"))
    assert summary["total_facilities"] == total == len(results), (
        f"summary says {summary['total_facilities']}, returned {len(results)}, "
        f"the graph has {total} Berlin facilities"
    )
    assert {r["iri"] for r in results} == facilities_in_city(graph, "Berlin")
    assert sum(summary["by_bucket"].values()) == sum(summary["by_priority"].values()) == total
    assert [r["rank"] for r in results] == list(range(1, total + 1))
    tiers = [PRIORITY_ORDER[r["priority"]] for r in results]
    assert tiers == sorted(tiers), "results must be ordered high → medium → low"


def test_e2e_records_to_fix_first_bucket_filter_and_provider_batches():
    everything = call_json("records_to_fix_first", {"city": "Berlin", "n": 3})
    expected = everything["summary"]["by_bucket"]["scope_check"]

    only = call_json("records_to_fix_first",
                     {"city": "Berlin", "bucket": "scope_check", "n": 200}, once=False)
    assert {r["bucket"] for r in only["results"]} == {"scope_check"}
    assert len(only["results"]) == only["summary"]["matching_filter"] == expected

    batches = call_json("records_to_fix_first", {"city": "Berlin", "group_by": "provider"},
                        once=False)
    assert "results" not in batches and batches["batches"], list(batches)
    covered = sum(batch["facilities"] for batch in batches["batches"])
    total = batches["summary"]["total_facilities"]
    assert covered == total - batches["excluded_scope_check_or_junk"], (
        f"provider batches cover {covered} facilities, expected every Berlin facility "
        f"({total}) except the scope-check/junk ones ({batches['excluded_scope_check_or_junk']})"
    )


# ---- find_nearby -----------------------------------------------------------------

def test_e2e_find_nearby_matches_distances_from_the_graph(graph):
    result = call_json("find_nearby", {"location": "13353", "radius_km": 1, "limit": 50},
                       once=False)
    in_postal_code = [
        address for address in graph.subjects(SCHEMA.postalCode, None)
        if value(graph, address, SCHEMA.postalCode) == "13353"
        and graph.value(address, SCHEMA.latitude) is not None
    ]
    latitude = sum(float(graph.value(a, SCHEMA.latitude)) for a in in_postal_code) / len(in_postal_code)
    longitude = sum(float(graph.value(a, SCHEMA.longitude)) for a in in_postal_code) / len(in_postal_code)
    assert result["origin"]["latitude"] == pytest.approx(latitude)
    assert result["origin"]["longitude"] == pytest.approx(longitude)

    expected = {}
    for facility in facilities(graph):
        address = address_of(graph, facility)
        if address is None or graph.value(address, SCHEMA.latitude) is None:
            continue
        distance = haversine_km(latitude, longitude, float(graph.value(address, SCHEMA.latitude)),
                                float(graph.value(address, SCHEMA.longitude)))
        if distance <= 1:
            expected[str(facility)] = distance
    found = {item["iri"]: item["distance_km"] for item in result["results"]}
    assert set(found) == set(expected) and expected, (
        f"find_nearby(13353, 1 km): missing {sorted(set(expected) - set(found))[:3]}, "
        f"unexpected {sorted(set(found) - set(expected))[:3]}"
    )
    for iri, distance in found.items():
        assert distance == pytest.approx(expected[iri], abs=0.01), iri
    distances = [item["distance_km"] for item in result["results"]]
    assert distances == sorted(distances), "results must be nearest first"


def test_e2e_find_nearby_unresolvable_location_reports_error():
    is_error, text = mcp_call("find_nearby", {"location": "Müllerstraße"})
    assert not is_error, f"find_nearby('Müllerstraße') returned an MCP error: {text[:200]!r}"
    result = json.loads(text)
    assert result["results"] == [] and "error" in result, (
        f"find_nearby with a street name should return an 'error' and no results, got: {text[:200]!r}"
    )


# ---- get_awo_details -------------------------------------------------------------

def test_e2e_get_awo_details_known_facility_matches_graph(graph):
    iri = CDF + "org-229a4e0b1081"
    result = call_json("get_awo_details", {"iri": iri})
    facility = URIRef(iri)
    address = address_of(graph, facility)
    for key, predicate in [("name", SCHEMA.name), ("telephone", SCHEMA.telephone),
                           ("email", SCHEMA.email), ("opening_hours", SCHEMA.openingHours)]:
        assert result[key] == value(graph, facility, predicate), f"get_awo_details {key}"
    for part in (SCHEMA.streetAddress, SCHEMA.postalCode, SCHEMA.addressLocality):
        assert value(graph, address, part) in result["address"], result["address"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(result["map_url"]).query)["query"][0]
    assert [float(part) for part in query.split(",")] == [
        float(value(graph, address, SCHEMA.latitude)), float(value(graph, address, SCHEMA.longitude))
    ], f"map link should point at the address coordinates: {result['map_url']}"


def test_e2e_get_awo_details_unknown_iri_is_a_message_not_an_error():
    is_error, text = mcp_call("get_awo_details", {"iri": "https://civic-data.de/federated-directory#nope"})
    assert not is_error, f"get_awo_details(<unknown iri>) returned an MCP error: {text[:200]!r}"
    assert text.startswith("No facility found"), f"expected a 'No facility found' message, got: {text[:200]!r}"


# ---- get_entity_card -------------------------------------------------------------

def test_e2e_get_entity_card_fields_and_links_match_graph(graph):
    iri = CDF + "org-34e986356c61"  # Frauenladen - Drogennotdienst Berlin gGmbH
    card = call_json("get_entity_card", {"iri": iri})
    facility = URIRef(iri)
    assert card["status"] == "ok" and card["entity_type"] == "facility", card.get("status")
    predicates = {"name": SCHEMA.name, "telephone": SCHEMA.telephone, "email": SCHEMA.email,
                  "opening_hours": SCHEMA.openingHours, "accessibility": SCHEMA.accessibilitySummary,
                  "alternate_name": SCHEMA.alternateName, "description": SCHEMA.description}
    for key, predicate in predicates.items():
        assert card["fields"][key] == value(graph, facility, predicate), f"get_entity_card {key}"
        assert (key in card["missing_fields"]) == (value(graph, facility, predicate) is None), (
            f"'{key}' listed as missing although the graph has a value, or the other way round"
        )
    address = card["relationships"]["address"]
    assert address["status"] == "resolved" and address["target_id"] == str(address_of(graph, facility))
    assert address["entity"]["fields"]["street"] == value(graph, address_of(graph, facility), SCHEMA.streetAddress)
    services = {str(s) for s in graph.subjects(SCHEMA.provider, facility)}
    assert {s["id"] for s in card["linked_services"]} == services and card["linked_services_total"] == len(services)


def test_e2e_get_entity_card_service_resolves_to_its_office(graph):
    service = URIRef(CDF + "service-04b4dec19d58")
    card = call_json("get_entity_card", {"iri": str(service)})
    office = graph.value(service, SCHEMA.provider)
    provider = card["relationships"]["provider"]
    assert card["entity_type"] == "service" and provider["status"] == "resolved"
    assert provider["target_id"] == str(office) and provider["target_type"] == "facility"
    assert provider["entity"]["fields"]["name"] == value(graph, office, SCHEMA.name)
    assert card["fields"]["opening_hours"] == value(graph, service, SCHEMA.openingHours)
    assert call_json("get_entity_card", {"iri": CDF + "nope"}, once=False)["status"] == "not_found"
    assert call_json("get_entity_card", {"iri": 'x") } #'}, once=False)["status"] == "invalid_iri"


# ---- search_service_candidates ---------------------------------------------------

def services_matching(graph, text):
    return {
        str(service)
        for service in graph.subjects(RDF.type, SCHEMA.Service)
        if any(fold(text) in fold(str(found))
               for predicate in (SCHEMA.name, SCHEMA.category, SCHEMA.audience)
               for found in graph.objects(service, predicate))
    }


def test_e2e_search_service_candidates_total_and_offices_match_graph(graph):
    result = call_json("search_service_candidates", {"query": "Schuldner", "limit": 25})
    assert result["total"] == len(services_matching(graph, "schuldner")) > 0
    assert {c["service_id"] for c in result["candidates"]} == services_matching(graph, "schuldner")
    for candidate in result["candidates"]:
        office = graph.value(URIRef(candidate["service_id"]), SCHEMA.provider)
        assert candidate["provider"]["id"] == str(office), candidate["service_id"]
        assert candidate["provider"]["name"] == value(graph, office, SCHEMA.name)
        assert candidate["location"]["locality"] == value(graph, address_of(graph, office), SCHEMA.addressLocality)


def test_e2e_search_service_candidates_paging_covers_every_match(graph):
    expected = services_matching(graph, "beratung")
    seen, offset = [], 0
    while offset is not None:
        page = call_json("search_service_candidates", {"query": "Beratung", "limit": 7, "offset": offset})
        assert page["total"] == len(expected) and page["returned"] <= 7
        seen += [c["service_id"] for c in page["candidates"]]
        offset = page["next_offset"]
    assert sorted(seen) == sorted(expected), "pages overlap or miss services"


# ---- search_directory ------------------------------------------------------------

def directory_matches(graph, term):
    """Facilities with the term in their own text, their address or a linked service."""
    own = (SCHEMA.name, SCHEMA.alternateName, SCHEMA.description, SCHEMA.category, SCHEMA.availableService,
           SCHEMA.audience, SCHEMA.accessibilitySummary, SCHEMA.openingHours)
    address_fields = (SCHEMA.streetAddress, SCHEMA.postalCode, SCHEMA.addressLocality)
    service_fields = (SCHEMA.name, SCHEMA.category, SCHEMA.audience, SCHEMA.areaServed)

    def has(node, predicates):
        return any(fold(term) in fold(str(found)) for p in predicates for found in graph.objects(node, p))

    return {
        str(facility) for facility in facilities(graph)
        if has(facility, own)
        or any(has(address, address_fields) for address in graph.objects(facility, SCHEMA.address))
        or any(has(service, service_fields) for service in graph.subjects(SCHEMA.provider, facility))
    }


def test_e2e_search_directory_matches_facility_service_or_address_text(graph):
    result = call_json("search_directory", {"query": "Schuldner", "limit": 25})
    expected = directory_matches(graph, "schuldner")
    assert result["total"] == len(expected) and {h["iri"] for h in result["hits"]} == expected
    narrow = call_json("search_directory", {"query": "Schuldner Kreuzberg", "limit": 25})
    assert {h["iri"] for h in narrow["hits"]} == expected & directory_matches(graph, "kreuzberg")


def test_e2e_search_directory_telephone_filter_and_summaries_cover_all_matches(graph):
    expected = directory_matches(graph, "beratung")
    with_phone = {iri for iri in expected if value(graph, URIRef(iri), SCHEMA.telephone)}
    everything = call_json("search_directory", {"query": "Beratung", "limit": 3})
    assert everything["total"] == len(expected) and everything["returned"] == 3
    assert everything["missing_counts"]["telephone"] == len(expected - with_phone), (
        "missing_counts must cover all matches, not only the returned page"
    )
    filtered = call_json("search_directory", {"query": "Beratung", "has_telephone": True, "limit": 25})
    assert filtered["total"] == len(with_phone)
    assert all(hit["telephone"] for hit in filtered["hits"])
    short = call_json("search_directory", {"query": "ab"}, once=False)
    assert short["status"] == "error", "a query without a 3+ character term must be rejected"


# ---- explain_provenance and list_sources (named graphs) ------------------------------
#
# directory.ttl has no pipeline graphs, so these tests serve a small TriG snippet shaped
# like the endpoint's graphs and read the expected values straight from those graphs.

CDP = Namespace("https://civic-data.de/pipeline#")
MATCHES = URIRef("urn:directory:data/pipeline/matches.ttl")
MAPPED = URIRef("urn:directory:data/pipeline/mapped.ttl")
FEDERATION = URIRef("urn:directory:config/federation.ttl")

PIPELINE = """
cdf:org-p1 a org:OrganizationalUnit ;
    schema:name "Beratung Ost" ;
    schema:telephone "+49 30 1", "+49 30 2" ;
    schema:openingHours "Mo 9-12" .

GRAPH <urn:directory:data/pipeline/matches.ttl> {
    cdf:org-p1 cdp:hasMember cdp:a-1, cdp:b-1 .
}
GRAPH <urn:directory:data/pipeline/mapped.ttl> {
    cdp:a-1 cdp:fromSource cdp:aSource ; schema:name "Beratung Ost" ;
        schema:telephone "+49 30 1" ; schema:openingHours "Mo 9-17" .
    cdp:b-1 cdp:fromSource cdp:bSource ; schema:name "Beratung Ost" ;
        schema:telephone "+49 30 2" ; schema:openingHours "Mo 9-12" .
    cdp:b-2 cdp:fromSource cdp:bSource ; schema:name "Anderer Eintrag" .
}
GRAPH <urn:directory:config/federation.ttl> {
    cdp:aSource rdfs:label "Quelle A" ; cdp:coverageLabel "Berlin" .
    cdp:bSource rdfs:label "Quelle B" ; cdp:coverageLabel "Germany" .
}
GRAPH <urn:directory:data/ingest/ingest-log.ttl> {
    cdp:obs-a cdp:ofSource cdp:aSource ; prov:atTime "2026-10-06T05:00:00Z" .
    cdp:obs-b cdp:ofSource cdp:bSource ; prov:atTime "2026-10-06T06:00:00Z" .
}
"""


def supplied_by(dataset, entity, predicate):
    """value -> labels of the sources whose records (members of the entity) carry it."""
    matches, mapped, federation = (dataset.graph(g) for g in (MATCHES, MAPPED, FEDERATION))
    result = {}
    for record in matches.objects(URIRef(entity), CDP.hasMember):
        label = str(federation.value(mapped.value(record, CDP.fromSource), RDFS.label))
        for found in mapped.objects(record, predicate):
            result.setdefault(str(found), set()).add(label)
    return result


def test_e2e_explain_provenance_attributes_each_value_to_its_source_records():
    dataset = load_dataset(PIPELINE)
    directory = dataset.graph(DATASET_DEFAULT_GRAPH_ID)
    entity = CDF + "org-p1"
    with serving_named_graphs(PIPELINE):
        result = call_json("explain_provenance", {"facility": entity}, once=False)

    assert {s["record"] for s in result["sources"]} == {
        str(record) for record in dataset.graph(MATCHES).objects(URIRef(entity), CDP.hasMember)
    }
    fields = {f["field"]: f for f in result["fields"]}
    for name, predicate in [("telephone", SCHEMA.telephone), ("opening_hours", SCHEMA.openingHours)]:
        values = fields[name]["values"]
        assert {v["value"]: set(v["sources"]) for v in values} == supplied_by(dataset, entity, predicate), name
        assert {v["value"] for v in values if v["in_directory"]} == {
            str(found) for found in directory.objects(URIRef(entity), predicate)
        }, f"{name}: in_directory must match the finished directory"
        assert fields[name]["status"] == "sources_differ", fields[name]
    assert fields["name"]["status"] == "sources_agree", fields["name"]


def test_e2e_explain_provenance_finds_entries_by_name_and_rejects_foreign_iris(graph):
    name = "Drehscheibe Kinder - und Jugendpolitik"
    named = [str(f) for f in facilities(graph) if fold(value(graph, f, SCHEMA.name)) == fold(name)]
    result = call_json("explain_provenance", {"facility": name.upper()})
    assert len(named) == 1 and result["iri"] == named[0], (named, result.get("iri"))
    assert result["name"] == name
    assert result["sources"] == [], "directory.ttl has no pipeline graphs, so no source records"

    containing = {str(f) for f in facilities(graph) if "jugend" in fold(value(graph, f, SCHEMA.name))}
    ambiguous = call_json("explain_provenance", {"facility": "Jugend"})
    assert len(containing) > 1 and ambiguous["found"] is False
    candidates = {c["iri"] for c in ambiguous["candidates"]}
    assert candidates <= containing and len(candidates) == min(len(containing), 10)
    assert ambiguous["more_candidates"] == (len(containing) > 10)

    is_error, text = mcp_call("explain_provenance", {"facility": "https://example.org/x"})
    assert is_error and "not a directory IRI" in text, text[:200]


def test_e2e_list_sources_counts_records_and_coverage_from_the_graphs():
    dataset = load_dataset(PIPELINE)
    mapped, federation = dataset.graph(MAPPED), dataset.graph(FEDERATION)
    with serving_named_graphs(PIPELINE):
        result = call_json("list_sources", once=False)

    records = Counter(
        str(federation.value(source, RDFS.label)) for source in mapped.objects(None, CDP.fromSource)
    )
    assert {s["source"]: s["source_records"] for s in result["sources"]} == dict(records)
    assert {s["source"]: s["coverage"] for s in result["sources"]} == {
        str(federation.value(source, RDFS.label)): str(federation.value(source, CDP.coverageLabel))
        for source in set(mapped.objects(None, CDP.fromSource))
    }
    assert all(s["harvested_at"] for s in result["sources"]), result["sources"]


def test_e2e_list_sources_without_pipeline_graphs_is_empty_with_notes():
    result = call_json("list_sources")
    assert result["sources"] == [], "directory.ttl has no pipeline graphs"
    assert result["notes"], "the notes on reading counts and dates must always be there"


# ---- find_help prompt ------------------------------------------------------------

def test_e2e_find_help_prompt_is_listed_and_renders():
    prompts = {p.name: p for p in mcp_list_prompts()}
    assert "find_help" in prompts, f"prompt 'find_help' not listed; listed: {sorted(prompts)}"
    arguments = {a.name: a.required for a in prompts["find_help"].arguments or []}
    assert arguments == {"situation": False, "location": False}, (
        f"find_help should take optional 'situation' and 'location', got {arguments}"
    )

    text = mcp_get_prompt("find_help", {"situation": "Ich trinke zu viel", "location": "13353"})
    for expected in ["Ich trinke zu viel", "13353", "find_nearby", "get_awo_details", "112"]:
        assert expected in text, f"rendered find_help prompt is missing {expected!r}"
    assert "{" not in text.replace("{{", ""), "rendered find_help prompt has an unfilled {placeholder}"

    empty = mcp_get_prompt("find_help", {})
    assert "not given yet" in empty, "find_help without arguments should say the inputs are not given yet"


def test_e2e_find_help_prompt_only_uses_tools_and_arguments_that_exist():
    text = mcp_get_prompt("find_help", {"situation": "Wohnungsverlust", "location": "10115"})
    tools = {tool.name: tool for tool in mcp_list_tools()}
    named_tools = {name for name in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text) if name in tools}
    assert named_tools == {"find_nearby", "get_awo_details"}, named_tools
    nearby_arguments = set(tools["find_nearby"].input_schema["properties"])
    for argument in ["radius_km", "accessible_only"]:
        assert argument in text and argument in nearby_arguments, (
            f"find_help tells the model to set {argument!r}, find_nearby takes {sorted(nearby_arguments)}"
        )
    sample = call_json("get_awo_details", {"iri": CDF + "org-229a4e0b1081"})
    assert "source_url" in text and "source_url" in sample, (
        "find_help refers to source_url, which get_awo_details must return"
    )
